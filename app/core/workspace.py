"""Docker 持久化工作空间:跨调用保持环境状态

对标主流 Agent:
- Devin: 持久化工作空间，可安装依赖、创建文件
- OpenHands: Docker 沙箱，支持持久化
- SWE-Agent: 一次性沙箱（对比）

核心能力:
1. 容器生命周期管理（创建、停止、重启）
2. 文件系统持久化（volume 挂载）
3. 环境快照和恢复
4. 资源限制（CPU、内存、磁盘）
5. 网络隔离
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class WorkspaceState(str, Enum):
    """工作空间状态"""
    CREATED = "created"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class WorkspaceConfig:
    """工作空间配置"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    image: str = "python:3.11-slim"
    workspace_dir: str = "/workspace"
    volumes: dict = field(default_factory=dict)  # {host_path: container_path}
    env_vars: dict = field(default_factory=dict)
    memory_limit: str = "512m"
    cpu_limit: float = 1.0
    network_enabled: bool = True
    auto_remove: bool = False
    created_at: float = field(default_factory=time.time)


@dataclass
class Workspace:
    """工作空间实例"""
    config: WorkspaceConfig
    state: WorkspaceState = WorkspaceState.CREATED
    container_id: Optional[str] = None
    container_name: str = ""
    host_workspace_dir: str = ""
    error_message: str = ""
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.config.id,
            "name": self.config.name,
            "state": self.state.value,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "host_workspace_dir": self.host_workspace_dir,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


