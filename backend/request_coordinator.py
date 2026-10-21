"""
公众号上游请求协调器

统一回答一个问题：「低优先级的历史回溯现在能不能向微信读书 / 微信发请求？」

优先级（数字越小越高）：
  0  前台任务：手动文章下载 / 文章列表浏览 / 到期的 RSS 最新同步
  1  手动「获取历史」单批任务
  2  空闲自动回溯

前台任务沿用各自原有的节奏，不受本模块限速；它们只需用 foreground() 登记自己正在运行，
历史回溯就会在下一次网络请求 / 下一篇文章之前让路（不会打断正在写入的文章）。

历史回溯的每一次上游请求（章节分页、review/single 链接解析、正文下载）都必须先调用
acquire_slot()：它负责请求间隔、准入检查（RSS 运行中或即将到期、防水墙、全局冷却、
严格可用账号、允许时段），不满足时抛出 Preempted，调用方保存进度后挂起。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

logger = logging.getLogger(__name__)

PRIORITY_FOREGROUND = 0
PRIORITY_MANUAL_BACKFILL = 1
PRIORITY_IDLE_BACKFILL = 2

# 准入失败原因 → 任务状态
REASON_FOREGROUND = "foreground_task_running"
REASON_RSS_RUNNING = "rss_update_running"
REASON_RSS_DUE = "rss_update_due_soon"
REASON_WATERWALL = "captcha_required"
REASON_GLOBAL_COOLDOWN = "global_cooldown"
REASON_ACCOUNT_COOLDOWN = "account_cooldown"
REASON_NO_ACCOUNT = "no_available_account"
REASON_OUTSIDE_WINDOW = "outside_allowed_hours"
REASON_INTERVAL = "request_interval"
REASON_STOPPING = "stopping"

PRIORITY_WAIT_REASONS = {REASON_FOREGROUND, REASON_RSS_RUNNING, REASON_RSS_DUE}

REASON_MESSAGES = {
    REASON_FOREGROUND: "有手动下载 / 文章浏览任务正在进行，历史回溯让路等待",
    REASON_RSS_RUNNING: "RSS 最新文章更新正在进行，历史回溯让路等待",
    REASON_RSS_DUE: "RSS 最新文章更新即将到期，历史回溯暂缓",
    REASON_WATERWALL: "检测到腾讯防水墙验证码拦截，历史回溯已暂停，请先完成人工验证",
    REASON_GLOBAL_COOLDOWN: "上游返回限流 / 风控，历史回溯处于全局冷却",
    REASON_ACCOUNT_COOLDOWN: "账号池中所有账号均在冷却，等待冷却结束",
    REASON_NO_ACCOUNT: "账号池中没有可用的微信读书账号",
    REASON_OUTSIDE_WINDOW: "当前不在允许的历史回溯时段内",
    REASON_STOPPING: "服务正在停止",
}

MAX_GLOBAL_COOLDOWN = 6 * 3600
BASE_GLOBAL_COOLDOWN = 10 * 60


@dataclass
class Admission:
    ok: bool
    reason: str = ""
    retry_at: float = 0.0
    message: str = ""

    @property
    def run_state(self) -> str:
        if self.ok:
            return "queued"
        return "waiting_priority" if self.reason in PRIORITY_WAIT_REASONS else "cooldown"

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "retry_at": self.retry_at,
                "message": self.message, "state": self.run_state}


class Preempted(BaseException):
    """协作式取消；绕过旧下载器的 except Exception，交由 Worker 保存进度。"""

    def __init__(self, admission: Admission):
        super().__init__(admission.message or admission.reason)
        self.admission = admission


def _safe_int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _default_settings() -> dict:
    from backend.config import get_settings

    return get_settings()


def _default_rss_probe() -> dict:
    """{"running": bool, "next_due_in": float | None}"""
    try:
        from backend.rss_scheduler import rss_scheduler

        return {"running": rss_scheduler.is_fetching(), "next_due_in": rss_scheduler.seconds_until_next_due()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("RSS 状态探测失败: %s", exc)
        return {"running": False, "next_due_in": None}


def _default_account_probe() -> dict:
    try:
        from backend.account_pool import account_pool

        return account_pool.strict_availability()
    except Exception as exc:  # noqa: BLE001
        logger.debug("账号池探测失败: %s", exc)
        return {"available": False, "reason": "no_account", "next_available_at": None}


def _default_waterwall_probe() -> dict:
    try:
        from backend.waterwall import WATERWALL, is_waterwall_active

        if is_waterwall_active():
            return {"active": True, "blocked_until": float(WATERWALL.get("blocked_until") or 0)}
    except Exception as exc:  # noqa: BLE001
        logger.debug("防水墙状态探测失败: %s", exc)
    return {"active": False, "blocked_until": 0.0}


class RequestCoordinator:
    def __init__(self, *, settings_provider=None, rss_probe=None, account_probe=None,
                 waterwall_probe=None, clock=None, now_struct=None, state_store=None):
        self._settings_provider = settings_provider or _default_settings
        self._rss_probe = rss_probe or _default_rss_probe
        self._account_probe = account_probe or _default_account_probe
        self._waterwall_probe = waterwall_probe or _default_waterwall_probe
        self._clock = clock or time.time
        self._now_struct = now_struct or (lambda ts: time.localtime(ts))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._foreground: dict[str, int] = {}
        self._foreground_ended_at = 0.0
        self._last_backfill_request_at = 0.0
        self._global_cooldown_until = 0.0
        self._global_cooldown_reason = ""
        self._restriction_streak = 0
        self._state_store = state_store
        self._persist = state_store is not None or settings_provider is None

    def _cooldown_store(self):
        if self._state_store is not None:
            return self._state_store
        if self._persist:
            from backend.backfill_store import get_store
            return get_store()
        return None

    # ── 前台任务登记 ─────────────────────────────────────

    @contextmanager
    def foreground(self, name: str = "task"):
        """前台任务（手动下载、文章浏览、RSS 最新同步）运行期间，历史回溯让路。"""
        with self._lock:
            self._foreground[name] = self._foreground.get(name, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                left = self._foreground.get(name, 1) - 1
                if left <= 0:
                    self._foreground.pop(name, None)
                else:
                    self._foreground[name] = left
                self._foreground_ended_at = self._clock()

    def foreground_active(self) -> bool:
        with self._lock:
            return bool(self._foreground)

    # ── 全局冷却 ─────────────────────────────────────────

    def note_upstream_restriction(self, reason: str, retry_after: float | None = None) -> float:
        """历史回溯遇到 403/429/499 等限流：指数退避的全局冷却，返回冷却结束时间。"""
        now = self._clock()
        with self._lock:
            self._restriction_streak += 1
            backoff = min(MAX_GLOBAL_COOLDOWN, BASE_GLOBAL_COOLDOWN * (2 ** (self._restriction_streak - 1)))
            if retry_after:
                backoff = max(backoff, float(retry_after))
            self._global_cooldown_until = max(self._global_cooldown_until, now + backoff)
            self._global_cooldown_reason = reason
            store = self._cooldown_store()
            if store is not None:
                store.set_meta("global_cooldown", {"until": self._global_cooldown_until, "reason": reason,
                                                   "streak": self._restriction_streak})
            logger.warning("历史回溯触发上游限制 (%s)，全局冷却 %.0f 秒", reason, backoff)
            return self._global_cooldown_until

    def note_upstream_success(self):
        with self._lock:
            self._restriction_streak = 0

    def clear_global_cooldown(self):
        with self._lock:
            self._global_cooldown_until = 0.0
            self._global_cooldown_reason = ""
            self._restriction_streak = 0
            store = self._cooldown_store()
            if store is not None:
                store.delete_meta("global_cooldown")

    def global_cooldown(self) -> tuple[float, str]:
        with self._lock:
            store = self._cooldown_store()
            saved = store.get_meta("global_cooldown", {}) if store is not None else {}
            if saved.get("until", 0) > self._global_cooldown_until:
                self._global_cooldown_until = saved["until"]
                self._global_cooldown_reason = saved["reason"]
                self._restriction_streak = saved.get("streak", 1)
            if self._global_cooldown_until <= self._clock():
                return 0.0, ""
            return self._global_cooldown_until, self._global_cooldown_reason

    # ── 设置 ─────────────────────────────────────────────

    def _settings(self) -> dict:
        try:
            return self._settings_provider() or {}
        except Exception:  # noqa: BLE001
            return {}

    def request_interval(self, settings: dict | None = None) -> float:
        settings = settings if settings is not None else self._settings()
        return max(0.0, _safe_float(settings.get("backfill_request_interval_seconds"), 5.0))

    def _window(self, settings: dict) -> tuple[int, int]:
        start = max(0, min(23, _safe_int(settings.get("backfill_start_hour"), 0)))
        end = max(1, min(24, _safe_int(settings.get("backfill_end_hour"), 24)))
        return start, end

    def in_allowed_window(self, now: float | None = None, settings: dict | None = None) -> bool:
        settings = settings if settings is not None else self._settings()
        start, end = self._window(settings)
        if start == 0 and end == 24:
            return True
        hour = self._now_struct(self._clock() if now is None else now).tm_hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _next_window_start(self, now: float, settings: dict) -> float:
        start, _ = self._window(settings)
        ts = now
        for _ in range(48):  # 最多向后查 48 个整点
            ts = (int(ts // 3600) + 1) * 3600
            if self.in_allowed_window(ts, settings) and self._now_struct(ts).tm_hour == start:
                return float(ts)
        return now + 3600

    # ── 准入判断 ─────────────────────────────────────────

    def check_admission(self, priority: int, now: float | None = None) -> Admission:
        now = self._clock() if now is None else now
        if priority <= PRIORITY_FOREGROUND:
            return Admission(True)
        if self._stop.is_set():
            return Admission(False, REASON_STOPPING, now + 60, REASON_MESSAGES[REASON_STOPPING])
        settings = self._settings()

        ww = self._waterwall_probe() or {}
        if ww.get("active"):
            until = max(_safe_float(ww.get("blocked_until"), 0), now + 60)
            return Admission(False, REASON_WATERWALL, until, REASON_MESSAGES[REASON_WATERWALL])

        until, reason = self.global_cooldown()
        if until > now:
            return Admission(False, REASON_GLOBAL_COOLDOWN, until,
                             f"{REASON_MESSAGES[REASON_GLOBAL_COOLDOWN]}（{reason}）")

        if not self.in_allowed_window(now, settings):
            nxt = self._next_window_start(now, settings)
            return Admission(False, REASON_OUTSIDE_WINDOW, nxt, REASON_MESSAGES[REASON_OUTSIDE_WINDOW])

        if self.foreground_active():
            return Admission(False, REASON_FOREGROUND, now + 30, REASON_MESSAGES[REASON_FOREGROUND])

        rss = self._rss_probe() or {}
        if rss.get("running"):
            return Admission(False, REASON_RSS_RUNNING, now + 30, REASON_MESSAGES[REASON_RSS_RUNNING])
        guard = max(0.0, _safe_float(settings.get("backfill_rss_guard_seconds"), 120))
        due_in = rss.get("next_due_in")
        if due_in is not None and guard > 0 and float(due_in) <= guard:
            # 等 RSS 这一轮跑完再来：到期时间之后留 60 秒余量
            retry = now + max(30.0, float(due_in) + 60.0)
            return Admission(False, REASON_RSS_DUE, retry, REASON_MESSAGES[REASON_RSS_DUE])

        acc = self._account_probe() or {}
        if not acc.get("available"):
            if acc.get("reason") == "account_cooldown" and acc.get("next_available_at"):
                return Admission(False, REASON_ACCOUNT_COOLDOWN, float(acc["next_available_at"]) + 5,
                                 REASON_MESSAGES[REASON_ACCOUNT_COOLDOWN])
            return Admission(False, REASON_NO_ACCOUNT, now + 600, REASON_MESSAGES[REASON_NO_ACCOUNT])

        # 前台任务刚结束：留一个请求间隔的缓冲，避免与其尾部请求贴得太近
        interval = self.request_interval(settings)
        with self._lock:
            fg_end = self._foreground_ended_at
        if fg_end and now - fg_end < interval:
            return Admission(False, REASON_FOREGROUND, fg_end + interval, REASON_MESSAGES[REASON_FOREGROUND])
        return Admission(True)

    def acquire_slot(self, priority: int, *, should_abort=None, sleep=None) -> None:
        """历史回溯每次上游请求前调用：先满足最小请求间隔（带随机抖动），再做准入检查。

        不满足准入时抛出 Preempted；should_abort() 返回真值（如用户暂停）时抛出 Preempted(paused)。
        """
        sleep = sleep or self._wait
        settings = self._settings()
        interval = self.request_interval(settings)
        while True:
            if should_abort:
                abort_reason = should_abort()
                if abort_reason:
                    raise Preempted(Admission(False, str(abort_reason), self._clock(), "任务已被暂停"))
            adm = self.check_admission(priority)
            if not adm.ok:
                raise Preempted(adm)
            with self._lock:
                now = self._clock()
                wait = self._last_backfill_request_at + interval - now
                if wait <= 0:
                    jitter = random.uniform(0, interval * 0.3) if interval > 0 else 0.0
                    self._last_backfill_request_at = now + jitter
                    break
            sleep(min(wait, 5.0))
        # 抖动部分在锁外等待，不阻塞其他线程查询状态
        with self._lock:
            extra = self._last_backfill_request_at - self._clock()
        if extra > 0:
            sleep(extra)
        if should_abort and (reason := should_abort()):
            raise Preempted(Admission(False, str(reason), self._clock(), "任务已被暂停"))
        admission = self.check_admission(priority)
        if not admission.ok:
            raise Preempted(admission)

    def _wait(self, seconds: float):
        self._stop.wait(max(0.0, seconds))

    def stop(self):
        self._stop.set()

    def reset_stop(self):
        self._stop.clear()

    # ── 状态快照 ─────────────────────────────────────────

    def snapshot(self) -> dict:
        now = self._clock()
        until, reason = self.global_cooldown()
        rss = self._rss_probe() or {}
        ww = self._waterwall_probe() or {}
        with self._lock:
            fg = sorted(self._foreground)
        return {
            "foreground_tasks": fg,
            "rss_running": bool(rss.get("running")),
            "rss_next_due_in": rss.get("next_due_in"),
            "waterwall_active": bool(ww.get("active")),
            "global_cooldown_until": until,
            "global_cooldown_reason": reason,
            "in_allowed_window": self.in_allowed_window(now),
            "idle_admission": self.check_admission(PRIORITY_IDLE_BACKFILL, now).as_dict(),
        }


# 全局单例
request_coordinator = RequestCoordinator()


# 线程上下文覆盖整个正文下载调用链，包含链接解析、重定向和媒体请求。
_network_local = threading.local()


@contextmanager
def backfill_network(coordinator, priority, should_abort):
    old = getattr(_network_local, "context", None)
    _network_local.context = (coordinator, priority, should_abort)
    try:
        yield
    finally:
        _network_local.context = old


def in_backfill_network():
    return getattr(_network_local, "context", None) is not None


import requests


class CoordinatedSession(requests.Session):
    def send(self, request, **kwargs):
        context = getattr(_network_local, "context", None)
        if context:
            coordinator, priority, abort = context
            coordinator.acquire_slot(priority, should_abort=abort)
        response = super().send(request, **kwargs)
        if context:
            from backend.waterwall import detect_waterwall

            content_type = response.headers.get("Content-Type", "")
            body = response.text if ("text" in content_type or "json" in content_type) else ""
            captcha = detect_waterwall(response.status_code, body) or any(
                marker in body for marker in ("wappoc_appmsgcaptcha", "当前环境异常", "安全验证")
            )
            if captcha:
                coordinator.note_upstream_restriction(REASON_WATERWALL)
                raise Preempted(Admission(False, REASON_WATERWALL, 0, REASON_MESSAGES[REASON_WATERWALL]))
            if response.status_code in (401, 403, 429, 499) or any(
                marker in body for marker in ('-2041', '操作频繁')
            ):
                retry = _safe_float(response.headers.get("Retry-After"), 0)
                until = coordinator.note_upstream_restriction(f"http_{response.status_code}", retry)
                from urllib.parse import urlparse
                target = urlparse(response.url or request.url)
                logger.warning("历史请求受限: http=%s url=%s://%s%s",
                               response.status_code, target.scheme, target.hostname, target.path)
                raise Preempted(Admission(False, REASON_GLOBAL_COOLDOWN, until,
                                         f"上游限制（HTTP {response.status_code}），已停止后续请求"))
        return response


def coordinated_get(url, **kwargs):
    if not in_backfill_network():
        return requests.get(url, **kwargs)
    with CoordinatedSession() as session:
        return session.get(url, **kwargs)
