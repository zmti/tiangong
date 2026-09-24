"""A/B 评估 API: /v1/eval/ab/*
对比两组 Agent 配置在同一数据集上的指标差异。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from app.eval.ab import ABEvaluator, ABReport
from app.eval.cases import EVAL_CASES as BASELINE_CASES
from app.eval.cases_v2 import EVAL_CASES as EXTENDED_CASES
from app.core.llm import LLMClient

router = APIRouter(prefix="/v1/eval/ab", tags=["eval-ab"])

# 合并用例集
ALL_CASES = BASELINE_CASES + EXTENDED_CASES

# 内存存储运行中的 A/B 任务
_ab_runs: dict[str, dict] = {}


class ABRunRequest(BaseModel):
    config_a: dict = Field(..., description="配置A，如 {'mode': 'single', 'max_steps': 10}")
    config_b: dict = Field(..., description="配置B，如 {'mode': 'review', 'critic_provider': 'qwen'}")
    provider_a: str = Field("", description="配置A的provider(留空用默认)")
    provider_b: str = Field("", description="配置B的provider(留空用默认)")
    case_ids: list[str] = Field(default_factory=list, description="指定用例ID列表，空=全部")
    category: str = Field("", description="指定分类(basic/memory/web/browser/db/chart/file/code/rag/multi_agent/planning)")
    max_concurrent: int = Field(3, ge=1, le=10, description="并发度")
    session_id_prefix: str = Field("ab_test", description="会话ID前缀")


class ABRunResponse(BaseModel):
    run_id: str
    status: str
    message: str


def _filter_cases(case_ids: list[str], category: str) -> list[dict]:
    cases = ALL_CASES
    if case_ids:
        cases = [c for c in cases if c["id"] in case_ids]
    if category:
        cases = [c for c in cases if c.get("category") == category]
    return cases


async def _run_ab_background(run_id: str, req: ABRunRequest) -> None:
    _ab_runs[run_id] = {"status": "running", "start_time": time.time()}
    try:
        cases = _filter_cases(req.case_ids, req.category)
        if not cases:
            _ab_runs[run_id] = {"status": "failed", "error": "没有匹配的用例"}
            return

        evaluator = ABEvaluator(
            config_a=req.config_a,
            config_b=req.config_b,
            provider_a=req.provider_a,
            provider_b=req.provider_b,
            max_concurrent=req.max_concurrent,
        )
        report = await evaluator.run(cases, req.session_id_prefix)

        # 落盘报告
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "ab_reports")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{run_id}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)

        _ab_runs[run_id] = {
            "status": "completed",
            "report": report.to_dict(),
            "report_path": out_path,
            "elapsed_seconds": report.elapsed_seconds,
        }
    except Exception as e:
        _ab_runs[run_id] = {"status": "failed", "error": str(e)}


@router.post("/run", response_model=ABRunResponse)
async def run_ab_test(req: ABRunRequest, background: BackgroundTasks) -> ABRunResponse:
    """启动 A/B 评估(异步后台运行)。"""
    run_id = f"ab_{int(time.time() * 1000)}_{os.urandom(4).hex()}"
    background.add_task(_run_ab_background, run_id, req)
    return ABRunResponse(run_id=run_id, status="started", message="A/B 评估已启动，请用 /status 查询进度")


@router.get("/status/{run_id}")
async def get_ab_status(run_id: str) -> dict:
    """查询 A/B 评估状态与结果。"""
    run = _ab_runs.get(run_id)
    if not run:
        # 尝试从磁盘加载
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "ab_reports")
        path = os.path.join(out_dir, f"{run_id}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                report = json.load(f)
            return {"status": "completed", "report": report}
        raise HTTPException(status_code=404, detail=f"运行不存在: {run_id}")
    return run


@router.get("/report/{run_id}")
async def get_ab_report(run_id: str) -> dict:
    """获取完整 A/B 报告。"""
    run = _ab_runs.get(run_id)
    if not run or run.get("status") != "completed":
        # 尝试从磁盘加载
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "ab_reports")
        path = os.path.join(out_dir, f"{run_id}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                report = json.load(f)
            return report
        raise HTTPException(status_code=404, detail=f"报告不存在或未完成: {run_id}")
    return run.get("report", {})


@router.get("/list")
async def list_ab_runs(limit: int = 20) -> dict:
    """列出最近的 A/B 运行记录。"""
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "ab_reports")
    runs = []
    if os.path.exists(out_dir):
        for fname in sorted(os.listdir(out_dir), reverse=True)[:limit]:
            if fname.endswith(".json"):
                path = os.path.join(out_dir, fname)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        report = json.load(f)
                    runs.append({
                        "run_id": fname[:-5],
                        "config_a": report.get("config_a"),
                        "config_b": report.get("config_b"),
                        "a_pass_rate": report.get("a_pass_rate"),
                        "b_pass_rate": report.get("b_pass_rate"),
                        "elapsed_seconds": report.get("elapsed_seconds"),
                        "total_cases": report.get("total_cases"),
                        "statistical_significance": report.get("statistical_significance"),
                    })
                except Exception:
                    pass
    return {"runs": runs, "total": len(runs)}


@router.post("/quick")
async def quick_ab_compare(
    config_a: dict,
    config_b: dict,
    provider_a: str = "",
    provider_b: str = "",
    case_ids: list[str] = [],
    category: str = "",
) -> dict:
    """快速同步 A/B 对比(少量用例，直接返回结果)。"""
    cases = _filter_cases(case_ids, category)
    if not cases:
        raise HTTPException(status_code=400, detail="没有匹配的用例")
    if len(cases) > 20:
        raise HTTPException(status_code=400, detail="快速模式最多 20 个用例，请用 /run 异步模式")

    evaluator = ABEvaluator(
        config_a=config_a,
        config_b=config_b,
        provider_a=provider_a,
        provider_b=provider_b,
        max_concurrent=5,
    )
    report = await evaluator.run(cases, "ab_quick")
    return report.to_dict()


@router.get("/cases")
async def list_eval_cases(category: str = "", limit: int = 50) -> dict:
    """列出可用评测用例。"""
    cases = ALL_CASES
    if category:
        cases = [c for c in cases if c.get("category") == category]
    return {
        "cases": [
            {
                "id": c["id"],
                "name": c.get("name", ""),
                "category": c.get("category", ""),
                "expect_tool": c.get("expect_tool", ""),
                "expect_keyword": c.get("expect_keyword", ""),
            }
            for c in cases[:limit]
        ],
        "total": len(cases),
        "categories": sorted({c.get("category", "?") for c in ALL_CASES}),
    }