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
