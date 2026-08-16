"""图片工具:图片生成(基于 OpenAI 兼容 /images/generations)。

生成的图片落盘到本地数据目录(data/images),返回本地静态 URL。
平台临时 URL 与巨型 data URL 都会导致历史图片无法长期显示,本地落盘可永久保留。
"""
from __future__ import annotations

import asyncio
import base64
import os
import uuid

import httpx
from pydantic import Field

from app.config import settings
from app.tools.registry import tool


def _image_enabled() -> bool:
    """无 base_url/key 时自动禁用图片工具。"""
    return bool(settings.openai_compat_base_url)


def _images_dir() -> str:
    path = os.path.join(settings.resolved_data_dir, "images")
    os.makedirs(path, exist_ok=True)
    return path


def _save_image(data: bytes, ext: str) -> str:
    """保存图片字节到本地,返回相对文件名。"""
    if ext not in ("png", "jpg", "jpeg", "webp", "gif"):
        ext = "png"
    name = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(_images_dir(), name), "wb") as f:
        f.write(data)
    return name


def _image_url(name: str) -> str:
    """拼本地静态 URL,供前端实时展示与历史回放直接访问。"""
    host = settings.agent_host
    if not host or host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{settings.agent_port}/images/{name}"


async def _generate_image_data_url(prompt: str, size: str = "512x512") -> str:
    """调用 /images/generations,把返回的图片落盘到本地,返回本地 URL。

    平台要求 `extra_body.response_format` 作为嵌套键传输(openai SDK 的 extra_body
    会合并到顶层,因此这里直接用 httpx 发原始 JSON)。
    """
    model = settings.image_model or settings.openai_compat_model
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": 1,
        "extra_body": {"response_format": "b64_json"},
    }
    headers = {
        "Authorization": f"Bearer {settings.openai_compat_api_key or 'sk-placeholder'}",
        "Content-Type": "application/json",
    }
    url = settings.openai_compat_base_url.rstrip("/") + "/images/generations"
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            if "response_format" in resp.text:
                # 模型不支持 b64:回退到 URL 输出
                payload.pop("extra_body", None)
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                raise RuntimeError(f"图片生成失败: {resp.status_code} {resp.text[:200]}")
        item = resp.json().get("data", [{}])[0]
        if item.get("b64_json"):
            raw = base64.b64decode(item["b64_json"])
            return _image_url(_save_image(raw, "png"))
        if item.get("url"):
            src = item["url"]
            ext = src.split("?", 1)[0].rsplit(".", 1)[-1].lower()
            dl = await client.get(src)
            dl.raise_for_status()
            return _image_url(_save_image(dl.content, ext))
    raise RuntimeError("图片生成返回为空")


@tool(
    "generate_image",
    "根据文字描述生成一张图片,适合插画、示意图、图标等。返回图片,适合在聊天中展示",
    timeout=180,
    enabled=_image_enabled(),
    terminal=True,
)
def generate_image(
    prompt: str = Field(description="图片内容描述,越具体越好"),
    size: str = Field("512x512", description="图片尺寸:256x256 / 512x512 / 1024x1024"),
) -> str:
    data = asyncio.run(_generate_image_data_url(prompt, size))
    return {"format": "local_url", "size": size, "image_url": data}