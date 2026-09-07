"""工具热加载:监控工具目录,自动注册新工具无需重启服务。

对标 LangChain 的 tool auto-discovery:
- 扫描 app/tools/ 目录下所有 .py 文件
- 自动导入并触发 @tool 装饰器注册
- 文件变更时自动重新加载(监听 mtime)
- 提供 /v1/tools/reload API 手动触发
- 安全限制:只加载 app/tools/ 目录下的文件,不加载外部路径
"""
from __future__ import annotations

import importlib
import inspect
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from app.tools.registry import get_registry, ToolRegistry

logger = logging.getLogger(__name__)


class ToolHotLoader:
    """工具热加载器:自动发现、导入、注册新工具。"""

    def __init__(
        self,
        tool_dir: str = "",
        poll_interval: float = 5.0,
    ):
        self._tool_dir = tool_dir or os.path.join(
            os.path.dirname(__file__), "..", "tools"
        )
        # 规范化路径,确保是绝对路径
        self._tool_dir = os.path.abspath(self._tool_dir)
        self._poll_interval = poll_interval
        self._lock = threading.RLock()
        self._loaded_files: dict[str, float] = {}  # path -> mtime
        self._watch_thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        """启动后台监控线程。"""
        if self._watch_thread and self._watch_thread.is_alive():
            return
        self._running = True
        self._watch_thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="tool-hot-loader"
        )
        self._watch_thread.start()

    def stop(self) -> None:
        """停止监控。"""
        self._running = False
        if self._watch_thread:
            self._watch_thread.join(timeout=2.0)

    def _watch_loop(self) -> None:
        """后台轮询工具目录,检测新文件。"""
        while self._running:
            try:
                self._scan_and_load()
            except Exception:
                logger.exception("工具热加载器扫描异常,跳过本次轮询")
            time.sleep(self._poll_interval)

    def _scan_and_load(self) -> None:
        """扫描工具目录,加载新文件。"""
        tool_path = Path(self._tool_dir)
        if not tool_path.exists() or not tool_path.is_dir():
            return
        # 安全检查:确保扫描目录没有被篡改(路径必须是预期的绝对路径)
        resolved_tool_dir = tool_path.resolve()
        if not str(resolved_tool_dir).startswith(self._tool_dir):
            logger.warning("工具目录路径被篡改,拒绝加载: %s != %s", resolved_tool_dir, self._tool_dir)
            return

        for py_file in tool_path.glob("*.py"):
            # 跳过私有文件
            if py_file.name.startswith("_") or py_file.name == "__init__.py":
                continue
            # 安全检查:确保解析后仍在工具目录内
            try:
                resolved = py_file.resolve()
                if not str(resolved).startswith(str(resolved_tool_dir)):
                    logger.warning("跳过越界文件: %s", py_file.name)
                    continue
            except OSError:
                continue
            mtime = py_file.stat().st_mtime
            file_key = str(py_file.resolve())

            if file_key not in self._loaded_files or self._loaded_files[file_key] < mtime:
                self._loaded_files[file_key] = mtime
                self._try_import(py_file)

    def _try_import(self, py_file: Path) -> None:
        """尝试导入并注册工具模块。"""
        try:
            # 构造模块名
            rel = py_file.relative_to(Path(self._tool_dir).parent)
            module_name = ".".join(rel.with_suffix("").parts)
            full_name = f"app.{module_name}"

            if full_name in sys.modules:
                # 已加载,检查是否有新工具
                module = sys.modules[full_name]
                self._check_new_tools(module, full_name)
            else:
                # 首次加载
                spec = importlib.util.spec_from_file_location(full_name, py_file)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[full_name] = module
                    spec.loader.exec_module(module)
        except Exception as e:
            logger.warning("工具热加载失败 %s: %s", py_file.name, e)

    def _check_new_tools(self, module, module_name: str) -> None:
        """检查模块中是否有新注册的 @tool 装饰器(同步版本)。"""
        registry = get_registry()
        # 直接访问内部 dict,避免 async lock
        existing_names = {name for name in registry._tools}
        for name, obj in inspect.getmembers(module, inspect.isfunction):
            if hasattr(obj, "_tool_metadata") and obj._tool_metadata["name"] not in existing_names:
                # 直接注册到内部 dict
                registry._tools[obj._tool_metadata["name"]] = obj
                logger.info("工具热加载新工具: %s from %s", obj._tool_metadata["name"], module_name)

    def reload_all(self) -> dict:
        """强制重新加载所有工具,返回统计。"""
        with self._lock:
            registry = get_registry()
            old_count = len(registry._tools)
            # 清除已加载记录
            self._loaded_files.clear()
            self._scan_and_load()
            new_count = len(registry._tools)
            return {
                "old_count": old_count,
                "new_count": new_count,
                "added": new_count - old_count,
                "tool_dir": self._tool_dir,
                "message": "工具注册表已刷新" if new_count > old_count else "无新工具",
            }

    def get_status(self) -> dict:
        """获取热加载器状态。"""
        return {
            "running": self._running,
            "monitored_files": len(self._loaded_files),
            "tool_dir": self._tool_dir,
        }


# 全局单例
_hot_loader: Optional[ToolHotLoader] = None


def get_tool_hot_loader() -> ToolHotLoader:
    global _hot_loader
    if _hot_loader is None:
        _hot_loader = ToolHotLoader()
    return _hot_loader


def start_tool_hot_loader() -> None:
    """启动工具热加载(在 main.py lifespan 中调用)。"""
    loader = get_tool_hot_loader()
    loader.start()
    logger.info("工具热加载器已启动,监控目录: %s", loader._tool_dir)
