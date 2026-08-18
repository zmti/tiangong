"""反思管理器:负责自评反思(Reflexion)和事实校验(LLM-as-judge)。

从 Agent 类中提取,降低主类职责。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from app.config import settings

if TYPE_CHECKING:
    from app.core.llm import LLMClient
    from app.runtime.state import AgentState


class ReflectManager:
    """反思管理器:统一管理 Reflexion 自评和事实校验。"""

    def __init__(
        self,
        reflect_enabled: bool = True,
        verify_enabled: bool = True,
        temperature: float = 0.2,
    ):
        self.reflect_enabled = reflect_enabled
        self.verify_enabled = verify_enabled
        self.temperature = temperature

    async def node_reflect(
        self, state: AgentState, critic_llm: LLMClient, max_rounds: int = 1,
    ) -> None:
        """自评反思(Reflexion 式):用 LLM 按结构化 schema 评价回答质量。

        不合格时把评审意见写回 messages,设置 reflect_rewrite 标志,
        由路由送回 llm 节点重写;通过则保持现状,路由停止。
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
            f"打分 1-10;若存在需要修正的问题,verdict 返回 rewrite 并列出 issues。"
        )
        try:
            review = await critic_llm.chat_json_schema(
                [
                    {"role": "system", "content": "你是严格但公正的评审者。"},
                    {"role": "user", "content": review_prompt},
                ],
                schema,
                temperature=0.2,
                validator=_validator,
            )
        except Exception:  # noqa: BLE001  # 评审失败视为通过
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
            state.metadata["reflect_rewrite"] = True
            state.messages.append({"role": "user", "content":
                "评审意见:你的回答存在以下问题,请修正后重新回答。\n- "
                + "\n- ".join(str(i) for i in issues)})

    async def node_verify(
        self, state: AgentState, llm: LLMClient, critic_llm: LLMClient,
    ) -> None:
        """最终答案事实校验(LLM-as-judge):核对答案断言是否被工具结果支撑。

        用评审模型检查最终答案与工具结果的一致性:发现未支撑断言/幻觉时,
        直接把工具证据注入并让模型重写一次答案(内联,不走 llm 节点)。
        校验失败/无工具结果/非必要一律通过,不影响主流程。
        """
        if not self.verify_enabled or not state.answer or not state.answer.strip():
            return
        if state.metadata.get("stream"):
            return
        tool_results = [
            s.get("result", "") for s in state.steps
            if s.get("type") == "tool" and s.get("result")
        ]
        if not tool_results:
            return
        if state.metadata.get("verify_rewritten"):
            return

        evidence = "\n\n".join(tool_results)[:4000]
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
            check = await critic_llm.chat_json_schema(
                [
                    {"role": "system", "content": "你是严格但公平的事实核查员。"},
                    {"role": "user", "content": prompt},
                ],
                schema,
                temperature=0.1,
                validator=_validator,
            )
        except Exception:  # noqa: BLE001  # 校验失败视为通过
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
                f"你的上一版回答包含未被证据支撑的断言,请只依据下面的工具结果重写回答:\n"
                f"未支撑断言:\n- " + "\n- ".join(str(u) for u in unsupported) + "\n\n"
                f"可用证据:\n{evidence}\n\n"
                f"重写要求:删除或修正所有无法由证据支撑的表述,不引入证据之外的新信息。"
                f"用户请求: {state.user_input}"
            )
            try:
                new_answer = await llm.chat(
                    [{"role": "user", "content": rewrite_prompt}],
                    temperature=0.2,
                )
            except Exception:  # noqa: BLE001
                return
            state.answer = new_answer
            state.steps.append({
                "step": sum(1 for s in state.steps if s.get("type") == "verify_rewrite") + 1,
                "type": "verify_rewrite",
                "content": new_answer,
            })
            state.emit({"type": "answer", "content": new_answer, "source": "verify"})
