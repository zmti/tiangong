"""记忆管理器:负责记忆检索、自动沉淀、历史压缩。

从 Agent 类中提取,降低主类职责。
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.core.llm import LLMClient


class MemoryManager:
    """记忆管理器:统一管理分层记忆检索、自动沉淀、历史压缩。"""

    def __init__(self, session_id: str = "", user_id: str = ""):
        self.session_id = session_id
        self.user_id = user_id or session_id
        self._memory_note: str = ""
        self._failure_note: str = ""
        self._success_note: str = ""

    @property
    def memory_note(self) -> str:
        return self._memory_note

    @property
    def failure_note(self) -> str:
        return self._failure_note

    @property
    def success_note(self) -> str:
        return self._success_note

    async def retrieve_memory_note(
        self, user_input: str, history: list[dict], llm: LLMClient,
    ) -> str:
        """异步检索分层记忆,构造注入提示块。"""
        if not settings.agent_memory_enabled:
            return ""
        try:
            from app.memory.layers import build_layered_memory_note, enable_layered

            if enable_layered():
                note = await build_layered_memory_note(
                    user_input, history, self.session_id, llm,
                )
                if note:
                    self._memory_note = note
                    return note
        except Exception:  # noqa: BLE001
            pass  # 分层失败回退旧逻辑
        try:
            from app.memory.ltm import get_memory_store

            store = get_memory_store()
            mems: list[dict] = await store.search_async(user_input, top_k=4, count_hit=True) or []
            if self.session_id:
                seen = {m["id"] for m in mems}
                for m in await store.search_async(
                    user_input, top_k=4, scope="session", session_id=self.session_id, count_hit=True
                ):
                    if m["id"] not in seen:
                        mems.append(m)
                        seen.add(m["id"])
        except Exception:  # noqa: BLE001
            pass  # 失败时保留已有 _memory_note,不覆盖为空
        if not mems:
            self._memory_note = ""
            return ""
        lines = [f"- {m['content']}" for m in mems]
        note = "根据长期记忆,以下信息可能与当前对话相关:\n" + "\n".join(lines)
        self._memory_note = note
        return note

    def retrieve_failure_note(self, registry) -> str:
        """检索失败经验,构造注入提示块。"""
        try:
            from app.memory.failure_memory import get_failure_memory
            tool_names = [t.name for t in registry.list() if t.enabled]
            store = get_failure_memory()
            self._failure_note = store.build_experience_note(tool_names[:30])
        except Exception:  # noqa: BLE001
            self._failure_note = ""
        return self._failure_note

    def retrieve_success_note(self, registry) -> str:
        """检索成功经验,构造注入提示块。"""
        try:
            from app.memory.success_memory import get_success_memory
            tool_names = [t.name for t in registry.list() if t.enabled]
            store = get_success_memory()
            self._success_note = store.build_experience_note(tool_names[:20])
        except Exception:  # noqa: BLE001
            self._success_note = ""
        return self._success_note

    async def auto_memory(
        self, llm: LLMClient, user_input: str, answer: str, history: list[dict],
    ) -> None:
        """对话结束后自动抽取事实存入长期记忆(默认关闭)。"""
        if not settings.agent_auto_memory:
            return
        # 质量过滤:极短回复或纯客套话跳过沉淀
        if getattr(settings, "memory_auto_filter_enabled", True):
            skip_kws = [k.strip() for k in (getattr(settings, "memory_skip_keywords", "") or "").split(",") if k.strip()]
            answer_clean = answer.strip()
            min_len = int(getattr(settings, "memory_min_length", 15) or 15)
            if len(answer_clean) < min_len:
                return
            if any(kw in answer_clean for kw in skip_kws):
                return
        try:
            recent = (history or [])[-6:]
            prompt = [
                {
                    "role": "system",
                    "content": (
                        "从以下对话中抽取值得长期记住的事实(用户偏好、个人信息、项目事实、约定)。"
                        "每条用一句话陈述。若没有值得记住的,返回空数组。"
                        '只输出 JSON,格式为 {"facts": ["...", "..."]}'
                    ),
                },
                {"role": "user", "content": json.dumps(recent, ensure_ascii=False)[:6000]},
            ]
            if hasattr(llm, "chat_json"):
                data = await llm.chat_json(prompt, temperature=0.2)
            else:
                resp = await llm.chat(prompt, temperature=0.2)
                data = _try_parse_json(resp)
            facts = data.get("facts", []) if isinstance(data, dict) else []
            if facts:
                from app.memory.ltm import get_memory_store
                from app.memory.decay import auto_dedup_and_evict

                store = get_memory_store()
                for f in facts[:5]:
                    if isinstance(f, str) and f.strip():
                        # M5: 保存前自动去重 + TTL 驱逐
                        dedup_result = auto_dedup_and_evict(
                            store, f.strip(), scope="global", source="auto"
                        )
                        if dedup_result["action"] == "saved":
                            store.save(f.strip(), source="auto")
                        # merged 或 evicted 已由 auto_dedup_and_evict 处理
                        # 分层记忆写侧
                        if settings.agent_layered_memory and self.session_id:
                            try:
                                store.save(
                                    f.strip(),
                                    scope="session",
                                    session_id=self.session_id,
                                    source="auto",
                                )
                            except Exception:  # noqa: BLE001
                                pass
        except Exception:  # noqa: BLE001
            pass

    async def prepare_history(self, llm: LLMClient, history: list[dict]) -> list[dict]:
        """历史超预算时,用摘要压缩旧消息。"""
        if not history or not settings.agent_summarize:
            return history
        # 优先从 message_builder 获取预算,否则用 LLMClient 的私有属性兜底
        from app.core.message_builder import MessageBuilder as _MB
        token_budget = getattr(getattr(llm, "_message_builder", None), "_token_budget", 0) or \
                       getattr(llm, "_token_budget", 0) or 0
        context_budget = getattr(getattr(llm, "_message_builder", None), "_context_budget", 0) or \
                         getattr(llm, "_context_budget", settings.context_char_budget) or \
                         settings.context_char_budget
        if token_budget and token_budget > 0:
            from app.memory.context import layered_compress

            processed, _stats = await layered_compress(
                llm,
                history,
                max_tokens=int(token_budget),
                head_tokens=0,
                min_keep=6,
            )
            return processed
        total = sum(
            len(str(m.get("content", ""))) for m in history if m.get("role") in ("user", "assistant")
        )
        # 用字符数做粗估(字符/4 ≈ token),与 context_budget 同单位比较
        if total <= context_budget * settings.agent_summary_threshold_ratio:
            return history
        keep: list[dict] = []
        acc = 0
        keep_budget = int(context_budget * 0.5)
        if getattr(settings, "context_preserve_priority", True):
            for m in reversed(history):
                role = m.get("role", "")
                content = str(m.get("content", ""))
                c = len(content)
                is_tool = role in ("tool", "plan")
                effective_c = (c if not is_tool
                               else min(c, settings.message_char_cap))
                if acc + effective_c <= keep_budget:
                    keep.insert(0, m)
                    acc += effective_c
                else:
                    break
        else:
            for m in reversed(history):
                c = len(str(m.get("content", "")))
                if acc + c <= keep_budget:
                    keep.insert(0, m)
                    acc += c
                else:
                    break
        old = history[: len(history) - len(keep)]
        if len(old) < 2:
            return history
        from app.memory.summarize import summarize_messages_async

        summary = await summarize_messages_async(llm, old)
        if summary:
            return [{
                "role": "system",
                "content": f"以下是本会话早期内容的摘要(请据此理解前文背景):\n{summary}",
            }] + keep
        return history

    def get_profile_hint(self) -> str:
        """获取用户画像提示。"""
        if not settings.agent_user_profile_enabled or not self.user_id:
            return ""
        from app.memory.profile import build_profile_prompt
        return build_profile_prompt(self.user_id) or ""


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
