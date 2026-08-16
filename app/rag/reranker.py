"""检索重排器:对召回候选做二次排序,提升 top-k 精度。

模式(settings.rerank_mode):
  none: 不重排,直接用混合打分结果(仅用于对比)
  rrf:   Reciprocal Rank Fusion —— 把向量分与关键词分各自的排序位置
        融合成一个分,对"多路召回取并集"场景特别稳(默认,零外部依赖)
  api:  调用外部 Rerank API(如 dashscope gte-rerank、Jina Reranker 等
        OpenAI 兼容的 rerank 接口),语义最强但需配置 key

所有模式都在 Retriever.search 中作为最后一道排序。
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Optional

from app.config import settings
from app.rag.embeddings import _keyword_tokens


def rrf_score(rank: int, k: int = 60) -> float:
    """单一路由的 RRF 得分:rank 从 1 开始。"""
    return 1.0 / (k + rank)


def rrf_fuse(routes: list[list[dict]]) -> list[dict]:
    """把多路排序结果的候选融合成一份(按 RRF 总分降序)。

    routes: 每路是候选 dict 列表(须含 'id');返回合并后的候选列表,
    每个候选额外带 fusion_score。仅保留下一步(可能再被关键词/API 重排)。
    """
    fused: dict[str, dict] = {}
    for route in routes:
        for rank, item in enumerate(route, start=1):
            cid = item["id"]
            if cid not in fused:
                entry = dict(item)
                entry["fusion_score"] = 0.0
                fused[cid] = entry
            fused[cid]["fusion_score"] += rrf_score(rank)
    return sorted(fused.values(), key=lambda x: x["fusion_score"], reverse=True)


def _keyword_overlap(query: str, text: str) -> float:
    """查询词命中文本的比例(0~1),作为 RRF 前的关键词排序依据。"""
    q_tokens = set(_keyword_tokens(query or ""))
    if not q_tokens:
        return 0.0
    t_tokens = set(_keyword_tokens(text or ""))
    if not t_tokens:
        return 0.0
    overlap = len(q_tokens & t_tokens)
    return overlap / len(q_tokens)


class Reranker:
    """重排器接口。"""

    def rerank(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        """对候选排序,返回前 top_k(可能就地修改候选)。"""
        raise NotImplementedError


class NoneReranker(Reranker):
    """不重排:仅按候选原始顺序截断。"""

    def rerank(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        return candidates[:top_k]


class RRFReranker(Reranker):
    """RRF:融合向量分与关键词分各自的排序位置。"""

    def rerank(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        if not candidates:
            return []
        # 向量路:候选自带 vector_score
        vec_route = sorted(candidates, key=lambda x: x.get("vector_score", 0.0), reverse=True)
        # 关键词路:用 query 对候选文本算关键词覆盖
        kw_route = sorted(
            candidates,
            key=lambda x: _keyword_overlap(query, x.get("metadata", {}).get("text", "")),
            reverse=True,
        )
        fused = rrf_fuse([vec_route, kw_route])
        # 并列融合分时,关键词命中优先(关键词路 rank1 应胜出)
        fused.sort(
            key=lambda x: (
                x["fusion_score"],
                x.get("keyword_score", 0.0),
                x.get("vector_score", 0.0),
            ),
            reverse=True,
        )
        # 保留原始打分字段,供上层展示;同名融合分合并
        result = []
        for item in fused:
            out = dict(item)
            out["rrf_score"] = round(item["fusion_score"], 6)
            result.append(out)
        return result[:top_k]


class APIReranker(Reranker):
    """调用外部 Rerank API(dashscope gte-rerank / Jina 等)。

    请求格式(dashscope):
      POST {base_url}  {model, query, documents: [...], top_n}
      返回 {results: [{index, relevance_score, ...}]}
    兼容 Jina:
      POST {base_url}/rerank  {model, query, documents, top_n}
      返回 {results: [{index, relevance_score, document}]}
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        import httpx

        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._httpx = httpx
        self._timeout = 30.0

    def rerank(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        if not candidates:
            return []
        docs = [c.get("metadata", {}).get("text", "") or c.get("id", "") for c in candidates]
        try:
            scores = asyncio.run(self._call(query, docs, top_n=top_k))
        except Exception:  # noqa: BLE001  # API 失败回退为原顺序,不影响主流程
            return candidates[:top_k]
        if not scores:
            return candidates[:top_k]
        ordered = []
        for idx in scores:
            if 0 <= idx < len(candidates):
                ordered.append(candidates[idx])
        return ordered[:top_k]

    async def _call(self, query: str, documents: list[str], top_n: int) -> list[int]:
        payload = {
            "model": self._model,
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
        }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with self._httpx.AsyncClient(timeout=self._timeout) as client:
            # 优先试 {base}/rerank(标准),失败再试裸 base(dashscope)
            for url in (f"{self._base_url}/rerank", self._base_url):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except Exception:  # noqa: BLE001
                    data = None
            if not data:
                return []
        results = data.get("results") or []
        out = []
        for r in sorted(results, key=lambda x: float(x.get("relevance_score", x.get("score", 0))), reverse=True):
            if "index" in r:
                out.append(int(r["index"]))
        return out


_lock = threading.Lock()
_instance: Optional[Reranker] = None


def get_reranker() -> Reranker:
    """按配置返回重排器单例。"""
    global _instance
    if _instance is not None:
        return _instance
    with _lock:
        if _instance is not None:
            return _instance
        mode = (settings.rerank_mode or "none").lower()
        if mode == "api" and settings.rerank_api_base_url and settings.rerank_api_model:
            _instance = APIReranker(
                settings.rerank_api_base_url,
                settings.rerank_api_key,
                settings.rerank_api_model,
            )
        elif mode == "rrf":
            _instance = RRFReranker()
        else:
            _instance = NoneReranker()
        return _instance
