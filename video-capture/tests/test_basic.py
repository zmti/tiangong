"""基础测试。"""
import pytest

def test_hello():
    from video-capture import hello
    assert hello() == "Hello from video-capture!"
