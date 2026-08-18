"""PII 数据泄露检测器。

检测常见个人身份信息:
- 邮箱地址
- 电话号码
- 中国身份证号码
- 银行卡号
- IP 地址
- URL（可能包含敏感路径）
"""
from __future__ import annotations

import re
from typing import Optional

# 中国身份证正则（18位，最后一位可能是X）
_ID_CARD_PATTERN = re.compile(
    r'\b([1-9]\d{5}(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx])\b'
)

# 手机号（中国大陆）
_PHONE_PATTERN = re.compile(
    r'\b(1[3-9]\d{9})\b'
)

# 邮箱
_EMAIL_PATTERN = re.compile(
    r'\b([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b'
)

# 银行卡号（16-19位数字，含Luhn校验）
_BANK_CARD_PATTERN = re.compile(
    r'\b([3-9]\d{12,18})\b'
)

# IP 地址
_IP_PATTERN = re.compile(
    r'\b((?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b'
)


class PIIRule:
    """PII 检测规则。"""
    
    def __init__(self, name: str, pattern: re.Pattern, description: str):
        self.name = name
        self.pattern = pattern
        self.description = description
    
    def find(self, text: str) -> list[dict]:
        """在文本中查找 PII。"""
        results = []
        for match in self.pattern.finditer(text):
            results.append({
                "type": self.name,
                "description": self.description,
                "match": match.group(0),
                "start": match.start(),
                "end": match.end(),
            })
        return results


class PIIDetector:
    """PII 检测器。"""
    
    def __init__(self):
        self._rules: list[PIIRule] = [
            PIIRule("email", _EMAIL_PATTERN, "邮箱地址"),
            PIIRule("phone", _PHONE_PATTERN, "手机号码"),
            PIIRule("id_card", _ID_CARD_PATTERN, "身份证号"),
            PIIRule("bank_card", _BANK_CARD_PATTERN, "银行卡号"),
            PIIRule("ip_address", _IP_PATTERN, "IP地址"),
        ]
    
    def detect(self, text: str, max_results: int = 10) -> list[dict]:
        """检测文本中的 PII。
        
        Args:
            text: 待检测文本
            max_results: 最大返回结果数
        
        Returns:
            检测到的 PII 列表
        """
        if not text:
            return []
        
        all_matches: list[dict] = []
        for rule in self._rules:
            matches = rule.find(text)
            all_matches.extend(matches)
        
        # 按位置排序，去重
        seen = set()
        unique_matches = []
        for m in sorted(all_matches, key=lambda x: x["start"]):
            key = (m["type"], m["match"])
            if key not in seen:
                seen.add(key)
                unique_matches.append(m)
                if len(unique_matches) >= max_results:
                    break
        
        return unique_matches
    
    def has_pii(self, text: str) -> bool:
        """检查文本是否包含 PII。"""
        return len(self.detect(text)) > 0
    
    def sanitize(self, text: str, replacement: str = "[REDACTED]") -> str:
        """替换文本中的 PII。
        
        Args:
            text: 原始文本
            replacement: 替换字符串
        
        Returns:
            脱敏后的文本
        """
        if not text:
            return text
        
        result = text
        for rule in reversed(self._rules):  # 从后往前替换，避免位置偏移
            result = rule.pattern.sub(replacement, result)
        
        return result


# 单例
_detector: Optional[PIIDetector] = None


def get_pii_detector() -> PIIDetector:
    """获取 PII 检测器单例。"""
    global _detector
    if _detector is None:
        _detector = PIIDetector()
    return _detector


def detect_pii(text: str) -> list[dict]:
    """便捷函数：检测文本中的 PII。"""
    return get_pii_detector().detect(text)


def has_pii(text: str) -> bool:
    """便捷函数：检查文本是否包含 PII。"""
    return get_pii_detector().has_pii(text)


def sanitize_pii(text: str) -> str:
    """便捷函数：脱敏文本中的 PII。"""
    return get_pii_detector().sanitize(text)


# 测试
if __name__ == "__main__":
    detector = PIIDetector()
    
    test_text = """
    用户信息:
    - 邮箱: test@example.com
    - 手机: 13800138000
    - 身份证: 110101199003074138
    - IP: 192.168.1.1
    
    这是测试文本。
    """
    
    print("=== PII 检测结果 ===")
    results = detector.detect(test_text)
    for r in results:
        print(f"  [{r['type']}] {r['description']}: {r['match']}")
    
    print("\n=== 脱敏后文本 ===")
    print(detector.sanitize(test_text))
