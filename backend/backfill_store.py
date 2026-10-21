"""
历史文章归档索引与渐进回溯进度库（SQLite，WAL 模式）

数据文件：data/backfill.db（与其他数据同在 /app/data 卷内，无需新增 Docker 卷）

表结构概览：
  archive_articles   公众号文章元数据索引，不设任何固定篇数上限（唯一键 fakeid + article_key）
  backfill_states    每个公众号的回溯游标、锚点、统计与暂停状态
  backfill_pending   待下载 / 失败重试队列（唯一键 fakeid + article_key，保证同一篇不重复排队）
  backfill_runs      每一轮回溯任务（手动 / 空闲自动）的运行记录；部分唯一索引保证
                     同一公众号同一时刻只有一条活跃任务
  backfill_daily     每日下载额度计数
  meta               迁移标记、调度器租约、Feed 版本号等

文章唯一键优先级：稳定 reviewId > 归一化微信文章 URL > 标题 + 发布时间组合（仅在前两者都缺失时）。
绝不只按「公众号名称 + 标题」去重。

旧数据（rss_articles.json / download_history.json）只读导入、先备份、幂等，可重复执行。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from backend.config import (
    ACCOUNTS_FILE,
    DATA_DIR,
    DOWNLOAD_HISTORY_FILE,
    load_json,
    normalize_wechat_url,
)

logger = logging.getLogger(__name__)

BACKFILL_DB_FILE = DATA_DIR / "backfill.db"
RSS_ARTICLES_FILE = DATA_DIR / "rss_articles.json"
RSS_SUBSCRIPTIONS_FILE = DATA_DIR / "rss_subscriptions.json"

SCHEMA_VERSION = 1

# 任务状态枚举（与 docs/history_backfill.md、前端展示保持一致）
RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_WAITING_PRIORITY = "waiting_priority"
RUN_COOLDOWN = "cooldown"
RUN_PAUSED = "paused"
RUN_COMPLETED_BATCH = "completed_batch"
RUN_END_CONFIRMED = "end_confirmed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"

ACTIVE_RUN_STATES = (RUN_QUEUED, RUN_RUNNING, RUN_WAITING_PRIORITY, RUN_COOLDOWN)
RUN_STATES = ACTIVE_RUN_STATES + (RUN_PAUSED, RUN_COMPLETED_BATCH, RUN_END_CONFIRMED, RUN_FAILED, RUN_CANCELLED)

# 文章下载状态
DL_DISCOVERED = "discovered"
DL_PENDING = "pending"
DL_DOWNLOADED = "downloaded"
DL_FAILED = "failed"
DL_PERMANENT = "permanent_failed"
_DL_RANK = {DL_DISCOVERED: 0, DL_PENDING: 1, DL_FAILED: 2, DL_PERMANENT: 3, DL_DOWNLOADED: 4}

# 待下载队列状态
PENDING = "pending"
PENDING_DONE = "done"
PENDING_FAILED = "failed"
PENDING_PERMANENT = "permanent_failed"

_ACTIVE_STATES_SQL = "(" + ",".join("'%s'" % s for s in ACTIVE_RUN_STATES) + ")"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS archive_articles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fakeid           TEXT NOT NULL,
    article_key      TEXT NOT NULL,
    review_id        TEXT NOT NULL DEFAULT '',
    normalized_url   TEXT NOT NULL DEFAULT '',
    link             TEXT NOT NULL DEFAULT '',
    title            TEXT NOT NULL DEFAULT '',
    account_name     TEXT NOT NULL DEFAULT '',
    author           TEXT NOT NULL DEFAULT '',
    digest           TEXT NOT NULL DEFAULT '',
    cover            TEXT NOT NULL DEFAULT '',
    publish_time     INTEGER NOT NULL DEFAULT 0,
    discovery_source TEXT NOT NULL DEFAULT '',
    download_status  TEXT NOT NULL DEFAULT '{DL_DISCOVERED}',
    local_path       TEXT NOT NULL DEFAULT '',
    last_error       TEXT NOT NULL DEFAULT '',
    rss_cached       INTEGER NOT NULL DEFAULT 0,
    feed_visible     INTEGER NOT NULL DEFAULT 1,
    position_hint    INTEGER,
    first_seen_at    REAL NOT NULL DEFAULT 0,
    updated_at       REAL NOT NULL DEFAULT 0,
    UNIQUE (fakeid, article_key)
);
CREATE INDEX IF NOT EXISTS idx_archive_fakeid_pub ON archive_articles (fakeid, publish_time DESC);
CREATE INDEX IF NOT EXISTS idx_archive_review ON archive_articles (fakeid, review_id);
CREATE INDEX IF NOT EXISTS idx_archive_url ON archive_articles (fakeid, normalized_url);
CREATE INDEX IF NOT EXISTS idx_archive_rss ON archive_articles (rss_cached, account_name, publish_time DESC);
CREATE INDEX IF NOT EXISTS idx_archive_feed ON archive_articles (feed_visible, publish_time DESC);

CREATE TABLE IF NOT EXISTS history_entries (
    ordinal INTEGER PRIMARY KEY, account TEXT, reader INTEGER,
    download_time REAL, publish_time REAL, record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_account ON history_entries (account, publish_time DESC);
CREATE INDEX IF NOT EXISTS idx_history_time ON history_entries (download_time DESC);

CREATE TABLE IF NOT EXISTS backfill_states (
    fakeid                TEXT PRIMARY KEY,
    nickname              TEXT NOT NULL DEFAULT '',
    enabled               INTEGER NOT NULL DEFAULT 0,
    paused                INTEGER NOT NULL DEFAULT 0,
    pause_reason          TEXT NOT NULL DEFAULT '',
    state                 TEXT NOT NULL DEFAULT 'idle',
    reason                TEXT NOT NULL DEFAULT '',
    next_offset           INTEGER NOT NULL DEFAULT 0,
    anchor_key            TEXT NOT NULL DEFAULT '',
    anchor_review_id      TEXT NOT NULL DEFAULT '',
    anchor_url            TEXT NOT NULL DEFAULT '',
    anchor_publish_time   INTEGER NOT NULL DEFAULT 0,
    scanned_count         INTEGER NOT NULL DEFAULT 0,
    discovered_count      INTEGER NOT NULL DEFAULT 0,
    downloaded_count      INTEGER NOT NULL DEFAULT 0,
    failed_count          INTEGER NOT NULL DEFAULT 0,
    earliest_publish_time INTEGER NOT NULL DEFAULT 0,
    last_success_at       REAL NOT NULL DEFAULT 0,
    last_run_at           REAL NOT NULL DEFAULT 0,
    next_eligible_at      REAL NOT NULL DEFAULT 0,
    last_error            TEXT NOT NULL DEFAULT '',
    end_confirmed         INTEGER NOT NULL DEFAULT 0,
    suspected_limit       INTEGER NOT NULL DEFAULT 0,
    empty_streak          INTEGER NOT NULL DEFAULT 0,
    error_streak          INTEGER NOT NULL DEFAULT 0,
    created_at            REAL NOT NULL DEFAULT 0,
    updated_at            REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS backfill_pending (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fakeid        TEXT NOT NULL,
    article_key   TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT '{PENDING}',
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0,
    priority      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL DEFAULT 0,
    UNIQUE (fakeid, article_key)
);
CREATE INDEX IF NOT EXISTS idx_pending_pick ON backfill_pending (fakeid, state, next_retry_at, priority);

CREATE TABLE IF NOT EXISTS backfill_runs (
    id          TEXT PRIMARY KEY,
    fakeid      TEXT NOT NULL,
    trigger     TEXT NOT NULL,
    state       TEXT NOT NULL,
    scan_count  INTEGER NOT NULL DEFAULT 10,
    reason      TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL DEFAULT 0,
    started_at  REAL NOT NULL DEFAULT 0,
    finished_at REAL NOT NULL DEFAULT 0,
    retry_at    REAL NOT NULL DEFAULT 0,
    retries     INTEGER NOT NULL DEFAULT 0,
    owner       TEXT NOT NULL DEFAULT '',
    lease_until REAL NOT NULL DEFAULT 0,
    scanned     INTEGER NOT NULL DEFAULT 0,
    discovered  INTEGER NOT NULL DEFAULT 0,
    downloaded  INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_fakeid ON backfill_runs (fakeid, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_state ON backfill_runs (state, retry_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active ON backfill_runs (fakeid)
    WHERE state IN {_ACTIVE_STATES_SQL};

CREATE TABLE IF NOT EXISTS backfill_daily (
    fakeid    TEXT NOT NULL,
    day       TEXT NOT NULL,
    downloads INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (fakeid, day)
);
"""

