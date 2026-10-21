"""
历史回溯管理接口 /api/backfill/*

| 方法     | 路径                                  | 功能                                   |
|----------|---------------------------------------|----------------------------------------|
| GET      | /api/backfill/accounts                | 批量获取已收藏公众号的回溯状态          |
| GET      | /api/backfill/<fakeid>/status         | 单公众号状态、最近任务、统计            |
| GET      | /api/backfill/<fakeid>/articles       | 已发现 / 已归档文章分页列表             |
| POST     | /api/backfill/<fakeid>/run-once       | 申请执行一轮（异步入队，202 + 任务 ID） |
| POST     | /api/backfill/<fakeid>/pause          | 暂停                                   |
| POST     | /api/backfill/<fakeid>/resume         | 恢复（按原断点继续）                    |
| POST     | /api/backfill/<fakeid>/auto           | 开关该公众号的空闲自动回溯              |
| POST     | /api/backfill/<fakeid>/retry-failed   | 失败文章重新排队                        |
| POST     | /api/backfill/<fakeid>/reset-cursor   | 重置扫描位置（保留归档，需 confirm）     |
| GET/PUT  | /api/backfill/settings                | 全局回溯配置                            |

访问控制：
  - 与项目其他接口一致，默认依赖部署层（只绑 127.0.0.1 + Nginx Basic Auth，见 deploy/README.md）
  - 额外可设环境变量 BACKFILL_ADMIN_TOKEN：设置后所有写操作必须带请求头 X-Admin-Token
  - 写操作只接受 application/json 请求体，所有参数服务端校验
"""

from __future__ import annotations

import hmac
import os

from flask import Blueprint, jsonify, request

from backend import backfill_store as bs
from backend.history_backfill import (
    MAX_SCAN,
    MIN_SCAN,
    backfill_service,
    validate_backfill_settings_update,
)

backfill_bp = Blueprint("backfill", __name__, url_prefix="/api/backfill")


def _service():
    return backfill_service


def _error(message: str, status: int, **extra):
    body = {"error": message}
    body.update(extra)
    return jsonify(body), status


@backfill_bp.before_request
def _guard_writes():
    if request.method not in ("POST", "PUT", "DELETE"):
        return None
    expected = (os.environ.get("BACKFILL_ADMIN_TOKEN") or "").strip()
    if expected:
        supplied = request.headers.get("X-Admin-Token", "")
        if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
            return _error("需要管理令牌（X-Admin-Token）", 401, code="admin_token_required")
    if request.content_length and not request.is_json:
        return _error("请求体必须为 application/json", 415)
    if request.content_length and not isinstance(request.get_json(silent=True), dict):
        return _error("请求体必须是合法 JSON 对象", 400)
    return None


@backfill_bp.before_app_request
def guard_legacy_settings():
    """旧设置入口也必须遵守同一份回溯配置校验和令牌规则。"""
    if request.path != "/api/settings" or request.method != "POST":
        return None
    error = _guard_writes()
    if error:
        return error
    data = _json_body()
    from backend.history_backfill import BACKFILL_SETTING_SPECS
    subset = {k: v for k, v in data.items() if k in BACKFILL_SETTING_SPECS or k.startswith("backfill_")}
    _, errors = validate_backfill_settings_update(subset)
    if errors:
        return _error("配置校验失败", 400, fields=errors)


def _json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _known_account_or_404(fakeid: str):
    if not fakeid or len(fakeid) > 200 or "${" in fakeid:
        return _error("无效的公众号标识", 400)
    if not _service().find_account(fakeid):
        return _error("未找到该公众号，请先收藏", 404)
    return None


@backfill_bp.route("/accounts", methods=["GET"])
def list_account_status():
    return jsonify({"accounts": _service().accounts_status()})


@backfill_bp.route("/<fakeid>/status", methods=["GET"])
def account_status(fakeid):
    err = _known_account_or_404(fakeid)
    if err:
        return err
    return jsonify(_service().status(fakeid))


