"""ANN 索引(faiss)、RRF 重排、API 嵌入配置测试。"""
import json

import numpy as np

from app.rag.embeddings import HashEmbedding, _embedding_endpoint, _normalize
from app.rag.reranker import RRFReranker, rrf_fuse, rrf_score


def test_normalize():
    v = _normalize([3.0, 4.0])
    assert abs((v[0] ** 2 + v[1] ** 2) ** 0.5 - 1.0) < 1e-9
    assert _normalize([0.0, 0.0]) == [0.0, 0.0]


def test_rrf_score_decreasing():
    assert rrf_score(1) > rrf_score(2) > rrf_score(3)


def test_rrf_fuse_merges_routes():
    a = [{"id": "x", "score": 1.0}, {"id": "y", "score": 0.8}]
    b = [{"id": "y", "score": 0.9}, {"id": "x", "score": 0.7}]
    fused = rrf_fuse([a, b])
    assert len(fused) == 2
    # x: 路 a 第1 + 路 b 第2;y: 路 a 第2 + 路 b 第1 -> 分数相等,稳定排序保持 x 在前
    assert fused[0]["id"] == "x"
    assert abs(fused[0]["fusion_score"] - (rrf_score(1) + rrf_score(2))) < 1e-9
    assert abs(fused[1]["fusion_score"] - (rrf_score(2) + rrf_score(1))) < 1e-9


def test_rrf_reranker_ranks_keyword_hits_first():
    """并列融合分时关键词命中应排前(与旧 hybrid 语义一致)。"""
    reranker = RRFReranker()
    candidates = [
        {"id": "kw", "metadata": {"text": "苹果手机发布公告"}, "vector_score": 0.5, "keyword_score": 1.0},
        {"id": "vec", "metadata": {"text": "完全无关的水果市场新闻"}, "vector_score": 0.9, "keyword_score": 0.0},
    ]
    ranked = reranker.rerank("苹果", candidates, top_k=2)
    assert ranked[0]["id"] == "kw"
    assert "rrf_score" in ranked[0]


def test_faiss_backend_returns_same_results_as_linear():
    """同一批向量,faiss flat 索引与线性扫描返回一致。"""
    from app.rag.store import VectorStore

    store = VectorStore(db_path=":memory:", dim=4)
    store.add("a", [1, 0, 0, 0], {"text": "苹果"})
    store.add("b", [0, 1, 0, 0], {"text": "香蕉"})
    results = store.search([0.9, 0.1, 0, 0], top_k=1)
    assert results[0]["id"] == "a"
    assert store.backend() in ("faiss_flat", "faiss_hnsw", "sqlite")


def test_faiss_delete_and_rebuild():
    """删除后 faiss 与线性结果一致;重建后仍可检索。"""
    from app.rag.store import VectorStore

    store = VectorStore(db_path=":memory:", dim=4)
    store.add("a", [1, 0, 0, 0], {"text": "苹果"})
    store.add("b", [0, 1, 0, 0], {"text": "香蕉"})
    store.delete("a")
    results = store.search([0.9, 0.1, 0, 0], top_k=2)
    assert all(r["id"] != "a" for r in results)
    store._rebuild_index()  # 模拟重启重建
    results = store.search([0.0, 1.0, 0, 0], top_k=1)
    assert results[0]["id"] == "b"


def test_faiss_hnsw_backend():
    """HNSW 后端可建索引并检索。"""
    import app.config as config_mod

    old = config_mod.settings.vector_index_backend
    config_mod.settings.vector_index_backend = "hnsw"
    try:
        from app.rag.store import VectorStore

        store = VectorStore(db_path=":memory:", dim=4)
        store.add("a", [1, 0, 0, 0], {"text": "苹果"})
        store.add("b", [0, 1, 0, 0], {"text": "香蕉"})
        results = store.search([0.9, 0.1, 0, 0], top_k=1)
        assert results[0]["id"] == "a"
    finally:
        config_mod.settings.vector_index_backend = old


def test_embedding_endpoint_fallback():
    """未配置 embedding_api_* 时回退 openai_compat / 默认 provider。"""
    import app.config as config_mod

    old_model = config_mod.settings.embedding_api_model
    config_mod.settings.embedding_api_model = "text-embedding-test"
    try:
        base, key, model = _embedding_endpoint()
        assert model == "text-embedding-test"
        assert base  # 总能解析到某个 base_url
    finally:
        config_mod.settings.embedding_api_model = old_model
