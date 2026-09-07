"""文件版本管理接口:查看历史 / 恢复 / 撤销(供桌面端 UI 使用)。"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.file_versions import file_version_store
from app.tools.file_tool import _resolve_write_path

router = APIRouter(prefix="/v1/files", tags=["files"])

# 允许读取文件的基础目录:通过 _resolve_write_path 白名单控制


def _validate_read_path(path: str) -> str:
    """校验读取路径在允许的根目录内(防目录穿越)。"""
    full = os.path.abspath(os.path.expanduser(path))
    # 与写入路径共用同一白名单机制
    allowed_roots = [
        os.path.abspath(os.path.expanduser(d.strip()))
        for d in (os.environ.get("ALLOWED_READ_DIRS") or "").split(",")
        if d.strip()
    ]
    if not allowed_roots:
        # 默认只允许 data 目录
        allowed_roots = [os.path.abspath("data")]
    for root in allowed_roots:
        if full == root or full.startswith(root + os.sep):
            return full
    raise ValueError(
        f"路径不在允许读取目录内: {full}。允许目录: {', '.join(allowed_roots)}"
    )


@router.get("/history")
async def file_history(path: str = Query(..., description="文件路径")) -> dict:
    try:
        full = _validate_read_path(path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    versions = file_version_store.list_versions(full)
    return {"path": full, "versions": versions}


@router.get("/content")
async def file_content(path: str = Query(..., description="文件路径")) -> dict:
    """读取允许写入目录内文件的当前内容(供 diff 视图)。"""
    try:
        full = _resolve_write_path(path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    # 额外验证读取路径也在白名单内
    try:
        _validate_read_path(path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail=f"文件不存在: {full}")
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}") from e
    return {"path": full, "content": content}


@router.get("/version")
async def file_version(version_id: int = Query(..., description="版本 id")) -> dict:
    ver = file_version_store.get_version(version_id)
    if ver is None:
        raise HTTPException(status_code=404, detail=f"版本不存在: {version_id}")
    return ver


class RestoreRequest(BaseModel):
    path: str = Field(..., description="文件路径")
    version_id: int = Field(..., description="要恢复到的版本 id")


@router.post("/restore")
async def file_restore(req: RestoreRequest) -> dict:
    try:
        full = _resolve_write_path(req.path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    ver = file_version_store.get_version(req.version_id)
    if ver is None:
        raise HTTPException(status_code=404, detail=f"版本不存在: {req.version_id}")
    if ver["path"] != full:
        raise HTTPException(status_code=400, detail="版本与文件不匹配")
    if os.path.isfile(full):
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                cur = f.read()
            file_version_store.save_version(full, cur, "overwrite")
        except OSError:
            pass
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    try:
        if ver["action"] == "create" and not ver["content"]:
            if os.path.isfile(full):
                os.remove(full)
            return {"path": full, "restored_version": ver["id"], "action": "removed"}
        with open(full, "w", encoding="utf-8") as f:
            f.write(ver["content"])
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"恢复失败: {e}") from e
    return {"path": full, "restored_version": ver["id"], "bytes": len(ver["content"].encode("utf-8"))}


class UndoRequest(BaseModel):
    path: str = Field(..., description="文件路径")


@router.post("/undo")
async def file_undo(req: UndoRequest) -> dict:
    try:
        full = _resolve_write_path(req.path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    ver = file_version_store.latest_version(full)
    if ver is None:
        raise HTTPException(status_code=404, detail="没有可撤销的历史版本")
    try:
        if ver["action"] == "create" and not ver["content"]:
            if os.path.isfile(full):
                os.remove(full)
            file_version_store.delete_version(ver["id"])
            return {"path": full, "action": "removed", "restored_version": ver["id"]}
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(ver["content"])
        file_version_store.delete_version(ver["id"])
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"撤销失败: {e}") from e
    return {"path": full, "action": "restored", "restored_version": ver["id"], "bytes": len(ver["content"].encode("utf-8"))}


@router.get("/stats")
async def file_version_stats() -> dict:
    return file_version_store.stats()
