"""自定义 OpenAI 兼容提供商注册表。

桌面端设置页可零代码接入新厂商:配置持久化到 data/providers.json,
LLMClient 按 provider id(如 custom-xxxx)查表构造客户端。
内置厂商仍走 .env;本注册表只管理"自定义 OpenAI 兼容"条目。
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings

_LOCK = threading.RLock()
_CACHE: Optional[Dict[str, Any]] = None

_ID_RE = re.compile(r"^custom-[0-9a-f]{8}$")


def _path() -> Path:
    return Path(settings.resolved_data_dir) / "providers.json"


def _load() -> Dict[str, Any]:
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            data: Dict[str, Any] = {}
            p = _path()
            if p.exists():
                try:
                    raw = json.loads(p.read_text("utf-8"))
                    if isinstance(raw, dict):
                        data = raw
                except Exception:
                    data = {}
            if not isinstance(data.get("custom"), list):
                data["custom"] = []
            _CACHE = data
        return _CACHE


def _save() -> None:
    with _LOCK:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_load(), ensure_ascii=False, indent=2), "utf-8")


def normalize_base_url(url: str) -> str:
    """规整 Base URL:去空白、补协议、去尾部斜杠。"""
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    return url.rstrip("/")


def mask_key(key: str) -> str:
    key = key or ""
    if len(key) <= 8:
        return "***" if key else ""
    return f"{key[:3]}***{key[-4:]}"


def list_custom() -> List[dict]:
    return list(_load().get("custom", []))


def get_custom(pid: str) -> Optional[dict]:
    if not pid or not pid.startswith("custom-"):
        return None
    for c in _load().get("custom", []):
        if c.get("id") == pid:
            return c
    return None


def upsert_custom(
    pid: Optional[str],
    name: str,
    base_url: str,
    api_key: str = "",
    model: str = "",
    models: Optional[List[str]] = None,
) -> dict:
    """新建或更新;更新时 api_key 留空表示沿用旧值。"""
    name = (name or "").strip()
    base_url = normalize_base_url(base_url)
    if not name:
        raise ValueError("名称不能为空")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"Base URL 无效: {base_url}")

    with _LOCK:
        items = _load().get("custom", [])
        entry: Optional[dict] = None
        if pid:
            entry = get_custom(pid)
        if entry is None:
            entry = {"id": f"custom-{uuid.uuid4().hex[:8]}"}
            items.append(entry)
        entry["name"] = name
        entry["base_url"] = base_url
        if api_key.strip():
            entry["api_key"] = api_key.strip()
        entry.setdefault("api_key", "")
        entry["model"] = (model or "").strip()
        if models is not None:
            entry["models"] = [m for m in models if m]
        entry.setdefault("models", [])
        _save()
        return dict(entry)


def delete_custom(pid: str) -> bool:
    with _LOCK:
        items = _load().get("custom", [])
        kept = [c for c in items if c.get("id") != pid]
        if len(kept) == len(items):
            return False
        _load()["custom"] = kept
        _save()
        return True


def invalidate() -> None:
    """丢弃内存缓存(下次访问重新读盘,供测试/外部改动后调用)。"""
    global _CACHE
    with _LOCK:
        _CACHE = None
