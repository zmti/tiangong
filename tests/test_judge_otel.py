"""LLM-as-Judge 与 OTLP 导出测试(不依赖外部服务)。"""
import json


def test_judge_extract_json():
    from app.eval.judge import _extract_json

    assert _extract_json('{"task_success": 1}') == {"task_success": 1}
    assert _extract_json('前缀 {"correctness": 5} 后缀') == {"correctness": 5}
    assert _extract_json("no json") is None


def test_judge_result_with_fake_llm():
    """judge_result 解析 judge 模型的 JSON 输出并附上 case_id。"""
    import asyncio

    from app.eval.judge import judge_result

    class FakeJudge:
        info = {"provider": "mock", "model": "judge-1"}

        async def chat_json(self, messages, temperature=0.0):
            return {
                "task_success": 1,
                "correctness": 4,
                "faithfulness": 5,
                "helpfulness": 4,
                "summary": "正确完成",
            }

    class FakeResult:
        steps = [{"type": "tool", "tool": "calculate", "arguments": "{}"}]
        answer = "答案是 161"

    case = {"id": "calc", "user_input": "计算 23*7"}
    data = asyncio.run(judge_result(case, FakeResult(), FakeJudge()))
    assert data["case_id"] == "calc"
    assert data["task_success"] == 1
    assert data["correctness"] == 4
    assert data["faithfulness"] == 5
    assert data["model"] == "judge-1"


def test_judge_result_error_returns_none():
    """judge 模型异常时返回 None,不影响原断言。"""
    import asyncio

    from app.eval.judge import judge_result

    class BrokenJudge:
        async def chat_json(self, messages, temperature=0.0):
            raise RuntimeError("boom")

    class FakeResult:
        steps = []
        answer = "hi"

    case = {"id": "x", "user_input": "hello"}
    data = asyncio.run(judge_result(case, FakeResult(), BrokenJudge()))
    assert data is None


def test_otlp_disabled_by_default():
    """未配置 OTLP 端点时不初始化导出器。"""
    import app.core.telemetry as tel

    old = tel.settings.otel_exporter_otlp_endpoint
    tel.settings.otel_exporter_otlp_endpoint = ""
    try:
        t = tel.Tracer()
        assert t._otlp is None  # noqa: SLF001
    finally:
        tel.settings.otel_exporter_otlp_endpoint = old


def test_otlp_init_with_bad_endpoint_falls_back():
    """OTLP 端点不可达时探测失败,直接禁用导出器,不影响本地记录。"""
    import app.core.telemetry as tel

    old = tel.settings.otel_exporter_otlp_endpoint
    tel.settings.otel_exporter_otlp_endpoint = "http://127.0.0.1:1/v1/traces"  # 不可达
    try:
        t = tel.Tracer()
        assert t._otlp is None  # noqa: SLF001  探测失败 => 不启用 OTLP
        t.record({"type": "run_start", "run_id": "t1"})
        t.record({"type": "run_end", "run_id": "t1", "ok": True})
        recent = t.recent(run_id="t1")
        assert any(e["type"] == "run_start" for e in recent)
    finally:
        tel.settings.otel_exporter_otlp_endpoint = old
