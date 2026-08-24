# SafetyEngine - unified with guardrails.py
# This module provides backward compatibility aliases

from app.core.guardrails import (  # noqa: F401
    GuardrailResult,
    GuardrailReport,
    check_injection,
    check_sensitive_paths,
    check_leaked_secrets,
)

# Alias for backward compatibility
class SafetyLevel:
    SAFE = "safe"
    WARNING = "warning"
    BLOCKED = "blocked"

class ThreatType:
    PROMPT_INJECTION = "prompt_injection"
    PRIVILEGE_ESCALATION = "privilege_escalation"
    DATA_LEAK = "data_leak"
    SYSTEM_PROMPT_LEAK = "system_prompt_leak"
    JAILBREAK = "jailbreak"
    MALICIOUS_CODE = "malicious_code"
    SENSITIVE_INFO = "sensitive_info"

class SafetyCheckResult:
    """包装 GuardrailResult 为 SafetyCheckResult"""
    def __init__(self, result):
        self._result = result
        self.level = "blocked" if not result.ok else "safe"
        self.threat_type = None
        self.message = result.reason
        self.sanitized_content = result.sanitized or ""
        self.confidence = 1.0 if not result.ok else 0.0
    
    @property
    def is_blocked(self):
        return not self._result.ok
    
    @property
    def needs_sanitization(self):
        return bool(self.sanitized_content)
    
    def to_dict(self):
        return {
            "level": self.level,
            "threat_type": self.threat_type,
            "message": self.message,
            "sanitized_content": self.sanitized_content,
            "confidence": self.confidence,
        }

def get_safety_engine():
    """获取安全引擎实例 (返回 None, 直接使用 guardrails 函数)"""
    return None

def check_input(text):
    """检查输入安全性"""
    from app.core.guardrails import check_injection, check_sensitive_paths, check_secrets
    results = []
    for checker in [check_injection, check_sensitive_paths, check_secrets]:
        try:
            result = checker(text)
            results.append(result)
            if not result.ok:
                return SafetyCheckResult(result)
        except:
            pass
    return SafetyCheckResult(GuardrailResult(name="safety", ok=True))
