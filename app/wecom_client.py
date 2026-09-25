"""企业微信 API 客户端 + 回调加解密。

实现：
- access_token 缓存（expires_in 提前 300s 刷新）
- message/send（agentid 必须是 int）
- menu create / get / delete
- 回调验签 + AES-256-CBC 加解密（WXBizMsgCrypt 等价实现，纯 pycryptodome）

支持 wecom.proxy_url 反代：所有 qyapi 请求走该基址（路径原样拼接）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from . import db

# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

def _cfg(key: str, default: str = "") -> str:
    return db.get_setting(key, default)


def _api_base() -> str:
    """企微 API 基址（走反代或直连）。"""
    proxy = _cfg("wecom.proxy_url", "").strip()
    if proxy:
        return proxy.rstrip("/")
    return _cfg("wecom.api_base", "https://qyapi.weixin.qq.com").rstrip("/")


def _agent_id() -> int:
    """agentid 必须是整数。"""
    raw = _cfg("wecom.agent_id", "0")
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# token 缓存
# ---------------------------------------------------------------------------

_token_cache: dict[str, Any] = {"token": "", "expires_at": 0.0}
_token_lock = None  # 用 httpx 自带同步即可，单进程

_token_lock = __import__("threading").Lock()


def get_token() -> str:
    """取 access_token，缓存有效期内复用，提前 300s 刷新。"""
    now = time.time()
    with _token_lock:
        if _token_cache["token"] and now < _token_cache["expires_at"]:
            return _token_cache["token"]

    corpid = _cfg("wecom.corp_id")
    secret = _cfg("wecom.secret")
    if not corpid or not secret:
        raise WeComError("corp_id 或 secret 未配置")

    url = f"{_api_base()}/cgi-bin/gettoken"
    resp = httpx.get(url, params={"corpid": corpid, "corpsecret": secret}, timeout=10)
    data = resp.json()
    if data.get("errcode") != 0:
        raise WeComError(f"gettoken 失败: {data.get('errmsg', data)}")

    token = data["access_token"]
    expires_in = int(data.get("expires_in", 7200))
    with _token_lock:
        _token_cache["token"] = token
        _token_cache["expires_at"] = now + expires_in - 300  # 提前 300s

    return token


def reset_token() -> None:
    """配置变更后清缓存。"""
    with _token_lock:
        _token_cache["token"] = ""
        _token_cache["expires_at"] = 0.0


# ---------------------------------------------------------------------------
# 消息发送
# ---------------------------------------------------------------------------

class WeComError(Exception):
    pass


def send_text(content: str, touser: str = "", toparty: str = "") -> dict:
    """发文本消息。touser/toparty 为空时用配置里的默认值。"""
    token = get_token()
    touser = touser or _cfg("wecom.touser", "@all")
    toparty = toparty or _cfg("wecom.toparty", "")
    agentid = _agent_id()
    if agentid == 0:
        raise WeComError("agent_id 未配置或非整数")

    body = {
        "touser": touser,
        "toparty": toparty,
        "msgtype": "text",
        "agentid": agentid,
        "text": {"content": content},
    }
    url = f"{_api_base()}/cgi-bin/message/send"
    resp = httpx.post(url, params={"access_token": token}, json=body, timeout=10)
    data = resp.json()
    if data.get("errcode") != 0:
        raise WeComError(f"send 失败: {data.get('errmsg', data)}")
    return data


# ---------------------------------------------------------------------------
# 菜单
# ---------------------------------------------------------------------------

def menu_create(buttons: list[dict]) -> dict:
    token = get_token()
    agentid = _agent_id()
    url = f"{_api_base()}/cgi-bin/menu/create"
    resp = httpx.post(
        url, params={"access_token": token, "agentid": agentid},
        json={"button": buttons}, timeout=10,
    )
    data = resp.json()
    if data.get("errcode") != 0:
        raise WeComError(f"menu/create 失败: {data.get('errmsg', data)}")
    return data


def menu_get() -> dict:
    token = get_token()
    agentid = _agent_id()
    url = f"{_api_base()}/cgi-bin/menu/get"
    resp = httpx.get(
        url, params={"access_token": token, "agentid": agentid}, timeout=10,
    )
    return resp.json()


def menu_delete() -> dict:
    token = get_token()
    agentid = _agent_id()
    url = f"{_api_base()}/cgi-bin/menu/delete"
    resp = httpx.get(
        url, params={"access_token": token, "agentid": agentid}, timeout=10,
    )
    return resp.json()


# ---------------------------------------------------------------------------
# 回调加解密（WXBizMsgCrypt 等价实现）
# ---------------------------------------------------------------------------

def _decode_aes_key(encoding_aes_key: str) -> bytes:
    """企微 EncodingAESKey（43 字符）+ '=' → base64decode → 32 字节 AES key。"""
    return base64.b64decode(encoding_aes_key + "=")


def _sign(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """sha1(sorted([token, timestamp, nonce, encrypt]))。"""
    parts = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(parts).encode()).hexdigest()


def verify_signature(token: str, timestamp: str, nonce: str, encrypt: str,
                     msg_signature: str) -> bool:
    return _sign(token, timestamp, nonce, encrypt) == msg_signature


def _pkcs7_unpad(data: bytes) -> bytes:
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 32:
        return data  # 异常，原样返回让上层报错
    return data[:-pad_len]


def decrypt(encrypt_b64: str, encoding_aes_key: str, corpid: str) -> bytes:
    """AES-256-CBC 解密企微回调消息体。返回明文 XML bytes。"""
    key = _decode_aes_key(encoding_aes_key)
    iv = key[:16]
    ciphertext = base64.b64decode(encrypt_b64)

    from Crypto.Cipher import AES  # pycryptodome
    cipher = AES.new(key, AES.MODE_CBC, iv)
    plaintext = cipher.decrypt(ciphertext)
    plaintext = _pkcs7_unpad(plaintext)

    # 明文结构：16 字节随机串 + 4 字节大端 msg_len + msg + corpid
    _rand = plaintext[:16]
    msg_len = struct.unpack("!I", plaintext[16:20])[0]
    msg = plaintext[20:20 + msg_len]
    received_corpid = plaintext[20 + msg_len:].decode("utf-8", errors="replace")

    if corpid and received_corpid != corpid:
        raise WeComError(f"corpid 不匹配: 期望 {corpid} 实际 {received_corpid}")
    return msg


def encrypt(msg: bytes, encoding_aes_key: str, corpid: str) -> str:
    """加密（回包用）。返回 base64。"""
    import os
    key = _decode_aes_key(encoding_aes_key)
    iv = key[:16]
    rand = os.urandom(16)
    msg_len = struct.pack("!I", len(msg))
    plain = rand + msg_len + msg + corpid.encode()
    pad_len = 32 - (len(plain) % 32)
    plain += bytes([pad_len]) * pad_len

    from Crypto.Cipher import AES
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(plain)
    return base64.b64encode(ciphertext).decode()


def parse_callback_xml(xml_bytes: bytes) -> dict:
    """解析企微回调 XML，返回扁平 dict（含 Encrypt 或解密后的各字段）。"""
    root = ET.fromstring(xml_bytes)
    result: dict[str, str] = {}
    for child in root:
        result[child.tag] = child.text or ""
    return result


def build_encrypted_reply(msg: str, token: str, timestamp: str, nonce: str,
                          encoding_aes_key: str, corpid: str) -> str:
    """构造加密回包 XML（被动回复消息用）。"""
    encrypt_b64 = encrypt(msg.encode(), encoding_aes_key, corpid)
    signature = _sign(token, timestamp, nonce, encrypt_b64)
    return (
        "<xml>"
        f"<Encrypt><![CDATA[{encrypt_b64}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
        f"<TimeStamp>{timestamp}</TimeStamp>"
        f"<Nonce><![CDATA[{nonce}]]></Nonce>"
        "</xml>"
    )


# ---------------------------------------------------------------------------
# 测试连通性（面板「测试」按钮用）
# ---------------------------------------------------------------------------

def test_connection() -> tuple[bool, str]:
    """调 gettoken 验证凭据是否有效。返回 (ok, detail)。"""
    try:
        token = get_token()
        if token:
            return True, "access_token 获取成功，凭据有效"
        return False, "返回空 token"
    except WeComError as e:
        return False, str(e)
    except Exception as e:
        return False, f"异常: {type(e).__name__}: {e}"
