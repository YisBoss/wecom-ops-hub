"""核心 API 路由：health/login/settings/targets/alerts/notify/menu/status。

按 CONTRACT §5 实现。鉴权通过 Cookie woh_session（auth.is_logged_in）。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from typing import Any

from .. import db, monitor, wecom_client
from ..settings import (
    DEFAULT_SETTINGS,
    INTERNAL_SETTING_KEYS,
    SECRET_KEYS,
    mask_settings,
    is_masked,
)
from .auth import is_logged_in, login, logout

logger = logging.getLogger("argus.routes")

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# 鉴权依赖
# ---------------------------------------------------------------------------

def require_auth(request: Request) -> None:
    if not is_logged_in(request):
        raise HTTPException(status_code=401, detail="unauthorized")


# ---------------------------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------------------------

class LoginBody(BaseModel):
    password: str


class SettingsBody(BaseModel):
    # 任意键值对；用 dict 而非字段，因为键名是动态的
    model_config = {"extra": "allow"}


class TargetBody(BaseModel):
    name: str
    enabled: bool = True
    url: str
    method: str = "GET"
    headers: Any = "{}"
    body: str = ""
    expect_status: str = "200-299"
    timeout_s: int = 10
    interval_s: int = 60
    fail_threshold: int = 2
    silence_minutes: int = 30
    notify_recovery: bool = True
    action_type: str = "none"
    action_method: str = "POST"
    action_url: str = ""
    action_headers: Any = "{}"
    action_body: str = ""
    action_confirm: bool = True
    # CONTRACT §7.1：把该目标的动作挂到企微菜单（空 = 不进菜单）
    menu_label: str = ""
    menu_order: int = 0


# ---------------------------------------------------------------------------
# health / login / logout
# ---------------------------------------------------------------------------

@router.get("/health")
async def health() -> dict:
    return {"ok": True, "version": "1.0.0"}


@router.post("/login")
async def do_login(body: LoginBody, response: Response) -> dict:
    token = login(body.password)
    if not token:
        raise HTTPException(status_code=401, detail="密码错误")
    response.set_cookie(
        key="woh_session", value=token, httponly=True,
        samesite="lax", max_age=7 * 24 * 3600,
    )
    return {"ok": True}


@router.post("/logout")
async def do_logout(response: Response) -> dict:
    logout()
    response.delete_cookie("woh_session")
    return {"ok": True}


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

@router.get("/settings")
async def get_settings(_: None = Depends(require_auth)) -> dict:
    values = db.get_all_settings()
    # 补齐缺失键
    for k, v in DEFAULT_SETTINGS.items():
        values.setdefault(k, v)
    # 内部键（如 menu.local）不是设置项，不外泄
    for k in INTERNAL_SETTING_KEYS:
        values.pop(k, None)
    return mask_settings(values)


@router.put("/settings")
async def put_settings(body: dict, _: None = Depends(require_auth)) -> dict:
    # 只接受已知键；值为 MASK 跳过
    updates = {}
    for k, v in body.items():
        if k not in DEFAULT_SETTINGS:
            continue
        if k in SECRET_KEYS and is_masked(str(v)):
            continue
        updates[k] = str(v)
    if updates:
        db.update_settings(updates)
        # 凭据变更清 token 缓存
        if any(k.startswith("wecom.") for k in updates):
            wecom_client.reset_token()
    return {"ok": True, "updated": list(updates.keys())}


@router.post("/settings/test")
async def test_settings(_: None = Depends(require_auth)) -> dict:
    ok, detail = wecom_client.test_connection()
    return {"ok": ok, "detail": detail}


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------

@router.get("/targets")
async def get_targets(_: None = Depends(require_auth)) -> list[dict]:
    targets = db.list_targets()
    # 附带最近探测状态
    for t in targets:
        last = db.get_last_probe(t["id"])
        t["last_probe"] = last
    return targets


@router.post("/targets")
async def create_target(body: TargetBody, _: None = Depends(require_auth)) -> dict:
    data = body.model_dump()
    return db.create_target(data)


@router.put("/targets/{tid}")
async def update_target(tid: int, body: dict, _: None = Depends(require_auth)) -> dict:
    result = db.update_target(tid, body)
    if result is None:
        raise HTTPException(status_code=404, detail="target not found")
    return result


@router.delete("/targets/{tid}")
async def delete_target(tid: int, _: None = Depends(require_auth)) -> dict:
    ok = db.delete_target(tid)
    if not ok:
        raise HTTPException(status_code=404, detail="target not found")
    return {"ok": True}


@router.post("/targets/{tid}/probe")
async def probe_target(tid: int, _: None = Depends(require_auth)) -> dict:
    target = db.get_target(tid)
    if target is None:
        raise HTTPException(status_code=404, detail="target not found")
    result = monitor.probe_once(target)
    db.add_probe_result(tid, result["ok"], result["status"],
                        result["latency_ms"], result["error"])
    return result


@router.post("/targets/{tid}/action")
async def run_action(tid: int, _: None = Depends(require_auth)) -> dict:
    target = db.get_target(tid)
    if target is None:
        raise HTTPException(status_code=404, detail="target not found")
    result = monitor._execute_action(target)
    db.add_event("action", {"target_id": tid, "name": target.get("name"), "result": result})
    return result


@router.get("/targets/{tid}/history")
async def target_history(tid: int, limit: int = 50,
                         _: None = Depends(require_auth)) -> list[dict]:
    return db.get_probe_history(tid, limit)


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------

@router.get("/alerts")
async def get_alerts(limit: int = 50, _: None = Depends(require_auth)) -> list[dict]:
    return db.list_alerts(limit)


# ---------------------------------------------------------------------------
# notify test
# ---------------------------------------------------------------------------

@router.post("/notify/test")
async def notify_test(_: None = Depends(require_auth)) -> dict:
    try:
        wecom_client.send_text("[测试] 这是来自 Argus 的测试消息。")
        return {"ok": True, "detail": "测试消息已发送"}
    except wecom_client.WeComError as e:
        return {"ok": False, "detail": str(e)}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# silence（菜单/面板控制全局静音）
# ---------------------------------------------------------------------------

class SilenceBody(BaseModel):
    minutes: int = 0


@router.post("/silence")
async def set_silence(body: SilenceBody, _: None = Depends(require_auth)) -> dict:
    if body.minutes <= 0:
        monitor.unsilence()
    else:
        monitor.silence(body.minutes)
    return {"ok": True, "silence_remaining_minutes": monitor.silence_remaining()}


# ---------------------------------------------------------------------------
# menu
# ---------------------------------------------------------------------------

def _default_menu_buttons() -> list[dict]:
    """默认菜单模板（CONTRACT §7 / §7.1）。

    企微硬限制：顶层 button 只能 1~3 个（errcode 40058），父按钮最多 5 个子按钮。
    `🔀 切换` 的内容来自数据库里带 `menu_label` 的目标（按 menu_order,id 取前 5），
    一个都没有时整块不出现。
    public_url 为空时不能生成 view 型按钮（企微会报错），退化成纯 click。
    """
    public_url = db.get_setting("panel.public_url", "").strip().rstrip("/")

    action_buttons: list[dict] = []
    for t in db.list_menu_targets(limit=5):
        if t.get("action_type") != "http" or not (t.get("action_url") or ""):
            logger.warning(
                "目标 %s(%s) 配了 menu_label「%s」但没有 HTTP 动作，跳过不进菜单",
                t.get("id"), t.get("name"), t.get("menu_label"),
            )
            continue
        action_buttons.append(
            {"type": "click", "name": t["menu_label"], "key": f"ACT:{t['id']}"}
        )

    if not public_url:
        # 顶层只能 3 个：动作按钮最多 1 个，剩下留给静音
        return (
            action_buttons[:1]
            + [
                {"type": "click", "name": "🔕 静音1小时", "key": "SILENCE_1H"},
                {"type": "click", "name": "🔔 解除静音", "key": "UNSILENCE"},
            ]
        )

    buttons: list[dict] = [
        {"type": "view", "name": "📊 状态", "url": f"{public_url}/#/status"},
    ]
    if action_buttons:
        buttons.append({"name": "🔀 切换", "sub_button": action_buttons})
    buttons.append({"name": "🔧 操作", "sub_button": [
        {"type": "click", "name": "🔕 静音1小时", "key": "SILENCE_1H"},
        {"type": "click", "name": "🔔 解除静音", "key": "UNSILENCE"},
        {"type": "view", "name": "🧪 自检", "url": f"{public_url}/#/selftest"},
        {"type": "view", "name": "🚨 告警", "url": f"{public_url}/#/alerts"},
        {"type": "view", "name": "⚙️ 设置", "url": f"{public_url}/#/settings"},
    ]})
    return buttons


def _name_error(name: Any, limit: int, label: str) -> str | None:
    """企微按**字节数**限制按钮名（一级 16 字节 / 二级 60 字节），不是按字符数。"""
    if not isinstance(name, str) or not name:
        return f"{label}缺少 name"
    n = len(name.encode("utf-8"))
    if n > limit:
        return f"{label}「{name}」太长：{n} 字节，上限 {limit} 字节（中文 3 字节、emoji 4~6 字节）"
    return None


def _validate_leaf(button: Any) -> str | None:
    """校验单个可点击按钮（view / click）。"""
    if not isinstance(button, dict):
        return "必须是对象"
    btype = button.get("type")
    if btype == "view":
        return None if button.get("url") else "view 型必须有 url"
    if btype == "click":
        return None if button.get("key") else "click 型必须有 key"
    return "type 只能是 view 或 click"


def _validate_menu_buttons(buttons: Any) -> str | None:
    """推送前本地校验菜单结构（CONTRACT §7）。通过返回 None，否则返回错误说明。

    目的是把企微的 40058 之类错误在本地拦下，换成用户看得懂的话。
    """
    if not isinstance(buttons, list) or not 1 <= len(buttons) <= 3:
        return "顶层 button 数量必须是 1~3 个（企微硬限制，errcode 40058）"
    for i, b in enumerate(buttons, 1):
        if not isinstance(b, dict):
            return f"第 {i} 个按钮必须是对象"
        err = _name_error(b.get("name"), 16, f"第 {i} 个按钮")
        if err:
            return err
        subs = b.get("sub_button")
        if subs is None:
            err = _validate_leaf(b)
            if err:
                return f"「{b['name']}」：{err}"
            continue
        if not isinstance(subs, list) or not 1 <= len(subs) <= 5:
            return f"「{b['name']}」的子按钮数量必须是 1~5 个"
        for k in ("type", "key", "url"):
            if b.get(k):
                return f"「{b['name']}」是父按钮，不能再带 {k}"
        for j, s in enumerate(subs, 1):
            label = f"「{b['name']}」第 {j} 个子按钮"
            err = _name_error(s.get("name") if isinstance(s, dict) else None, 60, label)
            if err:
                return err
            err = _validate_leaf(s)
            if err:
                return f"{label}：{err}"
    return None


def _local_menu() -> dict:
    """本地菜单：优先返回面板保存过的，没有则生成默认模板。"""
    saved = db.get_setting("menu.local", "")
    if saved:
        try:
            data = json.loads(saved)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict) and data.get("button"):
            return data
    return {"button": _default_menu_buttons()}


@router.get("/menu")
async def get_menu(_: None = Depends(require_auth)) -> dict:
    local = _local_menu()
    remote = None
    error = None
    try:
        data = wecom_client.menu_get()
        code = data.get("errcode")
        if code == 0:
            # 【实测】有菜单时 button 在**顶层**；部分文档写 {"menu":{"button":[...]}}，两种都兼容
            remote = data.get("menu")
            if remote is None and data.get("button"):
                remote = {"button": data["button"]}
        elif code == 46003:
            # 46003 = 菜单不存在
            remote = None
        else:
            error = data.get("errmsg", str(data))
    except wecom_client.WeComError as e:
        error = str(e)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    return {"remote": remote, "local": local, "error": error}


@router.put("/menu")
async def put_menu(body: dict, _: None = Depends(require_auth)) -> dict:
    if body.get("preset"):
        buttons = _default_menu_buttons()
    else:
        buttons = body.get("button", [])
    err = _validate_menu_buttons(buttons)
    if err:
        raise HTTPException(status_code=400, detail=err)
    # 先存本地：即使推送失败（例如反代没放行 menu/create），用户的编辑也不会丢
    db.update_settings({"menu.local": json.dumps({"button": buttons}, ensure_ascii=False)})
    try:
        result = wecom_client.menu_create(buttons)
    except wecom_client.WeComError as e:
        db.add_event("menu", {"action": "create", "buttons": buttons, "error": str(e)})
        raise HTTPException(status_code=502, detail=str(e))
    db.add_event("menu", {"action": "create", "buttons": buttons, "result": result})
    return {"ok": result.get("errcode") == 0, "detail": result.get("errmsg", ""), "raw": result}


@router.delete("/menu")
async def del_menu(_: None = Depends(require_auth)) -> dict:
    try:
        result = wecom_client.menu_delete()
    except wecom_client.WeComError as e:
        raise HTTPException(status_code=502, detail=str(e))
    db.add_event("menu", {"action": "delete", "result": result})
    return {"ok": result.get("errcode") == 0, "detail": result.get("errmsg", ""), "raw": result}


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@router.get("/status")
async def get_status(_: None = Depends(require_auth)) -> dict:
    overview = db.get_status_overview()
    overview["silence_remaining_minutes"] = monitor.silence_remaining()
    return overview
