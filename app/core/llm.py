"""多 Provider LLM 接入层,统一封装 OpenAI 兼容接口。

支持: deepseek / qwen / ollama / openai / 任意 OpenAI 兼容服务
所有 Provider 均走 /chat/completions OpenAI 兼容协议。
额外能力:原生 function calling、并行工具调用、结构化 JSON 输出、
严格 JSON Schema 输出(chat_json_schema + 校验回灌重试)、瞬态错误指数
退避重试、provider 故障转移、prompt cache 断点、reasoning 模型思维链分离。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from openai import AsyncOpenAI
import httpx

from app.config import settings, get_httpx_proxy
from app.core.telemetry import estimate_cost, tracer

logger = logging.getLogger(__name__)


def _exc_detail(e: BaseException, limit: int = 3) -> str:
    """异常详情: 类型名 + 消息 + traceback 末 N 帧(文件:行号|函数名)。

    桌面端后端 stdout 只进 Electron 主进程控制台、日志文件也可能未就绪,
    仅凭 "LLM 流式调用失败: list index out of range" 无法定位抛出点。
    把调用栈末几帧编进用户可见的错误消息里,截图即可定位,无需回查日志。
    """
    try:
        import traceback as _tb
        frames = _tb.extract_tb(e.__traceback__) or []
        loc = " <- ".join(
            f"{os.path.basename(f.filename)}:{f.lineno}|{f.name}" for f in frames[-limit:]
        )
        msg = str(e).strip()
        base = f"{type(e).__name__}: {msg}" if msg else type(e).__name__
        return base + (f" @ {loc}" if loc else "")
    except Exception:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


# ChatCompletion message 类型定义
Message = dict  # {"role": "user"/"assistant"/"system", "content": str}

# 瞬态错误特征:命中即判定为可重试(网络抖动/5xx 服务不可用/超时限流抖动)。
# 注意:终态错误(配额耗尽/401/404 鉴权与配置错误)由 _NON_RETRYABLE_MARKERS 先行拦截,
# 保证它们立即失败而非长时间退避重试。
# 中文标记:agnes/compat 网关返回中文限流文案(如"调用频率过高"),此前不被识别为
# 可重试 → 立即失败并累计熔断,导致后续请求"熔断冷却中,跳过"。现补齐中文特征。
_RETRYABLE_MARKERS = (
    "timeout", "timed out", "rate limit", "overloaded",
    "connection", "network", "500", "502", "503", "504",
    "temporarily unavailable", "try again",
    # 中文瞬态错误
    "频率", "限流", "过载", "繁忙", "稍后再试", "稍后重试",
    "请求过快", "调用次数", "超时", "网络",
    # 上游网关偶发把自己的校验错误当正文回包(见 _StreamHeadGuard 注释):
    # 属于 provider 侧抖动,重发同一请求通常即恢复,因此归入可重试。
    # 用自造中文标记而非网关原文,避免与 provider 正常回包文本撞车。
    "网关回包异常", "空内容块",
)

# 终态错误特征:命中即判定为不可重试(鉴权失败/配额耗尽/模型/路径配置错误)。
# 这类错误重试只会浪费时间并触发熔断,应立即失败并给出可操作提示。
# 注意:429 不在此列——上游 429 多为"请求频率过高"(瞬时限流,应退避重试);
# 真正的配额耗尽由 "quota"/"配额"/"insufficient" 等标记拦截。
_NON_RETRYABLE_MARKERS = (
    "401", "403", "404", "invalid api key", "incorrect api key",
    "unauthorized", "authentication", "not found", "api key",
    "does not exist", "insufficient", "quota", "exceeded your current quota",
    "配额", "余额不足", "欠费",
    "access denied", "arrearage", "arrears", "overdue", "not in good standing",
    "access_denied", "account",
)


def _is_retryable(error: str) -> bool:
    """可重试的瞬态错误(网络抖动/超时/5xx 服务不可用/瞬时限流),但不含终态错误。"""
    err = error.lower()
    if any(m in err for m in _NON_RETRYABLE_MARKERS):
        return False
    return any(m in err for m in _RETRYABLE_MARKERS)


def _is_rate_limit(error: str) -> bool:
    """限流类错误:需要比普通网络抖动更长的退避时间。"""
    err = error.lower()
    return any(m in err for m in ("rate limit", "429", "频率", "限流", "请求过快"))


# ---- 网关校验错误"被当正文回包"的防御(2026-09-20 "回答变成一句报错") ----
#
# 现象:assistant 气泡正文是 "empty content block is not allowed" / "empty message"
# 这类校验文案,还被写进会话历史。
#
# 取证一(机制,已确证):
#   data/traces/2026-09-20.jsonl 有 5 个 run 的 run_end answer_len 正好等于这两句话
#   的长度(13 / 34);checkpoints.db 里对应 run 的 metadata.resp 就是这两句话,
#   且 finish_reason=stop、tool_calls=0、reasoning 为空 —— 也就是**上游 200 正常流**,
#   只是正文内容是它自己的校验错误。这两句话在仓库里搜不到,不是本机生成的。
#
# 取证二(触发条件,2026-09-20 二轮修正):
#   会话 desktop-mu2rgdkv 的 messages 224~245 揭示了真正的形态 —— **错误文案是跟
#   有效 tool_calls 一起回来的**:每条 "empty content block is not allowed" 后面都紧跟
#   一条真实执行成功的 tool 结果(read_file / code_search / run_terminal / git_status…),
#   13:29:51 那一秒内连着 11 轮全中。
#   => 请求是被网关**接受**的(否则拿不到 tool_calls),所以不是"我们发了空内容块被 400"。
#   => 机制:模型这一轮只产出 tool_use、没有正文(纯工具调用轮次里很常见,尤其长会话),
#      provider 的适配层没有省略这个空 text 块,而是**把校验诊断当成正文填了回来**。
#      这也解释了为什么"重放失败请求即恢复":重放的是要正文的收尾请求,那一轮本来就有正文。
#   一轮修正时的"偶发抖动"结论据此推翻 —— 只要模型连续处于纯工具调用状态,就会连续出现。
#
# 结论:本机的缺陷不是"发出去了空内容块",而是**把网关的诊断文案当答案忠实渲染并落库**。
# 处置按"这一轮有没有拿到工具调用"分两路(关键,否则会误杀工具轮):
#   - 有 tool_calls:文案是噪音 → 丢弃,保留工具结果继续跑,用户什么都不该看到;
#   - 无 tool_calls:这一轮没有任何有效产出 → 抑制文案 + 抛可重试 LLMError,
#     交给 agent 既有的瞬态重试机制重发,用户看到的是重试提示而不是垃圾气泡。
# 另外补一条读侧清洗:历史里已落库的噪音气泡在 get_history 里被过滤,不再回放给界面/模型。
#
# 两道防线:
#   ① 回包守卫(主修,与触发条件无关):_StreamHeadGuard + _looks_like_gateway_error;
#   ② 出站规范化(加固,顺带消除不合规写法):_normalize_outgoing_messages
#      —— assistant + tool_calls 的空正文按 OpenAI 规范省略 content 键。
_GATEWAY_ERROR_ECHO_TEXTS = frozenset({
    "empty content block is not allowed",
    "content block is not allowed",
    "empty text block is not allowed",
    "text content block is not allowed",
    "empty message",
    "message content is empty",
    "content must not be empty",
    "message must not be empty",
})

# assistant 有 tool_calls 时 content 可为空(OpenAI 规范允许直接省略该键),
# 其余角色的空正文会退化成这些占位文本,保证内容块非空。
_EMPTY_CONTENT_PLACEHOLDER = {
    "tool": "(工具无输出)",
    "function": "(工具无输出)",
    "user": "(空消息)",
}


def _normalize_gateway_text(text: str) -> str:
    """归一化后比对网关报错原文(大小写/空白/尾标点不敏感)。"""
    return " ".join((text or "").lower().split()).strip(" .。!！:：")

def _looks_like_gateway_error(text: str) -> bool:
    """整段回包是否**就是**一句网关校验错误(而非正常回答里提到了它)。

    用**全等**比对而不是子串包含:正常回答里引用这句话(比如本次修复说明)时必须
    照常展示,不能误判成错误回包而被吞掉。
    """
    s = (text or "").strip()
    if not s or len(s) > 200:
        return False
    return _normalize_gateway_text(s) in _GATEWAY_ERROR_ECHO_TEXTS


def _strip_gateway_error_lines(text: str) -> str:
    """剔除文本中**独占一行**的网关诊断文案(行内引用不受影响)。

    用于"诊断文案 + 正常正文"混在一条回包里的情况:整段全等判定管不到它,
    若原样放行,用户就会看到那句报错夹在正文开头。这里只在**整行归一化后与
    诊断文案全等**时才删该行,所以 `这就是 empty content block is not allowed 的含义`
    这种行内引用不会被误删(全等匹配,非子串)。
    """
    if not text:
        return text
    lines = text.split("\n")
    kept = [ln for ln in lines if _normalize_gateway_text(ln) not in _GATEWAY_ERROR_ECHO_TEXTS]
    if len(kept) == len(lines):
        return text
    out = "\n".join(kept)
    return out.lstrip("\n")


def _normalize_outgoing_messages(messages: list[Message]) -> list[Message]:
    """出站消息规范化:消除空内容块(加固项,不是上述 bug 的触发条件)。

    - content 是多模态块列表:剔除空文本块;全空则退化为纯文本占位。
    - assistant + tool_calls 且正文为空:省略 content 键(OpenAI 规范写法,
      比传 "" 更稳;实测网关对 "" 也接受,但省略是更严格的等价形式)。
    - assistant 无 tool_calls 且正文为空 / system 空正文:整条丢弃
      (无信息量,留着反而制造空块)。
    - tool / user 空正文:填占位文本(tool 的 content 是必填字符串)。
    """
    out: list[Message] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        mm = dict(m)
        content = mm.get("content")
        if isinstance(content, list):
            blocks = [
                b for b in content
                if not (isinstance(b, dict) and b.get("type") == "text"
                        and not str(b.get("text") or "").strip())
            ]
            if blocks:
                mm["content"] = blocks
                out.append(mm)
                continue
            content = ""
            mm["content"] = ""
        if content is not None and not (isinstance(content, str) and not content.strip()):
            out.append(mm)  # 非空,原样保留
            continue
        role = str(mm.get("role") or "")
        if role == "assistant" and mm.get("tool_calls"):
            mm.pop("content", None)
        elif role in _EMPTY_CONTENT_PLACEHOLDER:
            mm["content"] = _EMPTY_CONTENT_PLACEHOLDER[role]
        elif role == "assistant":
            continue  # 空正文且无工具调用:无信息量,丢弃
        elif role in ("system", "developer"):
            continue
        else:
            mm["content"] = "(空)"
        out.append(mm)
    return out


class _StreamHeadGuard:
    """流式回包前 64 字符缓冲:识别"整段就是网关校验错误"的短回包。

    这类错误回包只有十几到几十字符,攒够 64 字符仍未命中即放行,
    因此正常回答只多承受极短的头 64 字符延迟;命中则整段抑制并抛
    可重试 LLMError,错误文案不会进入用户可见的答案区,也不会落库。

    为何要缓冲:这段错误文本本身就是"正文",若边收边 yield,前端已经把它
    渲染出来了,事后再报错也擦不掉(用户仍会看到那句报错)。缓冲到能判定为止
    是唯一能不污染界面的做法。
    """

    _HEAD_LEN = 64

    def __init__(self) -> None:
        self._buf = ""
        self._released = False

    def feed(self, piece: str) -> str:
        """吃进一段增量,返回本次可放行的文本(可能为空)。

        放行时顺带做行级清洗:若前 64 字符里裹着那句诊断文案
        ("empty message\\n\\n现在开始分析…" 这种),把该行去掉再放行。
        """
        if not piece:
            return ""
        if self._released:
            return _strip_gateway_error_lines(piece)
        self._buf += piece
        if len(self._buf) <= self._HEAD_LEN:
            return ""
        self._released = True
        out, self._buf = self._buf, ""
        return _strip_gateway_error_lines(out)

    def is_gateway_error(self) -> bool:
        """流正常结束且从未放行时,判定缓冲内容是否为网关校验错误。"""
        return (not self._released) and _looks_like_gateway_error(self._buf)

    def sample(self) -> str:
        return self._buf

    def discard(self) -> None:
        """丢弃缓冲并以"已放行"状态关闭(有 tool_calls 时用:文案是噪音,不 yield 也不报错)。"""
        self._buf = ""
        self._released = True

    def flush(self) -> str:
        """流正常结束且不是网关错误:放行剩余缓冲(同样做行级清洗)。"""
        out, self._buf = self._buf, ""
        self._released = True
        return _strip_gateway_error_lines(out)


class _ThinkingSplitter:
    """把流式正文里的 <thinking>...</thinking> 段切出来,与正文分离。

    为什么需要:非推理模型(如 agnes-3.0-flash)没有 reasoning_content 字段,
    思维链只能靠 CoT 提示让它写在 <thinking> 标签里,而这段内容混在 delta.content
    中一起流式吐回。若不切分,思考内容会直接渲染进答案区。

    chunk 边界可能把标签切断("<thi" + "nk>" + ...),因此必须跨 chunk 缓冲:
    未闭合的标签开头/结尾留在缓冲区里,等下一个 chunk 到齐再判定。
    """

    _OPEN_RE = re.compile(r"<thinking[^>]*>", re.IGNORECASE)
    _CLOSE = "</thinking>"
    _OPEN_HEAD = "<thinking"
    # 安全上限:闭合标签丢失(模型跑飞/被截断)时不能把后续正文全吞进思考区,
    # 否则用户看到的就是"思考一大坨 + 答案为空"。与前端 processTokenChunk 的
    # 8000 字符兜底保持一致。
    _MAX_OPEN = 8000

    def __init__(self) -> None:
        self._buf = ""
        self._in_thinking = False

    def feed(self, piece: str) -> tuple[str, str]:
        """吃进一段增量,返回 (thinking_text, answer_text)。"""
        if not piece:
            return "", ""
        self._buf += piece
        think_parts: list[str] = []
        ans_parts: list[str] = []
        while self._buf:
            if self._in_thinking:
                idx = self._buf.lower().find(self._CLOSE)
                if idx >= 0:
                    think_parts.append(self._buf[:idx])
                    self._buf = self._buf[idx + len(self._CLOSE):]
                    self._in_thinking = False
                    continue
                # 超过上限仍未闭合:强制退出 thinking 模式,后续内容回归正文
                if len(self._buf) > self._MAX_OPEN:
                    logger.warning(
                        "thinking 块超过 %d 字符仍未闭合,强制退出 thinking 模式", self._MAX_OPEN
                    )
                    think_parts.append(self._buf)
                    self._buf = ""
                    self._in_thinking = False
                    continue
                # 可能是被切断的结束标签,尾部留在缓冲里等下一个 chunk
                hold = self._tail_partial_close_len()
                if hold and len(self._buf) > hold:
                    think_parts.append(self._buf[:-hold])
                    self._buf = self._buf[-hold:]
                elif not hold:
                    think_parts.append(self._buf)
                    self._buf = ""
                break
            m = self._OPEN_RE.search(self._buf)
            if m:
                ans_parts.append(self._buf[:m.start()])
                self._buf = self._buf[m.end():]
                self._in_thinking = True
                continue
            # 没有完整开始标签:可能是被切断的 "<thi",留在缓冲里等下一个 chunk
            hold = self._tail_partial_open_len()
            if hold and len(self._buf) > hold:
                ans_parts.append(self._buf[:-hold])
                self._buf = self._buf[-hold:]
            elif not hold:
                ans_parts.append(self._buf)
                self._buf = ""
            break
        return "".join(think_parts), "".join(ans_parts)

    def flush(self) -> tuple[str, str]:
        """流结束:把缓冲区里剩下的内容按当前状态吐出(标签未闭合也不丢内容)。"""
        if not self._buf:
            return "", ""
        buf, self._buf = self._buf, ""
        if self._in_thinking:
            self._in_thinking = False
            return buf, ""
        return "", buf

    def _tail_partial_open_len(self) -> int:
        """缓冲区尾部疑似未完整的开始标签长度(如 "<thi"、"<thinking x")。"""
        pos = self._buf.lower().rfind("<")
        if pos < 0:
            return 0
        tail = self._buf[pos:]
        low = tail.lower()
        # 尾部是 "<thinking" 的前缀,且还没到 ">" —— 可能是标签被切断
        if self._OPEN_HEAD.startswith(low):
            return len(tail)
        if low.startswith(self._OPEN_HEAD) and ">" not in tail:
            return len(tail)
        return 0

    def _tail_partial_close_len(self) -> int:
        """缓冲区尾部疑似未完整的结束标签长度(如 "</thi")。"""
        low = self._buf.lower()
        for k in range(len(self._CLOSE) - 1, 0, -1):
            if low.endswith(self._CLOSE[:k]):
                return k
        return 0


def _gateway_error_exc(sample: str) -> LLMError:
    """把"网关校验错误被当正文回包"包装成可重试错误。

    消息里带 "网关回包异常" 标记 —— _is_retryable 靠它放行到 agent 的瞬态重试分支。
    实际观测到的是 provider 偶发抖动,重发同一请求即恢复,因此重试是有效处置。
    """
    return LLMError(
        "上游网关回包异常(把校验错误当正文返回,原文: "
        f"{(sample or '').strip()[:120]})。已自动重试。"
    )

# 推理模型:thinking 内容在 reasoning_content 字段,需分离透出
_REASONING_MODELS = ("deepseek-reasoner", "deepseek-r1", "o1", "o1-", "o3", "o3-", "reasoner")

_THINKING_PAIR_RE = re.compile(r"<thinking[^>]*>([\s\S]*?)</thinking>", re.IGNORECASE)
_THINKING_OPEN_RE = re.compile(r"<thinking[^>]*>([\s\S]*)$", re.IGNORECASE)


def split_thinking_text(content: str) -> tuple[str, str]:
    """把非流式回复里的 <thinking> 块分离出来。Returns: (thinking, body)。

    流式路径有 _ThinkingSplitter 做跨 chunk 拆分,但非流式(chat / chat_json /
    chat_json_schema)此前**完全不剥离** —— 而这些入口同样会被
    _maybe_wrap_with_thinking_prompt 注入 CoT,于是思考明文整段混进最终答案。
    实证:_node_verify 的内联重写走 chat(),落库 answer 开头就是 `<thinking>\\n1. **分析...`。
    """
    if not content or "<thinking" not in content.lower():
        return "", content
    blocks = _THINKING_PAIR_RE.findall(content)
    body = _THINKING_PAIR_RE.sub("", content)
    # 未闭合的开标签(截断/模型漏写 </thinking>):其后整体视为思考
    m = _THINKING_OPEN_RE.search(body)
    if m:
        blocks.append(m.group(1))
        body = body[: m.start()]
    thinking = "\n\n".join(b.strip() for b in blocks if b and b.strip())
    return thinking, body.strip()

PROVIDER_ALIASES = {
    "deepseek": "deepseek",
    "ds": "deepseek",
    "qwen": "qwen",
    "dashscope": "qwen",
    "tongyi": "qwen",
    "ollama": "ollama",
    "local": "ollama",
    "openai": "openai",
    "compat": "compat",
    "compatible": "compat",
}


class LLMError(Exception):
    pass


@dataclass
class ToolCall:
    """一次原生工具调用。"""

    id: str
    name: str
    arguments: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
        }

    def to_openai(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class ChatResult:
    """一次对话的完整结果:文本内容 + 原生工具调用列表。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: Optional[dict] = None
    reasoning: str = ""  # reasoning_content 思维链(仅推理模型返回)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def tool_calls_plain(self) -> list[dict]:
        return [tc.to_dict() for tc in self.tool_calls]


