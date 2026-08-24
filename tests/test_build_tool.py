"""构建感知工具测试。"""
import os
import uuid

import pytest

from app.config import settings
from app.tools.build_tool import detect_build_system, run_build, run_test


def _mk_dir() -> str:
    d = os.path.join(settings.resolved_data_dir, f"_test_build_{uuid.uuid4().hex[:8]}")
    os.makedirs(d, exist_ok=True)
    return d


def test_detect_build_system_python():
    d = _mk_dir()
    try:
        with open(os.path.join(d, "requirements.txt"), "w") as f:
            f.write("pytest>=8.0\n")
        result = detect_build_system.func(d)
        assert result["detected"] in ("pip", "pip-setup", "pip-poetry")
    finally:
        try:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def test_detect_build_system_no_match():
    d = _mk_dir()
    try:
        result = detect_build_system.func(d)
        assert result.get("detected") is None
    finally:
        try:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def test_detect_build_system_invalid_dir():
    result = detect_build_system.func("/definitely/not/a/real/dir/xyz")
    assert "error" in result


def test_run_build_no_system():
    d = _mk_dir()
    try:
        result = run_build.func(d)
        assert "error" in result or result.get("ok") is False
    finally:
        try:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def test_run_test_no_test_system():
    d = _mk_dir()
    try:
        result = run_test.func(d)
        assert "error" in result or "未找到" in result.get("error", "")
    finally:
        try:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
