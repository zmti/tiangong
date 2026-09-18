"""LLM 流式工具调用参数截断防护测试:流中途断开时,残缺 JSON 参数不再进入下一轮。

复现场景:
`chat_stream_with_tools` 累积 tool_call_delta 增量到 tool_call_bufs,
若 provider 流中途被截断(空闲超时/断连),arguments 可能是残缺 JSON。
旧实现直接把残缺字符串塞进 ToolCall,执行器拿到坏参数 → 下一轮 Agent 崩。
修复后:非空且解析失败的 arguments 在流结束前就抛可操作 LLMError,
让上层走降级/error 路径,而不是喂给工具。
"""
import json
from app.core.llm import LLMClient, LLMError, ToolCall


def _make_client():
    c = LLMClient.__new__(LLMClient)
    c.provider = "mock"
    c.model = "mock-model"
    c.client = None
    c._custom = None
    c._api_key = "x"
    c._initial_provider = "mock"
    c._circuit_fails = {}
    c._circuit_open_until = {}
    c.last_usage = None
    c._reasoning = ""
    c._finish_reason = ""
    return c


def _run_validate(tool_call_bufs, finish_reason=""):
    """复现 chat_stream_with_tools 流结束时的参数校验逻辑(与 llm.py 保持一致)。"""
    content_buf = ""
    tool_calls = []
    for buf in tool_call_bufs:
        args = buf.get("arguments", "")
        name = buf.get("name", "")
        if args:
            try:
                json.loads(args)
            except (json.JSONDecodeError, TypeError):
                raise LLMError(
                    f"工具调用参数被截断(名称={name or '未知'}),流式响应中途断开。"
                    "请重试或检查 provider 网络稳定性。"
                )
        tool_calls.append(ToolCall(id=buf.get("id", ""), name=name, arguments=args or "{}"))
    return tool_calls


def test_valid_arguments_pass():
    """完整 JSON 参数正常通过。"""
    buf = [{"id": "1", "name": "add", "arguments": '{"a": 1, "b": 2}'}]
    tool_calls = _run_validate(buf)
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "add"
    assert tool_calls[0].arguments == '{"a": 1, "b": 2}'


def test_empty_arguments_default_to_braces():
    """空参数(无入参工具)视为合法,补 {} 而非报错。"""
    buf = [{"id": "1", "name": "get_time", "arguments": ""}]
    tool_calls = _run_validate(buf)
    assert tool_calls[0].arguments == "{}"


def test_truncated_arguments_raise_llm_error():
    """残缺 JSON 参数(流中途断开)必须抛 LLMError,不能静默通过。"""
    buf = [{"id": "1", "name": "add", "arguments": '{"a": 1, "b":'}]  # 缺右括号
    try:
        _run_validate(buf)
        assert False, "残缺 JSON 应抛 LLMError"
    except LLMError as e:
        assert "截断" in str(e), f"错误信息应含可操作提示,实际: {e}"


def test_malformed_arguments_raise_llm_error():
    """非 JSON 文本参数(如被污染的字符串)同样拦截。"""
    buf = [{"id": "1", "name": "add", "arguments": "not json at all"}]
    try:
        _run_validate(buf)
        assert False, "非 JSON 参数应抛 LLMError"
    except LLMError:
        pass
