"""增强型多模态工具:图像深度理解、语音交互、视频分析。

对标 Gemini/Claude 的多模态能力:
- analyze_image_v2: 深度图像理解(场景/物体/文字/情感/结构)
- transcribe_audio: 语音转文字(已存在,增强版)
- synthesize_speech: 文字转语音
- analyze_video: 视频关键帧提取 + 时序理解
- cross_modal_reason: 跨模态推理(图文混合任务)
- image_edit: 基于文本指令的图像编辑请求
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from typing import Optional

from pydantic import Field

from app.tools.registry import tool


# ── 图像深度理解 ─────────────────────────────────────────────────
@tool(
    "analyze_image_v2",
    "深度分析图片:提取场景内容、物体列表、文字、色彩风格、情感基调、空间布局",
    timeout=60,
)
def analyze_image_v2(
    image_path: str = Field(default="", description="图片文件路径"),
    image_base64: str = Field(default="", description="图片 base64 编码"),
    question: str = Field(default="", description="具体问题(留空=自由描述)"),
    output_format: str = Field(default="structured", description="输出格式: structured/description/ocr_only"),
) -> dict:
    """深度图像分析:调用 LLM 多模态能力,返回结构化分析结果。"""
    from app.core.llm import LLMClient

    # 获取图片数据
    if image_path and not image_base64:
        if not os.path.exists(image_path):
            return {"error": f"文件不存在: {image_path}"}
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
    elif image_base64:
        img_b64 = image_base64
    else:
        return {"error": "请提供 image_path 或 image_base64"}

    # 检测 MIME
    try:
        preview = base64.b64decode(img_b64[:12])
    except Exception:
        preview = b""
    if preview[:3] == b'\xff\xd8\xff':
        mime = "image/jpeg"
    elif preview[:4] == b'\x89PNG':
        mime = "image/png"
    elif preview[:4] == b'RIFF' and img_b64[12:16] == b'WEBP':
        mime = "image/webp"
    elif preview[:3] == b'GIF':
        mime = "image/gif"
    else:
        mime = "image/jpeg"

    data_url = f"data:{mime};base64,{img_b64}"

    # 构造分析提示词
    if output_format == "structured":
        system_prompt = """你是专业的图像分析专家。请对图片进行多维度深度分析:
1. 场景描述:整体场景、时间、地点、天气
2. 物体列表:所有可见物体(名称 + 位置 + 大小)
3. 文字内容:OCR 提取的所有文字
4. 色彩风格:主色调、对比度、饱和度、艺术风格
5. 情感基调:图片传达的情感(积极/消极/中性)
6. 空间布局:前景/中景/背景、透视关系
7. 特殊元素:logo、品牌、地标、人物等

