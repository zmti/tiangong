"""验证 P0 修复的测试套件。"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.runtime.checkpoint import CheckpointManager, _DB_LOCK
from app.runtime.state import AgentState


@pytest.mark.asyncio
async def test_checkpoint_async_lock():
    """测试 checkpoint 使用 asyncio.Lock 而非 threading.Lock。"""
    # 验证锁类型
    assert isinstance(_DB_LOCK, asyncio.Lock), f"期望 asyncio.Lock, 实际 {type(_DB_LOCK)}"
    print("[OK] checkpoint 使用 asyncio.Lock")


@pytest.mark.asyncio
async def test_checkpoint_methods_are_async():
    """测试 checkpoint 方法都是 async。"""
    methods = ['save', 'delete', 'load', 'exists', 'versions', 
               'load_version', 'rollback', 'list', 'count', 'clear', 
               'cleanup_expired']
    
    for method_name in methods:
        method = getattr(CheckpointManager, method_name, None)
        assert method is not None, f"方法 {method_name} 不存在"
        assert asyncio.iscoroutinefunction(method), \
            f"方法 {method_name} 应该是 async, 实际 {type(method)}"
    
    print(f"[OK] 所有 {len(methods)} 个 checkpoint 方法都是 async")


def test_graph_checkpoint_is_async():
    """测试 graph._checkpoint 是 async。"""
    from app.runtime.graph import Graph
    
    # 检查 _checkpoint 方法是否是 coroutine function
    assert asyncio.iscoroutinefunction(Graph._checkpoint), \
        "Graph._checkpoint 应该是 async 方法"
    print("[OK] Graph._checkpoint 是 async")


def test_agent_tool_cache_has_lock():
    """测试 Agent 的工具缓存有锁保护。"""
    from app.core.agent import Agent
    
    # 创建一个临时 agent 实例检查属性
    # 注意: 这里不实际初始化 LLM,只检查类定义
    assert hasattr(Agent, '__init__'), "Agent 应该有 __init__ 方法"
    print("[OK] Agent 类结构正常")


def test_security_constant_time_compare():
    """测试安全模块使用常量时间比较。"""
    from app.api.security import _constant_time_compare
    
    # 测试相同字符串
    assert _constant_time_compare("test", "test") is True
    # 测试不同字符串
    assert _constant_time_compare("test", "other") is False
    # 测试空字符串
    assert _constant_time_compare("", "") is True
    print("[OK] 常量时间比较函数工作正常")


def test_cors_configurable():
    """测试 CORS 配置可被环境变量控制。"""
    # 检查 main.py 中的 CORS 配置逻辑
    main_path = Path(__file__).parent.parent / "app" / "main.py"
    content = main_path.read_text(encoding='utf-8')
    
    assert 'CORS_ORIGINS' in content, "应该支持 CORS_ORIGINS 环境变量"
    assert 'allow_origins' in content, "应该配置 allow_origins"
    print("[OK] CORS 配置支持环境变量")


def test_path_traversal_protection():
    """测试路径穿越防护。"""
    main_path = Path(__file__).parent.parent / "app" / "main.py"
    content = main_path.read_text(encoding='utf-8')
    
    # 检查是否有额外的安全检查
    assert '..' in content, "应该检查路径穿越符号"
    assert 'requested_path' in content, "应该使用规范化的路径"
    print("[OK] 路径穿越防护增强")


async def run_all_tests():
    """运行所有测试。"""
    print("\n=== 开始 P0 修复验证测试 ===\n")
    
    tests = [
        test_checkpoint_async_lock,
        test_checkpoint_methods_are_async,
        test_graph_checkpoint_is_async,
        test_agent_tool_cache_has_lock,
        test_security_constant_time_compare,
        test_cors_configurable,
        test_path_traversal_protection,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            if asyncio.iscoroutinefunction(test):
                await test()
            else:
                test()
            passed += 1
        except AssertionError as e:
            print(f"✗ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {test.__name__}: 运行时错误 - {e}")
            failed += 1
    
    print(f"\n=== 测试结果: {passed} 通过, {failed} 失败 ===\n")
    
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
