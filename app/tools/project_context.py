"""项目路径上下文:通过 ContextVar 让工具在运行时感知当前项目目录。"""
from __future__ import annotations

import contextvars
import os

_project_path: contextvars.ContextVar[str] = contextvars.ContextVar("project_path", default="")


def set_project_path(path: str) -> None:
    _project_path.set(path)


def get_project_path() -> str:
    return _project_path.get()


def resolve_cwd() -> str:
    """获取当前工作目录:项目路径 > settings.terminal_cwd > os.getcwd()。"""
    p = get_project_path()
    if p and os.path.isdir(p):
        import logging as _log
        _log.getLogger("project").debug("resolve_cwd -> %s", p)
        return p
    return os.getcwd()
