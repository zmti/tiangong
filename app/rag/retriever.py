"""检索器:接收查询,返回相关文档片段。

混合检索策略:
1. 向量召回(store,faiss 加速)
2. 向量分 + 关键词覆盖分加权混合,得到候选排序
3. 可选重排(settings.rerank_mode,默认 RRF 融合向量/关键词排序位置)

返回的每条结果含 vector_score / keyword_score / score / rrf_score,
兼容旧调用方对字段的读取。
"""
from __future__ import annotations

import threading

from app.config import settings
from app.rag.embeddings import _keyword_tokens, get_embedder, run_embed
from app.rag.reranker import get_reranker

_lock = threading.Lock()
_instance = None


def _build_store():
    """选择向量存储后端:pgvector 可用时用 PGVectorStore,否则 SQLite+faiss。"""
    try:  # noqa: PLW0129
        from app.core.storage import get_vector_backend

        backend, dsn = get_vector_backend()
        if backend == "pgvector":
            from app.rag.store_pg import PGVectorStore

            return PGVectorStore(dsn=dsn, table=settings.pgvector_client_table)
    except Exception:  # noqa: BLE001  # pgvector 不可用回退 SQLite+faiss
        pass
    from app.rag.store import VectorStore

    return VectorStore()


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
    def __init__(self, store: VectorStore):
        self.store = store
        self.embedder = get_embedder()

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """混合检索 + 可选重排:返回按相关性排序的片段。"""
        vec = run_embed(self.embedder, [query])[0]
        # 多取候选,留足混合/重排空间
        candidates = self.store.search(vec, top_k=max(top_k * 4, 10))
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
        reranker = get_reranker()
        ranked = reranker.rerank(query, scored, top_k=top_k)
        for item in ranked:
            item["score"] = item.get("rrf_score", item.get("score", 0.0))
        return ranked

    def format_context(self, query: str, top_k: int = 5, max_chars: int = 3000) -> str:
        results = self.search(query, top_k=top_k)
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


def get_retriever() -> Retriever:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = Retriever(_store)
    return _instance
