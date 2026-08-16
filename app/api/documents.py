"""文档管理接口:上传文件建立知识库,查看知识库统计。"""
from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from app.rag.documents import delete_document as delete_document_rag
from app.rag.documents import index_document
from app.rag.retriever import get_store

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
    store = get_store()
    sources: dict[str, int] = {}
    for meta in store._metas:  # noqa: SLF001
        src = meta.get("source", "unknown")
        sources[src] = sources.get(src, 0) + 1
    return {"documents": len(sources), "chunks": store.count(), "by_source": sources}


@router.delete("")
async def delete_document(source: str = Query(..., description="要删除的文档文件名")) -> dict:
    """按文件名删除单个文档及其所有向量块。"""
    return delete_document_rag(source)


@router.delete("/all")
async def clear_documents() -> dict:
    store = get_store()
    store.clear()
    return {"cleared": True, "chunks": 0}
