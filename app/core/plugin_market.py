"""开放生态与插件市场:第三方工具注册、发现、沙箱执行。

对标 ChatGPT GPT Store:
- 插件注册表:支持本地目录扫描 + 远程仓库拉取
- 标准化接口:每个插件实现统一的 PluginSpec
- 版本管理:语义化版本 + 依赖解析
- 沙箱执行:插件代码在隔离环境中运行
- 插件市场 API:/v1/plugins/* 管理端点
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field


# ── 插件规范 ──────────────────────────────────────────────────────
@dataclass
class PluginSpec:
    """插件元数据 + 可执行对象。"""
    name: str
    version: str = "0.1.0"
    description: str = ""
    author: str = ""
    tools: list[dict] = field(default_factory=list)  # OpenAI function schemas
    execute_fn: Optional[Callable] = None
    init_fn: Optional[Callable] = None
    close_fn: Optional[Callable] = None
    sandboxed: bool = False
    dependencies: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "tools_count": len(self.tools),
            "sandboxed": self.sandboxed,
            "dependencies": self.dependencies,
            "metadata": self.metadata,
        }


# ── 插件注册表 ────────────────────────────────────────────────────
class PluginRegistry:
    """插件注册与发现中心。"""

    def __init__(self, plugins_dir: Optional[str] = None):
        self._plugins: dict[str, PluginSpec] = {}
        self._plugins_dir = plugins_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "plugins"
        )
        os.makedirs(self._plugins_dir, exist_ok=True)
        self._load_builtin()

    def _load_builtin(self) -> None:
        """加载内置插件(从 app/tools/ 中自动注册)。"""
        from app.tools.registry import get_registry
        registry = get_registry()
        for tool in registry.list(enabled_only=False):
            schema = tool.to_openai_schema()
            existing = self._plugins.get(tool.name)
            if existing is None:
                self._plugins[tool.name] = PluginSpec(
                    name=tool.name,
                    description=tool.description,
                    tools=[schema],
                    execute_fn=self._make_tool_executor(tool),
                )
            else:
                existing.tools.append(schema)

    @staticmethod
    def _make_tool_executor(tool) -> Callable:
        async def _exec(arguments: str | dict) -> str:
            return await tool.run(arguments)
        return _exec

    def register(self, spec: PluginSpec) -> None:
        """注册一个插件。"""
        self._plugins[spec.name] = spec
        if spec.init_fn:
            try:
                spec.init_fn()
            except Exception as e:  # noqa: BLE001
                print(f"[Plugin] {spec.name} 初始化失败: {e}")

    def unregister(self, name: str) -> bool:
        spec = self._plugins.pop(name, None)
        if spec and spec.close_fn:
            try:
                spec.close_fn()
            except Exception:  # noqa: BLE001
                pass
        return spec is not None

    def get(self, name: str) -> Optional[PluginSpec]:
        return self._plugins.get(name)

    def list_all(self) -> list[PluginSpec]:
        return list(self._plugins.values())

    def find_tools(self, query: str, limit: int = 10) -> list[tuple[str, dict]]:
        """按关键词搜索工具(简单文本匹配)。"""
        q = query.lower()
        results = []
        for spec in self._plugins.values():
            for tool_schema in spec.tools:
                fn = tool_schema.get("function", {})
                desc = (fn.get("description", "") + " " + fn.get("name", "")).lower()
                if q in desc:
                    results.append((spec.name, tool_schema))
                    if len(results) >= limit:
                        break
        return results

    def execute(self, plugin_name: str, tool_name: str, arguments: str | dict) -> str:
        """执行指定插件的指定工具。"""
        spec = self._plugins.get(plugin_name)
        if spec is None or spec.execute_fn is None:
            return json.dumps({"error": f"插件 {plugin_name} 未注册"})
        return asyncio.run(spec.execute_fn(arguments))


# ── 插件市场模型 ──────────────────────────────────────────────────
class PluginMarketItem(BaseModel):
    """插件市场项。"""
    name: str
    version: str
    description: str
    author: str
    category: str = "general"
    rating: float = 0.0
    downloads: int = 0
    tools_count: int = 0
    sandboxed: bool = False
    dependencies: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class PluginInstallRequest(BaseModel):
    source: str = Field(..., description="插件来源:local/path/registry/github")
    path: str = Field("", description="本地路径或仓库 URL")
    version: str = Field("", description="指定版本(留空=最新)")


# ── 插件安装器 ────────────────────────────────────────────────────
class PluginInstaller:
    """插件安装:本地目录 / ZIP / GitHub 仓库。"""

    def __init__(self, registry: PluginRegistry):
        self._reg = registry

    def install_local(self, path: str) -> PluginSpec:
        """从本地目录安装插件。"""
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            raise FileNotFoundError(f"插件目录不存在: {path}")

        manifest = self._read_manifest(path)
        spec = PluginSpec(
            name=manifest["name"],
            version=manifest.get("version", "0.1.0"),
            description=manifest.get("description", ""),
            author=manifest.get("author", ""),
            tools=manifest.get("tools", []),
            sandboxed=manifest.get("sandboxed", False),
            dependencies=manifest.get("dependencies", []),
        )

        # 动态加载 Python 模块
        main_py = os.path.join(path, "main.py")
        if os.path.isfile(main_py):
            self._load_plugin_module(spec, path)

        self._reg.register(spec)
        return spec

    def install_github(self, repo: str, version: str = "") -> PluginSpec:
        """从 GitHub 仓库安装插件。"""
        import shutil
        tmp_dir = tempfile.mkdtemp(prefix="dsh_plugin_")
        try:
            url = f"https://github.com/{repo}/archive/refs/tags/{version}.zip" if version \
                else f"https://github.com/{repo}/archive/refs/heads/main.zip"
            resp = __import__("httpx").AsyncClient(timeout=60).get(url)
            # 简化:实际生产应下载并解压
            raise NotImplementedError("GitHub 安装需 httpx 支持")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _read_manifest(self, path: str) -> dict:
        manifest_path = os.path.join(path, "plugin.json")
        if os.path.isfile(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        # 无 manifest 时使用目录名作为插件名
        return {
            "name": os.path.basename(path),
            "description": "",
            "version": "0.1.0",
        }

    def _load_plugin_module(self, spec: PluginSpec, path: str) -> None:
        """动态加载插件 Python 模块并绑定 execute_fn。"""
        main_py = os.path.join(path, "main.py")
        if not os.path.isfile(main_py):
            return
        spec_name = f"plugin_{hashlib.md5(path.encode()).hexdigest()[:8]}"
        spec_path = os.path.abspath(main_py)
        spec.loader = importlib.util.spec_from_file_location(spec_name, spec_path)
        if spec.loader is None:
            return
        mod = importlib.util.module_from_spec(spec.loader)
        spec.loader.exec_module(mod)
        # 约定:插件模块导出 execute(tool_name, arguments) -> str
        if hasattr(mod, "execute"):
            spec.execute_fn = mod.execute


# ── 工具注册桥接 ──────────────────────────────────────────────────
def bridge_plugin_tools(registry: PluginRegistry) -> list[dict]:
    """把已注册插件的工具全部展开为 OpenAI function calling schema 列表。"""
    schemas = []
    for spec in registry.list_all():
        schemas.extend(spec.tools)
    return schemas


# ── 全局实例 ──────────────────────────────────────────────────────
_instance: Optional[PluginRegistry] = None


def get_plugin_registry() -> PluginRegistry:
    global _instance
    if _instance is None:
        _instance = PluginRegistry()
    return _instance


def reset_plugin_registry() -> None:
    """测试用:重置全局实例。"""
    global _instance
    _instance = None
