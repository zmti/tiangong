"""跨会话一致性与偏好学习:持久化用户偏好,跨会话保持一致行为。

对标 Claude 的长期记忆 + ChatGPT 的 Custom Instructions:
- 跨会话偏好:记住用户的语言偏好、技术栈、代码风格、回复风格
- 一致性保证:相同问题在不同会话给出一致的回答风格和深度
- 偏好进化:从交互历史自动学习和更新偏好
- 冲突检测:检测用户显式指令与历史偏好的冲突并解决
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from app.config import settings
from app.memory.profile import UserProfile, get_profile_store


# ── 跨会话偏好模型 ────────────────────────────────────────────────
@dataclass
class CrossSessionPreferences:
    """跨会话偏好:持久化存储,每次对话前加载。"""
    user_id: str = "default"
    # 沟通风格
    response_style: str = ""          # concise/detailed/humorous/academic/technical
    language: str = ""                # zh/en/mixed
    detail_level: str = ""            # high/medium/low
    # 技术领域
    primary_language: str = ""        # Python/TypeScript/Go/Rust...
    tech_stack: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    # 代码风格
    code_style: str = ""              # concise/detailed/functional/oop
    comment_style: str = ""           # inline/extensive/minimal
    naming_convention: str = ""       # snake_case/camelCase/PascalCase
    # 工作习惯
    preferred_format: str = ""        # markdown/code_table/plain
    error_handling: str = ""          # explicit/implicit/fail_fast
    testing_preference: str = ""      # unit/integration/e2e/tdd
    # 元数据
    version: int = 1
    last_updated: float = 0.0
    confidence: dict = field(default_factory=dict)  # 各字段的置信度
    source_history: list[dict] = field(default_factory=list)  # 偏好来源记录

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CrossSessionPreferences":
        known = {f.name for f in cls.__dataclass_fields__()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


class CrossSessionStore:
    """跨会话偏好存储:JSON 文件持久化。"""

    def __init__(self, data_dir: Optional[str] = None):
        self._data_dir = data_dir or settings.resolved_data_dir
        self._file = os.path.join(self._data_dir, "cross_session_prefs.json")
        self._lock = threading.RLock()
        self._prefs: dict[str, CrossSessionPreferences] = {}
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self._file):
                with open(self._file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                for uid, data in raw.items():
                    self._prefs[uid] = CrossSessionPreferences.from_dict(data)
        except Exception:  # noqa: BLE001
            self._prefs = {}

    def _save(self) -> None:
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(
                    {uid: p.to_dict() for uid, p in self._prefs.items()},
                    f, ensure_ascii=False, indent=2,
                )
        except Exception:  # noqa: BLE001
            pass

    def get(self, user_id: str) -> CrossSessionPreferences:
        with self._lock:
            return self._prefs.get(user_id) or CrossSessionPreferences(user_id=user_id)

    def save(self, user_id: str, prefs: CrossSessionPreferences) -> None:
        with self._lock:
            prefs.last_updated = time.time()
            prefs.version += 1
            self._prefs[user_id] = prefs
            self._save()

    def merge_from_dialogue(
        self,
        user_id: str,
        messages: list[dict],
        user_feedback: Optional[str] = None,
    ) -> CrossSessionPreferences:
        """从对话历史自动学习偏好。"""
        prefs = self.get(user_id)

        # 提取语言偏好
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if user_msgs:
            latest = user_msgs[-1].get("content", "")
            has_chinese = any(ord(c) > 127 for c in latest)
            has_english = any(c.isascii() and c.isalpha() for c in latest)
            if has_chinese and not has_english:
                prefs.language = "zh"
            elif has_english and not has_chinese:
                prefs.language = "en"
            elif has_chinese and has_english:
                prefs.language = "mixed"

        # 提取技术栈(从对话内容识别)
        all_content = " ".join(m.get("content", "") for m in messages).lower()
        tech_indicators = {
            "python": ["def ", "import ", "pip", "python"],
            "typescript": ["typescript", "interface", "type ", "npm", "yarn"],
            "react": ["react", "jsx", "tsx", "component", "useState", "useEffect"],
            "fastapi": ["fastapi", "uvicorn", "pydantic"],
            "django": ["django", "models.py", "views.py", "urls.py"],
            "rust": ["rust", "cargo", "fn main", "vec!"],
            "go": ["go ", "golang", "fmt.", "package main"],
            "docker": ["docker", "container", "kubernetes", "k8s"],
            "postgres": ["postgres", "postgresql", "psql", "sqlalchemy"],
        }
        for lang, indicators in tech_indicators.items():
            if any(ind in all_content for ind in indicators):
                if lang not in prefs.tech_stack:
                    prefs.tech_stack.append(lang)
                    prefs.confidence[lang] = min(1.0, prefs.confidence.get(lang, 0.3) + 0.2)

        # 从反馈更新
        if user_feedback:
            if "简洁" in user_feedback or "concise" in user_feedback:
                prefs.detail_level = "low"
                prefs.response_style = "concise"
            elif "详细" in user_feedback or "detailed" in user_feedback:
                prefs.detail_level = "high"
                prefs.response_style = "detailed"
            elif "代码" in user_feedback or "code" in user_feedback:
                prefs.primary_language = "code"

        prefs.source_history.append({
            "timestamp": time.time(),
            "message_count": len(messages),
            "feedback": user_feedback,
            "changes": {k: v for k, v in prefs.to_dict().items() if k not in ("version", "last_updated")},
        })
        prefs.source_history = prefs.source_history[-20:]  # 保留最近20条

        self.save(user_id, prefs)
        return prefs

    def build_injection_prompt(self, user_id: str = "default") -> str:
        """生成注入系统提示的偏好文本。"""
        prefs = self.get(user_id)
        if not any([
            prefs.response_style, prefs.language, prefs.detail_level,
            prefs.tech_stack, prefs.primary_language, prefs.code_style,
        ]):
            return ""

        lines = ["## 用户偏好(跨会话自动学习)"]
        if prefs.language:
            lines.append(f"- 语言: {prefs.language}")
        if prefs.response_style:
            lines.append(f"- 回复风格: {prefs.response_style}")
        if prefs.detail_level:
            lines.append(f"- 详细程度: {prefs.detail_level}")
        if prefs.tech_stack:
            lines.append(f"- 技术栈: {', '.join(prefs.tech_stack[:10])}")
        if prefs.primary_language:
            lines.append(f"- 主要语言: {prefs.primary_language}")
        if prefs.code_style:
            lines.append(f"- 代码风格: {prefs.code_style}")
        if prefs.comment_style:
            lines.append(f"- 注释风格: {prefs.comment_style}")
        if prefs.testing_preference:
            lines.append(f"- 测试偏好: {prefs.testing_preference}")

        return "\n".join(lines)

    def detect_conflict(self, user_id: str, new_instruction: str) -> Optional[str]:
        """检测新指令与历史偏好的冲突。"""
        prefs = self.get(user_id)
        conflicts = []

        # 示例:用户说"不要详细"但偏好是"详细"
        if "不要详细" in new_instruction or "简洁" in new_instruction:
            if prefs.detail_level == "high":
                conflicts.append("历史偏好详细,但当前请求简洁")
        if "详细" in new_instruction:
            if prefs.detail_level == "low":
                conflicts.append("历史偏好简洁,但当前请求详细")

        if conflicts:
            return "冲突检测: " + "; ".join(conflicts)
        return None


# ── 全局实例 ──────────────────────────────────────────────────────
_instance: Optional[CrossSessionStore] = None
_lock = threading.Lock()


def get_cross_session_store() -> CrossSessionStore:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = CrossSessionStore()
    return _instance
