"""验证 P0 修复的代码可以正常导入。"""
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_imports():
    """测试所有修改的模块可以正常导入。"""
    print("\n=== 导入测试 ===\n")
    
    modules = [
        ('app.runtime.checkpoint', 'CheckpointManager'),
        ('app.runtime.graph', 'Graph'),
        ('app.core.agent', 'Agent'),
        ('app.api.security', 'require_api_key'),
        ('app.main', 'app'),
    ]
    
    failed = []
    for module_name, class_name in modules:
        try:
            module = __import__(module_name, fromlist=[class_name])
            cls = getattr(module, class_name)
            print(f"✓ {module_name}.{class_name}")
        except Exception as e:
            print(f"✗ {module_name}.{class_name}: {e}")
            failed.append(f"{module_name}.{class_name}")
    
    return failed


def test_checkpoint_async():
    """测试 checkpoint 方法的异步性。"""
    print("\n=== Checkpoint 异步测试 ===\n")
    
    import asyncio
    from app.runtime.checkpoint import CheckpointManager, _DB_LOCK
    
    # 测试锁类型
    assert isinstance(_DB_LOCK, asyncio.Lock), \
        f"期望 asyncio.Lock，实际 {type(_DB_LOCK)}"
    print("✓ _DB_LOCK 是 asyncio.Lock")
    
    # 测试方法都是 async
    methods_to_check = ['save', 'delete', 'load', 'exists', 'versions',
                        'load_version', 'rollback', 'list', 'count', 'clear',
                        'cleanup_expired']
    
    for method_name in methods_to_check:
        method = getattr(CheckpointManager, method_name, None)
        assert method is not None, f"方法 {method_name} 不存在"
        assert asyncio.iscoroutinefunction(method), \
            f"方法 {method_name} 应该是 async"
    
    print(f"✓ 所有 {len(methods_to_check)} 个 checkpoint 方法是 async")


def test_graph_checkpoint_async():
    """测试 graph._checkpoint 是 async。"""
    print("\n=== Graph Checkpoint 测试 ===\n")
    
    import asyncio
    from app.runtime.graph import Graph
    
    assert asyncio.iscoroutinefunction(Graph._checkpoint), \
        "Graph._checkpoint 应该是 async 方法"
    print("✓ Graph._checkpoint 是 async")


def test_security_functions():
    """测试安全函数。"""
    print("\n=== 安全函数测试 ===\n")
    
    from app.api.security import _constant_time_compare
    
    # 测试相同字符串
    assert _constant_time_compare("test", "test") is True
    # 测试不同字符串
    assert _constant_time_compare("test", "other") is False
    # 测试空字符串
    assert _constant_time_compare("", "") is True
    # 测试长字符串
    long_a = "a" * 1000
    long_b = "a" * 1000
    long_c = "a" * 999 + "b"
    assert _constant_time_compare(long_a, long_b) is True
    assert _constant_time_compare(long_a, long_c) is False
    
    print("✓ 常量时间比较函数工作正常")


def test_agent_tool_cache():
    """测试 Agent 工具缓存锁。"""
    print("\n=== Agent 工具缓存测试 ===\n")
    
    # 检查 Agent 类是否有 _tool_cache_lock 属性定义
    from app.core.agent import Agent
    import inspect
    
    # 检查 __init__ 方法中是否有 _tool_cache_lock
    init_source = inspect.getsource(Agent.__init__)
    assert '_tool_cache_lock' in init_source, \
        "Agent.__init__ 应该初始化 _tool_cache_lock"
    
    print("✓ Agent 工具缓存有锁保护")


def main():
    """运行所有验证测试。"""
    print("=" * 60)
    print("P0 修复验证套件")
    print("=" * 60)
    
    all_failed = []
    
    # 1. 导入测试
    failed = test_imports()
    all_failed.extend(failed)
    
    # 2. 异步测试
    try:
        test_checkpoint_async()
    except Exception as e:
        print(f"✗ test_checkpoint_async: {e}")
        all_failed.append('test_checkpoint_async')
    
    try:
        test_graph_checkpoint_async()
    except Exception as e:
        print(f"✗ test_graph_checkpoint_async: {e}")
        all_failed.append('test_graph_checkpoint_async')
    
    # 3. 安全测试
    try:
        test_security_functions()
    except Exception as e:
        print(f"✗ test_security_functions: {e}")
        all_failed.append('test_security_functions')
    
    # 4. Agent 缓存测试
    try:
        test_agent_tool_cache()
    except Exception as e:
        print(f"✗ test_agent_tool_cache: {e}")
        all_failed.append('test_agent_tool_cache')
    
    # 总结
    print("\n" + "=" * 60)
    if all_failed:
        print(f"失败: {', '.join(all_failed)}")
        print("状态: ✗ 验证失败")
        return 1
    else:
        print("状态: ✓ 所有验证通过")
        return 0


if __name__ == "__main__":
    sys.exit(main())
