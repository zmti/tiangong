"""Graph 驱动的 Agent 核心。

架构:Agent 不再是手写 ReAct 循环,而是把一次运行建模为一张图:

    prepare(路由/工具路由/规划/构造消息) -> llm(调用模型)
        -> tools(并行执行工具/反思) -> llm  (循环,带步数预算)
        -> finalize(最终回答/自动记忆)     -> END
        -> giveup(达到步数上限)            -> END

关键能力:
- 逐节点 checkpoint:每个节点执行完把完整状态(消息/计划/步骤/路由)落盘
  (data/checkpoints.db),进程重启后可按 run_id 断点续跑。
- 与旧版行为完全兼容:原生 function calling + 并行工具 + 文本兜底、
  工具失败反思/自愈、任务规划、长期记忆注入/自动沉淀、工具路由、
  危险工具确认流、模型路由、会话摘要压缩、trace 记录。
"""
from __future__ import annotations

# 提前加载 core.planner 以取得 TaskPlan 定义(避免循环导入)
import app.core.planner as _core_planner
TaskPlan = _core_planner.TaskPlan

import asyncio
import datetime
import json
import logging
import re
import time
import uuid
from typing import Optional

from app.config import settings
from app.core.constants import (TEST_FAIL_MARKERS, TRANSIENT_PATTERNS, NO_RETRY_TOOLS,
                                is_context_length_error)
from app.runtime.graph import END, Graph, Node
from app.runtime.state import AgentState


def _clip(text: str, cap: int) -> str:
    """按字符上限截断文本,防止超大工具结果/图片 base64 撑爆上下文。"""
    if len(text) <= cap:
        return text
    return text[:cap] + "\n...[结果过长已截断]"


def _clip_history_for_json(history: list[dict]) -> str:
    """安全截断对话历史为 JSON:先逐条截断 content,再整体限制字节数。

    避免直接对 json.dumps 结果做字符截断导致 JSON 结构损坏。
    """
    # 修复: 记忆抽取 LLM 只认 OpenAI 合法角色, 先降级 error/plan/孤立tool
    from app.memory.session import sanitize_history_for_llm
    history = sanitize_history_for_llm(history)
    clipped = []
    total_bytes = 0
    for m in history:
        raw = m.get("content", "") or m.get("kind", "")
        if not str(raw).strip():
            continue
        s = str(raw)
        # 按字节截断(UTF-8 下中文字符 3 字节)
        if len(s.encode("utf-8")) > 500:
            s = s.encode("utf-8")[:497].decode("utf-8", errors="ignore")
            s = s + "…"
        clipped.append({"role": m.get("role", "user"), "content": s})
        total_bytes += len(json.dumps({"role": m.get("role", "user"), "content": s}, ensure_ascii=False).encode("utf-8"))
        if total_bytes > 5500:
            break
    return json.dumps(clipped, ensure_ascii=False)


def _truncate_to_token_budget(text: str, max_tokens: int, llm=None) -> tuple[str, bool]:
    """按 token 配额截断输出文本,返回 (文本, 是否被截断)。

    优先用 `llm.last_usage` 里的真实 completion_tokens 估算该模型的 token 密度;
    无用量信息时用启发式 estimate_tokens 估计。用于请求级 max_output_tokens 硬控制。
    """
    if max_tokens <= 0 or not text:
        return text, False
    from app.memory.context import estimate_tokens

    tokens = estimate_tokens(text)
    if tokens <= max_tokens:
        return text, False
    # 按比例算出可保留的字符数后截断
    keep_chars = max(1, int(len(text) * max_tokens / max(1, tokens)))
    cut = text[:keep_chars].rstrip()
    return cut, True


# ---- 续写/截断检测(从已删除的 app.agent_core 外科手术式回收) ----
# 背景:答案"输出中断"自动续写特性原本只在 app.agent_core 实现且未接入 live 图内核,
# 双内核收敛(删除 agent_core)时把这些与架构无关的纯判定函数回收进 live 模块,
# 保留其单测;特性本身的接入(run_stream/_stream_round 续写)仍待移植到图内核。

def _answer_looks_truncated(answer: str, min_len: int = 10, max_output_tokens: int = 0) -> tuple[bool, str]:
    """判断答案是否疑似被截断(未正常结束)。Returns: (is_truncated, reason)。

    仅当存在明确信号(触顶 max_output_tokens / 未闭合代码块 / 未完成标点)时才判为截断,
    默认视为正常结束——避免过度判定导致每次回答都多一次续写调用。
    max_output_tokens 硬限制优先于标点启发式(上游回 stop 但仍触顶时也视为截断)。
    """
    if not answer or not answer.strip():
        return False, ""
    # max_output_tokens 硬限制优先:即使结尾是正常标点,触顶也视为截断(上游回 stop 的兜底)
    if max_output_tokens > 0:
        from app.memory.context import estimate_tokens
        if estimate_tokens(answer) >= max_output_tokens:
            return True, "hit_max_output_tokens"
    s = answer.rstrip()
    # 未闭合的代码块:用户看到半截代码
    if s.count("```") % 2 == 1:
        return True, "code_fence_unclosed"
    # 显式完成标记
    if s.endswith(("(已完成)", "（已完成）", "(已完成)。", "（已完成）。")):
        return False, ""
    # 以明显的断句符号结尾,通常不算截断
    if s.endswith(("。", "！", "？", ".", "!", "?")):
        return False, ""
    # 以明显未完成的标点结尾
    if s.endswith(("：", ":", "，", ",", "、", ";", "；", "(", "（", "[", "【", "{", "『", "「", "-", "—", "<=", "要", "的", "与", "和", "及", "或", "但")):
        return True, "以未完成标点结尾"
    import re
    # 只看最后一行:原实现用 re.search(r"^\s*...", s) 且未开 MULTILINE,
    # 实际只匹配了**首行**,末行判断完全失效。
    last_line = s.rsplit("\n", 1)[-1].strip()
    # 末行只剩一个 Markdown 标题(标题后没有正文)→ 明显截断
    if re.match(r"#{1,6}\s*\S", last_line):
        return True, "以标题结尾(标题后无正文)"
    # 末行看起来像列表项/代码块开头,可能是正常结尾
    if re.match(r"(\d+[.)、]|[-*+|>]|```|~~~)", last_line):
        return False, ""
    return False, ""


def _strip_continuation_overlap(prev: str, cont: str) -> str:
    """去除续写内容中与前文重叠的前缀。"""
    if not prev or not cont:
        return cont
    prev = prev.rstrip()
    cont = cont.lstrip()
    max_overlap = min(len(prev), len(cont), 200)
    for i in range(max_overlap, 0, -1):
        if prev.endswith(cont[:i]):
            return cont[i:]
    return cont


# 续写相关
_CONTINUE_PROMPT = (
    "请从上一次回答中断的地方继续，不要重复已输出的内容。"
    "直接输出后续内容即可。"
)
_CONTINUE_DONE_MARKERS = ("(已完成)", "（已完成）", "(已完成)。", "（已完成）。")


from app.core.llm import LLMClient, ToolCall, LLMError
from app.core.planner import TaskPlan, _normalize_steps, apply_plan_tool, generate_plan
from app.core.prompts import TOOL_CALL_PATTERN, build_system_prompt, build_tools_description
from app.core.telemetry import _current_run_id, tracer
from app.core.observability import get_observability, traced, trace_block
from app.tools.categories import SYSTEM_TOOLS
from app.tools.registry import ToolRegistry, get_registry
from app.core.verification import run_verification_hook
from app.core.memory_consolidation import consolidate_memory, recall_memory, get_consolidator
from app.core.task_planner import TaskPlanner, PLAN_TEMPLATES, create_plan_from_template
from app.core.cost_control import get_cost_controller, CostController, BudgetExceededError

# 复用 core.planner 的 TaskPlan 定义
import app.core.planner as _core_planner
TaskPlan = _core_planner.TaskPlan

logger = logging.getLogger(__name__)

# 计划工具:在 ReAct 循环内被 Agent 拦截,维护私有计划状态
PLAN_TOOLS = ("set_plan", "update_plan")

# 多智能体编排模式(Orchestration 层),与执行模式(single/auto/review/ensemble)正交
ORCHESTRATION_MODES = ("sequential", "hierarchical", "consensual")

# 工具路由快速通道阈值: 简短问候直接跳过 LLM 路由
_TOOL_ROUTE_SHORT_INPUT_THRESHOLD = 15
_TOOL_ROUTE_SHORT_INPUT_KEYWORDS = ("代码", "文件", "搜索", "网页", "执行", "运行", "分析", "报告", "计算", "+", "-", "*", "/", "=")

# 任务规划快速通道阈值: 简短消息跳过规划
_PLAN_SHORT_INPUT_THRESHOLD = 20
_PLAN_SHORT_INPUT_KEYWORDS = ("代码", "文件", "搜索", "网页", "执行", "运行", "分析", "报告", "计算", "脚本", "程序")

# 确认流轮询间隔(秒)
_CONFIRM_POLL_INTERVAL = 0.8

# 错误分析最近消息数
_ERROR_RECENT_MESSAGE_COUNT = 6

# ---- "答案是没解析成功的工具调用块"的检测 ----
# 背景(2026-09-28 实测):文本模式下模型吐出
#   <tool_calls><invoke name="run_terminal">\n\n</invoke></tool_calls>
# (invoke 体为空 —— 参数被输出上限截断或模型本身没给参数)。XML 解析器解析不出
# tool_calls → 该轮被当作"没有工具调用" → finalize 直接把原始 XML 当成最终答案
# 发给了用户(会话 desktop-muc7dv38, 09:22:22),气泡里是一串 <tool_calls>。
_UNPARSED_TOOL_CALL_MAX_LEN = 4000
_UNPARSED_TOOL_CALL_MIN_PROSE = 20


def _looks_like_unparsed_tool_call(text: str) -> bool:
    """判断答案是否其实是"没解析成功的工具调用块"(而非给用户的正文)。

    判定分两步,尽量不误伤:
      1) 答案必须**以**工具调用块开头(允许被 ``` 代码块包裹)。
         正常回答即使夹带标签,也是先说正文;而以标签开头说明整条都是残骸。
      2) 把工具调用块剥掉后,剩下的实质正文(去掉符号/空白)不足 20 字符。
    """
    t = (text or "").strip()
    if not t or len(t) > _UNPARSED_TOOL_CALL_MAX_LEN:
        return False
    # 允许被 ```json / ```xml 代码块包裹
    t = re.sub(r'^```[a-zA-Z]*\s*', '', t)
    t = re.sub(r'\s*```$', '', t).strip()
    low = t.lower()
    if not low.startswith(("<tool_calls", "<tool_call", "<invoke", '{"tool"')):
        return False
    s = re.sub(r'<tool_calls?>[\s\S]*?</tool_calls?>', '', t, flags=re.IGNORECASE)
    s = re.sub(r'<invoke\b[\s\S]*?</invoke>', '', s, flags=re.IGNORECASE)
    s = re.sub(r'<invoke\b[^>]*/?>', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\{"tool"\s*:[\s\S]*?\}\s*\}', '', s)
    s = re.sub(r'</?(?:tool_calls?|invoke|parameter)\b[^>]*>', '', s, flags=re.IGNORECASE)
    return len(re.sub(r'[\s\-_*`|\[\]{}<>]+', '', s)) < _UNPARSED_TOOL_CALL_MIN_PROSE


# 退化答案的"过渡语"句式:开头是应答词,主体是"继续读取/查看/分析"类动作短语
_DEGENERATE_TRANSITION_RE = re.compile(r"^(好的|现在|让我|接下来|首先|继续|我)[，,]?")
_DEGENERATE_ACTION_RE = re.compile(r"(继续|读取|查看|分析|检查|扫描|深入)")


def _looks_like_degenerate_repeat(text: str) -> bool:
    """检出"同一句过渡语重复 N 遍"式退化答案(实证 2026-10-01 12:41/12:44)。

    模型在上下文里失去对话连续性后(见 _build_messages 的时间戳排序修复),
    会把"好的,我继续深入分析剩余的关键文件。"这类**开场过渡语**反复输出
    8 遍当成最终答案,finish_reason=stop,没有任何实质内容。

    判定尽量保守,避免误伤正常回答:
      - 长答案(>2000 字)不判;含标题/代码块/表格结构的不判;
      - 任一行超过 80 字符不判(退化行都是短句);
      - 主判定:按行贪心聚类(行间相似度>=0.55 归同簇),最大簇 >=3 行
        且覆盖 >=60% 的非空行;
      - 短答案特例(实证 12:44 的 71 字两条):<=200 字、全部行都是
        "应答词开头 + 继续类动作"的过渡语句式,且行间相似。
    """
    import difflib

    t = (text or "").strip()
    if not t or len(t) > 2000:
        return False
    # 有实质结构的答案(标题/代码/表格)不按退化处理
    if "```" in t or re.search(r"^#{1,6}\s", t, flags=re.MULTILINE) or t.count("|") >= 4:
        return False
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if len(lines) < 2 or any(len(ln) > 80 for ln in lines):
        return False
    # 结构化列表(每行 -/*/•/数字. 开头)是正常写法,不按退化处理
    _LIST_MARK = re.compile(r"^((?:[-*•]|\d{1,2}[.)、])\s*)")
    if sum(1 for ln in lines if _LIST_MARK.match(ln)) >= len(lines) * 0.6:
        return False
    clusters: list[list[str]] = []
    for ln in lines:
        for c in clusters:
            if max(difflib.SequenceMatcher(None, ln, x).ratio() for x in c) >= 0.55:
                c.append(ln)
                break
        else:
            clusters.append([ln])
    best = max((len(c) for c in clusters), default=0)
    if best >= 3 and best / len(lines) >= 0.6:
        return True
    # 短答案特例(实证 12:44 的 71 字两条,两行相似度只有 0.33 聚不了类):
    # <=200 字且**每一行**都是"应答词开头 + 继续类动作"的过渡语 —— 整条回答
    # 没有任何实质内容,判退化。要求每行都命中,正常回答总有一行带实际内容。
    if len(t) <= 200:
        return all(
            _DEGENERATE_TRANSITION_RE.match(ln) and _DEGENERATE_ACTION_RE.search(ln)
            for ln in lines
        )
    return False

# 明文"思考/思路"小标题:模型不按 <thinking> 标签走时,会把推理写成这种段落标题,
# 直接渲染到答案区会让整条回答变成"过程自述"(实证 09-20 的 "thinking 过程")
_ANSWER_NOISE_HEADING_RE = re.compile(
    # 注意:不剥 "# 解题思路" 这类 markdown 标题 —— 它是模型给答案选的结构,
    # 且正文内容都在这节里,去掉标题反而留下一截没有来历的正文。
    # 只剥没有 # 前缀的裸标题行(模型没按 <%s> 标签走时写的那种)。
    r"^(?:thinking[ \t]*过程|思考过程|思考流程|推理过程|分析过程)[ \t]*[:：]?[ \t]*$",
    re.IGNORECASE,
)

# 开场自述句首词
_NARRATION_HEAD_RE = re.compile(
    r"^(?:好的[、,，]?[ \t]*)?(?:现在|接下来|首先|接着|让我|我(?:来|先|马上|立即|这就|再)|先)"
)
# 自述里的动作词(命中才算"过程独白",避免误删正常陈述句)
_NARRATION_VERB_RE = re.compile(
    r"(分析|查看|看看|读取|检查|确认|尝试|梳理|跑一|运行|修复|调试|探索|验证|说明|执行|总结|汇总|梳理|定位|拉取|换用)"
)
# 答案里出现这些结构标记就当作"有实质内容",不参与自述剥离判断
_SUBSTANCE_MARK_RE = re.compile(
    r"(^#{1,6}[ \t]|```|\|.*\||^[-*•][ \t]|^\d{1,2}[.)、][ \t])", re.MULTILINE
)

_MIN_SUBSTANCE_CHARS = 120


def _is_narration_chunk(chunk: str) -> bool:
    """判断一小段文本是不是"过程自述"(开场白/我要做什么),而非实质内容。"""
    s = (chunk or "").strip()
    if not s or len(s) > 120:
        return False
    if _SUBSTANCE_MARK_RE.search(s):
        return False
    if not _NARRATION_HEAD_RE.match(s):
        return False
    # 带"结论"或以冒号引出内容的句子是正常陈述(如"先说结论:…"),不剥
    if "结论" in s or s.rstrip().endswith(("：", ":")):
        return False
    return bool(_NARRATION_VERB_RE.search(s))


def _clean_answer_noise(text: str) -> tuple[str, str]:
    """清洗答案里的"过程自述 + 明文思考段",返回 (清洗后正文, 被剥离的噪音)。

    为什么不只靠 <thinking> 标签:实测(09-20/10-01)模型经常不遵守标签格式,
    把推理写成 "thinking 过程" 小标题或 "我先分析…。" 这类开头自述,
    于是答案区变成一整篇自言自语 —— 用户看到的就是"输出信息偏差太大"。
    被剥离的内容不丢弃,由调用方转成 thinking 事件(用户仍可在思考面板看到)。

    刻意保守:只在"剥离后仍有 >=120 字实质内容"时才动手,且最多剥两段。
    """
    t = (text or "").strip()
    if not t:
        return "", ""

    # ① 剥掉明文思考小标题本身(正文内容保留 —— 模型常把真正的分析放在这节里)
    removed: list[str] = []
    lines = t.split("\n")
    kept_lines = []
    for ln in lines:
        if _ANSWER_NOISE_HEADING_RE.match(ln):
            removed.append(ln.strip())
            continue
        kept_lines.append(ln)
    if removed:
        t = "\n".join(kept_lines).strip()
        t = re.sub(r"\n{3,}", "\n\n", t)

    def _rest_len(parts: list[str]) -> int:
        return len(re.sub(r"\s+", "", "".join(parts)))

    # ② 剥掉开头的开场自述段落/句子(最多两段)
    paras = re.split(r"\n\s*\n", t)
    i = 0
    while i < len(paras) and i < 2 and paras[i].strip():
        p = paras[i].strip()
        rest = paras[:i] + paras[i + 1:]
        if _is_narration_chunk(p) and _rest_len(rest) >= _MIN_SUBSTANCE_CHARS:
            removed.append(p)
            paras.pop(i)
        else:
            break
    t = "\n\n".join(paras).strip()

    # ③ 单段长答案:只剥第一句(仍要求剩余有实质内容)
    if paras and len(paras) == 1:
        m = re.match(r"^[^。！？\n]{0,80}[。！？][ \t]*", t)
        if m:
            head, tail = m.group(0), t[m.end():]
            if (_is_narration_chunk(head)
                    and len(re.sub(r"\s+", "", tail)) >= _MIN_SUBSTANCE_CHARS):
                removed.append(head.strip())
                t = tail.strip()

    noise = "\n\n".join(x for x in removed if x).strip()
    return t, noise

# 历史摘要保留最近消息数
_MEMORY_RECENT_HISTORY_COUNT = 6

# 自动记忆沉淀单条事实最大字符数
_AUTO_MEMORY_FACT_MAX_CHARS = 30


class AgentResult:
    def __init__(self, answer: str, steps: list[dict], model: str, interrupt: Optional[dict] = None):
        self.answer = answer
        self.steps = steps
        self.model = model
        # 运行被 interrupt 暂停时携带暂停载荷(含待确认项);否则为 None
        self.interrupt = interrupt

    @property
    def interrupted(self) -> bool:
        return self.interrupt is not None


