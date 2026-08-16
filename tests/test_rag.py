"""RAG 模块测试:切块、向量化、检索、文档索引。"""
import asyncio

import numpy as np

from app.rag.chunker import chunk_text
from app.rag.documents import index_text
from app.rag.embeddings import HashEmbedding
from app.rag.retriever import get_retriever, get_store
from app.rag.store import VectorStore


def test_chunk_text():
    text = "这是第一段内容,用于测试。\n这是第二段,足够长以便触发切块。" * 20
    chunks = chunk_text(text, chunk_size=200, overlap=20)
    assert len(chunks) >= 2
    assert all(isinstance(c, str) and c for c in chunks)


def test_hash_embedding_dim_and_norm():
    emb = HashEmbedding(dim=256)
    vec = emb.embed("资产管理系统")
    assert len(vec) == 256
    norm = np.linalg.norm(vec)
    assert abs(norm - 1.0) < 1e-6


def test_vector_store_search():
    store = VectorStore(dim=4)
    store.add("a", [1, 0, 0, 0], {"text": "苹果"})
    store.add("b", [0, 1, 0, 0], {"text": "香蕉"})
    results = store.search([0.9, 0.1, 0, 0], top_k=1)
    assert results[0]["id"] == "a"


def test_index_and_retrieve():
    get_store().clear()
    index_text("中国的首都是北京。北京是政治文化中心。", source="test_doc")
    retriever = get_retriever()
    results = retriever.search("北京", top_k=2)
    assert len(results) >= 1
    assert results[0]["metadata"]["source"] == "test_doc"
    ctx = retriever.format_context("北京")
    assert "北京" in ctx


def test_rag_tool_registered():
    from app.tools.registry import get_registry

    registry = get_registry()
    tool = registry.get("rag_search")
    assert tool is not None
    assert "query" in tool.json_schema["properties"]


def test_retriever_keyword_score():
    from app.rag.retriever import keyword_score

    assert keyword_score("苹果 香蕉", "苹果很好吃") > 0
    assert keyword_score("苹果", "香蕉很好吃") == 0.0
    assert keyword_score("", "任意内容") == 0.0


def test_keyword_tokens_include_unigrams():
    """关键词打分用单字+二元组,提升中文短查询的命中率(向量存储保持不变)。"""
    from app.rag.embeddings import _keyword_tokens, _tokenize

    kt = set(_keyword_tokens("喜欢的颜色"))
    assert "颜" in kt and "色" in kt and "颜色" in kt
    # 单字提问现在能命中包含该字的内容
    assert "蓝" in set(_keyword_tokens("喜欢蓝色"))
    # _tokenize(向量用)保持原二元组语义,不产生单字
    assert "颜" not in set(_tokenize("喜欢的颜色"))


def test_retriever_keyword_unigram_boost():
    from app.rag.retriever import keyword_score

    # 加入单字后,含共同单字的文本获得非零关键词分(之前纯二元组可能为 0)
    assert keyword_score("颜色", "蓝色") > 0


def test_retriever_hybrid_ranks_keyword_hits():
    """关键词命中的片段应排在纯向量近似的前面。"""
    store = VectorStore(dim=4)
    # 精确命中"苹果"的片段
    store.add("kw", [1, 0, 0, 0], {"text": "苹果手机发布公告"})
    # 向量上与查询接近但不含"苹果"的片段
    store.add("vec", [0.99, 0.01, 0, 0], {"text": "完全无关的水果市场新闻"})

    from app.rag.embeddings import HashEmbedding
    from app.rag.retriever import Retriever

    retriever = Retriever(store)
    retriever.embedder = HashEmbedding(dim=4)
    results = retriever.search("苹果", top_k=2)
    assert results[0]["metadata"]["text"] == "苹果手机发布公告"
    assert "keyword_score" in results[0]
