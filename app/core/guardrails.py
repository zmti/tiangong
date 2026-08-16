"""Guardrail 护栏:输入/输出并行校验 + Prompt 注入防护。

对齐 OpenAI Agents SDK 的 Guardrails(并行 validator)设计:
- 输入护栏:在进入主循环前并行校验用户消息,拦截 Prompt 注入/越权指令/危险内容
- 输出护栏:在返回最终答案前校验,拦截敏感信息外泄、被注入的输出

每个校验器是独立的 (name, check_fn),返回 (ok, reason, sanitized)。
全部校验器并行执行;任一失败即整体拦截(可配置是否仅拦截而不阻断)。
失败行为:
- 拦截(block):不进入主循环/不返回答案,回复固定安全文案
- 仅记录(warn):放行但记录 violation 到 state.metadata["guardrail_violations"]
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.config import settings

# Prompt 注入模式:试图覆盖系统指令 / 越权 / 提示注入
_INJECTION_PATTERNS: tuple[str, ...] = (
    "ignore all previous",
    "ignore the previous",
    "ignore above",
    "忽略以上",
    "忽略之前的",
    "忽略所有之前的",
    "disregard",
    "forget everything",
    "forget all",
    "忘记所有",
    "系统提示词",
    "system prompt",
    "你是我的系统",
    "jailbreak",
    "越狱",
    "act as though",
    "without your instructions",
    "你的指令是",
    "你现在是",
    "角色扮演为管理员",
    "dans la suite",
    "假裝",
    "假装你是",
    "no longer your assistant",
    "不要遵守",
    "stop obeying",
    "override your",
    "bypass your",
    "绕过",
)

# 输出护栏:敏感信息(API Key / 密钥 / token)外泄模式
_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\b(sk-[A-Za-z0-9]{16,})\b"),
    re.compile(r"\b([A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})\b"),  # JWT
    re.compile(r"(?i)\b(api[-_]?key|secret|password|passwd|token)\b\s*[:=]\s*\S{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key
    re.compile(r"(?i)-----begin (rsa |openssh |ec |)private key-----"),
)

# 敏感文件路径 / 越权读取特征(输入护栏)
_SENSITIVE_PATH_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\.env\b"),
    re.compile(r"\.ssh[\\/](id_rsa|id_ed25519|config)"),
    re.compile(r"\.aws[\\/]credentials"),
    re.compile(r"credentials\.json"),
    re.compile(r"secret[\\/_-]?"),
    re.compile(r"password\.txt"),
    re.compile(r"\.git[\\/]config"),
)


@dataclass
class GuardrailResult:
    """一次护栏检查结果。"""

    name: str
    ok: bool
    reason: str = ""
    sanitized: Optional[str] = None  # 提供清洗后的文本时使用


@dataclass
class GuardrailReport:
    """一批校验器的汇总报告。"""

    blocked: bool = False
    violations: list[GuardrailResult] = field(default_factory=list)

    def reasons(self) -> list[str]:
        return [v.reason for v in self.violations]


# 校验器类型: (name, async check(text) -> GuardrailResult)
InputChecker = Callable[[str], GuardrailResult]
OutputChecker = Callable[[str], GuardrailResult]


def _contains_injection(text: str) -> Optional[str]:
    low = (text or "").lower()
    for pat in _INJECTION_PATTERNS:
        if pat.lower() in low:
            return pat
    return None


def check_injection(text: str) -> GuardrailResult:
    """Prompt 注入防护:检测试图覆盖系统指令/越权的内容。"""
    hit = _contains_injection(text)
    if hit:
        return GuardrailResult(
            name="prompt_injection",
            ok=False,
            reason=f"检测到疑似 Prompt 注入意图({hit!r}),已拦截以避免越权指令执行",
        )
    return GuardrailResult(name="prompt_injection", ok=True)


def check_sensitive_paths(text: str) -> GuardrailResult:
    """检测试图读取敏感文件/凭据的指令。"""
    for pat in _SENSITIVE_PATH_PATTERNS:
        if pat.search(text):
            return GuardrailResult(
                name="sensitive_paths",
                ok=False,
                reason=f"检测到试图访问敏感文件/凭据({pat.pattern!r}),已拦截",
            )
    return GuardrailResult(name="sensitive_paths", ok=True)


def check_leaked_secrets(text: str) -> GuardrailResult:
    """输出护栏:检测最终回答中外泄的密钥/令牌/私钥。"""
    for pat in _SECRET_PATTERNS:
        m = pat.search(text or "")
        if m:
            return GuardrailResult(
                name="secret_leak",
                ok=False,
                reason=f"输出中检测到疑似密钥/令牌({pat.pattern!r}),已脱敏",
                sanitized=pat.sub("[已脱敏]", text),
            )
    return GuardrailResult(name="secret_leak", ok=True)


def check_instruction_leak(text: str) -> GuardrailResult:
    """输出护栏:检测模型是否被诱导输出系统提示词/工具描述原文。"""
    markers = (
        "以下是系统提示词",
        "系统提示词如下",
        "我的工具描述",
        "tool description",
        "你是一个智能助手",
        "build_system_prompt",
    )
    low = (text or "").lower()
    for m in markers:
        if m.lower() in low and "{" in text and ("role" in low or "工具" in text):
            return GuardrailResult(
                name="instruction_leak",
                ok=False,
                reason=f"输出疑似泄露系统提示/工具描述({m!r}),已拦截",
            )
    return GuardrailResult(name="instruction_leak", ok=True)


def _run_parallel(checks, text: str) -> GuardrailReport:
    report = GuardrailReport()
    for check in checks:
        try:
            res = check(text)
        except Exception:  # noqa: BLE001  # 校验器自身异常不应拖垮主流程
            continue
        if not res.ok:
            report.violations.append(res)
            report.blocked = True
    return report


def run_input_guardrails(text: str, enabled: Optional[bool] = None) -> GuardrailReport:
    """输入护栏:进入主循环前并行校验用户消息。"""
    if enabled is None:
        enabled = settings.agent_guardrails_enabled
    if not enabled or not text:
        return GuardrailReport()
    checks: list[InputChecker] = [check_injection, check_sensitive_paths]
    return _run_parallel(checks, text)


def run_output_guardrails(text: str, enabled: Optional[bool] = None) -> GuardrailReport:
    """输出护栏:返回最终答案前校验。"""
    if enabled is None:
        enabled = settings.agent_guardrails_enabled
    if not enabled or not text:
        return GuardrailReport()
    checks: list[OutputChecker] = [check_leaked_secrets, check_instruction_leak]
    return _run_parallel(checks, text)


# 拦截时返回的固定安全文案
INPUT_BLOCK_MESSAGE = "您的请求包含系统无法处理的内容(疑似指令注入/越权操作),已停止处理。请描述您的真实需求。"
