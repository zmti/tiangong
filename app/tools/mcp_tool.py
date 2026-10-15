"""MCP 工具包装器:将远程 MCP 工具注册为本地工具。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import Field

from app.tools.registry import Tool, ToolRegistry, get_registry, tool


class MCPToolWrapper:
    """将 MCP 远程工具包装为本地工具。"""
    
    def __init__(
        self,
        name: str,
        description: str,
        mcp_client,
        mcp_tool_name: str,
        input_schema: dict,
        timeout: float = 30.0,
    ):
        self.name = name
        self.description = description
        self.mcp_client = mcp_client
        self.mcp_tool_name = mcp_tool_name
        self.input_schema = input_schema
        self.timeout = timeout
        self.enabled = True
        self.terminal = False
        self.is_async = True
    
    def to_openai_schema(self) -> dict:
        """转换为 OpenAI function calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            }
        }
    
    async def run(self, arguments: str | dict) -> str:
        """执行远程 MCP 工具。"""
        import json
        
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as e:
                return json.dumps({"error": f"参数解析失败: {e}"}, ensure_ascii=False)
        else:
            args = arguments
        
        result = await self.mcp_client.call_tool(self.mcp_tool_name, args)
        
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)


# ---- MCP 工具描述中文化(路由辅助) ----
# 英文 MCP 服务(windows-mcp/github 等)的工具名与描述都是英文,而用户 query 多为中文,
# 关键词路由层对「中文 query vs 英文工具」失配,mcp_ 工具永远选不进候选集,
# 导致 Agent「不会找 mcp 服务」。这里按工具名匹配能力词典,把中文关键词补进描述,
# 让中文 query 能在关键词层命中(对未来接入的任何英文 MCP 通用)。
_MCP_CAP_CN: dict[str, str] = {
    "screenshot": "截图 截屏 屏幕 桌面 界面",
    "snapshot": "截图 屏幕 界面 快照",
    "click": "点击 点一下 双击 鼠标",
    "type": "输入 打字 键盘 键入",
    "scroll": "滚动 翻页 滚屏",
    "move": "移动 拖拽",
    "app": "应用 软件 程序 窗口 打开",
    "process": "进程 任务 后台",
    "powershell": "PowerShell 终端 命令行 命令",
    "terminal": "终端 命令行 命令",
    "file": "文件 目录 磁盘 读写",
    "registry": "注册表",
    "clipboard": "剪贴板 复制 粘贴",
    "display": "显示器 屏幕 分辨率",
    "notification": "通知 弹窗 提醒",
    "scrape": "抓取 爬取 网页",
    "shortcut": "快捷键",
    "wait": "等待",
    "issue": "issue 工单 问题 反馈",
    "pull_request": "PR 合并请求 代码评审",
    "repo": "仓库 代码库",
    "repository": "仓库 代码库",
    "commit": "提交 git 版本",
    "branch": "分支",
    "merge": "合并",
    "fork": "fork 分叉",
    "webhook": "webhook 钩子",
    # 数据库/查询类 MCP(如 postgres 的 query 工具):按工具名子串匹配,
    # 让"查数据库/查一下xx表"等中文诉求能在关键词层命中 mcp_query。
    # 2026-10-04 追加"记录数/多少条/统计"等说法——查表行数时的常用措辞,
    # 此前这类 query 会被 query_sqlite 等内置工具抢走,mcp_query 漏选。
    "query": "查询 查 数据库 数据 表 sql 记录数 条数 统计 数量 多少条",
    "database": "数据库 库",
    "sql": "sql 查询 数据库 表 记录数 统计",
}


def _augment_mcp_description(raw_desc: str, mcp_tool_name: str) -> str:
    """给英文 MCP 工具描述补中文关键词,使中文 query 能路由命中 mcp_ 工具。"""
    name_l = mcp_tool_name.lower()
    desc_l = raw_desc.lower()
    hits: list[str] = []
    for cap, cn in _MCP_CAP_CN.items():
        if cap in name_l:
            for word in cn.split():
                wl = word.lower()
                # 描述里已有(英文/中文)就跳过,避免重复
                if wl in desc_l or word in raw_desc or word in hits:
                    continue
                hits.append(word)
    if hits:
        return f"{raw_desc}\n[支持: {' '.join(hits)}]"
    return raw_desc


def _usage_hint(mcp_tool_name: str, input_schema: dict | None) -> str:
    """给 MCP 工具描述追加"调用参数 + 用法"一行说明。

    目的: 模型在候选集里看到工具时, 参数名与用法一目了然, 不会凭'记忆'编出
    不存在的参数(如把 query_sqlite 的 db_path / 其他工具的 mode 串到 mcp_query 上)。
    参数列表**直接取自真实 inputSchema**, 绝不臆造。
    """
    props = (input_schema or {}).get("properties") or {}
    parts: list[str] = []
    for pname, pdef in props.items():
        ptype = pdef.get("type", "any") if isinstance(pdef, dict) else "any"
        parts.append(f"{pname}({ptype})")
    if not parts:
        return ""
    line = f"[调用参数: {', '.join(parts)}]"
    # 数据库/查询类工具额外提示"直接传完整 SQL、表名不确定先查 information_schema",
    # 减少"记错表名 / 编错参数"的幻觉。
    low = mcp_tool_name.lower()
    if any(k in low for k in ("query", "sql", "database", "db")):
        line += " 直接传完整 SQL(仅 SELECT 只读); 表名/库不确定时先查 information_schema.tables"
    return line


