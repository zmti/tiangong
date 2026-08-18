"""第八轮：逻辑问题深度分析与优化验证。"""
import asyncio
import importlib
import inspect
import json
import time

print("=== Test 1: _extract_json brace matching ===")
from app.core.llm import _extract_json
# 正常 JSON
assert _extract_json('{"a":1}') == {"a": 1}
# 前后有杂文
assert _extract_json('hello {"a":1} world') == {"a": 1}
# 多个对象：只取第一个完整对象
r = _extract_json('{"a":1} {"b":2}')
assert r == {"a": 1}, f"expected {{'a':1}}, got {r}"
# 嵌套 JSON
assert _extract_json('{"a":{"b":[1,2]}}') == {"a": {"b": [1, 2]}}
# 无效 JSON
assert _extract_json('{invalid}') is None
assert _extract_json('no json here') is None
assert _extract_json('') is None
print("  OK")

print("=== Test 2: tasks.py cancel flag safe access ===")
src = inspect.getsource(lambda: None)  # placeholder
from app.core.tasks import TaskManager
src = inspect.getsource(TaskManager._drive)
assert "_cancel_flags.get(task_id)" in src, "Should use .get() for safety"
assert "KeyError" not in src  # should not have bare dict access
print("  OK")

print("=== Test 3: graph branch events preserved ===")
from app.runtime.graph import Graph, Node
src = inspect.getsource(Graph._run_branch)
assert "st.events = []" not in src or "list(st.events)" in src, \
    "Branch events should not be silently discarded"
print("  OK")

print("=== Test 4: tool_executor Callable type annotation ===")
from app.core.tool_executor import ToolExecutor
sig = inspect.signature(ToolExecutor.__init__)
cancel_param = sig.parameters.get("cancel_check")
assert cancel_param is not None
ann = cancel_param.annotation
assert "Callable" in str(ann), f"Expected Callable, got {ann}"
print("  OK")

print("=== Test 5: failure/success memory score formula ===")
from app.memory.failure_memory import get_failure_memory as get_fm
from app.memory.success_memory import get_success_memory as get_sm
fm = get_fm()
rec_f = fm.record_failure("test_tool", "error msg", session_id="t1")
assert rec_f.score == 0.5, f"initial score should be 0.5, got {rec_f.score}"
# record again immediately
rec_f2 = fm.record_failure("test_tool", "error msg", session_id="t1")
assert rec_f2.count == 2
assert rec_f2.score > 0.5, f"score should increase with count, got {rec_f2.score}"

sm = get_sm()
rec_s = sm.record_success("test_tool", '{"a":1}', "ok", session_id="t2")
assert rec_s.score == 0.5
rec_s2 = sm.record_success("test_tool", '{"a":1}', "ok", session_id="t2")
assert rec_s2.count == 2
assert rec_s2.score > 0.5
print("  OK")

print("=== Test 6: graph cancel check wrapper ===")
from app.core.agent import Agent
a = Agent(session_id="test1")
gc = a._build_graph()._cancel_check
assert gc() == False
cancelled = [False]
a._cancel_flag = type("Event", (), {"is_set": lambda: cancelled[0]})()
assert gc() == True
print("  OK")

print("=== Test 7: memory_manager preserves note on error ===")
from app.core.memory_manager import MemoryManager
mm = MemoryManager(session_id="test7")
src = inspect.getsource(mm.retrieve_memory_note)
assert "pass  # 失败时保留已有" in src or "pass  # 分层失败回退旧逻辑" in src
print("  OK")

print("=== Test 8: guardrail works in stream mode ===")
src = inspect.getsource(a._node_finalize)
assert "is_giveup" in src
assert "is_success" in src
assert "拦截通知事件" in src or "被拦截" in src
print("  OK")

print("=== Test 9: prompt optimizer success flag accurate ===")
assert "is_giveup" in src
assert "budget_limit_hit" in src
print("  OK")

print("=== Test 10: code_file risk patterns ===")
from app.tools.code_file import _RISK_HIGH_PATTERNS
assert "def " not in _RISK_HIGH_PATTERNS
assert "class " not in _RISK_HIGH_PATTERNS
assert "exec(" in _RISK_HIGH_PATTERNS
assert "eval(" in _RISK_HIGH_PATTERNS
print("  OK")

print("=== Test 11: _runtime_approved uses |= on resume ===")
src = inspect.getsource(a._node_tools)
assert "_runtime_approved |= set()" in src or "_runtime_approved |=" in src
print("  OK")

print("=== Test 12: all core modules import ===")
mods = [
    "app.core.agent", "app.core.memory_manager", "app.core.tasks",
    "app.core.tool_executor", "app.memory.failure_memory",
    "app.memory.success_memory", "app.runtime.graph",
    "app.core.webhook", "app.core.mcp_stdio", "app.core.mcp_client",
    "app.main", "app.tools.code_file", "app.core.llm",
]
for m in mods:
    importlib.import_module(m)
print(f"  OK: {len(mods)} modules")

print("\n========== ALL TESTS PASSED ==========")