输出 JSON 格式:
{
  "scene": "场景描述",
  "objects": [{"name": "...", "position": "...", "size": "..."}],
  "text": "提取的文字",
  "colors": {"dominant": "...", "style": "..."},
  "emotion": "积极/消极/中性",
  "layout": "空间布局描述",
  "special": ["特殊元素列表"]
}"""
    elif output_format == "ocr_only":
        system_prompt = "请提取图片中所有可见文字，保持原始格式和顺序，只输出文字内容。"
    else:  # description
        system_prompt = "请用自然语言详细描述这张图片的内容、氛围和关键细节。"

    user_question = (
        f"请分析这张图片。\n\n"
        f"输出要求: {output_format}\n\n"
        f"具体问题: {question if question else '无'}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_question},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]

    try:
        llm = LLMClient()
        answer = asyncio.run(llm.chat(messages, temperature=0.3))
    except Exception as e:
        return {"error": f"图像分析失败: {e}"}

    # 尝试解析结构化输出
    if output_format == "structured":
        try:
            parsed = json.loads(answer)
            if isinstance(parsed, dict):
                return {"status": "ok", "format": "structured", **parsed}
        except json.JSONDecodeError:
            pass

    return {"status": "ok", "format": output_format, "analysis": answer}


# ── 语音交互 ──────────────────────────────────────────────────────
@tool(
    "transcribe_audio",
    "语音转文字:将音频文件转录为文本(支持 WAV/MP3/OGG/M4A/FLAC)",
    timeout=120,
)
def transcribe_audio(
    audio_path: str = Field(default="", description="音频文件路径"),
    audio_base64: str = Field(default="", description="音频 base64 编码"),
    language: str = Field(default="", description="语言代码(如 zh/en/ja,留空=自动检测)"),
) -> dict:
    """增强版语音转录:支持本地 Whisper 和 OpenAI 兼容 API。"""
    from app.core.multimodal import transcribe_audio as _transcribe
    from app.config import settings

    # 获取音频数据
    if audio_path and not audio_base64:
        if not os.path.exists(audio_path):
            return {"error": f"文件不存在: {audio_path}"}
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        filename = os.path.basename(audio_path)
    elif audio_base64:
        try:
            audio_bytes = base64.b64decode(audio_base64)
        except Exception as e:
            return {"error": f"Base64 解码失败: {e}"}
        filename = "audio.wav"
    else:
        return {"error": "请提供 audio_path 或 audio_base64"}

    # 优先使用配置的 ASR 服务
    if settings.asr_base_url and settings.asr_api_key:
        text = asyncio.run(_transcribe(audio_bytes, filename))
        if not text.startswith(("无法识别", "语音识别失败")):
            return {"status": "ok", "text": text, "language": language or "auto"}

    # 回退:检测本地 Whisper
    try:
        import whisper
        model = whisper.load_model("base")
        result = model.transcribe(audio_bytes, language=language or None)
        return {"status": "ok", "text": result["text"], "language": result.get("language", "unknown")}
    except ImportError:
        pass
    except Exception as e:
        return {"error": f"语音转录失败: {e}"}

    return {
        "status": "error",
        "error": "未配置 ASR 服务且未安装 Whisper。请在 .env 中设置 ASR_BASE_URL/ASR_API_KEY 或 pip install openai-whisper",
    }


@tool(
    "synthesize_speech",
    "文字转语音:TTS 合成,支持多种音色和语言",
    timeout=30,
)
def synthesize_speech(
    text: str = Field(description="要转换为语音的文本"),
    voice: str = Field(default="", description="音色名称(留空=默认)"),
    speed: float = Field(1.0, ge=0.5, le=2.0, description="语速(0.5x~2.0x)"),
    output_format: str = Field(default="mp3", description="输出格式: mp3/wav"),
) -> dict:
    """文字转语音:调用 TTS 服务。"""
    from app.core.multimodal import synthesize_speech as _synth
    from app.config import settings

    if not settings.tts_base_url and not settings.asr_base_url:
        # 尝试本地 pyttsx3
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty('rate', int(150 * speed))
            temp_dir = tempfile.mkdtemp()
            output_path = os.path.join(temp_dir, f"output.{output_format}")
            engine.save_to_file(text[:2000], output_path)
            engine.runAndWait()
            if os.path.exists(output_path):
                with open(output_path, "rb") as f:
                    audio_bytes = f.read()
                b64 = base64.b64encode(audio_bytes).decode()
                return {"status": "ok", "format": output_format, "audio_base64": b64[:200] + "...", "duration_estimate": len(text) / 15}
            return {"error": "本地 TTS 生成失败"}
        except ImportError:
            pass
        except Exception as e:
            return {"error": f"本地 TTS 失败: {e}"}

    # 使用在线 TTS
    audio_bytes = asyncio.run(_synth(text, voice))
    if audio_bytes:
        b64 = base64.b64encode(audio_bytes).decode()
        return {"status": "ok", "format": output_format, "audio_base64": b64[:200] + "...", "bytes": len(audio_bytes)}

    return {"status": "error", "error": "TTS 服务未配置或调用失败。请设置 TTS_BASE_URL 和 TTS_API_KEY"}


# ── 视频分析 ──────────────────────────────────────────────────────
@tool(
    "analyze_video",
    "分析视频内容:提取关键帧、生成摘要、识别场景和物体",
    timeout=120,
)
def analyze_video(
    video_path: str = Field(description="视频文件路径"),
    max_frames: int = Field(10, ge=1, le=30, description="最多提取的关键帧数"),
    fps: float = Field(1.0, description="帧采样率(每秒帧数)"),
    question: str = Field(default="", description="关于视频的问题(留空=生成摘要)"),
) -> dict:
    """视频分析:提取关键帧 + 调用多模态 LLM 理解。"""
    import os as _os

    if not _os.path.exists(video_path):
        return {"error": f"文件不存在: {video_path}"}

    from app.core.multimodal import extract_video_frames, model_supports_video
    from app.core.llm import LLMClient

    # 提取关键帧
    try:
        frames = asyncio.run(extract_video_frames(
            _open_file(video_path), fps=fps, max_frames=max_frames,
        ))
    except Exception as e:
        return {"error": f"视频帧提取失败(需 ffmpeg): {e}。请安装 ffmpeg 后重试。"}

    if not frames:
        return {"error": "未能提取视频帧,请检查视频格式是否支持"}

    llm = LLMClient()
    model_name = llm.info.get("model", "")
    supports_video = model_supports_video(model_name)

    if not supports_video:
        return {
            "status": "partial",
            "frame_count": len(frames),
            "frames": [{"index": i, "b64_preview": b[:100] + "..."} for i, (b, _) in enumerate(frames)],
            "notice": f"当前模型({model_name})不支持视频理解。已提取 {len(frames)} 个关键帧，建议切换到支持视觉的模型后重试。",
        }

    # 构建多模态消息
    content_parts = []
    for i, (frame_b64, idx) in enumerate(frames):
        content_parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}})
        content_parts.append({"type": "text", "text": f"[帧 {i+1}] 视频第 {idx+1} 秒"})

    question_text = question or "请详细描述这段视频的内容，包括场景、人物、动作、对话等。"
    content_parts.append({"type": "text", "text": question_text})

    messages = [{"role": "user", "content": content_parts}]

    try:
        answer = asyncio.run(llm.chat(messages, temperature=0.3))
    except Exception as e:
        return {"error": f"视频理解失败: {e}"}

    return {
        "status": "ok",
        "frame_count": len(frames),
        "fps": fps,
        "analysis": answer,
    }


def _open_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# ── 跨模态推理 ────────────────────────────────────────────────────
@tool(
    "cross_modal_reason",
    "跨模态推理:综合分析图文/音视频混合输入,进行多维度推理",
    timeout=60,
)
def cross_modal_reason(
    question: str = Field(description="需要回答的问题"),
    image_base64: str = Field(default="", description="关联图片(可选)"),
    audio_transcript: str = Field(default="", description="关联音频转录文本(可选)"),
    context_text: str = Field(default="", description="补充上下文文本(可选)"),
) -> dict:
    """跨模态综合推理:结合多种模态信息回答问题。"""
    from app.core.multimodal import build_cross_modal_prompt, model_supports_vision
    from app.core.llm import LLMClient

    if not image_base64 and not audio_transcript and not context_text:
        # 纯文本问题
        llm = LLMClient()
        answer = asyncio.run(llm.chat([
            {"role": "user", "content": question}
        ], temperature=0.3))
        return {"status": "ok", "modalities": ["text"], "answer": answer}

    # 构建多模态消息
    builder_content = []
    modalities = []

    if image_base64:
        builder_content.append({"type": "image_url", "image_url": {"url": image_base64}})
        modalities.append("image")

    if audio_transcript:
        builder_content.append({
            "type": "text",
            "text": f"[音频转录]\n{audio_transcript}"
        })
        modalities.append("audio")

    if context_text:
        builder_content.append({
            "type": "text",
            "text": f"[上下文]\n{context_text}"
        })
        modalities.append("context")

    builder_content.append({"type": "text", "text": question})

    llm = LLMClient()
    model_name = llm.info.get("model", "")
    supports_vision = model_supports_vision(model_name)

    if "image" in modalities and not supports_vision:
        return {
            "status": "degraded",
            "modalities": modalities,
            "notice": f"当前模型({model_name})不支持图像输入，将仅使用文本模态进行推理",
            "answer": asyncio.run(llm.chat([
                {"role": "user", "content": build_cross_modal_prompt(question, True, "audio" in modalities, False)}
            ], temperature=0.3)),
        }

    messages = [{"role": "user", "content": builder_content}]
    try:
        answer = asyncio.run(llm.chat(messages, temperature=0.3))
    except Exception as e:
        return {"status": "error", "error": str(e)}

    return {
        "status": "ok",
        "modalities": modalities,
        "model": model_name,
        "answer": answer,
    }
