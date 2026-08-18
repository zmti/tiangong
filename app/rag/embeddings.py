"""向量化:本地哈希嵌入(零依赖)与 OpenAI 兼容 Embeddings API。

后端由 settings.embedding_backend 控制:
  local: HashEmbedding(关键词哈希,零依赖,无语义)
  api:   APIEmbeddings(调用 OpenAI 兼容 /embeddings 接口,需配置 key)

API 嵌入独立配置(embedding_api_base_url/key/model),未配置时回退
openai_compat_* 再到当前 LLM provider,最后用 OpenAI 官方。
嵌入结果按 embedding_normalize 做 L2 归一化(余弦相似度/faiss IP 需要)。

无论用哪种,store 都会持久化。切换后端会导致向量维度/语义变化,
建议切换后清空知识库重新上传(提供 clear API)。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from typing import Protocol

from app.config import settings


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]: ...


def _tokenize(text: str) -> list[str]:
    """简单中文分词(二元组)+ 英文单词。"""
    tokens: list[str] = []
    for m in re.finditer(r"[a-zA-Z0-9]+", text):
        tokens.append(m.group(0).lower())
    zh = re.findall(r"[\u4e00-\u9fff]", text)
    for i in range(len(zh) - 1):
        tokens.append(zh[i] + zh[i + 1])
    if len(zh) == 1:
        tokens.append(zh[0])
    return tokens


def _keyword_tokens(text: str) -> list[str]:
    """检索用关键词 token(向量存储保持不变,仅用于关键词打分)。

    与 _tokenize 的区别:
    - 额外包含中文单字,提升"单字/简称提问"与长文本的关键词命中率
    - 若安装了 jieba,优先用其切词(语义更准),再叠加单字/二元组兜底
    不改变 _tokenize 输出,因此不会使已持久化的哈希向量失真。
    """
    tokens: list[str] = []
    try:  # jieba 可选:未安装则退化为单字+二元组
        import jieba  # noqa: PLC0415

        for w in jieba.cut(text):
            w = w.strip()
            if w and not w.isspace():
                tokens.append(w.lower())
    except ImportError:  # noqa: BLE001
        pass
    for m in re.finditer(r"[a-zA-Z0-9]+", text):
        tokens.append(m.group(0).lower())
    zh = re.findall(r"[\u4e00-\u9fff]", text)
    for i in range(len(zh) - 1):
        tokens.append(zh[i] + zh[i + 1])
    for ch in zh:
        tokens.append(ch)
    return tokens


def _normalize(vec: list[float]) -> list[float]:
    """L2 归一化:余弦相似度与 faiss IP 距离的前提。"""
    norm = sum(v * v for v in vec) ** 0.5
    if norm == 0:
        return vec
    return [v / norm for v in vec]


class HashEmbedding:
    """基于哈希桶的稀疏向量嵌入,维度固定,无外部模型依赖。"""

    def __init__(self, dim: int = 768, num_hashes: int = 2):
        self.dim = dim
        self.num_hashes = num_hashes

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = _tokenize(text)
        if not tokens:
            return vec
        for token in tokens:
            for h in range(self.num_hashes):
                digest = hashlib.md5(f"{token}#{h}".encode("utf-8")).hexdigest()
                idx = int(digest[:8], 16) % self.dim
                sign = 1.0 if int(digest[8:16], 16) % 2 == 0 else -1.0
                vec[idx] += sign
        return _normalize(vec)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def _embedding_endpoint() -> tuple[str, str, str]:
    """解析 API 嵌入端点,返回 (base_url, api_key, model)。优先级:
    embedding_api_* -> openai_compat_* -> 当前 LLM provider 对应配置 -> OpenAI 官方。
    """
    base = settings.embedding_api_base_url or settings.openai_compat_base_url
    key = settings.embedding_api_key or settings.openai_compat_api_key
    model = settings.embedding_api_model

    if not base:
        provider = settings.agent_llm_provider.lower()
        if provider in ("deepseek", "ds"):
            base = settings.deepseek_base_url
            key = key or settings.deepseek_api_key
        elif provider in ("qwen", "dashscope", "tongyi"):
            base = settings.qwen_base_url
            key = key or settings.qwen_api_key
        elif provider == "ollama":
            base = settings.ollama_base_url
            key = key or "ollama"
    return base, key, model


class APIEmbeddings:
    """调用 OpenAI 兼容 Embeddings API,带批处理、重试与维度缓存。"""

    def __init__(self, base_url: str, api_key: str, model: str):
        from openai import AsyncOpenAI

        self._base_url, self._api_key, self._model = base_url, api_key, model
        self._client = AsyncOpenAI(api_key=api_key or "sk-placeholder", base_url=base_url)
        if not self._model:
            raise ValueError("embedding_api_model 未配置")
        self.dim = self._load_dim()

    # ---- 维度缓存:嵌入维度变化会导致 faiss 索引失效,持久化以便校验 ----
    def _dim_cache_path(self) -> str:
        return os.path.join(settings.resolved_data_dir, "embedding_dim.json")

    def _load_dim(self) -> int:
        try:
            with open(self._dim_cache_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("model") == self._model and isinstance(data.get("dim"), int):
                # 同时更新进程级缓存
                _set_cached_dim(self._model, data["dim"])
                return data["dim"]
        except Exception:  # noqa: BLE001
            pass
        # 检查进程级缓存
        cached = _get_cached_dim(self._model)
        if cached > 0:
            return cached
        return 0

    def _save_dim(self, dim: int) -> None:
        try:
            os.makedirs(settings.resolved_data_dir, exist_ok=True)
            with open(self._dim_cache_path(), "w", encoding="utf-8") as f:
                json.dump({"model": self._model, "dim": dim}, f)
        except Exception:  # noqa: BLE001
            pass
        # 同步进程级缓存
        _set_cached_dim(self._model, dim)

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入,自动分批 + 瞬态错误重试,返回 L2 归一化向量。"""
        batch = max(1, int(settings.embedding_batch_size))
        retries = max(0, int(settings.embedding_retries))
        out: list[list[float]] = []
        for i in range(0, len(texts), batch):
            chunk = texts[i : i + batch]
            vectors = None
            last_err: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    vectors = asyncio.run(self._client_embed(chunk))
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if attempt < retries:
                        import time

                        time.sleep(0.5 * (2 ** attempt))
            if vectors is None:
                raise RuntimeError(f"Embedding API 调用失败: {last_err}")
            if not self.dim:
                self.dim = len(vectors[0]) if vectors else 0
                self._save_dim(self.dim)
            if settings.embedding_normalize:
                vectors = [_normalize(v) for v in vectors]
            out.extend(vectors)
        return out

    async def _client_embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.embeddings.create(model=self._model, input=texts)
        return [d.embedding for d in resp.data]


