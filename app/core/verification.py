"""自动验证钩子:代码变更后自动运行 pytest/ruff/mypy 验证。

在工具执行写入文件后触发,确保交付物可运行、符合规范。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    """验证结果。"""
    success: bool
    stdout: str
    stderr: str
    duration: float
    checks: dict[str, bool]  # pytest/ruff/mypy 单项结果


class VerificationHook:
    """自动验证钩子管理器。"""

    def __init__(self, project_root: Optional[str] = None):
        self.project_root = Path(project_root or settings.resolved_data_dir).resolve()
        self._enabled = getattr(settings, "agent_verification_enabled", True)
        self._timeout = getattr(settings, "agent_verification_timeout", 120)
        self._last_run = 0.0
        self._cooldown = getattr(settings, "agent_verification_cooldown", 5.0)  # 秒
        self._file_patterns = [
            r".*\.py$",
        ]
        self._ignore_patterns = [
            r".*__pycache__.*",
            r".*\.pytest_cache.*",
            r".*\.mypy_cache.*",
            r".*\.ruff_cache.*",
            r".*test_.*\.py$",  # 测试文件本身不触发
            r".*_test\.py$",
        ]

    def should_verify(self, file_path: str) -> bool:
        """判断文件是否需要触发验证。"""
        if not self._enabled:
            return False
        path = Path(file_path)
        # 忽略模式匹配
        for pattern in self._ignore_patterns:
            if re.match(pattern, str(path)):
                return False
        # 匹配模式
        for pattern in self._file_patterns:
            if re.match(pattern, str(path)):
                return True
        return False

    async def verify(self, file_path: str) -> VerificationResult:
        """运行完整验证套件: pytest && ruff && mypy。"""
        if not self.should_verify(file_path):
            return VerificationResult(True, "", "", 0.0, {"skipped": True})

        # 冷却期防抖
        now = time.time()
        if now - self._last_run < self._cooldown:
            logger.debug("验证冷却中,跳过")
            return VerificationResult(True, "", "", 0.0, {"cooldown": True})

        self._last_run = now
        start = time.time()

        checks = {}
        all_stdout = []
        all_stderr = []

        # 1) pytest
        pytest_ok, out, err = await self._run_cmd(
            ["python", "-m", "pytest", "-x", "-q", "--tb=short"],
            cwd=self.project_root,
        )
        checks["pytest"] = pytest_ok
        all_stdout.append(out)
        all_stderr.append(err)

        # 2) ruff check
        ruff_ok, out, err = await self._run_cmd(
            ["ruff", "check", "."],
            cwd=self.project_root,
        )
        checks["ruff"] = ruff_ok
        all_stdout.append(out)
        all_stderr.append(err)

        # 3) mypy (仅检查变更文件所在包,避免全量慢)
        mypy_ok, out, err = await self._run_cmd(
            ["mypy", "--ignore-missing-imports", "."],
            cwd=self.project_root,
        )
        checks["mypy"] = mypy_ok
        all_stdout.append(out)
        all_stderr.append(err)

        duration = time.time() - start
        success = all(checks.values())

        logger.info(
            "验证完成: pytest=%s ruff=%s mypy=%s (%.1fs)",
            checks.get("pytest"), checks.get("ruff"), checks.get("mypy"), duration
        )

        return VerificationResult(
            success=success,
            stdout="\n".join(all_stdout),
            stderr="\n".join(all_stderr),
            duration=duration,
            checks=checks,
        )

    async def _run_cmd(self, cmd: list[str], cwd: Path, timeout: int = None) -> tuple[bool, str, str]:
        """异步运行命令并返回 (成功, stdout, stderr)。"""
        timeout = timeout or self._timeout
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode == 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            return False, "", f"命令超时 ({timeout}s): {' '.join(cmd)}"
        except FileNotFoundError:
            return False, "", f"命令未找到: {cmd[0]} (请安装对应工具)"
        except Exception as e:
            return False, "", f"执行异常: {e}"


# 全局单例
_verification_hook: Optional[VerificationHook] = None


def get_verification_hook(project_root: Optional[str] = None) -> VerificationHook:
    global _verification_hook
    if _verification_hook is None:
        _verification_hook = VerificationHook(project_root)
    return _verification_hook


async def run_verification_hook(file_path: str, project_root: Optional[str] = None) -> VerificationResult:
    """便捷函数:触发验证钩子。"""
    hook = get_verification_hook(project_root)
    return await hook.verify(file_path)