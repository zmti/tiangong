"""文件监听器(FileWatcher) 单元测试。"""
import os
import shutil
import time
import uuid

import pytest

from app.config import settings
from app.core.file_watcher import FileEvent, FileWatcher


def _mk_watch_dir() -> str:
    """在项目 data 目录下创建唯一子目录(避免权限问题)。"""
    d = os.path.join(settings.resolved_data_dir, f"_test_watch_{uuid.uuid4().hex[:8]}")
    os.makedirs(d, exist_ok=True)
    return d


def _cleanup(d: str) -> None:
    try:
        shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def test_watcher_detects_created_file():
    watch_dir = _mk_watch_dir()
    watcher = FileWatcher(poll_interval=0.2, debounce_sec=0.1)
    events: list[FileEvent] = []
    watcher.watch(watch_dir, lambda e: events.append(e))
    watcher.start()
    try:
        fp = os.path.join(watch_dir, "new.txt")
        with open(fp, "w") as f:
            f.write("hello")
        time.sleep(1.0)
        created = [e for e in events if e.event_type == "created" and e.path == fp]
        assert len(created) >= 1, f"未检测到创建事件,实际: {[(e.path, e.event_type) for e in events]}"
    finally:
        watcher.stop()
        _cleanup(watch_dir)


def test_watcher_detects_modified_file():
    watch_dir = _mk_watch_dir()
    watcher = FileWatcher(poll_interval=0.2, debounce_sec=0.1)
    events: list[FileEvent] = []
    watcher.watch(watch_dir, lambda e: events.append(e))
    watcher.start()
    try:
        fp = os.path.join(watch_dir, "target.txt")
        with open(fp, "w") as f:
            f.write("v1")
        time.sleep(0.8)
        events.clear()
        with open(fp, "w") as f:
            f.write("v2")
        time.sleep(1.0)
        modified = [e for e in events if e.event_type == "modified" and e.path == fp]
        assert len(modified) >= 1, f"未检测到修改事件,实际: {[(e.path, e.event_type) for e in events]}"
    finally:
        watcher.stop()
        _cleanup(watch_dir)


def test_watcher_detects_deleted_file():
    watch_dir = _mk_watch_dir()
    watcher = FileWatcher(poll_interval=0.2, debounce_sec=0.1)
    events: list[FileEvent] = []
    watcher.watch(watch_dir, lambda e: events.append(e))
    watcher.start()
    try:
        fp = os.path.join(watch_dir, "to_remove.txt")
        with open(fp, "w") as f:
            f.write("x")
        time.sleep(0.8)
        events.clear()
        os.remove(fp)
        time.sleep(1.0)
        deleted = [e for e in events if e.event_type == "deleted" and e.path == fp]
        assert len(deleted) >= 1, f"未检测到删除事件,实际: {[(e.path, e.event_type) for e in events]}"
    finally:
        watcher.stop()
        _cleanup(watch_dir)


def test_watcher_ignores_noise_files():
    watch_dir = _mk_watch_dir()
    watcher = FileWatcher(poll_interval=0.2, debounce_sec=0.1)
    events: list[FileEvent] = []
    watcher.watch(watch_dir, lambda e: events.append(e))
    watcher.start()
    try:
        for suffix in (".tmp", ".bak", ".swp"):
            with open(os.path.join(watch_dir, f"noise{suffix}"), "w") as f:
                f.write("x")
        time.sleep(1.0)
        noise_paths = [e.path for e in events if any(n in e.path for n in (".tmp", ".bak", ".swp"))]
        assert len(noise_paths) == 0, f"噪声文件未被过滤: {noise_paths}"
    finally:
        watcher.stop()
        _cleanup(watch_dir)


def test_unwatch_removes_callback():
    watch_dir = _mk_watch_dir()
    watcher = FileWatcher()
    callback = lambda e: None
    watcher.watch(watch_dir, callback)
    assert watch_dir in watcher.get_watched_dirs()
    ok = watcher.unwatch(watch_dir, callback)
    assert ok is True
    assert watch_dir not in watcher.get_watched_dirs()
    _cleanup(watch_dir)


def test_unwatch_all():
    watch_dir = _mk_watch_dir()
    watcher = FileWatcher()
    watcher.watch(watch_dir, lambda e: None)
    watcher.watch(watch_dir, lambda e: None)
    ok = watcher.unwatch(watch_dir)
    assert ok is True
    assert watch_dir not in watcher.get_watched_dirs()
    _cleanup(watch_dir)


def test_unwatch_nonexistent():
    watcher = FileWatcher()
    ok = watcher.unwatch("/definitely/not/a/real/dir/xyz123")
    assert ok is False


def test_watcher_invalid_dir():
    watcher = FileWatcher()
    with pytest.raises(ValueError, match="目录不存在"):
        watcher.watch("/nonexistent/path/abc123", lambda e: None)


def test_file_event_to_dict():
    evt = FileEvent("/tmp/x.txt", "modified", 1234.5)
    d = evt.to_dict()
    assert d["path"] == "/tmp/x.txt"
    assert d["event_type"] == "modified"
    assert d["timestamp"] == 1234.5


def test_watcher_is_running_property():
    watch_dir = _mk_watch_dir()
    watcher = FileWatcher()
    assert watcher.is_running is False
    watcher.watch(watch_dir, lambda e: None)
    watcher.start()
    assert watcher.is_running is True
    watcher.stop()
    assert watcher.is_running is False
    _cleanup(watch_dir)