def run_embed(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    """在任意上下文(同步/异步/线程)安全调用嵌入器,带缓存。

    APIEmbeddings.embed_many 内部使用 asyncio.run,若在已运行的
    事件循环中直接调用会抛 RuntimeError。此处检测到运行中的循环时,
    改在独立工作线程中执行,从而兼容 agent 的 async 调用链。
    命中缓存直接返回,未命中则计算并写入缓存。
    """
    # 一级缓存:内存 dict (只在本地进程有效)
    from app.rag.cache import get_embedding_cache

    cache = get_embedding_cache()
    imported_hashes = [hash(t) for t in texts]
    all_cached: list[Optional[list[float]]] = [None] * len(texts)
    miss_indices: list[int] = []
    for i, t in enumerate(texts):
        cached = cache.get(t)
        if cached is not None:
            all_cached[i] = cached
        else:
            miss_indices.append(i)
    if not miss_indices:
        return [v for v in all_cached if v is not None]

    # 计算 miss 部分
    miss_texts = [texts[i] for i in miss_indices]

    def _compute() -> list[list[float]]:
        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False
        if running:
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(embedder.embed_many, miss_texts).result(timeout=180)
        return embedder.embed_many(miss_texts)

    miss_vectors = _compute()
    # 回填缓存
    for i, v in zip(miss_indices, miss_vectors):
        cache.set(texts[i], v)
    # 合并结果
    result = list(all_cached)
    for idx, v in zip(miss_indices, miss_vectors):
        result[idx] = v
    return result


_embedder = None
_embedder_lock = threading.Lock()
# 进程级维度缓存:避免每次检索都读写磁盘
_dim_cache: dict[str, int] = {}


def get_embedder() -> Embedder:
    """工厂:按配置返回嵌入器(单例)。"""
    global _embedder
    if _embedder is not None:
        return _embedder
    with _embedder_lock:
        if _embedder is not None:
            return _embedder
        backend = (settings.embedding_backend or "local").lower()
        if backend in ("api", "openai"):
            base, key, model = _embedding_endpoint()
            _embedder = APIEmbeddings(base_url=base, api_key=key, model=model)
        else:
            _embedder = HashEmbedding()
        return _embedder


def _get_cached_dim(model_key: str) -> int:
    """获取已缓存的嵌入维度,未命中返回 0。"""
    return _dim_cache.get(model_key, 0)


def _set_cached_dim(model_key: str, dim: int) -> None:
    """进程级维度缓存,避免每次读写磁盘。"""
    _dim_cache[model_key] = dim
