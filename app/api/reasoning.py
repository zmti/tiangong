"""推理引擎 API。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1/reasoning", tags=["reasoning"])


class ReasonRequest(BaseModel):
    question: str = Field(..., description="需要推理的问题")
    strategy: str = Field("cot", description="推理策略: cot/tot/self_consistent")
    max_steps: int = Field(10, ge=1, le=30, description="最大推理步数")
    n_branches: int = Field(3, ge=1, le=10, description="ToT 分支数")
    n_samples: int = Field(5, ge=2, le=20, description="Self-Consistency 采样数")


@router.post("/chain")
async def reason(req: ReasonRequest) -> dict:
    """链式推理入口。"""
    from app.core.reasoning_engine import reason as _reason
    import asyncio as _asyncio
    kwargs = {}
    if req.strategy == "cot":
        kwargs["max_steps"] = req.max_steps
    elif req.strategy in ("tot", "got"):
        kwargs["k"] = req.n_branches
        kwargs["max_rounds"] = min(req.max_steps // 3, 5)
    elif req.strategy == "self_consistent":
        kwargs["n_samples"] = req.n_samples

    result = _asyncio.run(_reason(req.question, strategy=req.strategy, **kwargs))
    return result.to_dict()


@router.get("/strategies")
async def list_strategies() -> dict:
    """列出支持的推理策略。"""
    return {
        "strategies": [
            {"name": "cot", "description": "Chain-of-Thought: 逐步推理,适合逻辑分析"},
            {"name": "tot", "description": "Tree-of-Thought: 多分支探索,适合复杂决策"},
            {"name": "got", "description": "Graph-of-Thought: 图结构推理,支持任意拓扑"},
            {"name": "self_consistent", "description": "Self-Consistency: 多次采样取众数,提高准确性"},
        ],
    }
