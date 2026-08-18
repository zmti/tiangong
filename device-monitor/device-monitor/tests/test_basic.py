"""基础测试。"""
import pytest

def test_hello():
    from device-monitor import hello
    assert hello() == "Hello from device-monitor!"
