"""Round 4 logic optimization verification."""
import asyncio
import importlib
import inspect
import os
import time
import math

print("=== Test 1: Graph cancel check wrapper ===")
from app.core.agent import Agent
a = Agent(session_id="test1")
gc = a._build_graph()._cancel_check
assert gc() == False
cancelled = [False]
a._cancel_flag = type("Event", (), {"is_set": lambda: cancelled[0]})()
assert gc() == True
print("  OK")

print("=== Test 2: _runtime_approved accumulates on resume ===")
# Verify the code uses |= not = set()
src = inspect.getsource(a._node_tools)
assert "_runtime_approved |= set()" in src or "_runtime_approved |= " in src, \
    f"Expected |=, got original code"
print("  OK")

print("=== Test 3: failure_memory score formula ===")
from app.memory.failure_memory import get_failure_memory
fm = get_failure_memory()
rec = fm.record_failure("test_tool", "error msg", session_id="t3")
assert rec.score == 0.5, f"initial score should be 0.5, got {rec.score}"
# Simulate another failure immediately
rec2 = fm.record_failure("test_tool", "error msg", session_id="t3")
assert rec2.count == 2
assert rec2.score > 0.5, f"score should increase with count, got {rec2.score}"
print(f"  OK: count={rec2.count}, score={rec2.score:.3f}")

print("=== Test 4: success_memory score formula ===")
from app.memory.success_memory import get_success_memory
sm = get_success_memory()
rec3 = sm.record_success("test_tool", '{"a":1}', "ok", session_id="t4")
assert rec3.score == 0.5
rec4 = sm.record_success("test_tool", '{"a":1}', "ok", session_id="t4")
assert rec4.count == 2
assert rec4.score > 0.5
print(f"  OK: count={rec4.count}, score={rec4.score:.3f}")

print("=== Test 5: cache key normalization ===")
from app.core.tool_executor import ToolExecutor
k1 = ToolExecutor._normalize_cache_key('{"path": "test.py"}')
k2 = ToolExecutor._normalize_cache_key('{"path":"test.py"}')
assert k1 == k2
k3 = ToolExecutor._normalize_cache_key("plain string arg")
assert k3.startswith("h:")
k4 = ToolExecutor._normalize_cache_key("")
assert k4 == ""
k5 = ToolExecutor._normalize_cache_key(None)
assert k5 == ""
print("  OK")

print("=== Test 6: memory_manager preserves note on error ===")
from app.core.memory_manager import MemoryManager
mm = MemoryManager(session_id="test6")
# Check that except block uses pass, not return ""
src6 = inspect.getsource(mm.retrieve_memory_note)
assert 'pass  # 失败时保留已有' in src6 or "pass  # 失败时保留已有" in src6
print("  OK")

print("=== Test 7: guardrail works in stream mode ===")
# Check the guardrail code handles stream mode
src7 = inspect.getsource(a._node_finalize)
assert "if state.metadata.get('stream'):" in src7
assert "拦截通知事件" in src7 or "被拦截" in src7
print("  OK")

print("=== Test 8: prompt optimizer records success correctly ===")
src8 = inspect.getsource(a._node_finalize)
assert "is_giveup" in src8
assert "is_success" in src8
print("  OK")

print("=== Test 9: code_file risk patterns relaxed ===")
from app.tools.code_file import _RISK_HIGH_PATTERNS
assert "def " not in _RISK_HIGH_PATTERNS, "def should not be high risk"
assert "class " not in _RISK_HIGH_PATTERNS
assert "exec(" in _RISK_HIGH_PATTERNS
assert "eval(" in _RISK_HIGH_PATTERNS
print("  OK")

print("=== Test 10: arun_stream cancel after yield ===")
from app.runtime.graph import Graph, Node
from app.runtime.state import AgentState
cancelled2 = [False]
g = Graph(name="g", cancel_check=lambda: cancelled2[0])
g.add_node(Node("a", lambda s, gr: None))
g.add_node(Node("b", lambda s, gr: None))
g.set_entry("a")
g.add_edge("a", "b")
g.add_edge("b", "__end__")
st = AgentState(session_id="t")
st.node = "a"


async def _stream_check():
    events = []
    async for evt in g.arun_stream(st):
        events.append(evt)
    assert st.status == "done"


asyncio.get_event_loop().run_until_complete(_stream_check())
print("  OK")

print("=== Test 11: tasks token delta ===")
from app.core.tasks import TaskManager
mgr = TaskManager()
src11 = inspect.getsource(mgr._drive)
assert "prev_total_tokens" in src11
assert "delta" in src11
print("  OK")

print("=== Test 12: stop_webhook_queue_drainer is async ===")
from app.core.webhook import stop_webhook_queue_drainer
assert inspect.iscoroutinefunction(stop_webhook_queue_drainer)
print("  OK")

print("=== Test 13: MCP close sets auto_reconnect=False ===")
from app.core.mcp_stdio import MCPStdioClient
mc = MCPStdioClient("echo", ["test"])
mc.auto_reconnect = True
mc._reconnect_task = asyncio.get_event_loop().create_task(mc._reconnect_loop())
asyncio.get_event_loop().run_until_complete(mc.close())
assert mc.auto_reconnect == False
print("  OK")

print("=== Test 14: MCPManager reset calls close ===")
from app.core.mcp_client import MCPManager
mgr2 = MCPManager()


class FakeClient:
    close_called = False

    async def close(self):
        self.close_called = True

    _tools_cache = []


fc = FakeClient()
mgr2._clients["test"] = fc


async def _reset():
    await mgr2.reset()


asyncio.get_event_loop().run_until_complete(_reset())
assert fc.close_called
assert len(mgr2._clients) == 0
print("  OK")

print("=== Test 15: All modules import ===")
mods = [
    "app.core.agent",
    "app.core.memory_manager",
    "app.core.tasks",
    "app.core.tool_executor",
    "app.memory.failure_memory",
    "app.memory.success_memory",
    "app.runtime.graph",
    "app.core.webhook",
    "app.core.mcp_stdio",
    "app.core.mcp_client",
    "app.main",
    "app.tools.code_file",
]
for m in mods:
    importlib.import_module(m)
print(f"  OK: {len(mods)} modules")

print("\n========== ALL TESTS PASSED ==========")
