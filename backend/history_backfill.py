"""
公众号历史文章渐进式回溯

  - 手动「获取历史」：每次只提交一个有界批次（扫描 ≤ scan_count 条原始章节 + 下载 ≤ scan_count 篇正文），
    异步入队、幂等；不绕过冷却、额度与更新优先级
  - 空闲自动回溯：默认关闭；开启后按公众号公平轮转，只在允许时段、全局空闲、有严格可用账号、
    额度未用尽时推进
  - 游标：原始章节 offset + 锚点文章 + 有限重叠读取 + 唯一键去重；每页入库 / 入队 / 游标推进同一事务提交
  - 只有上游明确 hasMore=false 的合法响应才标记「已确认历史终点」；连续空页只标记「疑似上游可见边界」
  - 容器重启后过期的 running 租约自动恢复为 queued，单实例租约保证不会并行跑两个回溯任务

数据与状态全部在 backend/backfill_store.py（data/backfill.db）中。
"""

from __future__ import annotations

import datetime
import logging
import math
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from backend import backfill_store as bs
from backend.backfill_upstream import (
    STATUS_AUTH,
    STATUS_BLOCKED,
    STATUS_END,
    STATUS_OK,
    STATUS_TEMPORARY,
    ChapterPage,
    WereadHistoryAdapter,
    short_link_from_review_id,
)
from backend.request_coordinator import (
    PRIORITY_IDLE_BACKFILL,
    PRIORITY_MANUAL_BACKFILL,
    REASON_ACCOUNT_COOLDOWN,
    REASON_MESSAGES,
    REASON_STOPPING,
    REASON_WATERWALL,
    Admission,
    Preempted,
    RequestCoordinator,
    backfill_network,
    request_coordinator,
)

logger = logging.getLogger(__name__)

MAX_PAGE_COUNT = 30            # 单次 mp/chapters 请求的 count 上限
MAX_RELOCATE = 3               # 锚点丢失时最多向前回探的页数
MAX_RUN_RETRIES = 6            # 单个任务因临时错误最多延后重试次数，超过即标记 failed
EMPTY_PAGE_RETRY = 15 * 60     # 首次遇到「空页且无 hasMore=false」时的复查间隔
SUSPECTED_LIMIT_RECHECK = 6 * 3600
LEASE_SECONDS = 120
SCHEDULER_TICK = 15
MIN_SCAN = 5
MAX_SCAN = 30

TRIGGER_MANUAL = "manual"
TRIGGER_IDLE = "idle"

# 下载错误文本中出现这些特征，说明正文抓取本身也撞上了风控，需要整体退避
_RISK_ERROR_MARKERS = ("验证码", "环境异常", "操作频繁", "安全验证", "wappoc_appmsgcaptcha")


# ---------------------------------------------------------------- 设置

BACKFILL_SETTING_SPECS = {
    "backfill_auto_enabled": ("bool", None, None),
    "backfill_scan_batch_size": ("int", MIN_SCAN, MAX_SCAN),
    "backfill_daily_download_limit": ("int", 0, 1000),
    "backfill_min_interval_minutes": ("int", 1, 1440),
    "backfill_download_workers": ("int", 1, 1),
    "backfill_page_overlap": ("int", 1, 10),
    "backfill_rss_guard_seconds": ("int", 0, 3600),
    "backfill_request_interval_seconds": ("float", 2, 300),
    "backfill_max_attempts": ("int", 1, 10),
    "backfill_start_hour": ("int", 0, 23),
    "backfill_end_hour": ("int", 1, 24),
    "rss_include_backfill": ("bool", None, None),
    "backfill_upload_enabled": ("bool", None, None),
}


def _defaults() -> dict:
    from backend.config import DEFAULT_SETTINGS

    return {k: DEFAULT_SETTINGS[k] for k in BACKFILL_SETTING_SPECS}


def _coerce(kind: str, value):
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in ("1", "0", "true", "false", "yes", "no", "on", "off"):
            return value.strip().lower() in ("1", "true", "yes", "on")
        raise ValueError("需要布尔值")
    if isinstance(value, bool):
        raise ValueError("需要数字")
    if kind == "int":
        f = float(value)
        if not math.isfinite(f):
            raise ValueError("需要有限数字")
        if f != int(f):
            raise ValueError("需要整数")
        return int(f)
    f = float(value)
    if not math.isfinite(f):
        raise ValueError("需要有限数字")
    return f


def normalize_backfill_settings(raw: dict | None) -> dict:
    """读取时的防御性归一化：类型不对用默认值，越界夹到范围内。"""
    raw = raw or {}
    out = _defaults()
    for key, (kind, lo, hi) in BACKFILL_SETTING_SPECS.items():
        if key not in raw:
            continue
        try:
            val = _coerce(kind, raw[key])
        except (TypeError, ValueError):
            continue
        if lo is not None:
            val = max(lo, min(hi, val))
        out[key] = val
    return out


def validate_backfill_settings_update(payload) -> tuple[dict, dict]:
    """写入时的严格校验：未知字段、类型错误、越界都返回错误，不做静默修正。"""
    if not isinstance(payload, dict):
        return {}, {"_": "请求体必须是 JSON 对象"}
    clean, errors = {}, {}
    for key, value in payload.items():
        spec = BACKFILL_SETTING_SPECS.get(key)
        if not spec:
            errors[key] = "未知的配置项"
            continue
        kind, lo, hi = spec
        try:
            val = _coerce(kind, value)
        except (TypeError, ValueError) as exc:
            errors[key] = str(exc)
            continue
        if lo is not None and not (lo <= val <= hi):
            errors[key] = f"取值范围 {lo}–{hi}"
            continue
        clean[key] = val
    if not errors:
        merged_start = clean.get("backfill_start_hour")
        merged_end = clean.get("backfill_end_hour")
        if merged_start is not None and merged_end is not None and merged_start == merged_end:
            errors["backfill_end_hour"] = "开始与结束时间不能相同"
    return clean, errors


# ---------------------------------------------------------------- 游标对齐


