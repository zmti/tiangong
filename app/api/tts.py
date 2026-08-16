"""语音合成接口:文本转语音(edge-tts,需联网)。"""
from __future__ import annotations

import io

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/v1", tags=["tts"])

VOICES = {
    "晓晓": "zh-CN-XiaoxiaoNeural",
    "云希": "zh-CN-YunxiNeural",
    "云健": "zh-CN-YunjianNeural",
    "晓伊": "zh-CN-XiaoyiNeural",
    "小宁": "zh-CN-XiaoxiaoNeural",
    "男声-云希": "zh-CN-YunxiNeural",
}


class TTSRequest(BaseModel):
    text: str = ""
    voice: str = "zh-CN-XiaoxiaoNeural"


@router.post("/tts")
async def tts(req: TTSRequest) -> StreamingResponse:
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="文本为空")
    if len(text) > 3000:
        raise HTTPException(status_code=413, detail="文本过长,最多 3000 字符")

    voice = req.voice
    if voice in VOICES:
        voice = VOICES[voice]

    import edge_tts

    try:
        communicate = edge_tts.Communicate(text, voice, rate="+0%")
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        data = buf.getvalue()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"语音合成失败(可能需要联网): {e}") from e
    if not data:
        raise HTTPException(status_code=502, detail="语音合成结果为空")

    return StreamingResponse(
        io.BytesIO(data),
        media_type="audio/mpeg",
        headers={"Content-Disposition": 'inline; filename="tts.mp3"'},
    )
