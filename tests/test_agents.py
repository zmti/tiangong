"""多智能体编排(角色/团队/handoff)测试。"""
import asyncio

from app.agents.handoff_tool import list_roles, get_team
from app.agents.roles import AgentRole, AgentTeam, ensure_default_roles
from app.tools.registry import ToolRegistry, tool


def run(coro):
    return asyncio.run(coro)


def test_default_roles_registered():
    ensure_default_roles()
    team = get_team()
    names = team.names()
    assert "researcher" in names
    assert "coder" in names
    assert "code_reviewer" in names


def test_list_roles_tool():
    out = list_roles.func()  # @tool 返回 Tool 对象,直接调底层函数
    assert out["roles"]
    assert any(r["name"] == "researcher" for r in out["roles"])


def test_team_custom_roles():
    team = AgentTeam([AgentRole(name="qa", system_prompt="你是测试工程师")])
    assert team.get("qa") is not None
    assert team.get("researcher") is None
    team.add(AgentRole(name="dev", system_prompt="你是开发"))
    assert "dev" in team.names()
    summary = team.summary()
    assert summary[0]["name"] == "qa"


def test_role_registry_override():
    from app.agents.roles import get_role_registry

    reg = get_role_registry()
    team = AgentTeam([AgentRole(name="special", system_prompt="x")])
    reg.set_team(team)
    try:
        assert reg.team.get("special") is not None
        assert reg.team.get("researcher") is None
    finally:
        reg.set_team(AgentTeam())  # 还原


def test_handoff_tool_unknown_role():
    from app.agents.handoff_tool import handoff_to_role

    out = run(handoff_to_role.func(role="no_such_role", instruction="hi"))
    assert out["status"] == "error"
    assert "未知角色" in out["error"]


# ---- 输出契约校验(expect JSON Schema) ----

def test_enforce_output_contract_no_expect():
    from app.tools.delegation_tool import enforce_output_contract

    ok, data, err = run(enforce_output_contract(None, "随便", ""))
    assert ok is True and data is None and err is None


def test_enforce_output_contract_invalid_schema():
    from app.tools.delegation_tool import enforce_output_contract

    ok, data, err = run(enforce_output_contract(None, "x", "not-json"))
    assert ok is False and err is not None


class _ContractLLM:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    async def chat_json_schema(self, messages, schema, temperature=0.2,
                               max_retries=2, validator=None):
        self.calls += 1
        return self._outputs.pop(0)


def test_enforce_output_contract_ok():
    from app.tools.delegation_tool import enforce_output_contract

    llm = _ContractLLM([{"name": "报告", "lines": 5}])
    schema = {"type": "object", "properties": {"name": {"type": "string"}, "lines": {"type": "integer"}},
              "required": ["name", "lines"]}
    ok, data, err = run(enforce_output_contract(llm, "汇报文本", schema))
    assert ok is True
    assert data == {"name": "报告", "lines": 5}
    assert llm.calls == 1


def test_enforce_output_contract_failure():
    from app.tools.delegation_tool import enforce_output_contract

    class _FailingLLM:
        async def chat_json_schema(self, messages, schema, **kw):
            raise RuntimeError("输出不满足 schema")

    ok, data, err = run(enforce_output_contract(_FailingLLM(), "汇报文本", {"type": "object"}))
    assert ok is False
    assert "不满足输出契约" in err


def test_delegate_task_with_expect_contract(monkeypatch):
    """delegate_task 带 expect 契约:满足时返回 answer_structured。"""
    from app.tools.delegation_tool import delegate_task, _delegate_depth, _delegate_parent
    from app.tools.delegation_tool import DelegateContext

    class _SubLLM:
        def __init__(self, *a, **k):
            self.provider = "sub"
            self.model = "sub-model"

        @property
        def info(self):
            return {"provider": self.provider, "model": self.model}

        async def chat(self, messages, temperature=0.7):
            return "子智能体完成,输出 3 行"

        async def chat_json_schema(self, messages, schema, **kw):
            return {"lines": 3}

    # 子智能体内部 from app.core.llm import LLMClient,替换为假实现
    monkeypatch.setattr("app.core.llm.LLMClient", _SubLLM)

    ctx = DelegateContext(provider="sub", model="sub-model", session_id="s1")
    tok_p = _delegate_parent.set(ctx)
    tok_d = _delegate_depth.set(0)
    try:
        out = run(delegate_task.func(instruction="写个报告", context="", tools="",
                                     max_steps=8, expect='{"type":"object"}'))
    finally:
        _delegate_parent.reset(tok_p)
        _delegate_depth.reset(tok_d)
    assert out["status"] == "ok"
    assert out["answer_structured"] == {"lines": 3}


def test_delegate_parallel_fans_out_tasks(monkeypatch):
    """delegate_parallel 并行扇出多个独立子任务并合并结果。"""
    import json

    from app.tools.delegation_tool import _delegate_depth, _delegate_parent, delegate_parallel
    from app.tools.delegation_tool import DelegateContext

    class _SubLLM:
        def __init__(self, *a, **k):
            self.provider = "sub"
            self.model = "sub-model"

        @property
        def info(self):
            return {"provider": self.provider, "model": self.model}

        async def chat(self, messages, temperature=0.7):
            return "子任务完成"

        async def chat_json_schema(self, messages, schema, **kw):
            return {"done": True}

    monkeypatch.setattr("app.core.llm.LLMClient", _SubLLM)

    ctx = DelegateContext(provider="sub", model="sub-model", session_id="s1")
    tok_p = _delegate_parent.set(ctx)
    tok_d = _delegate_depth.set(0)
    try:
        tasks = json.dumps([
            {"instruction": "调研话题A"},
            {"instruction": "调研话题B", "max_steps": 3},
        ])
        out = run(delegate_parallel.func(tasks=tasks, max_steps=8))
    finally:
        _delegate_parent.reset(tok_p)
        _delegate_depth.reset(tok_d)

    assert out["status"] == "ok"
    assert out["total"] == 2
    assert out["succeeded"] == 2
    assert len(out["results"]) == 2
    assert all(r["status"] == "ok" for r in out["results"])


def test_delegate_parallel_invalid_tasks():
    """tasks 不是合法 JSON 数组时返回错误。"""
    from app.tools.delegation_tool import delegate_parallel

    out = run(delegate_parallel.func(tasks="not-json", max_steps=5))
    assert "error" in out


def test_delegate_parallel_empty_tasks():
    from app.tools.delegation_tool import delegate_parallel

    out = run(delegate_parallel.func(tasks="[]", max_steps=5))
    assert "error" in out


def test_delegate_parallel_too_many_tasks():
    import json

    from app.tools.delegation_tool import delegate_parallel

    tasks = json.dumps([{"instruction": f"任务{i}"} for i in range(12)])
    out = run(delegate_parallel.func(tasks=tasks, max_steps=5))
    assert "error" in out
    assert "最多 10 项" in out["error"]
