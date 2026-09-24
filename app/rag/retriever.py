"""检索器:接收查询,返回相关文档片段。

混合检索策略:
1. 向量召回(store,faiss/Milvus/pgvector 加速)
2. 向量分 + 关键词覆盖分加权混合,得到候选排序
3. 可选重排(settings.rerank_mode,默认 RRF 融合向量/关键词排序位置)

多路召回(M5 增强):
4. BM25 本地词频召回 (app/rag/multi_retriever.py)
5. 查询扩展召回 (同义改写 + 子问题拆分)
6. RRF 融合三路结果

返回的每条结果含 vector_score / keyword_score / score / rrf_score,
兼容旧调用方对字段的读取。
"""
from __future__ import annotations

import threading
from typing import Optional

from app.config import settings
from app.rag.embeddings import _keyword_tokens, get_embedder, run_embed
from app.rag.multi_retriever import MultiPathRetriever, bm25_score, expand_query
from app.rag.reranker import get_reranker
from app.rag.vector_store_backend import get_vector_store_backend

_lock = threading.Lock()
_instance = None
_multi_instance = None


def _build_store():
    """使用新的向量存储后端抽象层,自动选择 faiss/Milvus/pgvector/SQLite"""
    return get_vector_store_backend(settings.vector_store_backend)


_store = _build_store()

# 向量分与关键词分的权重
VEC_WEIGHT = 0.6
KW_WEIGHT = 0.4


def get_store() -> object:
    return _store


def keyword_score(query: str, text: str) -> float:
    """关键词覆盖率:查询词命中文本的比例(单字+二元组+英文词)。"""
    q_tokens = set(_keyword_tokens(query or ""))
    if not q_tokens:
        return 0.0
    t_tokens = set(_keyword_tokens(text or ""))
    if not t_tokens:
        return 0.0
    overlap = len(q_tokens & t_tokens)
    return overlap / len(q_tokens)


class Retriever:
    """兼容层:保留旧 Retriever 接口,底层委托给 MultiPathRetriever。"""

    def __init__(self, store=None):
        self.store = store or _store
        self.embedder = get_embedder()

    def search(self, query: str, top_k: int = 5,
               filters: Optional[dict] = None,
               min_relevance: Optional[float] = None) -> list[dict]:
        """多路召回 + 兼容旧签名。"""
        multi = get_multi_retriever()
        if getattr(settings, "rag_multi_path_enabled", True):
            return multi.search(query, top_k=top_k, filters=filters, min_relevance=min_relevance)
        return self._legacy_search(query, top_k=top_k)

    def _legacy_search(self, query: str, top_k: int = 5) -> list[dict]:
        """旧版单路检索(向量+关键词混合)。"""
        from app.core.tenant_context import get_tenant

        vec = run_embed(self.embedder, [query])[0]
        tid = get_tenant() or "default"
        candidates = self.store.search(vec, top_k=max(top_k * 4, 10), tenant_id=tid)
        min_rel = getattr(settings, "rag_min_relevance", 0.0) or 0.0
        scored = []
        for r in candidates:
            text = r.get("metadata", {}).get("text", "")
            vec_score = r.get("score", 0.0)
            kw_score = keyword_score(query, text)
            hybrid = VEC_WEIGHT * vec_score + KW_WEIGHT * kw_score
            scored.append({
                "id": r["id"],
                "score": hybrid,
                "vector_score": round(vec_score, 4),
                "keyword_score": round(kw_score, 4),
                "metadata": r.get("metadata", {}),
            })
        if min_rel > 0:
            scored = [s for s in scored if s["score"] >= min_rel]
        reranker = get_reranker()
        ranked = reranker.rerank(query, scored, top_k=top_k)
        for item in ranked:
            item["score"] = item.get("rrf_score", item.get("score", 0.0))
        return ranked

    def format_context(self, query: str, top_k: int = 5, max_chars: int = 3000,
                       filters: Optional[dict] = None) -> str:
        results = self.search(query, top_k=top_k, filters=filters)
        if not results:
            return ""
        parts = []
        total = 0
        for r in results:
            meta = r.get("metadata", {})
            text = meta.get("text", "")
            source = meta.get("source", "unknown")
            if total + len(text) > max_chars:
                text = text[: max_chars - total]
            parts.append(f"【{source}】{text}")
            total += len(text)
            if total >= max_chars:
                break
        return "\n\n".join(parts)

    def format_context_with_refs(self, query: str, top_k: int = 5,
                                  max_chars: int = 3000,
                                  filters: Optional[dict] = None) -> tuple[str, list[dict]]:
        results = self.search(query, top_k=top_k, filters=filters)
        if not results:
            return "", []
        parts = []
        refs = []
        total = 0
        return_refs = getattr(settings, "rag_return_refs", True)
        for r in results:
            meta = r.get("metadata", {})
            text = meta.get("text", "")
            source = meta.get("source", "unknown")
            if total + len(text) > max_chars:
                text = text[: max_chars - total]
            parts.append(f"【{source}】{text}")
            if return_refs:
                refs.append({
                    "doc_id": meta.get("doc_id", ""),
                    "source": source,
                    "chunk_index": meta.get("chunk_index", 0),
                    "score": round(r.get("score", 0), 4),
                })
            total += len(text)
            if total >= max_chars:
                break
        return "\n\n".join(parts), refs

    def update_index(self, chunks: list[dict]) -> None:
        """更新多路召回索引(BM25 倒排 + 扩展查询缓存)。"""
        multi = get_multi_retriever()
        multi.update_index(chunks)


def get_multi_retriever() -> MultiPathRetriever:
    global _multi_instance
    if _multi_instance is None:
        with _lock:
            if _multi_instance is None:
                _multi_instance = MultiPathRetriever(store=_store)
    return _multi_instance


def get_retriever() -> Retriever:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = Retriever(_store)
    return _instance
