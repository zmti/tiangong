"""性能优化验证测试: 工具路由缓存 + Checkpoint 批量写入。"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.mark.asyncio
async def test_tool_route_cache():
    """测试工具路由缓存功能。"""
    from app.core.tool_route_cache import ToolRouteCache, RouteCacheEntry
    
    cache = ToolRouteCache(max_size=100, ttl_seconds=60)
    
    # 测试写入
    await cache.set("查询天气", ["weather"], 0.95, "用户询问天气")
    await cache.set("计算2+2", ["calculate"], 0.98, "数学计算")
    await cache.set("读取文件", ["read_file", "search_files"], 0.90, "文件操作")
    
    # 测试命中
    entry = await cache.get("查询天气")
    assert entry is not None, "应该命中缓存"
    assert entry.tools == ["weather"], "工具列表应该匹配"
    print("[OK] 缓存命中测试通过")
    
    # 测试未命中
    entry = await cache.get("不存在的查询")
    assert entry is None, "不应该命中缓存"
    print("[OK] 缓存未命中测试通过")
    
    # 测试统计
    stats = cache.get_stats()
    assert stats["hits"] == 1, f"期望1次命中，实际{stats['hits']}"
    assert stats["misses"] == 1, f"期望1次未命中，实际{stats['misses']}"
    assert stats["size"] == 3, f"期望大小3，实际{stats['size']}"
    print(f"[OK] 缓存统计正确: {stats}")
    
    # 测试淘汰
    for i in range(100):
        await cache.set(f"查询{i}", [f"tool{i}"], 0.5, f"测试{i}")
    stats = cache.get_stats()
    assert stats["size"] <= 100, f"缓存大小应该<=100，实际{stats['size']}"
    print(f"[OK] 缓存淘汰测试通过 (size={stats['size']})")


@pytest.mark.asyncio
async def test_batch_checkpoint():
    """测试批量 Checkpoint 写入功能。"""
    from app.runtime.batch_checkpoint import BatchCheckpointManager
    from app.runtime.checkpoint import CheckpointManager
    from app.runtime.state import AgentState
    import tempfile
    import os
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        
        # 创建基础管理器
        base_manager = CheckpointManager(db_path=db_path)
        
        # 创建批量管理器
        batch_manager = BatchCheckpointManager(
            base_manager,
            flush_interval=0.5,
            max_buffer_size=5,
        )
        await batch_manager.start()
        
        try:
            # 测试缓冲写入
            states = []
            for i in range(5):
                state = AgentState(
                    run_id=f"run_{i}",
                    session_id="test",
                    user_input=f"测试消息 {i}"
                )
                state.status = "running"
                state.node = "prepare"
                states.append(state)
            
            # 批量缓冲写入
            for state in states:
                await batch_manager.save(state)
            
            # 验证缓冲大小
            assert batch_manager.buffer_size == 5, f"期望缓冲5条，实际{batch_manager.buffer_size}"
            print(f"[OK] 缓冲写入测试通过 (buffer_size={batch_manager.buffer_size})")
            
            # 等待自动刷新
            await asyncio.sleep(0.8)
            
            # 验证数据已写入数据库
            for state in states:
                loaded = await base_manager.load(state.run_id)
                assert loaded is not None, f"无法加载 {state.run_id}"
            print("[OK] 批量刷盘测试通过")
            
            # 测试强制刷盘
            for i in range(5, 8):
                state = AgentState(run_id=f"force_run_{i}", session_id="test")
                state.status = "done"
                await batch_manager.save(state)
            
            await batch_manager.flush_all()
            
            # 验证强制刷盘的数据
            for i in range(5, 8):
                loaded = await base_manager.load(f"force_run_{i}")
                assert loaded is not None, f"无法加载 force_run_{i}"
            print("[OK] 强制刷盘测试通过")
            
        finally:
            await batch_manager.stop()
        
        # 验证统计
        stats = batch_manager.get_stats()
        assert stats["flushed_count"] >= 8, f"期望至少8次刷盘，实际{stats['flushed_count']}"
        print(f"[OK] 统计信息正确: {stats}")


@pytest.mark.asyncio
async def test_cache_integration():
    """测试缓存与 Agent 的集成。"""
    from app.core.tool_route_cache import get_tool_route_cache, clear_tool_route_cache
    
    # 清理之前的缓存
    await clear_tool_route_cache()
    
    cache = get_tool_route_cache()
    assert cache is not None, "缓存单例应该存在"
    
    # 写入测试数据
    await cache.set("测试查询", ["tool1", "tool2"], 0.9, "测试原因")
    
    # 读取验证
    entry = await cache.get("测试查询")
    assert entry is not None, "应该能读到缓存数据"
    assert entry.tools == ["tool1", "tool2"], "工具列表应该匹配"
    
    print("[OK] 缓存集成测试通过")


async def main():
    """运行所有测试。"""
    print("\n=== 性能优化验证测试 ===\n")
    
    tests = [
        ("工具路由缓存", test_tool_route_cache),
        ("批量 Checkpoint 写入", test_batch_checkpoint),
        ("缓存集成测试", test_cache_integration),
    ]
    
    passed = 0
    failed = 0
    
    for name, test in tests:
        try:
            print(f"\n--- 测试: {name} ---")
            t0 = time.time()
            await test()
            elapsed = time.time() - t0
            print(f"[OK] {name} 测试通过 ({elapsed:.2f}s)")
            passed += 1
        except Exception as e:
            print(f"✗ {name} 测试失败: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    
    print(f"\n=== 测试结果: {passed} 通过, {failed} 失败 ===\n")
    
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
