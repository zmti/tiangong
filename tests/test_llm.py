"""LLM 与配置测试。"""
import asyncio

import pytest

from app.config import settings
from app.core.llm import LLMClient, LLMError, _extract_json, build_llm


def test_llm_build_deepseek():
    client = build_llm("deepseek")
    assert client.provider == "deepseek"
    assert client.model == settings.deepseek_model


def test_llm_build_ollama():
    client = build_llm("ollama")
    assert client.provider == "ollama"
    assert client.model == settings.ollama_model


def test_llm_build_qwen():
    client = build_llm("qwen")
    assert client.provider == "qwen"
    assert client.model == settings.qwen_model


# ---- 可靠性:瞬态错误识别 / 重试 / 故障转移 ----

def test_retryable_marker_matching():
    assert LLMClient._is_retryable("Rate limit exceeded for org") is True
    assert LLMClient._is_retryable("connection reset by peer") is True
    assert LLMClient._is_retryable("HTTP 503 Service Unavailable") is True
    assert LLMClient._is_retryable("timed out after 30s") is True
    # 非瞬态错误不可重试
    assert LLMClient._is_retryable("401 invalid api key") is False
    assert LLMClient._is_retryable("model not found: gpt-x") is False


def test_extract_json_handles_fences():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('回复是 {"a": 1} 这样') == {"a": 1}
    assert _extract_json("不是 JSON") is None