class Agent:
    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        registry: Optional[ToolRegistry] = None,
        max_steps: Optional[int] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        route: Optional[bool] = None,
        session_id: Optional[str] = None,
        approve_tools: Optional[list[str]] = None,
        mode: Optional[str] = None,
        critic_provider: str = "",
        critic_model: str = "",
        ensemble_models: Optional[list[str]] = None,
        max_output_tokens: int = 0,
        orch_subtasks: int = 0,
        orch_candidates: Optional[list[str]] = None,
        task_config: Optional[dict] = None,
        project_path: str = "",
        fast_mode: bool = False,
        # 智能体硬规则(chat.py._build_agent 会传入, 缺了会抛 TypeError 导致对话主流程崩溃)。
        # 默认 None, 不影响其它调用方; 语义见 _execute_tool_once / 输入扫描处。
        rule_blocked_tools: Optional[list[str]] = None,
        rule_terms: Optional[list[str]] = None,
        rule_paths: Optional[list[str]] = None,
        rule_warn_terms: Optional[list[str]] = None,
        rule_warn_paths: Optional[list[str]] = None,
    ):
        self.llm = llm or LLMClient()
        # fast_mode: 轻量交互模式。置 True 时 _node_prepare 跳过任务规划/工具路由/
        # 记忆注入等"重 LLM 准备阶段"(各自带 AGENT_LLM_TIMEOUT 重试窗口),只做纯问答,
        # 适合 CLI/交互多轮对话——简单问答不必为每个问题跑一遍规划+记忆,避免 LLM
        # 限流/故障时每个问题都卡满超时。默认 False,API/后台任务路径行为不变。
        self.fast_mode = fast_mode
        # 项目路径:注入到 ContextVar,工具运行时可通过 resolve_cwd() 获取
        self.project_path = project_path
        if project_path:
            import logging as _log
            _log.getLogger("agent").info("Agent project_path=%s", project_path)
            from app.tools.project_context import set_project_path
            set_project_path(project_path)
        # 自动缩放配置:根据任务复杂度动态调整参数
        self._task_config = task_config
        if task_config:
            # task_config 优先，如果没有则使用参数或默认配置
            # 注意: max_steps=0 表示"不设步骤上限"(新语义),不能用 `or` 把 0 当成空值覆盖;
            # 这里用 is not None 判断:task_config 显式传 0 时保留 0。
            self.max_steps = task_config.get("max_steps") if task_config.get("max_steps") is not None else (max_steps if max_steps is not None else settings.agent_max_steps)
            self.context_budget = task_config.get("context_budget") or settings.context_char_budget
            self.mode = task_config.get("mode") or (mode or settings.agent_mode or "auto").lower()
        else:
            # max_steps=None → 用 settings;显式传 0 → 保留 0(不限制)
            self.max_steps = max_steps if max_steps is not None else settings.agent_max_steps
            self.context_budget = settings.context_char_budget
            self.mode = (mode or settings.agent_mode or "auto").lower()
        # 输出 token 配额硬控制:0 = 不限制;>0 时答案超过即截断并附提示
        self.max_output_tokens = max_output_tokens or int(settings.agent_default_max_output_tokens or 0)
        self.ensemble_models = list(ensemble_models or [])
        # 编排模式可调参数(UI 配置项):子任务数量提示 / 候选执行体清单。
        # 仅对 orchestration 类模式生效;执行模式(single/auto/review/ensemble)忽略。
        self.orch_subtasks = int(orch_subtasks or 0)
        _valid_modes = ("single", "auto", "review", "ensemble")
        self.orch_candidates = [c for c in (orch_candidates or []) if c in _valid_modes]
        # critic 独立评审模型(双角色反思);未配置时复用执行模型 self.llm。
        # 请求级 critic_provider/critic_model 优先,其次全局配置。
        self.critic_llm = None
        critic_provider = critic_provider or settings.agent_critic_provider
        critic_model = critic_model or settings.agent_critic_model
        if critic_provider:
            try:
                from app.core.llm import LLMClient as _L

                self.critic_llm = _L(critic_provider, critic_model or None)
            except Exception as e:  # noqa: BLE001
                logger.warning("critic_llm 初始化失败,降级为单模型模式: %s", e)
                self.critic_llm = None
        # 实例级评审/校验开关:single 关闭,review 强制开启,auto 跟随全局
        self._verify_enabled = settings.agent_verify
        self._reflect_enabled = settings.agent_reflect
        if self.mode == "single":
            self._verify_enabled = False
            self._reflect_enabled = False
        elif self.mode == "review":
            self._verify_enabled = True
            self._reflect_enabled = True
        if registry is None:
            # 显式取"真正注册了工具"的 app.tools.registry 实例(含 async list(),
            # 供 smart_router 使用)。模块全局 get_registry() 默认是空注册表、没有
            # async list() 方法,若 Agent 落到那个空表,工作流/对话里的 Agent 节点就
            # 调不到任何工具,智能路由也会反复打 "has no attribute 'list'" warning。
            # 调用方显式传入 registry 时保持原样。
            from app.tools.registry import get_registry as _tool_registry_get
            registry = _tool_registry_get()
        self.registry = registry
        self.system_prompt = system_prompt or settings.agent_system_prompt
        self.temperature = temperature if temperature is not None else settings.agent_temperature
        self.route = route if route is not None else settings.agent_routing_enabled
        # 上下文预算:实例级覆盖(子智能体可给更小预算以强制聚焦)。
        # 默认继承全局;delegate/handoff 创建子 Agent 时显式收紧。
        # 注意: task_config 中的 context_budget 已在上方赋值，此处仅用于无 task_config 时初始化
        if not hasattr(self, 'context_budget') or self.context_budget is None:
            self.context_budget = settings.context_char_budget
        self.token_budget = settings.agent_token_budget
        # 当前会话 ID:用于检索/写入 session 级长期记忆,并透传给记忆工具
        self.session_id = session_id or ""
        # 人机协作确认通过的危险工具(本次运行允许执行);空=未开启确认流或未批准
        self.approve_tools = list(approve_tools or [])
        # 智能体硬规则(chat.py._build_agent 传入, get_active_rules 已把 blocked/warn 分桶)。
        # blocked_* 会在工具执行处拦截; warn_* 按"仅记录不拦截"语义只存不拦(留观测钩子)。
        self.rule_blocked_tools = list(rule_blocked_tools or [])
        self.rule_terms = list(rule_terms or [])
        self.rule_paths = list(rule_paths or [])
        self.rule_warn_terms = list(rule_warn_terms or [])
        self.rule_warn_paths = list(rule_warn_paths or [])
        # 多模态内容块(OpenAI content parts):图片 image_url 等,随用户消息一起送 LLM。
        # 由 chat 接口解析 attachments 生成;模型不支持视觉时降级为文本提示。
        self.multimodal_parts: list[dict] = []
        # 分层记忆注入的提示块(由 _node_prepare 异步检索后缓存)
        self._memory_note: str = ""
        self._dangerous_tools = {s.strip() for s in settings.agent_dangerous_tools.split(",") if s.strip()}
        # 危险工具频率限制(共享实现,与 ToolExecutor 行为一致)
        from app.core.tool_safety import DangerousToolRateLimiter

        self._dangerous_rate_limiter = DangerousToolRateLimiter()
        # interrupt 式确认流:本次运行已由用户批准的工具名(runtime 运行时集合)
        self._runtime_approved: set[str] = set()
        # 工具路由:预选后只把相关工具子集暴露给模型;None=未路由(全量)
        self._active_tools: Optional[list[str]] = None
        self._always_tools = list(SYSTEM_TOOLS)
        # 原生工具调用开关:初始化时探测 LLM 是否支持
        self._native_enabled = settings.agent_native_tools and callable(getattr(self.llm, "chat_with_tools", None))
        # 基线值: 该实例"意图"的原生工具开关。_reset_run_state 每轮恢复到基线,
        # 而非无条件用全局 settings —— 允许调用方(CLI)通过覆盖 _native_enabled
        # 一次性关闭原生路径, 且多轮复用实例时不被 _reset_run_state 重置回去。
        self._native_baseline = self._native_enabled
        self._schemas_cache = None
        # 任务计划状态(Plan-then-Execute)
        self._plan: Optional[TaskPlan] = None
        self._plan_dirty = False
        # 工具结果去重缓存:同一运行内 (tool, args) -> result(仅成功结果)
        # 使用 OrderedDict 实现 LRU 淘汰(而非 FIFO)
        try:
            from collections import OrderedDict
            self._tool_cache: dict[tuple[str, str], str] = OrderedDict()
        except Exception as e:  # noqa: BLE001
            logger.warning("工具缓存初始化失败,降级为无缓存模式: %s", e)
            self._tool_cache = {}
        # 工具缓存锁:保护 OrderedDict 操作，防止并发访问导致损坏
        self._tool_cache_lock = asyncio.Lock()
        # 仅对安全(纯函数)工具启用缓存
        safe_tools_str = settings.agent_tool_cache_safe_tools or ""
        self._tool_cache_safe: set[str] = {
            t.strip() for t in safe_tools_str.split(",") if t.strip()
        }
        # 搜索查询历史:用于检测重复/相似搜索,防止搜索循环
        self._search_query_history: list[tuple[str, str]] = []  # (query, timestamp)
        self._search_max_repeats: int = getattr(settings, 'agent_search_max_repeats', 3)
        # 图运行时
        self._graph: Optional[Graph] = None
        self._checkpoint = None
        # 修复: 取消标志(asyncio.Event), 供 tasks.py 等调用方注入。
        # 旧实现 tasks.py 写入 agent._cancel_flag 但 Agent 从不读取该属性,
        # 导致取消形同虚设, 任务只能跑满 max_minutes 才停。
        # 现在 _build_graph 把它传入 Graph, 在节点边界检查并抛 CancelledError。
        self._cancel_flag: Optional[asyncio.Event] = None
        if settings.agent_checkpoint_enabled:
            from app.runtime.checkpoint import CheckpointManager

            try:
                self._checkpoint = CheckpointManager()
            except Exception as e:  # noqa: BLE001  # checkpoint 不可用不影响主流程
                logger.warning("checkpoint 加载失败,降级为无持久化模式: %s", e)
                self._checkpoint = None

        # 自动验证钩子:代码变更后运行 pytest/ruff/mypy
        self._verification_enabled = getattr(settings, "agent_verification_enabled", True)
        self._verification_hook = None
        if self._verification_enabled:
            from app.core.verification import get_verification_hook
            self._verification_hook = get_verification_hook(self.project_path)

        # 记忆固化器:每轮对话后提取关键决策/代码片段
        self._memory_consolidator = None
        if getattr(settings, "agent_memory_consolidation_enabled", True):
            from app.core.memory_consolidation import get_consolidator
            self._memory_consolidator = get_consolidator()

        # 增强版任务规划器:JSON 计划 + 检查点 + 可中断
        self._task_planner = None
        if self._checkpoint:
            self._task_planner = TaskPlanner(self._checkpoint)

        # 成本控制器:Token预算 + 模型分级路由 + 语义缓存
        self._cost_controller = None
        if getattr(settings, "agent_token_budget_session", 0) > 0 or getattr(settings, "agent_token_budget_request", 0) > 0 or getattr(settings, "agent_token_budget_task", 0) > 0:
            self._cost_controller = get_cost_controller()

        # 可观测性:追踪、指标、告警
        self._observability = None
        if getattr(settings, "agent_tracing_enabled", True):
            self._observability = get_observability()

    def _disable_native(self) -> None:
        """关闭原生工具路径(当前运行 + 后续轮次基线), 降级为文本模式。

        同时更新 _native_baseline, 使 _reset_run_state 跨轮恢复时也保持关闭 ——
        一旦某次原生调用失败/不支持, 后续复用同实例的多轮都走文本路径, 不再反复
        触碰原生路径(欠费/限流账号下原生调用更易失败)。
        """
        self._native_enabled = False
        self._native_baseline = False

    def _reset_run_state(self) -> None:
        """清空 Agent 级"跨任务"状态,使同一实例的多次 run() 互不串味。

        每个 run() 都会新建 AgentState(消息/步骤/计划快照是隔离的),但下列
        Agent 级字段在 __init__ 后不会自动重置:一旦某次运行设置过,会泄漏到
        下一次运行,导致模型带着上一问的 plan/工具路由/记忆/搜索历史去作答,
        表现为"换个问题答案还是上一问的"。服务器端每请求新建 Agent 实例故无感;
        CLI/交互端复用同一实例跨多轮提问时必然踩到,故在 run/run_stream 入口统一清零。

        保留:配置型字段(llm/registry/max_steps/system_prompt/temperature/route/
        context_budget/token_budget/session_id/approve_tools/multimodal_parts/
        _dangerous_tools/_dangerous_rate_limiter/_always_tools/_schemas_cache=初始值/
        _tool_cache_safe/_search_max_repeats/_graph/_checkpoint)——这些是实例配置,
        不随单次任务变化。
        """
        # 任务计划
        self._plan = None
        self._plan_dirty = False
        # 工具路由:恢复为"未路由(全量)"
        self._active_tools = None
        self._schemas_cache = None
        # 原生工具开关:恢复为该实例的基线意图(而非全局 settings),使调用方
        # 一次性关闭原生路径的意图在跨轮复用实例时不被重置回去。
        self._native_enabled = self._native_baseline
        # 工具结果去重缓存:清空,避免上一问的缓存命中下一问
        if isinstance(self._tool_cache, dict):
            self._tool_cache.clear()
        # 搜索去重历史:清空
        self._search_query_history = []
        # 记忆注入提示块:清空(下一次 _node_prepare 会重新检索)
        self._memory_note = ""
        # 运行中批准集合:回到"实例级基线批准"(调用方显式预设的跨轮放行意图),
        # 只清掉"本次运行动态批准"的部分。此前直接 set() 会把 CLI 在 init 阶段预设的
        # _runtime_approved(本地放行工具名单)在第 1 轮 run_stream 入口就冲掉,
        # 导致 --yolo 形同虚设、危险工具仍触发 interrupt 卡死。
        self._runtime_approved = set(getattr(self, "_baseline_approved", set()))

    # ---- 对外接口 ----

    async def run(self, user_input: str, history: list[dict] | None = None) -> AgentResult:
        """执行 Agent,返回结果和完整执行步骤。带 trace 记录。"""
        self._reset_run_state()
        
        # 成本控制:开始请求、检查预算
        task_id = getattr(self, "_current_task_id", "")
        if self._cost_controller:
            try:
                # 根据输入长度估算复杂度
                complexity = "simple" if len(user_input) < 100 else "standard" if len(user_input) < 500 else "complex"
                self._cost_controller.begin_request(
                    session_id=self.session_id,
                    task_id=task_id,
                    task_complexity=complexity,
                )
            except BudgetExceededError as e:
                return AgentResult(answer=f"预算超限: {e}", steps=[], model=self.llm.info["model"])

        # 启动追踪
        trace = None
        if self._observability:
            trace = self._observability.start_trace(
                name="agent_run",
                session_id=self.session_id,
                task_id=task_id,
                kind="agent_run",
            )

        if self.mode == "ensemble":
            result = await self._run_ensemble(user_input, history or [])
            if trace and self._observability:
                self._observability.end_trace(trace.trace_id, "ok" if result.answer else "error")
            return result
        if self.mode in ORCHESTRATION_MODES:
            result = await self._run_orchestrated(user_input, history or [])
            if trace and self._observability:
                self._observability.end_trace(trace.trace_id, "ok" if result.answer else "error")
            return result
        state = AgentState(session_id=self.session_id, user_input=user_input)
        result = await self._run_state(state, history or [], stream=False)
        if trace and self._observability:
            self._observability.end_trace(trace.trace_id, "ok" if result.answer else "error")
        return result

    async def resume(self, run_id: str, new_input: str | None = None,
                     resume_value: Optional[dict] = None,
                     stream: bool = False) -> "AgentResult | AsyncGenerator[dict, None]":
        """从 checkpoint 恢复并继续执行。

        Args:
            run_id: checkpoint run_id。
            new_input: 可选,恢复时替换的用户输入(很少用)。
            resume_value: interrupt 式确认流的用户决定。支持:
                - [工具名,...]:已批准的工具白名单
                - {confirmation_id: {"approved": bool}}:按确认请求逐个决定
            若运行处于中断状态则从中断节点继续,否则从头节点继续。
            stream: 是否流式返回事件。为 True 时返回一个异步生成器(逐条 yield
                思考/工具/token/done 事件),使 /v1/chat/resume 走 SSE 流式续跑
                (带 30s 心跳):前端续跑看门狗不会因"长时间零事件"误报红条,且能实时
                看到进度。为 False(默认,兼容 runs.py / enhancements.py 等旧调用方)
                时返回一次性 AgentResult,语义不变。
        """
        if self._checkpoint is None:
            raise RuntimeError("checkpoint 未启用,无法恢复运行")
        state = await self._checkpoint.load(run_id)
        if state is None:
            raise RuntimeError(f"未找到运行记录: {run_id}")
        if new_input is not None:
            state.user_input = new_input
        if state.status == "interrupted" and state.interrupt is not None:
            state.interrupt["resume"] = resume_value
            state.status = "running"
        self._sync_from_state(state)
        if stream:
            # 流式续跑:Agent.resume 直接返回生成器,chat.py 的
            # isinstance(gen, AsyncGeneratorType) 判定为真 → 走 _resume_sse
            # (SSE + 30s 心跳)。此前这里 await 后返回 AgentResult,该判定恒 False,
            # _resume_sse 成为死代码,续跑全程零事件 → 前端"续跑等待超时"红条。
            return self._resume_stream(state)
        return await self._run_state(state, state.metadata.get("history", []) or [], stream=False)

    async def _resume_stream(self, state: AgentState):
        """流式续跑:从已 resume 过渡的状态出发,逐条 yield 图事件。

        与 run_stream 语义一致,只是起点是从 checkpoint 恢复的 state(而非新建)。
        graph.arun_stream 与 arun 共用同一引擎与中断恢复逻辑,因此续跑行为一致,
        仅事件实时下发(供 SSE 实时刷新 + 心跳保活前端看门狗)。
        """
        run_id = state.run_id
        token = _current_run_id.set(run_id)
        tracer.record({
            "type": "run_start",
            "run_id": run_id,
            "session_id": self.session_id,
            "input": (state.user_input or "")[:200],
            "max_steps": self.max_steps,
            "tool_confirm": bool(settings.agent_tool_confirm),
            "resume": True,
        })
        t0 = time.time()
        graph = self._build_graph()
        state.metadata["history"] = state.metadata.get("history", []) or []
        state.metadata["stream"] = True
        cancelled = False
        try:
            async for evt in graph.arun_stream(state):
                yield evt
            # 取消标志命中:产出 cancelled 并抛出,让调用方(如 _resume_sse)感知
            if state.status == "cancelled":
                cancelled = True
                yield {"type": "cancelled", "model": state.metadata.get("model", ""),
                       "steps": state.steps}
                raise asyncio.CancelledError(f"任务被取消 (run {run_id[:8]})")
            model = state.metadata.get("model") or self.llm.info["model"]
            interrupt = None
            if state.status == "interrupted" and state.interrupt:
                interrupt = state.interrupt.get("value")
            done = {"type": "done", "model": model, "steps": state.steps, "run_id": run_id}
            # 传递 finish_reason:前端检测截断(d=length)用
            fr = state.metadata.get("finish_reason", "")
            if fr:
                done["finish_reason"] = fr
            if interrupt is not None:
                done["interrupt"] = interrupt
            if state.status == "failed" and state.error:
                done["error"] = state.error
            yield done
        except asyncio.CancelledError:
            # 取消传播: 记录 trace 后向外抛出, 让调用方感知并终止
            if not cancelled:
                logger.warning("resume_stream 被外部取消: run_id=%s", run_id)
                tracer.record({"type": "run_end", "run_id": run_id, "ok": False,
                               "error": "cancelled", "duration": round(time.time() - t0, 3)})
                _current_run_id.reset(token)
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("resume() 运行异常，记录 trace 后终止: %s", e)
            tracer.record({"type": "run_end", "run_id": run_id, "ok": False,
                           "error": str(state.error or "")[:300],
                           "duration": round(time.time() - t0, 3)})
            _current_run_id.reset(token)
            raise
        tracer.record({"type": "run_end", "run_id": run_id, "ok": True,
                       "steps": len(state.steps), "answer_len": len(state.answer or ""),
                       "duration": round(time.time() - t0, 3)})
        _current_run_id.reset(token)

    def checkpoints(self, limit: int = 50, status: Optional[str] = None) -> list[dict]:
        """列出已持久化的运行记录(供后台任务/断点续跑管理)。"""
        if self._checkpoint is None:
            return []
        return self._checkpoint.list(limit=limit, status=status)

    def versions(self, run_id: str) -> list[dict]:
        """列出某次运行的全部历史版本(时间旅行)。"""
        if self._checkpoint is None:
            return []
        return self._checkpoint.versions(run_id)

    async def rollback(self, run_id: str, version: int,
                       resume_value: Optional[dict] = None) -> AgentResult:
        """时间旅行:回滚到历史版本,从该节点重新分叉执行。

        加载 run_id 的指定历史版本,重置最新指针后继续执行。
        """
        if self._checkpoint is None:
            raise RuntimeError("checkpoint 未启用,无法回滚")
        state = self._checkpoint.rollback(run_id, version)
        if state is None:
            raise RuntimeError(f"未找到运行记录 {run_id} 的版本 {version}")
        if state.status == "interrupted" and state.interrupt is not None:
            state.interrupt["resume"] = resume_value
            state.status = "running"
        self._sync_from_state(state)
        return await self._run_state(state, state.metadata.get("history", []) or [], stream=False)

    async def run_stream(self, user_input: str, history: list[dict] | None = None):
        """流式执行:先产出思考/工具事件,最后流式产出答案增量。

        yield 事件 dict:
          {"type": "thinking", "content": "..."}   LLM 推理/工具调用原文
          {"type": "tool", "name": ..., "args": ..., "result": ...}
          {"type": "token", "content": "..."}
          {"type": "done", "model": ..., "steps": [...]}
        """
        self._reset_run_state()
        if self.mode == "ensemble":
            result = await self._run_ensemble(user_input, history or [])
            for ch in result.answer:
                yield {"type": "token", "content": ch}
            yield {"type": "done", "model": result.model, "steps": result.steps}
            return
        if self.mode in ORCHESTRATION_MODES:
            async for evt in self._run_orchestrated_stream(user_input, history or []):
                yield evt
            return
        state = AgentState(session_id=self.session_id, user_input=user_input)
        run_id = state.run_id
        token = _current_run_id.set(run_id)
        tracer.record({
            "type": "run_start",
            "run_id": run_id,
            "session_id": self.session_id,
            "input": (user_input or "")[:200],
            "max_steps": self.max_steps,
            "tool_confirm": bool(settings.agent_tool_confirm),
        })
        t0 = time.time()
        graph = self._build_graph()
        state.metadata["history"] = history or []
        state.metadata["stream"] = True
        cancelled = False
        try:
            async for evt in graph.arun_stream(state):
                yield evt
            # 修复: 取消标志命中时 state.status == "cancelled", 不再产出正常 done 事件,
            # 改为产出 cancelled 事件并抛出 CancelledError 让调用方(如 tasks.py)终止
            if state.status == "cancelled":
                cancelled = True
                yield {"type": "cancelled", "model": state.metadata.get("model", ""), "steps": state.steps}
                raise asyncio.CancelledError(f"任务被取消 (run {run_id[:8]})")
            model = state.metadata.get("model") or self.llm.info["model"]
            interrupt = None
            if state.status == "interrupted" and state.interrupt:
                interrupt = state.interrupt.get("value")
            done = {"type": "done", "model": model, "steps": state.steps}
            # 传递 finish_reason:前端检测截断(d=length)用
            fr = state.metadata.get("finish_reason", "")
            if fr:
                done["finish_reason"] = fr
            if interrupt is not None:
                done["interrupt"] = interrupt
            if state.status == "failed" and state.error:
                done["error"] = state.error
            yield done
        except asyncio.CancelledError:
            # 取消传播: 记录 trace 后向外抛出, 让调用方(任务管理器)感知并终止
            if not cancelled:
                logger.warning("run_stream 被外部取消: run_id=%s", run_id)
                tracer.record({"type": "run_end", "run_id": run_id, "ok": False,
                               "error": "cancelled", "duration": round(time.time() - t0, 3)})
                _current_run_id.reset(token)
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("run() 运行异常，记录 trace 后终止: %s", e)
            tracer.record({"type": "run_end", "run_id": run_id, "ok": False,
                           "error": str(state.error or "")[:300],
                           "duration": round(time.time() - t0, 3)})
            _current_run_id.reset(token)
            raise
        tracer.record({"type": "run_end", "run_id": run_id, "ok": True,
                       "steps": len(state.steps), "answer_len": len(state.answer or ""),
                       "duration": round(time.time() - t0, 3)})
        _current_run_id.reset(token)

    # ---- 图运行 ----

    async def _run_state(self, state: AgentState, history: list[dict], stream: bool = False) -> AgentResult:
        run_id = state.run_id
        token = _current_run_id.set(run_id)
        tracer.record({
            "type": "run_start",
            "run_id": run_id,
            "session_id": self.session_id,
            "input": (state.user_input or "")[:200],
            "max_steps": self.max_steps,
            "tool_confirm": bool(settings.agent_tool_confirm),
        })
        t0 = time.time()
        graph = self._build_graph()
        state.metadata["history"] = history
        state.metadata["stream"] = stream
        
        # 记录图执行指标
        if self._observability:
            self._observability.obs("graph.nodes_total", len(graph._nodes), {"session_id": self.session_id})
        
        try:
            state, _events = await graph.arun(state)
        except Exception as e:  # noqa: BLE001
            tracer.record({"type": "run_end", "run_id": run_id, "ok": False,
                           "error": str(e)[:300], "duration": round(time.time() - t0, 3)})
            if self._observability:
                self._observability.inc("graph.runs.failed", 1, {"session_id": self.session_id})
            _current_run_id.reset(token)
            raise
        tracer.record({"type": "run_end", "run_id": run_id, "ok": True,
                       "steps": len(state.steps), "answer_len": len(state.answer or ""),
                       "duration": round(time.time() - t0, 3)})
        if self._observability:
            self._observability.inc("graph.runs.success", 1, {"session_id": self.session_id})
            self._observability.obs("graph.duration_ms", round((time.time() - t0) * 1000), {"session_id": self.session_id})
        _current_run_id.reset(token)
        model = state.metadata.get("model") or self.llm.info["model"]
        interrupt = None
        if state.status == "interrupted" and state.interrupt:
            interrupt = state.interrupt.get("value")
        # 修复: 取消标志命中时 state.status == "cancelled", 抛出 CancelledError
        # 让调用方(如 tasks.py 非流式路径)感知并终止, 不再返回正常结果
        if state.status == "cancelled":
            raise asyncio.CancelledError(f"任务被取消 (run {run_id[:8]})")
        return AgentResult(answer=state.answer, steps=state.steps, model=model, interrupt=interrupt)

    def _sync_from_state(self, state: AgentState) -> None:
        """从 checkpoint 恢复时,把状态同步回 Agent 的运行时字段。"""
        self._active_tools = state.active_tools
        self._schemas_cache = None
        if state.plan is not None:
            self._plan = TaskPlan.from_dict(state.plan)
            self._plan_dirty = False
        else:
            self._plan = None

    def _make_cancel_check(self):
        """构造取消检查回调: 若 self._cancel_flag 已 set 则返回 True。

        传入 Graph 后, 引擎在每个节点边界(含 arun/arun_stream 循环)
        检查该回调, 命中即把 state 标记为 cancelled 并终止, 不再执行后续节点。
        修复 tasks.py 取消形同虚设的问题: 用户 cancel() 后, 当前 LLM/工具调用
        结束后下个节点边界立即中止, 不必等到 max_minutes 超时。
        """
        flag = self._cancel_flag
        if flag is None:
            return None
        def _check() -> bool:
            return flag.is_set()
        return _check

    def _build_graph(self) -> Graph:
        # 每次调用创建新图,避免多次 run 复用同一图实例导致 state 残留
        # 动态计算初始步数:基于 task_config 中的 max_steps
        # 注意: 实际执行时会通过 _get_dynamic_max_steps() 动态调整
        base_max = self.max_steps
        # Graph.max_iters: 当 max_steps>0 时按 base+10 余量兜底(旧行为,测试依赖);
        # 当 max_steps==0(不限制步骤上限)时,Graph 层仍保留绝对兜底 500,
        # 真正的无上限语义由 _node_llm 的 _HARD_ROUND_CAP=500 兜底 + 三重收敛机制
        # (同参 3 连败 / replan 耗尽 / 连续空答案)保证不会实际跑满 500 轮。
        max_iters = (base_max + 10) if base_max > 0 else 500
        g = Graph(name="agent", checkpoint=self._checkpoint,
                  max_iters=max_iters,
                  cancel_check=self._make_cancel_check(),
                  batch_checkpoint=True,
                  batch_flush_interval=float(getattr(settings, "agent_checkpoint_batch_flush_interval", 2.0) or 2.0),
                  batch_max_buffer=int(getattr(settings, "agent_checkpoint_batch_max_buffer", 10) or 10))
        g.add_node(Node("prepare", self._node_prepare))
        g.add_node(Node("llm", self._node_llm))
        g.add_node(Node("tools", self._node_tools))
        g.add_node(Node("finalize", self._node_finalize))
        g.add_node(Node("giveup", self._node_giveup))
        g.add_node(Node("reflect", self._node_reflect))
        g.add_node(Node("verify", self._node_verify))
        g.set_entry("prepare")
        g.add_edge("prepare", "llm")
        g.add_edge("tools", "llm")
        g.add_conditional_edge("llm", self._route_llm)
        g.add_conditional_edge("finalize", self._route_finalize)
        g.add_conditional_edge("reflect", self._route_reflect)
        g.add_conditional_edge("verify", self._route_verify)
        return g

    async def _route_llm(self, state: AgentState) -> Optional[str]:
        """llm 节点之后的路由:给上限→giveup;有工具调用→tools;否则→finalize。"""
        if state.metadata.get("giveup"):
            logger.warning("[_route_llm] → giveup (reason=%s)",
                           state.metadata.get("giveup_reason") or "unknown")
            return "giveup"
        if state.metadata.get("tool_calls"):
            logger.info("[_route_llm] → tools (count=%d)", len(state.metadata["tool_calls"]))
            return "tools"
        logger.info("[_route_llm] → finalize")
        return "finalize"

    def _get_dynamic_max_steps(self, state: AgentState) -> int:
        """根据计划进度动态计算最大步数。

        返回 0 表示"不设上限"(用户把 agent_max_steps 配成 0):此时由收敛机制
        兜底防死循环(同参 3 连败 / replan 耗尽 / 连续空答案),不再按轮数强制 giveup。

        规则: 基础步数 + 剩余步骤数 * 每步预算, 但设硬上限防止膨胀失控。
        硬上限 = base_max + 30 (默认 40), 避免复杂计划导致单请求运行 50 步以上。
        """
        base_max = self.max_steps
        if base_max <= 0:
            return 0  # 未设上限:交给收敛机制
        # 动态扩张的硬上限: 基础值 + 最多 30 步额外预算
        hard_cap = base_max + 30
        if not self._plan:
            return base_max

        completed = sum(1 for s in self._plan.steps if s.status == "done")
        total = len(self._plan.steps)
        remaining = total - completed

        if remaining <= 0:
            return base_max  # 计划已完成,使用基础步数

        # 动态上限 = 基础步数 + 剩余步骤 × 每步预算(受 hard_cap 约束)
        steps_per_plan_step = 2  # 每个计划步骤分配 2 步(思考 + 执行)
        dynamic_max = min(base_max + remaining * steps_per_plan_step, hard_cap)
        return int(dynamic_max)

    async def _route_finalize(self, state: AgentState) -> Optional[str]:
        """finalize 之后:简单回答跳过反思/校验;否则按预算进入反思→校验。"""
        answer = state.answer or ""
        has_tool_steps = any(s.get("type") == "tool" for s in state.steps)
        # 提升日志级别以便更容易诊断问题
        logger.info("[_route_finalize] answer_len=%d has_tools=%s consecutive_empty=%d",
                     len(answer or ""), has_tool_steps, state.metadata.get("consecutive_empty_answers", 0))
        # 空答案:跟踪连续空答案次数，超过阈值则触发 giveup
        if not answer or not answer.strip():
            consecutive_empty = int(state.metadata.get("consecutive_empty_answers", 0)) + 1
            state.metadata["consecutive_empty_answers"] = consecutive_empty
            logger.warning("[_route_finalize] empty answer #%d, will retry", consecutive_empty)
            # 连续 2 次空答案即放弃(避免 LLM 只输出 thinking 标签导致无限循环)
            if consecutive_empty >= 2:
                state.metadata["giveup"] = True
                state.metadata["giveup_reason"] = "empty_answers"
                logger.warning("[_route_finalize] giving up after %d consecutive empty answers", consecutive_empty)
                return "giveup"
            return "llm"
        # 有答案时重置计数器
        state.metadata["consecutive_empty_answers"] = 0
        # 简单回答(<200字且无工具调用):直接停止,跳过反思和校验
        # 提高阈值避免任务过早结束(原 100 字过低,容易导致 LLM 用简短回答逃避复杂任务)
        if len(answer.strip()) < 200 and not has_tool_steps:
            return END
        if not self._reflect_enabled:
            return END
        rounds = int(state.metadata.get("reflect_rounds", 0))
        if rounds >= settings.agent_reflect_max_rounds + 1:
            return END
        return "reflect"

    async def _route_reflect(self, state: AgentState) -> Optional[str]:
        """reflect 之后:空答案直接停止;通过→verify;不合格且仍有评审预算→回 llm 重写。"""
        answer = state.answer or ""
        if len(answer.strip()) < 50:
            return END
        if state.metadata.get("reflect_rewrite"):
            rounds = int(state.metadata.get("reflect_rounds", 0))
            if rounds >= settings.agent_reflect_max_rounds + 1:
                return "verify"
            return "llm"
        return "verify"

    async def _route_verify(self, state: AgentState) -> Optional[str]:
        """verify 之后:校验内已内联重写(最多一次),随后停止。"""
        return END

    # ---- 图节点 ----

    async def _node_prepare(self, state: AgentState, graph: Graph) -> None:
        """路由/工具路由/规划/构造消息:只在入口执行一次。"""
        user_input = state.user_input
        history = state.metadata.get("history") or []

        # 输入护栏
        from app.core.guardrails import INPUT_BLOCK_MESSAGE, run_input_guardrails
        guard = run_input_guardrails(user_input)
        if guard.blocked:
            state.answer = INPUT_BLOCK_MESSAGE
            state.steps.append({"step": 0, "type": "guardrail", "content": guard.reasons()})
            state.metadata["guardrail_violations"] = [v.__dict__ for v in guard.violations]
            return END

        # 准备阶段逐段超时保护:旧实现对整个 _run_prepare 套一层
        # max(30, agent_llm_timeout) 的 wait_for —— 路由 LLM + 摘要 LLM + 规划 LLM +
        # 记忆检索叠加时,单段耗时正常(各 <30s)但总和经常超 60s,一旦整体超时
        # 就丢弃全部准备结果回退到 [{role:user}] 裸消息,且 UI 在等待期间
        # 看不到任何事件(前端表现为"发送后一直卡")。现改为逐段限时,
        # 任一段超时跳过该段继续,保留已成功段落的结果。
        llm_step_timeout = max(20, int(getattr(settings, "agent_llm_timeout", 60) or 60))

        # 1) 模型路由(快)
        try:
            await asyncio.wait_for(self._maybe_route(user_input), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("模型路由超时,沿用默认模型")
        except Exception as e:  # noqa: BLE001
            logger.warning("模型路由失败,沿用默认模型: %s", e)

        state.metadata["model"] = self.llm.info["model"]
        if state.metadata.get("stream"):
            state.emit({"type": "thinking", "content": f"准备中: 模型 {state.metadata['model']}", "phase": "prepare"})

        # 轻量交互模式: 跳过工具路由/任务规划/记忆注入(各自带 AGENT_LLM_TIMEOUT 的
        # 重 LLM 调用)。多轮简单问答只需主 LLM 应答,省掉这些准备段即可秒级响应,
        # 且 LLM 限流/故障时不会为每个问题空转满超时。
        # 注意: 多轮会话下历史随轮次增长, _prepare_history 会触发摘要 LLM 压缩(带
        # AGENT_LLM_TIMEOUT*2 的超时窗口)——这正是"多轮每问都卡"的元凶。fast_mode 下
        # 改为纯本地截断(保留最近 N 条 + 字符预算), 完全不调 LLM, 既不卡也不丢近期上下文。
        if self.fast_mode:
            cap = int(getattr(settings, "context_char_budget", 0) or 0)
            trimmed = self._local_trim_history(history, cap) if cap > 0 else history[-40:]
            state.metadata["history"] = trimmed
            state.messages = self._build_messages(user_input, state.metadata.get("history", []) or [])
            state.active_tools = None
            return

        # 2) 历史摘要压缩(可能含 1-2 次 LLM 调用,最耗时)
        try:
            state.metadata["history"] = await asyncio.wait_for(
                self._prepare_history(history), timeout=llm_step_timeout * 2
            )
            if state.metadata.get("stream") and history and len(state.metadata["history"]) < len(history):
                state.emit({"type": "thinking", "content": "历史上下文已压缩", "phase": "prepare"})
        except asyncio.TimeoutError:
            # 超时: 用本地截断替代原始历史, 避免超长上下文导致 context_length 错误
            cap = int(getattr(settings, "context_char_budget", 0) or 0)
            state.metadata["history"] = self._local_trim_history(history, cap) if cap > 0 else history[-40:]
            logger.warning("历史摘要压缩超时, 本地截断为 %d 条消息", len(state.metadata["history"]))

        # 3) 工具路由(可命中缓存,快;失败回退全量工具)
        # 逐段限时: 路由内部 LLM/embedding 卡死时不再阻塞整个准备阶段
        try:
            route_evt = await asyncio.wait_for(
                self._maybe_select_tools(user_input, state.steps, 0),
                timeout=llm_step_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("工具路由超时,回退全量工具集")
            route_evt = None
        except Exception as e:  # noqa: BLE001
            logger.warning("工具路由异常,回退全量工具集: %s", e)
            route_evt = None
        if route_evt:
            state.emit(route_evt)
        # 路由失败/超时时 _active_tools 仍为 None → _schemas 会回退全量工具集,
        # 这里兜底剔除付费媒体工具,避免纯文本问答被顺手执行出图/出视频。
        self._apply_media_tool_gate(user_input, state.metadata.get("history", []) or [])

        # 4) 任务规划(1 次 LLM 调用)
        try:
            plan_created = await asyncio.wait_for(
                self._plan_task(user_input, state.steps, 0), timeout=llm_step_timeout
            )
        except asyncio.TimeoutError:
            logger.warning("任务规划超时,跳过规划直接执行")
            plan_created = False
        if plan_created:
            state.emit({"type": "plan", "action": "create",
                        "plan": self._plan.to_dict() if self._plan else None})
        if self._plan is not None:
            state.plan = self._plan.to_dict()

        # 5) 记忆注入(30s 内部超时,检索失败返回空串)
        try:
            self._memory_note = await asyncio.wait_for(
                self._build_memory_note_async(user_input, state.metadata.get("history", []) or []),
                timeout=35,
            )
        except asyncio.TimeoutError:
            logger.warning("记忆注入超时,跳过记忆继续执行")
            self._memory_note = ""

        # 5b) 跨会话记忆检索:从固化记忆库召回相关知识
        if self._memory_consolidator and not self.fast_mode:
            try:
                recalled = recall_memory(user_input, user_id="default", top_k=3)
                if recalled:
                    consolidator = get_consolidator()
                    memory_note = consolidator.build_memory_note(recalled, max_chars=1500)
                    if memory_note:
                        self._memory_note = (self._memory_note + "\n\n" + memory_note).strip()
                        logger.debug("跨会话记忆注入: %d 条", len(recalled))
            except Exception as e:
                logger.warning("跨会话记忆检索失败: %s", e)

        # 6) 任务规划器检查点恢复:如果有关联的 run_id 且处于暂停状态,尝试恢复计划
        if self._task_planner and self._checkpoint and not self.fast_mode:
            try:
                # 检查是否有可恢复的计划检查点
                checkpoints = await self._checkpoint.list(limit=10, status="paused", session_id=self.session_id)
                for cp in checkpoints:
                    if cp.get("metadata", {}).get("plan_id"):
                        # 尝试恢复计划
                        restored_plan = await self._task_planner.resume_from_checkpoint(cp["run_id"])
                        if restored_plan:
                            self._plan = restored_plan
                            state.plan = self._plan.to_dict()
                            logger.info("从检查点恢复任务计划: run_id=%s", cp["run_id"][:8])
                            break
            except Exception as e:
                logger.debug("计划检查点恢复失败(忽略): %s", e)

        # 7) 构造消息(纯本地计算,不超时)
        state.messages = self._build_messages(user_input, state.metadata.get("history", []) or [])

        state.active_tools = self._active_tools

    def _apply_media_tool_gate(self, user_input: str, history: list[dict] | None = None) -> None:
        """付费媒体工具闸门(内核层兜底)。

        图像/视频生成工具一旦被调用就要真实扣费并长时间等待,而 security 层已不再
        对它们强制确认(security.py: generate_image/generate_video 不再强制确认)。
        只有当请求里出现明确的多模态生成意图(图片/视频/画/海报/…)时才放行;否则
        一律从候选集剔除 —— 关键是覆盖"路由失败/未启用 → _active_tools 为 None →
        _schemas 回退全量工具集"这条路径,否则模型仍能看到并随手调用它们。

        实测事故:"根据当前知识库,分析智能体"(纯文本问答)被模型同时调用了
        generate_image 与 generate_video,界面出现"AI 正在作画/正在生成视频"。
        """
        try:
            from app.core.smart_router import filter_paid_media_tools
        except Exception:  # noqa: BLE001
            return
        # 多轮续问(如上一轮出图后说"再来一张/换个风格")本轮文本未必带"图片"二字,
        # 因此把最近几轮用户原话一并作为意图判据,避免误杀合法的多轮出图请求。
        probe = user_input or ""
        try:
            recent = [
                (m.get("content") or "") for m in (history or [])[-6:]
                if isinstance(m, dict) and m.get("role") == "user"
            ]
            if recent:
                probe = probe + " " + " ".join(str(c) for c in recent)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._active_tools is None:
                names = [t.name for t in self.registry.list_sync()]
            else:
                names = list(self._active_tools)
            kept = filter_paid_media_tools(probe, names)
            if len(kept) != len(names):
                logger.info(
                    "无明确多模态生成意图,已剔除付费媒体工具: %s",
                    sorted(set(names) - set(kept)),
                )
                self._active_tools = kept
                self._schemas_cache = None
        except Exception:  # noqa: BLE001
            logger.debug("媒体工具闸门应用失败(忽略)", exc_info=True)

    async def _run_prepare(self, state, user_input, history, steps, step):
        """prepare 主体逻辑(兼容旧调用,内部委托 _node_prepare 的逐段流程)。"""
        await self._maybe_route(user_input)
        state.metadata["model"] = self.llm.info["model"]
        history = await self._prepare_history(history)
        state.metadata["history"] = history
        route_evt = await self._maybe_select_tools(user_input, steps, step)
        if route_evt:
            state.emit(route_evt)
        self._apply_media_tool_gate(user_input, history)

        if await self._plan_task(user_input, steps, step):
            state.emit({"type": "plan", "action": "create",
                        "plan": self._plan.to_dict() if self._plan else None})
        if self._plan is not None:
            state.plan = self._plan.to_dict()

        self._memory_note = await self._build_memory_note_async(user_input, history)
        state.messages = self._build_messages(user_input, history)
    async def _node_llm(self, state: AgentState, graph: Graph) -> None:
        """调用 LLM;把结果(tool_call_mode/content/tool_calls)暂存到 metadata 供路由与 tools 节点使用。"""
        rounds = int(state.metadata.get("llm_rounds", 0))
        # 动态步数计算:基于计划进度自动扩展
        dynamic_max = self._get_dynamic_max_steps(state)
        
        total_steps = len(state.steps)
        # dynamic_max == 0(agent_max_steps=0,即"不设步骤上限"):跳过强制 giveup,
        # 由收敛机制保证不死循环:
        #  - 同一工具连续失败 3 次 → _tool_failure 触发 giveup
        #  - 重新规划次数耗尽(agent_replan_max_times)
        #  - 连续空答案 → _route_finalize 触发 giveup
        #  - 非空答案且无工具调用 → 直接 END(任务完成,非 giveup)
        if dynamic_max > 0 and rounds >= dynamic_max and total_steps >= dynamic_max:
            state.metadata["giveup"] = True
            state.metadata["giveup_reason"] = "steps_exhausted"
            logger.warning("steps exhausted: rounds=%d, total_steps=%d, dynamic_max=%d",
                          rounds, total_steps, dynamic_max)
            return
        # 不限制模式(dynamic_max==0)的最后防线:轮数无上限时,若 LLM 反复调工具而任务
        # 无进展(工具不报错、答案一直空、replan 已耗尽)会无限跑。用绝对轮数上限 500
        # 兜底,保证"不设上限"语义在实际中仍有界。
        _HARD_ROUND_CAP = 500
        if dynamic_max == 0 and rounds >= _HARD_ROUND_CAP:
            state.metadata["giveup"] = True
            state.metadata["giveup_reason"] = "steps_exhausted"
            logger.warning("unlimited mode hard cap hit: rounds=%d", rounds)
            return

        state.metadata["llm_rounds"] = rounds + 1

        is_stream = state.metadata.get("stream", False)
        
        # 语义缓存:仅非流式、无工具调用的简单问答尝试缓存
        cache_key = None
        if not is_stream and self._cost_controller and rounds == 0:
            # 构建查询文本(最后一条用户消息)
            last_user = ""
            for msg in reversed(state.messages):
                if msg.get("role") == "user":
                    last_user = msg.get("content", "")
                    break
            if last_user:
                cached = self._cost_controller.get_cached_response(last_user)
                if cached:
                    logger.info("语义缓存命中,跳过 LLM 调用")
                    state.metadata["tool_call_mode"] = "text"
                    state.metadata["resp"] = cached
                    state.metadata["tool_calls"] = []
                    state.metadata["from_cache"] = True
                    return

        try:
            if is_stream:
                tool_call_mode, resp, tool_calls = await self._call_llm_stream(state.messages, state)
            else:
                tool_call_mode, resp, tool_calls = await self._call_llm(state.messages)
        except LLMError as e:
            err_msg = str(e).lower()
            # context_length 错误: 修剪消息后重试一次
            if is_context_length_error(err_msg):
                logger.warning("LLM context_length 错误(round=%d), 修剪消息后重试: %s", rounds, e)
                self._trim_state_messages(state)
                # 二次重试: 失败则放弃
                try:
                    if is_stream:
                        tool_call_mode, resp, tool_calls = await self._call_llm_stream(state.messages, state)
                    else:
                        tool_call_mode, resp, tool_calls = await self._call_llm(state.messages)
                except LLMError as e2:
                    logger.warning("LLM 重试仍失败(round=%d): %s", rounds, e2)
                    state.emit({"type": "error", "content": f"执行出错: {e2}"})
                    state.metadata["tool_call_mode"] = "text"
                    state.metadata["resp"] = ""
                    state.metadata["tool_calls"] = []
                    state.metadata["giveup"] = True
                    state.metadata["giveup_reason"] = "llm_error"
                    state.metadata["last_llm_error"] = str(e2)
                    return
            else:
                # 限流/临时错误(429/503/网络抖动):退避重试若干次(根因2 修复),
                # 避免"模型限流一次就中断整个任务"。仍失败再放弃,并向前端发明确状态。
                max_rate_retry = int(getattr(settings, "agent_llm_rate_retries", 3) or 3)
                is_rate = any(
                    k in err_msg
                    for k in ("429", "rate", "ratelimit", "rate_limit", "too many requests",
                              "quota", "限流", "负载", "busy", "503", "502", "504",
                              "timeout", "timed out", "connection", "连接", "gateway")
                )
                if is_rate:
                    backoff = float(getattr(settings, "agent_llm_retry_delay", 0.5) or 0.5)
                    last_rate_err: LLMError = e
                    for _i in range(max_rate_retry):
                        if is_stream:
                            state.emit({"type": "status",
                                        "content": f"模型暂时限流,正在重试({_i + 1}/{max_rate_retry})..."})
                        await asyncio.sleep(backoff * (2 ** _i))
                        try:
                            if is_stream:
                                tool_call_mode, resp, tool_calls = await self._call_llm_stream(state.messages, state)
                            else:
                                tool_call_mode, resp, tool_calls = await self._call_llm(state.messages)
                            break
                        except LLMError as e3:
                            logger.warning("LLM 限流重试(%d/%d)仍失败: %s", _i + 1, max_rate_retry, e3)
                            last_rate_err = e3
                    else:
                        # 重试全部耗尽:明确告知用户是限流/超时,而非笼统的"执行出错"
                        logger.warning("LLM 限流重试耗尽(round=%d): %s", rounds, last_rate_err)
                        state.emit({"type": "error", "content": f"模型暂时不可用(限流/超时),请稍后重试: {last_rate_err}"})
                        state.metadata["tool_call_mode"] = "text"
                        state.metadata["resp"] = ""
                        state.metadata["tool_calls"] = []
                        state.metadata["giveup"] = True
                        state.metadata["giveup_reason"] = "llm_rate_limited"
                        state.metadata["last_llm_error"] = str(last_rate_err)
                        return
                    # 重试成功 → 落到下方 metadata 赋值,继续正常路由
                else:
                    logger.warning("LLM 调用失败(round=%d): %s", rounds, e)
                    state.emit({"type": "error", "content": f"执行出错: {e}"})
                    state.metadata["tool_call_mode"] = "text"
                    state.metadata["resp"] = ""
                    state.metadata["tool_calls"] = []
                    state.metadata["giveup"] = True
                    state.metadata["giveup_reason"] = "llm_error"
                    state.metadata["last_llm_error"] = str(e)
                    return
        state.metadata["tool_call_mode"] = tool_call_mode
        state.metadata["resp"] = resp or ""
        state.metadata["tool_calls"] = [tc.to_dict() for tc in tool_calls]
        # 追踪 finish_reason:截断检测(d=length)用
        finish_reason = getattr(self.llm, "_finish_reason", "") or ""
        if finish_reason:
            state.metadata["finish_reason"] = finish_reason
        # 提升日志级别以便更容易诊断问题
        logger.info("[_node_llm] round=%d resp_len=%d tool_calls=%d mode=%s",
                    rounds, len(resp or ""), len(tool_calls), tool_call_mode)

        # 推理模型:先把思维链透出为 thinking 事件(与正文分离)
        reasoning = getattr(self.llm, "_reasoning", "") or ""
        if reasoning:
            state.metadata["reasoning"] = reasoning
            # 流式模式下 thinking 已在 _call_llm_stream 里随 chunk 逐段 emit 过,
            # 这里再发一次整段会让思考内容在界面上显示两遍。
            if not is_stream:
                state.emit({"type": "thinking", "content": reasoning, "reasoning": True})

        if tool_call_mode == "native" and tool_calls:
            state.messages.append({
                "role": "assistant",
                "content": resp or "",
                "tool_calls": [tc.to_openai() for tc in tool_calls],
            })
            # 注意: 流式模式下 resp 的 token 已通过 _call_llm_stream 的 token 事件
            # 发送到 answer 区, 此处不再重复发射为 thinking 事件, 避免内容显示两遍
        elif tool_calls:
            state.messages.append({"role": "assistant", "content": resp or ""})
        # 无工具调用:最终回答由 finalize 节点处理
        
        # 记录用量并存入语义缓存(仅首轮、无工具调用、非流式)
        if self._cost_controller and rounds == 0 and not tool_calls and not is_stream:
            try:
                # 从 LLM 客户端获取 token 用量
                usage = getattr(self.llm, "_last_usage", None)
                if usage:
                    input_tokens = usage.get("prompt_tokens", 0)
                    output_tokens = usage.get("completion_tokens", 0)
                    self._cost_controller.end_request(input_tokens, output_tokens, {"round": rounds})
                # 缓存回答
                last_user = ""
                for msg in reversed(state.messages):
                    if msg.get("role") == "user":
                        last_user = msg.get("content", "")
                        break
                if last_user and resp:
                    self._cost_controller.cache_response(last_user, resp)
            except Exception as e:
                logger.debug("成本记录/缓存失败: %s", e)

    async def _node_tools(self, state: AgentState, graph: Graph) -> Optional[str]:
        """执行一批工具调用(并行)、回喂结果、更新计划、注入反思。终端工具成功则结束。

        interrupt 式确认流开启时:先预扫描工具调用,凡需要人工确认且未批准的,
        创建确认请求并 graph.interrupt(暂停落盘返回控制权),不执行任何工具;
        用户批准后 resume 重入本节点,已批准的工具放行执行。
        """
        tool_call_mode = state.metadata.get("tool_call_mode", "text")
        tool_calls = [
            ToolCall(id=tc.get("id", ""), name=tc.get("name", ""), arguments=tc.get("arguments", "{}"))
            for tc in state.metadata.get("tool_calls", []) or []
        ]
        step = int(state.metadata.get("llm_rounds", 1)) - 1

        # ---- interrupt 式确认:预扫描 ----
        # 由 _scan_pending_confirmations 依据各机制(agent_tool_confirm / decide)决定是否需确认
        if settings.agent_confirm_interrupt and tool_calls:
            pending = self._scan_pending_confirmations(tool_calls)
            if pending:
                resuming = bool(state.interrupt and state.interrupt.get("resume") is not None)
                if not resuming:
                    # 首次遇到需确认的工具:创建请求 -> 发事件 -> 中断暂停
                    requests = self._create_confirm_requests(pending)
                    state.emit({"type": "confirmation", "requests": requests})
                    graph.interrupt(state, {
                        "type": "confirmation",
                        "requests": requests,
                        "run_id": state.run_id,
                    })
                    return None
                # 恢复:按用户决定更新运行时批准集合,未批准的将被硬拦截
                self._runtime_approved = set()
                for tc, _decision in pending:
                    if self._is_tool_approved_by_resume(tc, state):
                        self._runtime_approved.add(tc.name)

        # 工具开始执行:先逐工具发 tool_call_start,再批量执行
        state.emit({
            "type": "tool_call_start",
            "tools": [
                {"name": tc.name, "arguments": tc.arguments}
                for tc in tool_calls
            ],
        })
        # 也发送逐工具的 tool_start 事件(供旧的 ExecTrace 兼容)
        for tc in tool_calls:
            state.emit({"type": "tool_start", "name": tc.name, "arguments": tc.arguments})
        batch = await self._execute_batch(tool_calls, state.steps, step)
        # 代码级自愈:对失败的代码类工具自动执行"分析-修复-重跑"闭环
        batch = await self._maybe_self_heal(state, batch, step)
        tool_seq = int(state.metadata.get("tool_seq", 0))
        for i, (tc, result_str, _is_err) in enumerate(batch):
            step_id = f"{state.run_id}_t{tool_seq + i}"
            if tool_call_mode == "native":
                state.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _clip(result_str, settings.message_char_cap),
                })
            else:
                state.messages.append({
                    "role": "user",
                    "content": f"工具 {tc.name} 执行结果如下,请基于结果继续:\n{_clip(result_str, settings.message_char_cap)}",
                })
            # 发送工具执行完成事件,携带 step_id 供前端去重(全局单调递增,跨批次唯一)
            state.emit({
                "type": "tool",
                "name": tc.name,
                "arguments": tc.arguments,
                "result": result_str,
                "step_id": step_id,
            })
        state.metadata["tool_seq"] = tool_seq + len(batch)

        # 防止 state.messages 无限增长导致 context_length 错误
        self._trim_state_messages(state)

        evt = self._flush_plan(state.steps, step)
        if evt:
            state.emit(evt)
        state.plan = self._plan.to_dict() if self._plan else None

        terminal = self._append_reflection(state.messages, batch, state._failures)
        if terminal:
            answer = f"已通过工具 {terminal} 完成任务,结果已展示。"
            state.answer = answer
            state.steps.append({"step": step + 1, "type": "answer", "content": answer})
            if state.metadata.get("stream"):
                for ch in answer:
                    state.emit({"type": "token", "content": ch})
            return END

        # 计划失败后重新规划:同工具连续失败达阈值时,基于失败原因生成修订版计划
        if await self._maybe_replan(state, batch):
            state.emit({"type": "plan", "action": "replan",
                        "plan": self._plan.to_dict() if self._plan else None})
        return None

    async def _try_continue_truncated(self, state: AgentState, graph: Graph) -> str:
        """答案被 max_tokens 截断时自动续调 LLM 补齐(根因1 修复)。

        触发:finish_reason=='length' 或启发式 _answer_looks_truncated 命中。
        仅对最终文本答案(tool_call_mode==text 且无待执行工具调用)生效。
        续写衔接:把当前答案作为上下文追加 _CONTINUE_PROMPT 让模型从断处继续,
        并用 _strip_continuation_overlap 去除续写内容里与前文重叠的前缀。
        流式模式会补发续写 token,非流式仅追加到 answer。
        """
        answer = state.metadata.get("resp", "") or ""
        # 仅最终文本答案需要续写;本轮还有待执行的工具调用时不续写。
        #
        # 注意:不能按 tool_call_mode == 'native' 排除!native 只表示"本轮走的是
        # function-calling 协议",当最终轮返回的是纯文本(没有 tool_calls)时
        # mode 依然残留为 native。旧判断 `mode not in (None, 'text')` 因此会把
        # native 模式下**所有**截断答案都放过 —— 实测 run 9fc46416:
        # tool_call_mode=native / tool_calls=0 / finish_reason=stop / resp 以"；"
        # 结尾(明显截断),却因这个判断直接 return,答案停在 2.3 节中途。
        # 判断依据只能是"本轮有没有待执行工具调用"。
        if state.metadata.get("tool_calls"):
            return answer
        if not answer:
            return answer
        truncated, reason = _answer_looks_truncated(answer, max_output_tokens=self.max_output_tokens)
        fr = state.metadata.get("finish_reason", "")
        if not truncated and fr != "length":
            return answer
        max_cont = int(getattr(settings, "agent_answer_continue_max", 0) or 0)
        if max_cont <= 0:
            logger.warning("[续写] 检测到答案截断(%s/finish_reason=%s)但未启用续写(agent_answer_continue_max=0)", reason, fr)
            return answer
        logger.info("[续写] 检测到答案截断(%s/finish_reason=%s),开始自动续写(最多 %d 次)", reason, fr, max_cont)
        is_stream = state.metadata.get("stream", False)
        for i in range(max_cont):
            cont_messages = list(state.messages) + [
                {"role": "assistant", "content": answer},
                {"role": "user", "content": _CONTINUE_PROMPT},
            ]
            try:
                # 注意解包顺序:_call_llm*/_call_llm_stream 返回 (mode, content, tool_calls)。
                # 旧代码写成 `cont, _mode, _calls = ...`,把 mode 字符串("text"/"native")
                # 当成了续写内容拼到答案尾部 —— 续写等于没有生效还污染了答案。
                if is_stream:
                    _mode, cont, _calls = await self._call_llm_stream(cont_messages, state)
                else:
                    _mode, cont, _calls = await self._call_llm(cont_messages)
            except LLMError as e:
                logger.warning("[续写] 第 %d 次续写调用失败: %s", i + 1, e)
                break
            if not cont or not cont.strip():
                break
            cont = _strip_continuation_overlap(answer, cont)
            answer = answer + cont
            state.metadata["resp"] = answer
            state.metadata["answer_continue_count"] = i + 1
            # 续写 token 已由下方 _call_llm_stream 在调用时实时 emit,
            # 这里不再手动 emit,避免重复渲染(非流式则随最终 answer 在 done 时渲染)。
            still, r2 = _answer_looks_truncated(answer, max_output_tokens=self.max_output_tokens)
            if not still:
                logger.info("[续写] 第 %d 次续写后答案完整,结束", i + 1)
                break
            logger.info("[续写] 第 %d 次续写后仍截断(%s),继续", i + 1, r2)
        return answer

    async def _rewrite_degenerate_answer(self, state: AgentState) -> str:
        """退化答案重写:明确告知模型上一轮无效,要求基于已有上下文给出实质回答。

        流式模式先发 answer_reset(前端会清空正文缓冲、复位思考状态、保留旧答案
        避免闪烁),随后重写的 token 正常逐字发出并覆盖正文。
        """
        is_stream = bool(state.metadata.get("stream"))
        state.metadata["degenerate_rewrite"] = True
        if is_stream:
            state.emit({"type": "answer_reset"})
        retry_messages = list(state.messages) + [
            {"role": "assistant", "content": "(上一条回复无效:只有重复的过渡语,没有实质内容)"},
            {"role": "user", "content": (
                "你上一条回复只有重复的过渡句(如「好的,我继续分析...」),没有任何实质内容,不合格。"
                "请直接给出对用户问题的正式回答:基于上文的工具结果与已有分析,"
                "输出具体内容(结论/问题清单/代码/步骤),不要再说过渡语。"
            )},
        ]
        try:
            # 解包顺序同 _try_continue_truncated:(mode, content, tool_calls)
            if is_stream:
                _mode, cont, _calls = await self._call_llm_stream(retry_messages, state)
            else:
                _mode, cont, _calls = await self._call_llm(retry_messages)
        except LLMError as e:
            logger.warning("[_node_finalize] 退化重写调用失败: %s", e)
            return (
                "上一轮回复无效(只有重复的过渡语),已拦截。请重试一次;"
                "若反复出现,请换个问法或新建会话。"
            )
        cont = (cont or "").strip()
        if not cont or _looks_like_degenerate_repeat(cont):
            logger.warning("[_node_finalize] 退化重写后仍不合格(len=%s),用拦截提示兜底", len(cont))
            return (
                "上一轮回复无效(只有重复的过渡语),已拦截。请重试一次;"
                "若反复出现,请换个问法或新建会话。"
            )
        return cont

    async def _node_finalize(self, state: AgentState, graph: Graph) -> None:
        """最终回答:更新计划、产出答案、自动沉淀记忆。流式模式逐 token 输出。"""
        logger.info("[_node_finalize] START resp_len=%d", len(state.metadata.get("resp") or ""))
        step = int(state.metadata.get("llm_rounds", 1)) - 1
        evt = self._finalize_plan(state.steps, step)
        if evt:
            state.emit(evt)
        state.plan = self._plan.to_dict() if self._plan else None

        # 统一从 _node_llm 已收集的响应开始;流式已逐 token 发出,非流式尚未生成完整回答
        answer = state.metadata.get("resp", "") or ""

        # 空答案兜底:如果 resp 为空，尝试重新生成
        if not answer:
            # 先尝试 reasoning 作为兜底(agnes 等模型可能把所有输出放在 reasoning_content 里)
            answer = state.metadata.get("reasoning", "") or ""
            if answer:
                logger.info("[_node_finalize] resp 为空,使用 reasoning 作为 answer (len=%d)", len(answer))
            # 如果仍然为空，非流式模式下补一次 LLM 调用（带超时保护）
            if not answer and not state.metadata.get("stream"):
                try:
                    answer = await asyncio.wait_for(
                        self.llm.chat(state.messages, temperature=self.temperature),
                        timeout=float(getattr(settings, "agent_llm_timeout", 60) or 60)
                    )
                except asyncio.TimeoutError:
                    logger.warning("finalize 阶段 LLM 补调用超时")

        # 截断自动续写(根因1 修复):答案被 max_tokens 截断时,自动续调 LLM 补齐
        state.metadata["resp"] = answer
        answer = await self._try_continue_truncated(state, graph)

        # 退化输出护栏:同一句过渡语重复 N 遍的"口水话答案"不落库、不展示,
        # 直接重写一次(根治在 _build_messages 的时间戳排序,这里是兜底)
        if _looks_like_degenerate_repeat(answer):
            logger.warning("[_node_finalize] 检出退化重复答案(len=%d),重写一次", len(answer))
            state.metadata["resp"] = answer
            answer = await self._rewrite_degenerate_answer(state)
            state.metadata["resp"] = answer

        # 输出 token 配额硬控制
        if self.max_output_tokens > 0 and answer:
            full, truncated = _truncate_to_token_budget(answer, self.max_output_tokens, self.llm)
            if truncated:
                answer = full + "\n...[已达输出配额上限,回答已截断]"
                if state.metadata.get("stream"):
                    state.emit({"type": "thinking",
                                "content": f"[已达输出配额上限 {self.max_output_tokens} token,回答已截断]"})

        state.answer = answer
        # 剥离 <thinking>...</thinking> 标签,确保存储的答案不含思考内容
        if state.answer:
            # 丢弃完整的 thinking 块（思考过程），保留正文
            state.answer = re.sub(r'<thinking[^>]*>[\s\S]*?</thinking>', '', state.answer, flags=re.IGNORECASE)
            # 清理可能残留的孤立标签
            state.answer = re.sub(r'<thinking[^>]*>', '', state.answer, flags=re.IGNORECASE)
            state.answer = re.sub(r'</thinking>', '', state.answer, flags=re.IGNORECASE)
            # 规范化空白
            state.answer = state.answer.strip()
            state.answer = re.sub(r'[ \t]+', ' ', state.answer)
            state.answer = re.sub(r'\n{3,}', '\n\n', state.answer)

        # 答案去噪:剥掉开场自述与明文思考段(详见 _clean_answer_noise 注释)。
        # 被剥的内容不丢 —— 转成 thinking 事件透出,用户仍能在思考面板看到;
        # 流式下正文已渲染,用 guardrail_overwrite 让前端换成清洗后的正文。
        if state.answer:
            cleaned, noise = _clean_answer_noise(state.answer)
            if noise and cleaned != state.answer and len(cleaned.strip()) >= 2:
                logger.info("[_node_finalize] 答案去噪: 剥离 %d 字自述/思考段(原 %d 字)",
                            len(noise), len(state.answer))
                state.metadata["answer_noise"] = noise
                state.answer = cleaned
                if state.metadata.get("stream"):
                    state.emit({"type": "thinking", "content": noise, "phase": "noise"})
                    state.emit({
                        "type": "guardrail_overwrite",
                        "content": cleaned,
                        "reasons": ["answer_noise"],
                        "stream_replaced": True,
                    })

        # 拦截"没解析成功的工具调用块"被当成最终答案(实证见 _looks_like_unparsed_tool_call)
        if _looks_like_unparsed_tool_call(state.answer):
            logger.warning("[_node_finalize] 答案为未解析的工具调用块,已替换为提示: %r",
                           state.answer[:200])
            state.metadata["unparsed_tool_call"] = True
            replacement = (
                "本轮中止：模型输出的工具调用没有带上可执行参数,已拦截以免显示成原始标记。\n\n"
                "建议:重试一次;若反复出现,把任务拆成更小的步骤描述。"
            )
            state.answer = replacement
            if state.metadata.get("stream"):
                # token 已逐字发出:复用 guardrail_overwrite 让前端覆盖已渲染正文
                state.emit({
                    "type": "guardrail_overwrite",
                    "content": replacement,
                    "reasons": ["unparsed_tool_call"],
                    "stream_replaced": True,
                })

        state.steps.append({"step": step + 1, "type": "answer", "content": state.answer})

        # 输出护栏:敏感信息/提示词泄露检测(流式模式也强制拦截, 通过 guardrail_overwrite 事件更正)
        from app.core.guardrails import run_output_guardrails

        out_guard = run_output_guardrails(answer)
        if out_guard.blocked:
            sanitized = next((v.sanitized for v in out_guard.violations if v.sanitized), None)
            replacement = sanitized or "回答包含敏感信息, 已由安全护栏拦截并脱敏。"
            state.steps.append({"step": step + 1, "type": "guardrail", "content": out_guard.reasons()})
            state.metadata["guardrail_violations"] = [v.__dict__ for v in out_guard.violations]
            state.answer = replacement
            if state.metadata.get("stream"):
                # 流式场景: token 已逐字发出, 发出 guardrail_overwrite 事件告知前端用替换文本
                # 覆盖已渲染内容(前端收到此事件时应清除正文并替换, 或追加拦截提示)
                state.emit({
                    "type": "guardrail_overwrite",
                    "content": replacement,
                    "reasons": out_guard.reasons(),
                    "stream_replaced": True,
                })
            # 非流式: answer 直接替换, 未发出任何 token, 护栏完全生效

        await self._maybe_auto_memory(state.user_input, state.answer, state.metadata.get("history", []) or [])

        # 成本预算实时状态注入上下文(供下一轮感知)
        if self._cost_controller:
            try:
                task_id = getattr(self, "_current_task_id", "")
                cost_note = self._cost_controller.build_cost_note(self.session_id, task_id)
                if cost_note:
                    # 注入到系统提示或作为 thinking 事件透出
                    state.emit({"type": "thinking", "content": cost_note, "phase": "cost"})
            except Exception as e:
                logger.debug("成本注入失败: %s", e)

        # 记忆固化:每轮对话后提取关键决策/代码片段存入 SQLite + 向量索引
        if self._memory_consolidator and state.metadata.get("history"):
            try:
                history = state.metadata.get("history", [])
                await consolidate_memory(self.session_id, history, user_id="default")
                logger.debug("记忆固化完成: session=%s", self.session_id[:8])
            except Exception as e:
                logger.warning("记忆固化失败: %s", e)

    async def _node_giveup(self, state: AgentState, graph: Graph) -> None:
        """达到步数上限/调用失败等:给出兜底回答,附带失败细节帮助用户重新尝试。

        旧版一律报"已尝试 {max_steps} 步",即使实际只跑了 1 轮就因 LLM 调用失败
        而中止——对用户是误导。现按 metadata.giveup_reason 区分真实原因,并展示
        实际执行轮数。
        """
        reason = state.metadata.get("giveup_reason", "")
        actual_rounds = int(state.metadata.get("llm_rounds", 0) or 0)
        giveup_at = self.max_steps if self.max_steps > 0 else actual_rounds or 1

        # 从对话历史中提取最后一条助手消息,了解最后一次尝试
        last_assistant = ""
        for msg in reversed(state.messages):
            if msg.get("role") == "assistant":
                last_assistant = (msg.get("content") or "")[:300]
                break

        if reason == "llm_error":
            err = str(state.metadata.get("last_llm_error", ""))[:300]
            answer = (
                "任务中止：模型调用出错。\n\n"
                f"详情: {err or '未知错误'}\n\n"
                "建议：点击重试；若持续出现，请检查模型服务状态或网络连接。"
            )
            state.answer = answer
            state.steps.append({"step": actual_rounds or 1, "type": "error",
                                "content": f"LLM 调用失败: {err[:200]}"})
            logger.info("giveup(llm_error): input=%r rounds=%d err=%s",
                        state.user_input[:100], actual_rounds, err[:200])
            if state.metadata.get("stream"):
                for ch in answer:
                    state.emit({"type": "token", "content": ch})
            return

        if reason == "empty_answers":
            answer = (
                f"任务中止：模型连续多次返回空回复（共执行 {actual_rounds or 1} 轮）。\n\n"
                "建议：换个说法重试，或把任务拆成更小的步骤描述。"
            )
            state.answer = answer
            state.steps.append({"step": actual_rounds or 1, "type": "error",
                                "content": f"连续空回复达到上限, 实际执行 {actual_rounds or 1} 轮"})
            logger.info("giveup(empty_answers): input=%r rounds=%d",
                        state.user_input[:100], actual_rounds)
            if state.metadata.get("stream"):
                for ch in answer:
                    state.emit({"type": "token", "content": ch})
            return

        # steps_exhausted / tool_failures / 其它:保留"步数"口径,但用实际轮数补充说明
        # 从 _failures 提取失败工具摘要
        failed_tools = []
        for name, count in sorted(state._failures.items(), key=lambda x: -x[1])[:3]:
            failed_tools.append(f"{name}(失败{count}次)")
        failed_str = "、".join(failed_tools) if failed_tools else "无"

        # 从 messages 提取最近的错误/反思消息作为线索
        recent_errors = []
        for msg in reversed(state.messages[-_ERROR_RECENT_MESSAGE_COUNT:]):
            content = msg.get("content", "") or ""
            if msg.get("role") == "assistant" and ("失败" in content or "error" in content.lower()
                                                   or "无法" in content):
                recent_errors.append(content[:150])
            elif msg.get("role") == "tool" and "error" in content.lower():
                recent_errors.append(f"[{msg.get('name', '?')}] {content[:150]}")
            if len(recent_errors) >= 3:
                break

        detail_parts = []
        if failed_tools:
            detail_parts.append(f"失败工具: {failed_str}")
        if last_assistant:
            detail_parts.append(f"最后尝试: {last_assistant[:100]}")
        if recent_errors:
            detail_parts.append("错误线索: " + " | ".join(recent_errors))
        if actual_rounds and actual_rounds < giveup_at:
            detail_parts.append(f"实际执行: {actual_rounds} 轮(上限 {giveup_at} 步)")
        detail = "; ".join(detail_parts) if detail_parts else "无额外信息"

        answer = (
            f"无法完成任务：已尝试 {giveup_at} 步，仍未能得到正确结果。\n\n"
            f"详情: {detail}\n\n"
            "建议：简化需求、提供更多背景信息，或分步骤描述任务。"
        )
        state.answer = answer
        state.steps.append({"step": giveup_at, "type": "error",
                            "content": f"达到最大步数限制({giveup_at}步)，失败工具: {failed_str}"})
        logger.info("giveup(reason=%s): input=%r steps=%d failures=%s last_assistant=%r",
                     reason or "unknown", state.user_input[:100], giveup_at, failed_str, last_assistant[:100])
        if state.metadata.get("stream"):
            for ch in answer:
                state.emit({"type": "token", "content": ch})

    def _critic(self):
        """返回评审模型:配置了独立 critic 则用它,否则复用执行模型。"""
        return self.critic_llm or self.llm

    # ---- 多模型协作:ensemble(并行做 → 评审选优) ----

    def _ensemble_specs(self) -> list[tuple[str, str]]:
        """解析参与 ensemble 的模型规格列表 [(provider, model), ...]。

        来源优先级:请求级 ensemble_models > 全局 agent_ensemble_models。
        每项形如 "provider:model" 或 "provider"(用该 provider 默认模型)。
        无法解析时回退为当前执行模型。最多 agent_ensemble_max 个。
        """
        raw = list(self.ensemble_models)
        if not raw:
            raw = [s.strip() for s in settings.agent_ensemble_models.split(",") if s.strip()]
        specs: list[tuple[str, str]] = []
        for item in raw:
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                provider, model = item.split(":", 1)
            else:
                provider, model = item, ""
            specs.append((provider, model))
        if not specs:
            info = self.llm.info
            specs = [(info.get("provider", ""), info.get("model", ""))]
        return specs[: max(1, int(settings.agent_ensemble_max))]

    async def _run_ensemble(self, user_input: str, history: list[dict]) -> AgentResult:
        """多个模型并行做同一任务,再用评审模型对全部回答打分选优。

        每个模型用独立子 Agent(单模型模式)执行同一任务,结果合并后
        交给评审模型(critic 或主模型)按质量排序,返回最佳回答。
        """
        from app.core.llm import LLMClient as _L

        specs = self._ensemble_specs()
        agents: list[tuple[tuple[str, str], Agent]] = []
        for spec in specs:
            provider, model = spec
            try:
                llm = _L(provider, model or None)
            except Exception as e:  # noqa: BLE001
                logger.warning("子 Agent LLM 创建失败，跳过 provider=%s model=%s: %s", provider, model, e)
                continue
            sub = Agent(
                llm=llm,
                registry=self.registry,
                max_steps=self.max_steps,
                system_prompt=self.system_prompt,
                session_id=self.session_id,
                mode="single",  # 子模型只做不评,避免递归
                critic_provider=(self.critic_llm.provider if self.critic_llm else ""),
                critic_model=(self.critic_llm.model if self.critic_llm else ""),
                project_path=self.project_path,
            )
            sub._active_tools = self._active_tools
            sub._schemas_cache = None
            agents.append((spec, sub))

        if not agents:
            return AgentResult(answer="ensemble 模式没有可用模型。", steps=[], model=self.llm.info["model"])

        try:
            results = await asyncio.gather(*[a.run(user_input, history) for _, a in agents])
        except Exception as e:  # noqa: BLE001
            return AgentResult(answer=f"ensemble 执行失败: {e}", steps=[], model=self.llm.info["model"])

        # 各子 Agent 的轨迹合并为 steps,便于可观测
        # 用配对列表(spec, agent, result)而非 zip(specs, results),避免创建失败跳过导致的下标错位
        paired = [(spec, agent, res) for (spec, agent), res in zip(agents, results)]
        steps: list[dict] = []
        for i, (spec, agent, res) in enumerate(paired):
            steps.append({
                "step": i + 1,
                "type": "ensemble_member",
                "provider": spec[0],
                "model": spec[1] or res.model,
                "answer": res.answer,
                "member_steps": res.steps,
            })
        steps.append({"step": len(paired) + 1, "type": "ensemble_judge"})

        judge_results = [res for _spec, _agent, res in paired]
        best = await self._judge_ensemble(user_input, judge_results)
        if best is not None:
            return AgentResult(answer=best.answer, steps=steps, model=best.model)
        # 评审失败:退回第一个有效结果
        return AgentResult(answer=judge_results[0].answer, steps=steps, model=judge_results[0].model)

    async def _judge_ensemble(self, user_input: str, results: list[AgentResult]) -> Optional[AgentResult]:
        """对全部候选回答打分,返回最佳结果;失败返回 None(退回第一候选)。"""
        if not results:
            return None
        if len(results) == 1:
            return results[0]
        try:
            candidate_texts = "\n\n".join(
                f"[候选 {i}]({r.model}):\n{r.answer}" for i, r in enumerate(results)
            )
            schema = {
                "type": "object",
                "properties": {
                    "winner": {"type": "integer", "description": "最佳回答的候选索引"},
                    "scores": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "index": {"type": "integer"},
                                "score": {"type": "number", "description": "1-10 质量分"},
                            },
                            "required": ["index", "score"],
                        },
                    },
                    "reason": {"type": "string", "description": "选优理由"},
                },
                "required": ["winner", "scores", "reason"],
            }

            def _validator(data):
                if not isinstance(data, dict):
                    return False, "结果不是对象"
                if not isinstance(data.get("winner"), int):
                    return False, "winner 必须是整数"
                if not isinstance(data.get("scores"), list):
                    return False, "scores 必须是数组"
                return True, None

            prompt = (
                f"用户请求:\n{user_input}\n\n"
                f"下面是多个模型对同一请求的回答。请从准确性、完整性、清晰度三个维度打分(1-10),"
                f"并选出最佳的一个(返回它的索引 winner)。\n\n{candidate_texts}"
            )
            verdict = await self._critic().chat_json_schema(
                [
                    {"role": "system", "content": "你是严格的评审者,从多个候选回答中选出质量最佳的一个。"},
                    {"role": "user", "content": prompt},
                ],
                schema,
                temperature=0.2,
                validator=_validator,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("ensemble 评审失败，随机选取第一个结果: %s", e)
            return None
        winner = int(verdict.get("winner", 0))
        if not (0 <= winner < len(results)):
            winner = 0
        return results[winner]

    # ---- 多智能体编排(Orchestration 层):Sequential / Hierarchical / Consensual ----
    #
    # 与"执行模式"(single/auto/review/ensemble)正交:执行模式决定"单个智能体怎么干",
    # 编排模式决定"多个智能体怎么协作"。二者通过 _make_worker 桥接——
    # 每个子任务都由一个新的执行模式 Agent 完成,因此层级叶子也可以是
    # ensemble / review 等执行模式(天然递归)。

    def _make_worker(self, mode_name: str, active_tools: Optional[list[str]] = None) -> "Agent":
        """为某个执行模式创建一个子智能体(执行体)。"""
        sub = Agent(
            llm=self.llm,
            registry=self.registry,
            session_id=self.session_id,
            mode=mode_name,
            critic_provider=(self.critic_llm.provider if self.critic_llm else ""),
            critic_model=(self.critic_llm.model if self.critic_llm else ""),
            ensemble_models=self.ensemble_models,
            max_output_tokens=self.max_output_tokens,
            project_path=self.project_path,
        )
        # 子 Agent 继承父 Agent 的工具路由结果,避免调用无关工具
        sub._active_tools = active_tools or self._active_tools
        sub._schemas_cache = None
        return sub

    async def _register_orch_agents(self, orch, modes: list[str] | None = None) -> None:
        """把执行模式注册为编排器的"智能体"(幂等),供任务分解时按需分配。

        modes: 限定只注册这些执行模式(来自 UI 的"候选执行体清单");为 None 时注册全部。
        """
        from app.core.orchestrator import AgentCapability

        roster = {
            "auto": "通用执行:自动判断是否调用工具完成任务",
            "single": "单模型直接执行,不做额外评审,速度快",
            "review": "执行后由独立评审模型核查质量,适合校验与总结",
            "ensemble": "多模型并行生成再由评审模型择优,适合高可靠任务",
        }
        tool_names = [t.name for t in self.registry.list_sync()]
        names = modes if modes else list(roster.keys())
        for name in names:
            if name not in roster:
                continue
            if name not in orch._agents:
                orch.register_agent(name, AgentCapability(
                    name=name, description=roster[name],
                    tools=list(tool_names), expertise=[name],
                ))

    def _consensual_candidates(self) -> list[str]:
        """consensual 模式下的并行候选执行模式清单。优先用 UI 配置的 orch_candidates。"""
        if self.orch_candidates:
            return [c for c in self.orch_candidates if c in ("single", "auto", "review", "ensemble")]
        if self.ensemble_models:
            return ["single", "auto", "ensemble"]
        return ["single", "auto", "review"]

    async def _run_orchestrated(self, user_input: str, history: list[dict]) -> AgentResult:
        """非流式入口:驱动编排流并聚合为 AgentResult。"""
        answer: list[str] = []
        steps: list[dict] = []
        async for ev in self._run_orchestrated_stream(user_input, history or []):
            if ev["type"] == "token":
                answer.append(ev["content"])
            elif ev["type"] in ("tool", "thinking", "plan", "orchestration"):
                steps.append(ev)
        return AgentResult(answer="".join(answer), steps=steps, model=self.llm.info["model"])

    async def _run_orchestrated_stream(self, user_input: str, history: list[dict]):
        """流式编排主循环:按 flow 类型分发,逐 token 转发子智能体输出。

        yield 事件:
          {"type": "orchestration", "phase": "start"|"task_start"|"task_done"|...}
          {"type": "token"|"thinking"|"tool"|"plan"}  (来自子智能体,原样转发)
        """
        flow = self.mode
        yield {"type": "orchestration", "phase": "start", "flow": flow,
               "message": f"启动 {flow} 多智能体编排"}

        if flow == "consensual":
            async for ev in self._orch_consensual(user_input, history or []):
                yield ev
            yield {"type": "orchestration", "phase": "end", "flow": flow}
            return

        if flow == "sequential":
            # 顺序多阶段:默认 auto 探索 + review 核查;若 UI 配置了候选执行体清单,
            # 则按清单顺序串行执行(至少 2 个阶段才构成"顺序编排")。
            pipeline = self.orch_candidates if len(self.orch_candidates) >= 2 else ["auto", "review"]
            context = ""
            for i, worker_mode in enumerate(pipeline):
                yield {"type": "orchestration", "phase": "task_start",
                       "index": i, "total": len(pipeline), "agent": worker_mode,
                       "task": "整体任务(顺序两阶段)"}
                prompt = user_input
                if context:
                    prompt = (f"前序阶段已完成的工作如下,请在其基础上继续完善:\n{context}\n\n"
                              f"用户原始请求: {user_input}")
                sink: list[str] = []
                async for ev in self._orch_run_worker(worker_mode, prompt, history, i, _sink=sink):
                    yield ev
                context += f"[阶段 {i + 1} · {worker_mode} 产出]\n{''.join(sink)}\n"
            yield {"type": "orchestration", "phase": "end", "flow": flow}
            return

        # hierarchical: 任务分解 -> 拓扑排序 -> 依赖顺序执行 -> 合成
        from app.core.orchestrator import get_orchestrator, SubTask

        orch = get_orchestrator()
        # 先注册执行模式"智能体",decompose_task 才能据此分配子任务。
        # 若 UI 配置了候选执行体清单,则只注册这些模式(子任务只能分配给它们)。
        await self._register_orch_agents(orch, modes=self.orch_candidates or None)
        try:
            sub_tasks = await orch.decompose_task(
                user_input, user_input, self.llm,
                num_subtasks=self.orch_subtasks,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("子任务分解失败，降级为单任务执行: %s", e)
            sub_tasks = None
        if not sub_tasks:
            sub_tasks = [SubTask(description=user_input, assigned_agent="auto")]
        sub_tasks = orch._topological_sort(sub_tasks)

        context = ""
        for i, task in enumerate(sub_tasks):
            worker_mode = task.assigned_agent or "auto"
            if worker_mode not in ("single", "auto", "review", "ensemble"):
                worker_mode = "auto"
            yield {"type": "orchestration", "phase": "task_start",
                   "index": i, "total": len(sub_tasks),
                   "task": task.description, "agent": worker_mode}
            prompt = task.description
            if context:
                prompt = (f"以下是前序子任务已完成的工作,请在其基础上继续:\n{context}\n\n"
                          f"当前子任务: {task.description}")
            answer_parts: list[str] = []
            try:
                async for ev in self._orch_run_worker(worker_mode, prompt, history, i, _sink=answer_parts):
                    yield ev
            except Exception as e:  # noqa: BLE001
                yield {"type": "orchestration", "phase": "task_error",
                       "index": i, "error": str(e)[:200]}
                break
            context += f"\n[子任务 {i + 1} · {worker_mode}]\n{''.join(answer_parts)}\n"
            yield {"type": "orchestration", "phase": "task_done",
                   "index": i, "agent": worker_mode,
                   "answer_preview": "".join(answer_parts)[:400]}
        yield {"type": "orchestration", "phase": "end", "flow": flow}

    async def _orch_run_worker(self, mode_name: str, prompt: str, history, index: int, _sink: list[str] | None = None):
        """驱动单个执行体并转发其事件;token 同时写入 _sink 供上层汇总上下文。"""
        worker = self._make_worker(mode_name, active_tools=self._active_tools)
        async for ev in worker.run_stream(prompt, history=history):
            if ev["type"] == "token":
                if _sink is not None:
                    _sink.append(ev["content"])
                yield ev
            elif ev["type"] in ("thinking", "tool", "plan", "tool_start"):
                yield ev

    async def _orch_consensual(self, user_input: str, history: list[dict]):
        """共识投票:多个执行体并行作答,评审模型投票择优后流式输出最优答案。"""
        candidates = self._consensual_candidates()
        yield {"type": "orchestration", "phase": "parallel_start",
               "candidates": candidates,
               "message": f"并行启动 {len(candidates)} 个执行体,随后投票择优"}
        workers = [self._make_worker(m, active_tools=self._active_tools) for m in candidates]
        try:
            results = await asyncio.gather(*[w.run(user_input, history=history) for w in workers])
        except Exception as e:  # noqa: BLE001
            yield {"type": "orchestration", "phase": "task_error", "error": str(e)[:200]}
            return

        yield {"type": "orchestration", "phase": "voting",
               "message": "多执行体完成,评审模型投票择优中"}
        best = await self._judge_ensemble(user_input, list(results))
        if best is None and results:
            best = results[0]
        if best is None:
            yield {"type": "orchestration", "phase": "task_error", "error": "共识投票未产生结果"}
            return
        yield {"type": "orchestration", "phase": "vote_result",
               "winner": best.model,
               "message": f"投票选出最佳执行体: {best.model}"}
        for ch in best.answer:
            yield {"type": "token", "content": ch}

    async def _node_reflect(self, state: AgentState, graph: Graph) -> None:
        """自评反思(Reflexion 式):用 LLM 按结构化 schema 评价回答质量。

        不合格时把评审意见写回 messages,设置 reflect_rewrite 标志,
        由 _route_reflect 送回 llm 节点重写;通过则保持现状,路由停止。
        评审调用失败/超时一律视为通过,不影响主流程。
        """
        state.metadata["reflect_rounds"] = int(state.metadata.get("reflect_rounds", 0)) + 1
        state.metadata["reflect_rewrite"] = False
        answer = state.answer or ""
        if not answer.strip():
            return

        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "integer", "description": "质量评分 1-10"},
                "issues": {"type": "array", "items": {"type": "string"},
                           "description": "需要修正的具体问题,没有则为空数组"},
                "verdict": {"type": "string", "enum": ["pass", "rewrite"]},
            },
            "required": ["score", "issues", "verdict"],
        }

        def _validator(data):
            if not isinstance(data, dict):
                return False, "结果不是对象"
            if not isinstance(data.get("score"), int) or not 1 <= data["score"] <= 10:
                return False, "score 必须是 1-10 的整数"
            if data.get("verdict") not in ("pass", "rewrite"):
                return False, "verdict 必须是 pass 或 rewrite"
            if not isinstance(data.get("issues"), list):
                return False, "issues 必须是数组"
            return True, None

        review_prompt = (
            f"你是评审者。评价下面的回答是否准确、完整地满足了用户请求。\n"
            f"用户请求: {state.user_input}\n\n"
            f"回答:\n{answer}\n\n"
            f"判断标准:\n"
            f"- 是否直接回答了用户的问题\n"
            f"- 是否引用了工具结果(如有)\n"
            f"- 是否有多余/错误/幻觉内容\n"
            f"打分 1-10;\n"
            f"- 分数>=7 且无事实错误时,verdict 返回 pass\n"
            f"- 只有存在事实错误、幻觉或严重不完整时,verdict 才返回 rewrite\n"
            f"- 格式、语气、详细程度等主观问题不作为 rewrite 理由\n"
        )
        try:
            review = await self._critic().chat_json_schema(
                [
                    {"role": "system", "content": "你是严格但公正的评审者。"},
                    {"role": "user", "content": review_prompt},
                ],
                schema,
                temperature=0.2,
                validator=_validator,
            )
        except Exception as e:  # noqa: BLE001  # 评审失败视为通过
            logger.warning("反思评审失败，降级为直接通过: %s", e)
            return

        verdict = review.get("verdict", "pass")
        issues = review.get("issues") or []
        state.steps.append({
            "step": sum(1 for s in state.steps if s.get("type") == "reflect") + 1,
            "type": "reflect",
            "score": review.get("score"),
            "verdict": verdict,
            "issues": issues,
        })
        if verdict == "rewrite" and issues:
            # 只在实际问题需要修正时才重写，避免对正确答案的过度修改
            critical_issues = [i for i in issues if any(kw in str(i) for kw in
                ["错误", "幻觉", "不实", "不正确", "矛盾", "缺失关键"])]
            if not critical_issues:
                # 只有主观建议时不重写，保持原答案
                state.metadata["reflect_rewrite"] = False
                return
            state.metadata["reflect_rewrite"] = True
            state.messages.append({"role": "user", "content":
                "请根据以下反馈修正回答中的事实错误：\n- "
                + "\n- ".join(str(i) for i in critical_issues)
                + "\n\n请直接给出修正后的完整回答，不要道歉或重复之前的错误。"})

    async def _node_verify(self, state: AgentState, graph: Graph) -> None:
        """最终答案事实校验(LLM-as-judge):核对答案断言是否被工具结果支撑。

        用评审模型检查最终答案与工具结果的一致性:发现未支撑断言/幻觉时,
        直接把工具证据注入并让模型重写一次答案(内联,不走 llm 节点)。
        校验失败/无工具结果/非必要一律通过,不影响主流程。
        """
        if not self._verify_enabled or not state.answer or not state.answer.strip():
            return
        tool_results = [
            s.get("result", "") for s in state.steps
            if s.get("type") == "tool" and s.get("result")
        ]
        if not tool_results:
            return  # 没有工具结果可供交叉核对,跳过校验
        if state.metadata.get("verify_rewritten"):
            return  # 已重写过一次,不再二次重写

        # 证据上限:原为 4000 字,只够装前几个工具结果。读文件类任务的工具结果
        # 动辄几万字,评审员看不到后面的证据就把大量真实结论判成"未支撑",
        # 重写时整段删掉 —— 表现为"答案被削短/信息量骤减"。放宽到 12000。
        evidence = "\n\n".join(tool_results)[:12000]
        schema = {
            "type": "object",
            "properties": {
                "supported": {"type": "boolean", "description": "答案是否全部被工具结果支撑"},
                "unsupported": {"type": "array", "items": {"type": "string"},
                                "description": "答案中未得到工具结果支撑的断言,没有则为空数组"},
            },
            "required": ["supported", "unsupported"],
        }

        def _validator(data):
            if not isinstance(data, dict):
                return False, "结果不是对象"
            if not isinstance(data.get("supported"), bool):
                return False, "supported 必须是布尔值"
            if not isinstance(data.get("unsupported"), list):
                return False, "unsupported 必须是数组"
            return True, None

        prompt = (
            f"你是事实核查员。下面是模型对用户请求的回答,以及回答所依据的工具结果。\n\n"
            f"用户请求: {state.user_input}\n\n"
            f"模型回答:\n{state.answer}\n\n"
            f"工具结果(证据):\n{evidence}\n\n"
            f"请判断回答中的每一个断言是否都有工具结果支撑。"
            f"不要把工具结果中不存在的信息当作支撑;含糊表述也算未支撑。"
            f"supported=true 表示全部支撑;否则 supported=false 并列出未支撑的断言。"
        )
        try:
            check = await self._critic().chat_json_schema(
                [
                    {"role": "system", "content": "你是严格但公平的事实核查员。"},
                    {"role": "user", "content": prompt},
                ],
                schema,
                temperature=0.1,
                validator=_validator,
            )
        except Exception as e:  # noqa: BLE001  # 校验失败视为通过
            logger.warning("事实校验失败，降级为直接通过: %s", e)
            return

        unsupported = check.get("unsupported") or []
        state.steps.append({
            "step": sum(1 for s in state.steps if s.get("type") == "verify") + 1,
            "type": "verify",
            "supported": bool(check.get("supported")),
            "unsupported": unsupported,
        })
        if not check.get("supported") and unsupported:
            state.metadata["verify_rewritten"] = True
            rewrite_prompt = (
                f"请根据以下工具结果修正回答中的事实错误:\n"
                f"未支撑断言:\n- " + "\n- ".join(str(u) for u in unsupported) + "\n\n"
                f"可用证据:\n{evidence}\n\n"
                f"重写要求:只删除或修正上面列出的未支撑断言，其余内容(结构、章节、"
                f"已有分析、代码引用)原样保留，不要缩短、不要重写整篇回答、不要改变结论；"
                f"不引入证据之外的新信息。"
                f"直接给出修正后的完整回答，不要道歉或重复之前的错误。"
                f"用户请求: {state.user_input}"
            )
            try:
                new_answer = await asyncio.wait_for(
                    self.llm.chat(
                        [{"role": "user", "content": rewrite_prompt}],
                        temperature=self.temperature,
                    ),
                    timeout=60
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("校验后 LLM 重写回答失败: %s", e)
                return
            state.answer = new_answer
            # 重写走非流式 chat(),CoT 注入产出的 <thinking> 已被 llm 层剥到
            # self.llm._reasoning。这里补发成 thinking 事件,否则用户看到"思考完成"
            # 却是空的 —— 重写轮恰好就是最终答案那一轮,没有别的 thinking 来源。
            _tk = getattr(self.llm, "_reasoning", "") or ""
            if _tk.strip():
                state.emit({"type": "thinking", "content": _tk, "phase": "verify"})
            state.steps.append({
                "step": sum(1 for s in state.steps if s.get("type") == "verify_rewrite") + 1,
                "type": "verify_rewrite",
                "content": new_answer,
            })
            state.emit({"type": "answer", "content": new_answer, "source": "verify"})

    # ---- 工具调用 ----

    @property
    def _schemas(self) -> list[dict]:
        if self._schemas_cache is None:
            if self._active_tools is None:
                tools = self.registry.list_sync()
            else:
                active = set(self._active_tools) | set(self._always_tools)
                tools = [t for t in self.registry.list_sync() if t.name in active]
            self._schemas_cache = [t.to_openai_schema() for t in tools]
        return self._schemas_cache

    async def _maybe_select_tools(self, user_input: str, steps: list[dict], step: int) -> Optional[dict]:
        """对话前用 LLM 预选相关工具子集(工具路由)。返回 SSE 事件;未路由/失败返回 None。

        优先使用增强型路由(smart_router 三级缓存+置信度),失败返回 None。
        """
        if not settings.agent_tool_router_enabled:
            return None
        if getattr(self.llm, "provider", "") == "mock":
            return None

        # 简短问候快速通道:跳过LLM路由
        cleaned = user_input.strip()
        if len(cleaned) <= _TOOL_ROUTE_SHORT_INPUT_THRESHOLD and not any(kw in cleaned for kw in _TOOL_ROUTE_SHORT_INPUT_KEYWORDS):
            return None
        # 改进：增加默认预选工具数量，从 10 提高到 20
        max_tools = min(int(settings.agent_max_tools or 20), len(self.registry.list_sync()))
        # 改进：提高置信度阈值，从 0.10 提高到 0.30
        min_confidence = 0.30

        # 尝试从缓存获取路由结果
        from app.core.tool_route_cache import get_tool_route_cache
        cache = get_tool_route_cache()
        cached = await cache.get(user_input)
        if cached is not None:
            logger.debug("使用缓存的工具路由结果: %d 个工具", len(cached.tools))
            self._active_tools = cached.tools
            self._schemas_cache = None
            steps.append({"step": step, "type": "tool_route", "tools": cached.tools, "reason": cached.reason, "cached": True})
            return {"type": "tool_route", "tools": cached.tools, "reason": cached.reason, "cached": True}

        # 使用智能路由(关键词+embedding+LLM 三级缓存 + 置信度评分)
        try:
            from app.core.smart_router import select_tools_enhanced
            # 添加超时保护
            route_timeout = 30.0
            selected, conf, reason = await asyncio.wait_for(
                select_tools_enhanced(
                    self.llm, user_input, self.registry,
                    max_tools=max_tools,
                    min_confidence=min_confidence,
                    user_tech_stack=getattr(self, "_tech_stack", []),
                ),
                timeout=route_timeout
            )
            if selected and conf >= min_confidence:
                self._active_tools = selected
                self._schemas_cache = None
                # 写入缓存
                await cache.set(user_input, selected, conf, reason)
                steps.append({"step": step, "type": "tool_route", "tools": selected, "reason": reason})
                return {"type": "tool_route", "tools": selected, "reason": reason}
        except asyncio.TimeoutError:
            logger.warning("智能工具路由超时，回退到全量工具集")
        except Exception as e:  # noqa: BLE001
            logger.warning("智能工具路由失败，回退到全量工具集: %s", e)

        return None

    async def _call_llm(self, messages: list[dict]) -> tuple[str, str, list[ToolCall]]:
        """调用 LLM。返回 (mode, content, tool_calls)。

        mode: "native"=原生 function calling / "text"=文本(手写 JSON)
        """
        # 获取 LLM 超时配置
        llm_timeout = float(getattr(settings, "agent_llm_timeout", 60) or 60)

        if self._native_enabled:
            try:
                result = await asyncio.wait_for(
                    self.llm.chat_with_tools(messages, tools=self._schemas, temperature=self.temperature),
                    timeout=llm_timeout
                )
                if result.tool_calls:
                    return "native", result.content, result.tool_calls
                # 原生模式无 tool_calls,但 content 可能是手写 JSON(部分 provider 忽略 tools)
                tool_calls = self._parse_tool_calls(result.content)
                if not tool_calls:
                    return "native", result.content, []
                return "text", result.content, tool_calls
            except asyncio.TimeoutError:
                raise LLMError(f"LLM 原生调用超时 ({llm_timeout}s)")
            except Exception as e:  # noqa: BLE001  # 原生调用失败,自动降级
                logger.warning("native 模式调用失败，自动降级: %s", e)
                self._disable_native()

        resp = await asyncio.wait_for(
            self.llm.chat(messages, temperature=self.temperature),
            timeout=llm_timeout
        )
        tool_calls = self._parse_tool_calls(resp)
        if not tool_calls:
            return "text", resp, []
        return "text", resp, tool_calls


    async def _call_llm_stream(
        self, messages: list[dict], state: AgentState
    ) -> tuple[str, str, list["ToolCall"]]:
        """流式调用 LLM:逐 token 发射事件,同时收集完整结果用于路由。"""
        if self._native_enabled:
            try:
                result_holder: dict = {"result": None}
                collected: list = []
                async for item in self.llm.chat_stream_with_tools(
                    messages, tools=self._schemas, temperature=self.temperature,
                    result_holder=result_holder,
                ):
                    if isinstance(item, dict):
                        state.emit(item)
                    else:
                        collected.append(item)
                        state.emit({"type": "token", "content": item})
                result = result_holder["result"]
                if result and result.tool_calls:
                    return "native", result.content or "", result.tool_calls
                full = "".join(collected)
                tool_calls = self._parse_tool_calls(full)
                if not tool_calls:
                    return "native", full, []
                return "text", full, tool_calls
            except LLMError as e:
                # 流中途空闲/总时长超时:此时可能已向用户吐出部分 token,
                # 降级到 text 模式会重新发起整个 LLM 请求,导致已输出内容重复 + 卡顿翻倍。
                # 区分: 建连前失败(collected 为空,未输出任何内容)可安全降级; 流中途失败必须直接上抛。
                if collected:
                    logger.warning("native 流式中途失败(已输出 %d 段内容),不再降级: %s", len(collected), e)
                    raise
                logger.warning("native 模式建连/调用失败,自动降级: %s", e)
                self._disable_native()
            except Exception as e:  # noqa: BLE001
                logger.warning("native 模式调用失败，自动降级: %s", e)
                self._disable_native()

        # 文本模式流式
        try:
            collected: list = []
            async for tok in self.llm.chat_stream(messages, temperature=self.temperature):
                if isinstance(tok, dict):
                    state.emit(tok)
                    continue
                collected.append(tok)
                state.emit({"type": "token", "content": tok})
            full = "".join(collected)
            tool_calls = self._parse_tool_calls(full)
            if not tool_calls:
                return "text", full, []
            return "text", full, tool_calls
        except Exception as e:  # noqa: BLE001
            logger.error("文本模式流式调用失败: %s", e)
            raise
    async def _execute_tool(self, name: str, arguments: str) -> str:
        """执行单个工具(带瞬态错误重试/退避),统一返回 JSON 字符串结果。"""
        if name in PLAN_TOOLS:
            return await self._execute_plan_tool(name, arguments)
        if name in NO_RETRY_TOOLS:
            return await self._execute_tool_once(name, arguments)
        retries = max(0, int(settings.agent_tool_retries))
        for attempt in range(retries + 1):
            result = await self._execute_tool_once(name, arguments)
            if attempt < retries and self._is_transient_error(result):
                await asyncio.sleep(settings.agent_tool_retry_delay * (2 ** attempt))
                continue
            return result

    async def _execute_tool_once(self, name: str, arguments: str) -> str:
        # 智能体硬规则:被显式禁止的工具一律拦下(放最前, 不执行任何工具逻辑)。
        # 词/路径类硬规则(rule_terms/rule_paths)与 warn 类由输入输出护栏(guardrails)统一处理,
        # 此处只拦"禁止的工具名", 避免和护栏重复实现打架。
        if self.rule_blocked_tools and name in self.rule_blocked_tools:
            logger.info("规则拦截工具: %s (rule_blocked_tools)", name)
            return json.dumps(
                {"error": f"工具 {name} 已被规则禁止执行。", "blocked": True},
                ensure_ascii=False,
            )
        # 确保项目路径上下文在每次工具调用时都已设置(线程安全)
        if self.project_path:
            from app.tools.project_context import set_project_path
            set_project_path(self.project_path)
        # 搜索查询去重:检测重复/相似搜索,防止搜索循环
        if name == "web_search":
            try:
                args = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
                query = args.get("query", "") if isinstance(args, dict) else ""
                if query and self._is_similar_search_query(query):
                    logger.warning("搜索查询过于相似,拦截重复搜索: %s", query[:50])
                    return json.dumps({
                        "error": f"搜索查询 '{query}' 与近期搜索过于相似,请不要重复搜索相同主题。"
                                 f"请基于已有搜索结果进行分析,或使用完全不同的搜索词。",
                        "blocked": True
                    }, ensure_ascii=False)
                if query:
                    self._record_search_query(query)
            except (json.JSONDecodeError, TypeError):
                pass
        # 危险工具频率限制(与 ToolExecutor 行为一致)
        if name in self._dangerous_tools:
            allowed, limit_msg = self._dangerous_rate_limiter.allow(name)
            if not allowed:
                return json.dumps({"error": limit_msg, "blocked": True}, ensure_ascii=False)
        # 工具路由裁剪:未选中的工具不允许执行,防止模型意外调用冷门工具
        if self._active_tools is not None and name not in self._active_tools and name not in self._always_tools:
            return json.dumps({"error": f"工具 {name} 未在当前任务启用(工具路由器未选中)"}, ensure_ascii=False)
        # 人工确认流:危险工具需在请求 approve_tools 中获批
        if settings.agent_tool_confirm and name in self._dangerous_tools and name not in self.approve_tools \
                and name not in self._runtime_approved:
            return json.dumps({
                "error": f"工具 {name} 需要用户确认后才能执行。请向用户说明将执行的操作,"
                         f"用户批准后再调用(批准方式:在请求 approve_tools 中带上 {name})"
            }, ensure_ascii=False)
        t0 = time.time()
        tool = self.registry.get(name)
        if tool is None:
            # 以前这里静默返回,排查"模型点了工具却说未知"时毫无线索。
            # 记下注册表规模与若干可用工具名,便于定位是"注册表为空/另一实例"还是"名字拼错"。
            try:
                _known = [t.name for t in self.registry.list_sync()]
            except Exception:  # noqa: BLE001
                _known = []
            logger.warning(
                "未知工具: %s (registry=%s, 共 %d 个工具, 近似名: %s)",
                name, type(self.registry).__name__, len(_known),
                [k for k in _known if name[:6] in k or k[:6] in name][:10],
            )
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)

        # 安全门禁:高风险操作必须等待用户人工批准(硬拦截,模型无法绕过)。
        # 已通过 approve_tools / interrupt 式运行时批准的工具直接放行。
        # 注意:此检查必须在工具缓存之前执行,防止缓存被用作绕过机制
        if settings.agent_confirm_enabled and name not in self.approve_tools \
                and name not in self._runtime_approved:
            from app.core.security import decide

            decision = decide(name, arguments)
            # 硬拦截: 命中 BLOCKED 命令时直接拒绝(模型无法绕过, 无需确认机会)
            if not decision.allowed:
                return json.dumps({
                    "error": f"操作已被硬拦截: {decision.reason}",
                    "blocked": True,
                    "fatal": True,
                    "ok": False,
                }, ensure_ascii=False)
            if decision.confirm:
                if settings.agent_confirm_interrupt:
                    # interrupt 模式下预扫描已先中断; 此处为兜底路径(预扫描未覆盖的调用)
                    # 返回 fatal 错误强制终止本次执行, 避免模型忽略"等待批准"提示继续调用
                    return json.dumps({
                        "error": f"操作已被拦截: {decision.reason}(等待用户批准)",
                        "blocked": True,
                        "fatal": True,
                        "confirmation_required": True,
                        "ok": False,
                    }, ensure_ascii=False)
                # 阻塞轮询模式: 等待用户批准(原行为保留)
                allowed, status = await self._await_confirmation(name, arguments, decision.reason)
                if not allowed:
                    return json.dumps({
                        "error": f"操作已被拦截: {decision.reason}(用户未批准)",
                        "blocked": True,
                        "confirmation_required": True,
                        "status": status,
                        "ok": False,
                    }, ensure_ascii=False)

        # 工具结果去重缓存:先查缓存,命中则直接返回,避免重复执行
        # 只有当 cache_size > 0 时才启用缓存查找
        max_cache_size = int(settings.agent_tool_cache_size or 0)
        if max_cache_size > 0 and self._tool_cache is not None and name in self._tool_cache_safe:
            key = (name, self._normalize_tool_args(arguments))
            async with self._tool_cache_lock:
                cached_result = self._tool_cache.get(key)
                if cached_result is not None:
                    return cached_result

        try:
            result = await tool.run(arguments)
        except Exception as e:  # noqa: BLE001
            tracer.record({"type": "tool", "name": name, "ok": False,
                           "error": str(e)[:300], "duration": round(time.time() - t0, 3),
                           "args_preview": (arguments or "")[:120]})
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        tracer.record({"type": "tool", "name": name, "ok": not self._is_tool_error(result),
                       "result_len": len(result), "duration": round(time.time() - t0, 3),
                       "args_preview": (arguments or "")[:120]})
        # 缓存成功结果(去重缓存):仅安全工具缓存,失败/副作用工具不缓存
        # 使用归一化 JSON 键(排序 key),避免 {"a":1,"b":2} 与 {"b":2,"a":1} 被视为不同键
        if self._tool_cache is not None and not self._is_tool_error(result) and name in self._tool_cache_safe:
            key = (name, self._normalize_tool_args(arguments))
            async with self._tool_cache_lock:
                if key not in self._tool_cache:
                    max_size = int(settings.agent_tool_cache_size or 0)
                    if max_size > 0 and len(self._tool_cache) >= max_size:
                        # LRU: 淘汰最久未使用的(字典第一个)
                        self._tool_cache.popitem(last=False)
                    self._tool_cache[key] = result
        return result

    @staticmethod
    def _normalize_tool_args(arguments: str) -> str:
        """归一化工具参数 JSON,使相同逻辑调用无论键序/数值类型如何都映射到同一键。

        - dict 按 key 排序
        - 数值 1 vs 1.0 统一为整数形式
        - 失败时回退到原始字符串(保守不改变行为)
        """
        if not isinstance(arguments, str) or not arguments.strip():
            return arguments or ""
        try:
            parsed = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return arguments
        try:
            def _norm(v):
                if isinstance(v, dict):
                    return {k: _norm(x) for k, x in sorted(v.items())}
                if isinstance(v, list):
                    return [_norm(x) for x in v]
                if isinstance(v, float) and v.is_integer():
                    return int(v)
                return v
            return json.dumps(_norm(parsed), ensure_ascii=False, sort_keys=True)
        except Exception:  # noqa: BLE001
            return arguments

    def _scan_pending_confirmations(self, tool_calls: list[ToolCall]) -> list[tuple[ToolCall, object]]:
        """预扫描一批工具调用,返回需要人工确认且未批准的 (tool_call, decision)。

        判定规则统一由共享实现（app.core.tool_safety）处理两套机制:
        - agent_tool_confirm 机制的危险工具(未在 approve_tools)
        - agent_confirm_enabled 机制的 decide() 命中 confirm
        """
        from app.core.tool_safety import scan_pending_confirmations as _scan

        return _scan(tool_calls, self.approve_tools, self._runtime_approved, self._dangerous_tools)

    def _create_confirm_requests(self, pending: list[tuple[ToolCall, object]]) -> list[dict]:
        """为待确认的工具调用创建持久化确认请求,返回请求列表(供事件与 resume 使用)。"""
        from app.core.tool_safety import create_confirm_requests as _create

        return _create(pending)

    def _is_tool_approved_by_resume(self, tc: ToolCall, state: AgentState) -> bool:
        """恢复运行时,判断该工具是否已被用户批准。

        优先采用运行时批准集合;其次解析 resume 值(list/dict);
        最后回退查询持久化 confirmations.db,兼容用户已通过前端批准的情况。
        """
        from app.core.tool_safety import is_tool_approved_by_resume as _approved

        return _approved(tc, state, runtime_approved=self._runtime_approved)

    async def _await_confirmation(self, name: str, arguments: str, reason: str) -> tuple[bool, str]:
        """创建确认请求并轮询等待用户批准。返回 (是否放行, 状态)。

        请求会持久化到 confirmations.db,前端轮询 pending 接口展示批准卡片;
        用户在卡片上批准/拒绝后,此处轮询到结果并放行或拦截。
        注:interrupt 式确认流(agent_confirm_interrupt)不走此阻塞路径。
        """
        from app.core.security import describe
        from app.tools.collaboration_tool import _get_store

        try:
            params = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
        except (json.JSONDecodeError, TypeError):
            params = {}
        if not isinstance(params, dict):
            params = {"raw": str(params)}

        store = _get_store()
        rec = store.create(
            action=name,
            description=f"{reason}。\n详情: {describe(name, arguments)}",
            params=params,
        )
        deadline = time.time() + max(1, int(settings.agent_confirm_timeout))
        while time.time() < deadline:
            await asyncio.sleep(_CONFIRM_POLL_INTERVAL)
            conf = store.get(rec["id"])
            if conf:
                if conf["status"] == "approved":
                    return True, "approved"
                if conf["status"] == "rejected":
                    return False, "rejected"
        # 超时:返回标记以便前端展示明确提示
        return False, "timeout"

    _TRANSIENT_PATTERN = TRANSIENT_PATTERNS

    @classmethod
    def _is_transient_error(cls, result_str: str) -> bool:
        """判断工具结果是否为可重试的瞬态错误(网络/超时/限流/服务不可用)。"""
        try:
            data = json.loads(result_str)
            if isinstance(data, dict) and data.get("error"):
                text = str(data["error"]).lower()
            else:
                return False
        except (json.JSONDecodeError, TypeError):
            return False
        return any(pat in text for pat in cls._TRANSIENT_PATTERN)

    async def _execute_plan_tool(self, name: str, arguments: str) -> str:
        """执行计划工具(set_plan/update_plan),更新 Agent 私有计划状态并返回 JSON。"""
        self._plan, result = apply_plan_tool(self._plan, name, arguments)
        self._plan_dirty = True
        return json.dumps(result, ensure_ascii=False)

    async def _execute_batch(
        self, tool_calls: list[ToolCall], steps: list[dict], step: int
    ) -> list[tuple[ToolCall, str, bool]]:
        """并行执行一批工具调用,记录执行步骤。"""
        from app.tools.memory_tool import _current_session

        # 透传当前 session_id 给记忆工具(memory_save/memory_search)
        token = _current_session.set(self.session_id)
        # 透传父 Agent 的模型/会话上下文给委派工具(delegate_task 据此创建子智能体)
        from app.tools.delegation_tool import set_delegate_context

        d_token = set_delegate_context(self.llm, self.session_id, self._active_tools)
        try:
            results = await asyncio.gather(
                *[self._execute_tool(tc.name, tc.arguments) for tc in tool_calls]
            )
        finally:
            _current_session.reset(token)
            from app.tools.delegation_tool import _delegate_parent

            _delegate_parent.reset(d_token)
        batch: list[tuple[ToolCall, str, bool]] = []
        for tc, result_str in zip(tool_calls, results):
            is_err = self._is_tool_error(result_str)
            steps.append({
                "step": step + 1,
                "type": "tool",
                "tool": tc.name,
                "arguments": tc.arguments,
                "result": result_str,
            })
            batch.append((tc, result_str, is_err))

        # 自动验证钩子:文件写入类工具成功后触发 pytest/ruff/mypy
        if self._verification_hook and not any(is_err for _, _, is_err in batch):
            file_write_tools = {"write_file", "edit_file", "apply_patch", "code_edit_file"}
            for tc, result_str, is_err in batch:
                if tc.name in file_write_tools and not is_err:
                    try:
                        # 从参数中提取文件路径
                        args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                        file_path = args.get("file_path") or args.get("path") or args.get("file")
                        if file_path:
                            logger.info("触发自动验证: %s -> %s", tc.name, file_path)
                            verification_result = await run_verification_hook(file_path, self.project_path)
                            if not verification_result.success:
                                logger.warning(
                                    "验证失败: pytest=%s ruff=%s mypy=%s",
                                    verification_result.checks.get("pytest"),
                                    verification_result.checks.get("ruff"),
                                    verification_result.checks.get("mypy"),
                                )
                                # 将验证失败信息回喂给模型,触发自愈/反思
                                steps.append({
                                    "step": step + 1,
                                    "type": "verification",
                                    "tool": "auto_verify",
                                    "result": {
                                        "success": False,
                                        "checks": verification_result.checks,
                                        "stdout": verification_result.stdout[:2000],
                                        "stderr": verification_result.stderr[:2000],
                                    },
                                })
                    except Exception as e:
                        logger.debug("验证钩子解析参数失败: %s", e)
        return batch

    # ---- 代码级自愈 ----

    async def _maybe_self_heal(
        self, state: AgentState,
        batch: list[tuple[ToolCall, str, bool]], step: int,
    ) -> list[tuple[ToolCall, str, bool]]:
        """对失败的代码类工具(run_code/run_terminal)尝试自动修复。

        调用 app.core.code_heal:LLM 分析失败输出→生成修复 patch→应用→重跑。
        自愈成功后用新结果替换原失败结果,并在 steps 记录 heal 步骤;
        自愈失败/未配置/非代码工具原样返回,交给后续反思逻辑。
        """
        if not settings.agent_code_self_heal:
            return batch
        if getattr(self.llm, "provider", "") == "mock":
            return batch
        out: list[tuple[ToolCall, str, bool]] = []
        heal_rounds = int(state.metadata.get("heal_rounds", 0))
        max_rounds = max(1, int(settings.agent_code_heal_rounds or 1))
        # 带"修复后仍失败"标记的结果不再重复自愈(收敛),原样交给后续反思
        for tc, result_str, is_err in batch:
            if not is_err or tc.name not in ("run_code", "run_terminal") \
                    or heal_rounds >= max_rounds or "heal_still_failing" in result_str:
                out.append((tc, result_str, is_err))
                continue
            try:
                from app.core.code_heal import heal_code_failure

                result = await heal_code_failure(
                    self.llm,
                    state.user_input,
                    tc.name,
                    tc.arguments,
                    result_str,
                    max_rounds=max_rounds - heal_rounds,
                )
            except Exception as e:  # noqa: BLE001  # 自愈失败不影响主循环
                logger.warning("代码自愈失败，保持原始错误结果: %s", e)
                result = {"healed": False}
            state.metadata["heal_rounds"] = heal_rounds + 1
            state.steps.append({
                "step": step + 1,
                "type": "heal",
                "tool": tc.name,
                "healed": bool(result.get("healed")),
                "attempts": result.get("rounds", 0),
                "details": result.get("steps", [])[-3:],
            })
            if result.get("healed"):
                out.append((tc, result.get("final", ""), False))
            else:
                out.append((tc, result_str, True))
        return out

    def _append_reflection(
        self, messages: list[dict], batch: list[tuple[ToolCall, str, bool]], failures: dict[str, int]
    ) -> Optional[str]:
        """对失败的工具体现反思提示;终结型工具成功则返回工具名。"""
        error_names = [tc.name for tc, _r, is_err in batch if is_err]
        terminal = None
        for tc, result_str, is_err in batch:
            tool = self.registry.get(tc.name)
            if tool is not None and tool.terminal and not is_err:
                terminal = tc.name
        for name in error_names:
            failures[name] = failures.get(name, 0) + 1

        # 搜索重复检测:检查是否在短时间内调用了过多搜索
        search_calls = [tc for tc, _, _ in batch if tc.name == "web_search"]
        if len(search_calls) > 0:
            total_searches = len(self._search_query_history)
            if total_searches >= self._search_max_repeats:
                hint = (
                    f"检测到已进行 {total_searches} 次搜索,请停止重复搜索。"
                    "请基于已有的搜索结果进行综合分析和回答,不要再调用 web_search。"
                    "如果现有信息不足,请直接说明哪些信息缺失,而不是继续搜索。"
                )
                messages.append({"role": "user", "content": hint})
                return terminal

        if error_names:
            # 测试/构建失败:给出定向的"定位→修改→重跑"闭环提示
            name_to_result = {tc.name: result_str for tc, result_str, _e in batch}
            test_fail = [n for n in error_names if self._looks_like_test_failure(n, name_to_result.get(n, ""))]
            if test_fail:
                hint = (
                    "检测到测试/构建失败(" + "、".join(test_fail)
                    + ")。请根据失败输出定位原因:先用 code_context 查看相关源码,"
                    "再用 edit_file/write_file 修改代码,然后重新运行测试确认通过。不要重复运行相同命令。"
                )
                messages.append({"role": "user", "content": hint})
                return terminal
            stubborn = [n for n in set(error_names) if failures[n] >= 2]
            if stubborn:
                hint = "以下工具已连续失败,请不要再使用它们,改用其他工具或方案:" + "、".join(stubborn)
            else:
                hint = (
                    "以下工具执行失败:" + "、".join(error_names)
                    + "。请分析原因,修正参数或换用其他工具重试,不要重复相同的调用。"
                )
            messages.append({"role": "user", "content": hint})
        return terminal

    @staticmethod
    def _is_tool_error(result_str: str) -> bool:
        try:
            data = json.loads(result_str)
            if isinstance(data, dict):
                if data.get("error"):
                    return True
                # 终端命令失败/被拦截也视为失败,触发反思(否则测试失败无法进入修复循环)
                if data.get("success") is False or data.get("blocked") is True:
                    return True
        except (json.JSONDecodeError, TypeError):
            pass
        return False

    # ---- 搜索查询去重 ----

    def _normalize_search_query(self, query: str) -> str:
        """归一化搜索查询:去除停用词、标点,提取核心关键词用于相似度比较。"""
        import re
        # 去除标点和空白:保留中文、英文、数字
        q = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff]+', ' ', query)
        # 常见停用词(搜索场景)
        stop_words = {'的', '了', '在', '是', '我', '有', '和', '就', '不', '人', '都', '一',
                      '一个', '上', '也', '很', '到', '说', '要', '去', '你', '会', '着',
                      '没有', '看', '好', '自己', '这', '他', '她', '它', '们', '那',
                      '最新', '新闻', '消息', '资讯', '信息', '搜索', '查找', '查询',
                      '请问', '请', '帮我', '告诉', '了解', '查看'}
        words = q.split()
        filtered = [w for w in words if w not in stop_words]
        return ' '.join(filtered)

    def _is_similar_search_query(self, query: str) -> bool:
        """检查搜索查询是否与近期查询过于相似,防止搜索循环。

        改进: 引入时间衰减权重 + 提高阈值至 0.7 + 双向包含检测。
        - 时间衰减: 越近的查询权重越高,避免 60 秒内所有查询等权累计导致误判
        - 阈值 0.7: 避免"帮我查北京天气"与"北京明天天气"这类合理追问被拦截
        - 双向包含: 短查询被长查询包含时也视为相似(中文分词不完整时的兜底)
        """
        import time
        now = time.time()
        normalized = self._normalize_search_query(query)
        if not normalized:
            return False
        # 清理超过 60 秒的历史记录
        self._search_query_history = [
            (q, t) for q, t in self._search_query_history if now - t < 60
        ]
        # 检查相似度:核心关键词集合的 Jaccard 相似度(带时间衰减)
        query_words = set(normalized.split())
        repeat_score = 0.0
        for hist_query, ts in self._search_query_history:
            hist_words = set(self._normalize_search_query(hist_query).split())
            if not query_words or not hist_words:
                continue
            # 双向包含: 一个词集合完全包含另一个(短查询被长查询包含)
            if query_words <= hist_words or hist_words <= query_words:
                similarity = 0.9  # 包含关系视为高度相似
            else:
                intersection = query_words & hist_words
                union = query_words | hist_words
                similarity = len(intersection) / len(union) if union else 0
            if similarity >= 0.7:
                # 时间衰减: 5 秒前权重 1.0, 60 秒前权重 0.1, 线性插值
                age = now - ts
                weight = max(0.1, 1.0 - (age / 60.0) * 0.9)
                repeat_score += weight
        return repeat_score >= self._search_max_repeats - 1

    def _record_search_query(self, query: str) -> None:
        """记录搜索查询到历史。"""
        import time
        self._search_query_history.append((query, time.time()))
        # 限制历史长度
        if len(self._search_query_history) > 20:
            self._search_query_history = self._search_query_history[-20:]

    # ---- 测试/构建失败闭环 ----

    _TEST_FAIL_MARKERS = TEST_FAIL_MARKERS

    @classmethod
    def _looks_like_test_failure(cls, name: str, result_str: str) -> bool:
        """判断工具结果是否为测试/构建失败输出(用于给出定向修复提示)。"""
        if name != "run_terminal":
            return False
        try:
            data = json.loads(result_str)
            if not isinstance(data, dict):
                return False
        except (json.JSONDecodeError, TypeError):
            return False
        out = str(data.get("stdout", "")) + "\n" + str(data.get("stderr", ""))
        low = out.lower()
        return any(m in low for m in cls._TEST_FAIL_MARKERS)

    # ---- 任务规划(Plan-then-Execute) ----

    async def _plan_task(self, user_input: str, steps: list[dict], step: int) -> bool:
        """复杂任务先生成执行计划。返回是否创建了计划。"""
        self._plan = None
        self._plan_dirty = False
        if not settings.agent_planning:
            return False
        # 添加超时保护，防止规划卡住
        plan_timeout = float(getattr(settings, "agent_llm_timeout", 60) or 60)
        try:
            plan = await asyncio.wait_for(
                self._maybe_plan(user_input),
                timeout=plan_timeout
            )
        except asyncio.TimeoutError:
            logger.warning("任务规划超时，跳过规划直接执行")
            return False
        if plan is None:
            return False
        self._plan = plan
        self._plan_dirty = False
        steps.append({"step": step, "type": "plan", "plan": plan.to_dict()})
        return True

    async def _maybe_plan(self, user_input: str) -> Optional[TaskPlan]:
        """让 LLM 判断复杂度并生成计划;失败/简单任务返回 None,不影响主循环。"""
        # 简短消息跳过规划
        cleaned = user_input.strip()
        if len(cleaned) <= _PLAN_SHORT_INPUT_THRESHOLD and not any(kw in cleaned for kw in _PLAN_SHORT_INPUT_KEYWORDS):
            return None
        # 添加超时保护
        plan_timeout = float(getattr(settings, "agent_llm_timeout", 60) or 60)
        try:
            plan = await asyncio.wait_for(
                generate_plan(self.llm, user_input, max_steps=settings.agent_plan_max_steps),
                timeout=plan_timeout
            )
        except asyncio.TimeoutError:
            logger.warning("任务规划 LLM 调用超时，跳过规划")
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning("任务规划生成失败，放弃分步计划: %s", e)
            return None
        if plan is None or not plan.steps:
            return None
        plan.set_current(0)
        return plan

    async def _maybe_replan(
        self, state: AgentState, batch: list[tuple[ToolCall, str, bool]]
    ) -> bool:
        """工具连续失败时,基于失败原因生成修订版执行计划并替换旧计划。

        触发条件:存在计划、同工具连续失败 ≥ agent_replan_after_failures、
        未超过最大重规划次数、非 mock LLM。
        失败/无计划/超预算均静默返回 False,不影响主流程。
        """
        if not settings.agent_replan_enabled or self._plan is None:
            return False
        if getattr(self.llm, "provider", "") == "mock":
            return False
        replan_times = int(state.metadata.get("replan_times", 0))
        if replan_times >= settings.agent_replan_max_times:
            return False
        # 找出本轮失败的且已连续失败达阈值的工具
        errors = []
        for tc, result_str, is_err in batch:
            if is_err and state._failures.get(tc.name, 0) >= settings.agent_replan_after_failures:
                errors.append(f"- {tc.name}(参数: {tc.arguments}) → {result_str[:200]}")
        if not errors:
            return False

        schema = {
            "type": "object",
            "properties": {
                "needs_plan": {"type": "boolean"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["title", "description"],
                    },
                },
            },
            "required": ["needs_plan", "steps"],
        }
        prompt = (
            f"你正在执行一个任务,现有计划的部分步骤执行失败,需要你重新规划。\n\n"
            f"原始任务: {state.user_input}\n\n"
            f"现有计划:\n" + "\n".join(f"{i+1}. [{s.status}] {s.title} - {s.description}"
                                      for i, s in enumerate(self._plan.steps))
            + "\n\n"
            f"失败的执行:\n" + "\n".join(errors) + "\n\n"
            f"请输出修订后的完整计划(needs_plan=true 并给出 steps)。"
        )
        try:
            data = await self.llm.chat_json_schema(
                [{"role": "system", "content": "你是任务规划助手,输出 JSON。"},
                 {"role": "user", "content": prompt}],
                schema,
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("任务规划 JSON 解析失败，降级为单步执行: %s", e)
            return False
        if not isinstance(data, dict) or not data.get("needs_plan"):
            return False
        steps = _normalize_steps(data.get("steps") or [], settings.agent_plan_max_steps)
        if not steps:
            return False
        state.metadata["replan_times"] = replan_times + 1
        self._plan = TaskPlan(steps)
        self._plan_dirty = True
        state.plan = self._plan.to_dict()
        state.steps.append({
            "step": len(state.steps) + 1,
            "type": "plan",
            "action": "replan",
            "reason": "工具连续失败,已重新规划",
            "plan": self._plan.to_dict(),
        })
        return True

    def _flush_plan(self, steps: list[dict], step: int) -> Optional[dict]:
        """计划状态变更后,记录到 steps 并返回 SSE 事件(无变更返回 None)。"""
        if not self._plan_dirty:
            return None
        self._plan_dirty = False
        plan_data = self._plan.to_dict() if self._plan else None
        steps.append({"step": step + 1, "type": "plan", "plan": plan_data})
        return {"type": "plan", "action": "update", "plan": plan_data}

    def _finalize_plan(self, steps: list[dict], step: int) -> Optional[dict]:
        """最终回答前,把剩余步骤标记完成并刷新计划状态。"""
        if self._plan is None:
            return None
        if not self._plan.is_complete():
            for s in self._plan.steps:
                if s.status in ("pending", "in_progress"):
                    s.status = "done"
            self._plan_dirty = True
        return self._flush_plan(steps, step)

    # ---- 路由 / 记忆 / 摘要 ----

    async def _maybe_route(self, user_input: str) -> None:
        """按任务复杂度切换模型(仅当显式启用路由)。"""
        if not self.route:
            return
        if getattr(self.llm, "provider", "") == "mock":
            return
        from app.core.smart_router import build_routed_llm

        routed = build_routed_llm(user_input)
        if routed is not None:
            self.llm = routed
        self._native_enabled = settings.agent_native_tools and callable(getattr(self.llm, "chat_with_tools", None))

    async def _prepare_history(self, history: list[dict]) -> list[dict]:
        """历史超预算时,用摘要压缩旧消息(替代粗暴截断)。

        使用 token 级预算(agent_token_budget>0 时),否则沿用字符预算。
        """
        if not history or not settings.agent_summarize:
            return history
        if self.token_budget and self.token_budget > 0:
            from app.memory.context import layered_compress

            processed, _stats = await layered_compress(
                self.llm,
                history,
                max_tokens=int(self.token_budget),
                head_tokens=0,
                min_keep=6,
            )
            return processed
        
        # 改进：使用 token 估算替代字符计数，更准确
        from app.memory.context import estimate_tokens
        total_tokens = sum(
            estimate_tokens(str(m.get("content", ""))) for m in history if m.get("role") in ("user", "assistant")
        )
        
        # 字符预算转换为 token 预算（约 4 字符/token）
        token_budget = int(self.context_budget / 4)
        summary_threshold = int(token_budget * settings.agent_summary_threshold_ratio)
        
        if total_tokens <= summary_threshold:
            return history
        
        # 改进：提高保留比例，从 50% 提高到 60%
        keep: list[dict] = []
        acc = 0
        keep_budget = int(token_budget * 0.4)  # 从 0.6 降低到 0.4, 更积极压缩
        for m in reversed(history):
            c = estimate_tokens(str(m.get("content", "")))
            if acc + c <= keep_budget:
                keep.insert(0, m)
                acc += c
            else:
                break
        old = history[: len(history) - len(keep)]
        if len(old) < 2:
            return history
        # 修复: 摘要 LLM 只认 OpenAI 合法角色, 先降级 error/plan/孤立tool 再传入
        from app.memory.session import sanitize_history_for_llm
        old = sanitize_history_for_llm(old)
        from app.memory.summarize import summarize_messages_async

        summary = await summarize_messages_async(self.llm, old)
        if summary:
            return [{
                "role": "system",
                "content": f"以下是本会话早期内容的摘要(请据此理解前文背景):\n{summary}",
            }] + keep
        return history

    def _local_trim_history(self, history: list[dict], char_budget: int) -> list[dict]:
        """纯本地截断历史(fast_mode 用, 不调 LLM): 从最旧的消息开始丢弃,
        直到累计字符量 <= char_budget, 且始终保留最后一条 user 输入。

        与 _prepare_history 的 LLM 摘要不同, 这里是 O(n) 本地操作, 无网络/超时风险,
        适合交互多轮——历史增长时只截断而不卡。
        """
        if not history:
            return history
        from app.memory.session import sanitize_history_for_llm

        san = sanitize_history_for_llm(history)
        keep: list[dict] = []
        acc = 0
        for m in reversed(san):
            c = len(str(m.get("content", "")))
            if keep and acc + c > char_budget:
                break
            keep.insert(0, m)
            acc += c
        # 保证最后一条 user 输入一定在(哪怕超预算也保留, 否则本轮问题丢失)
        if not any(m.get("role") == "user" for m in keep):
            for m in reversed(san):
                if m.get("role") == "user":
                    keep.insert(0, m)
                    break
        return keep

    def _trim_state_messages(self, state: AgentState) -> None:
        """工具循环中修剪 state.messages, 防止上下文无限增长导致 context_length 错误。

        策略: 不删除任何消息(保持角色序列完整), 只截断过长的 tool 结果和 assistant 内容。
        这样保证消息序列的连续性, 避免 LLM API 报错。
        """
        msgs = state.messages
        if not msgs:
            return
        cap = int(getattr(settings, "context_char_budget", 200000) or 0)
        if cap <= 0:
            return
        total = sum(len(str(m.get("content", ""))) for m in msgs)
        if total <= cap:
            return
        # 超出预算: 从最旧的 tool 结果开始截断内容
        # 保留 system prompt 和最近的消息不动
        target = int(cap * 0.7)
        trimmed_any = False
        for m in msgs:
            if total <= target:
                break
            role = m.get("role", "")
            content = m.get("content", "")
            if not content or not isinstance(content, str):
                continue
            if role == "tool":
                # 截断 tool 结果到 2000 字符
                if len(content) > 2000:
                    m["content"] = content[:2000] + "\n...(结果已截断)"
                    total -= len(content) - len(m["content"])
                    trimmed_any = True
            elif role == "assistant" and len(content) > 3000 and not m.get("tool_calls"):
                # 截断长 assistant 消息(不含 tool_calls, 否则会破坏 JSON)
                m["content"] = content[:3000] + "\n...(内容已截断)"
                total -= len(content) - len(m["content"])
                trimmed_any = True
        if trimmed_any:
            logger.info("_trim_state_messages: truncated long messages, ~%d chars remaining", total)

    async def _build_memory_note_async(self, user_input: str, history: list[dict]) -> str:
        """异步检索分层记忆(working/semantic/episodic),构造注入提示块。

        分层记忆开启时按三层注入;失败回退旧的单一 flat 检索。
        """
        if not settings.agent_memory_enabled:
            return ""
        # 修复: 历史里可能含 role=error/plan/tool 等自定义角色,
        # 摘要/检索 LLM 只认 OpenAI 合法角色, 先降级再传入
        from app.memory.session import sanitize_history_for_llm
        history = sanitize_history_for_llm(history)
        # 记忆检索超时保护
        memory_timeout = 30.0
        try:
            from app.memory.layers import build_layered_memory_note, enable_layered

            if enable_layered():
                note = await asyncio.wait_for(
                    build_layered_memory_note(
                        user_input, history, self.session_id, self.llm,
                    ),
                    timeout=memory_timeout
                )
                if note:
                    return note
        except asyncio.TimeoutError:
            logger.warning("记忆检索超时，跳过记忆注入")
        except Exception as e:  # noqa: BLE001
            logger.warning("记忆沉淀主流程失败: %s", e)  # 分层失败回退旧逻辑
        try:
            from app.memory.ltm import get_memory_store

            store = get_memory_store()
            mems = store.search(user_input, top_k=4, count_hit=True)
            if self.session_id:
                seen = {m["id"] for m in mems}
                for m in store.search(
                    user_input, top_k=4, scope="session", session_id=self.session_id, count_hit=True
                ):
                    if m["id"] not in seen:
                        mems.append(m)
                        seen.add(m["id"])
        except Exception as e:  # noqa: BLE001
            logger.warning("长期记忆检索失败，不回退历史上下文: %s", e)
            return ""
        if not mems:
            return ""
        lines = [f"- {m['content']}" for m in mems]
        return "根据长期记忆,以下信息可能与当前对话相关:\n" + "\n".join(lines)

    async def _maybe_auto_memory(self, user_input: str, answer: str, history: list[dict]) -> None:
        """对话结束后自动抽取事实和经验存入长期记忆(默认关闭)。

        不仅抽取事实性信息,还从工具执行步骤中提取成功经验/失败模式。
        """
        if not settings.agent_auto_memory:
            return
        try:
            recent = (history or [])[-_MEMORY_RECENT_HISTORY_COUNT:]
            # 修复: 自动记忆 LLM 只认 OpenAI 合法角色, 先降级 error/plan/孤立tool
            from app.memory.session import sanitize_history_for_llm
            recent = sanitize_history_for_llm(recent)
            prompt = [
                {
                    "role": "system",
                    "content": (
                        "从以下对话中抽取值得长期记住的内容。包括:\n"
                        "1. 事实:用户偏好、个人信息、项目事实、约定(每条不超过30字)\n"
                        "2. 经验:成功的工具调用模式和失败教训(如'使用 search_files 找到目标最快')\n"
                        "若没有值得记住的,返回空数组。\n"
                        '只输出 JSON,格式为 {"facts": ["内容1", "内容2"]}'
                        " facts 数组的每个元素必须是字符串，不能是对象。"
                    ),
                },
                {"role": "user", "content": _clip_history_for_json(recent)},
            ]
            if hasattr(self.llm, "chat_json"):
                data = await self.llm.chat_json(prompt, temperature=0.2)
            else:
                resp = await self.llm.chat(prompt, temperature=0.2)
                data = self._try_parse_json(resp)
            facts = data.get("facts", []) if isinstance(data, dict) else []
            # 规范化 facts: 可能是字符串列表，也可能是对象列表
            normalized_facts = []
            for f in facts:
                if isinstance(f, str) and f.strip():
                    normalized_facts.append(f.strip())
                elif isinstance(f, dict):
                    # 对象格式: 合并所有字段为字符串
                    parts = []
                    for k, v in f.items():
                        if v and str(v).strip():
                            parts.append(f"{k}: {v}")
                    if parts:
                        normalized_facts.append("、".join(parts))
            if normalized_facts:
                from app.memory.ltm import get_memory_store

                store = get_memory_store()
                max_facts = int(settings.agent_auto_memory_max_facts or 5)
                for f in normalized_facts[:max_facts]:
                    if isinstance(f, str) and f.strip():
                        store.save(f.strip(), source="auto")
                # 分层记忆写侧:同一事实同时写入 episodic(本会话情节作用域),
                # 与 layers._add_episodic 的检索口径对齐。
                if settings.agent_layered_memory and self.session_id:
                    max_episodic = int(settings.agent_auto_memory_max_episodic or 3)
                    for f in normalized_facts[:max_episodic]:
                        if isinstance(f, str) and f.strip():
                            try:
                                store.save(
                                    f.strip(),
                                    scope="session",
                                    session_id=self.session_id,
                                    source="auto",
                                )
                            except Exception as e:  # noqa: BLE001
                                logger.warning("会话记忆保存失败: %s", e)
        except Exception as e:  # noqa: BLE001
            logger.warning("记忆沉淀主流程失败: %s", e)

    @staticmethod
    def _try_parse_json(text: str):
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None

    # ---- 消息构造 ----

    def with_multimodal(self, parts: list[dict]) -> "Agent":
        """注入多模态内容块(OpenAI content parts),随后随用户消息一起送 LLM。"""
        self.multimodal_parts = list(parts or [])
        return self

    def _build_user_content(self, user_input: str) -> str | list[dict]:
        """构造最终用户消息 content:纯文本或 content parts 数组。

        视觉可用时返回 [text, image_url...];模型不支持视觉时把图片降级为
        文本提示,不让模型静默丢失图片信息。
        """
        if not self.multimodal_parts:
            return user_input
        model = self.llm.info.get("model", "")
        images = [p for p in self.multimodal_parts if p.get("type") == "image_url"]
        from app.core.multimodal import model_supports_vision

        if not model_supports_vision(model) or not settings.multimodal_vision_enabled:
            n = len(images)
            from app.core.multimodal import vision_notice

            notice = vision_notice(model, n) if n else ""
            text_parts = [p.get("text", "") for p in self.multimodal_parts if p.get("type") == "text"]
            text = "\n".join(t for t in text_parts if t)
            combined = (user_input.strip() + "\n" if user_input.strip() else "") + notice
            if text:
                combined = combined + "\n" + text
            return combined or user_input
        parts: list[dict] = [{"type": "text", "text": user_input}]
        for p in self.multimodal_parts:
            if p.get("type") in ("text", "image_url", "input_audio"):
                parts.append(p)
        return parts

    def _build_messages(self, user_input: str, history: list[dict]) -> list[dict]:
        schemas = self._schemas
        tools_desc = build_tools_description(schemas)
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        system = build_system_prompt(tools_desc, now_str, custom=self.system_prompt)

        memory_note = self._memory_note
        if memory_note:
            system += "\n\n" + memory_note

        if self._plan is not None:
            system += (
                "\n\n当前任务执行计划(请严格按步骤推进:每完成一步用 update_plan 标记进度,"
                "发现计划不合适应立即用 set_plan 修订):\n" + self._plan.summary()
            )

        messages: list[dict] = [{"role": "system", "content": system}]

        # 修复"messages[i].role: unknown variant 'error'"崩溃:
        # 会话历史里存了 role=error/plan/tool 等自定义角色(前端错误气泡/工具记录),
        # OpenAI 只接受 system/user/assistant/tool/function/developer。
        # 转换时: 自定义角色一律降级为 user, 并剥掉 kind/id/timestamp/meta 等非 OpenAI 字段。
        #
        # 修复"missing field 'tool_call_id'"崩溃(OpenAI 网关 400):
        # 历史里 role=tool 的消息(来自 chat_resume 等路径存库)不带 tool_call_id,
        # 且对应的 assistant.tool_calls 消息从未入库 → 孤立的 tool 消息永远不合法。
        # 策略: 只有当 tool 消息自带 tool_call_id 时才能保留 tool 角色,
        # 否则一律降级为 user(包上"工具 xxx 执行结果"前缀),彻底避免 400。
        def _to_llm_msg(msg: dict) -> Optional[dict]:
            # 统一走 session 模块的清洗逻辑(避免与记忆链路的清洗规则分叉)
            from app.memory.session import sanitize_history_for_llm

            converted = sanitize_history_for_llm([msg])
            if not converted:
                return None
            conv = converted[0]
            content = str(conv.get("content", ""))
            if len(content) > settings.message_char_cap:
                conv["content"] = content[: settings.message_char_cap] + "\n...[内容过长已截断]"
            return conv

        if settings.agent_token_budget and settings.agent_token_budget > 0:
            from app.memory.context import estimate_tokens

            head_tokens = estimate_tokens(system)
            budget = int(settings.agent_token_budget)
            from app.memory.context import budget_messages

            # 关键修复(2026-10-01):先按时间戳排序再裁剪。
            # 流式路径的 assistant 占位消息在"首个 token"时创建(chat.py),
            # 位置排在本轮几十条工具消息之前;update_message 结束时只改内容/时间戳
            # 不改位置 → "最新的最终回答"在列表顺序上反而早于本轮工具消息。
            # budget_messages 从尾部保留,会把最新回答当旧消息裁掉 → 模型上下文里
            # 一条 assistant 都没有("继续分析"实测如此)→ 模型失去对话连续性,
            # 退化输出成排重复的过渡语(实证 12:41 / 12:44 两次)。
            # 时间戳是可靠的:update_message 每次保存都会刷新为当前时间,
            # 排序后回答自然落在本轮工具消息之后。稳定排序,时间戳相同保持原序。
            history = sorted(
                history,
                key=lambda m: m.get("timestamp") if isinstance(m.get("timestamp"), (int, float)) else 0,
            )
            # 兼容新旧格式:先统一转成 OpenAI 合法消息,再按 token 预算裁剪
            clipped = []
            for msg in history:
                conv = _to_llm_msg(msg)
                if conv is None:
                    continue
                clipped.append(conv)
            messages += budget_messages(clipped, max_tokens=budget, head_tokens=head_tokens, min_keep=4)
        else:
            history = sorted(
                history,
                key=lambda m: m.get("timestamp") if isinstance(m.get("timestamp"), (int, float)) else 0,
            )
            total_chars = len(system)
            budget = settings.context_char_budget
            for msg in history:
                conv = _to_llm_msg(msg)
                if conv is None:
                    continue  # 跳过空内容/无法转换的消息
                total_chars += len(str(conv.get("content", ""))) + len(conv.get("role", "")) + 2
                if total_chars > budget:
                    break
                messages.append(conv)
        messages.append({"role": "user", "content": self._build_user_content(user_input)})

        # 最终安全网: 校验完整消息列表, 剥离任何残余的非法 tool 消息
        # (tool 角色必须带 tool_call_id, 且前面需有 assistant.tool_calls; 否则降级为 user)
        # 收集所有已声明的 tool_call_id (来自 assistant.tool_calls 消息)
        _known_tool_call_ids: set[str] = set()
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    tc_id = tc.get("id", "") if isinstance(tc, dict) else ""
                    if tc_id:
                        _known_tool_call_ids.add(tc_id)
        for i, m in enumerate(messages):
            if m.get("role") == "tool":
                tc_id = m.get("tool_call_id", "")
                # 缺 tool_call_id, 或 tool_call_id 找不到对应的 assistant.tool_calls → 降级为 user
                if not tc_id or tc_id not in _known_tool_call_ids:
                    logger.warning(
                        "[build_messages] 消息 #%d role=tool 无合法 tool_call_id (%r), 降级为 user",
                        i, tc_id,
                    )
                    m["role"] = "user"
                    _content = m.get("content", "")
                    if not _content.startswith("["):
                        m["content"] = "[工具执行结果(历史)]: " + _content
                    # 移除非法的 tool_call_id 字段
                    m.pop("tool_call_id", None)
        return messages

    @staticmethod
    def _parse_tool_call(text: str) -> Optional[tuple[str, str]]:
        """从 LLM 回复中解析工具调用 JSON(单条)。

        注意:此方法仅解析第一条工具调用。如果 LLM 返回多条调用,
        应使用 _parse_tool_calls 解析全部。
        """
        if not text or not text.strip():
            return None
        # 尝试匹配 JSON 数组格式 [...],提取第一条
        stripped = text.strip()
        if stripped.startswith('['):
            try:
                data = json.loads(stripped)
                if isinstance(data, list) and data:
                    first = data[0]
                    if isinstance(first, dict) and "tool" in first:
                        return first["tool"], json.dumps(first.get("arguments", {}), ensure_ascii=False)
            except json.JSONDecodeError:
                pass
            # 数组解析失败,继续尝试单条匹配
        # 尝试匹配单条 JSON 对象格式 {...}
        m = re.search(TOOL_CALL_PATTERN, text, re.DOTALL)
        if m:
            return m.group(1), m.group(2)
        # 尝试宽松解析: 独立 JSON 对象含 tool 字段
        try:
            data = json.loads(stripped)
            if isinstance(data, dict) and "tool" in data:
                args = data.get("arguments", {})
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                return data["tool"], args
        except json.JSONDecodeError:
            pass
        return None

    @staticmethod
    def _extract_balanced_json(text: str, start: int) -> Optional[str]:
        """从 text[start] 起提取一个平衡的 JSON 对象字符串(花括号配对)。

        用于替代正则贪婪匹配, 正确处理参数值中含嵌套 {} 或引号字符串内含花括号的场景。
        返回提取的 JSON 字符串(含最外层花括号), 失败返回 None。
        """
        if start >= len(text) or text[start] != "{":
            return None
        depth = 0
        in_string = False
        escape = False
        i = start
        while i < len(text):
            ch = text[i]
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start: i + 1]
            i += 1
        return None

    @staticmethod
    def _parse_tool_calls(text: str) -> list[ToolCall]:
        """从 LLM 回复中解析所有工具调用 JSON(支持单条和数组格式)。

        改进: 正则匹配失败时改用平衡花括号扫描提取 arguments 值, 避免参数
        嵌套 JSON 导致贪婪匹配错位或参数丢失。
        """
        if not text or not text.strip():
            return []
        stripped = text.strip()
        calls: list[ToolCall] = []
        # 尝试解析 JSON 数组
        if stripped.startswith('['):
            try:
                data = json.loads(stripped)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "tool" in item:
                            args = item.get("arguments", {})
                            if isinstance(args, dict):
                                args = json.dumps(args, ensure_ascii=False)
                            calls.append(ToolCall(id="", name=item["tool"], arguments=args))
                    if calls:
                        return calls
            except json.JSONDecodeError:
                pass
        # 正则逐块定位 {"tool": ..., "arguments": 起点, 再用平衡扫描提取完整 arguments JSON
        for m in re.finditer(r'\{\s*"tool"\s*:\s*"([^"]+)"\s*,\s*"arguments"\s*:', text):
            arg_str = Agent._extract_balanced_json(text, m.end())
            if arg_str is not None:
                calls.append(ToolCall(id="", name=m.group(1), arguments=arg_str))
        # 回退: 若平衡扫描未命中(非标准格式), 尝试原 TOOL_CALL_PATTERN 有限嵌套匹配
        if not calls:
            for m in re.finditer(TOOL_CALL_PATTERN, text, re.DOTALL):
                calls.append(ToolCall(id="", name=m.group(1), arguments=m.group(2)))
        # 尝试宽松解析单条
        if not calls:
            parsed = Agent._parse_tool_call(text)
            if parsed:
                calls.append(ToolCall(id="", name=parsed[0], arguments=parsed[1]))
        return calls
