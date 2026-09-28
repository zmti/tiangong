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
    """无任意生成端点时自动禁用图片工具(按 provider 路由后,compat/qwen/自定义均可)。

    设置页的「生成模型」开关**不**在这里摘除工具:那个开关的语义是"不用专门配的
    生成模型,改用当前对话所选的模型",能力本身照常可用。
    """
    from app.api.config_models import _any_gen_base_url
    return _any_gen_base_url()


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


def _download_with_curl(url: str, dest: str) -> bool:
    """用系统 curl 下载文件到 dest。成功返回 True。

    背景:platform-outputs.agnes-ai.space 的 CDN 会按 TLS 指纹掐断 Python
    (requests/httpx)的握手(Windows 10054 连接被强制关闭),curl(schannel)
    不受影响;但 schannel 的证书吊销检查在部分网络下必失败,需 --ssl-no-revoke。
    """
    import subprocess

    curl = None
    if os.name == "nt":
        cand = r"C:\Windows\System32\curl.exe"
        curl = cand if os.path.exists(cand) else None
    if not curl:
        import shutil
        curl = shutil.which("curl")
    if not curl:
        return False
    cmd = [curl, "-sSL", "--max-time", "120", "-o", dest, url]
    if os.name == "nt":
        cmd.insert(1, "--ssl-no-revoke")
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=150)
        return (
            r.returncode == 0
            and os.path.isfile(dest)
            and os.path.getsize(dest) > 0
        )
    except Exception:  # noqa: BLE001
        return False


def _generate_image_data_url(prompt: str, size: str = "512x512") -> tuple[str, bool, dict]:
    """调用 /images/generations,把返回的图片落盘到本地,返回 (URL, 是否本地, 用参元信息)。

    主动请求 response_format=b64_json:实测 agnes-image-2.0-flash 支持该参数,
    直接返回 base64 落盘,由本地 /images 路由提供,完全绕开外网 CDN。
    这点很关键:CDN 域名 platform-outputs.agnes-ai.space 会按 TLS 指纹掐断
    Python(requests/httpx 报 Windows 10054 连接被重置),也常整体不可达;
    一旦走"返回远程 URL"路径,后端下不来 + 前端也加载不了 → 界面完全不显示图片。
    若个别模型不支持 response_format,则去掉该参数重试一次(兼容旧认知)。
    """
    # 模型优先级:桌面端覆盖 > 跟随对话模型(支持多模态时)> .env/IMAGE_MODEL > 代码默认值
    # (image_model 为空时才回退到 openai_compat_model,保持原有兜底逻辑)
    from app.api.config_models import gen_endpoint_for_model, resolve_gen_model
    model, _model_source = resolve_gen_model(
        "image", settings.image_model or settings.openai_compat_model
    )
    # 按 provider 路由:选了某 provider 下的图片模型,请求发到该 provider 的端点
    ep = gen_endpoint_for_model(model, "image")
    base_url = ep["base_url"]
    api_key = ep["api_key"] or "sk-placeholder"
    # 跟着结果回给调用方:排查"到底用了哪个模型"时不必再猜优先级算得对不对
    _info = {
        "model": model,
        "model_source": _model_source,
        "provider": ep.get("provider", ""),
        "provider_name": ep.get("provider_name", ""),
    }
    if not base_url:
        raise RuntimeError(f"图片生成失败: 模型 {model} 所属端点未配置 base_url")
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": 1,
        "response_format": "b64_json",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    api_url = base_url.rstrip("/") + "/images/generations"

    # 调用 API 生成图片(图片生成通常较慢,超时放宽到 300s,避免被中途掐断)
    resp = requests.post(api_url, json=payload, headers=headers, timeout=300, verify=False)
    if resp.status_code >= 400:
        # 少数模型不支持 response_format,去掉该参数重试一次
        payload.pop("response_format", None)
        resp = requests.post(api_url, json=payload, headers=headers, timeout=300, verify=False)
    if resp.status_code >= 400:
        raise RuntimeError(f"图片生成失败: {resp.status_code} {resp.text[:200]}")

    item = resp.json().get("data", [{}])[0]

    # 优先使用 b64_json(无需外网 CDN,最稳)
    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
        name = _save_image(raw, "png")
        return _image_url(name), True, _info

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
                return _image_url(name), True, _info
            except Exception:
                if attempt < 2:
                    continue
                # Python TLS 指纹被 CDN 掐断(requests/httpx 均报 10054)时,
                # 回退系统 curl 下载并落盘本地——保证图片走 /images 本地展示路径
                tmp = os.path.join(_images_dir(), f"_dl_{uuid.uuid4().hex}.{ext or 'png'}")
                if _download_with_curl(src, tmp):
                    try:
                        with open(tmp, "rb") as f:
                            raw = f.read()
                        name = _save_image(raw, ext)
                        return _image_url(name), True, _info
                    finally:
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                # 彻底失败才返回原始 CDN URL 供前端直接展示(标记为远端,便于排查)
                return src, False, _info

    raise RuntimeError("图片生成返回为空")


@tool(
    "generate_image",
    "根据文字描述生成图片。支持真人照片、插画、示意图、图标、艺术图等任何类型。"
    "可指定尺寸:横版16:9(1792x1024)、竖版9:16(1024x1792)、正方形(1024x1024)等。",
    # 预算覆盖「两次 300s 生成请求 + 下载重试」的最坏路径(内部 requests 各自带超时,
    # 不会无限挂住);此前 360 会被 agent 外层 300s 批处理上限压死,导致已出图却
    # 被判超时。现外层不再设限(agent_tool_timeout=0),由本预算兜底。
    timeout=660,
    # 只挂动态开关,**不要**再传 enabled=_image_enabled():
    # 静态 enabled 在 import 那一刻求值并被永久固化(_enabled),会把"启动时用户
    # 恰好关着"这个瞬时状态变成永久否决 —— 用户后来在设置页重新打开,设置页显示
    # "已启用",工具却依然下线,只能重启后端才恢复(2026-09-24 实测复现)。
    enabled_fn=_image_enabled,
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
    data, local, info = _generate_image_data_url(prompt, size)
    return {
        "format": "local_url" if local else "remote_url",
        "size": size,
        "image_url": data,
        # model_source: override(设置页指定) / session(跟随对话模型) / env / default
        "model": info.get("model", ""),
        "model_source": info.get("model_source", ""),
        "provider": info.get("provider", ""),
        "provider_name": info.get("provider_name", ""),
    }
