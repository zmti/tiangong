"""工具执行安全门禁:在 Agent 执行工具前对高风险操作做强制人工确认。

对齐 Claude Code 的 PreToolUse Hooks / 权限模式设计:
- 规则引擎判断"工具 + 参数"是否属于高风险
- 高风险操作必须先创建确认请求并等待用户批准,未批准则拒绝执行(硬门禁,模型无法绕过)
- 确认记录持久化在 confirmations.db,前端轮询展示批准卡片
"""
from __future__ import annotations

import json
from dataclasses import dataclass


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
    "generate_image",    # 每次调用产生生成成本
    "generate_video",    # 每次调用产生生成成本
})

# run_terminal 命令命中这些关键字时需要确认(破坏性 / 影响面大,但不在硬黑名单内)
TERMINAL_CONFIRM_PATTERNS: tuple[str, ...] = (
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
    "rd /s",
    "rmdir /s",
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
    "sc delete",
    "net stop",
    "net user",
    "docker rm",
    "docker rmi",
    "docker system prune",
    "docker volume rm",
    "format ",
    "diskpart",
    "shutdown",
    "certutil -",
    "reg delete",
    "mkfs",
    ":(){",
)


def _arg(arguments: str, name: str, default: str = "") -> str:
    try:
        args = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
        if isinstance(args, dict) and isinstance(args.get(name, ""), str):
            return args.get(name, "")
    except (json.JSONDecodeError, TypeError):
        pass
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
        low = command.strip().lower()
        for pattern in TERMINAL_CONFIRM_PATTERNS:
            if pattern in low:
                return Decision(
                    confirm=True,
                    reason=f"命令包含危险 / 破坏性操作({pattern}),需要用户确认",
                )
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
