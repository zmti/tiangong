"""租户上下文:thread-local 当前租户信息,供各存储层自动过滤。

使用方式:
    from app.core.tenant_context import set_tenant, get_tenant, tenant_context
    # 在 API 路由中设置:
    with tenant_context("tenant_abc", "user"):
        result = await agent.run_stream(...)
    # 在各存储层读取:
    tid = get_tenant()  # "tenant_abc"

背景:
- contextvar 保证 asyncio 协程切换时上下文不丢失
- 与现有单租户模式完全兼容:未设置时 get_tenant() 返回 None,
  所有查询不过滤 tenant_id(保持向后兼容)
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Optional

_current_tenant_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "tenant_id", default=None
)
_current_role: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "role", default=None
)


def set_tenant(tenant_id: str, role: str = "user") -> None:
    """设置当前线程/协程的租户上下文。"""
    _current_tenant_id.set(tenant_id)
    _current_role.set(role)


def get_tenant() -> Optional[str]:
    """获取当前租户 ID。无上下文时返回 None。"""
    return _current_tenant_id.get()


def get_role() -> Optional[str]:
    """获取当前角色。"""
    return _current_role.get()


def clear_tenant() -> None:
    """清除当前租户上下文。"""
    _current_tenant_id.set(None)
    _current_role.set(None)


@contextmanager
def tenant_context(tenant_id: str, role: str = "user"):
    """上下文管理器,在 block 内设置租户上下文。"""
    token_id = _current_tenant_id.set(tenant_id)
    token_role = _current_role.set(role)
    try:
        yield
    finally:
        _current_tenant_id.reset(token_id)
        _current_role.reset(token_role)


def build_tenant_where(tenant_id: Optional[str] = None) -> tuple[str, tuple]:
    """构建 tenant_id 过滤条件。None 时返回空条件(不过滤,向后兼容)。"""
    tid = tenant_id or get_tenant()
    if not tid:
        return "", ()
    return "tenant_id = ?", (tid,)
