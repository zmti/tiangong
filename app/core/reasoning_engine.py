"""增强型链式推理引擎:CoT/ToT/GoT 多种推理策略。

对标 Claude 的链式推理能力:
- Chain-of-Thought (CoT): 逐步推理
- Tree-of-Thought (ToT): 多分支探索
- Graph-of-Thought (GoT): 图结构推理
- Self-Consistency: 多次采样取众数
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings
from app.core.llm import LLMClient
from app.tools.registry import tool
from pydantic import Field


# ── 推理策略枚举 ──────────────────────────────────────────────────
REASONING_STRATEGIES = ("cot", "tot", "got", "self_consistent")


@dataclass
class ThoughtNode:
    """推理树中的一个节点。"""
    id: str
    content: str
    score: float = 0.0
    children: list["ThoughtNode"] = field(default_factory=list)
    parent_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "score": self.score,
            "child_count": len(self.children),
            "metadata": self.metadata,
        }


@dataclass
class ReasoningResult:
    """推理结果。"""
    strategy: str = ""
    final_answer: str = ""
    thought_trace: list[dict] = field(default_factory=list)
    nodes_explored: int = 0
    confidence: float = 0.0
    duration_seconds: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "final_answer": self.final_answer,
            "thought_trace": self.thought_trace,
            "nodes_explored": self.nodes_explored,
            "confidence": round(self.confidence, 3),
            "duration_s": round(self.duration_seconds, 2),
            "error": self.error,
        }


# ── Chain-of-Thought 推理 ──────────────────────────────────────────
COT_PROMPT = """你是推理专家。请按步骤思考并给出最终答案。

请按以下格式输出:
1. 首先理解问题的核心
2. 逐步分析每个关键环节
3. 考虑可能的边界情况
4. 得出最终结论

问题: {question}

请逐步推理:
"""


async def reason_cot(llm: LLMClient, question: str, max_steps: int = 10) -> ReasoningResult:
    """Chain-of-Thought 逐步推理。"""
    t0 = time.time()
    prompt = COT_PROMPT.format(question=question[:2000])

    try:
        answer = await llm.chat([
            {"role": "system", "content": "你是一个擅长逻辑推理的助手。请一步一步思考，给出详细推理过程和最终答案。"},
            {"role": "user", "content": prompt},
        ], temperature=0.3)
    except Exception as e:
        return ReasoningResult(strategy="cot", error=str(e), duration_seconds=time.time() - t0)

    # 提取推理步骤
    steps = []
    for line in answer.split("\n"):
        line = line.strip()
        if line and any(c.isdigit() for c in line[:3]):
            steps.append(line[:200])

    return ReasoningResult(
        strategy="cot",
        final_answer=answer,
        thought_trace=[{"step": i, "content": s} for i, s in enumerate(steps)],
        nodes_explored=len(steps),
        duration_seconds=time.time() - t0,
    )


# ── Tree-of-Thought 推理 ───────────────────────────────────────────
TOT_PROMPT = """你是推理专家。对于这个问题，请给出 {k} 个不同的解决思路，并评估每个思路的优劣。

问题: {question}

