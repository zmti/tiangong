"""系统状态与健康检查接口。"""
from __future__ import annotations

from fastapi import APIRouter, Query

router = APIRouter(prefix="/v1/system", tags=["system"])


@router.get("/health")
async def health_check() -> dict:
    """系统健康检查。"""
    from app.config import settings
    from app.core.multimodal import model_supports_vision

    # 检测各功能状态
    embedding_backend = (settings.embedding_backend or "local").lower()
    has_embedding_api = bool(settings.embedding_api_base_url or settings.openai_compat_base_url)

    llm_provider = settings.agent_llm_provider.lower()
    # compat 对应的配置字段是 openai_compat_*，需要特殊处理
    if llm_provider == "compat":
        llm_model = settings.openai_compat_model
    else:
        llm_model = getattr(settings, f"{llm_provider}_model", "") or settings.agent_llm_provider
    supports_vision = model_supports_vision(llm_model)

    features = {
        "embedding": {
            "backend": embedding_backend,
            "api_configured": has_embedding_api,
            "status": "semantic_search_available" if has_embedding_api else "keyword_only",
        },
        "image_generation": {
            "enabled": bool(settings.openai_compat_base_url),
            "config": bool(settings.openai_compat_base_url and settings.openai_compat_api_key),
        },
        "image_vision": {
            "enabled": supports_vision,
            "model": llm_model,
            "model_supports_vision": supports_vision,
        },
        "audio_asr": {
            "enabled": bool(settings.asr_base_url),
        },
        "audio_tts": {
            "enabled": bool(settings.tts_base_url),
        },
        "tracing": {
            "enabled": settings.agent_tracing_enabled,
            "otlp_configured": bool(settings.otel_exporter_otlp_endpoint),
        },
    }

    return {
        "status": "healthy",
        "features": features,
        "config_summary": {
            "llm_provider": llm_provider,
            "llm_model": llm_model,
            "embedding_backend": embedding_backend,
        },
        "recommendations": _get_recommendations(features),
    }


def _get_recommendations(features: dict) -> list[str]:
    """根据当前配置给出改进建议。"""
    recs = []
    if features["embedding"]["status"] == "keyword_only":
        recs.append("配置 EMBEDDING_API_BASE_URL + EMBEDDING_API_KEY 以启用语义检索")
    if not features["image_generation"]["enabled"]:
        recs.append("配置 OPENAI_COMPAT_BASE_URL + OPENAI_COMPAT_API_KEY 以启用图片生成")
    if not features["image_vision"]["enabled"]:
        recs.append("使用支持 vision 的模型 (如 gpt-4o, qwen-vl, deepseek-vl) 以启用图片识别")
    if not features["audio_asr"]["enabled"]:
        recs.append("配置 ASR_BASE_URL 以启用语音识别")
    if not features["audio_tts"]["enabled"]:
        recs.append("配置 TTS_BASE_URL 以启用语音合成")
    return recs


def _probe_postgres() -> dict:
    """探测 Postgres 结构化存储的可用性(不抛异常, 仅返回状态)。"""
    from app.config import settings

    dsn = settings.storage_postgres_dsn or settings.pgvector_client_dsn
    try:
        import psycopg  # noqa: F401
        psycopg_ok = True
        psycopg_version = getattr(psycopg, "__version__", "?")
    except ImportError:
        psycopg_ok = False
        psycopg_version = ""

    reachable = False
    detail = ""
    if dsn:
        try:
            import psycopg
            with psycopg.connect(dsn, connect_timeout=3):
                reachable = True
        except Exception as e:  # noqa: BLE001
            reachable = False
            detail = str(e).splitlines()[0][:200]
    elif not psycopg_ok:
        detail = "未安装 psycopg, 需 pip install \"psycopg[binary]\""
    elif not dsn:
        detail = "未配置 STORAGE_POSTGRES_DSN / PGVECTOR_CLIENT_DSN"

    return {
        "psycopg_installed": psycopg_ok,
        "psycopg_version": psycopg_version,
        "dsn_configured": bool(dsn),
        "reachable": reachable,
        "detail": detail,
    }


@router.get("/storage")
async def storage_status() -> dict:
    """结构化存储后端选择状态: 配置了什么、实际生效哪个、Postgres 是否可用。"""
    from app.config import settings
    from app.core import storage_backend as _sb

    configured = (settings.storage_backend or "sqlite").lower()
    active_type = type(_sb.get_storage_backend()).__name__

    probe = _probe_postgres() if configured in ("postgresql", "pg", "postgres") else None
    active_is_postgres = active_type == "PostgresBackend"

    return {
        "configured_backend": configured,
        "active_backend": active_type,
        "fell_back_to_sqlite": configured == "postgresql" and active_is_postgres is False,
        "postgres": probe,
        "switch_to_postgres": {
            "env": [
                "STORAGE_BACKEND=postgresql",
                "STORAGE_POSTGRES_DSN=postgresql://user:pass@host:5432/dbname",
            ],
            "pip": "pip install \"psycopg[binary]\"",
            "note": "配置缺失/连接不可达时自动回退 SQLite, 主流程不受影响",
        },
    }


@router.post("/cleanup")
async def trigger_cleanup(
    days: int = Query(30, ge=1, description="会话保留天数"),
    checkpoint_days: int = Query(None, ge=1, description="检查点保留天数，缺省读 checkpoint_ttl_days"),
) -> dict:
    """手动触发数据清理（过期会话 + 检查点 + 日志 + 临时文件）。

    检查点 TTL 缺省走 settings.checkpoint_ttl_days(=7)，以更早回收
    checkpoints.db 的大 state blob。仅按天数裁剪，不会删正在运行的记录。
    """
    from app.core import cleanup as _cln

    if checkpoint_days is None:
        from app.config import settings as _cfg
        checkpoint_days = int(getattr(_cfg, "checkpoint_ttl_days", 7) or 7)

    result = _cln.run_cleanup(days=days, checkpoint_days=checkpoint_days)
    # 附带清理后体积快照，便于确认实际回收
    stats = _cln.get_cleanup_stats()
    result["stats_after"] = stats
    return result
