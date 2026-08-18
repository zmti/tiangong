"""用户画像/偏好持久化:记录用户的风格、语言、技术栈、工作习惯等。

设计思路(对齐 Claude / ChatGPT Custom Instructions):
- 每次对话结束后可自动更新画像(由 LLM 从对话中提取偏好)
- 画像结构化存储为 JSON,每次对话前注入作为系统提示的一部分
- 支持手动管理接口(/v1/user_profile/*)
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from app.config import settings


@dataclass
class UserProfile:
    """用户画像数据模型。"""
    user_id: str = "default"
    name: str = ""
    # 编程相关偏好
    preferred_language: str = ""          # 常用编程语言,如 "Python,TypeScript"
    style: str = ""                       # 代码风格偏好,如 "简洁/详细注释/函数式"
    experience_level: str = ""            # 经验级别: beginner/intermediate/expert
    # 沟通风格
    response_style: str = ""              # concise/detailed/humorous/academic
    language_preference: str = ""         # zh/en/mixed
    # 领域知识
    domains: list[str] = field(default_factory=list)  # ["web", "ml", "devops"]
    tech_stack: list[str] = field(default_factory=list)  # ["react", "fastapi", "postgres"]
    # 习惯与约束
    habits: list[str] = field(default_factory=list)     # 工作习惯
    constraints: list[str] = field(default_factory=list) # 必须遵守的约束
    # 元数据
    last_updated: float = 0.0
    version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "UserProfile":
        known = {f.name for f in cls.__dataclass_fields__()}  # type: ignore
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


class UserProfileStore:
    """用户画像存储:文件(JSON)或 SQLite 持久化。"""

    def __init__(self, data_dir: Optional[str] = None):
        self._data_dir = data_dir or settings.resolved_data_dir
        self._file = os.path.join(self._data_dir, "user_profiles.json")
        self._lock = threading.RLock()
        self._profiles: dict[str, UserProfile] = {}
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self._file):
                with open(self._file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                for uid, data in raw.items():
                    self._profiles[uid] = UserProfile.from_dict(data)
        except Exception:  # noqa: BLE001
            self._profiles = {}

    def _save(self) -> None:
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(
                    {uid: p.to_dict() for uid, p in self._profiles.items()},
                    f, ensure_ascii=False, indent=2,
                )
        except Exception:  # noqa: BLE001
            pass

    def get(self, user_id: str) -> UserProfile:
        with self._lock:
            return self._profiles.get(user_id) or UserProfile(user_id=user_id)

    def save(self, user_id: str, profile: UserProfile) -> None:
        with self._lock:
            profile.last_updated = time.time()
            profile.version += 1
            self._profiles[user_id] = profile
            self._save()

    def update_field(self, user_id: str, field_name: str, value) -> UserProfile:
        with self._lock:
            p = self._profiles.get(user_id)
            if p is None:
                p = UserProfile(user_id=user_id)
            setattr(p, field_name, value)
            p.last_updated = time.time()
            p.version += 1
            self._profiles[user_id] = p
            self._save()
            return p

    def list_all(self) -> list[dict]:
        with self._lock:
            return [p.to_dict() for p in self._profiles.values()]


_profile_store: Optional[UserProfileStore] = None
_profile_lock = threading.Lock()


def get_profile_store() -> UserProfileStore:
    global _profile_store
    if _profile_store is None:
        with _profile_lock:
            if _profile_store is None:
                _profile_store = UserProfileStore()
    return _profile_store


def build_profile_prompt(user_id: str = "default") -> str:
    """根据用户画像生成注入系统提示的文本块。"""
    store = get_profile_store()
    profile = store.get(user_id)
    if not profile or not any([
        profile.preferred_language, profile.style, profile.response_style,
        profile.language_preference, profile.domains, profile.tech_stack,
        profile.habits, profile.constraints,
    ]):
        return ""

    lines: list[str] = ["## 用户画像(自动注入)"]
    if profile.preferred_language:
        lines.append(f"- 常用语言: {profile.preferred_language}")
    if profile.style:
        lines.append(f"- 代码风格: {profile.style}")
    if profile.response_style:
        lines.append(f"- 回复风格: {profile.response_style}")
    if profile.language_preference:
        lines.append(f"- 语言偏好: {profile.language_preference}")
    if profile.domains:
        lines.append(f"- 擅长领域: {', '.join(profile.domains)}")
    if profile.tech_stack:
        lines.append(f"- 技术栈: {', '.join(profile.tech_stack)}")
    if profile.habits:
        lines.append(f"- 工作习惯: {', '.join(profile.habits)}")
    if profile.constraints:
        lines.append(f"- 约束: {', '.join(profile.constraints)}")

    return "\n".join(lines)