@dataclass
class Alignment:
    items: list               # 需要处理（入库 / 入队）的文章
    next_offset: int | None   # 新游标；None 表示本页不推进游标（向前回探中）
    action: str               # initial / aligned / anchor_deleted / backward / dedupe
    drift: int = 0


def _matches_anchor(item: dict, state: dict) -> bool:
    key, review_id, normalized = bs.article_identity(item)
    if key and key == state.get("anchor_key"):
        return True
    if review_id and review_id == state.get("anchor_review_id"):
        return True
    return bool(normalized) and normalized == state.get("anchor_url")


def align_page(page: ChapterPage, state: dict, has_anchor: bool) -> Alignment:
    """把一页上游章节与已保存的锚点对齐，处理新增 / 删除导致的 offset 漂移。"""
    items = list(page.articles)
    end = page.offset + page.raw_count
    if not has_anchor:
        return Alignment(items, end, "initial")

    expected = int(state.get("next_offset") or 0) - 1
    for idx, item in enumerate(items):
        if _matches_anchor(item, state):
            return Alignment(items[idx + 1:], end, "aligned", drift=int(item.get("position", 0)) - expected)

    anchor_time = int(state.get("anchor_publish_time") or 0)
    times = [int(i.get("publish_time") or 0) for i in items if int(i.get("publish_time") or 0) > 0]
    if anchor_time and times:
        if min(times) > anchor_time:
            # 整页都比锚点新：头部插入了较多新文章，锚点被挤到更后面 → 继续向后扫描（去重保证不重复）
            return Alignment(items, end, "dedupe")
        if max(times) < anchor_time:
            # 整页都比锚点旧：头部文章被删除，锚点前移；直接推进会留下空档 → 向前回探
            return Alignment(items, None, "backward")
        # 锚点时间落在本页内部：锚点文章本身被删除，从第一篇更旧的文章开始处理
        for idx, item in enumerate(items):
            pt = int(item.get("publish_time") or 0)
            if 0 < pt < anchor_time:
                return Alignment(items[idx:], end, "anchor_deleted")
        return Alignment(items, end, "dedupe")
    return Alignment(items, end, "dedupe")


# ---------------------------------------------------------------- 内部控制流


class _Defer(Exception):
    def __init__(self, state: str, reason: str, retry_at: float, message: str = "", count_retry: bool = False):
        super().__init__(message or reason)
        self.state = state
        self.reason = reason
        self.retry_at = retry_at
        self.message = message or REASON_MESSAGES.get(reason, reason)
        self.count_retry = count_retry


class _Pause(Exception):
    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason
        self.message = message or REASON_MESSAGES.get(reason, reason)


@dataclass
class _RunContext:
    run: dict
    fakeid: str
    nickname: str
    settings: dict
    priority: int
    scan_count: int
    scanned: int = 0
    downloaded: int = 0
    failed: int = 0
    end: bool = False
    suspected: bool = False
    quota_blocked: bool = False
    notes: list = field(default_factory=list)


