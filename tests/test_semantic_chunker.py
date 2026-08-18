"""语义切块测试。"""
import pytest

from app.rag.semantic_chunker import chunk_documents_semantic, semantic_chunk


def test_semantic_chunk_short_text():
    text = "这是一段很短的文字。"
    chunks = semantic_chunk(text)
    assert chunks == [text]


def test_semantic_chunk_empty():
    assert semantic_chunk("") == []


def test_semantic_chunk_no_llm():
    """无 LLM 客户端时降级到固定切分。"""
    long_text = "段落一的内容。\n\n段落二的内容。\n\n段落三的内容。"
    chunks = semantic_chunk(long_text, llm_client=None)
    assert isinstance(chunks, list)
    assert len(chunks) >= 1
    assert all(c.strip() for c in chunks)


def test_chunk_documents_semantic():
    docs = [{"text": "第一段内容。\n\n第二段内容。\n\n第三段内容。", "source": "test.txt"}]
    result = chunk_documents_semantic(docs, llm_client=None)
    assert len(result) >= 1
    assert all("metadata" in d for d in result)
    assert all("text" in d for d in result)
