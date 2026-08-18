"""image_ocr 工具:图片文字提取(OCR)。

支持两种模式:
1. Tesseract 本地 OCR(需安装 tesseract)
2. LLM 多模态 OCR(调用主模型的视觉能力,需模型支持)

无 Tesseract 且无视觉模型时自动降级为 analyze_image 的文字提取问题。
"""
from __future__ import annotations

import base64
import os
import subprocess
from typing import Optional

from pydantic import Field

from app.tools.registry import tool


def _has_tesseract() -> bool:
    """检测 Tesseract OCR 是否可用。"""
    try:
        result = subprocess.run(["tesseract", "--version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _has_vision_llm() -> bool:
    """检测当前 LLM 是否支持多模态(有 vision 能力的模型)。"""
    from app.config import settings
    model = settings.openai_compat_model or settings.deepseek_model or ""
    vision_keywords = ("vision", "flash", "pro", "omni", "gpt-4o", "claude", "gemini")
    return any(kw in model.lower() for kw in vision_keywords) or bool(settings.asr_base_url)


@tool("image_ocr", "从图片中提取文字(OCR),支持 Tesseract 本地引擎或 LLM 多模态回退", timeout=30)
def image_ocr(
    image_path: str = Field("", description="图片文件路径"),
    image_base64: str = Field("", description="图片 base64 编码(与 image_path 二选一)"),
    language: str = Field("chi_sim+eng", description="OCR 语言,默认中英混合(chi_sim+eng);Tesseract 支持 lang 列表: https://github.com/tesseract-ocr/tessdata"),
    engine: str = Field("auto", description="OCR 引擎: auto(优先 Tesseract,无则用 LLM)/ tesseract / llm"),
) -> dict:
    """OCR 文字提取。"""
    if not image_path and not image_base64:
        return {"error": "请提供 image_path 或 image_base64"}

    # 获取 base64
    if image_path:
        if not os.path.exists(image_path):
            return {"error": f"文件不存在: {image_path}"}
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
    else:
        img_b64 = image_base64

    # 检测图片类型
    img_bytes = base64.b64decode(img_b64[:100])
    if img_bytes[:3] == b'\xff\xd8\xff':
        mime = "image/jpeg"
    elif img_bytes[:4] == b'\x89PNG':
        mime = "image/png"
    elif img_bytes[:4] == b'RIFF':
        mime = "image/webp"
    else:
        mime = "image/jpeg"

    # 决定引擎
    use_engine = engine.lower()
    if use_engine == "auto":
        if _has_tesseract():
            use_engine = "tesseract"
        elif _has_vision_llm():
            use_engine = "llm"
        else:
            return {"error": "未检测到 Tesseract 或支持视觉的 LLM,无法执行 OCR"}
    elif use_engine == "tesseract" and not _has_tesseract():
        return {"error": "Tesseract 未安装,请指定 engine=llm 或安装 tesseract"}
    elif use_engine == "llm" and not _has_vision_llm():
        return {"error": "当前 LLM 不支持多模态,请指定 engine=tesseract 或安装 Tesseract"}

    if use_engine == "tesseract":
        return _tesseract_ocr(img_b64, mime, language)
    return _llm_ocr(img_b64, mime)


def _tesseract_ocr(img_b64: str, mime: str, language: str) -> dict:
    """使用 Tesseract 进行 OCR。"""
    try:
        import tempfile

        ext = "png" if "png" in mime else "jpg"
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
            tmp.write(base64.b64decode(img_b64))
            tmp_path = tmp.name
        out_path = tmp_path + ".txt"
        result = subprocess.run(
            ["tesseract", tmp_path, out_path[:-4], "-l", language],
            capture_output=True, text=True, timeout=60,
        )
        os.unlink(tmp_path)
        try:
            os.unlink(out_path)
        except OSError:
            pass
        if result.returncode != 0:
            return {"error": f"Tesseract 执行失败: {result.stderr[:300]}"}
        with open(out_path.replace(".txt", "") + ".txt" if not os.path.exists(out_path) else out_path, "r", encoding="utf-8") as f:
            text = f.read()
        # 重新读取(上一步可能路径有误)
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            text = ""
        return {
            "engine": "tesseract",
            "language": language,
            "text": text.strip(),
            "length": len(text),
        }
    except FileNotFoundError:
        return {"error": "Tesseract 命令未找到,请安装 tesseract-ocr"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Tesseract OCR 失败: {e}"}


def _llm_ocr(img_b64: str, mime: str) -> dict:
    """使用 LLM 多模态能力进行 OCR。"""
    try:
        from app.core.llm import LLMClient
        import asyncio

        llm = LLMClient()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请提取图片中的所有文字,保持原始格式和排版,只返回文字内容,不要添加任何解释。"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{img_b64}"},
                    },
                ],
            }
        ]
        resp = asyncio.run(llm.chat(messages, temperature=0.1))
        return {"engine": "llm", "text": resp, "length": len(resp)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"LLM OCR 失败: {e}"}


@tool("screenshot_ocr", "对截图文件进行 OCR(快捷工具,无需指定语言)", timeout=30)
def screenshot_ocr(
    screenshot_path: str = Field(description="截图文件路径(.png/.jpg/.webp)"),
) -> dict:
    """从系统截图或应用截图路径中提取文字。"""
    if not screenshot_path:
        return {"error": "请提供截图路径"}
    # 直接调用底层实现,不经过 Tool 包装器
    from app.tools.image_ocr import _tesseract_ocr, _llm_ocr, _has_tesseract, _has_vision_llm

    if _has_tesseract():
        try:
            import base64
            with open(screenshot_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()
            mime = "image/png" if screenshot_path.endswith(".png") else "image/jpeg"
            return _tesseract_ocr(img_b64, mime, "chi_sim+eng")
        except Exception as e:  # noqa: BLE001
            return {"error": f"OCR 失败: {e}"}
    elif _has_vision_llm():
        try:
            import base64
            with open(screenshot_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()
            return _llm_ocr(img_b64, "image/png")
        except Exception as e:  # noqa: BLE001
            return {"error": f"OCR 失败: {e}"}
    else:
        return {"error": "未检测到 Tesseract 或支持视觉的 LLM"}
