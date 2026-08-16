"""可观测性(telemetry)测试:事件记录、run_id 串联、聚合统计、成本估算。"""
from __future__ import annotations

import asyncio

from app.core.telemetry import _current_run_id, estimate_cost, tracer


def _reset_buffer():
    with tracer._lock:  # noqa: SLF001
        tracer._buffer = []  # noqa: SLF001


def test_record_and_recent():
    _reset_buffer()
    tracer.record({"type": "test_event", "run_id": "r1"})
    evts = tracer.recent(run_id="r1")
    assert len(evts) == 1 and evts[0]["type"] == "test_event"
    assert "ts" in evts[0]


def test_run_id_contextvar_linking():
    _reset_buffer()
    token = _current_run_id.set("run-abc")

    async def inner():
        return asyncio.current_task()

    asyncio.run(inner())  # 保证在 async 环境调用也不影响 contextvar
    tracer.record({"type": "llm_call", "provider": "test", "model": "m",
                   "prompt_tokens": 10, "completion_tokens": 5,
                   "tokens": 15, "cost": 0.0, "ok": True})
    _current_run_id.reset(token)
    evts = tracer.recent(run_id="run-abc")
    assert len(evts) == 1 and evts[0]["run_id"] == "run-abc"


def test_stats_aggregation():
    _reset_buffer()
    for i in range(3):
        tracer.record({"type": "llm_call", "provider": "p", "model": "m",
                       "prompt_tokens": 100, "completion_tokens": 50,
                       "tokens": 150, "cost": 0.001, "ok": True})
    tracer.record({"type": "llm_call", "provider": "p", "model": "m",
                   "prompt_tokens": 0, "completion_tokens": 0, "tokens": 0,
                   "cost": 0.0, "ok": False, "error": "boom"})
    tracer.record({"type": "tool", "name": "add", "ok": False})
    s = tracer.stats()
    assert s["llm_calls"] == 4
    assert s["errors"] == 2
    assert s["prompt_tokens"] == 300
    assert s["completion_tokens"] == 150
    assert s["total_tokens"] == 450
    assert abs(s["estimated_cost_usd"] - 0.003) < 1e-9


def test_runs_summary():
    _reset_buffer()
    tracer.record({"type": "run_start", "run_id": "r9", "input": "hi"})
    tracer.record({"type": "llm_call", "run_id": "r9", "tokens": 10, "cost": 0.0, "ok": True})
    tracer.record({"type": "tool", "run_id": "r9", "ok": True})
    tracer.record({"type": "run_end", "run_id": "r9", "ok": True, "steps": 2, "duration": 1.0})
    runs = tracer.runs()
    r = next(x for x in runs if x["run_id"] == "r9")
    assert r["llm_calls"] == 1
    assert r["tools"] == 1
    assert r["tokens"] == 10
    assert r["ok"] is True


def test_estimate_cost():
    assert estimate_cost("deepseek-chat", 1000, 500) > 0
    assert estimate_cost("unknown-model", 1000, 500) == 0.0
