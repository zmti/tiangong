"""API 安全:多租户 API Key 校验 + 向后兼容单 Key 模式。

两种模式:
1. 单 Key 模式(向后兼容):AGENT_API_KEY 非空时,所有请求匹配同一 Key
2. 多租户模式:DEFAULT_TENANT_ENABLED=true 时,每个 API Key 归属一个租户
   - Bearer <key> → 解析出 tenant_id + role
   - 自动注入 TenantContext,后续存储查询自动过滤 tenant_id
   - 用量统计自动记录到对应租户
"""
from __future__ import annotations

import hmac
import logging
from fastapi import Header, HTTPException

from app.config import settings

logger = logging.getLogger(__name__)


def _constant_time_compare(a: str, b: str) -> bool:
    """常量时间比较,防止时序攻击。"""
    return hmac.compare_digest(a.encode('utf-8'), b.encode('utf-8'))


async def require_api_key(
    authorization: str | None = Header(None, alias="Authorization"),
):
    """多租户 API Key 校验。"""
    from app.core.tenant_context import set_tenant, clear_tenant

    # 模式1: 单 Key (向后兼容)
    single_key = settings.agent_api_key
    if single_key:
        expected = f"Bearer {single_key}"
        # 使用常量时间比较防止时序攻击
        if not _constant_time_compare(authorization or "", expected):
            raise HTTPException(status_code=401, detail="无效的 API Key")
        return

    # 模式2: 多租户
    if not getattr(settings, "default_tenant_enabled", False):
        return  # 未启用多租户,放行

    if not authorization:
        raise HTTPException(status_code=401, detail="缺少 Authorization 头")

    # 提取 Key
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or not _constant_time_compare(parts[0], "Bearer"):
        raise HTTPException(status_code=401, detail="Authorization 格式错误,应为 Bearer <key>")
    raw_key = parts[1].strip()
    if not raw_key:
        raise HTTPException(status_code=401, detail="API Key 不能为空")

    # 验证
    try:
        from app.core.tenant import get_tenant_manager
        mgr = get_tenant_manager()
        info = mgr.verify_key(raw_key)
        if not info:
            raise HTTPException(status_code=401, detail="无效的 API Key")
        # 设置上下文
        set_tenant(info["tenant_id"], info["role"])
        logger.debug("API Key 验证通过: tenant=%s role=%s", info["tenant_id"], info["role"])
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Tenant 验证异常: %s", e)
        raise HTTPException(status_code=500, detail="租户验证服务异常")


async def require_admin(authorization: str | None = Header(None, alias="Authorization")):
    """要求管理员身份。

    多租户模式:需 admin 角色的 API Key;单 Key 模式:需有效 Bearer Key;
    未配置任何鉴权时拒绝,防止租户管理裸奔。
    """
    from app.core.tenant_context import get_role

    await require_api_key(authorization)

    role = get_role()
    if role is not None:
        if role != "admin":
            raise HTTPException(status_code=403, detail="需要 admin 角色")
        return

    # 单 Key 模式:验证通过即视为管理员
    if settings.agent_api_key:
        return

    # 完全未配置鉴权:拒绝访问(租户管理属敏感能力)
    raise HTTPException(
        status_code=403,
        detail="租户管理需要配置 AGENT_API_KEY 或 DEFAULT_TENANT_ENABLED=true",
    )


def get_current_tenant() -> tuple[str | None, str | None]:
    """获取当前请求的 (tenant_id, role)。"""
    from app.core.tenant_context import get_tenant, get_role
    return get_tenant(), get_role()
