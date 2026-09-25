"""FastAPI 入口。

- 启动时建表 + 补 settings + 启动监控调度
- 挂载 /api/* 路由、/wecom/callback 回调、静态文件面板
- 企微回调路由由 main 直接挂载（不走 /api 前缀）
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import db, monitor
from .api import routes as api_routes
from .api import callback as callback_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("argus")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动
    logger.info("初始化数据库...")
    db.init_db()
    logger.info("启动监控调度器...")
    monitor.start()
    yield
    # 关闭
    logger.info("停止监控调度器...")
    monitor.stop()


app = FastAPI(title="Argus", version="1.0.0", lifespan=lifespan)

# API 路由（/api 前缀）
app.include_router(api_routes.router)
# 企微回调（/wecom/callback，无 /api 前缀）
app.include_router(callback_routes.router)

# 静态文件面板
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", response_model=None)
async def index() -> FileResponse | PlainTextResponse:
    """面板首页：SPA 入口。"""
    index_path = os.path.join(_STATIC_DIR, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path)
    return PlainTextResponse("面板前端未部署（app/static/index.html 缺失）", status_code=503)
