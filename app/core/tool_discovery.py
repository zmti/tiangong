"""工具自动发现与注册系统。

支持：
1. 自动扫描 app/tools/ 目录
2. 基于装饰器的工具注册
3. 动态加载外部工具模块
4. 工具版本管理
"""
from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from pathlib import Path
from typing import Any, Callable, Optional

from app.tools.registry import get_registry

logger = logging.getLogger(__name__)


class ToolDiscovery:
    """工具发现器。"""
    
    def __init__(self, tools_dir: str = "app/tools"):
        self.tools_dir = Path(tools_dir)
        self._discovered: dict[str, dict] = {}
    
    def discover(self) -> dict[str, dict]:
        """发现所有工具。"""
        discovered = {}
        
        # 扫描工具目录
        for module_info in pkgutil.iter_modules([self.tools_dir]):
            if module_info.name.startswith('_'):
                continue
            
            try:
                module = importlib.import_module(
                    f"{self.tools_dir.replace('/', '.')}.{module_info.name}"
                )
                
                # 扫描模块中的工具函数
                for name, obj in inspect.getmembers(module):
                    if hasattr(obj, '_tool_meta'):
                        meta = obj._tool_meta
                        discovered[name] = {
                            "name": meta["name"],
                            "description": meta["description"],
                            "module": module_info.name,
                            "version": meta.get("version", "1.0.0"),
                            "timeout": meta.get("timeout", 30)
                        }
            except Exception as e:
                logger.warning(f"加载工具模块 {module_info.name} 失败: {e}")
        
        self._discovered = discovered
        return discovered
    
    def get_tool(self, name: str) -> Optional[dict]:
        """获取单个工具信息。"""
        return self._discovered.get(name)
    
    def list_tools(self) -> list[dict]:
        """列出所有工具。"""
        return list(self._discovered.values())
    
    def count(self) -> int:
        """工具数量。"""
        return len(self._discovered)


class ToolLoader:
    """工具加载器。"""
    
    def __init__(self, discovery: ToolDiscovery):
        self.discovery = discovery
    
    def load_all(self) -> int:
        """加载所有发现的工具。"""
        discovered = self.discovery.discover()
        loaded = 0
        
        for name, meta in discovered.items():
            try:
                # 获取实际函数
                module_path = f"{self.discovery.tools_dir.replace('/', '.')}.{meta['module']}"
                module = importlib.import_module(module_path)
                func = getattr(module, name)
                
                # 注册到全局注册表
                register_tool(func)
                loaded += 1
            except Exception as e:
                logger.error(f"加载工具 {name} 失败: {e}")
        
        return loaded


class DynamicToolLoader:
    """动态工具加载器（支持外部工具）。"""
    
    def __init__(self, external_tools_dir: str = "external_tools"):
        self.external_tools_dir = Path(external_tools_dir)
        self._loaded_modules: dict[str, Any] = {}
    
    def load_external_tool(self, tool_name: str) -> bool:
        """动态加载外部工具。"""
        tool_path = self.external_tools_dir / f"{tool_name}.py"
        
        if not tool_path.exists():
            logger.warning(f"外部工具不存在: {tool_path}")
            return False
        
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                f"external_tool_{tool_name}",
                tool_path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            
            self._loaded_modules[tool_name] = module
            return True
        except Exception as e:
            logger.error(f"加载外部工具 {tool_name} 失败: {e}")
            return False
    
    def unload_tool(self, tool_name: str) -> bool:
        """卸载外部工具。"""
        if tool_name in self._loaded_modules:
            del self._loaded_modules[tool_name]
            return True
        return False
    
    def list_loaded(self) -> list[str]:
        """列出已加载的外部工具。"""
        return list(self._loaded_modules.keys())
