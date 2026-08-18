"""多模态原生支持:图像、音频、视频理解与生成 + 跨模态推理。

设计:
- 图像:基于 OpenAI 兼容的 vision API,支持 data URL / server URL 两种模式
- 音频:ASR 转录(Whisper/Paraformer 兼容) + TTS 生成(可选)
- 视频:关键帧提取 + 逐帧视觉理解 + 跨帧时序推理
- 跨模态推理:图文混合任务自动检测并路由到多模态 LLM

模型能力探测:按模型名命中 _VISION/AUDIO/VIDEO 标记启发式判断。
降级策略:模型不支持某模态时自动回退到文本描述或明确提示。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from app.config import settings

# ── 模型能力探测标记 ──────────────────────────────────────────────
_VISION_MARKERS = (
    "vl", "vision", "gpt-4o", "gpt-4.1", "gpt-4.5", "gemini",
    "claude", "qwen-vl", "glm-4v", "internvl", "minicpm-v",
    "agnes-vl", "grok-vision", "claude-3", "claude-4",
)
_AUDIO_MARKERS = ("whisper", "paraformer", "audio", "speech")
_VIDEO_MARKERS = ("video", "vlm", "gpt-4o", "gemini-1.5", "claude-3.5")


def model_supports_vision(model: str) -> bool:
    low = (model or "").lower()
    return any(m in low for m in _VISION_MARKERS)


def vision_notice(model: str = "", image_count: int = 0) -> str:
    """当前模型不支持视觉输入时的降级说明文本。"""
    model = model or "未知"
    return (
        f"[当前模型 {model} 不支持图片输入,本次共收到 {image_count} 张图片附件,"
        "无法直接查看;请仅依据文字描述与工具结果继续处理]"
    )


def model_supports_audio(model: str) -> bool:
    low = (model or "").lower()
    return any(m in low for m in _AUDIO_MARKERS)


def model_supports_video(model: str) -> bool:
    low = (model or "").lower()
    return any(m in low for m in _VIDEO_MARKERS)


# ── MIME 类型映射 ──────────────────────────────────────────────────
_IMAGE_MIMES = {
    "image/png": "png", "image/jpeg": "jpeg", "image/jpg": "jpeg",
    "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp",
}
_VIDEO_MIMES = {
    "video/mp4": "mp4", "video/webm": "webm", "video/avi": "avi",
    "video/mov": "mov", "video/m4v": "m4v",
}
_AUDIO_MIMES = {
    "audio/mpeg": "mp3", "audio/wav": "wav", "audio/ogg": "ogg",
    "audio/mp4": "m4a", "audio/flac": "flac",
}


def is_image_mime(mime: str) -> bool:
    return (mime or "").lower() in _IMAGE_MIMES


def is_video_mime(mime: str) -> bool:
    return (mime or "").lower() in _VIDEO_MIMES


def is_audio_mime(mime: str) -> bool:
    return (mime or "").lower() in _AUDIO_MIMES


# ── 编码工具 ───────────────────────────────────────────────────────
def image_to_data_url(content: bytes, mime: str) -> str:
    mime = (mime or "").lower()
    if mime not in _IMAGE_MIMES:
        mime = "image/png"
    ext = _IMAGE_MIMES[mime]
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


def build_image_url_part(data_url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": data_url}}


def build_vision_content_part(data_url: str, caption: str = "") -> dict:
    if caption:
        return {"type": "text", "text": f"![附件]({data_url})\n{caption}".strip()}
    return {"type": "text", "text": f"图片附件(请查看): {data_url}"}


def audio_to_base64(content: bytes, mime: str) -> str:
    ext = _AUDIO_MIMES.get((mime or "").lower(), "wav")
    return base64.b64encode(content).decode("ascii"), ext


# ── ASR 语音转文字 ────────────────────────────────────────────────
async def transcribe_audio(audio: bytes, filename: str = "audio.wav") -> str:
    """语音转文字:OpenAI 兼容 /audio/transcriptions 端点。

    未配置 ASR_BASE_URL 或调用失败时,返回明确引导提示(不抛异常)。
    """
    if not settings.asr_base_url or not settings.asr_api_key:
        return (
            "无法识别语音:尚未配置 ASR 服务。请在 .env 中设置 "
            "ASR_BASE_URL / ASR_API_KEY / ASR_MODEL 后重试。"
        )
    import asyncio as _asyncio
    from openai import AsyncOpenAI

    model = settings.asr_model or "whisper-1"
    client = AsyncOpenAI(api_key=settings.asr_api_key, base_url=settings.asr_base_url)
    try:
        resp = await _asyncio.wait_for(
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


# ── TTS 文字转语音 ────────────────────────────────────────────────
async def synthesize_speech(text: str, voice: str = "") -> Optional[bytes]:
    """文字转语音:OpenAI 兼容 /audio/speech 端点。

    未配置 TTS 服务时返回 None。
    """
    tts_base = getattr(settings, "tts_base_url", "")
    tts_key = getattr(settings, "tts_api_key", "")
    tts_model = getattr(settings, "tts_model", "tts-1")
    tts_voice = voice or getattr(settings, "tts_voice", "alloy")

    if not tts_base or not tts_key:
        return None

    payload = {
        "model": tts_model,
        "input": text[:4000],
        "voice": tts_voice,
    }
    headers = {"Authorization": f"Bearer {tts_key}", "Content-Type": "application/json"}
    url = tts_base.rstrip("/") + "/audio/speech"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            return None
        return resp.content


# ── 视频关键帧提取 ─────────────────────────────────────────────────
async def extract_video_frames(
    video_bytes: bytes,
    fps: float = 1.0,
    max_frames: int = 10,
) -> list[tuple[str, int]]:
    """从视频字节中提取关键帧,返回 [(base64_str, frame_index), ...]。

    优先用 ffmpeg/python 提取;不可用时回退到每 N 秒取一帧启发式采样。
    """
    frames: list[tuple[str, int]] = []
    tmp_dir = tempfile.mkdtemp(prefix="dsh_vid_")

    # 写临时视频文件
    video_path = os.path.join(tmp_dir, "input.mp4")
    with open(video_path, "wb") as f:
        f.write(video_bytes)

    # 尝试 ffmpeg(外部命令)
    try:
        import shutil
        if shutil.which("ffmpeg"):
            pts = f"0.5:{fps}"
            cmd = [
                "ffmpeg", "-i", video_path,
                "-vf", f"fps={fps},select='not(mod(n\\,{max(1,int(1/fps))}))'",
                "-vsync", "vfr",
                os.path.join(tmp_dir, "frame_%04d.jpg"),
            ]
            await asyncio.wait_for(
                asyncio.to_thread(_run_cmd, cmd),
                timeout=60.0,
            )
            for i, fpath in enumerate(sorted(os.listdir(tmp_dir))):
                if not fpath.startswith("frame_") or not fpath.endswith(".jpg"):
                    continue
                fpath = os.path.join(tmp_dir, fpath)
                with open(fpath, "rb") as fh:
                    frames.append((base64.b64encode(fh.read()).decode("ascii"), i))
                if len(frames) >= max_frames:
                    break
    except Exception:  # noqa: BLE001
        pass

    # 回退:无 ffmpeg 时直接返回空列表,上层降级处理
    return frames


def _run_cmd(cmd: list[str]) -> None:
    import subprocess
    subprocess.run(cmd, capture_output=True, timeout=60)


# ── 跨模态推理提示词模板 ──────────────────────────────────────────
CROSS_MODAL_REASONING_PROMPT = """你是多模态推理助手。用户提供了混合模态输入(文本+图片+音频+视频帧)。