def _max_out_tokens() -> int:
    """请求级输出 token 上限:桌面端覆盖 > .env/环境变量 > 代码默认值。

    走 config_runtime 而不是直接读 settings,是为了让"输出被截断"这件事有救:
    在 data/config_overrides.json 里加一行
        {"agent_default_max_output_tokens": "32768"}
    就能抬高上限(改完需重启后端)。之前只读 settings,覆盖文件对这一项无效。
    """
    from app.core.config_runtime import resolve_value

    raw = resolve_value(
        "agent_default_max_output_tokens",
        getattr(settings, "agent_default_max_output_tokens", 0) or 0,
    )
    try:
        return int(str(raw).strip() or 0)
    except (TypeError, ValueError):
        return 0


def _http_timeout() -> "httpx.Timeout":
    """按配置派生 httpx 超时,避免底层读超时早于应用层空闲守卫。

    实测(2026-09-20):旧实现硬编码 `read=60.0`,而 `agent_llm_stream_idle_timeout=90`,
    于是 httpx 先抛原始 ReadTimeout(错误信息无类型名、无上下文)把仍在正常输出的流
    杀掉——上层只能整轮重生成或直接 giveup,用户看到"输出中断"。
    现在 read = max(空闲超时 + 30, 120)(可用 agent_llm_http_read_timeout 覆盖),
    connect 收紧到 15s 让连不上时快速失败进入重试。
    """
    override = int(getattr(settings, "agent_llm_http_read_timeout", 0) or 0)
    idle = int(getattr(settings, "agent_llm_stream_idle_timeout", 0) or 0)
    read = float(override) if override > 0 else float(max(idle + 30, 120))
    return httpx.Timeout(connect=15.0, read=read, write=60.0, pool=30.0)


