"""天工智能体 CLI 工具。

用法:
    python -m app.cli "你好"                    # 单次执行
    python -m app.cli --interactive              # 交互模式
    python -m app.cli --session my-session "hi" # 指定会话
    python -m app.cli --mode review "question"  # 使用评审模式
    python -m app.cli --stream "question"       # 流式输出
"""
from __future__ import annotations

import asyncio
import json
import sys
import argparse
import logging
from datetime import datetime
from pathlib import Path

# 确保项目根目录在 sys.path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="天工智能体 CLI 工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m app.cli "你好"
  python -m app.cli --interactive
  python -m app.cli --session my-session "继续上次的话题"
  python -m app.cli --mode review "分析这个代码"
  python -m app.cli --stream "生成一首诗"
  python -m app.cli --json "查询天气"
        """
    )

    parser.add_argument(
        "message",
        nargs="?",
        default=None,
        help="用户消息(交互模式下可省略)"
    )

    parser.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="进入交互模式"
    )

    parser.add_argument(
        "-s", "--session",
        type=str,
        default=None,
        help="指定会话 ID(默认为 cli_<timestamp>)"
    )

    parser.add_argument(
        "-m", "--mode",
        choices=["auto", "single", "review", "ensemble"],
        default="auto",
        help="协作模式(默认: auto)"
    )

    parser.add_argument(
        "--stream",
        action="store_true",
        help="流式输出结果"
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果"
    )

    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="最大步数(覆盖配置)"
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="显示详细日志"
    )

    parser.add_argument(
        "--no-history",
        action="store_true",
        help="不使用历史上下文"
    )

    return parser.parse_args()


def setup_logging(verbose: bool) -> None:
    """配置日志。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    )
    # 降低第三方库日志级别
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def print_banner() -> None:
    """显示 CLI 横幅。"""
    print("\n" + "=" * 50)
    print("  天工智能体 CLI")
    print("=" * 50)


def get_session_id(session: str | None) -> str:
    """生成会话 ID。"""
    if session:
        return session
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"cli_{timestamp}"


async def run_agent(
    message: str,
    session_id: str,
    mode: str = "auto",
    max_steps: int | None = None,
    use_history: bool = True
):
    """运行 Agent 并返回结果。"""
    from app.core.agent import Agent
    from app.config import settings

    # 构建 Agent
    llm_provider = settings.agent_llm_provider
    agent = Agent(
        session_id=session_id,
        mode=mode,
    )

    if max_steps:
        agent.max_steps = max_steps

    # 获取历史
    history = []
    if use_history:
        try:
            from app.memory.session import get_session_store
            store = get_session_store()
            history = await store.get_history(session_id)
        except Exception as e:
            print(f"[警告] 获取历史失败: {e}")

    return agent, history


def format_output(result, json_output: bool = False) -> None:
    """格式化并输出结果。"""
    if json_output:
        output = {
            "answer": result.answer,
            "model": result.model,
            "steps": len(result.steps),
            "steps_detail": [
                {
                    "step": s.get("step"),
                    "type": s.get("type"),
                    "content": str(s.get("content", ""))[:200]
                }
                for s in result.steps[-5:]  # 只显示最后 5 步
            ]
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print("\n" + "-" * 50)
        print("回答:")
        print("-" * 50)
        print(result.answer)
        print(f"\n[模型] {result.model}")
        print(f"[步骤] 共 {len(result.steps)} 步")


async def run_single(message: str, args: argparse.Namespace) -> None:
    """单次执行模式。"""
    from app.core.agent import Agent

    session_id = get_session_id(args.session)
    agent, history = await run_agent(
        message,
        session_id,
        mode=args.mode,
        max_steps=args.max_steps,
        use_history=not args.no_history
    )

    try:
        if args.stream:
            print("[流式输出中...]")
            async for event in agent.run_stream(message, history=history):
                if event["type"] == "token":
                    print(event["content"], end="", flush=True)
                elif event["type"] == "tool":
                    print(f"\n[工具调用] {event.get('name', 'unknown')}")
                elif event["type"] == "thinking":
                    print(f"\n[思考] {event.get('content', '')[:100]}...")
            print()
        else:
            result = await agent.run(message, history=history)
            format_output(result, args.json)
    except KeyboardInterrupt:
        print("\n[已中断]")
    except Exception as e:
        print(f"\n[错误] {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()


async def run_interactive(args: argparse.Namespace) -> None:
    """交互模式。"""
    from app.core.agent import Agent

    session_id = get_session_id(args.session)
    print(f"\n会话 ID: {session_id}")
    print(f"模式: {args.mode}")
    print("输入 'quit' 或 'exit' 退出\n")

    history = []

    while True:
        try:
            user_input = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("再见!")
            break

        # 执行 Agent
        try:
            agent, _ = await run_agent(
                user_input,
                session_id,
                mode=args.mode,
                max_steps=args.max_steps,
                use_history=True
            )

            if args.stream:
                print("AI: ", end="")
                async for event in agent.run_stream(user_input, history=history):
                    if event["type"] == "token":
                        print(event["content"], end="", flush=True)
                print()
            else:
                result = await agent.run(user_input, history=history)
                print(f"\nAI: {result.answer}")

            # 更新历史
            if not args.no_history:
                from app.memory.session import get_session_store
                store = get_session_store()
                await store.add_message(session_id, "user", content=user_input)
                await store.add_message(session_id, "assistant", content=result.answer)
                history = await store.get_history(session_id)

        except Exception as e:
            print(f"\n[错误] {e}")
            if args.verbose:
                import traceback
                traceback.print_exc()


async def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    print_banner()

    if args.interactive or not args.message:
        await run_interactive(args)
    else:
        await run_single(args.message, args)


if __name__ == "__main__":
    asyncio.run(main())
