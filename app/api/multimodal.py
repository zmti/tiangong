"""多模态 API 端点:ASR、视频理解、TTS。"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.security import require_api_key
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/multimodal", tags=["multimodal"])


class TranscribeRequest(BaseModel):
    """转录请求。"""
    language: Optional[str] = None
    model: Optional[str] = None


class TranscribeResponse(BaseModel):
    """转录响应。"""
    text: str
    duration: float
    language: str


class VideoSummaryRequest(BaseModel):
    """视频摘要请求。"""
    max_frames: int = 10
    fps: float = 1.0


class VideoSummaryResponse(BaseModel):
    """视频摘要响应。"""
    frames: int
    summary: str
    captions: list[str]


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_audio(
    file: UploadFile = File(...),
    req: TranscribeRequest = TranscribeRequest(),
    _=Depends(require_api_key),
):
    """语音转文字。"""
    if not settings.asr_base_url or not settings.asr_api_key:
        raise HTTPException(status_code=503, detail="ASR 服务未配置")

    try:
        from app.core.multimodal import transcribe_audio
        import asyncio

        content = await file.read()
        text = await asyncio.wait_for(
            transcribe_audio(content, file.filename or "audio.wav"),
            timeout=120
        )

        return TranscribeResponse(text=text, duration=0, language=req.language or "auto")
    except Exception as e:
        logger.error("Transcribe error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/video/summarize", response_model=VideoSummaryResponse)
async def summarize_video(
    file: UploadFile = File(...),
    req: VideoSummaryRequest = VideoSummaryRequest(),
    _=Depends(require_api_key),
):
    """视频理解:提取关键帧并生成摘要。"""
    try:
        from app.core.multimodal import extract_video_frames
        import asyncio

        content = await file.read()
        frames = await asyncio.wait_for(
            extract_video_frames(content, fps=req.fps, max_frames=req.max_frames),
            timeout=60
        )

        summary = f"视频共 {len(frames)} 帧"
        return VideoSummaryResponse(
            frames=len(frames),
            summary=summary,
            captions=[f"帧 {i}" for i in range(len(frames))]
        )
    except Exception as e:
        logger.error("Video summarize error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/synthesize")
async def synthesize_speech(
    text: str,
    voice: str = "",
    _=Depends(require_api_key),
):
    """文字转语音。"""
    if not settings.tts_base_url or not settings.tts_api_key:
        raise HTTPException(status_code=503, detail="TTS 服务未配置")

    try:
        from app.core.multimodal import synthesize_speech
        import asyncio

        audio = await asyncio.wait_for(
            synthesize_speech(text, voice),
            timeout=30
        )

        if not audio:
            raise HTTPException(status_code=500, detail="TTS 生成失败")

        return StreamingResponse(
            iter([audio]),
            media_type="audio/mpeg",
            headers={"Content-Disposition": "attachment; filename=speech.mp3"}
        )
    except Exception as e:
        logger.error("Synthesize error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def get_multimodal_status(_=Depends(require_api_key)):
    """获取多模态服务状态。"""
    return {
        "asr": {
            "enabled": bool(settings.asr_base_url),
            "model": settings.asr_model or "whisper-1",
        },
        "tts": {
            "enabled": bool(settings.tts_base_url),
            "voice": settings.tts_voice or "alloy",
        },
    }
