"""多模态理解工具:图片识别、OCR 文字提取、图片分析。"""
from __future__ import annotations

import base64
import json
import os
import tempfile
from typing import Optional

from app.tools.registry import tool


@tool("analyze_image", "分析图片内容,返回图片的描述", timeout=30)
def analyze_image(
    image_path: str = "",
    image_base64: str = "",
    question: str = "请描述这张图片的内容",
) -> dict:
    """
    分析图片内容(使用 LLM 多模态能力)。
    
    Args:
        image_path: 图片文件路径(与 image_base64 二选一)
        image_base64: 图片 base64 编码(与 image_path 二选一)
        question: 关于图片的问题
    
    Returns:
        分析结果
    """
    # 获取图片数据
    if image_path:
        if not os.path.exists(image_path):
            return {"error": f"文件不存在: {image_path}"}
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
    elif image_base64:
        img_b64 = image_base64
    else:
        return {"error": "请提供 image_path 或 image_base64"}
    
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
    
    # 调用 LLM 多模态接口
    try:
        from app.core.llm import LLMClient
        from app.config import settings
        
        llm = LLMClient()
        
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{img_b64}"
                        }
                    }
                ]
            }
        ]
        
        # 非流式调用
        import asyncio
        resp = asyncio.run(llm.chat(messages, temperature=0.3))
        
        return {"status": "ok", "answer": resp}
    
    except Exception as e:
        return {"error": f"图片分析失败: {e}"}


@tool("extract_text_from_image", "从图片中提取文字(OCR)", timeout=30)
def extract_text_from_image(
    image_path: str = "",
    image_base64: str = "",
) -> dict:
    """
    从图片中提取文字。
    
    Args:
        image_path: 图片文件路径
        image_base64: 图片 base64 编码
    
    Returns:
        提取的文字
    """
    return analyze_image.func(
        image_path=image_path,
        image_base64=image_base64,
        question="请提取图片中的所有文字,保持原始格式,只返回文字内容"
    )


@tool("compare_images", "对比两张图片的差异", timeout=30)
def compare_images(
    image1_path: str = "",
    image1_base64: str = "",
    image2_path: str = "",
    image2_base64: str = "",
) -> dict:
    """
    对比两张图片的差异。
    
    Args:
        image1_path: 第一张图片路径
        image1_base64: 第一张图片 base64
        image2_path: 第二张图片路径
        image2_base64: 第二张图片 base64
    
    Returns:
        对比结果
    """
    # 获取图片数据
    def get_b64(path, b64):
        if path:
            if not os.path.exists(path):
                return None, f"文件不存在: {path}"
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode(), None
        elif b64:
            return b64, None
        return None, "请提供图片"
    
    img1_b64, err1 = get_b64(image1_path, image1_base64)
    img2_b64, err2 = get_b64(image2_path, image2_base64)
    
    if err1:
        return {"error": f"图片1: {err1}"}
    if err2:
        return {"error": f"图片2: {err2}"}
    
    # 合并为一张图片发送给 LLM
    # 简化: 分别分析后对比
    try:
        from app.core.llm import LLMClient
        import asyncio
        
        llm = LLMClient()
        
        # 获取图片类型
        def get_mime(b64):
            img_bytes = base64.b64decode(b64[:100])
            if img_bytes[:3] == b'\xff\xd8\xff':
                return "image/jpeg"
            elif img_bytes[:4] == b'\x89PNG':
                return "image/png"
            return "image/jpeg"
        
        mime1 = get_mime(img1_b64)
        mime2 = get_mime(img2_b64)
        
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请对比这两张图片的异同点,详细描述它们的区别"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime1};base64,{img1_b64}"}
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime2};base64,{img2_b64}"}
                    }
                ]
            }
        ]
        
        resp = asyncio.run(llm.chat(messages, temperature=0.3))
        return {"status": "ok", "comparison": resp}
    
    except Exception as e:
        return {"error": f"图片对比失败: {e}"}


@tool("describe_chart", "分析图表内容并提取数据", timeout=30)
def describe_chart(
    image_path: str = "",
    image_base64: str = "",
) -> dict:
    """
    分析图表图片,提取数据和趋势。
    
    Args:
        image_path: 图表图片路径
        image_base64: 图表图片 base64
    
    Returns:
        图表分析结果
    """
    return analyze_image.func(
        image_path=image_path,
        image_base64=image_base64,
        question="请分析这个图表的类型、标题、坐标轴含义、数据趋势,并尽可能提取出具体的数据值。返回 JSON 格式。"
    )
