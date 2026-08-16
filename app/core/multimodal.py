"""多模态接入主链路:图片 data URL、语音转录(ASR)预留接口、视觉能力探测。

设计:
- chat 主链路原生支持图片附件:图片以 data URL 塞进 OpenAI 兼容的
  content part(种子: {"type": "text"/"image_url"}),不再只是纯文本注入。
- 模型不支持视觉时自动降级:仍把图片随用户消息带上,但附注"图片已上传
  但当前模型无法理解视觉内容",避免静默丢信息。
- ASR 走 OpenAI 兼容 /audio/transcriptions(wisper-1 / paraformer 等),
  未配置端点时返回明确提示,方便后续接入。
"""
from __future__ import annotations

import base64
import os

from app.config import settings

# 模型名里命中这些子串则视为支持视觉输入
_VISION_MODEL_MARKERS = (
    "vl", "vision", "gpt-4o", "gpt-4.1", "gpt-4.5", "gemini", "claude", "qwen-vl",
    "glm-4v", "internvl", "minicpm-v", "agnes-vl",
)

# 支持的图片 mime
_IMAGE_MIMES = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
}


def model_supports_vision(model: str) -> bool:
    """按模型名启发式判断是否支持视觉输入(供降级提示)。"""
    low = (model or "").lower()
    return any(m in low for m in _VISION_MODEL_MARKERS)


def is_image_mime(mime: str) -> bool:
    return (mime or "").lower() in _IMAGE_MIMES


def image_to_data_url(content: bytes, mime: str) -> str:
    """把图片二进制转 data URL,mime 归一化;未知类型回退 png。"""
    mime = (mime or "").lower()
    if mime not in _IMAGE_MIMES:
        mime = "image/png"
    ext = _IMAGE_MIMES[mime]
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


def build_vision_content_part(data_url: str, caption: str = "") -> dict:
    """构造 OpenAI 兼容的图片 content part(用户消息 content 数组内)。"""
    if caption:
        return {
            "type": "text",
            "text": f"![附件]({data_url})\n{caption}".strip(),
        }
    return {"type": "text", "text": f"图片附件(请查看): {data_url}"}


def build_image_url_part(data_url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": data_url}}


def vision_notice(model: str, n_images: int) -> str:
    """模型不支持视觉时的降级提示,注入用户消息。"""
    return (
        f"[系统提示] 本次消息包含 {n_images} 张图片,但当前模型 "
        f"({model}) 不支持视觉输入,无法理解图片内容。请切换支持视觉的模型"
        f"(如 qwen-vl)后重新提问。"
    )


async def transcribe_audio(audio: bytes, filename: str = "audio.wav") -> str:
    """语音转文字(ASR)。调用 OpenAI 兼容 /audio/transcriptions。

    未配置 asr_base_url 或调用失败时,返回明确的引导提示(不抛异常,便于
    服务端兜底返回),方便后续接入 whisper / paraformer 等。
    """
    if not settings.asr_base_url or not settings.asr_api_key:
        return (
            "无法识别语音:尚未配置 ASR 服务。请在 .env 中设置 "
            "ASR_BASE_URL / ASR_API_KEY / ASR_MODEL(OpenAI 兼容 /audio/transcriptions 端点)后重试。"
        )
    import asyncio

    from openai import AsyncOpenAI

    model = settings.asr_model or "whisper-1"
    client = AsyncOpenAI(api_key=settings.asr_api_key, base_url=settings.asr_base_url)
    try:
        resp = await asyncio.wait_for(
            client.audio.transcriptions.create(
                model=model,
                file=(filename, audio),
            ),
            timeout=max(1, int(settings.asr_timeout)),
        )
        text = getattr(resp, "text", "") or ""
        return text.strip() or "未能从音频中识别出内容。"
    except Exception as e:  # noqa: BLE001
        return f"语音识别失败: {e}"