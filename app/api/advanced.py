"""记忆精炼 / Embedding 缓存 / 技能管理 / A-B评估 / 性能监控 API"""
from __future__ import annotations

import json
import os
import time

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import settings
from app.core.skills import get_skill_registry
from app.eval.ab import ABEvaluator
from app.memory.consolidator import get_consolidator
from app.rag.cache import get_embedding_cache
from app.core.tool_route_cache import get_tool_route_cache

router = APIRouter(prefix="/v1", tags=["advanced"])

# 内存中缓存最近一次 A/B 报告路径
_last_report_path = os.path.join(settings.resolved_data_dir, "ab_report_latest.json")


# ---- 记忆精炼 ----

@router.post("/memory/consolidate")
async def consolidate_memories(
    force: bool = False,
    max_group_size: int = Query(15, ge=2, le=50),
    scope: str = Query("global", description="作用域:global/session/all"),
) -> dict:
    """触发记忆精炼(合并相似记忆、提炼用户画像)。"""
    consolidator = get_consolidator()
    target_scope = None if scope == "all" else scope
    result = consolidator.consolidate(
        force=force,
        max_group_size=max_group_size,
        target_scope=target_scope,
    )
    return result.to_dict()


@router.get("/memory/consolidate/status")
async def consolidation_status() -> dict:
    consolidator = get_consolidator()
    last_run = consolidator._last_run  # noqa: SLF001
    next_in = max(0, round((consolidator._min_interval_seconds - (time.time() - last_run)) / 60, 1))  # noqa: SLF001
    return {
        "enabled": consolidator.ready(),
        "last_run_at": last_run,
        "next_available_in_minutes": next_in,
    }


# ---- Embedding 缓存 ----

@router.get("/embedding_cache/stats")
async def embedding_cache_stats() -> dict:
    cache = get_embedding_cache()
    return cache.stats()


@router.post("/embedding_cache/invalidate")
async def embedding_cache_invalidate() -> dict:
    cache = get_embedding_cache()
    n = cache.invalidate_all()
    return {"invalidated": n}


# ---- 技能管理 ----

@router.get("/skills")
async def list_skills(enabled_only: bool = Query(True)) -> dict:
    reg = get_skill_registry()
    skills = reg.list(enabled_only=enabled_only)
    return {
        "skills": [
            {
                "name": s.name,
                "description": s.description,
                "steps": len(s.steps),
                "params": [p.name for p in s.params],
                "enabled": s.enabled,
            }
            for s in skills
        ],
        "total": len(skills),
    }


@router.get("/skills/{skill_name}")
async def get_skill(skill_name: str) -> dict:
    reg = get_skill_registry()
    skill = reg.get(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"未找到技能: {skill_name}")
    return {
        "name": skill.name,
        "description": skill.description,
        "steps": [
            {"tool": s.tool, "args": s.args, "label": s.label}
            for s in skill.steps
        ],
        "params": [
            {"name": p.name, "type": p.type_, "description": p.description,
             "default": p.default, "required": p.required}
            for p in skill.params
        ],
        "enabled": skill.enabled,
    }


@router.post("/skills/{skill_name}/toggle")
async def toggle_skill(skill_name: str, enabled: bool = True) -> dict:
    reg = get_skill_registry()
    skill = reg.get(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"未找到技能: {skill_name}")
    skill.enabled = enabled
    return {"name": skill_name, "enabled": enabled}


# ---- A/B 评估 ----

class ABRunRequest(BaseModel):
    config_a: dict = Field(..., description="Variant A 配置(JSON,如 {mode:'single', reflect:false})")
    config_b: dict = Field(..., description="Variant B 配置")
    provider_a: str = Field("", description="Variant A 模型 provider(留空=全局配置)")
    provider_b: str = Field("", description="Variant B 模型 provider(留空=全局配置)")
    max_concurrent: int = Field(3, ge=1, le=10, description="并行度")
    case_names: list[str] = Field(default_factory=list, description="指定用例名(空=全部)")
    case_categories: list[str] = Field(default_factory=list, description="指定分类(空=全部)")


@router.post("/eval/ab/run")
async def run_ab_test(req: ABRunRequest) -> dict:
    """在同一用例集上对比两组配置的通过率、成本和耗时。"""
    from app.eval.cases import load_cases

    cases = load_cases()
    if req.case_names:
        cases = [c for c in cases if c.get("name") in req.case_names]
    if req.case_categories:
        cases = [c for c in cases if c.get("category") in req.case_categories]

    if not cases:
        raise HTTPException(status_code=400, detail="没有匹配的用例")

    evaluator = ABEvaluator(
        config_a=req.config_a,
        config_b=req.config_b,
        provider_a=req.provider_a,
        provider_b=req.provider_b,
        max_concurrent=req.max_concurrent,
    )
    report = await evaluator.run(cases)
    report_dict = report.to_dict()
    # 落盘 JSON
    try:
        with open(_last_report_path, "w", encoding="utf-8") as f:
            json.dump(report_dict, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
    return report_dict


@router.get("/eval/ab/report")
async def get_ab_report(report_hash: str = Query("")) -> dict:
    """查询最近一次 A/B 评估报告(本地 JSON 文件)。"""
    if not os.path.exists(_last_report_path):
        return {"error": "暂无报告,请先运行 POST /v1/eval/ab/run"}
    try:
        with open(_last_report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
        return report
    except Exception as e:  # noqa: BLE001
        return {"error": f"读取报告失败: {e}"}
