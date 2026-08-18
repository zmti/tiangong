"""FastAPI 入口。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import attachments, chat, documents, files, mcp, mcp_server, runs, schedule, tasks, telemetry, tools, tts, user_profile, budget, advanced, analytics, assistants, plugins, safety, persistent_memory, planning, search, metrics, sessions, long_running, tenants, system, collaboration, reasoning, memory_cleanup, cross_session, collaboration_ui, artifacts
from app.api.security import require_api_key, require_admin
from app.config import settings
from app.tools.registry import get_registry
from app.api.chat import list_models



@asynccontextmanager
async def lifespan(app: FastAPI):
    # 预热: 触发工具注册
    from app.tools.registry import get_registry

    get_registry()

    # 嵌入后端质量检查:local(HashEmbedding)无语义,建议配置 API 后端
    try:
        from app.config import settings as _cfg
        if (_cfg.embedding_backend or "local").lower() == "local":
            backend = _cfg.embedding_api_base_url or _cfg.openai_compat_base_url
            if not backend:
                logger.warning(
                    "embedding_backend=local(HashEmbedding) 无语义检索能力,建议配置 "
                    "EMBEDDING_API_BASE_URL/EMBEDDING_API_KEY 以启用向量语义搜索"
                )
    except Exception:
        pass

    # 多模态功能检查:图片生成/识别/语音
    try:
        from app.config import settings as _cfg
        from app.core.multimodal import model_supports_vision

        # 图片生成检查
        if not _cfg.openai_compat_base_url:
            logger.warning(
                "[功能未启用] 图片生成功能需要配置 OPENAI_COMPAT_BASE_URL + OPENAI_COMPAT_API_KEY"
            )
        else:
            logger.info("[功能已启用] 图片生成 (base_url: %s)", _cfg.openai_compat_base_url)

        # 图片识别检查
        llm_provider = _cfg.agent_llm_provider.lower()
        llm_model = getattr(_cfg, f"{llm_provider}_model", "") or _cfg.agent_llm_provider
        if model_supports_vision(llm_model):
            logger.info("[功能已启用] 图片识别 (模型支持 vision: %s)", llm_model)
        else:
            logger.warning(
                "[功能受限] 图片识别可能不可用: 当前模型 '%s' 不支持视觉输入。"
                "建议使用支持 vision 的模型 (如 gpt-4o, qwen-vl, deepseek-vl)",
                llm_model
            )

        # ASR/TTS 检查
        if _cfg.asr_base_url:
            logger.info("[功能已启用] ASR 语音识别")
        else:
            logger.info("[功能未启用] ASR 语音识别 (需配置 ASR_BASE_URL)")

        if _cfg.tts_base_url:
            logger.info("[功能已启用] TTS 语音合成")
        else:
            logger.info("[功能未启用] TTS 语音合成 (需配置 TTS_BASE_URL)")

    except Exception:
        pass
    
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

    # 启动 Webhook 重试队列排空(后台定期检查未送达事件)
    try:
        from app.core.webhook import start_webhook_queue_drainer

        start_webhook_queue_drainer()
    except Exception:
        pass  # Webhook 初始化失败不影响主服务

    # 启动会话自动清理后台任务(A4)
    try:
        from app.core.session_archive import get_session_archiver

        archiver = get_session_archiver()
        archiver.start_background_cleanup()
    except Exception:
        pass  # 会话清理启动失败不影响主服务

    # 启动长时自主任务后台循环(M5)
    try:
        from app.core.long_running import get_long_running_manager

        mgr = get_long_running_manager()
        asyncio.get_event_loop().create_task(mgr.start_background_loop())
    except Exception:
        pass  # 长时任务循环启动失败不影响主服务

    # 初始化租户管理器(确保数据表就绪)
    try:
        from app.core.tenant import get_tenant_manager

        get_tenant_manager()
    except Exception:
        pass  # 租户初始化失败不影响主服务

    # 启动工具热加载器(app/tools/ 下新增 .py 自动注册)
    try:
        from app.core.tool_watcher import start_tool_hot_loader

        start_tool_hot_loader()
    except Exception:
        pass  # 热加载器启动失败不影响主服务

    # 启动记忆 TTL 后台清理(自动淘汰过期低价值记忆)
    try:
        from app.core.memory_ttl import start_memory_ttl_cleanup

        start_memory_ttl_cleanup()
    except Exception:
        pass  # 记忆 TTL 初始化失败不影响主服务

    yield

    # 优雅关闭:停止 Webhook 队列排空任务
    try:
        from app.core.webhook import stop_webhook_queue_drainer

        await stop_webhook_queue_drainer()
    except Exception:
        pass

    # 停止会话自动清理
    try:
        from app.core.session_archive import get_session_archiver

        get_session_archiver().stop_background_cleanup()
    except Exception:
        pass

    # 停止长时任务循环
    try:
        from app.core.long_running import get_long_running_manager

        mgr = get_long_running_manager()
        await mgr.stop_background_loop()
    except Exception:
        pass

    # 停止记忆 TTL 后台清理
    try:
        from app.core.memory_ttl import stop_memory_ttl_cleanup

        stop_memory_ttl_cleanup()
    except Exception:
        pass


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
app.add_api_route('/v1/models', list_models, methods=['GET'], tags=['chat'])

app.include_router(tools.router, dependencies=_auth)
app.include_router(files.router, dependencies=_auth)
app.include_router(documents.router, dependencies=_auth)
app.include_router(mcp.router, dependencies=_auth)
app.include_router(mcp_server.router, dependencies=_auth)
app.include_router(tts.router, dependencies=_auth)
app.include_router(telemetry.router, dependencies=_auth)
app.include_router(runs.router, dependencies=_auth)
app.include_router(tasks.router, dependencies=_auth)
app.include_router(attachments.router, dependencies=_auth)
app.include_router(schedule.router, dependencies=_auth)
app.include_router(user_profile.router, dependencies=_auth)
app.include_router(budget.router, dependencies=_auth)
app.include_router(advanced.router, dependencies=_auth)
app.include_router(analytics.router, dependencies=_auth)
app.include_router(plugins.router, dependencies=_auth)
app.include_router(assistants.router, dependencies=_auth)  # OpenAI Assistants API 兼容
app.include_router(metrics.router, dependencies=_auth)  # Prometheus metrics
app.include_router(sessions.router, dependencies=_auth)  # 会话归档/导入导出
app.include_router(safety.router, dependencies=_auth)
app.include_router(persistent_memory.router, dependencies=_auth)
app.include_router(planning.router, dependencies=_auth)
app.include_router(search.router, dependencies=_auth)
app.include_router(long_running.router, dependencies=_auth)  # 长时自主任务
app.include_router(tenants.router, dependencies=[Depends(require_admin)])  # 租户管理(仅管理员可操作)
app.include_router(system.router)  # 系统状态检查(无需认证)
app.include_router(collaboration.router, dependencies=_auth)
app.include_router(reasoning.router, dependencies=_auth)
app.include_router(memory_cleanup.router, dependencies=_auth)
app.include_router(cross_session.router, dependencies=_auth)
app.include_router(collaboration_ui.router, dependencies=_auth)
app.include_router(artifacts.router, dependencies=_auth)  # Artifacts 实时渲染预览

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
    """细化健康检查:返回 LLM 连通性、数据库状态、磁盘用量、运行时统计。"""
    import os as _os
    import sqlite3 as _sqlite3
    import time as _time
    from pathlib import Path as _Path

    status = {"status": "ok"}

    # 1. LLM 连通性(轻量探测:尝试解析 provider 配置,不做实际请求)
    llm_ok = bool(settings.agent_llm_provider)
    llm_detail = {
        "provider": settings.agent_llm_provider,
        "model": (getattr(settings, f"{settings.agent_llm_provider}_model", None) or
                  settings.openai_compat_model or ""),
        "key_configured": bool(getattr(settings, f"{settings.agent_llm_provider}_api_key", "") or
                              settings.openai_compat_api_key),
    }
    if not llm_detail["key_configured"] and settings.agent_llm_provider not in ("ollama",):
        llm_ok = False
        llm_detail["error"] = "API Key 未配置"
    status["llm"] = llm_detail

    # 2. 数据库连通性(Sessions + Checkpoints + Tasks)
    db_status = {}
    for db_name, db_path in [
        ("sessions", _os.path.join(settings.resolved_data_dir, "sessions.db")),
        ("checkpoints", _os.path.join(settings.resolved_data_dir, "checkpoints.db")),
        ("tasks", _os.path.join(settings.resolved_data_dir, "tasks.db")),
        ("memory", _os.path.join(settings.resolved_data_dir, "memory.db")),
    ]:
        try:
            if _os.path.isfile(db_path):
                with _sqlite3.connect(db_path) as conn:
                    table_name = "checkpoints" if db_name == "checkpoints" else db_name
                    rows = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
                    count = rows[0] if rows else 0
                db_status[db_name] = {"connected": True, "records": count}
            else:
                db_status[db_name] = {"connected": True, "records": 0, "note": "未创建(首次写入时自动建表)"}
        except Exception as e:  # noqa: BLE001
            db_status[db_name] = {"connected": False, "error": str(e)[:100]}
            llm_ok = False
    status["databases"] = db_status

    # 3. 磁盘用量(data 目录)
    data_dir = _Path(settings.resolved_data_dir)
    try:
        total_size = sum(f.stat().st_size for f in data_dir.rglob("*") if f.is_file())
        disk_total = _os.statvfs(settings.resolved_data_dir).f_frsize * _os.statvfs(settings.resolved_data_dir).f_blocks
        disk_free = _os.statvfs(settings.resolved_data_dir).f_frsize * _os.statvfs(settings.resolved_data_dir).f_bavail
        status["disk"] = {
            "data_dir_size_mb": round(total_size / 1024 / 1024, 2),
            "disk_total_gb": round(disk_total / 1024 / 1024 / 1024, 2),
            "disk_free_gb": round(disk_free / 1024 / 1024 / 1024, 2),
            "free_percent": round(disk_free / max(1, disk_total) * 100, 1),
        }
        if disk_free < disk_total * 0.05:  # 剩余不足 5%
            status["status"] = "degraded"
    except Exception:  # noqa: BLE001
        status["disk"] = {"error": "无法读取磁盘信息"}

    # 4. 运行时统计
    try:
        from app.core.telemetry import tracer as _tracer
        recent = _tracer.recent(limit=10)
        llm_calls_1min = sum(1 for e in recent if e.get("type") == "llm_call" and
                             (_time.time() - e.get("ts", 0)) < 60)
        status["runtime"] = {
            "recent_llm_calls_1min": llm_calls_1min,
            "buffer_size": len(_tracer._buffer),  # noqa: SLF001
        }
    except Exception:  # noqa: BLE001
        status["runtime"] = {"error": "无法读取运行时指标"}

    # 5. 后台任务状态
    try:
        from app.core.tasks import get_task_manager as _get_tm
        mgr = _get_tm()
        all_tasks = mgr.list(limit=100)
        status["tasks"] = {
            "total": len(all_tasks),
            "running": sum(1 for t in all_tasks if t.get("status") == "running"),
            "queued": sum(1 for t in all_tasks if t.get("status") == "queued"),
        }
    except Exception:  # noqa: BLE001
        status["tasks"] = {"error": "无法读取任务状态"}

    # 综合状态
    if all(v.get("connected") for v in db_status.values()):
        status["status"] = "ok"
    else:
        status["status"] = "degraded"

    return status
