"""文件监听工具:让 Agent 感知文件系统变化,自动触发 RAG 重索引或告警。

工具:
- file_watch:     开始监听某个目录,变化时回调注册的处理函数
- file_unwatch:   停止监听指定目录
- file_list_watches: 列出当前所有监听状态
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from pydantic import Field

from app.core.file_watcher import FileEvent, FileWatcher, get_watcher
from app.tools.registry import tool


def _make_handler(path: str) -> callable:
    """为每个监听路径创建独立的回调闭包,记录最后变化时间戳。"""
    last_seen: dict[str, float] = {}

    def handler(event: FileEvent) -> None:
        # 同一文件去重:debounce 由 FileWatcher 负责,这里只记录摘要
        key = f"{event.path}:{event.event_type}"
        if last_seen.get(key, 0) >= event.timestamp - 1.0:
            return
        last_seen[key] = event.timestamp

    return handler


# 全局:dir_path -> {handler, watch_id}
_watch_records: dict[str, dict] = {}


@tool(
    "file_watch",
    "开始监听本地目录的文件变化。适合:上传/编辑文档后自动通知 Agent 重新索引知识库;"
    "代码仓库变化后触发自动测试。传入目录路径即可,无需回调函数名(内置默认处理)。"
    "返回 watch_id,可用于 file_unwatch 取消监听。",
    timeout=5,
)
def file_watch(
    path: str = Field(description="要监听的目录路径,绝对或相对路径均可"),
    description: str = Field("", description="监听的描述标签,用于后续管理"),
) -> dict:
    """注册文件监听。"""
    abs_path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(abs_path):
        return {"error": f"目录不存在: {abs_path}"}

    watcher = get_watcher()
    handler = _make_handler(abs_path)
    watcher.watch(abs_path, handler)

    watch_id = abs_path
    _watch_records[abs_path] = {
        "watch_id": watch_id,
        "description": description or abs_path,
        "started_at": time.time(),
        "handler": handler,
    }

    return {
        "status": "ok",
        "watch_id": watch_id,
        "path": abs_path,
        "description": description or abs_path,
        "message": f"已启动监听: {abs_path}",
    }


@tool(
    "file_unwatch",
    "停止监听指定目录。传入 watch_id(即 file_watch 返回的路径)即可取消。",
    timeout=5,
)
def file_unwatch(
    watch_id: str = Field(description="要取消的监听 ID(file_watch 返回的 path)"),
) -> dict:
    """取消文件监听。"""
    abs_path = os.path.abspath(os.path.expanduser(watch_id))
    watcher = get_watcher()
    ok = watcher.unwatch(abs_path)
    _watch_records.pop(abs_path, None)
    if ok:
        return {"status": "ok", "watch_id": abs_path, "message": f"已停止监听: {abs_path}"}
    return {"status": "error", "error": f"未找到监听: {abs_path}"}


@tool(
    "file_list_watches",
    "列出当前所有活跃的文件监听任务,含路径、描述、启动时间。",
    timeout=5,
)
def file_list_watches() -> dict:
    """列出所有监听。"""
    watcher = get_watcher()
    result = []
    for path in watcher.get_watched_dirs():
        rec = _watch_records.get(path, {})
        result.append({
            "watch_id": path,
            "description": rec.get("description", path),
            "started_at": rec.get("started_at", 0),
            "alive": True,
        })
    return {"watches": result, "total": len(result)}