def register_mcp_tools(mcp_manager=None) -> int:
    """
    将所有 MCP 工具注册为本地工具。

    Args:
        mcp_manager: MCP 管理器实例,如果为 None 则使用全局管理器

    Returns:
        注册的工具数量
    """
    if mcp_manager is None:
        from app.core.mcp_client import get_mcp_manager
        mcp_manager = get_mcp_manager()

    registry = get_registry()
    count = 0

    for mcp_tool in mcp_manager.list_all_tools():
        tool_name = mcp_tool.get("name", "")
        if not tool_name:
            continue

        # 跳过已存在的工具
        if registry.get(f"mcp_{tool_name}") is not None:
            continue

        # 描述中文化:补中文能力关键词,让中文 query 能路由命中 mcp_ 工具
        raw_desc = mcp_tool.get("description", f"MCP 工具: {tool_name}")
        augmented_desc = _augment_mcp_description(raw_desc, tool_name)

        # 追加"调用参数 + 用法"说明(参数取自真实 inputSchema,杜绝模型编错参数名)
        input_schema = mcp_tool.get("inputSchema", {"type": "object", "properties": {}})
        usage = _usage_hint(tool_name, input_schema)
        if usage:
            augmented_desc = f"{augmented_desc}\n{usage}"

        # 创建工具包装器
        client = mcp_manager._tools_map[tool_name][1]
        # 超时层级必须「内层先超、外层兜底」:底层 MCPStdioClient 的 per-request
        # 超时(client.timeout)要小于外层 wrapper 超时。这样底层先走它自己的
        # TimeoutError 分支(mcp_stdio._request 里 self._pending.pop(mid)),干净
        # 清理挂起请求;外层仅作兜底。
        # 若外层先超(如旧默认 wrapper=30s < client=60s),asyncio.wait_for 取消会以
        # CancelledError 传到底层 _request(它只 except TimeoutError,不 except
        # CancelledError)→ _pending 永远不 pop → 句柄泄漏 + 已发给子进程的工具
        # 调用残留,多工具累积后表现为"越来越卡、超时难以及时处理"。
        wrapper = MCPToolWrapper(
            name=f"mcp_{tool_name}",
            description=augmented_desc,
            mcp_client=client,
            mcp_tool_name=tool_name,
            input_schema=input_schema,
            # 外层兜底 = 内层 + 余量,保证内层(stdio)必然先超时并自清理
            timeout=float(getattr(client, "timeout", 30.0)) + 5.0,
        )

        def _make_bridge(_wrapper):
            """按「值」把当前迭代的 wrapper 绑定进独立闭包, 返回真正的转发函数。

            为什么必须用工厂函数: 若在循环体里直接写
                async def bridge(**kwargs):
                    return await wrapper.run(kwargs)
            则所有 mcp_XXX 工具的 bridge 都闭包捕获同一个循环变量 wrapper。
            Python 闭包是「延迟绑定」——调用时才取值, 而循环结束后 wrapper
            已指向最后一项(windows-mcp 的 Registry)。结果: 调用任意
            mcp_query/mcp_Click/... 都会被转发到 Registry, 参数张冠李戴,
            报 call[registry_tool](mode, path) 这类校验错误(2026-10-04 实锤)。
            用 _make_bridge 让每个 bridge 捕获「本次迭代」的 wrapper, 根治。
            """
            async def bridge(**kwargs):
                """把参数整体作为 dict 转发给本工具自己的 MCP 包装器。"""
                return await _wrapper.run(kwargs)
            return bridge

        # 注册到全局注册器
        tool_obj = Tool(
            name=wrapper.name,
            description=wrapper.description,
            func=_make_bridge(wrapper),
            timeout=wrapper.timeout,
            params_model=None,
            json_schema=wrapper.input_schema,
            enabled=True,
            terminal=False,
        )
        registry.register(tool_obj)
        count += 1
    
    return count


def unregister_mcp_tools(registry: Optional[ToolRegistry] = None) -> int:
    """
    移除所有 MCP 工具。
    
    Args:
        registry: 工具注册器,如果为 None 则使用全局注册器
    
    Returns:
        移除的工具数量
    """
    if registry is None:
        registry = get_registry()
    
    mcp_tools = [t for t in registry.list_sync(enabled_only=False) if t.name.startswith("mcp_")]
    
    for tool in mcp_tools:
        registry.unregister(tool.name)
    
    return len(mcp_tools)
