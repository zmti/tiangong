"""用户画像 API:/v1/user_profile/*"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.memory.profile import UserProfile, get_profile_store, build_profile_prompt

router = APIRouter(prefix="/v1/user_profile", tags=["user_profile"])


class UpdateProfileRequest(BaseModel):
    user_id: str = Field("default", description="用户 ID,默认 'default'")
    name: str = Field("", description="用户名称")
    preferred_language: str = Field("", description="常用编程语言,如 'Python,TypeScript'")
    style: str = Field("", description="代码风格偏好")
    response_style: str = Field("", description="回复风格 concise/detailed/humorous/academic")
    language_preference: str = Field("", description="语言偏好 zh/en/mixed")
    domains: list[str] = Field(default_factory=list, description="擅长领域")
    tech_stack: list[str] = Field(default_factory=list, description="技术栈")
    habits: list[str] = Field(default_factory=list, description="工作习惯")
    constraints: list[str] = Field(default_factory=list, description="必须遵守的约束")


@router.get("")
async def get_profile(user_id: str = "default") -> dict:
    store = get_profile_store()
    profile = store.get(user_id)
    return {
        "user_id": user_id,
        "profile": profile.to_dict(),
        "system_prompt_hint": build_profile_prompt(user_id),
    }


@router.put("")
async def update_profile(req: UpdateProfileRequest) -> dict:
    store = get_profile_store()
    p = UserProfile(
        user_id=req.user_id,
        name=req.name,
        preferred_language=req.preferred_language,
        style=req.style,
        response_style=req.response_style,
        language_preference=req.language_preference,
        domains=req.domains,
        tech_stack=req.tech_stack,
        habits=req.habits,
        constraints=req.constraints,
    )
    store.save(req.user_id, p)
    return {"user_id": req.user_id, "version": p.version, "ok": True}


@router.delete("")
async def delete_profile(user_id: str = "default") -> dict:
    store = get_profile_store()
    if user_id in store._profiles:
        del store._profiles[user_id]
        store._save()
        return {"user_id": user_id, "deleted": True}
    raise HTTPException(status_code=404, detail=f"未找到用户画像: {user_id}")


@router.get("/list")
async def list_profiles() -> dict:
    store = get_profile_store()
    return {"profiles": store.list_all(), "total": len(store.list_all())}


class ExtractProfileRequest(BaseModel):
    user_id: str = Field("default", description="用户 ID")
    message: str = Field(..., description="用户最新消息(用于推断画像)")


@router.post("/extract")
async def extract_from_history(req: ExtractProfileRequest) -> dict:
    """基于用户消息推断并更新画像字段(轻量版,不依赖 LLM)。"""
    store = get_profile_store()
    p = store.get(req.user_id)
    # 简单启发式:从消息中提取技术关键词
    import re
    tech_kw = set(re.findall(r"(?:python|typescript|react|fastapi|django|node|postgres|redis|docker|k8s|aws|gcp|azure)",
                              req.message.lower()))
    if tech_kw:
        existing = set(p.tech_stack or [])
        p.tech_stack = list(existing | tech_kw)
    store.save(req.user_id, p)
    return {"user_id": req.user_id, "updated_fields": ["tech_stack"], "profile": p.to_dict()}
