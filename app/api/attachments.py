"""附件接口:上传 / 列出 / 删除 当前会话的本地文件(供聊天注入上下文)。"""
from __future__ import annotations

import os

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.config import settings
from app.memory.attachments import attachment_store

router = APIRouter(prefix="/v1/attachments", tags=["attachments"])


@router.get("/images/{att_id}")
async def get_image(att_id: str) -> FileResponse:
    """返回已保存图片文件(供 server_url 模式使用)。"""
    img_dir = os.path.join(settings.resolved_data_dir, "images")
    # 只允许文件名中不含路径分隔符
    safe_name = os.path.basename(att_id)
    path = os.path.join(img_dir, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(path)


@router.post("")
async def upload_attachment(
    file: UploadFile = File(...),
    session_id: str = Form("default"),
) -> dict:
    content = await file.read()
    mime = (file.content_type or "") if file.content_type else ""
    rec = attachment_store.add(session_id, file.filename or "file", content, mime=mime)
    if "error" in rec:
        raise HTTPException(status_code=400, detail=rec["error"])
    return rec


@router.get("")
async def list_attachments(session_id: str = Query("default")) -> dict:
    items = attachment_store.list(session_id)
    return {"attachments": items, "total": len(items)}


@router.delete("/{att_id}")
async def delete_attachment(att_id: str, session_id: str = Query("default")) -> dict:
    ok = attachment_store.remove(session_id, att_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"未找到附件: {att_id}")
    return {"attachment_id": att_id, "deleted": True}


@router.delete("")
async def clear_attachments(session_id: str = Query("default")) -> dict:
    n = attachment_store.clear(session_id)
    return {"deleted": n, "cleared": True}
