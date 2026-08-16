"""API 安全:可选的 Bearer API Key 校验。

设置 AGENT_API_KEY 后,所有 /v1 路由要求 Authorization: Bearer <key>。
留空(默认)则放行 —— 保持桌面本地部署零配置。
"""
from __future__ import annotations

from fastapi import Header, HTTPException

from app.config import settings


async def require_api_key(authorization: str | None = Header(None, alias="Authorization")):
    key = settings.agent_api_key
    if not key:
        return
    expected = f"Bearer {key}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="无效的 API Key,请携带 Authorization: Bearer <AGENT_API_KEY>")
