"""会话归档/导出 API:JSONL 格式导入导出、TTL 自动归档。"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.config import settings
from app.core.session_archive import get_session_archiver

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@router.get("/list")
async def list_sessions() -> dict:
    """列出所有活跃会话及其基本信息。"""
    archiver = get_session_archiver()
    sessions = archiver.list_sessions()
    return {
        "sessions": sessions,
        "total": len(sessions),
        "total_messages": sum(s["message_count"] for s in sessions),
        "total_chars": sum(s["total_chars"] for s in sessions),
    }


@router.post("/{session_id}/archive")
async def archive_session(session_id: str, force: bool = False) -> dict:
    """触发会话归档。force=True 时忽略大小阈值。"""
    archiver = get_session_archiver()
    from app.memory.session import SessionStore
    store = SessionStore()
    try:
        messages = await store.get_history(session_id)
    except Exception:
        messages = []
    size = sum(len(str(m.get("content", ""))) for m in messages)
    if not force and not archiver.should_archive(session_id, size):
        return {
            "session_id": session_id,
            "archived": False,
            "reason": f"会话大小 {size} 字符未达归档阈值 {archiver._archive_threshold}",
        }
    path = archiver.archive_session(session_id, messages)
    # 归档后删除活跃记录
    store.clear(session_id)
    return {"session_id": session_id, "archived": True, "path": path, "message_count": len(messages)}


@router.get("/{session_id}/export")
async def export_session(session_id: str) -> dict:
    """导出会话为 JSONL 文件(可下载)。"""
    archiver = get_session_archiver()
    from app.memory.session import SessionStore
    store = SessionStore()
    try:
        messages = await store.get_history(session_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    if not messages:
        raise HTTPException(status_code=404, detail="会话无消息记录")
    path = archiver.export_session(session_id, messages)
    return {"session_id": session_id, "path": path, "message_count": len(messages)}


@router.get("/{session_id}/export/download")
async def download_export(session_id: str) -> FileResponse:
    """下载 JSONL 导出文件。"""
    archiver = get_session_archiver()
    from app.memory.session import SessionStore
    store = SessionStore()
    try:
        messages = await store.get_history(session_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    if not messages:
        raise HTTPException(status_code=404, detail="会话无消息记录")
    path = archiver.export_session(session_id, messages)
    return FileResponse(path, filename=f"{session_id}.jsonl", media_type="application/x-ndjson")


@router.post("/{session_id}/import")
async def import_session(session_id: str, file_path: str = Query(..., description="JSONL 文件绝对路径")) -> dict:
    """从 JSONL 文件导入会话消息(安全限制:仅限 data 目录内)。"""
    data_dir = settings.resolved_data_dir
    abs_path = os.path.abspath(file_path)
    if not abs_path.startswith(data_dir):
        raise HTTPException(status_code=403, detail="仅允许导入 data 目录内的文件")
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {abs_path}")
    archiver = get_session_archiver()
    messages = archiver.import_session(abs_path)
    if not messages:
        raise HTTPException(status_code=400, detail="文件中无有效消息")
    from app.memory.session import SessionStore
    store = SessionStore()
    for m in messages:
        await store.add_message(session_id, m.get("role", "user"), m.get("content", ""))
    return {"session_id": session_id, "imported": len(messages)}


@router.get("/archives")
async def list_archives() -> dict:
    """列出所有归档的会话。"""
    archiver = get_session_archiver()
    archives = archiver.list_archives()
    return {"archives": archives, "total": len(archives)}


@router.post("/cleanup")
async def cleanup_expired(max_age_days: int = Query(90, ge=1, le=365)) -> dict:
    """清理超过指定天数的归档文件和活跃会话。"""
    archiver = get_session_archiver()
    archiver._retention_days = max_age_days
    cleaned = archiver.cleanup_expired()
    return {"cleaned": cleaned, "retention_days": max_age_days}


@router.post("/auto-cleanup")
async def auto_cleanup(force: bool = False) -> dict:
    """执行自动清理(归档大会话 + 清理过期会话)。force=True 时立即执行。

    默认情况下，每次请求只记录统计信息；force=True 时实际执行清理。
    后台会自动每 session_cleanup_interval_seconds 秒执行一次。
    """
    archiver = get_session_archiver()
    sessions = archiver.list_sessions()
    stats = {
        "active_sessions": len(sessions),
        "total_messages": sum(s["message_count"] for s in sessions),
        "total_chars": sum(s["total_chars"] for s in sessions),
        "threshold_chars": archiver._archive_threshold,
        "retention_days": archiver._retention_days,
        "need_archive": sum(1 for s in sessions if s["total_chars"] >= archiver._archive_threshold),
        "need_cleanup": sum(1 for s in sessions if s["age_days"] > archiver._retention_days),
    }
    if force:
        cleaned = await archiver.auto_cleanup()
        stats["cleaned_this_run"] = cleaned
        return {"stats": stats, "action": "force_cleanup", "cleaned": cleaned}
    return {"stats": stats, "action": "dry_run"}


@router.get("/auto-cleanup/status")
async def cleanup_status() -> dict:
    """查看自动清理状态（后台任务是否运行、上次清理结果）。"""
    archiver = get_session_archiver()
    return {
        "background_running": archiver._running,
        "cleanup_interval_seconds": archiver._cleanup_interval,
        "retention_days": archiver._retention_days,
        "archive_threshold_chars": archiver._archive_threshold,
        "total_archived": len(archiver.list_archives()),
    }
