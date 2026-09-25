"""监控探针调度器。

后台线程，按 settings.monitor.tick_seconds 心跳：
- 取 enabled targets，到点（基于上次探测时间）就探
- 结果入库
- 连续 fail_threshold 次失败 → 触发 fail 告警（受 silence_minutes 防抖）
- 恢复（上次失败、本次成功）→ 触发 recovery 告警（若 notify_recovery）

线程安全：一个调度线程顺序探测所有 target（target 数量不大，串行够用）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

import httpx

from . import db, wecom_client

logger = logging.getLogger("argus.monitor")

_stop_event = threading.Event()
_thread: threading.Thread | None = None

# 全局静音（菜单 click 触发），到期时间戳（0=未静音）
_silence_until = 0.0
_silence_lock = threading.Lock()


def silence(minutes: int) -> None:
    with _silence_lock:
        global _silence_until
        if minutes <= 0:
            _silence_until = 0.0  # 0 = 立即解除
        else:
            _silence_until = time.time() + minutes * 60


def unsilence() -> None:
    with _silence_lock:
        global _silence_until
        _silence_until = 0.0


def is_silenced() -> bool:
    with _silence_lock:
        return time.time() < _silence_until


def silence_remaining() -> int:
    with _silence_lock:
        rem = int((_silence_until - time.time()) / 60)
        return max(rem, 0)


# ---------------------------------------------------------------------------
# 探测逻辑
# ---------------------------------------------------------------------------

def _parse_expect(spec: str) -> set[int]:
    """解析 "200-299" / "200,301" / "200" → 状态码集合。"""
    spec = (spec or "").strip()
    if not spec:
        return set(range(200, 300))
    result: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            result.update(range(int(lo), int(hi) + 1))
        elif part:
            result.add(int(part))
    return result or set(range(200, 300))


def probe_once(target: dict) -> dict:
    """执行一次 HTTP 探测。返回 {ok, status, latency_ms, error}。"""
    ok_codes = _parse_expect(target.get("expect_status", "200-299"))
    method = (target.get("method") or "GET").upper()
    url = target["url"]
    timeout = float(target.get("timeout_s") or 10)
    headers = json.loads(target.get("headers") or "{}")
    body = target.get("body") or ""

    result = {"ok": False, "status": None, "latency_ms": None, "error": None}
    start = time.monotonic()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.request(method, url, headers=headers, content=body)
        latency = int((time.monotonic() - start) * 1000)
        result["status"] = resp.status_code
        result["latency_ms"] = latency
        if resp.status_code in ok_codes:
            result["ok"] = True
        else:
            result["error"] = f"状态码 {resp.status_code} 不在期望 {target.get('expect_status')}"
    except httpx.TimeoutException:
        result["error"] = f"超时 ({timeout}s)"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def _do_probe(target: dict) -> None:
    """探测 + 入库 + 告警判断。"""
    tid = target["id"]
    name = target.get("name", str(tid))
    result = probe_once(target)
    db.add_probe_result(tid, result["ok"], result["status"],
                        result["latency_ms"], result["error"])

    threshold = int(target.get("fail_threshold") or 2)
    silence_min = int(target.get("silence_minutes") or 30)
    notify_recovery = bool(target.get("notify_recovery", 1))

    last = db.get_last_probe(tid)
    prev_ok = None
    # 取倒数第二条判断上次状态
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT ok FROM probe_results WHERE target_id=? ORDER BY id DESC LIMIT 2",
        (tid,),
    ).fetchall()
    if len(rows) >= 2:
        prev_ok = bool(rows[1]["ok"])

    now_ok = result["ok"]

    # 失败告警
    if not now_ok:
        fails = db.count_recent_failures(tid)
        if fails >= threshold:
            _maybe_alert(tid, "fail",
                         f"[告警] {name} 探测失败\nURL: {target['url']}\n原因: {result['error'] or result['status']}",
                         silence_min)
    # 恢复告警
    elif prev_ok is False and notify_recovery:
        _maybe_alert(tid, "recovery",
                     f"[恢复] {name} 已恢复正常\nURL: {target['url']}\n状态: {result['status']} ({result['latency_ms']}ms)",
                     silence_min, force=True)


def _maybe_alert(target_id: int, kind: str, message: str,
                 silence_min: int, force: bool = False) -> None:
    """防抖 + 发送。force=True 时跳过 silence 检查（恢复必发）。"""
    # 静音期不发（force 跳过）
    if is_silenced() and not force:
        db.add_alert(target_id, kind, message, delivered=False)
        return

    # 防抖：silence_min 内同 kind 不重发
    if not force:
        last = db.get_last_alert_of_kind(target_id, kind)
        if last:
            last_ts = datetime.fromisoformat(last["ts"])
            elapsed = (datetime.now(timezone.utc) - last_ts).total_seconds()
            if elapsed < silence_min * 60:
                db.add_alert(target_id, kind, message, delivered=False)
                return

    alert_id = db.add_alert(target_id, kind, message, delivered=False)
    try:
        wecom_client.send_text(message)
        db.mark_alert_delivered(alert_id)
    except Exception as e:
        logger.warning("告警发送失败: %s", e)


def _execute_action(target: dict) -> dict:
    """执行目标绑定的 HTTP 动作（菜单「重启服务」之类）。"""
    if target.get("action_type") != "http":
        return {"ok": False, "detail": "该目标未配置 HTTP 动作"}
    method = (target.get("action_method") or "POST").upper()
    url = target.get("action_url") or ""
    if not url:
        return {"ok": False, "detail": "动作 URL 为空"}
    headers = json.loads(target.get("action_headers") or "{}")
    body = target.get("action_body") or ""
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.request(method, url, headers=headers, content=body)
        return {"ok": 200 <= resp.status_code < 400,
                "detail": f"HTTP {resp.status_code}", "status": resp.status_code}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# 调度循环
# ---------------------------------------------------------------------------

def _should_probe(target: dict, now_ts: float) -> bool:
    """基于上次探测时间 + interval 判断是否到点。"""
    last = db.get_last_probe(target["id"])
    if not last:
        return True
    last_ts = datetime.fromisoformat(last["ts"]).timestamp()
    interval = int(target.get("interval_s") or 60)
    return now_ts - last_ts >= interval


def _loop() -> None:
    logger.info("监控调度器启动")
    while not _stop_event.is_set():
        try:
            tick = int(db.get_setting("monitor.tick_seconds", "30"))
        except Exception:
            tick = 30
        tick = max(tick, 5)  # 下限 5s，别太狠
        now_ts = time.time()

        try:
            targets = db.list_enabled_targets()
            for t in targets:
                if _stop_event.is_set():
                    break
                if _should_probe(t, now_ts):
                    try:
                        _do_probe(t)
                    except Exception as e:
                        logger.exception("探测 target %s 失败: %s", t.get("id"), e)
        except Exception as e:
            logger.exception("调度循环异常: %s", e)

        # 清理旧数据
        try:
            retain = int(db.get_setting("log.retain_days", "7"))
            db.cleanup_old(retain)
        except Exception:
            pass

        _stop_event.wait(tick)
    logger.info("监控调度器停止")


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="monitor-loop", daemon=True)
    _thread.start()


def stop() -> None:
    _stop_event.set()
    global _thread
    if _thread:
        _thread.join(timeout=5)
        _thread = None
