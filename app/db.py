"""SQLite 数据访问层。

建表（CONTRACT §4）+ settings/targets/probe_results/alerts/events 的增删改查。
所有 SQL 在一个连接上执行，连接复用（线程安全用 check_same_thread=False）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .settings import DB_PATH, DEFAULT_SETTINGS

_LOCK = threading.Lock()
_conn: sqlite3.Connection | None = None


def _now() -> str:
    """统一时间戳格式：ISO8601 UTC。"""
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
    return _conn


@contextmanager
def cursor():
    """所有写操作走这个上下文，自动 commit/rollback + 线程锁。"""
    conn = get_conn()
    with _LOCK:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS targets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  url TEXT NOT NULL,
  method TEXT NOT NULL DEFAULT 'GET',
  headers TEXT NOT NULL DEFAULT '{}',
  body TEXT NOT NULL DEFAULT '',
  expect_status TEXT NOT NULL DEFAULT '200-299',
  timeout_s INTEGER NOT NULL DEFAULT 10,
  interval_s INTEGER NOT NULL DEFAULT 60,
  fail_threshold INTEGER NOT NULL DEFAULT 2,
  silence_minutes INTEGER NOT NULL DEFAULT 30,
  notify_recovery INTEGER NOT NULL DEFAULT 1,
  action_type TEXT NOT NULL DEFAULT 'none',
  action_method TEXT NOT NULL DEFAULT 'POST',
  action_url TEXT NOT NULL DEFAULT '',
  action_headers TEXT NOT NULL DEFAULT '{}',
  action_body TEXT NOT NULL DEFAULT '',
  action_confirm INTEGER NOT NULL DEFAULT 1,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS probe_results(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id INTEGER NOT NULL, ts TEXT NOT NULL,
  ok INTEGER NOT NULL, status INTEGER, latency_ms INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id INTEGER, ts TEXT NOT NULL, kind TEXT NOT NULL,
  message TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, source TEXT NOT NULL, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_probe_target ON probe_results(target_id, ts);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
"""


def init_db() -> None:
    """建表 + 用 DEFAULT_SETTINGS 补齐缺失键。幂等。"""
    with cursor() as cur:
        cur.executescript(_SCHEMA)
        for key, default_value in DEFAULT_SETTINGS.items():
            cur.execute(
                "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES(?,?,?)",
                (key, default_value, _now()),
            )


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def get_all_settings() -> dict[str, str]:
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_setting(key: str, default: str = "") -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def update_settings(values: dict[str, str]) -> None:
    with cursor() as cur:
        for key, value in values.items():
            cur.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, _now()),
            )


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------

_TARGET_COLS = (
    "id", "name", "enabled", "url", "method", "headers", "body",
    "expect_status", "timeout_s", "interval_s", "fail_threshold",
    "silence_minutes", "notify_recovery", "action_type", "action_method",
    "action_url", "action_headers", "action_body", "action_confirm",
    "created_at", "updated_at",
)


def _row_to_target(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("enabled", "notify_recovery", "action_confirm"):
        d[k] = bool(d[k])
    return d


def list_targets() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        f"SELECT {','.join(_TARGET_COLS)} FROM targets ORDER BY id"
    ).fetchall()
    return [_row_to_target(r) for r in rows]


def get_target(tid: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        f"SELECT {','.join(_TARGET_COLS)} FROM targets WHERE id=?", (tid,)
    ).fetchone()
    return _row_to_target(row) if row else None


def create_target(data: dict) -> dict:
    cols = [c for c in _TARGET_COLS if c not in ("id", "created_at", "updated_at")]
    vals = []
    for c in cols:
        if c in ("headers", "action_headers"):
            v = data.get(c, "{}")
            # 允许传 dict，自动序列化；传字符串保持原样
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            vals.append(v)
        elif c in ("enabled", "notify_recovery", "action_confirm"):
            vals.append(1 if data.get(c, True if c == "enabled" else (1 if c == "notify_recovery" else 1)) else 0)
        else:
            vals.append(data.get(c, ""))
    ts = _now()
    with cursor() as cur:
        cur.execute(
            f"INSERT INTO targets({','.join(cols)}, created_at, updated_at) "
            f"VALUES({','.join('?' * len(cols))}, ?, ?)",
            (*vals, ts, ts),
        )
        tid = cur.lastrowid
    result = get_target(tid)
    assert result is not None
    return result