_STATE_FIELDS = {
    "nickname", "enabled", "paused", "pause_reason", "state", "reason", "next_offset",
    "anchor_key", "anchor_review_id", "anchor_url", "anchor_publish_time",
    "scanned_count", "discovered_count", "downloaded_count", "failed_count",
    "earliest_publish_time", "last_success_at", "last_run_at", "next_eligible_at",
    "last_error", "end_confirmed", "suspected_limit", "empty_streak", "error_streak",
}
_RUN_FIELDS = {
    "trigger", "state", "scan_count", "reason", "message", "started_at", "finished_at",
    "retry_at", "retries", "owner", "lease_until", "scanned", "discovered", "downloaded",
    "failed", "stop_reason",
}


# ---------------------------------------------------------------- 文章标识


def article_identity(article: dict) -> tuple[str, str, str]:
    """返回 (article_key, review_id, normalized_url)。无法稳定标识时 article_key 为空串。"""
    review_id = str(article.get("review_id") or article.get("id") or article.get("aid") or "").strip()
    link = str(article.get("link") or article.get("url") or "").strip()
    normalized = normalize_wechat_url(link) if link else ""
    # 下载 URL 时为纯短链伪造的 MP_WXS_0_<sid> 不算稳定 reviewId（不同来源会生成不同前缀）
    if review_id.startswith("MP_WXS_0_"):
        review_id = ""
    if review_id:
        return "review:" + review_id, review_id, normalized
    if normalized:
        return "url:" + normalized, review_id, normalized
    title = str(article.get("title") or "").strip()
    publish_time = _to_int(article.get("publish_time") or article.get("update_time"))
    if title and publish_time > 0:
        digest = hashlib.sha1(f"{title}\x00{publish_time}".encode("utf-8")).hexdigest()[:20]
        return "combo:" + digest, review_id, normalized
    return "", review_id, normalized


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 账号目录


def load_account_directory() -> dict:
    """昵称 → fakeid 映射（已收藏公众号 + RSS 订阅），供旧数据导入与手动下载归档使用。"""
    mapping: dict[str, str] = {}
    for path in (RSS_SUBSCRIPTIONS_FILE, ACCOUNTS_FILE):
        data = load_json(path, [])
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            nick = str(item.get("nickname") or "").strip()
            fid = str(item.get("fakeid") or "").strip()
            if nick and fid and "${" not in fid:
                mapping[nick] = fid
    return mapping


def fakeid_for_account(nickname: str, directory: dict | None = None) -> str:
    """把公众号名称解析为稳定 fakeid；无法解析时返回 name:<昵称> 伪标识，保证数据不丢。"""
    nickname = str(nickname or "").strip()
    if not nickname:
        return ""
    directory = directory if directory is not None else load_account_directory()
    return directory.get(nickname) or ("name:" + nickname)


# ---------------------------------------------------------------- 存储


