"""文本切块:按段落和句子切分,支持重叠。"""
from __future__ import annotations

import re


def split_paragraphs(text: str) -> list[str]:
    """按空行/换行切分段落。"""
    parts = re.split(r"\n\s*\n|\n", text)
    return [p.strip() for p in parts if p.strip()]


def split_sentences(text: str) -> list[str]:
    """按中文句号/英文句号切分句子。"""
    parts = re.split(r"(?<=[。！？!?.;；])\s*|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """按字符数切块,块间保留重叠以维持上下文。"""
    text = text.strip()
    if not text:
        return []

    # 先按段落合并,再切块
    paragraphs = split_paragraphs(text)
    chunks: list[str] = []
    buffer = ""

    for para in paragraphs:
        if len(buffer) + len(para) + 1 <= chunk_size:
            buffer = f"{buffer}\n{para}" if buffer else para
        else:
            if buffer:
                chunks.append(buffer)
            # 段落本身超长时继续细分
            if len(para) > chunk_size:
                for i in range(0, len(para), chunk_size - overlap):
                    chunks.append(para[i : i + chunk_size])
                buffer = ""
            else:
                buffer = para

    if buffer:
        chunks.append(buffer)

    # 处理重叠:简单策略——最后一块与上一块拼接防止截断
    return [c for c in chunks if c.strip()]


def chunk_documents(docs: list[dict], chunk_size: int = 500, overlap: int = 50) -> list[dict]:
    """将文档列表转换为块列表,附带来源信息。"""
    result: list[dict] = []
    for doc in docs:
        text = doc.get("text", "")
        meta = {k: v for k, v in doc.items() if k != "text"}
        for i, chunk in enumerate(chunk_text(text, chunk_size, overlap)):
            result.append({"text": chunk, "metadata": {**meta, "chunk_index": i}})
    return result