class PersistentWorkspace:
    """持久化工作空间管理器"""

    def __init__(self, base_dir: str = "workspaces"):
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._workspaces: dict[str, Workspace] = {}
        self._lock = threading.Lock()
        self._docker_available = self._check_docker()

    def _check_docker(self) -> bool:
        """检查 Docker 是否可用"""
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5,
                text=True,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def create_workspace(self, config: Optional[WorkspaceConfig] = None) -> Workspace:
        """创建工作空间"""
        if config is None:
            config = WorkspaceConfig()
        
        workspace_dir = self._base_dir / config.id
        workspace_dir.mkdir(parents=True, exist_ok=True)
        
        workspace = Workspace(
            config=config,
            host_workspace_dir=str(workspace_dir),
            container_name=f"agent-workspace-{config.id[:8]}",
        )
        
        with self._lock:
            self._workspaces[config.id] = workspace
        
        if self._docker_available:
            self._create_docker_container(workspace)
        else:
            # 非 Docker 模式，使用本地目录
            workspace.state = WorkspaceState.RUNNING
            logger.info(f"创建本地工作空间: {workspace_dir}")
        
        return workspace

    def _create_docker_container(self, workspace: Workspace) -> None:
        """创建 Docker 容器"""
        config = workspace.config
        
        cmd = [
            "docker", "run",
            "-d",
            "--name", workspace.container_name,
            "-v", f"{workspace.host_workspace_dir}:{config.workspace_dir}",
            "-m", config.memory_limit,
            "--cpus", str(config.cpu_limit),
            "--label", f"agent-workspace={config.id}",
        ]
        
        if not config.network_enabled:
            cmd.append("--network=none")
        
        for key, value in config.env_vars.items():
            cmd.extend(["-e", f"{key}={value}"])
        
        if config.auto_remove:
            cmd.append("--rm")
        
        cmd.append(config.image)
        cmd.extend(["sleep", "infinity"])
        
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=30, text=True)
            if result.returncode == 0:
                workspace.container_id = result.stdout.strip()
                workspace.state = WorkspaceState.RUNNING
                logger.info(f"创建 Docker 容器: {workspace.container_name}")
            else:
                workspace.state = WorkspaceState.ERROR
                workspace.error_message = result.stderr
                logger.error(f"创建 Docker 容器失败: {result.stderr}")
        except Exception as e:
            workspace.state = WorkspaceState.ERROR
            workspace.error_message = str(e)
            logger.error(f"创建 Docker 容器异常: {e}")

    def get_workspace(self, workspace_id: str) -> Optional[Workspace]:
        """获取工作空间"""
        with self._lock:
            workspace = self._workspaces.get(workspace_id)
            if workspace:
                workspace.last_used_at = time.time()
            return workspace

    def stop_workspace(self, workspace_id: str) -> bool:
        """停止工作空间"""
        workspace = self.get_workspace(workspace_id)
        if not workspace:
            return False
        
        if workspace.container_id and self._docker_available:
            try:
                subprocess.run(
                    ["docker", "stop", workspace.container_name],
                    capture_output=True,
                    timeout=10,
                )
            except Exception as e:
                logger.warning(f"停止 Docker 容器失败: {e}")
        
        workspace.state = WorkspaceState.STOPPED
        return True

    def delete_workspace(self, workspace_id: str) -> bool:
        """删除工作空间"""
        workspace = self.get_workspace(workspace_id)
        if not workspace:
            return False
        
        # 停止容器
        if workspace.state == WorkspaceState.RUNNING:
            self.stop_workspace(workspace_id)
        
        # 删除容器
        if workspace.container_id and self._docker_available:
            try:
                subprocess.run(
                    ["docker", "rm", workspace.container_name],
                    capture_output=True,
                    timeout=10,
                )
            except Exception as e:
                logger.warning(f"删除 Docker 容器失败: {e}")
        
        # 删除本地目录
        workspace_dir = Path(workspace.host_workspace_dir)
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir, ignore_errors=True)
        
        with self._lock:
            if workspace_id in self._workspaces:
                del self._workspaces[workspace_id]
        
        return True

    def execute_in_workspace(
        self,
        workspace_id: str,
        command: str,
        timeout: int = 60,
    ) -> dict:
        """在工作空间中执行命令"""
        workspace = self.get_workspace(workspace_id)
        if not workspace:
            return {"error": "工作空间不存在"}
        
        if workspace.state != WorkspaceState.RUNNING:
            return {"error": f"工作空间状态: {workspace.state.value}"}
        
        if workspace.container_id and self._docker_available:
            return self._execute_in_docker(workspace, command, timeout)
        else:
            return self._execute_locally(workspace, command, timeout)

    def _execute_in_docker(
        self,
        workspace: Workspace,
        command: str,
        timeout: int,
    ) -> dict:
        """在 Docker 容器中执行命令"""
        try:
            result = subprocess.run(
                [
                    "docker", "exec",
                    workspace.container_name,
                    "bash", "-c", command,
                ],
                capture_output=True,
                timeout=timeout,
                text=True,
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
                "success": result.returncode == 0,
            }
        except subprocess.TimeoutExpired:
            return {"error": "执行超时", "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}

    def _execute_locally(
        self,
        workspace: Workspace,
        command: str,
        timeout: int,
    ) -> dict:
        """在本地执行命令(使用 shlex.split 避免 shell=True)。"""
        import shlex

        # 将命令字符串拆分为参数列表，避免 shell 注入
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return {"error": f"命令解析失败: {e}", "success": False}
        if not argv:
            return {"error": "命令不能为空", "success": False}

        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                timeout=timeout,
                text=True,
                cwd=workspace.host_workspace_dir,
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
                "success": result.returncode == 0,
            }
        except subprocess.TimeoutExpired:
            return {"error": "执行超时", "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}

    def list_workspaces(self) -> list[dict]:
        """列出所有工作空间"""
        with self._lock:
            return [ws.to_dict() for ws in self._workspaces.values()]

    def cleanup_stale(self, max_age_hours: float = 24.0) -> int:
        """清理过期的工作空间"""
        cutoff = time.time() - max_age_hours * 3600
        to_delete = []
        
        with self._lock:
            for ws_id, ws in self._workspaces.items():
                if ws.last_used_at < cutoff:
                    to_delete.append(ws_id)
        
        for ws_id in to_delete:
            self.delete_workspace(ws_id)
        
        return len(to_delete)


# 单例
_workspace_manager: Optional[PersistentWorkspace] = None


def get_workspace_manager() -> PersistentWorkspace:
    """获取工作空间管理器单例"""
    global _workspace_manager
    if _workspace_manager is None:
        from app.config import settings
        _workspace_manager = PersistentWorkspace(
            base_dir=os.path.join(settings.resolved_data_dir, "workspaces")
        )
    return _workspace_manager
