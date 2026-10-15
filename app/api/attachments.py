"""附件接口:上传 / 列出 / 删除 当前会话的本地文件(供聊天注入上下文)。"""
from __future__ import annotations

import os

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.config import settings
from app.memory.attachments import attachment_store

router = APIRouter(prefix="/v1/attachments", tags=["attachments"])


@router.get("/images/{att_id}")
async def get_image(att_id: str, session_id: str = Query("default")) -> FileResponse:
    """返回已保存图片文件(供 server_url 模式使用)。"""
    # 优先按 session 精确定位,避免旧的全目录扫描
    from app.memory.attachments import attachment_store
    path = attachment_store.get_path(session_id, att_id)
    if path and os.path.isfile(path):
        return FileResponse(path, media_type="image/png", filename=os.path.basename(path))
    # 回退:全盘扫描 attachments / images
    for s in os.listdir(os.path.join(settings.resolved_data_dir, "attachments")):
        sd = os.path.join(settings.resolved_data_dir, "attachments", s)
        if os.path.isdir(sd):
            for f in os.listdir(sd):
                if f.startswith(att_id + "_") and os.path.isfile(os.path.join(sd, f)):
                    return FileResponse(os.path.join(sd, f))
    img_dir = os.path.join(settings.resolved_data_dir, "images")
    safe_name = os.path.basename(att_id)
    path = os.path.join(img_dir, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(path)


@router.get("/file/{att_id}")
async def download_file(att_id: str, session_id: str = Query("default")) -> FileResponse:
    """通用下载接口:按 session + att_id 精确返回附件原始文件(带下载文件名)。

    供前端在会话内"预览/打开"用户上传的 Office/PDF/文本等附件。
    """
    from app.memory.attachments import attachment_store
    path = attachment_store.get_path(session_id, att_id)
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"附件不存在: {att_id}")
    rec = next((a for a in attachment_store.list(session_id) if a["id"] == att_id), None)
    filename = rec["name"] if rec else os.path.basename(path)
    media_type = (rec or {}).get("mime") or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=filename)


@router.get("/file/{att_id}/path")
async def file_path(att_id: str, session_id: str = Query("default")) -> dict:
    """返回附件在磁盘上的绝对路径,供桌面端 electron shell.openPath 调系统程序打开。

    仅返回后端数据目录内文件,前端拿路径后交给主进程,主进程再做白名单校验,
    避免渲染进程被诱导打开任意系统文件。
    """
    from app.memory.attachments import attachment_store
    path = attachment_store.get_path(session_id, att_id)
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"附件不存在: {att_id}")
    return {"path": path, "name": os.path.basename(path)}


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