class LLMClient:
    """统一 LLM 客户端,按 provider 从配置中组装 OpenAI 兼容客户端。"""

    # ---- Provider 熔断状态(进程级,跨实例共享) ----
    # 旧实现把熔断状态挂在实例属性上,但 chat.py 每请求 new 一个 LLMClient,
    # 熔断器跨请求永远清零——provider 真挂了也不会对下一个请求开路,
    # 重试+退避+故障转移每次都从头来。现提升到类属性,全进程共享:
    # 连续 N 个请求失败才触发熔断,冷却窗口内后续请求直接跳过该 provider。
    _circuit_fails: dict = {}
    _circuit_open_until: dict = {}
    _circuit_lock: "type[asyncio.Lock]" = None  # 惰性初始化(避免 import 时绑定事件循环)

    def __init__(self, provider: Optional[str] = None, model: Optional[str] = None):
        explicit = (provider or "").strip()
        raw = (explicit or settings.agent_llm_provider).lower()
        # 兼容历史整串格式 "provider:model" 与 "provider/model"(旧版前端/助手拼接)
        if explicit and (":" in explicit or "/" in explicit):
            from app.core.model_ref import parse_model_ref

            head, tail = parse_model_ref(explicit)
            if head and tail:
                if head.startswith("custom-"):
                    raw = head
                    if not model:
                        model = tail
                elif head in PROVIDER_ALIASES:
                    raw = head
                    if not model:
                        model = tail
        self._custom: Optional[dict] = None
        if raw.startswith("custom-"):
            from app.core.provider_registry import get_custom

            self._custom = get_custom(raw)
        if self._custom:
            self.provider = raw
        elif explicit and raw not in PROVIDER_ALIASES:
            # 显式指定的 provider 无法解析:明确报错,绝不静默换成别的模型
            raise LLMError(
                f"所选模型提供商不可用: {explicit}(不存在或已被删除)。"
                "请重新在左下角选择模型,或在设置-模型服务中检查该提供商。"
            )
        else:
            self.provider = PROVIDER_ALIASES.get(raw, "compat")
        # 记录原始 provider(用于故障转移后恢复主 provider)
        self._initial_provider = self.provider
        self._model_override = model
        self.client, self.model = self._build()
        self._api_key = self._get_key()
        self.last_usage: Optional[dict] = None  # 最近一次调用返回的 token 用量
        self._reasoning: str = ""  # 最近一次推理模型的思维链(reasoning_content)
        self._finish_reason: str = ""  # 最近一次调用的 finish_reason(length=输出截断)
        # 熔断状态已提升到类属性(LLMClient._circuit_fails / _circuit_open_until),
        # 进程级共享,跨请求生效。这里不再初始化实例级副本。

    # ---- 可观测性 ----

    def _trace(self, kind: str, t0: float, usage: Optional[dict], ok: bool = True,
               error: str = "", tools_n: int = 0, content: str = "") -> None:
        """记录一次 LLM 调用 trace(含 token 与成本估算)。"""
        usage = usage or {}
        cache_hit = int(usage.get("cache_hit_tokens", 0) or 0)
        cache_miss = int(usage.get("cache_miss_tokens", 0) or 0)
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        hit_rate = (cache_hit / prompt_tokens) if prompt_tokens else 0.0
        tracer.record({
            "type": "llm_call",
            "kind": kind,
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": usage.get("completion_tokens", 0),
            "tokens": usage.get("total_tokens", 0) or (prompt_tokens + usage.get("completion_tokens", 0)),
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
            "cache_hit_rate": round(hit_rate, 4),
            "cost": estimate_cost(self.model, prompt_tokens, usage.get("completion_tokens", 0)),
            "duration": round(time.time() - t0, 3),
            "ok": ok,
            "error": (error or "")[:300],
            "tools_n": tools_n,
            "reply_preview": (content or "")[:200],
        })

    def _get_key(self) -> str:
        if self._custom:
            return self._custom.get("api_key", "")
        p = self.provider
        if p == "deepseek":
            return settings.deepseek_api_key
        if p == "qwen":
            return settings.qwen_api_key
        if p == "ollama":
            return "ollama"
        if p == "openai":
            import os

            return os.environ.get("OPENAI_API_KEY", "")
        return settings.openai_compat_api_key

    def _build(self) -> tuple[AsyncOpenAI, str]:
        proxy = get_httpx_proxy()
        client_kwargs = {}
        if proxy:
            client_kwargs["http_client"] = httpx.AsyncClient(proxy=proxy, timeout=_http_timeout())
        
        if self._custom:
            return AsyncOpenAI(
                api_key=self._custom.get("api_key") or "sk-placeholder",
                base_url=self._custom["base_url"],
                timeout=_http_timeout(),
                **client_kwargs,
            ), self._model_override or self._custom.get("model") or ""
        p = self.provider
        if p == "deepseek":
            return AsyncOpenAI(
                api_key=settings.deepseek_api_key or "sk-placeholder",
                base_url=settings.deepseek_base_url,
                timeout=_http_timeout(),
                **client_kwargs,
            ), self._model_override or settings.deepseek_model
        if p == "qwen":
            return AsyncOpenAI(
                api_key=settings.qwen_api_key or "sk-placeholder",
                base_url=settings.qwen_base_url,
                timeout=_http_timeout(),
                **client_kwargs,
            ), self._model_override or settings.qwen_model
        if p == "ollama":
            return AsyncOpenAI(
                api_key="ollama",
                base_url=settings.ollama_base_url,
                timeout=_http_timeout(),
                **client_kwargs,
            ), self._model_override or settings.ollama_model
        if p == "openai":
            return AsyncOpenAI(timeout=_http_timeout(), **client_kwargs), self._model_override or "gpt-4o-mini"
        # compat: 任意 OpenAI 兼容
        return AsyncOpenAI(
            api_key=settings.openai_compat_api_key or "sk-placeholder",
            base_url=settings.openai_compat_base_url,
            timeout=_http_timeout(),
            default_headers={"User-Agent": "TianGong-Agent/1.0"},
            **client_kwargs,
        ), self._model_override or settings.openai_compat_model

    def _require_key(self) -> None:
        if self._custom and not self._api_key:
            raise LLMError(f"自定义提供商「{self._custom.get('name', self.provider)}」未配置 API Key,请在设置-模型服务中填写")
        if self.provider in ("deepseek", "qwen", "openai", "compat") and not self._api_key:
            raise LLMError(f"Provider {self.provider} 未配置 API Key,请在 .env 中设置")

    # ---- 可靠性:瞬态错误重试 + 故障转移 ----

    @classmethod
    def _is_retryable(cls, error: str) -> bool:
        return _is_retryable(error)

    def _failover_specs(self) -> list[tuple[str, str]]:
        """返回 [(provider, model), ...] 故障转移候选,主 provider 在前。"""
        specs: list[tuple[str, str]] = [(self.provider, self.model)]
        fb = settings.agent_llm_failover_provider
        if fb:
            fb_p = PROVIDER_ALIASES.get(fb.lower(), "compat")
            fb_m = settings.agent_llm_failover_model
            specs.append((fb_p, fb_m))
        return specs

    def _make_client(self, provider: str, model: str) -> tuple[AsyncOpenAI, str]:
        """按 provider 构造 OpenAI 兼容客户端(供故障转移时重建)。"""
        proxy = get_httpx_proxy()
        client_kwargs = {}
        if proxy:
            client_kwargs["http_client"] = httpx.AsyncClient(proxy=proxy, timeout=_http_timeout())
        
        if provider.startswith("custom-"):
            from app.core.provider_registry import get_custom

            entry = get_custom(provider)
            if entry:
                return AsyncOpenAI(
                    api_key=entry.get("api_key") or "sk-placeholder",
                    base_url=entry["base_url"],
                    **client_kwargs,
                ), model or entry.get("model") or ""
        p = provider
        if p == "deepseek":
            return AsyncOpenAI(
                api_key=settings.deepseek_api_key or "sk-placeholder",
                base_url=settings.deepseek_base_url,
                **client_kwargs,
            ), model or settings.deepseek_model
        if p == "qwen":
            return AsyncOpenAI(
                api_key=settings.qwen_api_key or "sk-placeholder",
                base_url=settings.qwen_base_url,
                **client_kwargs,
            ), model or settings.qwen_model
        if p == "ollama":
            return AsyncOpenAI(
                api_key="ollama",
                base_url=settings.ollama_base_url,
                **client_kwargs,
            ), model or settings.ollama_model
        if p == "openai":
            return AsyncOpenAI(**client_kwargs), model or "gpt-4o-mini"
        return AsyncOpenAI(
            api_key=settings.openai_compat_api_key or "sk-placeholder",
            base_url=settings.openai_compat_base_url,
            default_headers={"User-Agent": "TianGong-Agent/1.0"},
            **client_kwargs,
        ), model or settings.openai_compat_model

    async def _invoke(self, fn, kind: str, tools_n: int = 0, **trace_extra):
        """带重试+故障转移+熔断的 LLM 调用。

        fn(client, model) -> OpenAI 响应对象;成功返回响应,耗尽后抛 LLMError。
        语义:每个候选 provider 内对瞬态错误指数退避重试(限流类错误退避更长);
        该 provider 仍失败则切换到下一个候选(故障转移);连续失败触发熔断,
        冷却窗口内跳过该 provider——但若所有候选都被熔断跳过,等待主 provider
        冷却(封顶 30s)后再尝试一次,避免熔断窗口内请求"瞬时失败"。
        """
        specs = self._failover_specs()
        retries = max(0, int(settings.agent_llm_retries))
        timeout = float(settings.agent_llm_timeout or 60)  # 单次 LLM 调用超时
        last_err: Optional[Exception] = None

        async def _attempt(idx: int, provider: str, model: str):
            """对单个 provider 做一轮带退避的尝试;成功返回响应,耗尽返回 None。"""
            nonlocal last_err
            if idx == 0:
                # 主 provider:用 __init__ 预构建的客户端(子类可覆盖 self.client 便于测试)
                client, resolved_model = self.client, self.model
            else:
                client, resolved_model = self._make_client(provider, model)
            for attempt in range(retries + 1):
                t0 = time.time()
                try:
                    # 添加超时保护,防止 API 挂起导致永久阻塞
                    resp = await asyncio.wait_for(
                        fn(client, resolved_model),
                        timeout=timeout
                    )
                    self._record_success(provider)
                    self.last_usage = self._usage_of(resp)
                    # 恢复主 provider(熔断冷却期后),保证下次优先用主 provider
                    if idx > 0:
                        self._circuit_fails[self._initial_provider or provider] = 0
                        self._circuit_open_until.pop(self._initial_provider or provider, None)
                    self._trace(kind, t0, self.last_usage, tools_n=tools_n,
                                **trace_extra)
                    return resp
                except asyncio.TimeoutError:
                    self._trace(kind, t0, None, ok=False, error=f"timeout ({timeout}s)", tools_n=tools_n)
                    last_err = LLMError(f"LLM 调用超时 ({timeout}s)")
                    if attempt < retries:
                        await asyncio.sleep(settings.agent_llm_retry_delay * (2 ** attempt))
                        continue
                    break
                except Exception as e:  # noqa: BLE001
                    self._trace(kind, t0, None, ok=False, error=str(e), tools_n=tools_n)
                    last_err = e
                    # 终态错误(401 鉴权/404 路径错/配额耗尽)立即失败,不做指数退避,
                    # 避免 CLI/UI 在配额打满时长时间"重试中"挂起。
                    # 瞬态错误(网络抖动/5xx/限流)退避重试;限流类退避更长。
                    if not _is_retryable(str(e)):
                        break
                    if attempt < retries:
                        base = settings.agent_llm_retry_delay
                        if _is_rate_limit(str(e)):
                            base = max(base, 3.0)  # 限流:3s/6s 起步,给网关降温窗口
                        await asyncio.sleep(base * (2 ** attempt))
                        continue
                    break
            self._record_failure(provider)
            return None

        attempted = False
        for idx, (provider, model) in enumerate(specs):
            if self._is_circuit_open(provider):
                # 熔断冷却中:跳过该 provider(附带剩余秒数,便于用户判断)
                remain = max(0, int(self._circuit_open_until.get(provider, 0.0) - time.time()))
                last_err = LLMError(f"provider {provider} 熔断冷却中(剩余约{remain}s),跳过")
                continue
            attempted = True
            resp = await _attempt(idx, provider, model)
            if resp is not None:
                return resp
        # 所有候选都被熔断跳过(一个都没实际尝试):等待主 provider 冷却剩余时间
        # (封顶 30s)后重试一次,而不是让这一轮请求直接失败。
        if not attempted and specs:
            provider, model = specs[0]
            remain = max(0.0, self._circuit_open_until.get(provider, 0.0) - time.time())
            wait = min(remain + 0.5, 30.0)
            if wait >= 0.5:
                logger.info("LLM 所有 provider 熔断冷却中,等待 %.1fs 后重试 %s", wait, provider)
                await asyncio.sleep(wait)
            if not self._is_circuit_open(provider):
                resp = await _attempt(0, provider, model)
                if resp is not None:
                    return resp
        friendly = _friendly_llm_error(last_err, self.provider, self.model)
        raise LLMError(friendly) from last_err

    # ---- Provider 熔断状态(进程级,类属性共享) ----
    def _is_circuit_open(self, provider: str) -> bool:
        if settings.agent_llm_circuit_threshold <= 0:
            return False
        until = self._circuit_open_until.get(provider, 0.0)
        if until > time.time():
            return True
        if until > 0:
            # 冷却结束:清零计数,允许再次尝试
            self._circuit_fails[provider] = 0
            self._circuit_open_until[provider] = 0.0
        return False

    def _record_success(self, provider: str) -> None:
        self._circuit_fails[provider] = 0
        self._circuit_open_until[provider] = 0.0

    def _record_failure(self, provider: str) -> None:
        if settings.agent_llm_circuit_threshold <= 0:
            return
        fails = self._circuit_fails.get(provider, 0) + 1
        self._circuit_fails[provider] = fails
        if fails >= settings.agent_llm_circuit_threshold:
            self._circuit_open_until[provider] = time.time() + settings.agent_llm_circuit_cooldown

    # ---- 客户端资源管理 ----

    async def aclose(self) -> None:
        """关闭本实例持有的 OpenAI 客户端(及其底层 httpx 连接池)。

        修复: 此前 AsyncOpenAI / httpx.AsyncClient 在故障转移、ensemble
        并发生成等多个路径被反复重建且从不关闭,连接池泄漏。调用方(如
        Agent 在 ensemble/故障转移后、应用关停时)应对旧实例 aclose()。
        幂等: 重复调用安全。
        """
        for client in (getattr(self, "client", None),):
            if client is not None:
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass
        self.client = None

    # ---- 缓存控制 / reasoning ----

    def _prepare_messages(self, messages: list[Message]) -> list[Message]:
        """出站消息统一准备:先消除空内容块,再注入缓存断点。

        **所有** LLM 请求入口(chat / chat_stream / chat_with_tools /
        chat_stream_with_tools / chat_json / chat_json_schema)都必须经过这里,
        保证空内容块在任何路径下都不会被发到上游。
        """
        return self._cache_control_messages(_normalize_outgoing_messages(messages))

    def _cache_control_messages(self, messages: list[Message]) -> list[Message]:
        """给 system 消息注入 cache_control 断点,保持稳定前缀命中缓存。"""
        if not settings.agent_prompt_cache:
            return messages
        out: list[Message] = []
        for i, m in enumerate(messages):
            mm = dict(m)
            if m.get("role") == "system":
                mm["cache_control"] = {"type": "ephemeral"}
            out.append(mm)
        return out

    def _is_reasoning_model(self) -> bool:
        return any(t in (self.model or "").lower() for t in _REASONING_MODELS)

    def _maybe_wrap_with_thinking_prompt(self, messages: list[Message]) -> list[Message]:
        """为非推理模型添加 CoT 提示,使非推理模型也能产出结构化思考。

        仅在以下条件下注入:
        - agent_reasoning_enabled=True
        - 当前模型不是推理模型(_is_reasoning_model=False)
        - 消息中还没有 thinking 类系统提示

        通过修改最后一条 user 消息的尾部追加思考指令,
        让模型在最终回答前先进行逐步推理。
        """
        if not settings.agent_reasoning_enabled:
            return messages
        if self._is_reasoning_model():
            return messages
        if not messages:
            return messages
        # 在**最后一条 user 消息**尾部追加思考指令。
        # 旧实现取 messages[-1]:ReAct 第 2 轮起末条是 role=tool(native 模式追加的
        # 工具结果),role 不是 user 就直接 return,后续轮次永远拿不到 CoT 指令。
        idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                idx = i
                break
        if idx is None:
            return messages
        # 旧实现还有一条"system 消息里出现 'think' 子串就跳过"的判重。
        # SYSTEM_PROMPT 的最终回答规范本身就写了 `<thinking>` 标签(要求分析过程
        # 走该标签),于是这个判重**恒真** —— 主流程一轮 CoT 都没注入过,
        # 思考区因此一直是空的(实证:会话 desktop-mu2rgdkv 近 15 条 assistant
        # 消息里 13 条 meta.thinking 为空)。判重只认本 suffix 即可。
        last = messages[idx]
        content = last.get("content", "")
        # 多模态内容(content 是 list 时)不注入 CoT 提示
        if isinstance(content, list):
            return messages
        if not content or not content.strip():
            return messages
        thinking_suffix = (
            "\n\n---\n\n请分步骤思考:先列出解题思路或关键信息,再给出最终答案。"
            "用 <thinking>...</thinking> 标签包裹你的逐步推理过程。"
        )
        if thinking_suffix not in content:
            last = dict(last)
            last["content"] = content.rstrip() + thinking_suffix
            messages = list(messages)
            messages[idx] = last  # 注意:必须是 idx,不是 -1(末条可能是 tool)
        return messages

    def _extract_reasoning(self, msg) -> str:
        rc = getattr(msg, "reasoning_content", None) or ""
        if rc:
            return rc
        if not self._is_reasoning_model() or not settings.agent_reasoning_enabled:
            return ""
        return getattr(msg, "reasoning", "") or ""

    @staticmethod
    def _usage_of(resp) -> Optional[dict]:
        """从 OpenAI 响应中提取 token 用量(含 prompt cache 命中量)。"""
        u = getattr(resp, "usage", None)
        if u is None:
            return None
        prompt_tokens = int(getattr(u, "prompt_tokens", 0) or 0)
        # 缓存命中:DeepSeek 用 prompt_cache_hit_tokens;OpenAI 风格用 prompt_tokens_details.cached_tokens
        cache_hit = int(getattr(u, "prompt_cache_hit_tokens", 0) or 0)
        details = getattr(u, "prompt_tokens_details", None)
        if details is not None:
            cache_hit = max(cache_hit, int(getattr(details, "cached_tokens", 0) or 0))
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": int(getattr(u, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(u, "total_tokens", 0) or 0),
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": max(0, prompt_tokens - cache_hit),
        }

    async def chat(self, messages: list[Message], temperature: float = 0.7) -> str:
        """非流式对话,返回完整回复文本。"""
        self._require_key()
        messages = self._prepare_messages(messages)
        messages = self._maybe_wrap_with_thinking_prompt(messages)
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        max_out = _max_out_tokens()
        if max_out > 0:
            kwargs["max_tokens"] = max_out

        async def _fn(client, model):
            k = dict(kwargs)
            k["model"] = model
            return await client.chat.completions.create(**k)

        resp = await self._invoke(_fn, "chat")
        msg = resp.choices[0].message
        self._reasoning = self._extract_reasoning(msg)
        # 推理模型:如果 content 为空但 reasoning_content 有内容,使用 reasoning 作为答案
        content = msg.content or ""
        if not content and self._reasoning:
            content = self._reasoning
        # 网关把"空内容块"校验错误当正文回包:不能当答案用(见 _StreamHeadGuard 注释)
        if _looks_like_gateway_error(content):
            logger.warning("上游网关把校验错误当正文回包(chat): %r", content[:120])
            raise _gateway_error_exc(content)
        # 诊断文案混在正文里(独占一行)时剔掉,避免首行露出报错
        content = _strip_gateway_error_lines(content)
        # 非流式同样注入了 CoT,必须把 <thinking> 块剥出来:
        # 剥出的内容存进 self._reasoning(推理内容通道),调用方按需透出,
        # 不要让它以明文混在最终答案里。
        tk, content = split_thinking_text(content)
        if tk:
            self._reasoning = ((self._reasoning or "") + "\n\n" + tk).strip()
        return content

    async def chat_stream(self, messages: list[Message], temperature: float = 0.7):
        """流式对话,逐个产出文本增量(str);推理模型先产出 thinking 增量。

        修复: 流式消费加空闲超时(流内两个 chunk 之间超过 stream_idle_timeout 秒视为卡死,
        立即抛 LLMError,不再无限挂起),避免 provider 中途停顿导致前端"一直卡"。
        """
        self._require_key()
        messages = self._prepare_messages(messages)
        # 与其他 5 个入口保持一致:非推理模型靠 CoT 提示产出 <thinking> 块,
        # 再由 _ThinkingSplitter 切成 thinking 事件(此前漏了这行 → 桌面端思考区永远为空)
        messages = self._maybe_wrap_with_thinking_prompt(messages)
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        max_out = _max_out_tokens()
        if max_out > 0:
            kwargs["max_tokens"] = max_out

        async def _fn(client, model):
            k = dict(kwargs)
            k["model"] = model
            return await client.chat.completions.create(**k)

        resp = await self._invoke(_fn, "chat_stream")
        reasoning = ""
        head = _StreamHeadGuard()
        splitter = _ThinkingSplitter()
        idle_timeout = float(getattr(settings, "agent_llm_stream_idle_timeout", 0) or 0)
        total_deadline = (
            time.time() + int(settings.agent_llm_stream_total_timeout)
            if settings.agent_llm_stream_total_timeout > 0 else 0.0
        )
        try:
            async for chunk in self._stream_with_idle_timeout(resp, idle_timeout):
                # 总时长硬超时: 防止 provider 以极慢速度持续吐字导致流永不结束
                if total_deadline and time.time() > total_deadline:
                    raise LLMError(
                        f"LLM 流式调用超过总时长上限({settings.agent_llm_stream_total_timeout}s), 已中断"
                    )
                # 单 chunk 防御:兼容网关偶发畸形帧(如空 choices 变体/坏 JSON 解析出
                # IndexError),只跳过该帧,不杀整条流(实测 2026-09-20 "list index out of range")
                try:
                    if chunk.usage:
                        self.last_usage = self._usage_of(chunk)
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    rc = getattr(delta, "reasoning_content", None)
                    if rc:
                        reasoning += rc
                        yield {"type": "thinking", "content": rc}
                        continue
                    if delta and delta.content:
                        # 非推理模型(或 CoT 注入生效时)把思维链写在 <thinking> 里,
                        # 先切出来单独当 thinking 事件,剩下的才是正文
                        tk, ans = splitter.feed(delta.content)
                        if tk:
                            reasoning += tk
                            yield {"type": "thinking", "content": tk}
                        if not ans:
                            continue
                        # 前 64 字符先过守卫:整段回包就是网关校验错误时不放行
                        # (见 _StreamHeadGuard),避免错误文案被当正文渲染/落库
                        out = head.feed(ans)
                        if out:
                            yield out
                except LLMError:
                    raise
                except Exception as chunk_err:  # noqa: BLE001
                    logger.warning("跳过畸形流 chunk(chat_stream): %s", chunk_err)
                    continue
        except LLMError:
            self._reasoning = reasoning
            raise
        except Exception as e:  # noqa: BLE001
            self._reasoning = reasoning
            self._trace("chat_stream", time.time(), None, ok=False, error=str(e))
            logger.exception("chat_stream 流式调用失败")  # 完整 traceback 便于定位yield回灌的异常
            raise LLMError(f"LLM 流式调用失败: {_exc_detail(e)}") from e
        # 流正常结束:整段回包就是网关校验错误 → 抑制并以可重试错误上报
        if head.is_gateway_error():
            sample = head.sample()
            self._reasoning = reasoning
            logger.warning("上游网关把校验错误当正文回包(chat_stream): %r", sample[:120])
            self._trace("chat_stream", time.time(), None, ok=False, error=sample[:300])
            raise _gateway_error_exc(sample)
        # 收尾:缓冲区里可能还有未闭合标签内的思考内容 / 正文残余
        tk, ans = splitter.flush()
        if tk:
            reasoning += tk
            yield {"type": "thinking", "content": tk}
        if ans:
            out = head.feed(ans)
            if out:
                yield out
        tail = head.flush()
        if tail:
            yield tail
        self._reasoning = reasoning
        self._trace("chat_stream", time.time(), self.last_usage)

    @staticmethod
    async def _stream_with_idle_timeout(stream, idle_timeout: float):
        """消费 OpenAI 流式响应;idle_timeout>0 时,相邻 chunk 间隔超过该值即视为上游卡死。

        用 asyncio.wait_for 包住每次 __anext__ —— 这是对流内停顿的唯一超时保护,
        覆盖 provider 建连后长时间不吐字 / 中途断流不回 EOF 的场景。
        """
        if idle_timeout <= 0:
            async for chunk in stream:
                yield chunk
            return
        aiter = stream.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(aiter.__anext__(), timeout=idle_timeout)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError as e:
                raise LLMError(
                    f"LLM 流式响应空闲超时 ({idle_timeout:.0f}s 无新数据),已中断。"
                    "请检查网络或换用更稳定的 provider/模型。"
                ) from e
            except Exception as e:  # noqa: BLE001
                # 取帧阶段(而不是处理阶段)的异常:SDK 解析畸形 SSE 帧等。
                # 包装成带类型名的 LLMError,让上层按瞬态错误整轮重试,而不是裸杀流;
                # 同时 traceback 经 logger.exception 落入 data/logs/backend.log。
                logger.exception("流式迭代异常(__anext__)")
                raise LLMError(f"LLM 流式响应中断: {_exc_detail(e)}") from e
            yield chunk

    @property
    def supports_native_tools(self) -> bool:
        """是否支持原生 function calling。

        ollama 的 OpenAI 兼容端点对 tools 的支持随版本而异;
        统一交给上层:先尝试原生,失败自动降级(见 agent.py)。
        """
        return settings.agent_native_tools

    async def chat_with_tools(
        self,
        messages: list[Message],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        """带工具的函数调用:返回文本内容 + 原生 tool_calls(可为多个,支持并行)。

        Args:
            messages: 消息列表(需含 role/tool_call_id 以支持多轮工具对话)
            tools: OpenAI function schemas;为空则等价于 chat()
        """
        self._require_key()
        messages = self._prepare_messages(messages)
        messages = self._maybe_wrap_with_thinking_prompt(messages)
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools

        async def _fn(client, model):
            k = dict(kwargs)
            k["model"] = model
            return await client.chat.completions.create(**k)

        resp = await self._invoke(_fn, "chat_with_tools", tools_n=len(tools or []))
        choice = resp.choices[0]
        msg = choice.message
        tool_calls: list[ToolCall] = []
        for tc in getattr(msg, "tool_calls", None) or []:
            tool_calls.append(
                ToolCall(
                    id=tc.id or "",
                    name=getattr(tc.function, "name", ""),
                    arguments=getattr(tc.function, "arguments", "") or "{}",
                )
            )
        content = msg.content or ""
        # 与流式路径同构的两路处置(见文件头"网关校验错误被当正文回包"注释):
        # 有 tool_calls → 文案是噪音,清空后照常返回工具;无 tool_calls → 可重试报错。
        if _looks_like_gateway_error(content):
            if tool_calls:
                logger.warning(
                    "上游网关把校验错误当正文回包(chat_with_tools),本轮有 %d 个 tool_calls,"
                    "丢弃噪音文案后继续: %r", len(tool_calls), content[:120],
                )
                content = ""
            else:
                logger.warning("上游网关把校验错误当正文回包(chat_with_tools): %r", content[:120])
                raise _gateway_error_exc(content)
        else:
            # 诊断文案混在正常正文里(独占一行)时也剔掉,避免首行露出报错
            content = _strip_gateway_error_lines(content)
            # 非流式同样注入了 CoT:剥出 <thinking> 块,存进 _reasoning 通道,
            # 不要让它明文混进正文(与 chat() 一致)。
            _tk, content = split_thinking_text(content)
            if _tk:
                self._reasoning = ((self._reasoning or "") + "\n\n" + _tk).strip()
        if not content and not tool_calls and self._reasoning:
            # 推理模型:content 为空但 reasoning 有内容时,用 reasoning 兜底(与 chat() 一致)
            content = _strip_gateway_error_lines(self._reasoning)
        return ChatResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason=getattr(choice, "finish_reason", "") or "",
            usage=self.last_usage,
            reasoning=self._extract_reasoning(msg),
        )

    async def chat_stream_with_tools(
        self,
        messages: list[Message],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.7,
        result_holder: Optional[dict] = None,
    ):
        """流式对话+工具调用:边流式产出 token/thinking 事件,边收集 tool_calls。

        参数:
            result_holder: 可选的 dict,流结束后将 ChatResult 写入 result_holder["result"]。
                          调用方在迭代完成后读取此对象获取完整结果。

        yield:
            str: 文本增量(delta content)
            dict: {"type": "thinking", "content": "..."} 推理内容
            dict: {"type": "tool_call_delta", "index": int, "id": str, "name": str, "arguments": str}
                  工具调用增量(可用于前端实时展示工具名)
        """
        self._require_key()
        messages = self._prepare_messages(messages)
        messages = self._maybe_wrap_with_thinking_prompt(messages)
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
        # 传递 max_output_tokens 限制, 防止 provider 默认值过低导致截断
        max_out = _max_out_tokens()
        if max_out > 0:
            kwargs["max_tokens"] = max_out

        async def _fn(client, model):
            k = dict(kwargs)
            k["model"] = model
            return await client.chat.completions.create(**k)

        resp = await self._invoke(_fn, "chat_stream", tools_n=len(tools or []))
        reasoning = ""
        head = _StreamHeadGuard()
        splitter = _ThinkingSplitter()
        content_buf = ""
        tool_call_bufs: list[dict] = []  # 每个 tool_call 的累积数据
        finish_reason = ""
        idle_timeout = float(getattr(settings, "agent_llm_stream_idle_timeout", 0) or 0)
        total_deadline = (
            time.time() + int(settings.agent_llm_stream_total_timeout)
            if settings.agent_llm_stream_total_timeout > 0 else 0.0
        )
        try:
            async for chunk in self._stream_with_idle_timeout(resp, idle_timeout):
                # 总时长硬超时: 防止 provider 以极慢速度持续吐字导致流永不结束
                if total_deadline and time.time() > total_deadline:
                    raise LLMError(
                        f"LLM 流式调用超过总时长上限({settings.agent_llm_stream_total_timeout}s), 已中断"
                    )
                # 单 chunk 防御:兼容网关偶发畸形帧/消费端异常,只跳过该帧不杀整条流
                try:
                    if chunk.usage:
                        self.last_usage = self._usage_of(chunk)
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    # 追踪 finish_reason(截断检测用)
                    fr = getattr(choice, "finish_reason", None)
                    if fr:
                        finish_reason = fr
                    rc = getattr(delta, "reasoning_content", None)
                    if rc:
                        reasoning += rc
                        yield {"type": "thinking", "content": rc}
                        continue
                    # 文本内容
                    if delta and delta.content:
                        # 与 chat_stream 同处理:CoT 注入后 <thinking> 混在正文里,
                        # 先切出来当 thinking 事件,只有正文进 content_buf/答案区
                        # (否则思考内容会被当成答案渲染,还会污染工具调用解析)
                        tk, ans = splitter.feed(delta.content)
                        if tk:
                            reasoning += tk
                            yield {"type": "thinking", "content": tk}
                        if not ans:
                            continue
                        content_buf += ans
                        # 前 64 字符先过守卫:整段回包就是网关校验错误时不放行
                        # (content_buf 仍保留全文,仅抑制对外的 yield)
                        out = head.feed(ans)
                        if out:
                            yield out
                    # 工具调用增量
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if not isinstance(idx, int) or idx < 0:
                                logger.warning("跳过非法 tool_call 增量(index=%r)", idx)
                                continue
                            while len(tool_call_bufs) <= idx:
                                tool_call_bufs.append({"id": "", "name": "", "arguments": ""})
                            if tc_delta.id:
                                tool_call_bufs[idx]["id"] = tc_delta.id
                            if tc_delta.function:
                                fn = tc_delta.function
                                if fn.name:
                                    tool_call_bufs[idx]["name"] = fn.name
                                if fn.arguments:
                                    tool_call_bufs[idx]["arguments"] += fn.arguments
                            # 实时发射工具调用增量事件
                            yield {
                                "type": "tool_call_delta",
                                "index": idx,
                                "id": tool_call_bufs[idx]["id"],
                                "name": tool_call_bufs[idx]["name"],
                                "arguments": tool_call_bufs[idx]["arguments"],
                            }
                except LLMError:
                    raise
                except Exception as chunk_err:  # noqa: BLE001
                    logger.warning("跳过畸形流 chunk(chat_stream_with_tools): %s", chunk_err)
                    continue
        except LLMError:
            # 空闲超时/流中断等 LLMError 保留原始可操作提示,不再包一层
            raise
        except Exception as e:  # noqa: BLE001
            self._trace("chat_stream", time.time(), None, ok=False, error=str(e))
            logger.exception("chat_stream_with_tools 流式调用失败")  # 完整 traceback 便于定位yield回灌的异常
            raise LLMError(f"LLM 流式调用失败: {_exc_detail(e)}") from e
        # 收尾:标签可能刚好卡在流末尾被切断,未闭合的 <thinking> 块也要吐出来(不能丢)
        tk, ans = splitter.flush()
        if tk:
            reasoning += tk
            yield {"type": "thinking", "content": tk}
        if ans:
            out = head.feed(ans)
            if out:
                yield out
        logger.debug("[chat_stream_with_tools] stream loop ended, content_len=%d", len(content_buf))
        self._reasoning = reasoning
        self._finish_reason = finish_reason
        # 流正常结束:整段回包就是网关校验错误 → 抑制并以可重试错误上报
        # (必须在截断提示拼接之前判断,否则错误文案会被追加警告后缀)
        if head.is_gateway_error():
            sample = head.sample()
            if tool_call_bufs:
                # 关键分支(2026-09-20 二轮修正):不能用抛错把工具轮一起杀掉。
                # 这一轮的正文是 provider 拿诊断文案填空 text 块塞回来的噪音,
                # 丢掉它照样能正常跑工具;抛错反而会丢弃整轮 tool_calls 去重试,
                # 而模型连续纯工具调用时会把重试次数耗尽 → 用户看到"任务中止"。
                logger.warning(
                    "上游网关把校验错误当正文回包(chat_stream_with_tools),"
                    "本轮有 %d 个 tool_calls,丢弃噪音文案后继续: %r",
                    len(tool_call_bufs), sample[:120],
                )
                self._trace("chat_stream", time.time(), None, ok=False,
                            error="[已丢弃的同轮噪音] " + sample[:280])
                head.discard()
                content_buf = ""
            else:
                logger.warning("上游网关把校验错误当正文回包(chat_stream_with_tools): %r", sample[:120])
                self._trace("chat_stream", time.time(), None, ok=False, error=sample[:300])
                raise _gateway_error_exc(sample)
        tail = head.flush()
        if tail:
            yield tail

        # 检测截断: finish_reason=length 表示达到 max_tokens 上限
        if finish_reason == "length" and not tool_call_bufs:
            logger.warning("LLM 响应被截断(finish_reason=length, content_len=%d)", len(content_buf))
            # 追加截断提示,让前端显示
            content_buf += "\n\n⚠️ 回复被截断: 已达到输出长度上限。可尝试缩短上下文或增加 max_output_tokens。"
        elif finish_reason == "length" and tool_call_bufs:
            # 工具调用轮被截断:参数 JSON 常常被网关"补成合法 JSON"后放行,
            # 于是残缺代码照样进沙箱 → SyntaxError: unterminated string literal。
            # 这里留取证日志;真正的拦截由 run_code 等工具的完整性预检兜底。
            logger.warning(
                "LLM 工具调用在输出上限处被截断(finish_reason=length, calls=%d, args_len=%s)",
                len(tool_call_bufs), [len(b.get("arguments", "")) for b in tool_call_bufs],
            )

        # 构建最终结果
        tool_calls = []
        for buf in tool_call_bufs:
            args = buf.get("arguments", "")
            name = buf.get("name", "")
            # 工具调用参数完整性校验:流中途被截断时 arguments 可能是残缺 JSON,
            # 直接塞给执行器会让下一轮 Agent 拿着坏参数崩。残缺时抛可操作 LLMError,
            # 让上层(流式降级 / error 事件)感知,而不是喂给工具。
            # 空参数视为合法({}),仅非空且解析失败时拦截。
            if args:
                try:
                    json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    raise LLMError(
                        f"工具调用参数被截断(名称={name or '未知'}),流式响应中途断开。"
                        "请重试或检查 provider 网络稳定性。"
                    )
            tool_calls.append(ToolCall(
                id=buf.get("id", ""),
                name=name,
                arguments=args or "{}",
            ))
        result = ChatResult(
            content=content_buf,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning=reasoning,
            usage=self.last_usage,
        )
        if result_holder is not None:
            result_holder["result"] = result

    async def chat_json(
        self,
        messages: list[Message],
        temperature: float = 0.2,
    ) -> dict:
        """结构化 JSON 输出:response_format=json_object,返回解析后的 dict。

        某些 provider 不支持 response_format,会回退为普通 chat 再解析 JSON。
        """
        self._require_key()
        messages = self._prepare_messages(messages)
        # 结构化输出入口**不注入 CoT**:要求 <thinking> 标签与要求返回纯 JSON
        # 互相打架,会让 _extract_json 拿到带思考前缀的文本。
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }

        async def _fn(client, model):
            k = dict(kwargs)
            k["model"] = model
            return await client.chat.completions.create(**k)

        try:
            resp = await self._invoke(_fn, "chat_json")
            content = resp.choices[0].message.content or ""
        except Exception:  # noqa: BLE001
            content = await self.chat(messages, temperature=temperature)

        data = _extract_json(content)
        if isinstance(data, dict):
            return data
        # JSON 解析失败:重试一次,带纠错提示让模型修正
        try:
            fix_messages = list(messages)
            fix_messages.append({
                "role": "user",
                "content": f"上次输出不是合法 JSON，请重新输出完整合法的 JSON。上次输出: {content[:300]}",
            })
            fix_resp = await self.chat(fix_messages, temperature=temperature)
            data = _extract_json(fix_resp)
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            pass
        raise LLMError(f"结构化输出解析失败: 模型未返回合法 JSON ({content[:200]})")

    async def chat_json_schema(
        self,
        messages: list[Message],
        schema: dict,
        temperature: float = 0.2,
        max_retries: int = 2,
        validator=None,
        strict_mode: Optional[bool] = None,
    ) -> dict:
        """严格结构化输出:response_format 绑 JSON Schema + 校验重试。

        Args:
            schema: JSON Schema(dict),传入 response_format 与提示词
            max_retries: 校验失败时的重试次数(把校验错误回灌给模型)
            validator: 可选 callable(data) -> (ok, error_msg);不传则仅做 JSON 解析
            strict_mode: 是否启用严格校验(检查 required/类型);None=跟随 settings
        返回符合 schema 的 dict;耗尽仍失败抛 LLMError。
        增强:校验失败时返回字段级详细错误,便于 LLM 精准修正。
        """
        from app.config import settings as _cfg
        self._require_key()
        messages = self._prepare_messages(messages)
        # 同 chat_json:结构化输出不注入 CoT(见 chat_json 注释)
        schema_help = (
            f"你必须严格输出符合以下 JSON Schema 的合法 JSON(不要包裹 markdown):\n"
            f"{json.dumps(schema, ensure_ascii=False)}\n"
        )
        prompt = list(messages)
        if prompt and prompt[0].get("role") == "system":
            prompt[0] = {**prompt[0], "content": prompt[0].get("content", "") + "\n\n" + schema_help}
        else:
            prompt = [{"role": "system", "content": schema_help}] + prompt

        # 严格模式:优先使用传入值,否则跟随 settings
        strict_mode = strict_mode if strict_mode is not None else bool(
            getattr(_cfg, "agent_structured_output_strict", True)
        )
        max_retries = int(getattr(_cfg, "agent_structured_output_max_retries", 3) or max_retries)

        def _validate_field_level(data: dict, schema: dict, prefix: str = "") -> list[str]:
            """字段级校验:返回详细错误列表(含字段路径)。"""
            errors: list[str] = []
            props = schema.get("properties", {})
            required = schema.get("required", [])

            # 检查必填字段
            for field in required:
                if field not in data:
                    path = f"{prefix}.{field}" if prefix else field
                    errors.append(f"缺少必填字段: {path}")

            # 检查类型
            for field, value in data.items():
                path = f"{prefix}.{field}" if prefix else field
                if field in props:
                    field_schema = props[field]
                    expected_type = field_schema.get("type", "")
                    if expected_type == "string" and not isinstance(value, str):
                        errors.append(f"字段 {path} 期望字符串类型,实际为 {type(value).__name__}")
                    elif expected_type == "number" and not isinstance(value, (int, float)):
                        errors.append(f"字段 {path} 期望数字类型,实际为 {type(value).__name__}")
                    elif expected_type == "integer" and not isinstance(value, int):
                        errors.append(f"字段 {path} 期望整数类型,实际为 {type(value).__name__}")
                    elif expected_type == "boolean" and not isinstance(value, bool):
                        errors.append(f"字段 {path} 期望布尔类型,实际为 {type(value).__name__}")
                    elif expected_type == "array" and not isinstance(value, list):
                        errors.append(f"字段 {path} 期望数组类型,实际为 {type(value).__name__}")
                    elif expected_type == "object" and not isinstance(value, dict):
                        errors.append(f"字段 {path} 期望对象类型,实际为 {type(value).__name__}")
                    # 递归检查嵌套对象
                    if expected_type == "object" and isinstance(value, dict) and props[field].get("properties"):
                        errors.extend(_validate_field_level(value, props[field], path))
            return errors

        # 尝试 json_schema(最严格) → json_object(降级) → 普通 chat+手动校验(最宽)
        async def _try_with_format(format_type: str, kwargs_extra: dict) -> tuple:
            """尝试用指定 response_format 调用,返回 (content, error)"""
            kwargs = {
                "model": self.model,
                "messages": prompt,
                "temperature": temperature,
            }
            kwargs.update(kwargs_extra)
            kwargs["response_format"] = {"type": format_type}
            if format_type == "json_schema" and strict_mode:
                kwargs["response_format"]["json_schema"] = {
                    "name": "output", "strict": True, "schema": schema
                }
            try:
                resp = await self._invoke(
                    lambda client, model: client.chat.completions.create(**{**kwargs, "model": model}),
                    f"chat_json_schema_{format_type}",
                )
                return resp.choices[0].message.content or "", None
            except Exception as e:  # noqa: BLE001
                return None, str(e)

        last_err: Optional[str] = None
        last_content = ""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                # 仅追加错误反馈,不重复发送历史内容(避免 token 膨胀)
                feedback = (
                    f"上次输出未通过校验: {last_err[:300]}\n"
                    f"请重新输出符合 schema 的 JSON(只输出 JSON,不要包裹 markdown)。"
                )
                prompt = list(messages) + [{"role": "user", "content": feedback}]
            content, err = await _try_with_format("json_schema", {})
            if err:
                # 降级到 json_object
                content, err2 = await _try_with_format("json_object", {})
                if err2:
                    last_err = f"JSON 提取失败: {err2}"
                    last_content = ""
                    continue
            else:
                last_err = None

            last_content = content
            data = _extract_json(content)
            if not isinstance(data, dict):
                last_err = "输出不是合法 JSON 对象"
                continue
            # 字段级校验(优先级高于简单 required 检查)
            if strict_mode:
                field_errors = _validate_field_level(data, schema)
                if field_errors:
                    last_err = "字段校验失败:\n" + "\n".join(f"  - {e}" for e in field_errors)
                    continue
            # 额外校验:检查 required 字段(兼容旧逻辑)
            required_fields = schema.get("required", [])
            missing = [f for f in required_fields if f not in data]
            if missing and strict_mode:
                last_err = f"缺少必填字段: {', '.join(missing)}"
                continue
            # 调用自定义 validator
            if validator is not None:
                ok, err = validator(data)
                if not ok:
                    last_err = err or "未通过校验"
                    continue
            return data
        raise LLMError(f"结构化输出校验失败(重试 {max_retries} 次): {last_err[:500]}")

    @property
    def info(self) -> dict:
        return {"provider": self.provider, "model": self.model}


