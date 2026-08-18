"""运行记录接口:checkpoint 列表 / 详情 / 断点续跑 / 删除(桌面端运行记录面板)。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.core.agent import Agent
from app.core.llm import LLMClient, LLMError
from app.core.tenant_context import get_tenant, build_tenant_where
from app.runtime.checkpoint import CheckpointManager

router = APIRouter(prefix="/v1/runs", tags=["runs"])


def _manager() -> CheckpointManager:
    try:
        return CheckpointManager()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"checkpoint 不可用: {e}") from e


@router.get("")
async def list_runs(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str = Query("", description="按状态过滤: running/done/failed/interrupted"),
) -> dict:
    """列出已持久化的运行记录(checkpoint 摘要)。"""
    from app.core.tenant_context import get_tenant
    status = str(status or "")
    tenant = get_tenant()
    mgr = _manager()
    runs = mgr.list(limit=limit, offset=offset, status=status or None, tenant_id=tenant)
    return {"runs": runs, "total": len(runs)}


@router.get("/{run_id}")
async def get_run(run_id: str) -> dict:
    """单个运行详情(含计划/步骤/答案等摘要)。"""
    mgr = _manager()
    # 直接按主键加载,避免全量扫描
    state = mgr.load(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"未找到运行记录: {run_id}")
    return state.summary()


@router.post("/{run_id}/resume")
async def resume_run(run_id: str) -> dict:
    """从中断处恢复一次运行(断点续跑)。

    与 /v1/chat/resume 等价,但 session_id 从 checkpoint 恢复,
    桌面端运行记录面板无需关心会话即可一键续跑。
    """
    mgr = _manager()
    # 直接按主键加载
    state = mgr.load(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"未找到运行记录: {run_id}")
    session_id = state.session_id
    try:
        agent = Agent(session_id=session_id)
        result = await agent.resume(run_id)
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except LLMError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    resp = {
        "answer": result.answer,
        "run_id": run_id,
        "session_id": session_id,
        "model": result.model,
        "steps": result.steps,
    }
    if result.interrupted:
        resp["interrupted"] = True
        resp["interrupt"] = result.interrupt
    return resp


@router.delete("/{run_id}")
async def delete_run(run_id: str) -> dict:
    """删除一条运行记录(checkpoint)。"""
    ok = _manager().delete(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"未找到运行记录: {run_id}")
    return {"run_id": run_id, "deleted": True}


@router.post("/cleanup")
async def cleanup_runs() -> dict:
    """清理过期 checkpoint(超过 checkpoint_ttl_days 天的已完成/已取消记录)。"""
    from app.config import settings as _cfg
    ttl = int(getattr(_cfg, "checkpoint_ttl_days", 7) or 7)
    removed = _manager().cleanup_expired(ttl_days=ttl)
    return {"removed": removed, "ttl_days": ttl}
