"""工具调用 JSON 解析健壮性测试(验证 _parse_tool_calls 平衡扫描改进)。

覆盖场景:
- 参数值含嵌套 JSON 对象
- 参数值含字符串字面花括号/转义引号
- 多条工具调用
- 数组格式
"""
import json

from app.core.agent import Agent
from app.core.llm import ToolCall


def _build_array_text() -> str:
    """用 json.dumps 构造合法 JSON 数组, 避免手写转义出错。"""
    return json.dumps([
        {"tool": "read_file", "arguments": {"path": "a.py", "meta": {"encoding": "utf-8"}}},
        {"tool": "write_file", "arguments": {"path": "b.py", "content": "x = {'a': 1}", "meta": {"size": 10}}},
    ], ensure_ascii=False)


def test_parse_nested_json_arguments():
    """参数值含嵌套 JSON 对象时, 平衡扫描应正确提取完整 arguments。"""
    # LLM 输出: 单条工具调用, arguments 含嵌套 dict
    text = json.dumps({
        "tool": "run_code",
        "arguments": {
            "code": "def f(x): return x + 1",
            "use_docker": True,
            "env": {"PYTHONPATH": ""},
        },
    })
    calls = Agent._parse_tool_calls(text)
    assert len(calls) == 1
    parsed_args = json.loads(calls[0].arguments)
    assert parsed_args["code"] == "def f(x): return x + 1"
    assert parsed_args["env"] == {"PYTHONPATH": ""}


def test_parse_multiple_calls_with_nested_args():
    """多条工具调用, 参数均含嵌套 JSON, 平衡扫描应全部命中。"""
    text = _build_array_text()
    calls = Agent._parse_tool_calls(text)
    assert len(calls) == 2
    assert calls[0].name == "read_file"
    assert json.loads(calls[0].arguments)["meta"] == {"encoding": "utf-8"}
    assert calls[1].name == "write_file"
    assert json.loads(calls[1].arguments)["content"] == "x = {'a': 1}"


def test_parse_string_value_with_braces():
    """参数值字符串字面含花括号(如代码片段), 平衡扫描应正确处理引号内花括号。"""
    text = json.dumps({
        "tool": "edit_file",
        "arguments": {"old": "if True: pass", "new": 'if x: print({"k": 1})'},
    })
    calls = Agent._parse_tool_calls(text)
    assert len(calls) == 1
    parsed = json.loads(calls[0].arguments)
    assert parsed["new"] == 'if x: print({"k": 1})'


def test_parse_fallback_to_old_pattern():
    """非标准格式(平衡扫描失败)回退到原 TOOL_CALL_PATTERN 有限嵌套匹配。"""
    # 不含 "tool" 键的独立 JSON 对象, 走 _parse_tool_call 宽松解析
    text = json.dumps({"tool": "add", "arguments": {"a": 1}})
    calls = Agent._parse_tool_calls(text)
    assert len(calls) >= 1
    assert calls[0].name == "add"


def test_extract_balanced_json_basic():
    """_extract_balanced_json 直接测试: 简单/嵌套/字符串含花括号。"""
    # 简单对象
    assert Agent._extract_balanced_json('{"a":1}', 0) == '{"a":1}'
    # 嵌套
    assert Agent._extract_balanced_json('{"a":{"b":1}}', 0) == '{"a":{"b":1}}'
    # 字符串内含花括号
    assert Agent._extract_balanced_json('{"s":"{hi}"}', 0) == '{"s":"{hi}"}'
    # 字符串内含转义引号(\\\")+花括号: 用 json.dumps 保证输入本身是合法 JSON,
    # 验证转义目标不被误计入配平, 嵌套 { 仍在字符串内被正确忽略
    nested = json.dumps({"s": 'a"{b}', "t": 1})
    assert Agent._extract_balanced_json(nested, 0) == nested
    nested2 = json.dumps({"s": 'x = {\'a\': 1}', "n": 2})
    assert Agent._extract_balanced_json(nested2, 0) == nested2
    # 失败: 起点不是 {
    assert Agent._extract_balanced_json('abc', 0) is None
    # 失败: 未闭合
    assert Agent._extract_balanced_json('{"a":1', 0) is None


def test_parse_array_format_still_works():
    """JSON 数组格式(第一条路径)仍正常工作。"""
    text = '[{"tool": "add", "arguments": {"a": 1, "b": 2}}, {"tool": "calculate", "arguments": {"expr": "3+4"}}]'
    calls = Agent._parse_tool_calls(text)
    assert len(calls) == 2
    assert calls[0].name == "add"
    assert json.loads(calls[0].arguments) == {"a": 1, "b": 2}
    assert calls[1].name == "calculate"
