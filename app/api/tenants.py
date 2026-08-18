"""多租户管理 API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api.security import require_admin
from app.config import settings
from app.core.tenant import get_tenant_manager

router = APIRouter(prefix="/v1/tenants", tags=["tenants"])


def _mgr():
    try:
        return get_tenant_manager()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"租户管理器不可用: {e}") from e


# ---- 租户 CRUD ----

@router.get("")
async def list_tenants(
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    """列出所有租户。需 admin 权限。"""
    mgr = _mgr()
    tenants = mgr.list_tenants(limit=limit)
    return {"tenants": tenants, "total": len(tenants)}


@router.post("")
async def create_tenant(
    tenant_id: str = Query(..., description="租户唯一标识(slug)"),
    name: str = Query(..., description="租户名称"),
    plan: str = Query("free", description="计划等级:free/pro/enterprise"),
) -> dict:
    """创建租户。需 admin 权限。"""
    mgr = _mgr()
    try:
        tenant = mgr.create_tenant(tenant_id=tenant_id, name=name, plan=plan)
        return {"tenant_id": tenant_id, "name": name, "plan": plan, "created": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/{tenant_id}")
async def get_tenant(tenant_id: str) -> dict:
    """获取租户详情。"""
    mgr = _mgr()
    tenant = mgr.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail=f"租户不存在: {tenant_id}")
    usage = mgr.get_usage(tenant_id)
    return {"tenant": tenant, "usage": usage}


@router.put("/{tenant_id}")
async def update_tenant(
    tenant_id: str,
    name: str = Query(None, description="新名称"),
    plan: str = Query(None, description="新计划"),
    status: str = Query(None, description="新状态:active/suspended"),
) -> dict:
    """更新租户。需 admin 权限。"""
    mgr = _mgr()
    kwargs = {}
    if name is not None:
        kwargs["name"] = name
    if plan is not None:
        kwargs["plan"] = plan
    if status is not None:
        kwargs["status"] = status
    if not kwargs:
        raise HTTPException(status_code=400, detail="至少指定一个更新字段")
    tenant = mgr.update_tenant(tenant_id, **kwargs)
    if not tenant:
        raise HTTPException(status_code=404, detail=f"租户不存在: {tenant_id}")
    return {"tenant_id": tenant_id, "updated": True, "tenant": tenant}


@router.delete("/{tenant_id}")
async def delete_tenant(tenant_id: str) -> dict:
    """删除租户及其所有数据。需 admin 权限。"""
    mgr = _mgr()
    mgr.delete_tenant(tenant_id)
    return {"tenant_id": tenant_id, "deleted": True}


# ---- API Key 管理 ----

@router.get("/{tenant_id}/keys")
async def list_keys(tenant_id: str) -> dict:
    """列出租户的 API Keys。需 admin 或同租户 user。"""
    mgr = _mgr()
    keys = mgr.list_api_keys(tenant_id)
    return {"tenant_id": tenant_id, "keys": keys, "total": len(keys)}


@router.post("/{tenant_id}/keys")
async def create_key(
    tenant_id: str,
    name: str = Query(..., description="Key 名称"),
    role: str = Query("user", description="角色:user/admin/readonly"),
    expires_days: int = Query(0, description="过期天数,0=永不过期"),
) -> dict:
    """生成新 API Key。返回明文 Key(仅显示一次)。需 admin 或同租户 admin。"""
    mgr = _mgr()
    expires_at = 0
    if expires_days > 0:
        import time
        expires_at = time.time() + expires_days * 86400
    try:
        result = mgr.create_api_key(
            tenant_id=tenant_id,
            name=name,
            role=role,
            expires_at=expires_at,
        )
        return {
            "key": result["key"],
            "key_prefix": result["key_prefix"],
            "tenant_id": result["tenant_id"],
            "role": result["role"],
            "warning": "请妥善保管此 Key,仅显示一次",
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/{tenant_id}/keys/{key_prefix}")
async def revoke_key(tenant_id: str, key_prefix: str) -> dict:
    """吊销 API Key。需 admin 或同租户 admin。"""
    mgr = _mgr()
    ok = mgr.revoke_key(tenant_id, key_prefix)
    if not ok:
        raise HTTPException(status_code=404, detail="Key 不存在或已吊销")
    return {"tenant_id": tenant_id, "key_prefix": key_prefix, "revoked": True}


# ---- 用量统计 ----

@router.get("/{tenant_id}/usage")
async def get_usage(tenant_id: str) -> dict:
    """获取租户用量统计。"""
    mgr = _mgr()
    usage = mgr.get_usage(tenant_id)
    if not usage:
        raise HTTPException(status_code=404, detail=f"租户无用量记录: {tenant_id}")
    return {"tenant_id": tenant_id, "usage": usage}


@router.post("/{tenant_id}/usage/reset")
async def reset_usage(tenant_id: str) -> dict:
    """重置用量统计(月初清零)。需 admin 权限。"""
    mgr = _mgr()
    mgr.reset_usage(tenant_id)
    return {"tenant_id": tenant_id, "reset": True}


# ---- 当前请求租户信息 ----

@router.get("/me")
async def get_current_tenant() -> dict:
    """获取当前请求的租户信息。"""
    from app.core.tenant_context import get_tenant, get_role
    tid = get_tenant()
    role = get_role()
    if not tid:
        return {"tenant_id": None, "role": None, "mode": "single_key"}
    mgr = _mgr()
    tenant = mgr.get_tenant(tid)
    usage = mgr.get_usage(tid) if tenant else {}
    return {
        "tenant_id": tid,
        "role": role,
        "mode": "multi_tenant" if settings.default_tenant_enabled else "single_key",
        "tenant": tenant,
        "usage": usage,
    }
