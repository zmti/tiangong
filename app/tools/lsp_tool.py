"""LSP 客户端:通过 HTTP/Stdio 调用语言服务器,获取定义跳转、引用查询、悬停信息。

支持的语言服务器:
- Python: pylsp (http://localhost:20840) 或通过 pylsp 的 stdio 模式
- TypeScript/JavaScript: typescript-language-server (stdio)
- Go: gopls (stdio)
- Rust: rust-analyzer (stdio)

默认配置通过 AGENT_LSP_SERVERS(.env) 控制,格式为 JSON。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from typing import Optional

from pydantic import Field

from app.config import settings
from app.tools.registry import tool

# 语言服务器配置(可从 .env 的 AGENT_LSP_SERVERS 覆盖)
DEFAULT_LSP_SERVERS: dict[str, dict] = {
    "python": {"command": "pylsp", "args": [], "languages": ["python"]},
    "typescript": {
        "command": "typescript-language-server",
        "args": ["--stdio"],
        "languages": ["typescript", "javascript", "tsx", "jsx"],
    },
}


def _resolve_lsp_config(language: str) -> Optional[dict]:
    """解析指定语言的 LSP 服务器配置。"""
    raw = (settings.agent_lsp_servers or "").strip()
    if not raw:
        return DEFAULT_LSP_SERVERS.get(language)
    try:
        custom = json.loads(raw)
        if isinstance(custom, dict):
            return custom.get(language) or DEFAULT_LSP_SERVERS.get(language)
    except json.JSONDecodeError:
        pass
    return DEFAULT_LSP_SERVERS.get(language)


class LSPClient:
    """简单的 LSP stdio 客户端。"""

    def __init__(self, command: str, args: list[str]):
        self._command = command
        self._args = args
        self._proc: Optional[subprocess.Popen] = None
        self._msg_id = 0
        self._lock = threading.Lock()

    def _next_id(self) -> int:
        with self._lock:
            self._msg_id += 1
            return self._msg_id

    def _ensure_proc(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        exe = shutil.which(self._command)
        if not exe:
            return False
        try:
            self._proc = subprocess.Popen(
                [exe, *self._args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            # 发送 initialize
            init_msg = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "capabilities": {},
                    "workspaceFolders": None,
                },
            }
            self._proc.stdin.write(json.dumps(init_msg) + "\n")
            self._proc.stdin.flush()
            # 读取 initialize 响应
            resp = self._read_response()
            return resp is not None
        except Exception:
            self._proc = None
            return False

    def _read_response(self) -> Optional[dict]:
        if self._proc is None or self._proc.stdout is None:
            return None
        try:
            self._proc.stdout.seek(0)
            line = self._proc.stdout.readline()
            if not line:
                return None
            return json.loads(line.strip())
        except Exception:
            return None

    def send_request(self, method: str, params: dict, timeout: float = 10.0) -> Optional[dict]:
        """发送 LSP 请求并等待响应。"""
        import time as _time

        if not self._ensure_proc():
            return None
        msg_id = self._next_id()
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        try:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
            start = _time.time()
            while True:
                if self._proc is None or self._proc.stdout is None:
                    return None
                line = self._proc.stdout.readline()
                if not line:
                    break
                try:
                    resp = json.loads(line.strip())
                    if resp.get("id") == msg_id:
                        return resp.get("result")
                except json.JSONDecodeError:
                    continue
                if _time.time() - start > timeout:
                    return None
        except Exception:
            return None
        return None

    def close(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None


# 全局 LSP 客户端缓存:language -> client
_lsp_clients: dict[str, LSPClient] = {}
_lsp_lock = threading.Lock()


def _get_lsp_client(language: str) -> Optional[LSPClient]:
    """获取或创建指定语言的 LSP 客户端。"""
    config = _resolve_lsp_config(language)
    if not config:
        return None
    cmd = config.get("command", "")
    args = config.get("args", [])
    key = f"{cmd}:{' '.join(args)}"
    with _lsp_lock:
        client = _lsp_clients.get(key)
        if client is None:
            client = LSPClient(cmd, args)
            _lsp_clients[key] = client
        return client


def _detect_language(filepath: str) -> Optional[str]:
    """根据文件扩展名检测语言。"""
    ext_map = {
        ".py": "python",
        ".ts": "typescript", ".tsx": "typescript", ".js": "typescript", ".jsx": "typescript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".cs": "csharp",
        ".cpp": "cpp", ".c": "c", ".h": "c",
        ".php": "php",
        ".rb": "ruby",
        ".kt": "kotlin",
        ".swift": "swift",
        ".vue": "typescript",
    }
    ext = os.path.splitext(filepath)[1].lower()
    return ext_map.get(ext)


def lsp_hover(filepath: str, line: int, column: int = 0) -> dict:
    """查询光标处的悬停信息(类型、文档注释)。"""
    language = _detect_language(filepath)
    if not language:
        return {"error": f"不支持的语言: {filepath}"}
    client = _get_lsp_client(language)
    if not client:
        return {"error": f"未配置 {language} 的 LSP 服务器,请安装 {language}-language-server"}
    params = {
        "textDocument": {"uri": f"file://{os.path.abspath(filepath)}"},
        "position": {"line": line - 1, "character": column},
    }
    result = client.send_request("textDocument/hover", params)
    if result is None:
        return {"filepath": filepath, "line": line, "column": column, "hover": None}
    contents = result.get("contents", {})
    if isinstance(contents, str):
        value = contents
    elif isinstance(contents, dict):
        value = contents.get("value", "")
    else:
        value = str(contents)
    return {"filepath": filepath, "line": line, "column": column, "hover": value.strip()[:2000]}


def lsp_references(filepath: str, line: int, column: int = 0) -> dict:
    """查询符号的所有引用位置。"""
    language = _detect_language(filepath)
    if not language:
        return {"error": f"不支持的语言: {filepath}"}
    client = _get_lsp_client(language)
    if not client:
        return {"error": f"未配置 {language} 的 LSP 服务器"}
    params = {
        "textDocument": {"uri": f"file://{os.path.abspath(filepath)}"},
        "position": {"line": line - 1, "character": column},
    }
    result = client.send_request("textDocument/references", params)
    refs = []
    if isinstance(result, list):
        for r in result[:50]:
            uri = r.get("uri", "")
            loc = r.get("range", {})
            refs.append({
                "file": uri.replace("file://", ""),
                "line": loc.get("start", {}).get("line", 0) + 1,
                "col": loc.get("start", {}).get("character", 0),
            })
    return {"filepath": filepath, "line": line, "references": refs, "count": len(refs)}


def lsp_definition(filepath: str, line: int, column: int = 0) -> dict:
    """查询符号的定义位置。"""
    language = _detect_language(filepath)
    if not language:
        return {"error": f"不支持的语言: {filepath}"}
    client = _get_lsp_client(language)
    if not client:
        return {"error": f"未配置 {language} 的 LSP 服务器"}
    params = {
        "textDocument": {"uri": f"file://{os.path.abspath(filepath)}"},
        "position": {"line": line - 1, "character": column},
    }
    result = client.send_request("textDocument/definition", params)
    if isinstance(result, list) and result:
        r = result[0]
        uri = r.get("uri", "")
        loc = r.get("range", {})
        return {
            "filepath": filepath,
            "definition": {
                "file": uri.replace("file://", ""),
                "line": loc.get("start", {}).get("line", 0) + 1,
                "col": loc.get("start", {}).get("character", 0),
            },
        }
    return {"filepath": filepath, "line": line, "definition": None}


# 工具注册
@tool(
    "lsp_hover",
    "查询代码中指定位置的悬停信息(类型注解、文档注释、函数签名)。"
    "用于在编写/阅读代码时快速了解符号含义。",
    timeout=15,
)
def lsp_hover_tool(
    filepath: str = Field(description="代码文件路径"),
    line: int = Field(1, description="行号(从 1 开始)"),
    column: int = Field(0, description="列号(从 0 开始,默认行首)"),
) -> dict:
    return lsp_hover(filepath, line, column)


@tool(
    "lsp_references",
    "查询代码中指定符号的所有引用位置(调用方列表)。"
    "用于理解某个函数/类被哪些地方使用。",
    timeout=15,
)
def lsp_references_tool(
    filepath: str = Field(description="代码文件路径"),
    line: int = Field(1, description="符号所在行号"),
    column: int = Field(0, description="符号所在列号(从 0 开始)"),
) -> dict:
    return lsp_references(filepath, line, column)


@tool(
    "lsp_definition",
    "跳转到符号定义位置(如查看函数实现、变量声明)。",
    timeout=15,
)
def lsp_definition_tool(
    filepath: str = Field(description="代码文件路径"),
    line: int = Field(1, description="符号所在行号"),
    column: int = Field(0, description="符号所在列号(从 0 开始)"),
) -> dict:
    return lsp_definition(filepath, line, column)


def shutdown_lsp_clients() -> None:
    """进程退出时关闭所有 LSP 连接。"""
    for client in _lsp_clients.values():
        client.close()
    _lsp_clients.clear()
