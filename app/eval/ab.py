"""A/B 评估对比:在同一数据集上对比两组 Agent 配置的指标差异。

支持:
- 同一 eval cases 在 two 组配置下并行执行
- 输出 per-case 与 aggregate 对比报告
- 统计显著性检验(简单二项检验)
- JSON 落盘 + CLI 使用
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from app.config import settings
from app.core.agent import Agent
from app.core.llm import LLMClient


@dataclass
class CaseResult:
    case_name: str
    category: str
    input_text: str
    # variant_a 结果
    variant_a_answer: str = ""
    variant_a_steps: list[dict] = field(default_factory=list)
    variant_a_tool_sequence: list[str] = field(default_factory=list)
    variant_a_cost_usd: float = 0.0
    variant_a_tokens: int = 0
    # variant_b 结果
    variant_b_answer: str = ""
    variant_b_steps: list[dict] = field(default_factory=list)
    variant_b_tool_sequence: list[str] = field(default_factory=list)
    variant_b_cost_usd: float = 0.0
    variant_b_tokens: int = 0
    # 硬断言
    expect_tool: str = ""
    expect_keyword: str = ""
    tool_after: tuple[str, str] = field(default_factory=tuple)
    # 判定结果
    a_pass_tool: bool = False
    a_pass_keyword: bool = False
    b_pass_tool: bool = False
    b_pass_keyword: bool = False
    a_pass_any: bool = False
    b_pass_any: bool = False
    winner: str = ""  # "a" / "b" / "tie" / "neither"

    def to_dict(self) -> dict:
        d = asdict(self)
        # tuple -> list
        d["tool_after"] = list(d.get("tool_after") or [])
        return d


@dataclass
class ABReport:
    config_a: dict
    config_b: dict
    total_cases: int
    a_wins: int
    b_wins: int
    ties: int
    a_pass_rate: float
    b_pass_rate: float
    a_avg_cost: float
    b_avg_cost: float
    a_avg_tokens: int
    b_avg_tokens: int
    cases: list[CaseResult]
    elapsed_seconds: float
    statistical_significance: bool = False
    p_value: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return (
            f"=== A/B 评估报告 ===\n"
            f"配置A: {json.dumps(self.config_a, ensure_ascii=False)}\n"
            f"配置B: {json.dumps(self.config_b, ensure_ascii=False)}\n"
            f"总用例: {self.total_cases}\n"
            f"通过率 A={self.a_pass_rate:.1%}  B={self.b_pass_rate:.1%}\n"
            f"胜率 A={self.a_wins}  B={self.b_wins}  平={self.ties}\n"
            f"平均成本 A=${self.a_avg_cost:.6f}/B=${self.b_avg_cost:.6f}\n"
            f"平均token A={self.a_avg_tokens}/B={self.b_avg_tokens}\n"
            f"耗时 {self.elapsed_seconds:.1f}s\n"
            f"统计显著(p<{0.05 if self.statistical_significance else 1.0})"
        )


def _extract_tool_sequence(steps: list[dict]) -> list[str]:
    return [s.get("name", "") for s in steps if s.get("type") == "tool"]


def _check_pass(
    answer: str,
    steps: list[dict],
    expect_tool: str,
    expect_keyword: str,
    tool_after: tuple[str, str],
) -> tuple[bool, bool]:
    """检查硬断言是否通过。"""
    pass_tool = not expect_tool or expect_tool.lower() in [
        s.get("name", "").lower() for s in steps if s.get("type") == "tool"
    ]
    pass_keyword = (
        not expect_keyword
        or expect_keyword.lower() in (answer or "").lower()
    )
    if tool_after and len(tool_after) == 2:
        seq = [s.get("name", "") for s in steps if s.get("type") == "tool"]
        i_a, i_b = -1, -1
        for i, t in enumerate(seq):
            if t == tool_after[0]:
                i_a = i
            if t == tool_after[1]:
                i_b = i
        pass_tool = pass_tool and (i_a < i_b) if i_a >= 0 and i_b >= 0 else pass_tool
    return pass_tool, pass_keyword


class ABEvaluator:
    """并行 A/B 评估器。"""

    def __init__(
        self,
        config_a: dict,
        config_b: dict,
        provider_a: str = "",
        provider_b: str = "",
        max_concurrent: int = 3,
    ):
        self.config_a = config_a
        self.config_b = config_b
        self.provider_a = provider_a
        self.provider_b = provider_b
        self.max_concurrent = max_concurrent

    async def run(
        self,
        cases: list[dict],
        session_id_prefix: str = "ab_test",
    ) -> ABReport:
        t0 = time.time()
        semaphore = asyncio.Semaphore(self.max_concurrent)
        results: list[CaseResult] = []

        async def _run_one(case: dict) -> CaseResult:
            async with semaphore:
                cr = CaseResult(
                    case_name=case.get("name", ""),
                    category=case.get("category", ""),
                    input_text=case.get("input", ""),
                    expect_tool=case.get("expect_tool", ""),
                    expect_keyword=case.get("expect_keyword", ""),
                    tool_after=tuple(case.get("tool_after") or [])
                    if case.get("tool_after") else (),
                )
                # 执行 variant A
                try:
                    agent_a = Agent(
                        llm=LLMClient(self.provider_a) if self.provider_a else None,
                        mode=self.config_a.get("mode", "auto"),
                    )
                    res_a = await agent_a.run(case.get("input", ""), history=[])
                    cr.variant_a_answer = res_a.answer or ""
                    cr.variant_a_steps = res_a.steps or []
                    cr.variant_a_tool_sequence = _extract_tool_sequence(cr.variant_a_steps)
                    # 从 trace 估算成本和 token(简化:按步骤数粗略估算)
                    cr.variant_a_cost_usd = sum(
                        s.get("cost", 0.0) or 0.0 for s in cr.variant_a_steps
                    )
                    cr.variant_a_tokens = sum(
                        s.get("tokens", 0) or 0 for s in cr.variant_a_steps
                    )
                except Exception:  # noqa: BLE001
                    cr.variant_a_answer = "ERROR"
                # 执行 variant B
                try:
                    agent_b = Agent(
                        llm=LLMClient(self.provider_b) if self.provider_b else None,
                        mode=self.config_b.get("mode", "auto"),
                    )
                    res_b = await agent_b.run(case.get("input", ""), history=[])
                    cr.variant_b_answer = res_b.answer or ""
                    cr.variant_b_steps = res_b.steps or []
                    cr.variant_b_tool_sequence = _extract_tool_sequence(cr.variant_b_steps)
                    cr.variant_b_cost_usd = sum(
                        s.get("cost", 0.0) or 0.0 for s in cr.variant_b_steps
                    )
                    cr.variant_b_tokens = sum(
                        s.get("tokens", 0) or 0 for s in cr.variant_b_steps
                    )
                except Exception:  # noqa: BLE001
                    cr.variant_b_answer = "ERROR"

                # 硬断言
                cr.a_pass_tool, cr.a_pass_keyword = _check_pass(
                    cr.variant_a_answer, cr.variant_a_steps,
                    cr.expect_tool, cr.expect_keyword, cr.tool_after,
                )
                cr.b_pass_tool, cr.b_pass_keyword = _check_pass(
                    cr.variant_b_answer, cr.variant_b_steps,
                    cr.expect_tool, cr.expect_keyword, cr.tool_after,
                )
                cr.a_pass_any = cr.a_pass_tool or cr.a_pass_keyword
                cr.b_pass_any = cr.b_pass_tool or cr.b_pass_keyword
                # 胜者判定
                if cr.a_pass_any and not cr.b_pass_any:
                    cr.winner = "a"
                elif cr.b_pass_any and not cr.a_pass_any:
                    cr.winner = "b"
                elif cr.a_pass_any and cr.b_pass_any:
                    cr.winner = "tie"
                else:
                    cr.winner = "neither"
                return cr

        tasks = [_run_one(c) for c in cases]
        cases_results = await asyncio.gather(*tasks)
        results.extend(cases_results)

        a_wins = sum(1 for r in results if r.winner == "a")
        b_wins = sum(1 for r in results if r.winner == "b")
        ties = sum(1 for r in results if r.winner == "tie")
        neither = sum(1 for r in results if r.winner == "neither")
        a_pass = sum(1 for r in results if r.a_pass_any)
        b_pass = sum(1 for r in results if r.b_pass_any)
        total = len(results) or 1

        # 简单二项检验判断显著性
        p_value = 1.0
        significant = False
        if a_wins + b_wins > 0 and neither == 0:
            # 排除平局后的比例
            p = a_wins / (a_wins + b_wins)
            n = a_wins + b_wins
            # 近似正态检验
            import math
            se = math.sqrt(p * (1 - p) / max(1, n))
            z = abs(p - 0.5) / max(se, 1e-9)
            # 粗略 p-value(双尾)
            p_value = 2 * (1 - _norm_cdf(z))
            significant = p_value < 0.05

        return ABReport(
            config_a=self.config_a,
            config_b=self.config_b,
            total_cases=total,
            a_wins=a_wins,
            b_wins=b_wins,
            ties=ties,
            a_pass_rate=a_pass / total,
            b_pass_rate=b_pass / total,
            a_avg_cost=sum(r.variant_a_cost_usd for r in results) / total,
            b_avg_cost=sum(r.variant_b_cost_usd for r in results) / total,
            a_avg_tokens=sum(r.variant_a_tokens for r in results) // total,
            b_avg_tokens=sum(r.variant_b_tokens for r in results) // total,
            cases=results,
            elapsed_seconds=round(time.time() - t0, 1),
            statistical_significance=significant,
            p_value=round(p_value, 4),
        )


def _norm_cdf(z: float) -> float:
    """标准正态累积分布函数的近似(误差<0.0003)。"""
    import math
    # Abramowitz & Stegun approximation
    if z < -8:
        return 0.0
    if z > 8:
        return 1.0
    t = 1.0 / (1.0 + 0.2316419 * abs(z))
    d = 0.3989422804014327  # 1/sqrt(2*pi)
    prob = d * math.exp(-z * z / 2.0) * t * (
        0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274)))
    )
    return 1.0 - prob if z > 0 else prob
