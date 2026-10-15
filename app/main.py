"""FastAPI 入口。"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api import attachments, chat, documents, project, files, mcp, mcp_server, runs, schedule, tasks, telemetry, tools, tts, user_profile, budget, advanced, analytics, assistants, plugins, safety, persistent_memory, planning, search, metrics, sessions, long_running, tenants, system, collaboration, reasoning, memory_cleanup, cross_session, collaboration_ui, artifacts, workflow, multimodal, rules, config_models, eval_ab, audit
from app.api.security import require_api_key, require_admin
from app.config import settings, APP_VERSION
from app.tools.registry import get_registry
from app.api.chat import list_models

logger = logging.getLogger(__name__)


def _setup_file_logging():
    """桌面端(Electron 拉起 uvicorn)时 stdout 只进主进程控制台,不可回查。
    追加一个文件 handler,把 WARNING+ 日志(含异常 traceback)落到 data/logs/backend.log,
    便于事后定位"LLM 流式调用失败: (空)"这类没有落库细节的错误。"""
    try:
        log_dir = os.path.join(settings.resolved_data_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.FileHandler(os.path.join(log_dir, "backend.log"), encoding="utf-8")
        fh.setLevel(logging.WARNING)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.WARNING)
        root.addHandler(fh)
        # uvicorn 启动时会用 logging.config.dictConfig(..., disable_existing_loggers=True)
        # 把已经创建好的 app.* logger 置为 disabled,导致 logger.exception 到不了 root handler
        # (表现为 backend.log 始终为空)。这里显式重新启用并恢复向上传播。
        for name in list(logging.root.manager.loggerDict):
            if name.startswith("app."):
                lg = logging.getLogger(name)
                lg.disabled = False
                lg.propagate = True
        # 启动标记: 既验证文件日志链路可用,也可据此确认进程加载的是哪份代码
        logger.warning("文件日志已启用 -> %s (pid=%s)", os.path.join(log_dir, "backend.log"), os.getpid())
    except Exception as e:  # noqa: BLE001
        logger.warning("文件日志初始化失败(不影响主服务): %s", e)


# ===== 初始化函数定义 =====

def _init_registry():
    """预热: 触发工具注册"""
    from app.tools.registry import get_registry
    get_registry()


def _check_embedding_backend():
    """嵌入后端质量检查"""
    try:
        if (settings.embedding_backend or "local").lower() == "local":
            backend = settings.embedding_api_base_url or settings.openai_compat_base_url
            if not backend:
                logger.warning(
                    "embedding_backend=local(HashEmbedding) 无语义检索能力,建议配置 "
                    "EMBEDDING_API_BASE_URL/EMBEDDING_API_KEY 以启用向量语义搜索"
                )
    except Exception as e:
        logger.warning("嵌入后端检查失败(不影响主服务): %s", e)


def _check_multimodal_features():
    """多模态功能检查:图片生成/识别/语音"""
    try:
        from app.core.multimodal import model_supports_vision

        # 图片生成检查
        if not settings.openai_compat_base_url:
            logger.warning(
                "[功能未启用] 图片生成功能需要配置 OPENAI_COMPAT_BASE_URL + OPENAI_COMPAT_API_KEY"
            )
        else:
            logger.info("[功能已启用] 图片生成 (base_url: %s)", settings.openai_compat_base_url)

        # 图片识别检查
        llm_provider = settings.agent_llm_provider.lower()
        # compat 对应的配置字段是 openai_compat_*，需要特殊处理
        if llm_provider == "compat":
            llm_model = settings.openai_compat_model
        else:
            llm_model = getattr(settings, f"{llm_provider}_model", "") or settings.agent_llm_provider
        if model_supports_vision(llm_model):
            logger.info("[功能已启用] 图片识别 (模型支持 vision: %s)", llm_model)
        else:
            logger.warning(
                "[功能受限] 图片识别可能不可用: 当前模型 '%s' 不支持视觉输入。"
                "建议使用支持 vision 的模型 (如 gpt-4o, qwen-vl, deepseek-vl)",
                llm_model
            )

        # ASR/TTS 检查
        if settings.asr_base_url:
            logger.info("[功能已启用] ASR 语音识别")
        else:
            logger.info("[功能未启用] ASR 语音识别 (需配置 ASR_BASE_URL)")

        if settings.tts_base_url:
            logger.info("[功能已启用] TTS 语音合成")
        else:
            logger.info("[功能未启用] TTS 语音合成 (需配置 TTS_BASE_URL)")

    except Exception as e:
        logger.warning("多模态功能检查失败(不影响主服务): %s", e)


# 后台任务引用:防止 MCP 初始化 task 在事件循环里被 GC(需保留到它注册完工具)
_mcp_init_task: Optional[asyncio.Task] = None
# 启动 checkpoint GC 后台任务引用(需在 _shutdown 取消, 避免悬空协程)
_ckpt_gc_task: Optional[asyncio.Task] = None


async def _init_mcp():
    """初始化 MCP 连接并把远程工具注册进 Agent 工具集。

    ⚠️ 本函数会被 _startup 以「后台 create_task」方式调度,而**不是**直接 await。
    原因:MCP 初始化可能很慢——stdio 服务如 windows-mcp(uvx) 首次握手 30s+,
    npx 类服务(如 github/playwright)首次要下载包。若同步 await 会阻塞 lifespan
    startup,FastAPI 进不了 yield、/health 迟迟不响应 → 桌面端 main.js 的
    waitForBackend(25s) 判活失败就反复 spawn 新后端,雪崩成无限死循环
    (2026-10-01 后端起不来的事故根因)。改为后台后:FastAPI 立即 serve、
    /health 立即 200;MCP 工具在后台注册完前 Agent 暂用不到(可接受)。
    """
    try:
        from app.core.mcp_client import init_mcp_from_config
        from app.tools.mcp_tool import register_mcp_tools

        await init_mcp_from_config()
        register_mcp_tools()
    except Exception as e:
        logger.warning("MCP 初始化失败(不影响主服务): %s", e)


def _init_scheduler():
    """启动定时任务调度器"""
    try:
        from app.tools.schedule_tool import _get_scheduler
        _get_scheduler().start()
    except Exception as e:
        logger.warning("定时任务调度器启动失败(不影响主服务): %s", e)


def _init_webhook_queue():
    """启动 Webhook 重试队列排空"""
    try:
        from app.core.webhook import start_webhook_queue_drainer
        start_webhook_queue_drainer()
    except Exception as e:
        logger.warning("Webhook 队列排空启动失败(不影响主服务): %s", e)


def _init_session_archiver():
    """启动会话自动清理后台任务"""
    try:
        from app.core.session_archive import get_session_archiver
        archiver = get_session_archiver()
        archiver.start_background_cleanup()
    except Exception as e:
        logger.warning("会话自动清理启动失败(不影响主服务): %s", e)


async def _init_long_running():
    """启动长时自主任务后台循环"""
    try:
        from app.core.long_running import get_long_running_manager
        mgr = get_long_running_manager()
        asyncio.get_running_loop().create_task(mgr.start_background_loop())
    except Exception as e:
        logger.warning("长时任务后台循环启动失败(不影响主服务): %s", e)


def _init_tenant_manager():
    """初始化租户管理器"""
    try:
        from app.core.tenant import get_tenant_manager
        get_tenant_manager()
    except Exception as e:
        logger.warning("租户管理器初始化失败(不影响主服务): %s", e)


def _init_tool_hot_loader():
    """启动工具热加载器"""
    try:
        from app.core.tool_watcher import start_tool_hot_loader
        start_tool_hot_loader()
    except Exception as e:
        logger.warning("工具热加载器启动失败(不影响主服务): %s", e)


def _init_memory_ttl():
    """启动记忆 TTL 后台清理 (使用统一记忆管理器)"""
    try:
        from app.core.memory_manager import get_memory_manager
        manager = get_memory_manager()
        # 统一记忆管理器已内置 TTL 和清理功能
        logger.info("统一记忆管理器初始化完成")
    except Exception as e:
        logger.warning("记忆管理器初始化失败(不影响主服务): %s", e)


async def _init_checkpoint_gc(delay: float = 15.0) -> None:
    """启动后自动跑一次过期 checkpoint 清理(主表终态 + 孤儿版本 + VACUUM)。

    checkpoint_versions 每行一份完整 state 快照, 跨 run 无 TTL 会无限膨胀
    (实测 182MB), 拖慢所有 checkpoint 读写(表现为运行卡顿/会话卡死)。
    此前 cleanup_expired 只有手动端点调用、从无人调度 -> 版本表只增不减。
    这里在启动后延迟 delay 秒(让后端完全起来、/health 可响应)后台跑一次,
    每次重启都会回收过期数据; 手动触发仍可用 POST /v1/runs/cleanup。

    放后台 create_task 而非在 _startup 里 await: VACUUM 需独占重写库文件,
    若阻塞 lifespan 会让 /health 迟迟不响应 -> 桌面端反复 spawn 后端死循环。
    全程 try/except, 清理失败不影响主服务启动。
    """
    try:
        await asyncio.sleep(delay)
        from app.config import settings as _cfg
        from app.runtime.checkpoint import CheckpointManager
        ttl = int(getattr(_cfg, "checkpoint_ttl_days", 7) or 7)
        cm = CheckpointManager()
        res = await cm.cleanup_expired(ttl_days=ttl)
        logger.info(
            "启动 checkpoint GC: 删主表 %d / 版本表 %d, VACUUM=%s (ttl=%dd)",
            res.get("checkpoints", 0), res.get("versions", 0),
            res.get("vacuum"), ttl,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("启动 checkpoint GC 失败(不影响主服务): %s", e)


def _init_audit_log():
    """启动审计日志后台刷新"""
    try:
        from app.core.audit import get_audit_logger
        logger_ = get_audit_logger()
        logger_.start()
        logger.info("审计日志已启动")
    except Exception as e:
        logger.warning("审计日志初始化失败(不影响主服务): %s", e)


async def _init_tool_route_cache():
    """初始化工具路由缓存"""
    try:
        from app.core.tool_route_cache import get_tool_route_cache
        cache = get_tool_route_cache()
        logger.info("工具路由缓存已初始化")
    except Exception as e:
        logger.warning("工具路由缓存初始化失败(不影响主服务): %s", e)


def _init_third_party_skills():
    """加载第三方技能定义(YAML/JSON 工具编排)进 SkillRegistry。

    第三方把技能定义文件丢进 settings.skill_third_party_dir(默认 <data_dir>/skills),
    重启后端即被注册,Agent 的 list_skills / run_skill 工具直接可见、可调用。
    这是第三方声明式技能的入口——之前 load_skills_from_directory 全仓零调用,
    第三方技能根本进不来。
    """
    if not settings.agent_skill_marketplace_enabled:
        return
    try:
        from app.core.skills import load_skills_from_directory
        import yaml  # noqa: F401  确保 yaml 可用(load_skills_from_directory 内部用)
        skill_dir = settings.skill_third_party_dir or os.path.join(settings.resolved_data_dir, "skills")
        if not os.path.isdir(skill_dir):
            logger.info("第三方技能目录不存在,跳过: %s", skill_dir)
            return
        n = load_skills_from_directory(skill_dir)
        if n:
            logger.info("已加载 %d 个第三方技能(目录: %s)", n, skill_dir)
        else:
            logger.info("第三方技能目录为空: %s", skill_dir)
    except Exception as e:
        logger.warning("第三方技能加载失败(不影响主服务): %s", e)


async def _init_workflow_schedules():
    """启动工作流定时调度后台循环"""
    try:
        import uuid as _uuid

        from app.api.workflow import _schedules, _load_workflow, _ensure_executor
        from app.core.workflow.db import get_db
        import asyncio

        async def _schedule_runner():
            while True:
                try:
                    now = time.time()
                    for sched_id, sched in list(_schedules.items()):
                        if not sched.get("enabled", True):
                            continue
                        next_run = sched.get("next_run_at", 0)
                        if next_run and now >= next_run:
                            wf_id = sched["workflow_id"]
                            wf = _load_workflow(wf_id)
                            if wf:
                                executor = _ensure_executor(wf_id)
                                run_id = f"run_{int(now * 1000)}_{_uuid.uuid4().hex[:6]}"
                                await executor.run(
                                    wf, sched.get("inputs", {}),
                                    run_id=run_id, session_id=sched.get("session_id", "default"),
                                )
                                logger.info(f"定时调度执行完成: {sched_id} -> {wf_id}")
                            # 计算下次运行时间
                            try:
                                from croniter import croniter
                                import datetime
                                cron = croniter(sched["cron_expr"], datetime.datetime.now())
                                next_run_ts = cron.get_next(datetime.datetime).timestamp()
                                _schedules[sched_id]["next_run_at"] = next_run_ts
                                get_db().update_schedule(sched_id, next_run_at=next_run_ts,
                                                        last_run_at=now)
                            except Exception as e2:
                                logger.warning(f"调度时间计算失败 {sched_id}: {e2}")
                except Exception as e:
                    logger.warning(f"调度循环异常: {e}")
                await asyncio.sleep(10)  # 每10秒检查一次

        asyncio.get_running_loop().create_task(_schedule_runner())
        logger.info("工作流调度器已启动")
    except Exception as e:
        logger.warning("工作流调度器启动失败(不影响主服务): %s", e)


# ===== 停止函数定义 =====

async def _stop_webhook_queue():
    try:
        from app.core.webhook import stop_webhook_queue_drainer
        await stop_webhook_queue_drainer()
    except Exception as e:
        logger.warning("Webhook 队列排空停止失败: %s", e)


def _stop_session_archiver():
    try:
        from app.core.session_archive import get_session_archiver
        get_session_archiver().stop_background_cleanup()
    except Exception as e:
        logger.warning("会话自动清理停止失败: %s", e)


async def _stop_long_running():
    try:
        from app.core.long_running import get_long_running_manager
        mgr = get_long_running_manager()
        await mgr.stop_background_loop()
    except Exception as e:
        logger.warning("长时任务循环停止失败: %s", e)


def _stop_memory_ttl():
    """停止记忆 TTL 清理 (统一记忆管理器无需单独停止)"""
    pass


# ===== 统一初始化入口 =====

def _sweep_orphan_runs(db_path: Optional[str] = None):
    """启动清扫:把上一个进程遗留的 running 孤儿(后端崩溃/重启时被遗弃的 run)
    标记为 interrupted,避免前端恢复时读到 running 永远 loading。

    仅清扫 updated_at 早于本次启动的 run —— 启动时后端刚拉起,不可能有正在进行的
    run,因此全部 running 都是上一进程的孤儿,安全改写。

    Args:
        db_path: 测试可注入临时库;生产留空则使用 settings.resolved_data_dir 下的
            checkpoints.db。
    """
    try:
        if not db_path:
            from app.config import settings as _s
            db_path = os.path.join(getattr(_s, "resolved_data_dir", ""), "checkpoints.db")
        if not db_path or not os.path.exists(db_path):
            return
        con = sqlite3.connect(db_path, timeout=10)
        try:
            now = time.time()
            # 主表:status 列 + state JSON 顶层 status 同步改写,并记录清扫标记
            cur = con.execute(
                "UPDATE checkpoints SET "
                "status='interrupted', updated_at=?, "
                "state=json_set(json_set(state,'$.status','interrupted'),"
                "'$.metadata.swept_by_startup',1) "
                "WHERE status='running' AND updated_at < ?",
                (now, now - 5.0),
            )
            n = cur.rowcount
            con.commit()
            if n:
                logger.warning(
                    "启动清扫:将 %d 个遗留 running run 标记为 interrupted(后端重启孤儿)", n
                )
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("启动清扫失败(不影响启动): %s", e)


async def _startup():
    """应用启动时统一初始化所有组件"""
    # 同步初始化
    _init_registry()
    _init_third_party_skills()  # 第三方声明式技能(YAML/JSON)进 SkillRegistry,list_skills/run_skill 可见
    _check_embedding_backend()
    _check_multimodal_features()
    _sweep_orphan_runs()  # 必须在其他组件之前:清扫上一进程崩溃遗留的 running 孤儿
    _init_scheduler()
    _init_webhook_queue()
    _init_session_archiver()
    _init_tenant_manager()
    _init_tool_hot_loader()
    _init_memory_ttl()
    _init_audit_log()

    # 异步初始化
    # MCP 放后台跑:stdio/npx 服务连接可能很慢(30s~120s),若在此 await 会阻塞
    # lifespan → /health 不响应 → 桌面端反复 spawn 后端成死循环。后台后 FastAPI
    # 立即 serve,MCP 工具异步注册(注册完前 Agent 暂用不到,可接受)。见 _init_mcp docstring。
    global _mcp_init_task, _ckpt_gc_task
    _mcp_init_task = asyncio.create_task(_init_mcp())
    _ckpt_gc_task = asyncio.create_task(_init_checkpoint_gc())  # 启动后延迟回收过期 checkpoint
    await _init_long_running()
    await _init_tool_route_cache()
    await _init_workflow_schedules()


async def _shutdown():
    """应用关闭时优雅停止所有组件"""
    # 取消仍在后台跑的 MCP 初始化 task(若尚未注册完,关闭时丢弃,不留悬空协程)
    global _mcp_init_task, _ckpt_gc_task
    if _mcp_init_task is not None and not _mcp_init_task.done():
        _mcp_init_task.cancel()
        try:
            await _mcp_init_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _mcp_init_task = None
    # 取消启动 checkpoint GC(若 15s 延迟内就关闭,避免悬空协程)
    if _ckpt_gc_task is not None and not _ckpt_gc_task.done():
        _ckpt_gc_task.cancel()
        try:
            await _ckpt_gc_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _ckpt_gc_task = None
    await _stop_webhook_queue()
    _stop_session_archiver()
    await _stop_long_running()
    _stop_memory_ttl()
    
    # 停止审计日志
    try:
        from app.core.audit import get_audit_logger
        await get_audit_logger().stop()
    except Exception as e:
        logger.warning("审计日志停止失败: %s", e)
    
    # 停止 Telemetry 清理
    try:
        from app.core.telemetry import tracer
        tracer.stop_background_cleanup()
    except Exception as e:
        logger.warning("Telemetry 清理停止失败: %s", e)
    
    # 停止批量 Checkpoint 管理器
    try:
        from app.runtime.batch_checkpoint import stop_batch_checkpoint_manager
        await stop_batch_checkpoint_manager()
    except Exception as e:
        logger.warning("批量 Checkpoint 管理器停止失败: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_file_logging()
    await _startup()
    yield
    await _shutdown()


app = FastAPI(title="Agent Service", version=APP_VERSION, lifespan=lifespan)

# 桌面端渲染进程(file:// 源)需跨域访问本地后端
# 生产环境建议通过环境变量配置允许的源列表
_cors_origins = os.environ.get("CORS_ORIGINS", "").split(",") if os.environ.get("CORS_ORIGINS") else []
if _cors_origins and "" in _cors_origins:
    _cors_origins.remove("")
if _cors_origins:
    # 明确指定允许的来源
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # 开发模式:允许所有来源(仅本地桌面端)
    logger.warning("CORS 未配置,允许所有来源(开发模式)")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

_auth = [Depends(require_api_key)] if settings.agent_api_key else []
app.include_router(project.router, dependencies=_auth)
app.include_router(chat.router, dependencies=_auth)
app.add_api_route('/v1/models', list_models, methods=['GET'], tags=['chat'])

app.include_router(tools.router, dependencies=_auth)
app.include_router(files.router, dependencies=_auth)
app.include_router(documents.router, dependencies=_auth)
app.include_router(mcp.router, dependencies=_auth)
app.include_router(mcp_server.router, dependencies=_auth)
app.include_router(tts.router, dependencies=_auth)
app.include_router(config_models.router, dependencies=_auth)  # 生成模型配置(读写)
app.include_router(telemetry.router, dependencies=_auth)
app.include_router(runs.router, dependencies=_auth)
app.include_router(tasks.router, dependencies=_auth)
app.include_router(workflow.router, dependencies=_auth)
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
app.include_router(multimodal.router, dependencies=_auth)
app.include_router(long_running.router, dependencies=_auth)  # 长时自主任务
app.include_router(tenants.router, dependencies=[Depends(require_admin)])  # 租户管理(仅管理员可操作)
app.include_router(system.router)  # 系统状态检查(无需认证)
app.include_router(collaboration.router, dependencies=_auth)
app.include_router(reasoning.router, dependencies=_auth)
app.include_router(memory_cleanup.router, dependencies=_auth)
app.include_router(cross_session.router, dependencies=_auth)
app.include_router(collaboration_ui.router, dependencies=_auth)
app.include_router(artifacts.router, dependencies=_auth)  # Artifacts 实时渲染预览
app.include_router(rules.router, dependencies=_auth)  # 智能体规则
app.include_router(eval_ab.router, dependencies=_auth)  # A/B 评估对比
app.include_router(audit.router, dependencies=_auth)  # 审计日志

# 本地生成图片的静态目录(data/images),供聊天与历史直接展示
_images_dir = os.path.join(settings.resolved_data_dir, "images")
os.makedirs(_images_dir, exist_ok=True)
# 使用 API 路由代替 StaticFiles，避免 PyInstaller 打包问题
@app.get("/images/{filename}")
async def get_image(filename: str):
    """提供本地生成的图片"""
    from fastapi import HTTPException
    # 防路径穿越:规范化路径并严格校验
    real_images_dir = os.path.realpath(_images_dir)
    requested_path = os.path.realpath(os.path.join(real_images_dir, filename))
    # 严格检查:请求路径必须是图片目录的子路径
    if not (requested_path == real_images_dir or requested_path.startswith(real_images_dir + os.sep)):
        raise HTTPException(status_code=403, detail="访问被拒绝:路径穿越检测")
    # 额外安全检查:不允许路径中包含特殊字符
    if ".." in filename or "~" in filename or "\\" in filename or "/" in filename.replace(os.sep, ""):
        raise HTTPException(status_code=400, detail="非法文件名")
    if not os.path.isfile(requested_path):
        raise HTTPException(status_code=404, detail="File not found")
    # 检测 MIME 类型
    ext = os.path.splitext(filename)[1].lower()
    mime_types = {
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.bmp': 'image/bmp',
    }
    mime_type = mime_types.get(ext, 'application/octet-stream')
    return FileResponse(requested_path, media_type=mime_type)

# 本地生成视频的静态目录(data/videos),供聊天与历史直接播放
_videos_dir = os.path.join(settings.resolved_data_dir, "videos")
os.makedirs(_videos_dir, exist_ok=True)
# 使用 API 路由代替 StaticFiles，避免 PyInstaller 打包问题并防止路径穿越
@app.get("/videos/{filename}")
async def get_video(filename: str):
    """提供本地生成的视频"""
    from fastapi import HTTPException
    real_videos_dir = os.path.realpath(_videos_dir)
    requested_path = os.path.realpath(os.path.join(real_videos_dir, filename))
    # 严格检查:请求路径必须是视频目录的子路径
    if not (requested_path == real_videos_dir or requested_path.startswith(real_videos_dir + os.sep)):
        raise HTTPException(status_code=403, detail="访问被拒绝:路径穿越检测")
    # 额外安全检查:不允许路径中包含特殊字符
    if ".." in filename or "~" in filename or "\\" in filename or "/" in filename.replace(os.sep, ""):
        raise HTTPException(status_code=400, detail="非法文件名")
    if not os.path.isfile(requested_path):
        raise HTTPException(status_code=404, detail="File not found")
    import mimetypes
    mime_type, _ = mimetypes.guess_type(filename)
    mime_type = mime_type or 'video/mp4'
    return FileResponse(requested_path, media_type=mime_type)


@app.get("/")
async def root() -> dict:
    return {
        "service": "agent-service",
        "provider": settings.agent_llm_provider,
        "version": APP_VERSION,
        "endpoints": {
            "chat": ["/v1/chat", "/v1/chat/stream", "/v1/stream"],
            "tools": ["/v1/tools"],
            "documents": ["/v1/documents/upload", "/v1/documents/stats", "/v1/documents/all"],
            "mcp": ["/mcp/", "/mcp/tools", "/mcp/tools/{name}/call", "/mcp/tools/batch", "/mcp/health"],
        },
        "tools_count": len(get_registry().list_sync(enabled_only=True)),
    }


@app.get("/health")
async def health() -> dict:
    """细化健康检查:返回 LLM 连通性、数据库状态、磁盘用量、运行时统计。"""
    import os as _os
    import sqlite3 as _sqlite3
    import time as _time
    from pathlib import Path as _Path

    from app.config import APP_VERSION

    status = {"status": "ok", "version": APP_VERSION}

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
    _VALID_TABLE_NAMES = {"sessions", "checkpoints", "tasks", "memories", "memory"}
    # 各库的实际表名:memory.db 建表名为 memories(旧版可能为 memory),逐个候选尝试
    _TABLE_CANDIDATES = {
        "sessions": ("sessions",),
        "checkpoints": ("checkpoints",),
        "tasks": ("tasks",),
        "memory": ("memories", "memory"),
    }
    for db_name, db_path in [
        ("sessions", _os.path.join(settings.resolved_data_dir, "sessions.db")),
        ("checkpoints", _os.path.join(settings.resolved_data_dir, "checkpoints.db")),
        ("tasks", _os.path.join(settings.resolved_data_dir, "tasks.db")),
        ("memory", _os.path.join(settings.resolved_data_dir, "memory.db")),
    ]:
        try:
            if _os.path.isfile(db_path):
                with _sqlite3.connect(db_path) as conn:
                    count = None
                    last_err = None
                    for table_name in _TABLE_CANDIDATES.get(db_name, (db_name,)):
                        if table_name not in _VALID_TABLE_NAMES:
                            continue
                        try:
                            rows = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
                            count = rows[0] if rows else 0
                            break
                        except Exception as te:  # noqa: BLE001  该候选表不存在,试下一个
                            last_err = te
                    if count is None:
                        raise last_err or RuntimeError("无可用表")
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
        # 用 shutil.disk_usage:os.statvfs 在 Windows 上不存在,原实现在此平台必然抛错
        import shutil as _shutil
        _usage = _shutil.disk_usage(settings.resolved_data_dir)
        disk_total, disk_free = _usage.total, _usage.free
        status["disk"] = {
            "data_dir_size_mb": round(total_size / 1024 / 1024, 2),
            "disk_total_gb": round(disk_total / 1024 / 1024 / 1024, 2),
            "disk_free_gb": round(disk_free / 1024 / 1024 / 1024, 2),
            "free_percent": round(disk_free / max(1, disk_total) * 100, 1),
        }
        if disk_free < disk_total * 0.05:  # 剩余不足 5%
            status["status"] = "degraded"
    except Exception as e:  # noqa: BLE001
        logger.debug("磁盘信息读取失败: %s", e)
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
    except Exception as e:  # noqa: BLE001
        logger.debug("运行时指标读取失败: %s", e)
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
    except Exception as e:  # noqa: BLE001
        logger.debug("任务状态读取失败: %s", e)
        status["tasks"] = {"error": "无法读取任务状态"}

    # 综合状态
    if all(v.get("connected") for v in db_status.values()):
        status["status"] = "ok"
    else:
        status["status"] = "degraded"

    return status