@backfill_bp.route("/<fakeid>/articles", methods=["GET"])
def account_articles(fakeid):
    err = _known_account_or_404(fakeid)
    if err:
        return err
    try:
        limit = int(request.args.get("limit", "50"))
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        return _error("limit 和 offset 必须为整数", 400)
    status = (request.args.get("status") or "").strip() or None
    if limit is None or offset is None or not (1 <= limit <= 500) or offset < 0:
        return _error("limit 取值 1–500，offset 不能为负", 400)
    if status and status not in (bs.DL_DISCOVERED, bs.DL_PENDING, bs.DL_DOWNLOADED, bs.DL_FAILED, bs.DL_PERMANENT):
        return _error("未知的 status 过滤值", 400)
    rows, total = _service().store.list_articles(fakeid, limit=limit, offset=offset, status=status)
    keep = ("article_key", "title", "link", "publish_time", "download_status", "local_path", "last_error",
            "discovery_source", "first_seen_at")
    return jsonify({"articles": [{k: r.get(k) for k in keep} for r in rows], "total": total,
                    "limit": limit, "offset": offset})


@backfill_bp.route("/<fakeid>/run-once", methods=["POST"])
def run_once(fakeid):
    err = _known_account_or_404(fakeid)
    if err:
        return err
    data = _json_body()
    scan_count = data.get("scan_count")
    if scan_count is not None:
        if isinstance(scan_count, bool) or not isinstance(scan_count, int) or not (MIN_SCAN <= scan_count <= MAX_SCAN):
            return _error(f"scan_count 必须是 {MIN_SCAN}–{MAX_SCAN} 之间的整数", 400)
    res = _service().submit_run(fakeid, scan_count)
    if not res.get("accepted"):
        return jsonify(res), 409
    return jsonify(res), 202


@backfill_bp.route("/<fakeid>/pause", methods=["POST"])
def pause(fakeid):
    err = _known_account_or_404(fakeid)
    if err:
        return err
    return jsonify(_service().pause(fakeid))


@backfill_bp.route("/<fakeid>/resume", methods=["POST"])
def resume(fakeid):
    err = _known_account_or_404(fakeid)
    if err:
        return err
    return jsonify(_service().resume(fakeid))


@backfill_bp.route("/<fakeid>/auto", methods=["POST"])
def set_auto(fakeid):
    err = _known_account_or_404(fakeid)
    if err:
        return err
    enabled = _json_body().get("enabled")
    if not isinstance(enabled, bool):
        return _error("enabled 必须是布尔值", 400)
    return jsonify(_service().set_auto(fakeid, enabled))


@backfill_bp.route("/<fakeid>/retry-failed", methods=["POST"])
def retry_failed(fakeid):
    err = _known_account_or_404(fakeid)
    if err:
        return err
    include_permanent = _json_body().get("include_permanent", False)
    if not isinstance(include_permanent, bool):
        return _error("include_permanent 必须是布尔值", 400)
    return jsonify(_service().retry_failed(fakeid, include_permanent=include_permanent))


@backfill_bp.route("/<fakeid>/reset-cursor", methods=["POST"])
def reset_cursor(fakeid):
    err = _known_account_or_404(fakeid)
    if err:
        return err
    if _json_body().get("confirm") is not True:
        return _error("重置扫描位置需要确认：请求体需包含 {\"confirm\": true}", 400, code="confirm_required")
    try:
        return jsonify(_service().reset_cursor(fakeid))
    except RuntimeError as exc:
        return _error(str(exc), 409)


@backfill_bp.route("/settings", methods=["GET"])
def get_backfill_settings():
    return jsonify({"settings": _service().settings(), "fixed": {"backfill_download_workers": 1}})


@backfill_bp.route("/settings", methods=["PUT"])
def put_backfill_settings():
    from backend.config import get_settings, save_settings

    clean, errors = validate_backfill_settings_update(request.get_json(silent=True))
    if errors:
        return _error("配置校验失败", 400, fields=errors)
    settings = get_settings()
    start = clean.get("backfill_start_hour", settings.get("backfill_start_hour", 0))
    end = clean.get("backfill_end_hour", settings.get("backfill_end_hour", 24))
    if start == end:
        return _error("配置校验失败", 400, fields={"backfill_end_hour": "开始与结束时间不能相同"})
    settings.update(clean)
    save_settings(settings)
    _service().wake()
    return jsonify({"message": "历史回溯配置已保存", "settings": _service().settings()})
