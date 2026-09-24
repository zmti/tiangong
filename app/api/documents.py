"""文档管理接口:上传文件建立知识库,查看知识库统计,重置索引。"""
from __future__ import annotations

import os

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from app.rag.documents import delete_document as delete_document_rag
from app.rag.documents import index_document
from app.rag.retriever import get_store
from app.config import settings

router = APIRouter(prefix="/v1/documents", tags=["documents"])
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB


@router.post("/upload")
async def upload_document(file: UploadFile = File(...)) -> dict:
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="文件超过 50MB 限制")

    result = index_document(file.filename, content)
    return result


@router.get("/stats")
async def document_stats() -> dict:
    from app.core.tenant_context import get_tenant
    tid = get_tenant() or "default"
    store = get_store()
    sources: dict[str, int] = {}
    cnt = 0
    # 走抽象层的公开遍历接口:不支持枚举的后端返回空列表,不会让只读统计接口 500
    for _cid, meta in store.iter_items(tid):
        src = meta.get("source", "unknown")
        sources[src] = sources.get(src, 0) + 1
        cnt += 1
    return {"documents": len(sources), "chunks": cnt, "by_source": sources}


@router.delete("")
async def delete_document(source: str = Query(..., description="要删除的文档文件名")) -> dict:
    """按文件名删除单个文档及其所有向量块。"""
    return delete_document_rag(source)


@router.delete("/all")
async def clear_documents() -> dict:
    from app.core.tenant_context import get_tenant
    tid = get_tenant() or "default"
    store = get_store()
    ids = [cid for cid, _meta in store.iter_items(tid)]
    for cid in ids:
        store.delete(cid)
    return {"cleared": True, "chunks": len(ids)}


@router.post("/reset")
async def reset_index(force: bool = False, confirm: str = "") -> dict:
    """重置整个向量索引(A9: 切换 Embedding 后端后必须调用,否则旧索引维度/语义与新后端不兼容)。

    force=true 时直接清空;否则需传入 confirm=1。
    同时清除 faiss 内存索引并从 SQLite 重新构建。
    """
    if not force and confirm != "1":
        raise HTTPException(
            status_code=400,
            detail="为防止误操作,请输入 confirm=1 确认重置,或传 force=true 强制重置",
        )
    from app.core.tenant_context import get_tenant
    tid = get_tenant() or "default"
    store = get_store()
    ids = [cid for cid, _meta in store.iter_items(tid)]
    count = len(ids)
    for cid in ids:
        store.delete(cid)
    # 清除 faiss 内存索引(下次 search 时自动重建)
    try:
        store._index = None  # noqa: SLF001
        store._int_ids.clear()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass
    return {
        "reset": True,
        "previous_chunks": count,
        "note": "向量索引已清空;重新上传文档将重建索引",
    }
