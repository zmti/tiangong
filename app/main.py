"""FastAPI 入口。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import attachments, chat, documents, files, mcp, runs, schedule, tasks, telemetry, tools, tts
from app.api.security import require_api_key
from app.config import settings
from app.tools.registry import get_registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 预热: 触发工具注册
    from app.tools.registry import get_registry

    get_registry()
    
    # 初始化 MCP 连接(如果配置了)并把远程工具注册进 Agent 工具集
    try:
        from app.core.mcp_client import init_mcp_from_config
        from app.tools.mcp_tool import register_mcp_tools

        await init_mcp_from_config()
        register_mcp_tools()
    except Exception:
        pass  # MCP 初始化失败不影响主服务

    # 启动定时任务调度器(注册内置函数并加载持久化任务)
    try:
        from app.tools.schedule_tool import _get_scheduler

        _get_scheduler().start()
    except Exception:
        pass  # 调度器启动失败不影响主服务
    
    yield


app = FastAPI(title="Agent Service", version="1.0.0", lifespan=lifespan)

# 桌面端渲染进程(file:// 源)需跨域访问本地后端
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_auth = [Depends(require_api_key)] if settings.agent_api_key else []
app.include_router(chat.router, dependencies=_auth)
app.include_router(tools.router, dependencies=_auth)
app.include_router(files.router, dependencies=_auth)
app.include_router(documents.router, dependencies=_auth)
app.include_router(mcp.router, dependencies=_auth)
app.include_router(tts.router, dependencies=_auth)
app.include_router(telemetry.router, dependencies=_auth)
app.include_router(runs.router, dependencies=_auth)
app.include_router(tasks.router, dependencies=_auth)
app.include_router(attachments.router, dependencies=_auth)
app.include_router(schedule.router, dependencies=_auth)

# 本地生成图片的静态目录(data/images),供聊天与历史直接展示
_images_dir = os.path.join(settings.resolved_data_dir, "images")
os.makedirs(_images_dir, exist_ok=True)
app.mount("/images", StaticFiles(directory=_images_dir), name="images")


@app.get("/")
async def root() -> dict:
    return {
        "service": "agent-service",
        "provider": settings.agent_llm_provider,
        "version": "1.0.0",
        "endpoints": {
            "chat": ["/v1/chat", "/v1/chat/stream", "/v1/stream"],
            "tools": ["/v1/tools"],
            "documents": ["/v1/documents/upload", "/v1/documents/stats", "/v1/documents/all"],
            "mcp": ["/mcp/", "/mcp/tools", "/mcp/tools/{name}/call", "/mcp/tools/batch", "/mcp/health"],
        },
        "tools_count": len(get_registry().list(enabled_only=True)),
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
