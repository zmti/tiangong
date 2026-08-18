"""语义切块:使用 LLM 在语义边界处切分长文本,提高 RAG 检索准确性。

相比固定字符切分(Semantic Chunking):
- 按段落/章节的语义完整性切分,避免截断关键上下文
- 块间有重叠以维持连贯性
- 通过 LLM 判断切分点是否合适

当 LLM 不可用时,自动降级到固定字符切分(同 chunker.py 的逻辑)。
"""
from __future__ import annotations

import json
import re
from typing import Optional

# 切分提示词模板
CHUNK_PROMPT = """你是一个文本切分专家。请将以下文本在语义最完整的边界处切分为若干段。

规则:
1. 每段应保持完整的语义(一个主题/观点/步骤)
2. 段落长度在 200-800 字之间
3. 段落之间有重叠(最后一段的前1/3与下一段的前1/3内容对应)
4. 只输出 JSON 数组,不要其他文字

输出格式:
["段落1", "段落2", ...]

当前文本(共 {total_chars} 字):
{text[:2000]}"""


def _fallback_chunk(
    text: str,
    chunk_size: int = 500,
    overlap: int = 50,
) -> list[str]:
    """固定字符切分(降级方案)。"""
    from app.rag.chunker import chunk_text
    return chunk_text(text, chunk_size, overlap)


def semantic_chunk(
    text: str,
    llm_client=None,
    chunk_size: int = 500,
    overlap: int = 50,
    max_chunks: int = 20,
) -> list[str]:
    """语义切分:优先用 LLM,不可用时降级到固定字符切分。"""
    text = text.strip()
    if not text:
        return []

    # 短文本直接返回
    if len(text) <= chunk_size:
        return [text]

    # 尝试语义切分
    if llm_client is not None:
        try:
            result = _llm_chunk(text, llm_client, chunk_size, max_chunks)
            if result and len(result) > 1:
                return result
        except Exception:  # noqa: BLE001
            pass  # LLM 失败,降级

    # 降级:固定字符切分
    return _fallback_chunk(text, chunk_size, overlap)


def _llm_chunk(
    text: str,
    llm_client,
    chunk_size: int,
    max_chunks: int,
) -> list[str]:
    """调用 LLM 进行语义切分。"""
    total_chars = len(text)
    prompt = CHUNK_PROMPT.format(total_chars=total_chars, text=text[:2000])

    # 调用 LLM (chat_json 方法)
    response = llm_client.chat(
        [{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=2000,
    )
    raw = (response or "").strip()

    # 提取 JSON 数组
    # 去掉 markdown 代码块
    raw = re.sub(r"```(?:json)?\n?", "", raw)
    raw = re.sub(r"```\s*$", "", raw)
    raw = raw.strip()

    try:
        chunks = json.loads(raw)
        if isinstance(chunks, list) and all(isinstance(c, str) for c in chunks):
            return [c.strip() for c in chunks if c.strip()][:max_chunks]
    except json.JSONDecodeError:
        pass

    # LLM 返回非 JSON,退回固定切分
    return []


def chunk_documents_semantic(
    docs: list[dict],
    llm_client=None,
    chunk_size: int = 500,
    overlap: int = 50,
    max_chunks: int = 20,
) -> list[dict]:
    """对文档列表进行语义切分。"""
    result = []
    for doc in docs:
        text = doc.get("text", "")
        meta = {k: v for k, v in doc.items() if k != "text"}
        for i, chunk in enumerate(semantic_chunk(text, llm_client, chunk_size, overlap, max_chunks)):
            result.append({"text": chunk, "metadata": {**meta, "chunk_index": i, "method": "semantic"}})
    return result
