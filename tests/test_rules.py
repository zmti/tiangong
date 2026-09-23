"""智能体规则 API + 注入 + 护栏 测试。

覆盖:
- 规则集/规则 CRUD(建集/加规则/开关/改名/删集)
- /v1/rules/active 分桶(instructions / blocked_tools / blocked_terms / blocked_paths,含 warn 桶)
- get_active_instructions 过滤/全量/空
- Agent 硬规则:工具拦截 + 输入/输出护栏
- 无规则回归:行为不变
"""
import asyncio
import json

import pytest

from app.core.storage_backend import InMemoryBackend, reset_storage_backend
from app.core.guardrails import run_input_guardrails, run_output_guardrails


@pytest.fixture(autouse=True)
def _fresh_storage(monkeypatch):
    """每个用例用独立内存后端,避免跨用例污染。"""
    reset_storage_backend()
    yield
    reset_storage_backend()


@pytest.fixture
def store(monkeypatch):
    """让 app.api.rules 的模块级 _db 指向内存后端,并初始化 schema。"""
    import app.api.rules as rules_mod

    backend = InMemoryBackend()
    backend.init_db({
        "rules": [
            {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
            {"name": "set_id", "type": "TEXT", "constraints": "NOT NULL"},
            {"name": "set_name", "type": "TEXT", "constraints": "NOT NULL"},
            {"name": "name", "type": "TEXT", "constraints": "NOT NULL"},
            {"name": "type", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'instruction'"},
            {"name": "content", "type": "TEXT", "constraints": "NOT NULL DEFAULT ''"},
            {"name": "enabled", "type": "INTEGER", "constraints": "NOT NULL DEFAULT 1"},
            {"name": "action", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'block'"},
            {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
            {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
        ],
    })
    monkeypatch.setattr(rules_mod, "_db", backend)
    return rules_mod


# ---- CRUD ----

def test_create_set_and_list(store):
    res = asyncio.run(store.create_rule_set(store.SetCreate(name="代码规则")))
    assert res["set_name"] == "代码规则"
    lst = asyncio.run(store.list_rule_sets())
    assert lst["total"] == 1
    assert lst["sets"][0]["set_id"] == res["set_id"]


def test_add_rule_and_view(store):
    res = asyncio.run(store.create_rule_set(store.SetCreate(name="S")))
    sid = res["set_id"]
    r = asyncio.run(store.add_rule(
        sid,
        store.RuleCreate(set_id=sid, set_name="S", name="中文",
                         type="instruction", content="回答用中文", enabled=True)))
    assert r["name"] == "中文"
    assert r["type"] == "instruction"
    assert r["enabled"] is True


def test_update_toggle_and_content(store):
    res = asyncio.run(store.create_rule_set(store.SetCreate(name="S")))
    sid = res["set_id"]
    r = asyncio.run(store.add_rule(sid, store.RuleCreate(
        set_id=sid, set_name="S", name="R", type="term_block",
        content="机密,内部", enabled=True)))
    rid = r["id"]
    # 开关
    u1 = asyncio.run(store.update_rule(rid, store.RuleUpdate(enabled=False)))
    assert u1["enabled"] is False
    # 内容
    u2 = asyncio.run(store.update_rule(rid, store.RuleUpdate(content="x, y", enabled=True)))
    assert u2["content"] == "x, y"
    assert u2["enabled"] is True


def test_rename_set_updates_all_rows(store):
    res = asyncio.run(store.create_rule_set(store.SetCreate(name="旧名")))
    sid = res["set_id"]
    asyncio.run(store.add_rule(sid, store.RuleCreate(set_id=sid, set_name="旧名",
                                                      name="R", content="a")))
    asyncio.run(store.rename_rule_set(sid, store.SetRename(name="新名")))
    lst = asyncio.run(store.list_rule_sets())
    assert lst["sets"][0]["set_name"] == "新名"
    assert lst["sets"][0]["rules"][0]["set_name"] == "新名"


def test_delete_set_removes_rules(store):
    res = asyncio.run(store.create_rule_set(store.SetCreate(name="S")))
    sid = res["set_id"]
    asyncio.run(store.add_rule(sid, store.RuleCreate(set_id=sid, set_name="S",
                                                      name="R", content="a")))
    asyncio.run(store.delete_rule_set(sid))
    lst = asyncio.run(store.list_rule_sets())
    assert lst["total"] == 0


def test_add_rule_unknown_set_404(store):
    import pytest as _pt
    with _pt.raises(Exception):
        asyncio.run(store.add_rule(
            "rs_missing", store.RuleCreate(set_id="rs_missing", set_name="X", name="R")))


# ---- active 分桶 ----

def test_active_buckets_and_dedupe(store):
    asyncio.run(store.create_rule_set(store.SetCreate(name="S")))
    res = asyncio.run(store.list_rule_sets())
    sid = res["sets"][0]["set_id"]
    # 软指令
    asyncio.run(store.add_rule(sid, store.RuleCreate(set_id=sid, set_name="S",
        name="中文", type="instruction", content="回答必须用简体中文", enabled=True)))
    # 禁工具(逗号分隔)
    asyncio.run(store.add_rule(sid, store.RuleCreate(set_id=sid, set_name="S",
        name="禁终端", type="tool_block", content="run_terminal, delete_email", enabled=True)))
    # 禁词
    asyncio.run(store.add_rule(sid, store.RuleCreate(set_id=sid, set_name="S",
        name="禁词", type="term_block", content="机密,内部代号", enabled=True)))
    # 禁路径
    asyncio.run(store.add_rule(sid, store.RuleCreate(set_id=sid, set_name="S",
        name="禁路径", type="path_block", content="C:\\secrets, /etc/passwd", enabled=True)))

    act = store.get_active_rules(None)
    assert act["instructions"][0]["content"] == "回答必须用简体中文"
    assert "run_terminal" in act["blocked_tools"]
    assert "delete_email" in act["blocked_tools"]
    assert "机密" in act["blocked_terms"]
    assert "C:\\secrets" in act["blocked_paths"]
    # warn 桶默认为空(action=block)
    assert act["warn_terms"] == []
    assert act["warn_paths"] == []


def test_disabled_rule_excluded_from_active(store):
    asyncio.run(store.create_rule_set(store.SetCreate(name="S")))
    sid = asyncio.run(store.list_rule_sets())["sets"][0]["set_id"]
    r = asyncio.run(store.add_rule(sid, store.RuleCreate(set_id=sid, set_name="S",
        name="禁", type="tool_block", content="run_code", enabled=True)))
    # 关闭
    asyncio.run(store.update_rule(r["id"], store.RuleUpdate(enabled=False)))
    act = store.get_active_rules(None)
    assert "run_code" not in act["blocked_tools"]


def test_warn_action_bucket(store):
    asyncio.run(store.create_rule_set(store.SetCreate(name="S")))
    sid = asyncio.run(store.list_rule_sets())["sets"][0]["set_id"]
    asyncio.run(store.add_rule(sid, store.RuleCreate(set_id=sid, set_name="S",
        name="提示词", type="term_block", content="foo", enabled=True, action="warn")))
    act = store.get_active_rules(None)
    assert "foo" in act["warn_terms"]
    assert "foo" not in act["blocked_terms"]


def test_get_active_instructions_empty(store):
    assert store.get_active_instructions(None) == ""


def test_get_active_instructions_full_and_filtered(store):
    asyncio.run(store.create_rule_set(store.SetCreate(name="S")))
    sid = asyncio.run(store.list_rule_sets())["sets"][0]["set_id"]
    r1 = asyncio.run(store.add_rule(sid, store.RuleCreate(set_id=sid, set_name="S",
        name="A", type="instruction", content="指令一", enabled=True)))
    r2 = asyncio.run(store.add_rule(sid, store.RuleCreate(set_id=sid, set_name="S",
        name="B", type="instruction", content="指令二", enabled=True)))
    full = store.get_active_instructions(None)
    assert "指令一" in full and "指令二" in full
    # 仅取 r1
    only = store.get_active_instructions([r1["id"]])
    assert "指令一" in only and "指令二" not in only


# ---- Agent 硬规则集成 ----

def _make_agent(store, **kwargs):
    from app.core.agent import Agent
    from app.core.llm import LLMClient
    from app.tools.registry import ToolRegistry

    llm = LLMClient.__new__(LLMClient)  # 不触发真实 provider 构造
    llm.provider = "mock"
    llm.model = "mock"
    agent = Agent(llm=llm, registry=ToolRegistry(), max_steps=5, **kwargs)
    agent._checkpoint = None
    return agent


def test_agent_tool_blocked_by_rule(store):
    asyncio.run(store.create_rule_set(store.SetCreate(name="S")))
    sid = asyncio.run(store.list_rule_sets())["sets"][0]["set_id"]
    asyncio.run(store.add_rule(sid, store.RuleCreate(set_id=sid, set_name="S",
        name="禁工具", type="tool_block", content="run_code", enabled=True)))
    act = store.get_active_rules(None)

    agent = _make_agent(store, rule_blocked_tools=act["blocked_tools"])
    out = asyncio.run(agent._execute_tool_once("run_code", "{}"))
    data = json.loads(out)
    assert data.get("blocked") is True
    assert "禁用" in data["error"]


def test_agent_unblocked_tool_not_affected(store):
    # 仅禁 run_code,run_terminal 不受影响 → 走正常执行(未知工具则报"未知工具")
    agent = _make_agent(store, rule_blocked_tools=["run_code"])
    out = asyncio.run(agent._execute_tool_once("some_unknown_tool", "{}"))
    data = json.loads(out)
    assert "run_code" not in json.dumps(out)
    # 未禁的工具名不出现"用户规则禁用"文案
    assert "已被用户规则禁用" not in data.get("error", "")


def test_agent_no_rules_behavior_unchanged(store):
    """无规则时:_rule_blocked_tools 空 set,工具拦截分支不触发。"""
    agent = _make_agent(store)
    assert agent._rule_blocked_tools == set()
    assert agent._rule_terms is None
    assert agent._rule_paths is None
    # 输入护栏不传 user_terms 时与旧版一致
    report = run_input_guardrails("今天天气怎么样")
    assert not report.blocked


def test_agent_system_prompt_injects_blocked_tools_note(store):
    """有规则禁用工具时,系统提示须事实声明被禁工具,避免 LLM 凭空声称'被禁用'。"""
    agent = _make_agent(store, rule_blocked_tools=["run_terminal", "run_code"])
    assert "[工具禁用状态·实时]" in agent.system_prompt
    assert "run_terminal" in agent.system_prompt
    assert "run_code" in agent.system_prompt
    assert "已被用户规则禁用" not in agent.system_prompt  # 提示里是事实声明,不是拒绝文案


def test_agent_system_prompt_injects_no_block_when_empty(store):
    """无禁用工具时,系统提示须显式声明'全部可用',并禁止编造禁用说法。"""
    agent = _make_agent(store)
    assert "[工具禁用状态·实时]" in agent.system_prompt
    assert "没有任何工具被用户规则禁用" in agent.system_prompt
    assert "不要向用户谎称" in agent.system_prompt


def test_build_rule_blocked_note_static():
    """_build_rule_blocked_note 直接覆盖两种分支。"""
    from app.core.agent import Agent
    blocked = Agent._build_rule_blocked_note(["run_terminal", "run_code"])
    assert "run_terminal" in blocked and "run_code" in blocked
    empty = Agent._build_rule_blocked_note(None)
    assert "没有任何工具被用户规则禁用" in empty
    # 去重 + 排序
    assert Agent._build_rule_blocked_note(["b", "a", "a"]) == Agent._build_rule_blocked_note(["a", "b"])


def test_agent_rule_terms_input_guardrail(store):
    agent = _make_agent(store, rule_terms=["机密"], rule_paths=["C:\\secrets"])
    agent._rule_terms_action = "block"
    report = run_input_guardrails("泄露机密信息", user_terms=agent._rule_terms,
                                   user_paths=agent._rule_paths,
                                   user_terms_action=agent._rule_terms_action)
    assert report.blocked
    assert any(v.name == "user_term_block" for v in report.violations)


def test_agent_rule_terms_output_guardrail(store):
    agent = _make_agent(store, rule_terms=["内部代号"])
    report = run_output_guardrails("输出含内部代号", user_terms=agent._rule_terms)
    assert report.blocked
