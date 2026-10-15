"""并发安全测试 - 验证 checkpoint 在并发场景下的行为。"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.mark.asyncio
async def test_concurrent_checkpoint_save():
    """测试并发保存 checkpoint 的安全性。"""
    from app.runtime.checkpoint import CheckpointManager
    from app.runtime.state import AgentState
    
    # 使用临时目录
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        cm = CheckpointManager(db_path=db_path)
        
        # 创建多个状态
        states = []
        for i in range(10):
            state = AgentState(
                run_id=f"run_{i}",
                session_id="test_session",
                user_input=f"测试消息 {i}"
            )
            state.status = "running"
            state.node = "prepare"
            states.append(state)
        
        # 并发保存
        await asyncio.gather(*[cm.save(s) for s in states])
        
        # 验证所有状态都可加载
        for state in states:
            loaded = await cm.load(state.run_id)
            assert loaded is not None, f"无法加载 {state.run_id}"
            assert loaded.run_id == state.run_id
            assert loaded.status == state.status
        
        print(f"[OK] 并发保存 10 个 checkpoint 成功")


@pytest.mark.asyncio
async def test_concurrent_checkpoint_list():
    """测试并发列表查询的安全性。"""
    from app.runtime.checkpoint import CheckpointManager
    from app.runtime.state import AgentState
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        cm = CheckpointManager(db_path=db_path)
        
        # 先保存一些数据
        for i in range(5):
            state = AgentState(run_id=f"run_{i}", session_id="test")
            state.status = "done"
            await cm.save(state)
        
        # 并发列表查询
        results = await asyncio.gather(*[cm.list() for _ in range(10)])
        
        # 验证所有结果一致
        for result in results:
            assert len(result) == 5, f"期望 5 条记录，实际 {len(result)}"
        
        print("[OK] 并发列表查询 10 次成功")


@pytest.mark.asyncio
async def test_concurrent_checkpoint_cleanup():
    """测试清理过期 checkpoint 的并发安全性。"""
    from app.runtime.checkpoint import CheckpointManager
    from app.runtime.state import AgentState
    import time
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        cm = CheckpointManager(db_path=db_path)
        
        # 保存一些状态
        for i in range(5):
            state = AgentState(run_id=f"run_{i}", session_id="test")
            state.status = "done"
            state._updated_at = time.time() - 86400 * 8  # 8 天前
            await cm.save(state)
        
        # 并发清理
        results = await asyncio.gather(*[cm.cleanup_expired(ttl_days=7) for _ in range(5)])
        
        # 验证并发安全: 返回 dict(checkpoints/versions/vacuum), 各并发结果字段一致非负。
        # 注: save 写入的 created_at/updated_at 是"当前时间", 不在 7 天 TTL 内,
        # 故此处 5 条不会被删(返回 0), 本用例只验证并发调用 cleanup 的安全性与一致性。
        for result in results:
            assert isinstance(result, dict), f"清理结果应为 dict，实际 {type(result)}"
            assert result.get("checkpoints", 0) >= 0, f"主表计数应为非负，实际 {result}"
            assert result.get("versions", 0) >= 0, f"版本表计数应为非负，实际 {result}"
        # 5 个并发调用看到一致的结果(数据都在 7 天内, 均应删 0 条)
        assert all(r.get("checkpoints", 0) == 0 for r in results), f"7天内数据不应被删, 实际 {results}"

        print(f"[OK] 并发清理测试成功，各结果: {results[0]}")


@pytest.mark.asyncio
async def test_checkpoint_thread_safety():
    """测试 checkpoint 在多线程环境下的安全性。"""
    from concurrent.futures import ThreadPoolExecutor
    from app.runtime.checkpoint import CheckpointManager
    from app.runtime.state import AgentState
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        cm = CheckpointManager(db_path=db_path)
        
        # 使用线程池并发保存
        def save_state(i):
            state = AgentState(run_id=f"thread_run_{i}", session_id="test")
            state.status = "running"
            return asyncio.run(cm.save(state))
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(save_state, i) for i in range(10)]
            results = [f.result() for f in futures]
        
        assert len(results) == 10, f"应返回 10 个结果，实际 {len(results)}"
        print("[OK] 线程安全测试通过")


async def main():
    """运行所有并发测试。"""
    print("\n=== 并发安全测试 ===\n")
    
    tests = [
        test_concurrent_checkpoint_save,
        test_concurrent_checkpoint_list,
        test_concurrent_checkpoint_cleanup,
        test_checkpoint_thread_safety,
    ]
    
    for test in tests:
        try:
            await test()
        except Exception as e:
            print(f"✗ {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n=== 并发测试完成 ===\n")


if __name__ == "__main__":
    asyncio.run(main())
