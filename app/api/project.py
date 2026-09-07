"""项目相关API接口。"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from fastapi import APIRouter, Body, Query

router = APIRouter(prefix="/v1/project", tags=["project"])


def _history_file() -> str:
    """返回项目历史持久化文件路径。"""
    try:
        from app.config import settings
        data_dir = settings.resolved_data_dir
    except Exception:
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "project_history.json")


def _load_history() -> list:
    """从文件加载项目历史，同时兼容旧的环境变量方式。"""
    # 优先从文件读取
    filepath = _history_file()
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # 回退到环境变量
    try:
        raw = os.environ.get("AGENT_PROJECT_HISTORY", "[]")
        return json.loads(raw)
    except Exception:
        return []


def _save_history(history: list) -> None:
    """保存项目历史到文件和环境变量。"""
    # 写文件
    filepath = _history_file()
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    # 同时写环境变量（兼容旧逻辑）
    os.environ["AGENT_PROJECT_HISTORY"] = json.dumps(history, ensure_ascii=False)


@router.get("/list")
async def list_projects() -> dict:
    """列出所有项目文件夹。"""
    return {"projects": _load_history()}


@router.get("/browse")
async def browse_project() -> dict:
    """返回当前工作目录作为默认项目（桌面端应使用 IPC 对话框）。"""
    cwd = os.getcwd()
    return {
        "path": cwd,
        "name": os.path.basename(cwd) or cwd,
        "is_default": True,
    }


@router.post("/switch")
async def switch_project(body: dict = Body(...)) -> dict:
    """切换当前项目，并更新项目历史。"""
    path = body.get("path", "").strip()
    if not path:
        return {"ok": False, "error": "路径不能为空"}
    # 标准化路径
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return {"ok": False, "error": f"目录不存在: {path}"}
    name = os.path.basename(path)
    # 加载现有历史
    history = _load_history()
    # 移除已存在的相同路径
    history = [p for p in history if p.get("path") != path]
    # 添加到开头
    history.insert(0, {"path": path, "name": name, "selected_at": time.time()})
    if len(history) > 20:
        history = history[:20]
    _save_history(history)
    return {"ok": True, "path": path, "name": name}


@router.post("/save-history")
async def save_project_history(body: dict = Body(...)) -> dict:
    """前端保存项目历史。"""
    projects = body.get("projects", [])
    for p in projects:
        p.setdefault("selected_at", time.time())
    _save_history(projects)
    return {"ok": True, "count": len(projects)}


@router.post("/remove")
async def remove_project(body: dict = Body(...)) -> dict:
    """从项目历史中删除指定项目。"""
    path = body.get("path", "").strip()
    if not path:
        return {"ok": False, "error": "路径不能为空"}
    path = os.path.abspath(path)
    history = _load_history()
    history = [p for p in history if p.get("path") != path]
    _save_history(history)
    return {"ok": True, "path": path}


@router.get("/history")
async def get_project_history() -> dict:
    """获取项目历史列表。"""
    return {"projects": _load_history()}
