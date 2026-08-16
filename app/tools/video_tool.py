"""视频工具:视频生成(基于 agnes /v1/videos 异步任务)。

创建视频任务后轮询 agnesapi 直到完成,返回视频地址。
"""
from __future__ import annotations

import asyncio
import time

import httpx
from pydantic import Field

from app.config import settings
from app.tools.registry import tool


def _video_enabled() -> bool:
    """无 base_url/key 时自动禁用视频工具。"""
    return bool(settings.openai_compat_base_url)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.openai_compat_api_key or 'sk-placeholder'}",
        "Content-Type": "application/json",
    }


async def _create_video(prompt: str, width: int, height: int, num_frames: int, frame_rate: int) -> str:
    import json

    base = settings.openai_compat_base_url.rstrip("/")
    payload = {
        "model": "agnes-video-v2.0",
        "prompt": prompt,
        "width": width,
        "height": height,
        "num_frames": num_frames,
        "frame_rate": frame_rate,
    }
    # base 可能已含 /v1(如 https://apihub.agnes-ai.cn/v1),避免拼出 /v1/v1/videos
    if base.endswith("/v1"):
        api = base[: -len("/v1")] + "/v1/videos"
    else:
        api = base + "/videos"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(api, json=payload, headers=_headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"视频任务创建失败: {resp.status_code} {resp.text[:200]}")
        return resp.json().get("video_id") or resp.json().get("id") or resp.json().get("task_id")


async def _poll_video(task_id: str, timeout: float = 600) -> dict:
    """轮询视频任务,直到 completed 或 failed,返回最终结果。"""
    import json

    base = settings.openai_compat_base_url.rstrip("/")
    if base.endswith("/v1"):
        host = base[: -len("/v1")] or base
    else:
        host = base
    start = time.time()
    last_progress = 0
    async with httpx.AsyncClient(timeout=30) as client:
        while time.time() - start < timeout:
            resp = await client.get(f"{host}/agnesapi?video_id={task_id}", headers=_headers())
            if resp.status_code >= 400:
                raise RuntimeError(f"视频查询失败: {resp.status_code} {resp.text[:200]}")
            data = resp.json()
            status = data.get("status") or data.get("internal_status")
            progress = data.get("internal_progress") or 0
            if progress != last_progress:
                last_progress = progress
            if status == "completed" or data.get("completed_at"):
                return data
            if status == "failed" or data.get("error"):
                raise RuntimeError(f"视频生成失败: {data.get('error') or status}")
            await asyncio.sleep(5)
    raise RuntimeError(f"视频生成超时({int(timeout)}s),请稍后重试")


@tool(
    "generate_video",
    "根据文字描述生成一段短视频(约3~18秒),适合动效演示、场景短片等。注意:视频生成需要几分钟,请耐心等待结果",
    timeout=700,
    enabled=_video_enabled(),
    terminal=True,
)
def generate_video(
    prompt: str = Field(description="视频内容描述,越具体越好(主体/动作/场景/镜头/光线/风格)"),
    duration: int = Field(3, description="视频时长(秒): 3 / 5 / 10 / 18"),
) -> str:
    duration = max(3, min(18, duration))
    frames = {3: 81, 5: 121, 10: 241, 18: 441}
    num_frames = frames[duration]
    task_id = asyncio.run(_create_video(prompt, 1152, 768, num_frames, 24))
    data = asyncio.run(_poll_video(task_id))
    import json as _json

    video_url = (
        data.get("video_url")
        or data.get("url")
        or data.get("remixed_from_video_id")
        or data.get("file_url")
        or ""
    )
    return {
        "format": "url",
        "duration": duration,
        "task_id": task_id,
        "video_url": video_url,
        "message": "视频已生成",
    }