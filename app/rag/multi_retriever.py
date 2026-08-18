"""RAG 多路召回检索器。

在原有向量+关键词混合检索基础上,新增三路召回策略:

  1. 向量召回: 现有 embedder + store.search (faiss/pgvector)
  2. BM25 召回: 本地词频统计,精确匹配专业术语/代码片段
  3. 查询扩展召回: 用 LLM 生成同义改写 + 子问题拆分,扩大召回覆盖面

三路结果经 RRF(fusion rank)融合排序,可选 cross-encoder 重排。

支持元数据过滤(source/doc_id/tenant_id)和最小相关性阈值。
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import Counter
from typing import Optional

from app.config import settings
from app.rag.embeddings import _keyword_tokens, get_embedder, run_embed
from app.rag.reranker import get_reranker

_lock = threading.Lock()
_instance = None

# 全局倒排索引(内存缓存,按 source 分桶)
_inv_index: dict[str, dict[str, int]] = {}  # term -> {doc_id: tf}
_doc_lengths: dict[str, int] = {}           # doc_id -> char count
_avgdl = 200                                # 平均文档长度


def _build_or_update_index(chunks: list[dict]) -> None:
    """增量更新 BM25 倒排索引(按 doc_id 聚合词频)。"""
    global _avgdl
    total_len = 0
    for chunk in chunks:
        doc_id = chunk.get("metadata", {}).get("source", chunk.get("id", ""))
        text = chunk.get("text", "") or chunk.get("metadata", {}).get("text", "")
        if not text:
            continue
        tokens = _keyword_tokens(text)
        freq = Counter(tokens)
        prev = _inv_index.get(doc_id, {})
        merged: dict[str, int] = dict(prev)
        for t, c in freq.items():
            merged[t] = merged.get(t, 0) + c
        _inv_index[doc_id] = merged
        total_len += len(text)
    _avgdl = total_len / max(len(_inv_index), 1)


def bm25_score(query: str, doc_id: str, k1: float = 1.5, b: float = 0.75) -> float:
    """计算 BM25 分数。k1 控制词频饱和,b 控制长度归一化。"""
    q_terms = _keyword_tokens(query)
    if not q_terms or doc_id not in _inv_index:
        return 0.0
    doc_terms = _inv_index[doc_id]
    doc_len = len(doc_terms) * _avgdl  # 近似文档长度
    score = 0.0
    for term in q_terms:
        tf = doc_terms.get(term, 0)
        if tf == 0:
            continue
        idf = math.log(1.0 + (len(_inv_index) - tf + 0.5) / (tf + 0.5))
        freq_term = (tf * (k1 + 1.0)) / (tf + k1 * (1.0 - b + b * doc_len / max(_avgdl, 1)))
        score += idf * freq_term
    return score / max(len(q_terms), 1)


def expand_query(query: str) -> list[str]:
    """查询扩展:返回原始查询 + 简化改写 + 子问题拆分。

    纯本地策略,无需 LLM 调用。若配置了 expand_query_llm=true,则通过
    LLM 生成同义扩展(需联网)。
    """
    variations = [query]
    # 去掉标点/空格统一
    cleaned = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", query).strip()
    if cleaned != query:
        variations.append(cleaned)
    # 提取关键词作为子查询
    tokens = _keyword_tokens(query)
    if len(tokens) >= 2:
        # 取前3个核心词做子查询
        variations.append(" ".join(tokens[:3]))
    # 配置驱动的 LLM 扩展
    if getattr(settings, "rag_query_expand_llm", False):
        try:
            from app.core.llm import LLMClient
            llm = LLMClient()
            resp = llm.chat([
                {"role": "user", "content": (
                    f"用中文回答,直接输出同义改写和子问题,每行一个,不需要解释:\n{query}"
                )}
            ], max_tokens=200)
            for line in (resp or "").split("\n"):
                line = line.strip()
                if line and len(line) > 3:
                    variations.append(line)
        except Exception:  # noqa: BLE001
            pass
    return list(dict.fromkeys(variations))  # 去重但保留顺序


class MultiPathRetriever:
    """多路召回检索器。"""

    def __init__(self, store=None):
        self.store = store
        self.embedder = get_embedder()
        self._lock = threading.RLock()
        self._last_index_time = 0.0
        self._index_chunks: list[dict] = []

    def update_index(self, chunks: list[dict]) -> None:
        """更新 BM25 索引(增量追加)。"""
        with self._lock:
            self._index_chunks.extend(chunks)
            _build_or_update_index(chunks)
            self._last_index_time = time.time()

    def search(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[dict] = None,
        min_relevance: Optional[float] = None,
    ) -> list[dict]:
        """多路召回 + RRF 融合。

        filters: {"source": "xxx", "doc_id": "yyy", "tenant_id": "zzz"}
        """
        min_rel = min_relevance or (getattr(settings, "rag_min_relevance", 0.0) or 0.0)

        # ---- 路1: 向量召回 ----
        vec_results = self._vector_search(query, top_k=top_k * 4, filters=filters)

        # ---- 路2: BM25 召回 ----
        bm25_results = self._bm25_search(query, top_k=top_k * 4, filters=filters)

        # ---- 路3: 查询扩展召回 ----
        expanded = expand_query(query)
        expanded_results: list[dict] = []
        seen_ids: set = set(r["id"] for r in vec_results) | set(r["id"] for r in bm25_results)
        for exp_q in expanded[1:]:  # 跳过原始查询(已在路1中)
            exp_vec = self._vector_search(exp_q, top_k=top_k * 2, filters=filters)
            for r in exp_vec:
                if r["id"] not in seen_ids:
                    r["score"] = r.get("score", 0.0) * 0.8  # 扩展查询降权
                    expanded_results.append(r)
                    seen_ids.add(r["id"])

        # ---- RRF 融合 ----
        fused = self._rrf_fusion(vec_results, bm25_results, expanded_results, k_rrf=60)

        # ---- 过滤低相关度 ----
        if min_rel > 0:
            fused = [r for r in fused if r.get("score", 0.0) >= min_rel]

        # ---- 可选交叉编码器重排 ----
        reranker = get_reranker()
        if reranker:
            fused = reranker.rerank(query, fused, top_k=top_k)
        else:
            fused = fused[:top_k]

        # 补充路径信息供调试
        for item in fused:
            if "paths" not in item:
                item["paths"] = self._identify_paths(item["id"], vec_results, bm25_results, expanded_results)
        return fused

    def _vector_search(
        self, query: str, top_k: int = 20, filters: Optional[dict] = None
    ) -> list[dict]:
        """向量召回。"""
        try:
            from app.core.tenant_context import get_tenant

            vec = run_embed(self.embedder, [query])[0]
            resolved_tid = (filters or {}).get("tenant_id") or get_tenant() or "default"
            results = self.store.search(vec, top_k=max(top_k, 10), tenant_id=resolved_tid)
            scored = []
            for r in results:
                scored.append({
                    "id": r["id"],
                    "score": r.get("score", 0.0),
                    "vector_score": round(r.get("score", 0.0), 4),
                    "keyword_score": 0.0,
                    "bm25_score": 0.0,
                    "metadata": r.get("metadata", {}),
                    "_path": "vector",
                })
            return scored
        except Exception:  # noqa: BLE001
            return []

    def _bm25_search(
        self, query: str, top_k: int = 20, filters: Optional[dict] = None
    ) -> list[dict]:
        """BM25 召回。从 store 获取候选,计算 BM25 分。"""
        try:
            tid = filters.get("tenant_id") if filters else None
            # 获取所有向量(doc_id → text)
            from app.core.tenant_context import get_tenant
            resolved_tid = tid or get_tenant() or "default"
            conn = self.store._connect()
            try:
                rows = conn.execute(
                    "SELECT id, meta FROM vectors WHERE tenant_id = ?",
                    (resolved_tid,),
                ).fetchall()
            finally:
                self.store._close(conn)
            scored = []
            for row in rows:
                doc_id = row["id"]
                meta = json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
                text = meta.get("text", "")
                if not text:
                    continue
                score = bm25_score(query, doc_id)
                if score > 0:
                    scored.append({
                        "id": doc_id,
                        "score": score,
                        "vector_score": 0.0,
                        "keyword_score": 0.0,
                        "bm25_score": round(score, 4),
                        "metadata": meta,
                        "_path": "bm25",
                    })
            scored.sort(key=lambda x: x["bm25_score"], reverse=True)
            return scored[:top_k]
        except Exception:  # noqa: BLE001
            return []

    def _rrf_fusion(
        self,
        vec_results: list[dict],
        bm25_results: list[dict],
        exp_results: list[dict],
        k_rrf: int = 60,
    ) -> list[dict]:
        """Reciprocal Rank Fusion 融合三路结果。"""
        rank_scores: dict[str, float] = {}
        path_info: dict[str, list[str]] = {}

        for results_list, path_name in [
            (vec_results, "vector"),
            (bm25_results, "bm25"),
            (exp_results, "expanded"),
        ]:
            for rank, item in enumerate(results_list, start=1):
                doc_id = item["id"]
                rrf = 1.0 / (k_rrf + rank)
                rank_scores[doc_id] = rank_scores.get(doc_id, 0.0) + rrf
                path_info.setdefault(doc_id, []).append(path_name)

        merged = []
        for doc_id, rrf_score in sorted(rank_scores.items(), key=lambda x: -x[1]):
            # 找原始 item 获取 metadata
            base = None
            for lst in [vec_results, bm25_results, exp_results]:
                for item in lst:
                    if item["id"] == doc_id:
                        base = item
                        break
                if base:
                    break
            if not base:
                continue
            merged.append({
                "id": doc_id,
                "score": round(rrf_score, 6),
                "vector_score": base.get("vector_score", 0.0),
                "keyword_score": base.get("keyword_score", 0.0),
                "bm25_score": base.get("bm25_score", 0.0),
                "metadata": base.get("metadata", {}),
                "paths": path_info.get(doc_id, []),
                "_path": ",".join(path_info.get(doc_id, [])),
            })
        return merged

    def _identify_paths(self, doc_id: str, *lists: list[dict]) -> list[str]:
        """识别 doc_id 出现在哪些路。"""
        paths = []
        for lst in lists:
            for item in lst:
                if item["id"] == doc_id:
                    paths.append(item.get("_path", "unknown"))
                    break
        return list(set(paths))

    @staticmethod
    def _filter_kwargs(filters: Optional[dict]) -> dict:
        """把 filters dict 转为 store.search() 的 keyword 参数。"""
        kwargs = {}
        if not filters:
            return kwargs
        if "tenant_id" in filters and filters["tenant_id"]:
            kwargs["tenant_id"] = filters["tenant_id"]
        return kwargs

    def format_context(
        self, query: str, top_k: int = 5, max_chars: int = 3000,
        filters: Optional[dict] = None,
    ) -> str:
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
            paths = r.get("paths", [])
            path_tag = f"[{','.join(paths)}]" if paths else ""
            parts.append(f"【{source}】{path_tag} {text}")
            total += len(text)
            if total >= max_chars:
                break
        return "\n\n".join(parts)

    def format_context_with_refs(
        self, query: str, top_k: int = 5, max_chars: int = 3000,
        filters: Optional[dict] = None,
    ) -> tuple[str, list[dict]]:
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
                    "paths": r.get("paths", []),
                })
            total += len(text)
            if total >= max_chars:
                break
        return "\n\n".join(parts), refs


def get_retriever() -> MultiPathRetriever:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                try:
                    from app.rag.retriever import get_store as _get_legacy_store
                    store = _get_legacy_store()
                except Exception:
                    store = None
                _instance = MultiPathRetriever(store=store)
    return _instance
