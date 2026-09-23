"""复杂任务执行诊断脚本。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def check_configuration():
    """检查当前配置是否适合复杂任务。"""
    print("\n=== 配置检查 ===\n")
    
    try:
        from app.config import settings
        
        checks = [
            ("最大步骤数", "agent_max_steps", 20, 100),
            ("上下文字符预算", "context_char_budget", 100000, 200000),
            ("Token 预算", "agent_token_budget", 0, 50000),
            ("计划步骤上限", "agent_plan_max_steps", 6, 10),
            ("工具预选数量", "agent_tool_router_max_tools", 10, 20),
            ("反思开关", "agent_reflect", True, False),
            ("反思轮次", "agent_reflect_max_rounds", 1, 0),
            ("校验开关", "agent_verify", True, False),
        ]
        
        issues = []
        for name, attr, current, recommended in checks:
            value = getattr(settings, attr, current)
            status = "✓" if value >= recommended else "✗"
            print(f"{status} {name}: {value} (建议: {recommended})")
            if value < recommended:
                issues.append(f"{name} 设置为 {value}，建议至少 {recommended}")
        
        print()
        if issues:
            print("发现问题:")
            for issue in issues:
                print(f"  - {issue}")
            print()
            print("建议修改 .env 文件:")
            for name, attr, current, recommended in checks:
                if getattr(settings, attr, current) < recommended:
                    env_name = attr.upper()
                    print(f"  {env_name}={recommended}")
        else:
            print("✓ 配置适合复杂任务")
            
    except Exception as e:
        print(f"✗ 配置检查失败: {e}")
        import traceback
        traceback.print_exc()


async def check_api_health():
    """检查 API 健康状态。"""
    print("\n=== API 健康检查 ===\n")
    
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("http://localhost:8080/health")
            if resp.status_code == 200:
                data = resp.json()
                print("✓ 服务健康")
                print(f"  版本: {data.get('version', 'unknown')}")
                print(f"  Provider: {data.get('provider', 'unknown')}")
                
                # 检查数据库
                db_status = data.get('databases', {})
                for db_name, status in db_status.items():
                    if not status.get('connected', False):
                        print(f"  ✗ {db_name} 数据库连接失败")
                    else:
                        print(f"  ✓ {db_name} 数据库正常 ({status.get('records', 0)} 条记录)")
            else:
                print(f"✗ 健康检查失败: HTTP {resp.status_code}")
    except Exception as e:
        print(f"✗ API 连接失败: {e}")
        print("  请确保服务正在运行: python -m uvicorn app.main:app --port 8080")


async def test_complex_task():
    """测试复杂任务执行。"""
    print("\n=== 复杂任务测试 ===\n")
    
    test_cases = [
        ("简单任务", "今天是几号？", "auto"),
        ("中等任务", "帮我搜索最近的 AI 新闻并总结", "auto"),
        ("复杂任务", "分析这个项目结构，找出潜在 bug，生成测试用例和报告", "review"),
    ]
    
    try:
        import httpx
        async with httpx.AsyncClient(timeout=120.0) as client:
            for name, message, mode in test_cases:
                print(f"\n测试: {name}")
                print(f"  消息: {message[:50]}...")
                print(f"  模式: {mode}")
                
                try:
                    resp = await client.post(
                        "http://localhost:8080/v1/chat",
                        json={
                            "message": message,
                            "session_id": f"diag_{name}",
                            "stream": False,
                            "mode": mode,
                        },
                        headers={"Content-Type": "application/json"}
                    )
                    
                    if resp.status_code == 200:
                        data = resp.json()
                        answer = data.get("answer", "")[:100]
                        steps = data.get("steps", [])
                        print(f"  ✓ 成功 (步骤: {len(steps)})")
                        print(f"    回答: {answer}...")
                    else:
                        print(f"  ✗ 失败: HTTP {resp.status_code}")
                        print(f"    错误: {resp.text[:200]}")
                        
                except Exception as e:
                    print(f"  ✗ 异常: {e}")
                    
    except Exception as e:
        print(f"✗ 测试执行失败: {e}")


async def show_trace_stats():
    """显示 trace 统计信息。"""
    print("\n=== Trace 统计 ===\n")
    
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            # 获取统计
            resp = await client.get("http://localhost:8080/v1/traces/stats")
            if resp.status_code == 200:
                data = resp.json()
                print("最近运行统计:")
                print(f"  LLM 调用次数: {data.get('llm_calls', 0)}")
                print(f"  工具调用次数: {data.get('tool_calls', 0)}")
                print(f"  错误次数: {data.get('errors', 0)}")
                print(f"  Token 用量: {data.get('total_tokens', 0)}")
                print(f"  成本估算: ${data.get('estimated_cost_usd', 0):.4f}")
            else:
                print(f"✗ 获取统计失败: HTTP {resp.status_code}")
            
            # 获取最近运行
            resp = await client.get("http://localhost:8080/v1/traces/runs?limit=5")
            if resp.status_code == 200:
                runs = resp.json()
                print("\n最近 5 次运行:")
                for run in runs:
                    print(f"  - {run.get('run_id', 'unknown')[:8]}... | "
                          f"步骤: {run.get('steps', 0)} | "
                          f"Token: {run.get('tokens', 0)} | "
                          f"耗时: {run.get('duration', 0):.1f}s | "
                          f"{'✓' if run.get('ok') else '✗'}")
            else:
                print(f"✗ 获取运行列表失败: HTTP {resp.status_code}")
                
    except Exception as e:
        print(f"✗ Trace 检查失败: {e}")


async def main():
    """主诊断函数。"""
    print("\n" + "=" * 60)
    print("复杂任务执行诊断工具")
    print("=" * 60)
    
    await check_configuration()
    await check_api_health()
    await test_complex_task()
    await show_trace_stats()
    
    print("\n" + "=" * 60)
    print("诊断完成")
    print("=" * 60)
    print("\n建议操作:")
    print("1. 根据配置检查结果调整 .env 文件")
    print("2. 重启服务使配置生效")
    print("3. 重新运行诊断验证")
    print("4. 查看 trace 日志分析具体失败原因")
    print()


if __name__ == "__main__":
    asyncio.run(main())
