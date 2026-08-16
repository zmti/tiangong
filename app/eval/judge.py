"""LLM-as-Judge 评测:用强模型对 Agent 的回答质量做多维打分。

与 BFCL 式硬断言(工具命中/关键词)互补,衡量"做得好不好"而非"有没有做":
- task_success   :任务是否成功完成(0~1)
- correctness    :答案正确性(1~5)
- faithfulness   :是否基于工具结果/上下文作答,无幻觉(1~5)
- helpfulness    :对用户帮助度、清晰度(1~5)

用法(在 runner 中通过 --judge 开启,或用 judge_result 单独调用)。
失败/模型不可用时降级为 None,不影响原回归断言。
"""
from __future__ import annotations

import json
import time
from typing import Optional

from app.core.llm import LLMClient

JUDGE_PROMPT = """你是严谨的智能体评测员。请根据"任务"、Agent 实际执行步骤与最终回答,给出质量评分。

任务: {task}

执行步骤(工具调用):
{steps}

最终回答:
{answer}

请从以下维度打分:
1. task_success: 任务是否被正确完成(0 或 1)
2. correctness: 回答正确性(1-5,5=完全正确)
3. faithfulness: 是否忠实基于工具结果/提供上下文,无编造(1-5,5=完全忠实)
4. helpfulness: 帮助度与清晰度(1-5)

只输出 JSON,不要任何其他文字:
{{"task_success": 0, "correctness": 4, "faithfulness": 5, "helpfulness": 4, "summary": "一句话理由"}}
"""


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            return None
    return None


async def judge_result(
    case: dict,
    result,
    judge_llm: Optional[LLMClient] = None,
    timeout: int = 60,
) -> Optional[dict]:
    """用 LLM 评判一次执行结果。失败返回 None(不影响原断言)。"""
    try:
        llm = judge_llm or LLMClient()
        steps = []
        for s in (result.steps or [])[:40]:
            if s.get("type") == "tool":
                steps.append(f"- {s.get('tool')}: {str(s.get('arguments', ''))[:80]}")
            elif s.get("type") == "answer":
                steps.append(f"- 回答: {str(s.get('content', ''))[:80]}")
        prompt = [
            {"role": "system", "content": JUDGE_PROMPT.format(
                task=str(case.get("user_input", ""))[:2000],
                steps="\n".join(steps) or "(无工具调用)",
                answer=str(getattr(result, "answer", "") or "")[:4000],
            )},
            {"role": "user", "content": "请评测。"},
        ]
        import asyncio

        if hasattr(llm, "chat_json"):
            data = await asyncio.wait_for(llm.chat_json(prompt, temperature=0.0), timeout=timeout)
        else:
            resp = await asyncio.wait_for(llm.chat(prompt, temperature=0.0), timeout=timeout)
            data = _extract_json(resp)
        if not isinstance(data, dict):
            return None
        data["case_id"] = case.get("id", "")
        data["model"] = getattr(llm, "info", {}).get("model", "")
        return data
    except Exception:  # noqa: BLE001
        return None
