"""鉴权：HttpOnly Cookie woh_session。

简单方案：session token = 首次登录密码的 sha256；登录成功设 cookie。
适合单用户面板（本项目的定位）。
"""

from __future__ import annotations

import hashlib
import secrets

from fastapi import Request

from .. import db
from ..settings import MASK

_SESSION_TOKEN: str = ""  # 进程内；重启后重新登录


def _password_hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str) -> bool:
    stored = db.get_setting("panel.admin_password", "admin")
    return _password_hash(password) == _password_hash(stored)


def login(password: str) -> str:
    """验证成功返回 session token，失败返回空串。"""
    if not verify_password(password):
        return ""
    global _SESSION_TOKEN
    _SESSION_TOKEN = secrets.token_urlsafe(32)
    return _SESSION_TOKEN


def logout() -> None:
    global _SESSION_TOKEN
    _SESSION_TOKEN = ""


def is_logged_in(request: Request) -> bool:
    token = request.cookies.get("woh_session", "")
    return bool(_SESSION_TOKEN) and token == _SESSION_TOKEN


def mask(value: str) -> str:
    return MASK if value else ""
