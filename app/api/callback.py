"""企微回调路由：/wecom/callback GET 验证 + POST 消息接收。

不鉴权（企微直接访问），但必须验签 + AES 解密。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse, Response as FastResponse

from .. import db, monitor, wecom_client

router = APIRouter()


def _callback_params(request: Request) -> tuple[str, str, str, str]:
    """从 query 提取验签四元组。"""
    msg_signature = request.query_params.get("msg_signature", "")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")
    echostr = request.query_params.get("echostr", "")
    return msg_signature, timestamp, nonce, echostr


def _callback_creds() -> tuple[str, str, str]:
    """(token, aes_key, corp_id)。"""
    token = db.get_setting("wecom.callback_token", "")
    aes_key = db.get_setting("wecom.callback_aes_key", "")
    corp_id = db.get_setting("wecom.corp_id", "")
    return token, aes_key, corp_id


@router.get("/wecom/callback")
async def callback_verify(request: Request) -> PlainTextResponse:
    """URL 验证：校验签名 → AES 解密 echostr → 返回明文 echostr。"""
    msg_signature, timestamp, nonce, echostr = _callback_params(request)
    token, aes_key, corp_id = _callback_creds()

    if not all([token, aes_key, corp_id, msg_signature, timestamp, nonce, echostr]):
        return PlainTextResponse("missing params", status_code=400)

    if not wecom_client.verify_signature(token, timestamp, nonce, echostr, msg_signature):
        return PlainTextResponse("signature mismatch", status_code=403)

    try:
        plain = wecom_client.decrypt(echostr, aes_key, corp_id)
        return PlainTextResponse(plain.decode("utf-8", errors="replace"))
    except Exception:
        return PlainTextResponse("decrypt failed", status_code=500)


@router.post("/wecom/callback")
async def callback_receive(request: Request) -> PlainTextResponse:
    """接收消息/事件：验签 + 解密 + 处理 click 事件，5 秒内返回 success。"""
    msg_signature, timestamp, nonce, _ = _callback_params(request)
    token, aes_key, corp_id = _callback_creds()
    body = await request.body()

    if not all([token, aes_key, corp_id]):
        return PlainTextResponse("success")

    try:
        parsed = wecom_client.parse_callback_xml(body)
        encrypt = parsed.get("Encrypt", "")
        if not encrypt:
            return PlainTextResponse("success")

        if not wecom_client.verify_signature(token, timestamp, nonce, encrypt, msg_signature):
            return PlainTextResponse("success")

        plain = wecom_client.decrypt(encrypt, aes_key, corp_id)
        msg = wecom_client.parse_callback_xml(plain)
    except Exception:
        # 任何异常都返回 success，避免企微重试
        return PlainTextResponse("success")

    db.add_event("wecom_callback", dict(msg))

    # 处理 click 事件
    if msg.get("MsgType") == "event" and msg.get("Event") == "click":
        event_key = msg.get("EventKey", "")
        if event_key == "SILENCE_1H":
            monitor.silence(60)
            reply_msg = f"已开启静音 1 小时。当前剩余 {monitor.silence_remaining()} 分钟。"
        elif event_key == "UNSILENCE":
            monitor.unsilence()
            reply_msg = "已解除静音。告警恢复正常推送。"
        else:
            return PlainTextResponse("success")
        # 被动回复确认消息（加密回包）
        ts = str(int(time.time()))
        nonce_resp = nonce or str(int(time.time()))
        reply_xml = wecom_client.build_encrypted_reply(
            reply_msg, token, ts, nonce_resp, aes_key, corp_id,
        )
        return FastResponse(content=reply_xml, media_type="application/xml")

    return PlainTextResponse("success")
