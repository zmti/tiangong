#!/usr/bin/env python3
"""天工智能体 CLI 工具。

用法:
    python cli.py                    # 进入交互模式
    python cli.py "你好"             # 单次执行
    python cli.py --provider qwen    # 使用通义千问
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import argparse
import logging
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))


def _force_utf8_stdio() -> None:
    """Windows 下 stdout/stdin 默认 GBK, 而 LLM 返回的是 UTF-8 中文, 直接 print 会乱码或
    编码报错(表现为"会话不能输出信息")。这里把标准流重配置为 UTF-8 + errors=replace,
    使任何终端(GBK/UTF-8)下中文都能正常显示, 个别无法编码的字符降级为 ? 而不中断。"""
    for stream, name in ((sys.stdout, "stdout"), (sys.stdin, "stdin")):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            # 非可重配置流(如已关闭/重定向到非 TextIO)时静默跳过, 不影响功能
            pass
    # 同时设环境变量兜底(对子进程/后续 print 也生效)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


_force_utf8_stdio()

class Color:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RED = '\033[31m'
    GREEN = '\033[32m'
    YELLOW = '\033[33m'
    CYAN = '\033[36m'

    @staticmethod
    def use_color() -> bool:
        """只有 TTY 且终端支持 ANSI 转义才上色。

        - 非 TTY(管道/重定向)一律不上色, 避免输出混入 \033[ 控制符。
        - Windows: Win10+ 终端(CMD/PowerShell)普遍支持 ANSI 转义, 而 run_cli.bat
          用 chcp 65001 设了 UTF-8 码页, 说明预期 Windows 也彩色; 旧逻辑
          `os.name != 'nt'` 直接排除 nt 导致 Windows 恒无色, 与 chcp 65001 矛盾。
          这里改为: TTY 即上色(Windows 下额外尝试启用 VT 处理, 失败则降级无色)。
        - 可用 NO_COLOR 环境变量强制关闭(常见约定), FORCE_COLOR 强制开启。
        """
        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("FORCE_COLOR"):
            return True
        if not sys.stdout.isatty():
            return False
        if os.name == "nt":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                # SetConsoleOutputCP(65001) 使控制台按 UTF-8 解释, 同时启用 ANSI
                kernel32.SetConsoleOutputCP(65001)
                kernel32.SetConsoleCP(65001)
                # 启用 VT 处理, 让 \033[ 转义生效; 返回 0 表示不支持, 降级无色
                handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
                mode = ctypes.c_uint32()
                if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                    return False
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
            except Exception:
                # 无控制台/重定向到非真实 TTY 时静默降级为无色
                return False
        return True

def cprint(text: str = '', color: str = '', bold: bool = False, dim: bool = False) -> None:
    """打印(可选着色)。text 缺省为空字符串, 可空调用以换行。"""
    if not Color.use_color():
        print(text, flush=True)
        return
    parts = []
    if bold:
        parts.append(Color.BOLD)
    if dim:
        parts.append(Color.DIM)
    if color:
        parts.append(color)
    parts.append(text)
    parts.append(Color.RESET)
    print(''.join(parts), flush=True)

class TGCli:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.session_id = None
        self.agent = None
        self.history = []
        self.setup_logging()

    def setup_logging(self) -> None:
        level = logging.DEBUG if self.args.verbose else logging.WARNING
        logging.basicConfig(level=level)
        logging.getLogger('httpx').setLevel(logging.WARNING)
        if not self.args.verbose:
            # 交互对话界面默认不显示库内部 WARNING(如 "empty answer #1 will retry" 等),
            # 避免它们混进对话流里;用 -v 可恢复完整日志用于排障。
            logging.getLogger('app').setLevel(logging.ERROR)
            logging.getLogger('app.core').setLevel(logging.ERROR)
            logging.getLogger('app.runtime').setLevel(logging.ERROR)

    def show_banner(self) -> None:
        cprint("╔══════════════════════════════════════╗", Color.CYAN, bold=True)
        cprint("║     天工智能体 - CLI               ║", Color.CYAN, bold=True)
        cprint("╚══════════════════════════════════════╝", Color.CYAN, bold=True)

    async def init_agent(self) -> None:
        from app.core.agent import Agent
        from app.core.llm import LLMClient
        from app.config import settings

        if self.args.session:
            self.session_id = self.args.session
        elif self.args.interactive:
            # 交互模式默认用一个固定会话名, 重开 CLI 能续接上次会话(而非每次新时间戳)。
            # 想要全新会话用 -s <名称>。
            self.session_id = "cli_default"
        else:
            self.session_id = f"cli_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        # 交互模式: 载入该 session 的历史, 实现跨启动的多轮记忆续接
        if self.args.interactive and not self.args.no_history:
            self.history = await self._load_history(self.session_id)
            if self.history:
                cprint(f"(已载入会话 {self.session_id} 的 {len(self.history)} 条历史, 可继续上次话题)", dim=True)

        # 构建 LLMClient，应用 provider 和 model 覆盖
        provider = self.args.provider or settings.agent_llm_provider
        model = self.args.model
        llm = LLMClient(provider=provider, model=model)
        # 记录实际解析到的 provider/model,供报错时给出可操作提示
        self._resolved_provider = llm.provider
        self._resolved_model = llm.model

        # 轻量模式: 交互多轮会话默认开启(跳过规划/工具路由/记忆注入等重准备段),
        # 避免 LLM 限流/欠费时每个问题都卡满 AGENT_LLM_TIMEOUT。
        # --fast/--no-fast 显式指定时从其指定; 未指定时: 交互模式默认开, 单次执行默认关。
        if self.args.fast is None:
            self.args.fast = self.args.interactive or self.args.message is None

        self.agent = Agent(
            llm=llm,
            session_id=self.session_id,
            mode="single",  # CLI 模式使用 single，跳过反思/校验加速响应
            fast_mode=self.args.fast,  # 交互轻量模式: 跳过规划/工具路由/记忆注入等重准备段
            # --yolo: 把危险工具全部预批准(approve_tools)。CLI 没有确认 UI, 不开它的话
            # agent_confirm_interrupt 会在批次里遇到任一致命工具时 interrupt 暂停整批、
            # 一个工具都不执行 → 表现为"工具通道异常/返回空"。默认关, 本地开发用 --yolo。
            approve_tools=list(self._yolo_approve()) if getattr(self.args, "yolo", False) else None,
        )
        # 保留原生工具调用通道(不要 _disable_native! 关了它, 流式纯文本就解析不出
        # 工具调用, "分析项目/读文件"等需要工具的任务会退化成让"用户粘贴内容" → 看起来
        # 像"工具无法返回结果")。防卡靠的是单次 LLM 超时(AGENT_LLM_TIMEOUT), 不是关工具。
        # 仅当用户显式 --no-tools 时才关闭原生工具(纯聊天场景)。
        if getattr(self.args, "no_tools", False):
            self.agent._disable_native()
        # CLI 是本地开发场景、没有确认 UI。默认放行本地工具(跳过 interrupt 式确认),
        # 否则默认路径下 run_terminal/write_file 等命中 confirm 时整批工具被拦、全回空,
        # 表现为"工具通道异常/无法读写"。只有 --strict 才保留逐工具人工确认。
        # 放行名单: 本地副作用工具(yolo 名单); 需要更强限制用 --strict。
        auto_approve = getattr(self.args, "yolo", False) or not getattr(self.args, "strict", False)
        if auto_approve:
            names = self._yolo_approve()
            self.agent.approve_tools.extend(names)
            # 关键: 写进"实例级基线批准"(_baseline_approved)。_reset_run_state 每轮
            # 会把 _runtime_approved 恢复成基线 —— 若不写基线, 第 1 轮 run_stream 入口
            # 的 _reset_run_state 就会把这些放行名单冲掉, 危险工具仍触发 interrupt 卡死
            # (这正是 --yolo 之前形同虚设的根因)。
            self.agent._baseline_approved = set(names)
            self.agent._runtime_approved.update(names)

        if self.args.max_steps:
            self.agent.max_steps = self.args.max_steps

        cprint(f"会话: {self.session_id} | 模式: {self.args.mode} | Provider: {provider} | 模型: {model or llm.model}", dim=True)

    async def _load_history(self, session_id: str) -> list[dict]:
        """从 session store 载入历史(最近 40 条), 失败返回空。"""
        if not session_id:
            return []
        try:
            from app.memory.session import get_session_store
            store = get_session_store()
            hist = await store.get_history(session_id)
            return hist[-40:] if hist else []
        except Exception:
            return []

    def _yolo_approve(self) -> list[str]:
        """--yolo 时返回需预批准的危险工具名单(所有会命中 confirm 的工具)。

        这些工具会被加入 Agent.approve_tools / _runtime_approved, 跳过 interrupt
        式确认, 使 CLI 无 UI 环境下任务能跑完。"""
        from app.config import settings
        tools = [s.strip() for s in settings.agent_dangerous_tools.split(",") if s.strip()]
        # 兜底: 常见危险/副作用工具, 确保本地开发能自动放行
        for t in ("run_terminal", "run_code", "write_file", "edit_file", "delete_file",
                  "apply_patch", "send_email", "delete_email"):
            if t not in tools:
                tools.append(t)
        return tools

    async def run_message(self, message: str) -> None:
        if not self.agent:
            await self.init_agent()

        # 交互模式默认流式(避免 LLM 等待期间界面无任何输出),单次执行默认非流式。
        # self.args.stream 可能为 None(未显式指定),按场景取默认。
        stream = self.args.stream
        if stream is None:
            stream = self.args.interactive or self.args.message is None

        # 交互模式 input() 已显示 "你: 输入" 这一行, 这里不再重打一遍(避免 "你: 你: ..." 双前缀)。
        # 非交互单次执行没有 input 提示符, 需要回显用户输入。
        if not self.args.interactive:
            cprint(f"你: {message}", bold=True)

        if stream:
            await self._run_streamed(message)
            return

        try:
            # 不默认套外层 asyncio.wait_for:Agent 内部已为"单次 LLM 调用"与"流式空闲"
            # 设了独立超时(见 LLMClient._invoke / _stream_with_idle_timeout),外层再套一个
            # 固定 llm_timeout*3 的总闸会在多工具/多轮任务还没跑完时就误杀,表现为
            # "一直执行超时"。默认不限制总时长,只限制单次 LLM;需要硬上限时用 --timeout。
            run_coro = self.agent.run(message, history=self.history)
            if self.args.timeout and self.args.timeout > 0:
                result = await asyncio.wait_for(run_coro, timeout=int(self.args.timeout))
            else:
                result = await run_coro

            # 显示回答
            answer = result.answer or "(无回答)"
            cprint(f"AI: {answer}", Color.GREEN)

            if self.args.verbose:
                cprint(f"  [模型] {result.model}", dim=True)
                cprint(f"  [步骤] {len(result.steps)} 步", dim=True)

            # 保存历史:无论成败都记 user 提问(否则失败的轮次丢失上下文),
            # answer 有内容才记 assistant;历史做长度截断避免多轮后撑爆上下文预算
            if not self.args.no_history:
                try:
                    from app.memory.session import get_session_store
                    store = get_session_store()
                    await store.add_message(self.session_id, "user", content=message)
                    if result.answer:
                        await store.add_message(self.session_id, "assistant", content=result.answer)
                    self.history = await self._load_capped_history(store)
                except Exception:
                    pass

        except KeyboardInterrupt:
            cprint("[已中断]", Color.YELLOW)
        except asyncio.TimeoutError:
            cprint(f"[错误] 执行超时（超过 {self.args.timeout} 秒）。默认不设总超时;任务复杂可调大 --timeout。", Color.RED)
        except Exception as e:
            cprint(f"[错误] {self._llm_hint(e)}", Color.RED)
            if self.args.verbose:
                import traceback
                traceback.print_exc()

    def _llm_hint(self, e: Exception) -> str:
        """把 LLM 异常收敛为单行可操作提示(不再刷屏/多空行)。"""
        msg = str(e)
        low = msg.lower()
        prov = f"provider={self._resolved_provider} model={self._resolved_model}"
        if "arrears" in low or "arrearage" in low or "overdue" in low or "欠费" in msg:
            return f"{msg} —— 账户欠费/未开通(Arrearage)({prov})。请在服务商后台充值/开通服务。"
        if "429" in msg or "rate limit" in low or "quota" in low or "limit" in low:
            return f"{msg} —— API 限流/配额({prov})。可换服务商: .env 设 AGENT_LLM_PROVIDER=qwen/deepseek/ollama,或 --provider qwen。"
        if "401" in msg or "403" in msg or "unauthorized" in low or "api key" in low:
            return f"{msg} —— API Key 无效/无权限({prov})。请检查 .env 对应 *_API_KEY。"
        if "access denied" in low:
            return f"{msg} —— 访问被拒(欠费/未开通/无权限)({prov})。请确认账号正常且已开通对应模型服务。"
        if "404" in msg or "not found" in low:
            return f"{msg} —— 模型/路径不存在({prov})。请检查 *_MODEL / *_BASE_URL。"
        if "timeout" in low or "timed out" in low:
            return f"{msg} —— 单次 LLM 调用超时。可调大 .env 的 AGENT_LLM_TIMEOUT(秒)或换更快 provider。"
        if "not configured" in low or "未配置" in msg:
            return f"{msg} —— provider '{self._resolved_provider}' 缺配置。请在 .env 补齐 Key/URL/Model。"
        return f"{msg} ({prov})"

    async def _run_streamed(self, message: str) -> None:
        """流式执行:与桌面端/后端 API 相同的逐 token + 工具进度事件流。

        长任务不再"黑盒卡住":工具调用/计划/思考都会实时打印,token 逐字流出。
        空闲/总时长超时由 LLMClient 内部(stream_idle/total)负责,这里不再套外层总闸。
        """
        stream = self.agent.run_stream(message, history=self.history)
        answer_buf: list[str] = []
        try:
            async for event in stream:
                etype = event.get("type")
                if etype == "token":
                    print(event.get("content", ""), end="", flush=True)
                    answer_buf.append(event.get("content", ""))
                elif etype == "thinking":
                    pass  # 推理内容默认不刷屏,--verbose 时再输出
                elif etype == "tool_call_start":
                    # 工具开始: 逐个显示 工具名 + 参数摘要
                    for t in (event.get("tools") or []):
                        cprint(f"  [工具开始] {t.get('name','?')} {self._arg_brief(t.get('arguments',''))}",
                               Color.CYAN, dim=True)
                elif etype == "tool":
                    # 工具完成: 显示 工具名 + 结果摘要
                    result = event.get("result", "")
                    cprint(f"  [工具结果] {event.get('name','?')}: {self._brief(result)}",
                           Color.CYAN, dim=True)
                elif etype == "plan":
                    cprint(f"  [计划] {event.get('action', '')}", Color.CYAN, dim=True)
                elif etype == "done":
                    cprint()  # token 流结束,换行
                elif etype == "heartbeat":
                    pass
        except KeyboardInterrupt:
            print()
            cprint("[已中断]", Color.YELLOW)
        except Exception as e:
            cprint()
            cprint(f"[错误] {e}", Color.RED)
            low = str(e).lower()
            if "rate limit" in low or "429" in str(e) or "limit" in low:
                cprint(f"提示: API 限流/配额。provider={self._resolved_provider} model={self._resolved_model}。", Color.YELLOW, dim=True)
            if self.args.verbose:
                import traceback
                traceback.print_exc()
            print()
        finally:
            # 关键: 流被打断(中断/异常)或提前退出时, 必须 aclose() 关闭 async generator,
            # 否则底层 httpcore 流式响应生成器遗留, 事件循环关闭时 asyncio 报
            # "Task exception was never retrieved / async generator 未清理" 噪音。
            try:
                if hasattr(stream, "aclose"):
                    await stream.aclose()
            except Exception:
                pass

        answer = "".join(answer_buf).strip()
        # 流式模式 token 已逐字打印完整答案, 不再重复输出 "AI: ..."(避免同一句出现两次)。
        # 仅在没有任何 token 产出时, 才提示一句。
        if not answer and not self.args.verbose:
            cprint("(无返回内容)", dim=True)
        if self.args.verbose and answer:
            cprint("  (已按 token 实时输出,上面为完整答案)", dim=True)

        if not self.args.no_history:
            try:
                from app.memory.session import get_session_store
                store = get_session_store()
                await store.add_message(self.session_id, "user", content=message)
                if answer:
                    await store.add_message(self.session_id, "assistant", content=answer)
                self.history = await self._load_capped_history(store)
            except Exception:
                pass

    @staticmethod
    def _brief(text, limit=200) -> str:
        """把任意结果/参数压成单行摘要, 避免刷屏(换行→空格, 截断)。"""
        if text is None:
            return ""
        s = str(text).replace("\r", "").replace("\n", " ")
        s = " ".join(s.split())
        return s[:limit] + ("…" if len(s) > limit else "")

    @staticmethod
    def _arg_brief(arguments, limit=80) -> str:
        """工具参数摘要: 尽量从 JSON 里挑出最相关的键值, 失败则原样截断。"""
        if not arguments:
            return ""
        s = str(arguments).strip()
        try:
            import json as _json
            d = _json.loads(s)
            if isinstance(d, dict):
                priority = ("query","path","file","file_path","files","pattern","cmd","command","code","url","language","limit")
                shown = []
                for k in priority:
                    if k in d:
                        shown.append(f"{k}={TGCli._brief(d[k], 40)}")
                if not shown:
                    shown = [f"{k}={TGCli._brief(v,30)}" for k,v in list(d.items())[:3]]
                return " ".join(shown)
        except Exception:
            pass
        return TGCli._brief(s, limit)

    async def _load_capped_history(self, store) -> list[dict]:
        """取会话历史并截断到最近 N 条,避免多轮交互后历史无限增长撑爆上下文预算。"""
        hist = await store.get_history(self.session_id)
        cap = 40  # 保留最近 40 条(20 轮问答),更早的交给 Agent 的摘要压缩处理
        return hist[-cap:] if len(hist) > cap else hist

    def show_help(self) -> None:
        print()
        cprint("命令:", Color.CYAN, bold=True)
        cprint("  /help      显示帮助", dim=True)
        cprint("  /quit      退出", dim=True)
        cprint("  /history   查看历史消息数", dim=True)
        cprint("  /models    列出当前 provider 的可用模型", dim=True)
        cprint("  /model     查看当前模型", dim=True)
        cprint("  /model <名称>  切换模型", dim=True)
        print()

    async def list_models(self, provider: str = None) -> None:
        """列出可用模型，可选指定 provider。"""
        try:
            from app.api.chat import list_models as api_list_models
            from app.config import settings

            # 使用传入的 provider，或当前配置的 provider
            provider = provider or self.args.provider or settings.agent_llm_provider
            result = await api_list_models(provider)

            print()
            cprint(f"可用模型 (provider: {result.get('provider', 'unknown')}):", Color.CYAN, bold=True)
            cprint(f"当前默认: {result.get('default', 'N/A')}", dim=True)
            print()

            for model in result.get("models", []):
                model_id = model.get("id", model.get("name", "unknown"))
                model_name = model.get("name", model_id)
                desc = model.get("description", "")
                marker = "✓" if model_id == result.get("default") else " "
                print(f"  {marker} {model_id:20s} {model_name:30s} {desc}")
            print()

        except Exception as e:
            cprint(f"获取模型列表失败: {e}", Color.RED)
            cprint(f"提示: 请检查 provider '{provider}' 是否正确配置", Color.YELLOW)

    async def switch_model(self, model_name: str) -> None:
        """切换模型并重新初始化 Agent。"""
        self.args.model = model_name
        # 重新初始化 Agent 以应用新模型
        self.agent = None
        await self.init_agent()
        cprint(f"模型已切换为: {model_name}", Color.GREEN)
        cprint("请继续提问", dim=True)

    async def interactive_loop(self) -> None:
        self.show_banner()
        await self.init_agent()

        while True:
            try:
                user_input = input(f"{Color.CYAN}你: {Color.RESET}").strip()
                sys.stdout.flush()
            except (EOFError, KeyboardInterrupt):
                print()
                cprint("再见!", dim=True)
                break

            if not user_input:
                continue

            if user_input.startswith('/'):
                cmd = user_input.split()[0].lower()  # 只小写化命令，保留参数原样
                if cmd in ('/quit', '/exit', '/q'):
                    cprint("再见!", dim=True)
                    break
                elif cmd == '/help':
                    self.show_help()
                elif cmd == '/history':
                    cprint(f"历史消息数: {len(self.history)}", dim=True)
                elif cmd == '/models':
                    parts = user_input.split()
                    if len(parts) > 1:
                        await self.list_models(parts[1])
                    else:
                        await self.list_models()
                elif cmd == '/model':
                    parts = user_input.split()
                    if len(parts) > 1:
                        await self.switch_model(parts[1])
                    else:
                        cprint(f"当前模型: {self.args.model or 'default'}", dim=True)
                else:
                    cprint(f"未知命令: {user_input}", Color.YELLOW)
                continue

            await self.run_message(user_input)

    async def single_mode(self, message: str) -> None:
        await self.init_agent()
        await self.run_message(message)
        print()

    async def run(self) -> None:
        if self.args.message:
            await self.single_mode(self.args.message)
        else:
            await self.interactive_loop()

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="天工智能体 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python cli.py "你好"
  python cli.py -i
  python cli.py --provider qwen "使用千问回答"
        """
    )
    parser.add_argument("message", nargs="?", help="消息内容")
    parser.add_argument("-i", "--interactive", action="store_true", help="交互模式")
    parser.add_argument("-s", "--session", help="会话 ID")
    parser.add_argument("-m", "--mode", choices=["auto", "single", "review", "ensemble"], default="auto")
    parser.add_argument("--provider", choices=["deepseek", "qwen", "ollama", "compat"], help="LLM 服务商")
    parser.add_argument("--max-steps", type=int, help="最大步数")
    parser.add_argument("--model", help="指定模型名称")
    parser.add_argument("--timeout", type=int, default=0,
                        help="整个 Agent 运行的硬超时(秒);0=不限制(默认),单次 LLM 仍受 AGENT_LLM_TIMEOUT 约束")
    parser.add_argument("--stream", action="store_true", default=None,
                        help="流式输出(逐 token + 工具/思考进度);交互模式默认开启,避免卡住时界面无任何输出")
    parser.add_argument("--no-stream", dest="stream", action="store_false",
                        help="关闭流式,一次性等待完整回答(非交互单次执行默认)")
    parser.add_argument("--no-tools", action="store_true",
                        help="关闭原生工具调用(纯聊天);默认保留工具,否则读文件/分析项目等任务无法执行")
    parser.add_argument("--yolo", action="store_true",
                        help="自动批准危险工具(跳过确认流)。默认交互已自动放行本地工具,"
                             "此开关保留以显式表达。--strict 可取消自动放行。")
    parser.add_argument("--strict", action="store_true",
                        help="取消默认的工具自动放行, 每个危险工具都需逐次确认(无 UI 时会卡住, 慎用)")
    # 交互模式默认走轻量路径(跳过规划/工具路由/记忆注入这些带 AGENT_LLM_TIMEOUT 的
    # 重准备段),避免多轮会话里每个问题都为准备阶段空转满超时。需要完整 Agent 能力时
    # 用 --no-fast 关闭。
    fast_group = parser.add_mutually_exclusive_group()
    fast_group.add_argument("--fast", dest="fast", action="store_true", default=None,
                            help="轻量交互模式: 跳过任务规划/工具路由/记忆注入,只做纯问答")
    fast_group.add_argument("--no-fast", dest="fast", action="store_false",
                            help="关闭轻量模式,启用完整 Agent 准备阶段")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    parser.add_argument("--no-history", action="store_true", help="不使用历史")
    return parser.parse_args()

async def main() -> None:
    args = parse_args()
    cli = TGCli(args)
    await cli.run()

if __name__ == "__main__":
    asyncio.run(main())

# 扩展 CLI 支持模型选择