class _FakeResp:
    def __init__(self, content="", usage=None, reasoning="", tool_calls=None, finish="stop"):
        self.choices = [_FakeChoice(content, reasoning, tool_calls, finish)]
        self.usage = usage or {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


class _FakeChoice:
    def __init__(self, content, reasoning, tool_calls, finish):
        self.message = _FakeMsg(content, reasoning, tool_calls)
        self.finish_reason = finish


class _FakeMsg:
    def __init__(self, content, reasoning, tool_calls):
        self.content = content
        self.reasoning_content = reasoning
        self.tool_calls = tool_calls or []


class _Completions:
    def __init__(self, inner):
        self._inner = inner

    async def create(self, **kw):
        return await self._inner(**kw)


class _Chat:
    def __init__(self, inner):
        self.completions = _Completions(inner)


def _client_wrapper(inner):
    """把 async def f(**kw) 包装成带 .chat.completions.create 的假客户端。"""
    return type("FakeOpenAI", (), {"chat": _Chat(inner)})()


class _FlakyClient:
    """前 N 次抛瞬态错误,之后成功。"""

    def __init__(self, fail_times=2):
        self.calls = 0
        self.fail_times = fail_times
        self.used_model = None

    async def chat_completions(self, **kw):
        self.calls += 1
        self.used_model = kw.get("model")
        if self.calls <= self.fail_times:
            raise TimeoutError("request timed out (network)")
        return _FakeResp(content="ok")


def test_chat_retry_on_transient_error(monkeypatch):
    flaky = _FlakyClient(fail_times=1)
    client = build_llm("deepseek")
    client._api_key = "test-key"
    monkeypatch.setattr(settings, "agent_llm_retries", 2)
    monkeypatch.setattr(settings, "agent_llm_retry_delay", 0.01)
    client.client = _client_wrapper(flaky.chat_completions)

    async def _run():
        return await client.chat([{"role": "user", "content": "hi"}])

    assert asyncio.run(_run()) == "ok"
    assert flaky.calls == 2  # 1 次失败 + 1 次成功


def test_chat_no_retry_on_hard_error(monkeypatch):
    class _HardClient:
        def __init__(self):
            self.calls = 0

        async def chat_completions(self, **kw):
            self.calls += 1
            raise ValueError("401 invalid api key")

    hard = _HardClient()
    client = build_llm("deepseek")
    client._api_key = "test-key"
    monkeypatch.setattr(settings, "agent_llm_retries", 3)
    client.client = _client_wrapper(hard.chat_completions)

    from app.core.llm import LLMError

    with pytest.raises(LLMError):
        asyncio.run(client.chat([{"role": "user", "content": "hi"}]))
    assert hard.calls == 1  # 非瞬态错误不重试


def test_chat_failover_to_backup(monkeypatch):
    """主 provider 持续失败时切换到备用 provider。"""
    class _BadClient:
        async def chat_completions(self, **kw):
            raise ConnectionError("connection refused")

    class _GoodClient:
        async def chat_completions(self, **kw):
            return _FakeResp(content="backup-ok")

    client = build_llm("deepseek")
    client._api_key = "test-key"
    monkeypatch.setattr(settings, "agent_llm_retries", 0)
    monkeypatch.setattr(settings, "agent_llm_failover_provider", "qwen")
    client.client = _client_wrapper(_BadClient().chat_completions)  # 主 provider 失败
    # 备用 provider 通过 _make_client 构建
    monkeypatch.setattr(client, "_make_client", lambda p, m: (_client_wrapper(_GoodClient().chat_completions), "m"))

    assert asyncio.run(client.chat([{"role": "user", "content": "hi"}])) == "backup-ok"


# ---- Reasoning 模型适配 ----

def test_extract_reasoning_separated():
    client = build_llm("deepseek")
    client.model = "deepseek-reasoner"
    msg = _FakeMsg(content="final answer", reasoning="thinking...", tool_calls=None)
    assert client._extract_reasoning(msg) == "thinking..."


# ---- 严格结构化输出 ----

def test_chat_json_schema_validation_retry(monkeypatch):
    """输出不符 schema 时回灌错误重试,最终返回合法结果。"""
    outputs = [
        _FakeResp(content='{"age": "not-a-number"}'),  # 校验失败
        _FakeResp(content='{"age": 30}'),  # 校验通过
    ]

    class _SeqClient:
        def __init__(self):
            self.outputs = list(outputs)

        async def chat_completions(self, **kw):
            return self.outputs.pop(0)

    seq = _SeqClient()
    client = build_llm("deepseek")
    client._api_key = "test-key"
    monkeypatch.setattr(settings, "agent_llm_retries", 0)
    client.client = _client_wrapper(seq.chat_completions)

    schema = {"type": "object", "properties": {"age": {"type": "integer"}}, "required": ["age"]}

    def _validator(data):
        if not isinstance(data.get("age"), int):
            return False, "age 必须是整数"
        return True, None

    async def _run():
        return await client.chat_json_schema([{"role": "user", "content": "x"}], schema, validator=_validator)

    assert asyncio.run(_run()) == {"age": 30}
    assert seq.outputs == []  # 两次调用都消耗


# ---- Prompt cache 观测 ----

class _Usage:
    def __init__(self, prompt, completion, total, cache_hit=0, cached_tokens=None):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total
        # DeepSeek 风格
        self.prompt_cache_hit_tokens = cache_hit
        # OpenAI 风格
        self.prompt_tokens_details = type("D", (), {"cached_tokens": cached_tokens})() if cached_tokens is not None else None


def test_usage_of_extracts_deepseek_cache_hit():
    resp = _FakeResp(usage=_Usage(prompt=100, completion=20, total=120, cache_hit=60))
    u = LLMClient._usage_of(resp)
    assert u["cache_hit_tokens"] == 60
    assert u["cache_miss_tokens"] == 40
    assert u["prompt_tokens"] == 100


def test_usage_of_extracts_openai_cached_tokens():
    resp = _FakeResp(usage=_Usage(prompt=200, completion=30, total=230, cached_tokens=150))
    u = LLMClient._usage_of(resp)
    assert u["cache_hit_tokens"] == 150
    assert u["cache_miss_tokens"] == 50


def test_usage_of_no_cache_fields():
    resp = _FakeResp(usage=_Usage(prompt=10, completion=5, total=15))
    u = LLMClient._usage_of(resp)
    assert u["cache_hit_tokens"] == 0
    assert u["cache_miss_tokens"] == 10


def test_stats_aggregates_cache_hit_rate():
    from app.core.telemetry import tracer

    tracer._buffer.clear()
    tracer.record({
        "type": "llm_call", "provider": "deepseek", "model": "m",
        "prompt_tokens": 100, "completion_tokens": 20, "cache_hit_tokens": 40,
        "cache_hit_rate": 0.4, "cost": 0.001, "ok": True,
    })
    s = tracer.stats()
    assert s["cache_hit_tokens"] == 40
    assert s["cache_hit_rate"] == 0.4
    assert s["prompt_tokens"] == 100


# ---- Provider 熔断 ----

def test_circuit_breaker_skips_failing_provider(monkeypatch):
    """连续失败达到阈值后,后续调用直接跳过该 provider(不再发起请求)。"""
    LLMClient._circuit_fails.clear()
    LLMClient._circuit_open_until.clear()
    client = build_llm("deepseek")
    client._api_key = "test-key"
    monkeypatch.setattr(settings, "agent_llm_retries", 0)
    monkeypatch.setattr(settings, "agent_llm_circuit_threshold", 2)
    monkeypatch.setattr(settings, "agent_llm_circuit_cooldown", 60)

    calls = {"n": 0}

    class _AlwaysFail:
        async def chat_completions(self, **kw):
            calls["n"] += 1
            raise TimeoutError("connection reset")

    client.client = _client_wrapper(_AlwaysFail().chat_completions)

    # 前两次失败触发熔断
    for _ in range(2):
        with pytest.raises(LLMError):
            asyncio.run(client.chat([{"role": "user", "content": "x"}]))

    assert client._is_circuit_open("deepseek") is True

    # 熔断打开后,主 provider 被跳过:不再调用其客户端,直接失败(无备用则抛错)
    calls["n"] = 0
    with pytest.raises(LLMError):
        asyncio.run(client.chat([{"role": "user", "content": "x"}]))
    assert calls["n"] == 0  # 没有发起真实请求

    # 冷却结束(模拟时间流逝)后恢复:再次发起请求尝试该 provider
    client._circuit_open_until["deepseek"] = 0.0
    with pytest.raises(LLMError):
        asyncio.run(client.chat([{"role": "user", "content": "x"}]))
    assert calls["n"] == 1  # 恢复后确实重新尝试了一次
    LLMClient._circuit_fails.clear()
    LLMClient._circuit_open_until.clear()


def test_circuit_breaker_success_resets_counter(monkeypatch):
    LLMClient._circuit_fails.clear()
    LLMClient._circuit_open_until.clear()
    client = build_llm("deepseek")
    client._api_key = "test-key"
    monkeypatch.setattr(settings, "agent_llm_retries", 0)
    monkeypatch.setattr(settings, "agent_llm_circuit_threshold", 3)

    # 记录一次失败后,一次成功应清零
    client._record_failure("deepseek")
    client._record_failure("deepseek")
    assert client._circuit_fails["deepseek"] == 2
    client._record_success("deepseek")
    assert client._circuit_fails["deepseek"] == 0
    client._circuit_open_until["deepseek"] = 0.0
    client._circuit_fails["deepseek"] = 0
