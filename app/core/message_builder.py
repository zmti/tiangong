"""消息构造器:负责构建 LLM 消息和用户消息。

从 Agent 类中提取,降低主类职责。
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Optional

from app.config import settings
from app.core.prompts import build_system_prompt, build_tools_description

if TYPE_CHECKING:
    from app.core.llm import LLMClient
    from app.core.planner import TaskPlan


class MessageBuilder:
    """消息构造器:统一管理 system prompt 构建、消息裁剪、用户消息构造。"""

    def __init__(
        self,
        llm: LLMClient,
        system_prompt: str = "",
        token_budget: int = 0,
        context_budget: int = 0,
    ):
        self.llm = llm
        self.system_prompt = system_prompt
        self.token_budget = token_budget
        self.context_budget = context_budget

    def build_system_message(
        self,
        schemas: list[dict],
        memory_note: str = "",
        failure_note: str = "",
        success_note: str = "",
        profile_hint: str = "",
        skill_descs: str = "",
        plan: Optional[TaskPlan] = None,
        feedback_hint: str = "",
        fewshot_hint: str = "",
    ) -> str:
        """构建完整的 system prompt。"""
        tools_desc = build_tools_description(schemas)
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        system = build_system_prompt(tools_desc, now_str, custom=self.system_prompt)

        if memory_note:
            system += "\n\n" + memory_note
        if failure_note:
            system += "\n\n" + failure_note
        if success_note:
            system += "\n\n" + success_note
        if profile_hint:
            system += "\n\n" + profile_hint
        if feedback_hint:
            system += "\n\n" + feedback_hint
        if fewshot_hint:
            system += "\n\n" + fewshot_hint
        if skill_descs:
            system += f"\n\n## 可用高级技能(可一次性完成多步操作)\n{skill_descs}"
        if plan is not None:
            system += (
                "\n\n当前任务执行计划(请严格按步骤推进:每完成一步用 update_plan 标记进度,"
                "发现计划不合适应立即用 set_plan 修订):\n" + plan.summary()
            )
        return system

    def build_messages(
        self,
        system: str,
        history: list[dict],
        user_input: str,
        multimodal_parts: Optional[list[dict]] = None,
    ) -> list[dict]:
        """构建完整的 messages 列表(含历史裁剪)。"""
        messages: list[dict] = [{"role": "system", "content": system}]
        if self.token_budget and self.token_budget > 0:
            from app.memory.context import estimate_tokens, budget_messages

            head_tokens = estimate_tokens(system)
            clipped = [{
                **msg,
                "content": (
                    content[: settings.message_char_cap] + "\n...[内容过长已截断]"
                    if isinstance(content, str) and len(content) > settings.message_char_cap
                    else content
                ),
            } for msg in history if msg.get("role") not in ("tool", "plan")]
            messages += budget_messages(clipped, max_tokens=self.token_budget, head_tokens=head_tokens, min_keep=4)
        else:
            total_chars = len(system)
            for msg in history:
                if msg.get("role") in ("tool", "plan"):
                    continue
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > settings.message_char_cap:
                    content = content[: settings.message_char_cap] + "\n...[内容过长已截断]"
                line = f"{msg.get('role', '')}:{content}"
                if total_chars + len(line) > self.context_budget:
                    break
                messages.append({**msg, "content": content})
                total_chars += len(line)
        # A1 修复: 传递模型名用于 vision 检测，避免空字符串导致全部图片被降级
        model_name = getattr(self.llm, "info", {}).get("model", "")
        messages.append({"role": "user", "content": self._build_user_content(user_input, multimodal_parts, model_name)})
        return messages

    def _build_user_content(
        self,
        user_input: str,
        multimodal_parts: Optional[list[dict]] = None,
        model: str = "",
    ) -> str | list[dict]:
        """构造最终用户消息 content:纯文本或 content parts 数组。

        model 来自 llm.info["model"],用于判断当前模型是否支持视觉输入。
        """
        if not multimodal_parts:
            return user_input
        images = [p for p in multimodal_parts if p.get("type") == "image_url"]
        try:
            from app.core.multimodal import model_supports_vision, vision_notice
            if not model_supports_vision(model) or not settings.multimodal_vision_enabled:
                n = len(images)
                notice = vision_notice(model, n) if n else ""
                text_parts = [p.get("text", "") for p in multimodal_parts if p.get("type") == "text"]
                text = "\n".join(t for t in text_parts if t)
                combined = (user_input.strip() + "\n" if user_input.strip() else "") + notice
                if text:
                    combined = combined + "\n" + text
                return combined or user_input
        except ImportError:
            pass
        parts: list[dict] = [{"type": "text", "text": user_input}]
        for p in multimodal_parts:
            if p.get("type") in ("text", "image_url", "input_audio"):
                parts.append(p)
        return parts
