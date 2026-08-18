"""跨会话偏好学习 API。"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1/cross-session", tags=["cross-session"])


class LearnPreferencesRequest(BaseModel):
    session_id: str = "default"
    messages: list[dict] = Field(default_factory=list, description="对话历史")
    feedback: str = Field("", description="用户反馈")


@router.get("/preferences")
async def get_preferences(user_id: str = Query("default")) -> dict:
    """获取跨会话偏好。"""
    from app.core.cross_session import get_cross_session_store
    store = get_cross_session_store()
    prefs = store.get(user_id)
    return prefs.to_dict()


@router.post("/learn")
async def learn(req: LearnPreferencesRequest) -> dict:
    """从对话中学习偏好。"""
    from app.core.cross_session import get_cross_session_store
    store = get_cross_session_store()
    prefs = store.merge_from_dialogue(req.session_id, req.messages, req.feedback)
    return {
        "user_id": req.session_id,
        "updated_fields": {
            "language": prefs.language,
            "tech_stack": prefs.tech_stack,
            "response_style": prefs.response_style,
        },
        "version": prefs.version,
    }


@router.get("/injection")
async def get_injection(user_id: str = Query("default")) -> str:
    """获取注入到系统提示的偏好文本。"""
    from app.core.cross_session import get_cross_session_store
    store = get_cross_session_store()
    return store.build_injection_prompt(user_id)


@router.post("/record")
async def record_consistency(req: BaseModel) -> dict:
    """记录一次跨会话一致性快照。"""
    from app.core.memory_cleanup import get_consistency_engine
    engine = get_consistency_engine()
    question = getattr(req, "question", "")
    style = getattr(req, "answer_style", "detailed")
    detail = getattr(req, "detail_level", "medium")
    lang = getattr(req, "language", "zh")
    tech = getattr(req, "tech_stack", [])
    engine.record_consistency(question, style, detail, lang, tech)
    return {"recorded": True, "question_hash": question[:50] if question else ""}


@router.get("/consistency-hint")
async def consistency_hint(
    question: str = Query(..., description="当前问题"),
    session_id: str = Query("default", description="当前会话"),
) -> dict:
    """获取跨会话一致性提示。"""
    from app.core.memory_cleanup import get_consistency_engine
    engine = get_consistency_engine()
    hint = engine.get_consistency_hint(question, session_id)
    return {"hint": hint, "has_hint": bool(hint)}