def update_target(tid: int, data: dict) -> dict | None:
    existing = get_target(tid)
    if existing is None:
        return None
    cols = [c for c in _TARGET_COLS if c not in ("id", "created_at")]
    sets = []
    vals = []
    for c in cols:
        if c not in data:
            continue
        v = data[c]
        if c in ("headers", "action_headers") and isinstance(v, (dict, list)):
            v = json.dumps(v)
        if c in ("enabled", "notify_recovery", "action_confirm"):
            v = 1 if v else 0
        sets.append(f"{c}=?")
        vals.append(v)
    if not sets:
        return existing
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(tid)
    with cursor() as cur:
        cur.execute(
            f"UPDATE targets SET {','.join(sets)} WHERE id=?", (*vals,)
        )
    return get_target(tid)


def delete_target(tid: int) -> bool:
    with cursor() as cur:
        cur.execute("DELETE FROM targets WHERE id=?", (tid,))
        return cur.rowcount > 0


def list_enabled_targets() -> list[dict]:
    """调度器用：只取 enabled=1 的。"""
    conn = get_conn()
    rows = conn.execute(
        f"SELECT {','.join(_TARGET_COLS)} FROM targets WHERE enabled=1 ORDER BY id"
    ).fetchall()
    return [_row_to_target(r) for r in rows]


# ---------------------------------------------------------------------------
# probe_results
# ---------------------------------------------------------------------------

def add_probe_result(target_id: int, ok: bool, status: int | None,
                     latency_ms: int | None, error: str | None) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO probe_results(target_id, ts, ok, status, latency_ms, error) "
            "VALUES(?,?,?,?,?,?)",
            (target_id, _now(), 1 if ok else 0, status, latency_ms, error),
        )


def get_probe_history(target_id: int, limit: int = 50) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, target_id, ts, ok, status, latency_ms, error "
        "FROM probe_results WHERE target_id=? ORDER BY id DESC LIMIT ?",
        (target_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_last_probe(target_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, target_id, ts, ok, status, latency_ms, error "
        "FROM probe_results WHERE target_id=? ORDER BY id DESC LIMIT 1",
        (target_id,),
    ).fetchone()
    return dict(row) if row else None


def count_recent_failures(target_id: int) -> int:
    """从最近一次成功之后连续失败多少次。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT ok FROM probe_results WHERE target_id=? ORDER BY id DESC LIMIT 100",
        (target_id,),
    ).fetchall()
    count = 0
    for r in rows:
        if r["ok"]:
            break
        count += 1
    return count


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------

def add_alert(target_id: int | None, kind: str, message: str, delivered: bool = False) -> int:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO alerts(target_id, ts, kind, message, delivered) "
            "VALUES(?,?,?,?,?)",
            (target_id, _now(), kind, message, 1 if delivered else 0),
        )
        return cur.lastrowid


def list_alerts(limit: int = 50) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, target_id, ts, kind, message, delivered "
        "FROM alerts ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_last_alert_of_kind(target_id: int, kind: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, target_id, ts, kind, message, delivered "
        "FROM alerts WHERE target_id=? AND kind=? ORDER BY id DESC LIMIT 1",
        (target_id, kind),
    ).fetchone()
    return dict(row) if row else None


def mark_alert_delivered(alert_id: int) -> None:
    with cursor() as cur:
        cur.execute("UPDATE alerts SET delivered=1 WHERE id=?", (alert_id,))


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------

def add_event(source: str, payload: dict | str | None = None) -> None:
    payload_str = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    with cursor() as cur:
        cur.execute(
            "INSERT INTO events(ts, source, payload) VALUES(?,?,?)",
            (_now(), source, payload_str),
        )


# ---------------------------------------------------------------------------
# 通用清理（settings 里有 log.retain_days）
# ---------------------------------------------------------------------------

def cleanup_old(days: int) -> int:
    """删掉超过 retain_days 天的 probe_results / alerts / events。返回删除行数。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    n = 0
    with cursor() as cur:
        cur.execute("DELETE FROM probe_results WHERE ts < ?", (cutoff,))
        n += cur.rowcount
        cur.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
        n += cur.rowcount
        cur.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        n += cur.rowcount
    return n


def get_status_overview() -> dict:
    """概览数据：总数 / down 数 / 最近告警。"""
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) AS c FROM targets").fetchone()["c"]
    # down = 最近一次探测 ok=0 的 target
    down_rows = conn.execute(
        "SELECT t.id FROM targets t "
        "JOIN probe_results p ON p.id = (SELECT MAX(id) FROM probe_results WHERE target_id=t.id) "
        "WHERE t.enabled=1 AND p.ok=0"
    ).fetchall()
    down = len(down_rows)
    recent_alerts = list_alerts(5)
    return {"targets_total": total, "targets_down": down, "last_alerts": recent_alerts}
