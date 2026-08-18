"""MCP stdio 传输客户端:通过子进程 JSON-RPC(2.0)连接标准 MCP 服务器。

覆盖主流 MCP 服务器的 stdio 启动方式(如 npx / uvx / python -m 等),
接口与 HTTP 版 MCPClient 保持一致,可被 MCPManager 统一管理。
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional


class MCPStdioClient:
    """基于子进程 stdin/stdout 的 MCP 客户端(newline-delimited JSON-RPC)。"""

    def __init__(
        self,
        command: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        timeout: float = 60.0,
        cwd: Optional[str] = None,
        auto_reconnect: bool = True,
        reconnect_interval: float = 5.0,
    ):
        self.command = command
        self.args = list(args or [])
        self.env_extra = dict(env or {})
        self.timeout = timeout
        self.cwd = cwd
        self.auto_reconnect = auto_reconnect
        self._reconnect_interval = reconnect_interval
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader = None
        self._writer = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._tools_cache: list[dict] = []
        self._connected = False
        self._read_task: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()
        self._reconnect_task: Optional[asyncio.Task] = None

    # ---- 生命周期 ----

    async def connect(self) -> dict:
        """启动子进程并完成 initialize 握手。"""
        if self._proc is not None and self._proc.returncode is None:
            return {"status": "connected", "server": {"name": self.command}}
        env = os.environ.copy()
        env.update({k: str(v) for k, v in self.env_extra.items()})
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.cwd,
            )
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "error": f"启动 MCP 进程失败: {e}"}
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._writer = self._proc.stdin
        self._reader = self._proc.stdout
        self._read_task = asyncio.create_task(self._read_loop())
        try:
            await self._request("initialize", {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "agent-service", "version": "1.0.0"},
            })
            await self._notify("notifications/initialized")
        except Exception as e:  # noqa: BLE001
            await self.close()
            return {"status": "error", "error": f"initialize 失败: {e}"}
        self._connected = True
        # 启动自动重连监控(仅当配置了 auto_reconnect)
        if self.auto_reconnect:
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())
        return {"status": "connected", "server": {"name": self.command}}

    async def close(self) -> None:
        # 停止自动重连:先禁用标志,再取消任务,防止竞态
        self.auto_reconnect = False
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None
        # 先取消读任务并等待其结束,再关闭进程
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                pass
            self._read_task = None
        proc = self._proc
        self._proc = None
        self._connected = False
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def _reconnect_loop(self) -> None:
        """自动重连循环:进程退出后以指数退避尝试重新启动。"""
        from app.config import settings as _settings
        consecutive_failures = 0
        max_delay = float(getattr(_settings, "mcp_reconnect_max_delay", 60.0))
        max_failures = int(getattr(_settings, "mcp_reconnect_max_failures", 10))
        while self.auto_reconnect:
            try:
                # 指数退避,但上限受配置限制
                delay = min(
                    self._reconnect_interval * (2 ** min(consecutive_failures, 6)),
                    max_delay,
                )
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            if self._proc is not None and self._proc.returncode is None:
                consecutive_failures = 0  # 进程仍在运行,重置计数
                continue
            result = await self.connect()
            if result.get("status") == "connected":
                consecutive_failures = 0
                self._tools_cache.clear()
            else:
                consecutive_failures += 1
                import logging
                logging.getLogger(__name__).warning(
                    "MCP Stdio 重连失败 (%d/%d): %s",
                    consecutive_failures, max_failures, result.get("error", "unknown"),
                )
                if consecutive_failures >= max_failures:
                    # 连续失败达到阈值后停止重连,避免无意义重试
                    break

    # ---- 与 HTTP 版一致的接口 ----

    async def list_tools(self) -> list[dict]:
        try:
            result = await self._request("tools/list", {})
        except Exception as e:  # noqa: BLE001
            return [{"error": f"获取工具列表失败: {e}"}]
        tools = (result or {}).get("tools", []) if isinstance(result, dict) else []
        self._tools_cache = tools
        return self._tools_cache

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict:
        try:
            result = await self._request("tools/call", {"name": tool_name, "arguments": arguments or {}})
        except Exception as e:  # noqa: BLE001
            return {"error": f"调用工具失败: {e}"}
        if not isinstance(result, dict):
            return {"result": str(result)}
        is_error = bool(result.get("isError", False))
        text_parts: list[str] = []
        for c in result.get("content", []) or []:
            ctype = c.get("type", "")
            if ctype == "text":
                text_parts.append(str(c.get("text", "")))
            elif ctype == "image":
                text_parts.append(f"[image: {c.get('mimeType', 'image/png')};base64,{c.get('data', '')}]")
            else:
                text_parts.append(str(c.get("text", "") or c.get("resource", "")))
        text = "\n".join(text_parts)
        if is_error:
            return {"error": text or "MCP 工具执行出错"}
        return {"result": text}

    async def get_tool_schema(self, tool_name: str) -> Optional[dict]:
        if not self._tools_cache:
            await self.list_tools()
        for t in self._tools_cache:
            if t.get("name") == tool_name:
                return t
        return None

    def to_openai_schemas(self) -> list[dict]:
        schemas = []
        for tool in self._tools_cache:
            schemas.append({
                "type": "function",
                "function": {
                    "name": f"mcp_{tool.get('name', 'unknown')}",
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                },
            })
        return schemas

    # ---- JSON-RPC 底层 ----

    async def _request(self, method: str, params: dict) -> Any:
        mid = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[mid] = fut
        await self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        try:
            msg = await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise RuntimeError(f"MCP 请求超时: {method}")  # noqa: TRY301
        if isinstance(msg, dict) and "error" in msg:
            raise RuntimeError(f"MCP {method} 错误: {msg.get('error')}")
        return msg.get("result") if isinstance(msg, dict) else None

    async def _notify(self, method: str, params: Optional[dict] = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def _send(self, msg: dict) -> None:
        async with self._write_lock:
            if self._writer is None:
                raise RuntimeError("MCP 进程未启动")
            self._writer.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            await self._writer.drain()

    async def _read_loop(self) -> None:
        while self._reader is not None:
            try:
                line = await self._reader.readline()
            except Exception:  # noqa: BLE001
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("method"):
                # 服务器主动通知/请求:处理 ping,其余忽略
                if msg.get("method") == "ping" and msg.get("id") is not None:
                    asyncio.create_task(self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}}))
                continue
            fut = self._pending.pop(msg.get("id"), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