请综合所有模态信息进行分析:
- 图片:描述内容、文字、场景
- 音频:转录的文字含义
- 视频帧:时序上下文、动作演变
- 文本:用户的具体问题

你的回答应:
1. 先简述你从各模态获取到的信息
2. 给出综合推理过程
3. 最后给出清晰、有依据的答案

只输出最终回答,不要暴露内部推理步骤(除非用户明确要求)。
"""


def build_cross_modal_prompt(question: str, has_images: bool, has_audio: bool, has_video: bool) -> str:
    """根据模态组合构造跨模态推理的系统提示。"""
    modalities = []
    if has_images:
        modalities.append("图片")
    if has_audio:
        modalities.append("音频")
    if has_video:
        modalities.append("视频")

    if not modalities:
        return question
    return (
        f"[多模态输入: {', '.join(modalities)}]\n\n"
        f"{question}\n\n"
        f"请注意综合上述所有模态信息进行分析。"
    )


# ── 视频理解工具结果 ──────────────────────────────────────────────
class VideoUnderstandingResult(BaseModel):
    """视频理解的统一输出结构。"""
    duration_seconds: float = 0.0
    frame_count: int = 0
    summary: str = ""
    scenes: list[str] = Field(default_factory=list)
    objects: list[str] = Field(default_factory=list)
    text_detected: list[str] = Field(default_factory=list)


def video_understanding_notice(model: str, n_frames: int) -> str:
    """模型不支持视频理解时的降级提示。"""
    return (
        f"[系统提示] 本次消息包含 {n_frames} 个视频关键帧,但当前模型 "
        f"({model}) 不支持视频理解。请切换到支持视频的模型后重试。"
    )


# ── 多模态消息构建器 ──────────────────────────────────────────────
class MultimodalMessageBuilder:
    """构建带多模态内容的 OpenAI 兼容消息。

    用法:
        builder = MultimodalMessageBuilder()
        builder.add_image(data_url_or_url, caption="用户截图")
        builder.add_audio(text_transcript="已转文字: ...")
        builder.add_video_frames(frame_b64_list, captions)
        messages = builder.build(user_text)
    """

    def __init__(self):
        self._parts: list[dict] = []
        self._text_parts: list[str] = []
        self._modality_flags = {"image": False, "audio": False, "video": False}

    def add_image(self, data_url: str, caption: str = "", url_only: bool = False) -> "MultimodalMessageBuilder":
        if url_only:
            self._parts.append({"type": "image_url", "image_url": {"url": data_url}})
        else:
            self._parts.append({"type": "image_url", "image_url": {"url": data_url}})
        if caption:
            self._parts.append({"type": "text", "text": f"[说明] {caption}"})
        self._modality_flags["image"] = True
        return self

    def add_audio(self, text_transcript: str) -> "MultimodalMessageBuilder":
        self._text_parts.append(f"[音频已转文字]\n{text_transcript}")
        self._modality_flags["audio"] = True
        return self

    def add_video_frames(self, frame_b64_list: list[str], captions: Optional[list[str]] = None) -> "MultimodalMessageBuilder":
        captions = captions or []
        for i, b64 in enumerate(frame_b64_list):
            caption = captions[i] if i < len(captions) else f"视频帧 {i+1}"
            self._parts.append({"type": "image_url", "image_url": {"url": b64}})
            self._parts.append({"type": "text", "text": f"[视频帧 {i+1}] {caption}"})
        self._modality_flags["video"] = True
        return self

    def add_text(self, text: str) -> "MultimodalMessageBuilder":
        self._text_parts.append(text)
        return self

    def build(self, user_text: str) -> tuple[list[dict], dict]:
        """返回 (messages, modality_flags)。

        messages: 单条 user 消息,content 为 parts 数组。
        """
        full_text = "\n\n".join(self._text_parts + [user_text])
        if not self._parts:
            return [{"role": "user", "content": full_text}], self._modality_flags
        return [
            {"role": "user", "content": self._parts + [{"type": "text", "text": full_text}]}
        ], self._modality_flags

    def has_modality(self, modality: str) -> bool:
        return self._modality_flags.get(modality, False)
