"""跨平台文件监听器:轮询 mtime 检测目录变化,触发回调。

设计要点:
- 纯标准库实现,零外部依赖(不引入 watchdog)
- 跨平台:Windows/macOS/Linux 通用
- 多目录并发监听,线程安全
- 防重复触发:同一文件的变化在 debounce 窗口内只触发一次
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class FileEvent:
    """一次文件变化的事件。"""

    def __init__(self, path: str, event_type: str, timestamp: float):
        self.path = path
        # "created" / "modified" / "deleted"
        self.event_type = event_type
        self.timestamp = timestamp

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
        }


class FileWatcher:
    """轻量级目录监听器:定期扫描 mtime,触发注册回调。"""

    # 默认扫描间隔(秒),兼顾响应速度与 CPU 开销
    DEFAULT_POLL_INTERVAL = 2.0
    # 防抖窗口(秒):同一文件在此窗口内只触发一次回调
    DEFAULT_DEBOUNCE_SEC = 5.0

    def __init__(
        self,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        debounce_sec: float = DEFAULT_DEBOUNCE_SEC,
    ):
        self._poll_interval = max(0.5, poll_interval)
        self._debounce_sec = max(0.1, debounce_sec)
        # dir_path -> set of (path, mtime) snapshots
        self._snapshots: dict[str, dict[str, float]] = {}
        # dir_path -> list of callbacks(path, event_type)
        self._callbacks: dict[str, list[Callable]] = {}
        # 防抖: path -> last_fire_timestamp
        self._debounce: dict[str, float] = {}
        self._running = False
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None

    def watch(
        self,
        directory: str,
        on_event: Callable[[FileEvent], None],
        recursive: bool = True,
    ) -> None:
        """注册对某个目录的监听。on_event 接收 FileEvent 对象。"""
        abs_dir = os.path.abspath(os.path.expanduser(directory))
        if not os.path.isdir(abs_dir):
            raise ValueError(f"目录不存在: {abs_dir}")

        with self._lock:
            self._snapshots.setdefault(abs_dir, {})
            self._callbacks.setdefault(abs_dir, []).append(on_event)

    def unwatch(self, directory: str, on_event: Optional[Callable] = None) -> bool:
        """移除监听。不传 on_event 则移除该目录全部回调。"""
        abs_dir = os.path.abspath(os.path.expanduser(directory))
        with self._lock:
            if abs_dir not in self._snapshots:
                return False
            if on_event is None:
                self._snapshots.pop(abs_dir, None)
                self._callbacks.pop(abs_dir, None)
                return True
            cbs = self._callbacks.get(abs_dir, [])
            if on_event in cbs:
                cbs.remove(on_event)
                if not cbs:
                    self._callbacks.pop(abs_dir, None)
                    self._snapshots.pop(abs_dir, None)
                return True
            return False

    def start(self) -> None:
        """启动后台扫描线程。幂等:已运行则忽略。"""
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._scan_loop, daemon=True, name="file-watcher")
        self._thread.start()

    def stop(self) -> None:
        """停止扫描线程。"""
        with self._lock:
            self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _scan_loop(self) -> None:
        while self._running:
            try:
                self._scan_once()
            except Exception:
                logger.exception("文件监听器扫描异常,跳过本次轮询")
            time.sleep(self._poll_interval)

    def _scan_once(self) -> None:
        with self._lock:
            dirs_to_scan = list(self._snapshots.keys())

        for dir_path in dirs_to_scan:
            current = self._build_snapshot(dir_path)
            old = self._snapshots.get(dir_path, {})
            self._snapshots[dir_path] = current

            events = self._diff_events(old, current, dir_path)
            for event in events:
                self._maybe_fire(dir_path, event)

    def _build_snapshot(self, dir_path: str) -> dict[str, float]:
        """遍历目录,返回 path -> mtime 快照。"""
        snap: dict[str, float] = {}
        try:
            for root, _dirs, files in os.walk(dir_path):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        snap[fp] = os.path.getmtime(fp)
                    except OSError:
                        pass
        except OSError:
            pass
        return snap

    @staticmethod
    def _diff_events(
        old: dict[str, float], new: dict[str, float], root: str
    ) -> list[FileEvent]:
        """对比两次快照,产出变化事件列表。"""
        events: list[FileEvent] = []
        now = time.time()
        # 新增 & 修改
        for path, mtime in new.items():
            if path not in old:
                events.append(FileEvent(path, "created", now))
            elif old[path] != mtime:
                events.append(FileEvent(path, "modified", now))
        # 删除
        for path in old:
            if path not in new:
                events.append(FileEvent(path, "deleted", now))
        return events

    def _maybe_fire(self, dir_path: str, event: FileEvent) -> None:
        """防抖后触发回调。"""
        # 跳过隐藏目录和常见噪声
        if self._is_noise(event.path):
            return

        last = self._debounce.get(event.path, 0.0)
        if time.time() - last < self._debounce_sec:
            return
        self._debounce[event.path] = time.time()

        with self._lock:
            cbs = list(self._callbacks.get(dir_path, []))
        for cb in cbs:
            try:
                cb(event)
            except Exception:
                logger.warning("文件监听回调异常,继续其他回调: %s", e)

    @staticmethod
    def _is_noise(path: str) -> bool:
        """过滤明显噪声文件(临时文件、缓存、IDE 配置等)。"""
        from pathlib import Path

        p = Path(path)
        suffix = p.suffix.lower()
        stem = p.stem
        # 编辑器临时文件(含后缀模式如 .tmp, .bak, .swp 以及尾缀 ~)
        if suffix in (".tmp", ".bak", ".swp", ".temp") or stem.endswith("~"):
            return True
        # 常见缓存/忽略目录
        noisy_parent = any(
            n in path.lower()
            for n in (".cache/", "__pycache__", "node_modules/.cache", "/.git/")
        )
        return noisy_parent

    @property
    def is_running(self) -> bool:
        return self._running

    def get_watched_dirs(self) -> list[str]:
        with self._lock:
            return list(self._snapshots.keys())


# 全局单例
_watcher: Optional[FileWatcher] = None
_watcher_lock = threading.Lock()


def get_watcher() -> FileWatcher:
    """获取全局 FileWatcher 单例(懒加载,自动启动)。"""
    global _watcher
    if _watcher is None:
        with _watcher_lock:
            if _watcher is None:
                _watcher = FileWatcher()
                _watcher.start()
    return _watcher


def shutdown_watcher() -> None:
    """进程退出时调用,停止监听。"""
    global _watcher
    if _watcher is not None:
        _watcher.stop()
        _watcher = None
