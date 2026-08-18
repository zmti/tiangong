"""安全与对齐引擎:向后兼容层。

从 guardrails.py 导入增强后的检测规则,保持 SafetyEngine API 不变。
api/safety.py 继续使用此模块。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.config import settings


class SafetyLevel(str, Enum):
    SAFE = "safe"
    WARNING = "warning"
    BLOCKED = "blocked"


class ThreatType(str, Enum):
    PROMPT_INJECTION = "prompt_injection"
    PRIVILEGE_ESCALATION = "privilege_escalation"
    DATA_LEAK = "data_leak"
    SYSTEM_PROMPT_LEAK = "system_prompt_leak"
    JAILBREAK = "jailbreak"
    MALICIOUS_CODE = "malicious_code"
    SENSITIVE_INFO = "sensitive_info"


@dataclass
class SafetyCheckResult:
    """安全检测结果。"""
    level: SafetyLevel = SafetyLevel.SAFE
    threat_type: Optional[ThreatType] = None
    message: str = ""
    sanitized_content: str = ""
    confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "threat_type": self.threat_type.value if self.threat_type else None,
            "message": self.message,
            "sanitized_content": self.sanitized_content,
            "confidence": round(self.confidence, 3),
        }

    @property
    def is_blocked(self) -> bool:
        return self.level == SafetyLevel.BLOCKED

    @property
    def needs_sanitization(self) -> bool:
        return bool(self.sanitized_content)


# 从 guardrails 导入增强后的模式
from app.core.guardrails import (  # noqa: E402  # pylint: disable=wrong-import-position
    _INJECTION_PATTERNS as _GD_INJECTION,
    _SECRET_PATTERNS as _GD_SECRETS,
    _SENSITIVE_PATH_PATTERNS as _GD_SENSITIVE_PATHS,
)


def _compile_pattern_group(patterns: tuple) -> list:
    import re
    return [re.compile(p) if not hasattr(p, 'pattern') else p for p in patterns]


# 合并注入+jailbreak模式
_COMPILED_INJECTION = _compile_pattern_group(_GD_INJECTION)
_COMPILED_SECRETS = list(_GD_SECRETS)
_COMPILED_SENSITIVE_PATHS = list(_GD_SENSITIVE_PATHS)


class SafetyEngine:
    """安全与对齐检测引擎(基于 guardrails 增强规则)。"""

    def __init__(self):
        self._blocked_terms = self._load_blocked_terms()
        self._check_log: list[dict] = []
        self._max_log = 1000

    @staticmethod
    def _load_blocked_terms() -> list[str]:
        blocked = getattr(settings, "guardrail_blocked_terms", "")
        if blocked:
            return [t.strip() for t in blocked.split(",") if t.strip()]
        return []

    def check_input(self, text: str, context: Optional[str] = None) -> SafetyCheckResult:
        """检测输入消息的安全性。"""
        if not text:
            return SafetyCheckResult()

        import re
        text_lower = text.lower()

        # 1. Prompt 注入检测(含 jailbreak)
        for pat in _COMPILED_INJECTION:
            if isinstance(pat, str):
                if pat.lower() in text_lower:
                    return SafetyCheckResult(
                        level=SafetyLevel.BLOCKED,
                        threat_type=ThreatType.PROMPT_INJECTION,
                        message="检测到可能的 Prompt 注入攻击",
                        confidence=0.9,
                    )
            else:
                if pat.search(text):
                    return SafetyCheckResult(
                        level=SafetyLevel.BLOCKED,
                        threat_type=ThreatType.PROMPT_INJECTION,
                        message="检测到可能的 Prompt 注入攻击",
                        confidence=0.9,
                    )

        # 2. 敏感文件路径检测
        for pat in _COMPILED_SENSITIVE_PATHS:
            if pat.search(text):
                return SafetyCheckResult(
                    level=SafetyLevel.BLOCKED,
                    threat_type=ThreatType.PRIVILEGE_ESCALATION,
                    message="检测到试图访问敏感文件/凭据",
                    confidence=0.85,
                )

        # 3. 禁止词汇
        for term in self._blocked_terms:
            if term.lower() in text_lower:
                return SafetyCheckResult(
                    level=SafetyLevel.WARNING,
                    threat_type=ThreatType.SENSITIVE_INFO,
                    message=f"输入包含禁止词汇: {term}",
                    confidence=0.95,
                )

        return SafetyCheckResult(level=SafetyLevel.SAFE)

    def check_output(self, text: str, check_sensitive: bool = True, check_leak: bool = True) -> SafetyCheckResult:
        """检测输出的安全性(脱敏、泄漏检测)。"""
        if not text:
            return SafetyCheckResult()

        sanitized = text
        found_sensitive = []

        if check_sensitive:
            import re
            for pat in _COMPILED_SECRETS:
                matches = pat.findall(sanitized)
                if matches:
                    found_sensitive.extend(matches)
                    for match in matches:
                        sanitized = pat.sub("[REDACTED]", sanitized)

        if found_sensitive:
            return SafetyCheckResult(
                level=SafetyLevel.WARNING,
                threat_type=ThreatType.DATA_LEAK,
                message=f"输出包含 {len(found_sensitive)} 处敏感信息,已自动脱敏",
                sanitized_content=sanitized,
                confidence=0.95,
            )

        # 系统提示词泄漏检测
        if check_leak:
            system_indicators = [
                "以下是系统提示词", "系统提示词如下", "我的工具描述",
                "tool description", "你是一个智能助手", "build_system_prompt",
            ]
            for indicator in system_indicators:
                if indicator in text and "{" in text and ("role" in text.lower() or "工具" in text):
                    return SafetyCheckResult(
                        level=SafetyLevel.WARNING,
                        threat_type=ThreatType.SYSTEM_PROMPT_LEAK,
                        message="输出疑似包含系统提示词内容",
                        sanitized_content="[部分内容因安全原因已脱敏]",
                        confidence=0.6,
                    )

        return SafetyCheckResult(
            level=SafetyLevel.SAFE,
            sanitized_content=sanitized if sanitized != text else "",
        )

    def check_tool_call(self, tool_name: str, arguments: dict, dangerous_tools: Optional[list[str]] = None) -> SafetyCheckResult:
        """检测工具调用的安全性。"""
        dangerous_tools = dangerous_tools or getattr(
            settings, "agent_dangerous_tools", "run_terminal,run_code"
        ).split(",")

        if tool_name in dangerous_tools:
            return SafetyCheckResult(
                level=SafetyLevel.WARNING,
                threat_type=ThreatType.PRIVILEGE_ESCALATION,
                message=f"危险工具调用: {tool_name},需要人工确认",
                confidence=0.8,
            )

        args_str = str(arguments)
        for pat in _COMPILED_SECRETS:
            if pat.search(args_str):
                return SafetyCheckResult(
                    level=SafetyLevel.BLOCKED,
                    threat_type=ThreatType.MALICIOUS_CODE,
                    message="工具参数中包含疑似敏感信息",
                    confidence=0.9,
                )

        return SafetyCheckResult(level=SafetyLevel.SAFE)

    def record_check(self, result: SafetyCheckResult, input_text: str = "") -> None:
        """记录检测结果到日志。"""
        entry = {
            "timestamp": result.timestamp,
            "level": result.level.value,
            "threat_type": result.threat_type.value if result.threat_type else None,
            "input_preview": input_text[:100] if input_text else "",
        }
        self._check_log.append(entry)
        if len(self._check_log) > self._max_log:
            self._check_log = self._check_log[-self._max_log:]

    def get_stats(self) -> dict:
        """获取安全检测统计。"""
        levels = {"safe": 0, "warning": 0, "blocked": 0}
        types: dict = {}
        for entry in self._check_log:
            levels[entry["level"]] = levels.get(entry["level"], 0) + 1
            tt = entry.get("threat_type")
            if tt:
                types[tt] = types.get(tt, 0) + 1
        return {
            "total_checks": len(self._check_log),
            "levels": levels,
            "threat_types": types,
            "recent": self._check_log[-20:],
        }


# ── 全局实例 ──────────────────────────────────────────────────────
_instance: Optional[SafetyEngine] = None
_lock = __import__('threading').Lock()


def get_safety_engine() -> SafetyEngine:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = SafetyEngine()
    return _instance


def reset_safety_engine() -> None:
    """测试用:重置全局实例。"""
    global _instance  # pylint: disable=global-statement
    _instance = None
