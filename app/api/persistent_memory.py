"""记忆管理增强 API:/v1/persistent-memory/*

功能:
- GET /v1/persistent-memory — 查询持久化记忆
- POST /v1/persistent-memory — 保存记忆
- POST /v1/persistent-memory/merge — 触发记忆融合
- GET /v1/persistent-memory/stats — 记忆统计
- GET /v1/profile — 获取用户画像
- PUT /v1/profile — 更新用户画像
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.core.tenant_context import get_tenant

router = APIRouter(prefix="/v1/persistent-memory", tags=["persistent-memory"])


@router.get("")
async def list_memories(
    user_id: str = Query("default", description="用户 ID"),
    memory_type: str = Query("", description="记忆类型过滤: fact/preference/habit/decision/skill"),
    top_k: int = Query(20, ge=1, le=100, description="返回条数"),
) -> dict:
    """列出持久化记忆。"""
    from app.memory.persistent_memory import get_persistent_store
    from app.memory.ltm import get_memory_store

    tenant = get_tenant()
    store = get_persistent_store()
    types = [memory_type] if memory_type else None
    memories = store.recall("", user_id=user_id, memory_types=types, top_k=top_k * 5)
    ltm_store = get_memory_store()
    # 按租户过滤记忆列表
    all_mem = await ltm_store.list(limit=top_k, tenant_id=tenant)
    return {"memories": all_mem, "total": len(all_mem)}


@router.post("")
async def save_memory(req: BaseModel) -> dict:
    """保存一条持久化记忆。"""
    from app.memory.persistent_memory import get_persistent_store
    from app.core.tenant_context import get_tenant

    tenant = get_tenant()
    store = get_persistent_store()
    content = getattr(req, "content", "")
    memory_type = getattr(req, "memory_type", "fact")
    keywords = getattr(req, "keywords", [])
    user_id = getattr(req, "user_id", "default")

    if not content:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="content 不能为空")

    result = store.save(
        content=content,
        memory_type=memory_type,
        keywords=keywords if isinstance(keywords, list) else [],
        user_id=user_id,
        tenant_id=tenant,
    )
    return result


@router.post("/merge")
async def merge_memories(
    user_id: str = Query("default"),
    threshold: float = Query(0.85, ge=0.5, le=0.99, description="相似度阈值"),
) -> dict:
    """触发记忆融合。"""
    from app.memory.persistent_memory import run_persistent_merge

    result = run_persistent_merge(user_id, threshold)
    return result


@router.get("/stats")
async def memory_stats(user_id: str = Query("default")) -> dict:
    """记忆统计。"""
    from app.memory.ltm import get_memory_store
    from app.core.tenant_context import get_tenant

    tenant = get_tenant()
    store = get_memory_store()
    stats = await store.stats()
    # 确保统计结果包含当前租户
    stats["tenant_id"] = tenant or "default"
    return stats


# ── 用户画像 API ─────────────────────────────────────────────────
@router.get("/profile")
async def get_profile(user_id: str = Query("default")) -> dict:
    """获取用户画像。"""
    from app.memory.persistent_memory import get_evolver

    evolver = get_evolver()
    profile = evolver.get(user_id)
    return profile.to_dict()


class ProfileUpdateRequest(BaseModel):
    user_id: str = "default"
    name: str = ""
    preferred_language: str = ""
    style: str = ""
    response_style: str = ""
    language_preference: str = ""
    domains: list[str] = Field(default_factory=list)
    tech_stack: list[str] = Field(default_factory=list)
    habits: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


@router.put("/profile")
async def update_profile(req: ProfileUpdateRequest) -> dict:
    """更新用户画像。"""
    from app.memory.persistent_memory import get_evolver
    from app.memory.persistent_memory import UserProfileV2

    evolver = get_evolver()
    profile = evolver.get(req.user_id)
    profile.name = req.name
    profile.preferred_language = req.preferred_language
    profile.style = req.style
    profile.response_style = req.response_style
    profile.language_preference = req.language_preference
    profile.domains = req.domains
    profile.tech_stack = req.tech_stack
    profile.habits = req.habits
    profile.constraints = req.constraints
    evolver.save(req.user_id, profile)
    return profile.to_dict()


@router.post("/profile/learn")
async def learn_profile(session_id: str = Query("default")) -> dict:
    """从最近对话中自动学习用户画像。"""
    from app.memory.persistent_memory import get_evolver
    from app.memory.session import get_session_store

    evolver = get_evolver()
    store = get_session_store()
    history = await store.get_history(session_id)
    learned = evolver.learn_from_dialogue(session_id, history)
    return {
        "user_id": session_id,
        "updated_fields": {
            "tech_stack": learned.tech_stack,
            "domains": learned.domains,
            "language_preference": learned.language_preference,
        },
    }