请以 JSON 格式输出:
{{
  "thoughts": [
    {{"id": 1, "approach": "思路描述", "pros": ["优点1"], "cons": ["缺点1"], "score": 0.8}},
    ...
  ],
  "best_approach": 1,
  "reasoning": "为什么选择这个思路"
}}
"""


async def reason_tot(
    llm: LLMClient,
    question: str,
    k: int = 3,
    max_rounds: int = 3,
) -> ReasoningResult:
    """Tree-of-Thought 多分支探索推理。"""
    t0 = time.time()
    all_thoughts: list[ThoughtNode] = []
    current_question = question

    for round_i in range(max_rounds):
        prompt = TOT_PROMPT.format(question=current_question, k=k)
        try:
            if hasattr(llm, "chat_json"):
                data = await llm.chat_json([
                    {"role": "system", "content": "请严格按 JSON 格式输出，不要其他文字。"},
                    {"role": "user", "content": prompt},
                ], temperature=0.5)
            else:
                resp = await llm.chat([
                    {"role": "system", "content": "请严格按 JSON 格式输出。"},
                    {"role": "user", "content": prompt},
                ], temperature=0.5)
                data = _parse_json(resp)
        except Exception:
            break

        if not isinstance(data, dict) or "thoughts" not in data:
            break

        thoughts = data.get("thoughts", [])
        best_id = data.get("best_approach", 1)

        for t in thoughts:
            node = ThoughtNode(
                id=f"r{round_i}_t{t.get('id', 0)}",
                content=t.get("approach", ""),
                score=t.get("score", 0.5),
                metadata={"pros": t.get("pros", []), "cons": t.get("cons", [])},
            )
            all_thoughts.append(node)

        # 选择最佳思路深入
        best = next((t for t in thoughts if t.get("id") == best_id), thoughts[0] if thoughts else None)
        if best:
            current_question = f"基于思路「{best.get('approach', '')}」，请详细展开推理过程，给出具体步骤和最终答案。"

    # 综合所有思路得出结论
    if all_thoughts:
        all_thoughts.sort(key=lambda x: x.score, reverse=True)
        top_nodes = all_thoughts[:3]
        combined = "\n".join(f"{i+1}. [{n.score:.2f}] {n.content}" for i, n in enumerate(top_nodes))
        reasoning = data.get("reasoning", "") if isinstance(data, dict) else ""

        try:
            final = await llm.chat([
                {"role": "system", "content": "请综合以下多个推理思路，给出最终结论。"},
                {"role": "user", "content": f"原始问题: {question}\n\n推理思路:\n{combined}\n\n已有分析: {reasoning}\n\n请给出最终答案。"},
            ], temperature=0.2)
        except Exception:
            final = top_nodes[0].content if top_nodes else ""

        return ReasoningResult(
            strategy="tot",
            final_answer=final,
            thought_trace=[n.to_dict() for n in all_thoughts],
            nodes_explored=len(all_thoughts),
            confidence=max(n.score for n in all_thoughts) if all_thoughts else 0.0,
            duration_seconds=time.time() - t0,
        )

    return ReasoningResult(strategy="tot", error="ToT 推理失败", duration_seconds=time.time() - t0)


# ── Self-Consistency 推理 ──────────────────────────────────────────
async def reason_self_consistent(
    llm: LLMClient,
    question: str,
    n_samples: int = 5,
) -> ReasoningResult:
    """Self-Consistency: 多次采样取众数答案。"""
    t0 = time.time()
    answers: list[str] = []

    for i in range(n_samples):
        try:
            ans = await llm.chat([
                {"role": "system", "content": "请仔细思考后给出答案。每种采样都要独立推理。"},
                {"role": "user", "content": question},
            ], temperature=0.7)
            answers.append(ans.strip())
        except Exception:
            continue

    if not answers:
        return ReasoningResult(strategy="self_consistent", error="所有采样失败", duration_seconds=time.time() - t0)

    # 找众数
    from collections import Counter
    # 简化:按首句分组
    def _key(a):
        first_line = a.split("\n")[0][:50]
        return first_line

    counts = Counter(_key(a) for a in answers)
    most_common_key = counts.most_common(1)[0][0]
    consensus_answers = [a for a in answers if _key(a) == most_common_key]

    # 用更多样本综合
    if len(consensus_answers) >= 2:
        combined = "\n\n".join(consensus_answers)
        try:
            final = await llm.chat([
                {"role": "system", "content": "以下是多个独立推理的结果，请综合给出最可靠的答案。"},
                {"role": "user", "content": f"问题: {question}\n\n推理结果:\n{combined}"},
            ], temperature=0.2)
        except Exception:
            final = consensus_answers[0]
    else:
        final = answers[0]

    return ReasoningResult(
        strategy="self_consistent",
        final_answer=final,
        thought_trace=[{"sample": i, "answer_preview": a[:100]} for i, a in enumerate(answers)],
        nodes_explored=n_samples,
        confidence=len(consensus_answers) / len(answers) if answers else 0.0,
        duration_seconds=time.time() - t0,
    )


# ── 统一推理入口 ──────────────────────────────────────────────────
async def reason(
    question: str,
    strategy: str = "cot",
    llm: Optional[LLMClient] = None,
    **kwargs,
) -> ReasoningResult:
    """统一推理入口，支持多种策略。"""
    llm = llm or LLMClient()
    strategy = (strategy or "cot").lower()

    if strategy == "cot":
        return await reason_cot(llm, question, **kwargs)
    elif strategy == "tot":
        return await reason_tot(llm, question, **kwargs)
    elif strategy == "self_consistent":
        return await reason_self_consistent(llm, question, **kwargs)
    elif strategy == "got":
        # GoT 用 ToT 近似
        return await reason_tot(llm, question, **kwargs)
    else:
        return ReasoningResult(strategy=strategy, error=f"未知的推理策略: {strategy}")


def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


# ── 工具注册 ──────────────────────────────────────────────────────
@tool(
    "chain_of_thought",
    "链式推理:逐步分析问题，给出详细的推理过程和最终答案",
    timeout=120,
)
def chain_of_thought(
    question: str = Field(description="需要推理的问题"),
    strategy: str = Field("cot", description="推理策略: cot=逐步推理(tot=多分支探索, self_consistent=多次采样), 默认 cot"),
    max_steps: int = Field(10, description="最大推理步数(cot 策略)"),
    n_branches: int = Field(3, description="ToT 分支数"),
    n_samples: int = Field(5, description="Self-Consistency 采样数"),
) -> dict:
    """链式推理工具:支持多种推理策略。"""
    import asyncio as _asyncio
    kwargs = {}
    if strategy == "cot":
        kwargs["max_steps"] = max_steps
    elif strategy == "tot":
        kwargs["k"] = n_branches
        kwargs["max_rounds"] = max(2, min(max_steps // 3, 5))
    elif strategy == "self_consistent":
        kwargs["n_samples"] = n_samples

    result = _asyncio.run(reason(question, strategy, **kwargs))
    return result.to_dict()
