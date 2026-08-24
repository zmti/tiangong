"""厂商:模型 引用解析,统一处理历史遗留的 "provider:model" 与 "provider/model" 两种格式。"""

from __future__ import annotations

from typing import Optional, Tuple


def parse_model_ref(s: Optional[str]) -> Tuple[str, str]:
    if not s:
        return ("", "")
    s = s.strip()
    if ":" in s:
        head, tail = s.split(":", 1)
    elif "/" in s:
        head, tail = s.split("/", 1)
    else:
        return ("", s)
    head = head.strip().lower()
    tail = tail.strip()
    if not head or not tail:
        return ("", s)
    return (head, tail)


def format_model_ref(provider: str, model: str) -> str:
    return f"{provider}:{model}"
