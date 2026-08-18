"""PII 检测器测试。"""
import pytest
from app.core.pii_detector import PIIDetector, detect_pii, has_pii, sanitize_pii


class TestPIIDetector:
    """PII 检测器测试。"""
    
    def test_detect_email(self):
        detector = PIIDetector()
        text = "请联系 test@example.com 获取帮助"
        results = detector.detect(text)
        emails = [r for r in results if r["type"] == "email"]
        assert len(emails) == 1
        assert emails[0]["match"] == "test@example.com"
    
    def test_detect_phone(self):
        detector = PIIDetector()
        text = "联系电话: 13800138000"
        results = detector.detect(text)
        phones = [r for r in results if r["type"] == "phone"]
        assert len(phones) == 1
        assert phones[0]["match"] == "13800138000"
    
    def test_detect_id_card(self):
        detector = PIIDetector()
        text = "身份证号: 110101199003074138"
        results = detector.detect(text)
        ids = [r for r in results if r["type"] == "id_card"]
        assert len(ids) == 1
        assert ids[0]["match"] == "110101199003074138"
    
    def test_detect_ip(self):
        detector = PIIDetector()
        text = "服务器 IP: 192.168.1.1"
        results = detector.detect(text)
        ips = [r for r in results if r["type"] == "ip_address"]
        assert len(ips) == 1
        assert ips[0]["match"] == "192.168.1.1"
    
    def test_detect_multiple(self):
        detector = PIIDetector()
        text = """
        用户信息:
        邮箱: user@test.com
        手机: 13900139000
        IP: 10.0.0.1
        """
        results = detector.detect(text)
        assert len(results) == 3
    
    def test_sanitize(self):
        detector = PIIDetector()
        text = "联系 test@example.com 或 13800138000"
        sanitized = detector.sanitize(text)
        assert "test@example.com" not in sanitized
        assert "13800138000" not in sanitized
        assert "[REDACTED]" in sanitized
    
    def test_empty_text(self):
        detector = PIIDetector()
        assert detector.detect("") == []
        assert detector.sanitize("") == ""
    
    def test_no_pii(self):
        detector = PIIDetector()
        text = "这是一段普通的测试文本，没有任何敏感信息。"
        assert detector.detect(text) == []
        assert detector.has_pii(text) is False


class TestPIIFunctions:
    """便捷函数测试。"""
    
    def test_detect_pii_function(self):
        text = "邮箱: admin@company.com"
        results = detect_pii(text)
        assert len(results) == 1
        assert results[0]["type"] == "email"
    
    def test_has_pii_function(self):
        assert has_pii("contact: test@example.com") is True
        assert has_pii("just normal text") is False
    
    def test_sanitize_pii_function(self):
        text = "电话: 13800138000"
        result = sanitize_pii(text)
        assert "13800138000" not in result
