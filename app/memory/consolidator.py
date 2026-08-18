"""记忆精炼与合并:定期将散落记忆聚合成结构化"认知",避免记忆噪声膨胀。

对标 Anthropic Claude 的工作记忆设计:
- 工作记忆 episodic(短期对话片段) → 自动提炼为 semantic(稳定事实)
- 相似/重叠记忆合并,去重
- 生成"用户画像摘要"和"领域知识摘要"两类精炼记忆

触发:通过 /v1/memory/consolidate 手动触发,或后台调度器定时调用。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Optional

from app.config import settings
from app.memory.ltm import MemoryStore, get_memory_store


class ConsolidationResult:
    def __init__(
        self,
        merged_ids: list[str],
        new_ids: list[str],
        total_before: int,
        total_after: int,
        stats: dict,
    ):
        self.merged_ids = merged_ids
        self.new_ids = new_ids
        self.total_before = total_before
        self.total_after = total_after
        self.stats = stats

    def to_dict(self) -> dict:
        return {
            "merged_into": len(self.merged_ids),
            "newly_created": len(self.new_ids),
            "total_before": self.total_before,
            "total_after": self.total_after,
            **self.stats,
        }


class MemoryConsolidator:
    """记忆精炼引擎:按周期批量扫描记忆,触发 LLM 合并+提炼。

    策略:
    1. 分组:按 scope + 最近 24h 内创建的时间窗口分组
    2. 合并:用 critic 模型对每组做去重/合并,生成精炼版记忆
    3. 提炼:从会话历史/工具结果中提取用户偏好和领域知识
    """

    def __init__(self, store: Optional[MemoryStore] = None):
        self.store = store or get_memory_store()
        self._lock = threading.Lock()
        self._last_run: float = 0.0
        self._min_interval_seconds = float(settings.agent_consolidate_min_interval or 3600)

    def ready(self) -> bool:
        """检查是否满足下次运行条件(间隔 + 开关)。"""
        if not settings.agent_consolidate_enabled:
            return False
        now = time.time()
        return (now - self._last_run) >= self._min_interval_seconds

    def consolidate(
        self,
        force: bool = False,
        max_group_size: int = 15,
        target_scope: Optional[str] = None,
    ) -> ConsolidationResult:
        """执行一次记忆精炼。

        Args:
            force: 跳过间隔检查强制运行
            max_group_size: 每组最多处理多少条记忆(防单次调用过大)
            target_scope: 仅精炼指定作用域(默认 global)
        """
        if not force and not self.ready():
            mins = round((self._min_interval_seconds - (time.time() - self._last_run)) / 60, 1)
            return ConsolidationResult(
                merged_ids=[], new_ids=[], total_before=0, total_after=0,
                stats={"skipped": True, "reason": f"距上次精炼不足间隔({mins}分钟后才能再跑)"},
            )

        before = self.store.stats()
        merged_ids: list[str] = []
        new_ids: list[str] = []

        try:
            from app.core.llm import build_llm
            llm = build_llm()
        except Exception:  # noqa: BLE001
            return ConsolidationResult([], [], before["total"], before["total"],
                                       {"error": "LLM 不可用,无法执行精炼"})

        # 1. 批量合并:按 scope 取最近 N 条未合并过的记忆
        scope = target_scope or "global"
        raw = self.store.list(limit=200, scope=scope)
        groups = self._group_memories(raw, max_group_size=max_group_size)

        for i, group in enumerate(groups):
            if len(group) < 2:
                continue
            group_ids = [m["id"] for m in group]
            group_text = "\n\n".join(m["content"] for m in group)
            try:
                merged = self._merge_group(llm, group_text, group)
                if merged and merged.get("content"):
                    # 保存合并结果
                    kw = list(set(merged.get("keywords") or []))
                    result = self.store.save(
                        content=merged["content"],
                        keywords=kw,
                        scope=scope,
                        source="consolidated",
                    )
                    new_ids.append(result.get("id", ""))
                    merged_ids.extend(group_ids)
                    # 删除原组
                    for gid in group_ids:
                        self.store.delete(gid)
            except Exception:  # noqa: BLE001
                continue

        # 2. 提炼用户画像(若配置开启)
        profile_text = ""
        if settings.agent_user_profile_enabled:
            try:
                profile_text = self._extract_user_profile(llm, scope)
            except Exception:  # noqa: BLE001
                pass

        self._last_run = time.time()
        after = self.store.stats()
        return ConsolidationResult(
            merged_ids=merged_ids,
            new_ids=[nid for nid in new_ids if nid],
            total_before=before["total"],
            total_after=after["total"],
            stats={
                "groups_merged": len(groups) if groups else 0,
                "profile_extracted": bool(profile_text),
                "profile_preview": (profile_text or "")[:300],
            },
        )

    def _group_memories(self, memories: list[dict], max_group_size: int = 15) -> list[list[dict]]:
        """把记忆按关键词重叠分组(简单贪心)。"""
        if not memories:
            return []
        groups: list[list[dict]] = [[memories[0]]]
        for mem in memories[1:]:
            mem_kw = set(mem.get("keywords") or [])
            placed = False
            for g in groups:
                g_kw = set()
                for m in g:
                    g_kw |= set(m.get("keywords") or [])
                if g_kw & mem_kw or len(g) >= max_group_size:
                    if len(g) < max_group_size:
                        g.append(mem)
                        placed = True
                        break
            if not placed:
                groups.append([mem])
        return groups

    async def _merge_group(self, llm, group_text: str, group: list[dict]) -> Optional[dict]:
        """让 LLM 合并一组相似记忆,返回合并后内容+关键词。"""
        try:
            from app.core.llm import ToolCall as _TC
            schema = {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "合并后的精炼内容"},
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "精炼后的关键词列表",
                    },
                },
                "required": ["content", "keywords"],
            }

            def _validator(data):
                if not isinstance(data, dict):
                    return False, "结果不是对象"
                if not isinstance(data.get("content"), str) or not data.get("content").strip():
                    return False, "content 必须是非空字符串"
                if not isinstance(data.get("keywords"), list):
                    return False, "keywords 必须是数组"
                return True, None

            prompt = (
                "以下是多条关于同一主题的记忆片段,请把它们合并成一条精炼、无冗余的记忆:\n\n"
                f"{group_text}\n\n"
                "输出要求:内容要准确保留原意,删除重复信息,关键词要覆盖主题。"
            )
            resp = await llm.chat_json_schema(
                [{"role": "user", "content": prompt}],
                schema,
                temperature=0.1,
                validator=_validator,
            )
            return resp
        except Exception:  # noqa: BLE001
            return None

    async def _extract_user_profile(self, llm, scope: str) -> str:
        """从全局记忆中提取用户偏好,生成用户画像摘要。"""
        try:
            memories = self.store.list(limit=50, scope=scope)
            if not memories:
                return ""
            texts = "\n".join(m["content"][:200] for m in memories if m.get("source") == "manual")
            if not texts.strip():
                return ""

            schema = {
                "type": "object",
                "properties": {
                    "preferences": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "用户偏好(编程风格/语言偏好/工作习惯等)",
                    },
                    "domain_expertise": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "用户擅长的领域",
                    },
                    "notes": {"type": "string", "description": "其他备注"},
                },
                "required": ["preferences", "domain_expertise", "notes"],
            }

            def _validator(data):
                if not isinstance(data, dict):
                    return False, "结果不是对象"
                return True, None

            resp = await llm.chat_json_schema(
                [
                    {"role": "system", "content": "你是用户画像分析助手。从用户的记忆内容中推断其偏好和能力领域。"},
                    {"role": "user", "content": f"请分析以下用户相关记忆,提取偏好和擅长领域:\n{texts}"},
                ],
                schema,
                temperature=0.1,
                validator=_validator,
            )
            return json.dumps(resp, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return ""


_consolidator: Optional[MemoryConsolidator] = None
_consolidator_lock = threading.Lock()


def get_consolidator() -> MemoryConsolidator:
    global _consolidator
    if _consolidator is None:
        with _consolidator_lock:
            if _consolidator is None:
                _consolidator = MemoryConsolidator()
    return _consolidator
