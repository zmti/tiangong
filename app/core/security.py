"""工具执行安全门禁:在 Agent 执行工具前对高风险操作做强制人工确认。

对齐 Claude Code 的 PreToolUse Hooks / 权限模式设计:
- 规则引擎判断"工具 + 参数"是否属于高风险
- 高风险操作必须先创建确认请求并等待用户批准,未批准则拒绝执行(硬门禁,模型无法绕过)
- 命令三级分类:SAFE(无需确认) / NEED_CONFIRM(需确认) / BLOCKED(硬拦截)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    """门禁判定结果。"""

    allowed: bool = True
    confirm: bool = False
    reason: str = ""


# 无条件必须确认的工具(成本 / 不可逆 / 对外发送)
ALWAYS_CONFIRM_TOOLS: frozenset[str] = frozenset({
    "send_email",        # 对外发送邮件,不可逆
    "delete_email",      # 删除邮件,不可逆
    # generate_image / generate_video 不再强制确认,由用户自行决定成本
})

# ---- 统一命令分类(合并 terminal_tool.py 的 DANGEROUS_PATTERNS 和本文件的 TERMINAL_CONFIRM_PATTERNS) ----

# 硬拦截:命中即拒绝执行,不给用户确认机会
BLOCKED_COMMAND_PATTERNS: tuple[str, ...] = (
    "format ",           # 格式化磁盘
    "diskpart",          # 磁盘分区操作
    "deltree",           # 递归删除目录
    "shutdown",          # 关机/重启
    "reg delete",        # 删除注册表
    "net user",          # 用户账户管理
    "sc delete",         # 删除系统服务
    "taskkill /f /im",   # 强制结束系统进程
    "rm -rf /",          # 删除根目录
    ":(){",              # fork 炸弹
    "mkfs",              # 格式化文件系统
    "rd /s",             # 递归删除目录
    "rmdir /s",          # 递归删除目录
    # 改进: 解释器/解码器嵌套执行(可绕过上面所有 blacklist 子串检测)
    "python -c",         # python -c "..."
    "python3 -c",
    "node -e",
    "node --eval",
    "perl -e",
    "ruby -e",
    "php -r",
    "base64 -d",         # 解码执行 payload
    "base64 --decode",
    "certutil -decode",
    "powershell -command",
    "powershell -enc",    # 编码命令
    "/bin/sh -c",
    "/bin/bash -c",
    "sh -c",
)

# 需确认:命中时需用户确认后才执行(破坏性/影响面大,但不在硬黑名单内)
CONFIRM_COMMAND_PATTERNS: tuple[str, ...] = (
    "git push --force",
    "git push -f",
    "git reset --hard",
    "git clean",
    "git checkout --",
    "git revert --no-commit",
    "rm -rf",
    "rm -r",
    "del /s",
    "del /q",
    "drop table",
    "drop database",
    "truncate",
    "delete from",
    "update ",
    "insert into",
    "pip uninstall",
    "pipenv uninstall",
    "npm uninstall",
    "yarn remove",
    "npm publish",
    "taskkill",
    "sc stop",
    "net stop",
    "docker rm",
    "docker rmi",
    "docker system prune",
    "docker volume rm",
    "certutil -",
)

# 安全命令:纯信息查询/只读操作,即使 agent_tool_confirm 开启也无需确认
SAFE_COMMAND_PATTERNS: tuple[str, ...] = (
    "--version",
    "--help",
    "-h",
    "-?",
    "where ",
    "which ",
    "pip show",
    "npm list",
    "npm info",
    "yarn info",
    "pip list",
    "pip freeze",
    "python --version",
    "node --version",
    "java -version",
    "go version",
    "rustc --version",
    "gcc --version",
    "docker ps",
    "docker images",
    "docker --version",
    "git status",
    "git log",
    "git diff",
    "git show",
    "git branch",
    "git remote",
    "dir ",
    "ls ",
    "cat ",
    "type ",
    "echo ",
    "whoami",
    "hostname",
    "ipconfig",
    "ifconfig",
    "ping ",
    "curl ",
    "wget ",
    "redis-cli ping",
    "redis-cli info",
    "redis-cli --version",
)


def _classify_command(command: str) -> str:
    """分类命令为 'safe' / 'confirm' / 'blocked'。

    优先级: blocked > confirm > safe > 默认(confirm)
    """
    low = command.strip().lower()
    for pattern in BLOCKED_COMMAND_PATTERNS:
        if pattern.lower() in low:
            return "blocked"
    for pattern in CONFIRM_COMMAND_PATTERNS:
        if pattern.lower() in low:
            return "confirm"
    for pattern in SAFE_COMMAND_PATTERNS:
        if pattern.lower() in low:
            return "safe"
    return "confirm"  # 未知命令默认需确认


def _arg(arguments: str, name: str, default: str = "") -> str:
    try:
        args = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
        if isinstance(args, dict) and isinstance(args.get(name, ""), str):
            return args.get(name, "")
    except (json.JSONDecodeError, TypeError) as exc:
        import logging
        logging.getLogger(__name__).debug("Failed to parse tool arguments: %s", exc)
    return default


def decide(name: str, arguments: str) -> Decision:
    """判断一次工具调用是否需要在执行前请求人工确认。"""
    if name in ALWAYS_CONFIRM_TOOLS:
        return Decision(
            confirm=True,
            reason=f"工具 {name} 属于高风险操作(成本 / 不可逆 / 对外发送),需要用户确认",
        )
    if name == "run_terminal":
        command = _arg(arguments, "command", "")
        classification = _classify_command(command)
        if classification == "blocked":
            return Decision(
                allowed=False,
                confirm=False,
                reason=f"命令包含危险/破坏性操作,已被硬拦截",
            )
        if classification == "confirm":
            return Decision(
                confirm=True,
                reason=f"命令可能具有破坏性或影响面较大,需要用户确认",
            )
        # safe: 无需确认
    return Decision(allowed=True)


def describe(name: str, arguments: str, cap: int = 300) -> str:
    """生成给用户看的人类可读操作描述。"""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
    except (json.JSONDecodeError, TypeError):
        args = {}
    if not isinstance(args, dict):
        args = {"raw": str(args)}

    if name == "run_terminal":
        command = str(args.get("command", ""))
        cwd = str(args.get("cwd", ""))
        text = f"执行命令: {command}"
        if cwd:
            text += f" (目录: {cwd})"
        return text[:cap]
    if name == "send_email":
        parts = []
        for k in ("to", "subject", "cc", "bcc"):
            if args.get(k):
                parts.append(f"{k}={args.get(k)}")
        return ("发送邮件 " + " ".join(parts))[:cap] if parts else "发送邮件"
    if name == "delete_email":
        return f"删除邮件 uid={args.get('uid')} folder={args.get('folder')}"
    if name in ("generate_image", "generate_video"):
        return f"生成{name.replace('generate_', '')}: {str(args.get('prompt', ''))[:100]}"

    compact = json.dumps(args, ensure_ascii=False)
    return compact[:cap]