def _extract_json(text: str):
    """从文本中提取 JSON 对象;失败返回 None。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 找到第一个 {,然后用括号匹配找到对应的 },避免多余内容被纳入
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    end = start
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _friendly_llm_error(last_err: Exception, provider: str, model: str) -> str:
    """把底层 LLM 异常包装成可操作的错误信息,指明 provider/model 与下一步。"""
    base = f"LLM 调用失败 (provider={provider} model={model}): {last_err}"
    low = str(last_err).lower()
    if "arrears" in low or "arrearage" in low or "overdue" in low or "not in good standing" in low \
            or "欠费" in low or "good standing" in low:
        return base + " —— 账户欠费/未开通(Arrearage)。请在服务商后台确认账号在正常服务状态并完成充值/开通模型服务。"
    if "429" in low or "rate limit" in low or "quota" in low or "limit" in low:
        return base + " —— API 限流/配额耗尽。请等待限流恢复,或在设置中切换服务商/模型(如 qwen、deepseek)。"
    if "401" in low or "403" in low or "unauthorized" in low or "api key" in low:
        return base + " —— API Key 无效或无权限。请检查该 provider 的 *_API_KEY 配置。"
    if "access denied" in low:
        return base + " —— 访问被拒(可能欠费/未开通服务/无该模型权限)。请确认账号在正常状态并已开通对应模型服务。"
    if "404" in low or "not found" in low or "model" in low:
        return base + " —— 模型或路径不存在。请检查 *_MODEL / *_BASE_URL 配置。"
    if "timeout" in low or "timed out" in low:
        return base + " —— 调用超时。可增大 AGENT_LLM_TIMEOUT(秒)。"
    return base


def build_llm(provider: Optional[str] = None) -> LLMClient:
    return LLMClient(provider)