class BackfillStore:
    """线程安全的 SQLite 存储；所有读写共用一个连接并以 RLock 串行化。"""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path or BACKFILL_DB_FILE)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=30, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if not row:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ── 事务 ────────────────────────────────────────────

    @contextmanager
    def transaction(self):
        """BEGIN IMMEDIATE … COMMIT；任何异常整体回滚。可嵌套（内层并入外层事务）。"""
        with self._lock:
            if self._tx_depth:
                self._tx_depth += 1
                try:
                    yield self._conn
                finally:
                    self._tx_depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth = 1
            try:
                yield self._conn
            except BaseException:
                self._tx_depth = 0
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._tx_depth = 0
                self._conn.execute("COMMIT")

    def _query(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _query_one(self, sql: str, params=()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    # ── meta ────────────────────────────────────────────

    def get_meta(self, key: str, default=None):
        row = self._query_one("SELECT value FROM meta WHERE key=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    def set_meta(self, key: str, value, conn=None):
        payload = json.dumps(value, ensure_ascii=False)
        if conn is not None:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, payload))
            return
        with self.transaction() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, payload))

    def delete_meta(self, key: str):
        with self.transaction() as c:
            c.execute("DELETE FROM meta WHERE key=?", (key,))

    def _bump_feed_version(self, conn):
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('feed_version', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
        )

    def feed_version(self) -> int:
        row = self._query_one("SELECT value FROM meta WHERE key='feed_version'")
        return _to_int(row["value"]) if row else 0

    def try_acquire_lease(self, name: str, owner: str, ttl: float, now: float | None = None) -> bool:
        """跨进程互斥租约（用于保证只有一个回溯调度器实例在工作）。"""
        now = time.time() if now is None else now
        key = "lease:" + name
        with self.transaction() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            current = {}
            if row:
                try:
                    current = json.loads(row["value"]) or {}
                except (TypeError, ValueError):
                    current = {}
            if current and current.get("owner") != owner and _to_float(current.get("expires")) > now:
                return False
            self.set_meta(key, {"owner": owner, "expires": now + ttl}, conn=c)
            return True

    def release_lease(self, name: str, owner: str):
        key = "lease:" + name
        with self.transaction() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row:
                try:
                    current = json.loads(row["value"]) or {}
                except (TypeError, ValueError):
                    current = {}
                if current.get("owner") == owner:
                    c.execute("DELETE FROM meta WHERE key=?", (key,))

    # ── 文章索引 ─────────────────────────────────────────

    def _find_existing(self, conn, fakeid: str, key: str, review_id: str, normalized_url: str,
                       title: str = "", publish_time: int = 0):
        row = conn.execute(
            "SELECT * FROM archive_articles WHERE fakeid=? AND article_key=?", (fakeid, key)
        ).fetchone()
        if row:
            return row
        if review_id:
            row = conn.execute(
                "SELECT * FROM archive_articles WHERE fakeid=? AND review_id=? LIMIT 1", (fakeid, review_id)
            ).fetchone()
            if row:
                return row
        if normalized_url:
            row = conn.execute(
                "SELECT * FROM archive_articles WHERE fakeid=? AND normalized_url=? LIMIT 1",
                (fakeid, normalized_url),
            ).fetchone()
            if row:
                return row
        return None

    def upsert_article(self, fakeid: str, article: dict, *, source: str, conn=None,
                       feed_visible: bool | None = None, rss_cached: bool | None = None,
                       download_status: str | None = None, local_path: str | None = None,
                       last_error: str | None = None, position_hint: int | None = None,
                       now: float | None = None) -> tuple[str, bool]:
        """插入或合并一篇文章，返回 (article_key, 是否新建)。

        合并规则：有效字段只补不删；下载状态只升不降（已下载不会被失败覆盖）；
        已有本地路径不被空值或其他路径覆盖；首次发现来源保持不变。
        """
        if conn is None:
            with self.transaction() as c:
                return self.upsert_article(
                    fakeid, article, source=source, conn=c, feed_visible=feed_visible,
                    rss_cached=rss_cached, download_status=download_status, local_path=local_path,
                    last_error=last_error, position_hint=position_hint, now=now,
                )
        now = time.time() if now is None else now
        key, review_id, normalized_url = article_identity(article)
        if not key or not fakeid:
            return "", False
        fields = {
            "link": str(article.get("link") or article.get("url") or ""),
            "title": str(article.get("title") or ""),
            "account_name": str(article.get("account_name") or article.get("account") or ""),
            "author": str(article.get("author") or ""),
            "digest": str(article.get("digest") or ""),
            "cover": str(article.get("cover") or article.get("cover_url") or ""),
            "publish_time": _to_int(article.get("publish_time") or article.get("update_time")),
        }
        existing = self._find_existing(conn, fakeid, key, review_id, normalized_url,
                                       fields["title"], fields["publish_time"])
        if existing is None:
            conn.execute(
                """INSERT INTO archive_articles
                   (fakeid, article_key, review_id, normalized_url, link, title, account_name, author,
                    digest, cover, publish_time, discovery_source, download_status, local_path, last_error,
                    rss_cached, feed_visible, position_hint, first_seen_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fakeid, key, review_id, normalized_url, fields["link"], fields["title"],
                    fields["account_name"], fields["author"], fields["digest"], fields["cover"],
                    fields["publish_time"], source, download_status or DL_DISCOVERED, local_path or "",
                    last_error or "", 1 if rss_cached else 0,
                    1 if (feed_visible is None or feed_visible) else 0, position_hint, now, now,
                ),
            )
            self._bump_feed_version(conn)
            return key, True

        ex = dict(existing)
        updates: dict = {}
        for name, value in fields.items():
            if value and (not ex.get(name) or name == "account_name"):
                updates[name] = value
        if review_id and not ex.get("review_id"):
            updates["review_id"] = review_id
        if normalized_url and not ex.get("normalized_url"):
            updates["normalized_url"] = normalized_url
        if download_status and _DL_RANK.get(download_status, 0) > _DL_RANK.get(ex.get("download_status"), 0):
            updates["download_status"] = download_status
        if download_status == DL_DOWNLOADED and ex.get("download_status") != DL_DOWNLOADED:
            updates["download_status"] = DL_DOWNLOADED
        if local_path and not ex.get("local_path"):
            updates["local_path"] = local_path
        if last_error is not None and ex.get("download_status") != DL_DOWNLOADED:
            updates["last_error"] = last_error
        if rss_cached and not ex.get("rss_cached"):
            updates["rss_cached"] = 1
        if feed_visible and not ex.get("feed_visible"):
            # 最新同步 / 手动下载再次见到这篇文章时，才把它提升为 Feed 可见
            updates["feed_visible"] = 1
        if position_hint is not None and ex.get("position_hint") is None:
            updates["position_hint"] = position_hint
        if updates:
            updates["updated_at"] = now
            cols = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE archive_articles SET {cols} WHERE id=?", (*updates.values(), ex["id"]))
            self._bump_feed_version(conn)
        return ex["article_key"], False

    def forget_local_paths(self, paths) -> int:
        """用户删除了本地下载（清空 / 删除下载历史）后，让索引不再指向已删除的目录。

        文章元数据保留（归档索引不因清理文件而丢失），只把下载状态退回「已发现」。
        """
        paths = [str(p) for p in (paths or []) if p]
        if not paths:
            return 0
        now = time.time()
        changed = 0
        with self.transaction() as conn:
            for p in paths:
                cur = conn.execute(
                    "UPDATE archive_articles SET local_path='', download_status=?, updated_at=? WHERE local_path=?",
                    (DL_DISCOVERED, now, p),
                )
                changed += cur.rowcount
            if changed:
                self._bump_feed_version(conn)
        return changed

    def get_article(self, fakeid: str, article_key: str) -> dict | None:
        return self._query_one(
            "SELECT * FROM archive_articles WHERE fakeid=? AND article_key=?", (fakeid, article_key)
        )

    def find_article(self, fakeid: str, article: dict) -> dict | None:
        key, review_id, normalized_url = article_identity(article)
        if not key:
            return None
        title = str(article.get("title") or "")
        publish_time = _to_int(article.get("publish_time") or article.get("update_time"))
        with self._lock:
            row = self._find_existing(self._conn, fakeid, key, review_id, normalized_url, title, publish_time)
            return dict(row) if row else None

    def count_articles(self, fakeid: str) -> dict:
        row = self._query_one(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN download_status='downloaded' THEN 1 ELSE 0 END) AS downloaded,
                      MIN(CASE WHEN publish_time > 0 THEN publish_time END) AS earliest,
                      MAX(publish_time) AS latest
               FROM archive_articles WHERE fakeid=?""",
            (fakeid,),
        ) or {}
        return {
            "total": _to_int(row.get("total")),
            "downloaded": _to_int(row.get("downloaded")),
            "earliest_publish_time": _to_int(row.get("earliest")),
            "latest_publish_time": _to_int(row.get("latest")),
        }

    def list_articles(self, fakeid: str, limit: int = 50, offset: int = 0,
                      status: str | None = None) -> tuple[list[dict], int]:
        where = "fakeid=?"
        params: list = [fakeid]
        if status:
            where += " AND download_status=?"
            params.append(status)
        total = _to_int((self._query_one(f"SELECT COUNT(*) AS n FROM archive_articles WHERE {where}", params) or {}).get("n"))
        rows = self._query(
            f"SELECT * FROM archive_articles WHERE {where} ORDER BY publish_time DESC, id DESC LIMIT ? OFFSET ?",
            (*params, max(1, limit), max(0, offset)),
        )
        return rows, total

    def rss_cached_articles(self, account_name: str | None = None) -> dict:
        """原 rss_articles.json 的订阅缓存视图：{公众号名称: [条目...]}，按发布时间倒序，不截断。"""
        if account_name:
            rows = self._query(
                "SELECT * FROM archive_articles WHERE rss_cached=1 AND account_name=? "
                "ORDER BY publish_time DESC, id DESC",
                (account_name,),
            )
        else:
            rows = self._query(
                "SELECT * FROM archive_articles WHERE rss_cached=1 ORDER BY publish_time DESC, id DESC"
            )
        out: dict[str, list] = {}
        for r in rows:
            out.setdefault(r["account_name"], []).append(_row_to_rss_item(r))
        return out

    def sync_history_file(self, path=None, records=None):
        """兼容 JSON 的索引缓存；未变化的文件不解析，分页只读取所需行。"""
        from backend.config import DOWNLOAD_HISTORY_LOCK
        path = Path(path or DOWNLOAD_HISTORY_FILE)
        with DOWNLOAD_HISTORY_LOCK:
            signature = [str(path), path.stat().st_mtime_ns, path.stat().st_size] if path.exists() else [str(path), 0, 0]
            if self.get_meta("history_signature") == signature:
                return
            if records is None:
                records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            if not isinstance(records, list):
                raise ValueError("download_history.json 必须为数组")
            directory = load_account_directory()
            with self.transaction() as conn:
                conn.execute("DELETE FROM history_entries")
                for ordinal, raw in enumerate(records):
                    if not isinstance(raw, dict):
                        continue
                    account = raw.get("account") or raw.get("account_name") or ""
                    if account == "微信视频号":
                        continue
                    reader = bool(raw.get("success") and raw.get("path")) or any(
                        word in str(raw.get("error") or "") for word in ("删除", "屏蔽", "过期"))
                    conn.execute("INSERT INTO history_entries VALUES (?,?,?,?,?,?)", (
                        ordinal, account, int(reader), _to_float(raw.get("time")),
                        _to_float(raw.get("publish_time") or raw.get("time")), json.dumps(raw, ensure_ascii=False)))
                    if raw.get("success") and account:
                        fid = raw.get("fakeid") or fakeid_for_account(account, directory)
                        self.upsert_article(fid, {**raw, "account_name": account,
                                                  "publish_time": raw.get("publish_time") or raw.get("time")},
                                            source="backfill" if raw.get("backfill") else "manual", conn=conn,
                                            feed_visible=not raw.get("backfill"), download_status=DL_DOWNLOADED,
                                            local_path=raw.get("path") or "")
                self.set_meta("history_signature", signature, conn=conn)

    def history_page(self, *, account="", reader=False, publish=False, limit=50, offset=0):
        where, args = ["1=1"], []
        if reader:
            where.append("reader=1")
        accounts = [r["account"] for r in self._query(
            "SELECT DISTINCT account FROM history_entries WHERE " + " AND ".join(where) + " AND account != '' ORDER BY account")]
        if account:
            where.append("account=?")
            args.append(account)
        condition = " AND ".join(where)
        total = self._query_one("SELECT COUNT(*) AS n FROM history_entries WHERE " + condition, args)["n"]
        order = "publish_time" if publish else "download_time"
        rows = self._query(f"SELECT ordinal, record FROM history_entries WHERE {condition} ORDER BY {order} DESC, ordinal DESC LIMIT ? OFFSET ?",
                           (*args, limit if limit > 0 else -1, offset))
        return [{**json.loads(r["record"]), "_index": r["ordinal"]} for r in rows], total, accounts

    def feed_page(self, account=None, *, include_backfill=False, limit=0, offset=0):
        where = ["(rss_cached=1 OR download_status='downloaded')", "account_name != '微信视频号'"]
        args = []
        if not include_backfill:
            where.append("feed_visible=1")
        if account:
            # 公众号改名后仍按稳定 fakeid 查询其旧记录。
            where.append("(account_name=? OR fakeid=?)")
            args.extend((account, fakeid_for_account(account)))
        rows = self._query("SELECT * FROM archive_articles WHERE " + " AND ".join(where) +
                           " ORDER BY publish_time DESC, id DESC LIMIT ? OFFSET ?",
                           (*args, limit if limit > 0 else -1, offset))
        return [{**_row_to_rss_item(r), "account": account or r["account_name"]} for r in rows]

    # ── 回溯状态 ─────────────────────────────────────────

    def ensure_state(self, fakeid: str, nickname: str = "", conn=None) -> dict:
        now = time.time()
        if conn is None:
            with self.transaction() as c:
                return self.ensure_state(fakeid, nickname, conn=c)
        row = conn.execute("SELECT * FROM backfill_states WHERE fakeid=?", (fakeid,)).fetchone()
        if row:
            if nickname and row["nickname"] != nickname:
                conn.execute("UPDATE backfill_states SET nickname=?, updated_at=? WHERE fakeid=?",
                             (nickname, now, fakeid))
                row = conn.execute("SELECT * FROM backfill_states WHERE fakeid=?", (fakeid,)).fetchone()
            return dict(row)
        conn.execute(
            "INSERT INTO backfill_states (fakeid, nickname, created_at, updated_at) VALUES (?,?,?,?)",
            (fakeid, nickname, now, now),
        )
        return dict(conn.execute("SELECT * FROM backfill_states WHERE fakeid=?", (fakeid,)).fetchone())

    def get_state(self, fakeid: str) -> dict | None:
        return self._query_one("SELECT * FROM backfill_states WHERE fakeid=?", (fakeid,))

    def list_states(self) -> list[dict]:
        return self._query("SELECT * FROM backfill_states ORDER BY fakeid")

    def update_state(self, fakeid: str, conn=None, **fields):
        fields = {k: v for k, v in fields.items() if k in _STATE_FIELDS}
        if not fields:
            return
        if conn is None:
            with self.transaction() as c:
                return self.update_state(fakeid, conn=c, **fields)
        self.ensure_state(fakeid, conn=conn)
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE backfill_states SET {cols} WHERE fakeid=?", (*fields.values(), fakeid))

    def _update_cursor(self, conn, fakeid: str, cursor: dict):
        """游标推进（独立出来便于测试注入「提交前崩溃」）"""
        self.update_state(fakeid, conn=conn, **cursor)

    def commit_scan_page(self, fakeid: str, *, articles: list[dict], cursor: dict,
                         run_id: str | None = None, scanned_inc: int = 0,
                         feed_visible: bool = False, enqueue: bool = True,
                         now: float | None = None) -> dict:
        """一页扫描结果原子提交：文章入库 + 待下载入队 + 游标推进 + 运行计数，任一步失败整体回滚。

        articles: 本页需要入库的文章（可含 position_hint）
        cursor:   backfill_states 字段更新（next_offset / anchor_* / end_confirmed …）
        enqueue:  是否把尚未下载的文章加入待下载队列（INSERT OR IGNORE，同一篇不会重复排队）
        """
        now = time.time() if now is None else now
        created = 0
        queued = 0
        with self.transaction() as conn:
            self.ensure_state(fakeid, conn=conn)
            for art in articles:
                key, is_new = self.upsert_article(
                    fakeid, art, source="backfill", conn=conn, feed_visible=feed_visible,
                    position_hint=art.get("position_hint"), now=now,
                )
                if not key:
                    continue
                created += 1 if is_new else 0
                if not enqueue:
                    continue
                row = conn.execute(
                    "SELECT download_status, link, review_id FROM archive_articles WHERE fakeid=? AND article_key=?",
                    (fakeid, key),
                ).fetchone()
                if not row or row["download_status"] in (DL_DOWNLOADED, DL_PERMANENT):
                    continue
                if not (row["link"] or row["review_id"]):
                    continue
                cur = conn.execute(
                    """INSERT OR IGNORE INTO backfill_pending
                       (fakeid, article_key, state, attempts, next_retry_at, priority, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (fakeid, key, PENDING, 0, 0, _to_int(art.get("position_hint")), now, now),
                )
                if cur.rowcount:
                    queued += 1
                    conn.execute(
                        "UPDATE archive_articles SET download_status=?, updated_at=? "
                        "WHERE fakeid=? AND article_key=? AND download_status IN (?, ?)",
                        (DL_PENDING, now, fakeid, key, DL_DISCOVERED, DL_FAILED),
                    )
            conn.execute(
                "UPDATE backfill_states SET discovered_count = discovered_count + ?, "
                "scanned_count = scanned_count + ? WHERE fakeid=?",
                (created, int(scanned_inc), fakeid),
            )
            self._update_cursor(conn, fakeid, cursor)
            if run_id:
                conn.execute(
                    "UPDATE backfill_runs SET scanned = scanned + ?, discovered = discovered + ? WHERE id=?",
                    (int(scanned_inc), created, run_id),
                )
        return {"created": created, "queued": queued}

    # ── 运行记录 ─────────────────────────────────────────

    def create_run(self, fakeid: str, trigger: str, scan_count: int, *, state: str = RUN_QUEUED,
                   reason: str = "", message: str = "", retry_at: float = 0,
                   now: float | None = None) -> tuple[dict, bool]:
        """创建任务；若该公众号已有活跃任务则原样返回它（幂等），返回 (run, 是否新建)。"""
        now = time.time() if now is None else now
        run_id = "bf_%s_%s" % (time.strftime("%Y%m%d%H%M%S", time.localtime(now)), uuid.uuid4().hex[:8])
        with self.transaction() as conn:
            existing = conn.execute(
                f"SELECT * FROM backfill_runs WHERE fakeid=? AND state IN {_ACTIVE_STATES_SQL}", (fakeid,)
            ).fetchone()
            if existing:
                return dict(existing), False
            try:
                conn.execute(
                    """INSERT INTO backfill_runs
                       (id, fakeid, trigger, state, scan_count, reason, message, created_at, retry_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (run_id, fakeid, trigger, state, int(scan_count), reason, message, now, retry_at),
                )
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    f"SELECT * FROM backfill_runs WHERE fakeid=? AND state IN {_ACTIVE_STATES_SQL}", (fakeid,)
                ).fetchone()
                return dict(existing), False
            return dict(conn.execute("SELECT * FROM backfill_runs WHERE id=?", (run_id,)).fetchone()), True

    def get_run(self, run_id: str) -> dict | None:
        return self._query_one("SELECT * FROM backfill_runs WHERE id=?", (run_id,))

    def get_active_run(self, fakeid: str) -> dict | None:
        return self._query_one(
            f"SELECT * FROM backfill_runs WHERE fakeid=? AND state IN {_ACTIVE_STATES_SQL}", (fakeid,)
        )

    def list_runs(self, fakeid: str, limit: int = 10) -> list[dict]:
        return self._query(
            "SELECT * FROM backfill_runs WHERE fakeid=? ORDER BY created_at DESC LIMIT ?", (fakeid, limit)
        )

    def update_run(self, run_id: str, conn=None, **fields):
        fields = {k: v for k, v in fields.items() if k in _RUN_FIELDS}
        if not fields:
            return
        if conn is None:
            with self.transaction() as c:
                return self.update_run(run_id, conn=c, **fields)
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE backfill_runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def next_runnable_run(self, now: float | None = None) -> dict | None:
        """手动任务优先，其次空闲任务；同级按创建先后。只返回已到可执行时间的活跃任务。"""
        now = time.time() if now is None else now
        return self._query_one(
            f"""SELECT * FROM backfill_runs
                WHERE state IN ('{RUN_QUEUED}', '{RUN_WAITING_PRIORITY}', '{RUN_COOLDOWN}')
                  AND retry_at <= ?
                ORDER BY CASE trigger WHEN 'manual' THEN 0 ELSE 1 END, created_at
                LIMIT 1""",
            (now,),
        )

    def has_runnable_manual(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(self._query_one(
            f"""SELECT 1 FROM backfill_runs WHERE trigger='manual'
                AND state IN ('{RUN_QUEUED}', '{RUN_WAITING_PRIORITY}', '{RUN_COOLDOWN}') AND retry_at <= ? LIMIT 1""",
            (now,),
        ))

    def claim_run(self, run_id: str, owner: str, lease_seconds: float, now: float | None = None) -> bool:
        """把任务原子地标记为 running（租约）；已被其他实例持有未过期租约时返回 False。"""
        now = time.time() if now is None else now
        with self.transaction() as conn:
            cur = conn.execute(
                f"""UPDATE backfill_runs SET state='{RUN_RUNNING}', owner=?, lease_until=?,
                        started_at=CASE WHEN started_at > 0 THEN started_at ELSE ? END
                    WHERE id=? AND (state IN ('queued', 'waiting_priority', 'cooldown')
                        OR (state = '{RUN_RUNNING}' AND lease_until < ?))""",
                (owner, now + lease_seconds, now, run_id, now),
            )
            return cur.rowcount == 1

    def renew_run_lease(self, run_id: str, owner: str, lease_seconds: float):
        with self.transaction() as conn:
            conn.execute(
                "UPDATE backfill_runs SET lease_until=? WHERE id=? AND owner=?",
                (time.time() + lease_seconds, run_id, owner),
            )

    def recover_stale_runs(self, owner: str, now: float | None = None) -> int:
        """重启恢复：其他实例（已失效）遗留的 running 任务改回 queued，等待重新执行。"""
        now = time.time() if now is None else now
        with self.transaction() as conn:
            rows = conn.execute(
                f"SELECT id, fakeid FROM backfill_runs WHERE state='{RUN_RUNNING}' AND lease_until < ?",
                (now,),
            ).fetchall()
            for r in rows:
                conn.execute(
                    f"UPDATE backfill_runs SET state='{RUN_QUEUED}', owner='', lease_until=0, retry_at=0, "
                    "reason='recovered_after_restart', message='服务重启后自动恢复，将按原断点继续' WHERE id=?",
                    (r["id"],),
                )
                conn.execute(
                    f"UPDATE backfill_states SET state='{RUN_QUEUED}', reason='recovered_after_restart' "
                    "WHERE fakeid=?",
                    (r["fakeid"],),
                )
            return len(rows)

    def cancel_active_runs(self, fakeid: str, state: str = RUN_PAUSED, reason: str = "",
                           include_running: bool = False, trigger: str | None = None) -> int:
        now = time.time()
        states = ACTIVE_RUN_STATES if include_running else (RUN_QUEUED, RUN_WAITING_PRIORITY, RUN_COOLDOWN)
        placeholders = ",".join("?" for _ in states)
        with self.transaction() as conn:
            cur = conn.execute(
                f"UPDATE backfill_runs SET state=?, reason=?, finished_at=?, lease_until=0 "
                f"WHERE fakeid=? AND state IN ({placeholders})" + (" AND trigger=?" if trigger else ""),
                (state, reason, now, fakeid, *states, *((trigger,) if trigger else ())),
            )
            return cur.rowcount

    # ── 待下载队列 ───────────────────────────────────────

    def next_pending(self, fakeid: str, limit: int, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        return self._query(
            f"""SELECT p.*, a.link, a.title, a.review_id, a.normalized_url, a.publish_time, a.cover,
                       a.digest, a.author, a.account_name
                FROM backfill_pending p
                JOIN archive_articles a ON a.fakeid = p.fakeid AND a.article_key = p.article_key
                WHERE p.fakeid=? AND p.state='{PENDING}' AND p.next_retry_at <= ?
                ORDER BY p.priority, p.id LIMIT ?""",
            (fakeid, now, max(0, int(limit))),
        )

    def next_pending_retry_at(self, fakeid: str) -> float | None:
        row = self._query_one(
            f"SELECT MIN(next_retry_at) AS t FROM backfill_pending WHERE fakeid=? AND state='{PENDING}'", (fakeid,)
        )
        return _to_float(row["t"]) if row and row["t"] is not None else None

    def pending_counts(self, fakeid: str) -> dict:
        rows = self._query("SELECT state, COUNT(*) AS n FROM backfill_pending WHERE fakeid=? GROUP BY state", (fakeid,))
        counts = {PENDING: 0, PENDING_DONE: 0, PENDING_FAILED: 0, PENDING_PERMANENT: 0}
        for r in rows:
            counts[r["state"]] = _to_int(r["n"])
        return counts

    def _refresh_failed_count(self, conn, fakeid: str):
        failed_total = conn.execute(
            f"SELECT COUNT(*) FROM backfill_pending WHERE fakeid=? AND state IN ('{PENDING_FAILED}', '{PENDING_PERMANENT}')",
            (fakeid,),
        ).fetchone()[0]
        conn.execute("UPDATE backfill_states SET failed_count=? WHERE fakeid=?", (int(failed_total), fakeid))

    def mark_pending_done(self, fakeid: str, article_key: str, *, local_path: str = "",
                          article_updates: dict | None = None, run_id: str | None = None,
                          day: str | None = None, count_quota: bool = True) -> bool:
        """正文下载成功：出队、文章标记已下载、计数 +1。队列里没有这篇时只更新文章，返回 False。"""
        now = time.time()
        with self.transaction() as conn:
            cur = conn.execute(
                f"UPDATE backfill_pending SET state='{PENDING_DONE}', last_error='', updated_at=? "
                f"WHERE fakeid=? AND article_key=? AND state != '{PENDING_DONE}'",
                (now, fakeid, article_key),
            )
            dequeued = cur.rowcount == 1
            row = conn.execute(
                "SELECT * FROM archive_articles WHERE fakeid=? AND article_key=?", (fakeid, article_key)
            ).fetchone()
            if row:
                updates = {"download_status": DL_DOWNLOADED, "last_error": "", "updated_at": now}
                if local_path and not row["local_path"]:
                    updates["local_path"] = local_path
                for k, v in (article_updates or {}).items():
                    if k in ("title", "digest", "cover", "author", "publish_time", "link") and v and not row[k]:
                        updates[k] = v
                cols = ", ".join(f"{k}=?" for k in updates)
                conn.execute(f"UPDATE archive_articles SET {cols} WHERE id=?", (*updates.values(), row["id"]))
                self._bump_feed_version(conn)
            if dequeued:
                if day and count_quota:
                    self._incr_daily(conn, fakeid, day)
                conn.execute(
                    "UPDATE backfill_states SET downloaded_count = downloaded_count + 1, last_success_at=? "
                    "WHERE fakeid=?",
                    (now, fakeid),
                )
                if run_id:
                    conn.execute("UPDATE backfill_runs SET downloaded = downloaded + 1 WHERE id=?", (run_id,))
                self._refresh_failed_count(conn, fakeid)
            return dequeued

    def mark_pending_failed(self, fakeid: str, article_key: str, *, error: str, permanent: bool,
                            max_attempts: int, retry_delay: float, run_id: str | None = None,
                            day: str | None = None) -> str:
        """记录一次失败：永久失败直接终止；否则未达上限则按退避延迟重试。返回新的队列状态。"""
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT attempts FROM backfill_pending WHERE fakeid=? AND article_key=?", (fakeid, article_key)
            ).fetchone()
            attempts = (_to_int(row["attempts"]) if row else 0) + 1
            if permanent:
                new_state = PENDING_PERMANENT
            elif attempts >= max_attempts:
                new_state = PENDING_FAILED
            else:
                new_state = PENDING
            conn.execute(
                "UPDATE backfill_pending SET state=?, attempts=?, next_retry_at=?, last_error=?, updated_at=? "
                "WHERE fakeid=? AND article_key=?",
                (new_state, attempts, now + retry_delay if new_state == PENDING else 0,
                 str(error or "")[:500], now, fakeid, article_key),
            )
            dl_status = {PENDING_PERMANENT: DL_PERMANENT, PENDING_FAILED: DL_FAILED}.get(new_state, DL_PENDING)
            conn.execute(
                "UPDATE archive_articles SET download_status=?, last_error=?, updated_at=? "
                "WHERE fakeid=? AND article_key=? AND download_status != ?",
                (dl_status, str(error or "")[:500], now, fakeid, article_key, DL_DOWNLOADED),
            )
            if day:
                # 失败的尝试同样消耗了上游请求，计入每日额度
                self._incr_daily(conn, fakeid, day)
            self._refresh_failed_count(conn, fakeid)
            conn.execute("UPDATE backfill_states SET last_error=? WHERE fakeid=?", (str(error or "")[:500], fakeid))
            if run_id:
                conn.execute("UPDATE backfill_runs SET failed = failed + 1 WHERE id=?", (run_id,))
            return new_state

    def retry_failed(self, fakeid: str, include_permanent: bool = False) -> int:
        now = time.time()
        states = [PENDING_FAILED] + ([PENDING_PERMANENT] if include_permanent else [])
        placeholders = ",".join("?" for _ in states)
        with self.transaction() as conn:
            keys = [r["article_key"] for r in conn.execute(
                f"SELECT article_key FROM backfill_pending WHERE fakeid=? AND state IN ({placeholders})",
                (fakeid, *states),
            ).fetchall()]
            for key in keys:
                conn.execute(
                    f"UPDATE backfill_pending SET state='{PENDING}', attempts=0, next_retry_at=0, updated_at=? "
                    "WHERE fakeid=? AND article_key=?",
                    (now, fakeid, key),
                )
                conn.execute(
                    "UPDATE archive_articles SET download_status=?, updated_at=? "
                    "WHERE fakeid=? AND article_key=? AND download_status != ?",
                    (DL_PENDING, now, fakeid, key, DL_DOWNLOADED),
                )
            if keys:
                self._refresh_failed_count(conn, fakeid)
            return len(keys)

    # ── 每日额度 ─────────────────────────────────────────

    def _incr_daily(self, conn, fakeid: str, day: str):
        conn.execute(
            "INSERT INTO backfill_daily (fakeid, day, downloads) VALUES (?, ?, 1) "
            "ON CONFLICT(fakeid, day) DO UPDATE SET downloads = downloads + 1",
            (fakeid, day),
        )

    def daily_downloads(self, fakeid: str, day: str) -> int:
        row = self._query_one("SELECT downloads FROM backfill_daily WHERE fakeid=? AND day=?", (fakeid, day))
        return _to_int(row["downloads"]) if row else 0


def _row_to_rss_item(r: dict) -> dict:
    """把索引行还原为 rss_articles.json 中的条目结构（兼容原读取方）。"""
    return {
        "title": r.get("title") or "",
        "link": r.get("link") or "",
        "cover": r.get("cover") or "",
        "digest": r.get("digest") or "",
        "author": r.get("author") or r.get("account_name") or "",
        "update_time": _to_int(r.get("publish_time")),
        "path": r.get("local_path") or "",
        "id": r.get("review_id") or "",
    }


# ---------------------------------------------------------------- 旧数据迁移

MIGRATION_META_KEY = "legacy_migration_v1"


def migrate_legacy_data(store: BackfillStore, *, force: bool = False,
                        data_dir: Path | None = None) -> dict:
    """把 rss_articles.json 与 download_history.json 幂等导入 SQLite 索引。

    - 只读源文件，绝不修改或删除；导入前把源文件复制到 data/backups/ 下
    - 单一事务：任何异常整体回滚，索引保持导入前状态
    - 重复执行不会重复建档（按唯一键合并），已下载状态与本地路径不会被失败记录覆盖
    """
    if not force and store.get_meta(MIGRATION_META_KEY):
        return {"skipped": True}

    data_dir = Path(data_dir or DATA_DIR)
    rss_file = data_dir / RSS_ARTICLES_FILE.name
    history_file = data_dir / DOWNLOAD_HISTORY_FILE.name
    rss_data = json.loads(rss_file.read_text(encoding="utf-8")) if rss_file.exists() else {}
    history = json.loads(history_file.read_text(encoding="utf-8")) if history_file.exists() else []
    if not isinstance(rss_data, dict):
        raise ValueError("rss_articles.json 必须为对象，迁移已取消")
    if not isinstance(history, list):
        raise ValueError("download_history.json 必须为数组，迁移已取消")

    backup_dir = None
    sources = [p for p in (rss_file, history_file) if p.exists()]
    if sources:
        backup_dir = data_dir / "backups" / (time.strftime("backfill-migration-%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        backup_dir.mkdir(parents=True, exist_ok=True)
        for p in sources:
            shutil.copy2(p, backup_dir / p.name)

    directory = load_account_directory()
    stats = {"rss_items": 0, "history_items": 0, "created": 0, "backup_dir": str(backup_dir or "")}
    now = time.time()
    with store.transaction() as conn:
        for nickname, items in rss_data.items():
            if not isinstance(items, list):
                continue
            fakeid = fakeid_for_account(nickname, directory)
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                art = dict(raw)
                art.setdefault("account_name", nickname)
                path = str(raw.get("path") or "")
                _, created = store.upsert_article(
                    fakeid, art, source="rss_latest", conn=conn, feed_visible=True, rss_cached=True,
                    download_status=DL_DOWNLOADED if path else DL_PERMANENT, local_path=path, now=now,
                )
                stats["rss_items"] += 1
                stats["created"] += 1 if created else 0
        for raw in history:
            if not isinstance(raw, dict) or not raw.get("success"):
                continue
            account = str(raw.get("account") or raw.get("account_name") or "")
            if not account or account == "微信视频号":
                continue
            fakeid = raw.get("fakeid") or fakeid_for_account(account, directory)
            art = dict(raw)
            art["account_name"] = account
            is_backfill = bool(raw.get("backfill"))
            _, created = store.upsert_article(
                fakeid, art, source="backfill" if is_backfill else "manual", conn=conn,
                feed_visible=not is_backfill, download_status=DL_DOWNLOADED,
                local_path=str(raw.get("path") or ""), now=now,
            )
            stats["history_items"] += 1
            stats["created"] += 1 if created else 0
        store.set_meta(MIGRATION_META_KEY, {"time": now, **stats}, conn=conn)
    logger.info("历史归档索引迁移完成: %s", stats)
    return stats


# ---------------------------------------------------------------- 单例

_store: BackfillStore | None = None
_store_lock = threading.Lock()


def get_store() -> BackfillStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                candidate = BackfillStore(BACKFILL_DB_FILE)
                try:
                    migrate_legacy_data(candidate)
                except Exception as exc:  # noqa: BLE001
                    candidate.close()
                    logger.error("历史归档索引迁移失败（已回滚，旧数据未受影响）: %s", exc)
                    raise
                _store = candidate
    return _store


def set_store(store: BackfillStore | None):
    """替换全局存储实例（测试 / 工具脚本使用）"""
    global _store
    with _store_lock:
        _store = store


if __name__ == "__main__":  # python -m backend.backfill_store [migrate|stats]
    import sys

    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    st = BackfillStore(BACKFILL_DB_FILE)
    if cmd == "migrate":
        print(json.dumps(migrate_legacy_data(st, force=True), ensure_ascii=False, indent=2))
    else:
        rows = st._query("SELECT fakeid, COUNT(*) AS n FROM archive_articles GROUP BY fakeid ORDER BY n DESC")
        print(json.dumps({"db": str(st.db_path), "accounts": rows}, ensure_ascii=False, indent=2))
