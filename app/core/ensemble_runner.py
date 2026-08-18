"""Ensemble 运行器:负责多模型并行执行+评审选优。

从 Agent 类中提取,降低主类职责。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from app.config import settings

if TYPE_CHECKING:
    from app.core.llm import LLMClient
    from app.tools.registry import ToolRegistry


class EnsembleRunner:
    """Ensemble 运行器:多个模型并行做同一任务,评审选优。"""

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        ensemble_models: Optional[list[str]] = None,
        max_steps: int = 30,
        system_prompt: str = "",
        session_id: str = "",
        critic_llm: Optional[LLMClient] = None,
    ):
        self.llm = llm
        self.registry = registry
        self.ensemble_models = list(ensemble_models or [])
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self.session_id = session_id
        self.critic_llm = critic_llm

    def _ensemble_specs(self) -> list[tuple[str, str]]:
        """解析参与 ensemble 的模型规格列表。"""
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

    async def run(self, user_input: str, history: list[dict]):
        """多个模型并行做同一任务,再用评审模型对全部回答打分选优。"""
        from app.core.llm import LLMClient as _L
        from app.core.agent import Agent, AgentResult

        specs = self._ensemble_specs()
        agents: list[Agent] = []
        for provider, model in specs:
            try:
                llm = _L(provider, model or None)
            except Exception:  # noqa: BLE001
                continue
            sub = Agent(
                llm=llm,
                registry=self.registry,
                max_steps=self.max_steps,
                system_prompt=self.system_prompt,
                session_id=self.session_id,
                mode="single",
                critic_provider=(self.critic_llm.provider if self.critic_llm else ""),
                critic_model=(self.critic_llm.model if self.critic_llm else ""),
            )
            agents.append(sub)

        if not agents:
            from app.core.agent import AgentResult
            return AgentResult(answer="ensemble 模式没有可用模型。", steps=[], model=self.llm.info["model"])

        try:
            results = await asyncio.gather(*[a.run(user_input, history) for a in agents])
        except Exception as e:  # noqa: BLE001
            from app.core.agent import AgentResult
            return AgentResult(answer=f"ensemble 执行失败: {e}", steps=[], model=self.llm.info["model"])

        steps: list[dict] = []
        for i, (spec, res) in enumerate(zip(specs, results)):
            steps.append({
                "step": i + 1,
                "type": "ensemble_member",
                "provider": spec[0],
                "model": spec[1] or res.model,
                "answer": res.answer,
                "member_steps": res.steps,
            })
        steps.append({"step": len(specs) + 1, "type": "ensemble_judge"})

        best = await self._judge(user_input, results)
        from app.core.agent import AgentResult
        if best is not None:
            return AgentResult(answer=best.answer, steps=steps, model=best.model)
        return AgentResult(answer=results[0].answer, steps=steps, model=results[0].model)

    async def _judge(self, user_input: str, results: list) -> Optional[object]:
        """对全部候选回答打分,返回最佳结果。"""
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
            verdict = await (self.critic_llm or self.llm).chat_json_schema(
                [
                    {"role": "system", "content": "你是严格的评审者,从多个候选回答中选出质量最佳的一个。"},
                    {"role": "user", "content": prompt},
                ],
                schema,
                temperature=0.2,
                validator=_validator,
            )
        except Exception:  # noqa: BLE001
            return None
        winner = int(verdict.get("winner", 0))
        if not (0 <= winner < len(results)):
            winner = 0
        return results[winner]
