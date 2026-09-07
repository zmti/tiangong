"""图片工具:图片生成(基于 OpenAI 兼容 /images/generations)。

生成的图片直接保存到 data/images/ 目录,返回本地静态 URL。
为避免 PyInstaller 打包的 StaticFiles 问题,同时注册到 attachments API 供前端访问。
"""
from __future__ import annotations

import base64
import os
import uuid

import requests
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
    """保存图片字节到本地,返回文件名。"""
    if ext not in ("png", "jpg", "jpeg", "webp", "gif"):
        ext = "png"
    name = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(_images_dir(), name), "wb") as f:
        f.write(data)
    return name


def _image_url(name: str) -> str:
    """返回图片的相对路径 URL。

    不拼接绝对地址,避免 agent_host/agent_port 配置与实际运行端口不一致时
    (如 Electron 打包后固定 8517 端口)前端加载图片 404。
    前端通过 App.backendUrl 拼接完整地址。
    """
    return f"/images/{name}"


def _generate_image_data_url(prompt: str, size: str = "512x512") -> str:
    """调用 /images/generations,把返回的图片落盘到本地,返回本地静态 URL。

    注意:agnes-image 模型不支持 response_format 参数。
    优先使用 b64_json(更快),回退到 URL 下载。
    """
    model = settings.image_model or settings.openai_compat_model
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": 1,
    }
    headers = {
        "Authorization": f"Bearer {settings.openai_compat_api_key or 'sk-placeholder'}",
        "Content-Type": "application/json",
    }
    api_url = settings.openai_compat_base_url.rstrip("/") + "/images/generations"

    # 调用 API 生成图片(图片生成通常较慢,超时放宽到 300s,避免被中途掐断)
    resp = requests.post(api_url, json=payload, headers=headers, timeout=300, verify=False)
    if resp.status_code >= 400:
        raise RuntimeError(f"图片生成失败: {resp.status_code} {resp.text[:200]}")

    item = resp.json().get("data", [{}])[0]

    # 优先使用 b64_json(更快,无需额外下载)
    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
        name = _save_image(raw, "png")
        return _image_url(name)

    # 回退到 URL 下载
    if item.get("url"):
        src = item["url"]
        ext = src.split("?", 1)[0].rsplit(".", 1)[-1].lower()
        # 重试机制
        for attempt in range(3):
            try:
                dl = requests.get(src, timeout=180, verify=False)
                dl.raise_for_status()
                name = _save_image(dl.content, ext)
                return _image_url(name)
            except Exception as e:
                if attempt < 2:
                    continue
                # CDN 不可达时,直接返回原始 CDN URL 供前端展示
                return src

    raise RuntimeError("图片生成返回为空")


@tool(
    "generate_image",
    "根据文字描述生成图片。支持真人照片、插画、示意图、图标、艺术图等任何类型。"
    "可指定尺寸:横版16:9(1792x1024)、竖版9:16(1024x1792)、正方形(1024x1024)等。",
    timeout=360,
    enabled=_image_enabled(),
    terminal=True,
)
def generate_image(
    prompt: str = Field(description="图片内容描述,越具体越好(语言:中文或英文均可)"),
    size: str = Field(
        "1024x1024",
        description=(
            "图片尺寸(宽高像素)。可选值:256x256 / 512x512 / 1024x1024(正方形默认) / "
            "1024x1792(竖版9:16,适合手机壁纸) / 1792x1024(横版16:9,适合宽屏)。"
        ),
    ),
) -> str:
    data = _generate_image_data_url(prompt, size)
    return {"format": "local_url", "size": size, "image_url": data}
