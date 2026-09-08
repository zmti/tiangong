"""验证清理机制的测试套件。"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.mark.asyncio
async def test_telemetry_cleanup():
    """测试 Telemetry 清理功能。"""
    from app.core.telemetry import Tracer
    
    tracer = Tracer(max_buffer=100, retention_days=0.001)  # 极短 TTL 用于测试
    
    # 添加一些模拟事件
    now = time.time()
    for i in range(10):
        tracer.record({
            "type": "test",
            "ts": now - (i * 100),  # 不同时间戳
            "data": f"test_{i}",
        })
    
    # 验证事件已添加
    assert len(tracer._buffer) == 10, f"期望10条事件，实际{len(tracer._buffer)}"
    print("[OK] 事件添加测试通过")
    
    # 等待一段时间让事件过期
    await asyncio.sleep(0.1)
    
    # 清理过期事件
    cleaned = await tracer.cleanup_old_events()
    
    # 验证清理结果
    assert cleaned > 0, f"应该清理至少1条事件，实际清理{cleaned}条"
    print(f"[OK] 清理测试通过 (清理了 {cleaned} 条事件)")
    
    # 验证剩余事件
    assert len(tracer._buffer) < 10, f"应该少于10条事件，实际{len(tracer._buffer)}"
    print(f"[OK] 清理后剩余 {len(tracer._buffer)} 条事件")


@pytest.mark.asyncio
async def test_error_formatting():
    """测试统一错误格式。"""
    from app.core.exceptions import (
        BadRequestError,
        UnauthorizedError,
        NotFoundError,
        LLMError,
        format_error_response,
    )
    
    # 测试各种异常类型
    errors = [
        BadRequestError("参数错误", field="username"),
        UnauthorizedError("Token 无效"),
        NotFoundError("用户不存在"),
        LLMError("模型超时", provider="deepseek", model="deepseek-chat"),
    ]
    
    for error in errors:
        response = error.to_response(request_id="test-123")
        
        # 验证响应格式
        assert response.success is False, "success 应该为 False"
        assert response.error_code, "应该有 error_code"
        assert response.message, "应该有 message"
        assert response.request_id == "test-123", "request_id 应该匹配"
        assert response.timestamp > 0, "timestamp 应该有效"
        
        print(f"[OK] 错误格式化测试通过: {error.error_code}")
    
    # 测试通用异常转换
    try:
        raise ValueError("测试异常")
    except Exception as e:
        response = format_error_response(e, request_id="test-456")
        assert response.error_code == "INTERNAL_ERROR"
        print("[OK] 通用异常转换测试通过")


@pytest.mark.asyncio
async def test_session_cleanup():
    """测试会话清理功能。"""
    from app.memory.session import SessionStore
    import tempfile
    import os
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        store = SessionStore(db_path=db_path)
        
        # 创建一些测试会话
        for i in range(5):
            await store.add_message(f"session_{i}", "user", content=f"测试消息 {i}")
            await asyncio.sleep(0.01)
        
        # 验证会话已创建
        sessions = await store.list_sessions(limit=100)
        assert len(sessions) == 5, f"期望5个会话，实际{len(sessions)}"
        print(f"[OK] 创建测试会话通过 ({len(sessions)} 个)")
        
        # 测试 dry_run 清理
        result = await store.cleanup_expired(max_age_days=0, min_messages=0)
        assert result["total_checked"] == 5
        print(f"[OK] Dry run 清理测试通过 (将删除 {result['deleted']} 个)")
        
        # 测试实际清理
        result = await store.cleanup_expired(max_age_days=0, min_messages=0)
        assert result["deleted"] == 5, f"期望删除5个，实际删除{result['deleted']}"
        print(f"[OK] 实际清理测试通过 (删除了 {result['deleted']} 个)")


@pytest.mark.asyncio
async def test_archive_session():
    """测试会话归档功能。"""
    from app.memory.session import SessionStore
    import tempfile
    import os
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        store = SessionStore(db_path=db_path)
        
        # 创建一个有内容的会话
        session_id = "archive_test"
        await store.add_message(session_id, "user", content="第一条消息")
        await store.add_message(session_id, "assistant", content="回复内容")
        await store.add_message(session_id, "user", content="第三条消息")
        
        # 归档会话
        ok = await store.archive_session(session_id)
        assert ok, "归档应该成功"
        print("[OK] 会话归档测试通过")
        
        # 验证归档后的消息数量
        history = await store.get_history(session_id)
        assert len(history) == 1, f"期望1条摘要消息，实际{len(history)}"
        assert history[0].get("kind") == "summary", "应该是摘要类型"
        print("[OK] 归档内容验证通过")


async def main():
    """运行所有测试。"""
    print("\n=== 清理机制验证测试 ===\n")
    
    tests = [
        ("Telemetry 清理", test_telemetry_cleanup),
        ("错误格式化", test_error_formatting),
        ("会话清理", test_session_cleanup),
        ("会话归档", test_archive_session),
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
