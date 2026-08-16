"""eval 回归集的冒烟测试:确保用例与 runner 可导入、判定/落盘/基线对比逻辑正确,不调用真实 LLM。"""
from __future__ import annotations

import json
import os
import tempfile

import app.eval.runner as runner
from app.eval.cases import EVAL_CASES


def test_cases_well_formed():
    assert len(EVAL_CASES) >= 1
    ids = [c["id"] for c in EVAL_CASES]
    assert len(ids) == len(set(ids)), "用例 id 重复"
    for c in EVAL_CASES:
        assert c["user_input"]
        assert c.get("expect_tool") or c.get("expect_keyword"), f"用例 {c['id']} 缺少断言"


class _StubStepResult:
    def __init__(self, used_tools, answer):
        self.steps = [{"type": "tool", "tool": t} for t in used_tools]
        self.answer = answer


def test_check_pass():
    ok, reasons = runner._check(_StubStepResult(["calculate"], "结果是 161"), EVAL_CASES[1])
    assert ok and not reasons


def test_check_fail_missing_tool():
    ok, reasons = runner._check(_StubStepResult([], "161"), EVAL_CASES[1])
    assert not ok
    assert any("calculate" in r for r in reasons)


def test_check_fail_missing_keyword():
    ok, reasons = runner._check(_StubStepResult(["calculate"], "我算不出来"), EVAL_CASES[1])
    assert not ok
    assert any("161" in r for r in reasons)


def test_save_json_roundtrip():
    results = [{"id": "calc", "ok": True, "reasons": [], "steps": 2, "seconds": 1.0}]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "result.json")
        runner.save_json(results, path)
        data = json.load(open(path, encoding="utf-8"))
        assert data["passed"] == 1 and data["total"] == 1


def test_compare_baseline_no_regression():
    results = [{"id": "calc", "ok": True, "reasons": []}, {"id": "time", "ok": True, "reasons": []}]
    baseline = {"cases": {"calc": True, "time": True}}
    assert runner.compare_baseline(results, baseline) == []


def test_compare_baseline_detects_regression():
    results = [{"id": "calc", "ok": False, "reasons": ["未调用期望工具 calculate"]}]
    baseline = {"cases": {"calc": True}}
    regressions = runner.compare_baseline(results, baseline)
    assert len(regressions) == 1 and "calc" in regressions[0]


def test_compare_baseline_skips_expected_fail():
    results = [{"id": "calc", "ok": False, "reasons": []}]
    baseline = {"cases": {"calc": False}}
    assert runner.compare_baseline(results, baseline) == []