def _today(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _next_midnight(ts: float) -> float:
    d = datetime.datetime.fromtimestamp(ts).date() + datetime.timedelta(days=1)
    return datetime.datetime.combine(d, datetime.time(0, 0, 5)).timestamp()


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


# ---------------------------------------------------------------- 服务


class HistoryBackfillService:
    def __init__(self, *, store=None, coordinator: RequestCoordinator | None = None, adapter=None,
                 downloader=None, settings_provider=None, accounts_provider=None,
                 history_writer=None, clock=None, owner: str | None = None):
        self._store = store
        self.coordinator = coordinator or request_coordinator
        self.adapter = adapter or WereadHistoryAdapter()
        self._downloader = downloader
        self._settings_provider = settings_provider
        self._accounts_provider = accounts_provider
        self._history_writer = history_writer
        self.clock = clock or time.time
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._stopping = threading.Event()
        self._wake = threading.Event()

    # ── 依赖 ────────────────────────────────────────────

    @property
    def store(self) -> bs.BackfillStore:
        if self._store is None:
            self._store = bs.get_store()
        return self._store

    def settings(self) -> dict:
        if self._settings_provider:
            raw = self._settings_provider()
        else:
            from backend.config import get_settings

            raw = get_settings()
        return normalize_backfill_settings(raw)

    def _raw_settings(self) -> dict:
        if self._settings_provider:
            return self._settings_provider() or {}
        from backend.config import get_settings

        return get_settings()

    def _download(self, link: str, out_dir: Path, title: str, review_id: str) -> dict:
        if self._downloader:
            return self._downloader(link, out_dir, title, review_id=review_id)
        from backend.account_pool import strict_borrow
        from backend.downloader import download_single_article

        # 正文下载内部还会间接借用微信读书账号（免风控正文接口），严格模式保证不借出冷却账号
        with strict_borrow():
            return download_single_article(link, out_dir, title, review_id=review_id)

    def _write_history(self, record: dict):
        if self._history_writer:
            self._history_writer(record)
            return
        from backend.config import append_download_history

        append_download_history([record])

    def favorite_accounts(self) -> list[dict]:
        if self._accounts_provider:
            return list(self._accounts_provider() or [])
        from backend.config import ACCOUNTS_FILE, load_json

        data = load_json(ACCOUNTS_FILE, [])
        return [a for a in data if isinstance(a, dict) and a.get("fakeid")] if isinstance(data, list) else []

    def find_account(self, fakeid: str) -> dict | None:
        for acc in self.favorite_accounts():
            if acc.get("fakeid") == fakeid or acc.get("bookId") == fakeid:
                return acc
        try:
            from backend.rss_scheduler import rss_scheduler

            sub = rss_scheduler.get_subscription(fakeid)
            if sub:
                return {"fakeid": fakeid, "nickname": sub.get("nickname") or fakeid}
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    def _book_id(fakeid: str) -> str:
        from backend.accounts import biz_to_book_id

        return biz_to_book_id(fakeid)

    def _download_dir(self) -> Path:
        from backend.config import OUTPUT_DIR

        raw = self._raw_settings()
        return Path(raw.get("download_dir") or str(OUTPUT_DIR))

    # ── 对外操作（API 调用） ─────────────────────────────

    def submit_run(self, fakeid: str, scan_count: int | None = None, trigger: str = TRIGGER_MANUAL) -> dict:
        """申请执行一轮；异步入队、幂等。返回 API 响应体。"""
        acc = self.find_account(fakeid)
        if not acc:
            raise LookupError("未找到该公众号，请先收藏")
        settings = self.settings()
        scan_count = int(scan_count or settings["backfill_scan_batch_size"])
        scan_count = max(MIN_SCAN, min(MAX_SCAN, scan_count))
        nickname = acc.get("nickname") or fakeid
        state = self.store.ensure_state(fakeid, nickname)
        if state.get("paused"):
            return {
                "accepted": False, "state": bs.RUN_PAUSED, "reason": state.get("pause_reason") or "paused",
                "message": "该公众号的历史回溯已暂停，请先点击「继续」",
            }
        counts = self.store.pending_counts(fakeid)
        if state.get("end_confirmed") and not counts[bs.PENDING]:
            return {
                "accepted": False, "state": bs.RUN_END_CONFIRMED, "reason": "end_confirmed",
                "message": "已确认到达可见历史终点，且没有待下载文章；如需重新扫描请先「重置扫描位置」",
            }

        priority = PRIORITY_MANUAL_BACKFILL if trigger == TRIGGER_MANUAL else PRIORITY_IDLE_BACKFILL
        adm = self.coordinator.check_admission(priority)
        now = self.clock()
        if adm.ok and float(state.get("next_eligible_at") or 0) > now:
            adm = Admission(False, "batch_interval", state["next_eligible_at"], "等待批次间隔或失败复查时间")
        init_state = bs.RUN_QUEUED if adm.ok else adm.run_state
        run, created = self.store.create_run(
            fakeid, trigger, scan_count, state=init_state,
            reason="" if adm.ok else adm.reason, message="" if adm.ok else adm.message,
            retry_at=0 if adm.ok else adm.retry_at, now=now,
        )
        if not created and trigger == TRIGGER_MANUAL and run.get("trigger") != TRIGGER_MANUAL:
            # 已有空闲任务在排队：升级为手动优先级，不新建第二条
            self.store.update_run(run["id"], trigger=TRIGGER_MANUAL, scan_count=max(scan_count, int(run["scan_count"])))
            run = self.store.get_run(run["id"])
        if created:
            self.store.update_state(fakeid, state=init_state, reason=run.get("reason") or "",
                                    next_eligible_at=run.get("retry_at") or 0)
        self._wake.set()
        return self._run_response(run, duplicate=not created)

    def _run_response(self, run: dict, duplicate: bool = False) -> dict:
        state = run.get("state")
        retry_at = float(run.get("retry_at") or 0)
        reason = run.get("reason") or ""
        if state == bs.RUN_QUEUED:
            message = "已提交历史回溯任务，将遵守现有冷却与更新优先级"
            reason = reason or "waiting_for_idle_window"
        elif state == bs.RUN_RUNNING:
            message = "该公众号的历史回溯任务正在执行"
        else:
            base = run.get("message") or REASON_MESSAGES.get(reason, "已排队等待")
            message = f"已排队／冷却中：{base}" + (f"，预计 {_fmt_ts(retry_at)} 后可继续" if retry_at else "")
        if duplicate:
            message = "已有进行中的任务，未重复创建。" + message
        return {
            "accepted": True,
            "duplicate": duplicate,
            "task_id": run["id"],
            "state": state,
            "reason": reason,
            "retry_at": retry_at or None,
            "message": message,
            "scan_count": run.get("scan_count"),
            "trigger": run.get("trigger"),
        }

    def pause(self, fakeid: str) -> dict:
        self._require_account(fakeid)
        self.store.ensure_state(fakeid)
        self.store.update_state(fakeid, paused=1, pause_reason="user_paused", state=bs.RUN_PAUSED, reason="user_paused")
        cancelled = self.store.cancel_active_runs(fakeid, state=bs.RUN_PAUSED, reason="user_paused")
        return {"paused": True, "cancelled_queued": cancelled,
                "message": "已暂停；正在执行的请求会在下一次网络请求前停止并保存进度"}

    def resume(self, fakeid: str) -> dict:
        acc = self._require_account(fakeid)
        state = self.store.ensure_state(fakeid, acc.get("nickname") or "")
        self.store.update_state(fakeid, paused=0, pause_reason="", state="idle", reason="")
        if state.get("pause_reason") == "captcha_required":
            self.coordinator.clear_global_cooldown()
        latest = (self.store.list_runs(fakeid, 1) or [None])[0]
        resumed = None
        if latest and latest.get("state") == bs.RUN_PAUSED:
            # 恢复原任务的扫描和下载预算；不能用扫描剩余额度覆盖尚未完成的下载额度。
            self.store.update_run(latest["id"], state=bs.RUN_QUEUED, reason="resumed",
                                  retry_at=0, finished_at=0, owner="", lease_until=0)
            resumed = self._run_response(self.store.get_run(latest["id"]))
            self.wake()
        return {"paused": False, "resumed_task": resumed, "message": "已恢复，将从原断点继续回溯"}

    def set_auto(self, fakeid: str, enabled: bool) -> dict:
        acc = self._require_account(fakeid)
        self.store.ensure_state(fakeid, acc.get("nickname") or "")
        self.store.update_state(fakeid, enabled=1 if enabled else 0)
        if not enabled:
            self.store.cancel_active_runs(fakeid, state=bs.RUN_CANCELLED, reason="auto_disabled", trigger=TRIGGER_IDLE)
        global_on = self.settings()["backfill_auto_enabled"]
        msg = "已开启该公众号的空闲自动回溯" if enabled else "已关闭该公众号的空闲自动回溯"
        if enabled and not global_on:
            msg += "（全局「空闲自动回溯」开关当前为关闭，需在系统设置中开启后才会自动执行）"
        self._wake.set()
        return {"enabled": bool(enabled), "global_auto_enabled": global_on, "message": msg}

    def retry_failed(self, fakeid: str, include_permanent: bool = False) -> dict:
        self._require_account(fakeid)
        n = self.store.retry_failed(fakeid, include_permanent=include_permanent)
        task = None
        if n:
            task = self.submit_run(fakeid, trigger=TRIGGER_MANUAL)
        return {"requeued": n, "task": task,
                "message": f"已重新排队 {n} 篇失败文章" if n else "没有可重试的失败文章"}

    def reset_cursor(self, fakeid: str) -> dict:
        self._require_account(fakeid)
        active = self.store.get_active_run(fakeid)
        if active and active.get("state") == bs.RUN_RUNNING:
            raise RuntimeError("任务正在执行，请先暂停并等待当前请求结束后再重置")
        self.store.cancel_active_runs(fakeid, state=bs.RUN_CANCELLED, reason="cursor_reset")
        self.store.update_state(
            fakeid, next_offset=0, anchor_key="", anchor_review_id="", anchor_url="", anchor_publish_time=0,
            scanned_count=0, end_confirmed=0, suspected_limit=0, empty_streak=0, error_streak=0,
            state="idle", reason="cursor_reset", next_eligible_at=0,
        )
        return {"reset": True, "message": "扫描位置已重置为最新处；已归档文章与待下载队列均保留"}

    def _require_account(self, fakeid: str) -> dict:
        acc = self.find_account(fakeid)
        if not acc:
            raise LookupError("未找到该公众号，请先收藏")
        return acc

    # ── 状态 ─────────────────────────────────────────────

    def status(self, fakeid: str, include_runs: bool = True) -> dict:
        acc = self._require_account(fakeid)
        store = self.store
        settings = self.settings()
        state = store.get_state(fakeid) or {}
        counts = store.count_articles(fakeid)
        pend = store.pending_counts(fakeid)
        active = store.get_active_run(fakeid)
        now = self.clock()
        earliest_candidates = [t for t in (int(state.get("earliest_publish_time") or 0),
                                           counts["earliest_publish_time"]) if t > 0]
        current_state = (active or {}).get("state") or state.get("state") or "idle"
        reason = (active or {}).get("reason") or state.get("reason") or ""
        try:
            from backend.rss_scheduler import rss_scheduler

            sub = rss_scheduler.get_subscription(fakeid)
        except Exception:  # noqa: BLE001
            sub = None
        out = {
            "fakeid": fakeid,
            "nickname": acc.get("nickname") or state.get("nickname") or fakeid,
            "rss_subscribed": bool(sub and sub.get("enabled")),
            "state": current_state,
            "reason": reason,
            "reason_message": (active or {}).get("message") or REASON_MESSAGES.get(reason, ""),
            "paused": bool(state.get("paused")),
            "pause_reason": state.get("pause_reason") or "",
            "auto_enabled": bool(state.get("enabled")),
            "global_auto_enabled": settings["backfill_auto_enabled"],
            "archived_count": counts["total"],
            "downloaded_count": counts["downloaded"],
            "scanned_count": int(state.get("scanned_count") or 0),
            "next_offset": int(state.get("next_offset") or 0),
            "pending_count": pend[bs.PENDING],
            "failed_count": pend[bs.PENDING_FAILED],
            "permanent_failed_count": pend[bs.PENDING_PERMANENT],
            "earliest_publish_time": min(earliest_candidates) if earliest_candidates else 0,
            "latest_publish_time": counts["latest_publish_time"],
            "end_confirmed": bool(state.get("end_confirmed")),
            "suspected_limit": bool(state.get("suspected_limit")),
            "next_eligible_at": float(state.get("next_eligible_at") or 0),
            "last_run_at": float(state.get("last_run_at") or 0),
            "last_success_at": float(state.get("last_success_at") or 0),
            "last_error": state.get("last_error") or "",
            "daily": {"limit": settings["backfill_daily_download_limit"],
                      "used": store.daily_downloads(fakeid, _today(now))},
            "active_run": active,
            "total_known": None,  # 上游不提供可靠的历史总量，不虚构百分比
        }
        if include_runs:
            out["recent_runs"] = store.list_runs(fakeid, 8)
            out["coordinator"] = self.coordinator.snapshot()
        return out

    def accounts_status(self) -> list[dict]:
        out = []
        for acc in self.favorite_accounts():
            try:
                out.append(self.status(acc["fakeid"], include_runs=False))
            except LookupError:
                continue
        return out

    # ── 调度 ─────────────────────────────────────────────

    def pick_idle_candidate(self, now: float | None = None) -> str | None:
        settings = self.settings()
        if not settings["backfill_auto_enabled"]:
            return None
        now = self.clock() if now is None else now
        favorites = {a["fakeid"] for a in self.favorite_accounts()}
        today = _today(now)
        candidates = []
        for st in self.store.list_states():
            fid = st["fakeid"]
            if fid not in favorites or not st.get("enabled") or st.get("paused"):
                continue
            if float(st.get("next_eligible_at") or 0) > now:
                continue
            if self.store.get_active_run(fid):
                continue
            pending = self.store.pending_counts(fid)[bs.PENDING]
            if st.get("end_confirmed") and not pending:
                continue
            quota_left = settings["backfill_daily_download_limit"] - self.store.daily_downloads(fid, today)
            if quota_left <= 0:
                continue
            candidates.append((float(st.get("last_run_at") or 0), fid))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

    def tick(self) -> bool:
        """调度器单次迭代：执行一个到期任务；没有则按公平轮转挑一个空闲回溯候选。返回是否执行了任务。"""
        if not self.store.try_acquire_lease("backfill_scheduler", self.owner, LEASE_SECONDS, self.clock()):
            return False
        now = self.clock()
        self.recover()
        run = self.store.next_runnable_run(now)
        if run is None:
            fid = self.pick_idle_candidate(now)
            if fid:
                adm = self.coordinator.check_admission(PRIORITY_IDLE_BACKFILL, now)
                if not adm.ok:
                    return False
                acc = self.find_account(fid) or {}
                run, _ = self.store.create_run(fid, TRIGGER_IDLE, self.settings()["backfill_scan_batch_size"], now=now)
                self.store.ensure_state(fid, acc.get("nickname") or "")
        if run is None:
            return False
        # 下载 / 网络阻塞可能长于租约；独立心跳保活，防止第二个进程重复执行。
        done = threading.Event()
        def heartbeat():
            while not done.wait(LEASE_SECONDS / 3):
                self.store.try_acquire_lease("backfill_scheduler", self.owner, LEASE_SECONDS, self.clock())
                self.store.renew_run_lease(run["id"], self.owner, LEASE_SECONDS)
        thread = threading.Thread(target=heartbeat, daemon=True, name="backfill-lease")
        thread.start()
        try:
            self.execute_run(run)
        finally:
            done.set()
            thread.join(timeout=1)
        return True

    def recover(self) -> int:
        if self._history_writer is None:
            self.store.sync_history_file()
        n = self.store.recover_stale_runs(self.owner, self.clock())
        if n:
            logger.info("历史回溯：恢复 %d 个中断的任务", n)
        return n

    # ── 执行 ─────────────────────────────────────────────

    def execute_run(self, run: dict) -> dict:
        store = self.store
        fakeid = run["fakeid"]
        trigger = run.get("trigger") or TRIGGER_MANUAL
        priority = PRIORITY_MANUAL_BACKFILL if trigger == TRIGGER_MANUAL else PRIORITY_IDLE_BACKFILL
        settings = self.settings()
        acc = self.find_account(fakeid)
        now = self.clock()
        if float(run.get("retry_at") or 0) > now:
            return store.get_run(run["id"])
        if not acc:
            store.update_run(run["id"], state=bs.RUN_CANCELLED, reason="account_removed", finished_at=now,
                             message="公众号已取消收藏")
            return store.get_run(run["id"])
        nickname = acc.get("nickname") or fakeid
        state = store.ensure_state(fakeid, nickname)
        if state.get("paused"):
            store.update_run(run["id"], state=bs.RUN_PAUSED, reason=state.get("pause_reason") or "paused",
                             finished_at=now)
            return store.get_run(run["id"])
        if trigger == TRIGGER_IDLE and not (settings["backfill_auto_enabled"] and state.get("enabled")):
            store.update_run(run["id"], state=bs.RUN_CANCELLED, reason="auto_disabled", finished_at=now)
            return store.get_run(run["id"])

        adm = self.coordinator.check_admission(priority, now)
        if not adm.ok:
            self._defer_run(run, _Defer(adm.run_state, adm.reason, adm.retry_at, adm.message))
            return store.get_run(run["id"])
        if not store.claim_run(run["id"], self.owner, LEASE_SECONDS, now):
            return store.get_run(run["id"])

        store.update_state(fakeid, state=bs.RUN_RUNNING, reason="", last_run_at=now)
        ctx = _RunContext(
            run=run, fakeid=fakeid, nickname=nickname, settings=settings, priority=priority,
            scan_count=max(MIN_SCAN, min(MAX_SCAN, int(run.get("scan_count") or settings["backfill_scan_batch_size"]))),
            scanned=int(run.get("scanned") or 0), downloaded=int(run.get("downloaded") or 0),
            failed=int(run.get("failed") or 0),
        )
        try:
            with backfill_network(self.coordinator, priority, self._abort_check(ctx)):
                self._scan(ctx)
                self._download_pending(ctx)
        except Preempted as exc:
            self._handle_preempted(run, exc.admission)
            return store.get_run(run["id"])
        except _Defer as exc:
            self._defer_run(run, exc)
            return store.get_run(run["id"])
        except _Pause as exc:
            self._pause_for(run, exc)
            return store.get_run(run["id"])
        except Exception as exc:  # noqa: BLE001
            logger.exception("历史回溯任务异常 [%s]", nickname)
            end = self.clock()
            store.update_run(run["id"], state=bs.RUN_FAILED, reason="internal_error", message=str(exc)[:300],
                             finished_at=end, owner="", lease_until=0, stop_reason="internal_error")
            store.update_state(fakeid, state=bs.RUN_FAILED, reason="internal_error", last_error=str(exc)[:300],
                               next_eligible_at=end + settings["backfill_min_interval_minutes"] * 60)
            return store.get_run(run["id"])

        self._finish_run(ctx)
        return store.get_run(run["id"])

    # ── 扫描 ─────────────────────────────────────────────

    def _abort_check(self, ctx: _RunContext):
        def check():
            if self._stopping.is_set():
                return REASON_STOPPING
            st = self.store.get_state(ctx.fakeid) or {}
            if st.get("paused"):
                return "user_paused"
            if ctx.priority == PRIORITY_IDLE_BACKFILL and self.store.has_runnable_manual(self.clock()):
                return "foreground_task_running"
            if ctx.priority == PRIORITY_IDLE_BACKFILL and not (st.get("enabled") and self.settings()["backfill_auto_enabled"]):
                return "auto_disabled"
            self.store.renew_run_lease(ctx.run["id"], self.owner, LEASE_SECONDS)
            return None
        return check

    def _slot(self, ctx: _RunContext):
        self.coordinator.acquire_slot(ctx.priority, should_abort=self._abort_check(ctx))

    def _quota_left(self, ctx: _RunContext) -> int:
        used = self.store.daily_downloads(ctx.fakeid, _today(self.clock()))
        return ctx.settings["backfill_daily_download_limit"] - used

    def _scan(self, ctx: _RunContext):
        store = self.store
        remaining = ctx.scan_count - ctx.scanned
        if remaining <= 0:
            return
        state = store.get_state(ctx.fakeid) or {}
        if state.get("end_confirmed"):
            ctx.notes.append("end_confirmed")
            return
        backlog = store.pending_counts(ctx.fakeid)[bs.PENDING]
        if self._quota_left(ctx) <= 0 and backlog >= ctx.scan_count:
            ctx.notes.append("daily_quota_reached")
            ctx.quota_blocked = True
            return
        book_id = self._book_id(ctx.fakeid)
        overlap = int(ctx.settings["backfill_page_overlap"])
        override_start = None
        relocations = 0
        prev_short = False

        while remaining > 0:
            state = store.get_state(ctx.fakeid) or {}
            next_offset = int(state.get("next_offset") or 0)
            has_anchor = bool(state.get("anchor_key")) and next_offset > 0
            if override_start is not None:
                start = override_start
            else:
                start = max(0, next_offset - overlap) if has_anchor else next_offset
            lead = max(0, next_offset - start) if has_anchor and override_start is None else 0
            count = max(1, min(MAX_PAGE_COUNT, lead + remaining))

            self._slot(ctx)
            page = self.adapter.fetch_chapters(book_id, start, count)
            now = self.clock()

            if page.status not in (STATUS_OK, STATUS_END):
                if page.status == STATUS_TEMPORARY and page.reason == "empty_page":
                    streak = int(state.get("empty_streak") or 0) + 1
                    suspected = streak >= 2 or prev_short
                    store.update_state(ctx.fakeid, empty_streak=streak, suspected_limit=1 if suspected else 0,
                                       reason="empty_page")
                    if suspected:
                        ctx.suspected = True
                        ctx.notes.append("suspected_upstream_limit")
                        return
                    raise _Defer(bs.RUN_COOLDOWN, "empty_page", now + EMPTY_PAGE_RETRY,
                                 "上游返回空页但未明确到达末页，稍后复查（不视为历史终点）", count_retry=True)
                self._raise_for_page(ctx, page, state, now)

            self.coordinator.note_upstream_success()
            for art in page.articles:
                if not art.get("link") and art.get("review_id"):
                    self._slot(ctx)
                    art["link"] = self.adapter.resolve_link(art["review_id"]) or ""

            align = align_page(page, state, has_anchor)
            if align.action == "aligned" and not align.items and page.status != STATUS_END:
                # 重叠部分恰好到可见边界时，必须探测游标之后，不能反复读取同一锚点。
                if page.offset < next_offset:
                    override_start = next_offset
                    continue
            if align.action == "backward" and relocations < MAX_RELOCATE and page.offset > 0:
                # 锚点前移（头部文章被删除）：向前回探一页重新定位。回探请求次数受 MAX_RELOCATE 限制，
                # 不占用本批扫描额度，也不提交本页（游标对齐后这些文章会被正常读到）
                relocations += 1
                override_start = max(0, page.offset - count)
                ctx.notes.append("anchor_relocating")
                continue
            if align.action == "backward":
                if page.offset > 0:
                    raise _Pause("anchor_relocation_failed", "列表漂移超出定位范围，请重置扫描位置重新校验；归档已保留")
                align = Alignment(align.items, page.raw_count, "anchor_deleted")
            override_start = None

            if align.action in ("aligned", "anchor_deleted") and align.items:
                first_position = align.items[0]["position"]
                consumed = max(1, page.offset + page.raw_count - first_position)
                allowed_end = first_position + remaining
            else:
                consumed = max(1, page.raw_count)
                allowed_end = page.offset + remaining
            if consumed > remaining:
                # 锚点前移时这一页可能包含超出本批预算的新条目；只提交预算内的游标。
                page.articles = [a for a in page.articles if a["position"] < allowed_end]
                align.items = [a for a in align.items if a["position"] < allowed_end]
                align.next_offset = allowed_end
                page.status = STATUS_OK
                consumed = remaining
            cursor = {
                "next_offset": align.next_offset,
                "empty_streak": 0,
                "error_streak": 0,
                "suspected_limit": 0,
                "reason": "" if align.action in ("initial", "aligned") else align.action,
                "last_error": "",
            }
            anchor = next((a for a in reversed(page.articles) if bs.article_identity(a)[0]), None)
            if anchor:
                key, rid, norm = bs.article_identity(anchor)
                cursor.update(anchor_key=key, anchor_review_id=rid, anchor_url=norm,
                              anchor_publish_time=int(anchor.get("publish_time") or 0))
            times = [int(a.get("publish_time") or 0) for a in page.articles if int(a.get("publish_time") or 0) > 0]
            if times:
                earliest = int(state.get("earliest_publish_time") or 0)
                cursor["earliest_publish_time"] = min([t for t in (earliest, min(times)) if t > 0])
            if page.status == STATUS_END:
                cursor["end_confirmed"] = 1
            self._commit_page(ctx, page, align.items, cursor=cursor, consumed=consumed, now=now)
            remaining -= consumed
            if align.drift:
                ctx.notes.append(f"drift_{align.drift:+d}")
            if page.status == STATUS_END:
                ctx.end = True
                return
            prev_short = page.raw_count < count and page.has_more is None

    def _commit_page(self, ctx: _RunContext, page: ChapterPage, items: list, *, cursor: dict,
                     consumed: int, now: float):
        rows = []
        for art in items:
            if not bs.article_identity(art)[0]:
                continue
            row = dict(art)
            row["account_name"] = ctx.nickname
            row["position_hint"] = int(art.get("position", 0))
            rows.append(row)
        self.store.commit_scan_page(
            ctx.fakeid, articles=rows, cursor=cursor, run_id=ctx.run["id"], scanned_inc=consumed,
            feed_visible=False, enqueue=True, now=now,
        )
        ctx.scanned += consumed

    def _raise_for_page(self, ctx: _RunContext, page: ChapterPage, state: dict, now: float):
        self.store.update_state(ctx.fakeid, error_streak=int(state.get("error_streak") or 0) + 1,
                                last_error=f"{page.status}:{page.reason}")
        if page.status == STATUS_BLOCKED:
            if page.reason == "captcha":
                raise _Pause("captcha_required", REASON_MESSAGES[REASON_WATERWALL])
            if page.reason == "no_available_account":
                raise _Defer(bs.RUN_COOLDOWN, REASON_ACCOUNT_COOLDOWN, now + 300,
                             REASON_MESSAGES[REASON_ACCOUNT_COOLDOWN])
            until = self.coordinator.note_upstream_restriction(page.reason, page.retry_after)
            raise _Defer(bs.RUN_COOLDOWN, page.reason, until,
                         f"上游限制（{page.reason}，HTTP {page.http_status}），历史回溯全局冷却至 {_fmt_ts(until)}")
        if page.status == STATUS_AUTH:
            raise _Defer(bs.RUN_COOLDOWN, "auth_error", now + 600,
                         "微信读书登录态失效（401），已交由账号池处理，10 分钟后重试", count_retry=True)
        retries = int(ctx.run.get("retries") or 0)
        backoff = min(3600, 120 * (2 ** retries))
        raise _Defer(bs.RUN_COOLDOWN, page.reason or "temporary_error", now + backoff,
                     f"临时错误（{page.reason or 'unknown'}），{int(backoff // 60)} 分钟后重试；不推进游标",
                     count_retry=True)

    # ── 下载 ─────────────────────────────────────────────

    def _download_pending(self, ctx: _RunContext):
        store = self.store
        per_batch = ctx.scan_count - (ctx.downloaded + ctx.failed)
        quota = self._quota_left(ctx)
        limit = min(per_batch, quota)
        if quota <= 0 and store.pending_counts(ctx.fakeid)[bs.PENDING]:
            ctx.quota_blocked = True
            if "daily_quota_reached" not in ctx.notes:
                ctx.notes.append("daily_quota_reached")
        if limit <= 0:
            return
        from backend.downloader import sanitize
        out_dir = self._download_dir() / sanitize(ctx.nickname)
        for item in store.next_pending(ctx.fakeid, limit, self.clock()):
            archived = store.get_article(ctx.fakeid, item["article_key"])
            if archived and archived["download_status"] == bs.DL_DOWNLOADED:
                store.mark_pending_done(ctx.fakeid, item["article_key"], count_quota=False)
                continue
            self._slot(ctx)
            self._download_one(ctx, item, out_dir)

    def _download_one(self, ctx: _RunContext, item: dict, out_dir: Path):
        store = self.store
        settings = ctx.settings
        review_id = item.get("review_id") or ""
        link = item.get("link") or short_link_from_review_id(review_id)
        title = item.get("title") or "article"
        day = _today(self.clock())
        if not link and not review_id:
            store.mark_pending_failed(ctx.fakeid, item["article_key"], error="缺少文章链接", permanent=True,
                                      max_attempts=1, retry_delay=0, run_id=ctx.run["id"])
            ctx.failed += 1
            return
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            result = self._download(link, out_dir, title, review_id) or {}
        except Exception as exc:  # noqa: BLE001
            result = {"success": False, "error": str(exc), "is_permanent": False}

        if result.get("success"):
            path = result.get("path") or ""
            record = {
                "title": result.get("title") or title,
                "link": link,
                "account": ctx.nickname,
                "author": result.get("author", ""),
                "success": True,
                "time": time.time(),
                "error": None,
                "path": path,
                "cover_url": result.get("cover_url") or item.get("cover") or "",
                "digest": result.get("digest") or item.get("digest") or "",
                "publish_time": item.get("publish_time") or result.get("publish_time") or 0,
                "has_markdown": result.get("has_markdown", False),
                "has_pdf": result.get("has_pdf", False),
                "backfill": True,
                "fakeid": ctx.fakeid,
                "review_id": review_id,
            }
            # 先写下载历史再出队：即使两步之间进程退出，也只会在恢复后被识别为已下载，不会丢文件索引
            self._write_history(record)
            store.mark_pending_done(
                ctx.fakeid, item["article_key"], local_path=path, run_id=ctx.run["id"], day=day,
                article_updates={"cover": record["cover_url"], "digest": record["digest"], "author": record["author"]},
            )
            ctx.downloaded += 1
            return

        error = str(result.get("error") or "下载失败")
        attempts = int(item.get("attempts") or 0)
        retry_delay = min(24 * 3600, 600 * (2 ** attempts))
        store.mark_pending_failed(
            ctx.fakeid, item["article_key"], error=error, permanent=bool(result.get("is_permanent")),
            max_attempts=settings["backfill_max_attempts"], retry_delay=retry_delay, run_id=ctx.run["id"], day=day,
        )
        ctx.failed += 1
        if any(m in error for m in _RISK_ERROR_MARKERS):
            until = self.coordinator.note_upstream_restriction("article_risk_control")
            raise _Defer(bs.RUN_COOLDOWN, "article_risk_control", until,
                         f"正文抓取遇到风控（{error[:60]}），历史回溯全局冷却至 {_fmt_ts(until)}")

    # ── 结束 / 延后 / 暂停 ───────────────────────────────

    def _finish_run(self, ctx: _RunContext):
        store = self.store
        now = self.clock()
        state = store.get_state(ctx.fakeid) or {}
        pending = store.pending_counts(ctx.fakeid)[bs.PENDING]
        if state.get("end_confirmed") and not pending:
            final, reason = bs.RUN_END_CONFIRMED, "end_confirmed"
        else:
            final = bs.RUN_COMPLETED_BATCH
            reason = "suspected_upstream_limit" if ctx.suspected else (
                "daily_quota_reached" if ctx.quota_blocked else "batch_done")
        interval = ctx.settings["backfill_min_interval_minutes"] * 60
        if ctx.suspected:
            next_eligible = now + max(interval, SUSPECTED_LIMIT_RECHECK)
        elif ctx.quota_blocked:
            next_eligible = max(now + interval, _next_midnight(now))
        else:
            next_eligible = now + interval
            retry_at = store.next_pending_retry_at(ctx.fakeid)
            if state.get("end_confirmed") and retry_at:
                next_eligible = max(next_eligible, retry_at)
        notes = ",".join(dict.fromkeys(ctx.notes))
        store.update_run(ctx.run["id"], state=final, reason=reason, finished_at=now, owner="", lease_until=0,
                         stop_reason=notes, message=self._summary(ctx, final))
        store.update_state(ctx.fakeid, state=final, reason=reason, next_eligible_at=next_eligible, last_run_at=now)
        logger.info("历史回溯批次完成 [%s]: %s", ctx.nickname, self._summary(ctx, final))

    @staticmethod
    def _summary(ctx: _RunContext, final: str) -> str:
        parts = [f"扫描 {ctx.scanned} 条", f"下载成功 {ctx.downloaded} 篇"]
        if ctx.failed:
            parts.append(f"失败 {ctx.failed} 次")
        if final == bs.RUN_END_CONFIRMED:
            parts.append("已确认历史终点")
        elif ctx.suspected:
            parts.append("疑似上游可见边界，尚未确认历史终点")
        if ctx.quota_blocked:
            parts.append("今日下载额度已用尽")
        return "，".join(parts)

    def _defer_run(self, run: dict, d: _Defer):
        store = self.store
        retries = int((store.get_run(run["id"]) or run).get("retries") or 0) + (1 if d.count_retry else 0)
        settings = self.settings()
        if retries > MAX_RUN_RETRIES:
            now = self.clock()
            store.update_run(run["id"], state=bs.RUN_FAILED, reason=d.reason, message=f"多次重试仍失败：{d.message}",
                             finished_at=now, owner="", lease_until=0, retries=retries, stop_reason=d.reason)
            store.update_state(run["fakeid"], state=bs.RUN_FAILED, reason=d.reason, last_error=d.message,
                               next_eligible_at=now + settings["backfill_min_interval_minutes"] * 60)
            return
        store.update_run(run["id"], state=d.state, reason=d.reason, message=d.message, retry_at=d.retry_at,
                         owner="", lease_until=0, retries=retries, stop_reason=d.reason)
        store.update_state(run["fakeid"], state=d.state, reason=d.reason, next_eligible_at=d.retry_at)

    def _handle_preempted(self, run: dict, adm: Admission):
        store = self.store
        now = self.clock()
        if adm.reason == "user_paused":
            store.update_run(run["id"], state=bs.RUN_PAUSED, reason="user_paused", message="用户暂停，进度已保存",
                             finished_at=now, owner="", lease_until=0, stop_reason="user_paused")
            store.update_state(run["fakeid"], state=bs.RUN_PAUSED, reason="user_paused")
            return
        if adm.reason == REASON_WATERWALL:
            self._pause_for(run, _Pause(REASON_WATERWALL, adm.message))
            return
        if adm.reason == "auto_disabled":
            store.update_run(run["id"], state=bs.RUN_CANCELLED, reason="auto_disabled",
                             finished_at=now, owner="", lease_until=0)
            store.update_state(run["fakeid"], state="idle", reason="auto_disabled")
            return
        if adm.reason == REASON_STOPPING:
            store.update_run(run["id"], state=bs.RUN_QUEUED, reason="recovered_after_restart",
                             message="服务停止，重启后按原断点继续", retry_at=0, owner="", lease_until=0)
            store.update_state(run["fakeid"], state=bs.RUN_QUEUED, reason="service_stopping")
            return
        self._defer_run(run, _Defer(adm.run_state, adm.reason, adm.retry_at or now + 60, adm.message))

    def _pause_for(self, run: dict, p: _Pause):
        store = self.store
        now = self.clock()
        store.update_run(run["id"], state=bs.RUN_PAUSED, reason=p.reason, message=p.message, finished_at=now,
                         owner="", lease_until=0, stop_reason=p.reason)
        store.update_state(run["fakeid"], paused=1, pause_reason=p.reason, state=bs.RUN_PAUSED, reason=p.reason,
                           last_error=p.message)

    # ── 生命周期 ─────────────────────────────────────────

    def wake(self):
        self._wake.set()

    def wait_for_wake(self, timeout: float):
        self._wake.wait(timeout)
        self._wake.clear()

    def stop(self):
        self._stopping.set()
        self._wake.set()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()


# ---------------------------------------------------------------- 外部下载登记


def record_external_download(account_name: str, article: dict, result: dict, source: str = "manual"):
    """手动下载 / RSS 最新同步完成后登记到归档索引；若该文章正排在回溯下载队列里则直接出队，避免重复下载。

    任何异常只记日志，不影响原下载流程。
    """
    try:
        if not account_name or account_name in ("url_download", "链接下载", "微信视频号"):
            return
        store = bs.get_store()
        fakeid = bs.fakeid_for_account(account_name)
        success = bool(result.get("success"))
        art = {
            "id": article.get("id") or article.get("aid") or "",
            "link": article.get("original_url") or article.get("link") or "",
            "title": result.get("title") or article.get("title") or "",
            "account_name": account_name,
            "author": result.get("author") or "",
            "digest": result.get("digest") or article.get("digest") or "",
            "cover": result.get("cover_url") or article.get("cover") or "",
            "publish_time": article.get("update_time") or result.get("publish_time") or 0,
        }
        status = bs.DL_DOWNLOADED if success else (bs.DL_PERMANENT if result.get("is_permanent") else bs.DL_FAILED)
        key, _ = store.upsert_article(
            fakeid, art, source=source, feed_visible=True, download_status=status,
            local_path=(result.get("path") or "") if success else "",
            last_error=None if success else str(result.get("error") or ""),
        )
        if success and key:
            store.mark_pending_done(fakeid, key, local_path=result.get("path") or "", count_quota=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("登记外部下载到归档索引失败: %s", exc)


# ---------------------------------------------------------------- 调度线程


class BackfillScheduler:
    def __init__(self, service: HistoryBackfillService | None = None):
        self.service = service or HistoryBackfillService()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.service._stopping.clear()
            self.service.coordinator.reset_stop()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="history-backfill")
            self._thread.start()

    def _loop(self):
        logger.info("历史回溯调度器已启动 (owner=%s)", self.service.owner)
        recovered = False
        while not self.service.stopping:
            try:
                if not recovered and self.service.store.try_acquire_lease(
                        "backfill_scheduler", self.service.owner, LEASE_SECONDS):
                    self.service.recover()
                    recovered = True
                if recovered and self.service.tick():
                    continue  # 刚执行完一个任务，立即看看是否还有到期任务
            except Exception as exc:  # noqa: BLE001
                logger.error("历史回溯调度器异常: %s", exc)
            self.service.wait_for_wake(SCHEDULER_TICK)
        logger.info("历史回溯调度器已停止")
        self.service.store.release_lease("backfill_scheduler", self.service.owner)

    def stop(self, timeout: float = 5):
        self.service.stop()
        self.service.coordinator.stop()
        if self._thread:
            self._thread.join(timeout=timeout)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


backfill_service = HistoryBackfillService()
backfill_scheduler = BackfillScheduler(backfill_service)
