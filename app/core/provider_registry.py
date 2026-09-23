"""自定义 OpenAI 兼容提供商注册表。

桌面端设置页可零代码接入新厂商:配置持久化到 data/providers.json,
LLMClient 按 provider id(如 custom-xxxx)查表构造客户端。
内置厂商仍走 .env;本注册表只管理"自定义 OpenAI 兼容"条目与"内置厂商扩展模型"。
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

BUILTIN_PROVIDERS = {"deepseek", "qwen", "openai", "compat", "ollama"}


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
            if not isinstance(data.get("builtin_ext"), dict):
                data["builtin_ext"] = {}
            _CACHE = data
        return _CACHE


def _save() -> None:
    global _CACHE
    with _LOCK:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_CACHE, ensure_ascii=False, indent=2), "utf-8")
        # 写入成功后刷新缓存,确保后续读取拿到最新数据
        _CACHE = None


def normalize_base_url(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    return url.rstrip("/")


def mask_key(key: str) -> str:
    key = key or ""
    if len(key) <= 8:
        return "***" if key else ""
    return f"{key[:3]}***{key[-4:]}"


def _infer_capabilities(model_id: str) -> list:
    """按模型 id 推断能力,仅在模型未显式标注 capabilities 时兜底。

    项目里已有一批用户添加的模型(如 agnes-image-2.1-flash / agnes-video-2.5),
    当初写入时还没有 capabilities 字段。若不推断,它们既进不了「生成模型」候选,
    又会混进对话模型下拉——两头不讨好。显式标注优先,推断只是兜底。
    """
    mid = (model_id or "").lower()
    caps = []
    if "image" in mid:
        caps.append("image")
    if "video" in mid:
        caps.append("video")
    return caps


def _norm_model(m: Any) -> Optional[dict]:
    if isinstance(m, str):
        mid = m.strip()
        if not mid:
            return None
        return {"id": mid, "name": mid, "context": None, "default": False, "capabilities": _infer_capabilities(mid)}
    if isinstance(m, dict):
        mid = (m.get("id") or m.get("model") or "").strip()
        if not mid:
            return None
        return {
            "id": mid,
            "name": (m.get("name") or mid).strip(),
            "context": m.get("context"),
            "default": bool(m.get("default")),
            # 能力标记:["image"] / ["video"] / ["image","video"],用于生成模型下拉候选
            "capabilities": list(m.get("capabilities") or []) or _infer_capabilities(mid),
        }
    return None


def _default_model_of(entry: dict) -> str:
    models = entry.get("models") or []
    if not models:
        return (entry.get("model") or "").strip()
    dm = (entry.get("default_model") or entry.get("model") or "").strip()
    if dm and any(x.get("id") == dm for x in models):
        return dm
    for x in models:
        if x.get("default"):
            return x["id"]
    return models[0]["id"]


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
    models: Optional[List[Any]] = None,
) -> dict:
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
        if model:
            entry["model"] = model.strip()
        # 清除旧默认值再重算:否则 _default_model_of 优先读上一次保存的
        # default_model,会把用户本次通过单选钮新选的默认模型覆盖掉
        entry.pop("default_model", None)
        if models is not None:
            entry["models"] = [x for x in (_norm_model(m) for m in models) if x]
        entry.setdefault("models", [])
        entry["default_model"] = _default_model_of(entry)
        entry["model"] = entry["default_model"]
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


def list_models(pid: str) -> List[dict]:
    entry = get_custom(pid)
    if not entry:
        return []
    dm = _default_model_of(entry)
    out = []
    for m in entry.get("models", []) or []:
        m = _norm_model(m)
        if not m:
            continue
        m = dict(m)
        m["default"] = (m.get("id") == dm)
        out.append(m)
    # 兜底:models 数组为空但配置了默认 model 时,返回单元素列表,
    # 保证模型选择器 / 管理面板 / 厂商列表的"模型数量"能正确显示已有模型
    if not out and entry.get("model"):
        out.append({
            "id": entry["model"],
            "name": entry.get("name") or entry["model"],
            "default": True,
        })
    return out


def add_model(
    pid: str,
    model_id: str,
    name: str = "",
    context: Optional[int] = None,
    capabilities: Optional[list] = None,
) -> dict:
    entry = get_custom(pid)
    if not entry:
        raise ValueError("提供商不存在")
    model_id = (model_id or "").strip()
    if not model_id:
        raise ValueError("模型 ID 不能为空")
    models = entry.setdefault("models", [])
    for m in models:
        if m.get("id") == model_id:
            if name:
                m["name"] = name.strip()
            if context is not None:
                m["context"] = context
            if capabilities is not None:
                m["capabilities"] = list(capabilities)
            break
    else:
        models.append(_norm_model({
            "id": model_id,
            "name": name or model_id,
            "context": context,
            "capabilities": capabilities or [],
        }))
    if not entry.get("default_model") and not entry.get("model"):
        entry["default_model"] = model_id
    entry["default_model"] = _default_model_of(entry)
    entry["model"] = entry["default_model"]
    _save()
    return dict(entry)


def remove_model(pid: str, model_id: str) -> dict:
    entry = get_custom(pid)
    if not entry:
        raise ValueError("提供商不存在")
    models = entry.get("models", []) or []
    kept = [m for m in models if m.get("id") != model_id]
    entry["models"] = kept
    dm = (entry.get("default_model") or entry.get("model") or "").strip()
    if dm == model_id:
        entry["default_model"] = kept[0]["id"] if kept else ""
    entry["default_model"] = _default_model_of(entry)
    entry["model"] = entry["default_model"]
    _save()
    return dict(entry)


def set_default_model(pid: str, model_id: str) -> dict:
    entry = get_custom(pid)
    if not entry:
        raise ValueError("提供商不存在")
    if not any((m.get("id") == model_id) for m in entry.get("models", []) or []):
        raise ValueError("模型不存在")
    entry["default_model"] = model_id
    entry["model"] = model_id
    _save()
    return dict(entry)


def get_builtin_ext() -> Dict[str, List[dict]]:
    data = _load()
    ext = data.get("builtin_ext")
    if not isinstance(ext, dict):
        ext = {}
        data["builtin_ext"] = ext
    return ext


def add_builtin_model(provider: str, model_id: str, name: str = "", context: Optional[int] = None) -> dict:
    if provider not in BUILTIN_PROVIDERS:
        raise ValueError(f"未知内置厂商: {provider}")
    model_id = (model_id or "").strip()
    if not model_id:
        raise ValueError("模型 ID 不能为空")
    ext = get_builtin_ext()
    lst = ext.setdefault(provider, [])
    for m in lst:
        if m.get("id") == model_id:
            if name:
                m["name"] = name.strip()
            if context is not None:
                m["context"] = context
            break
    else:
        lst.append(_norm_model({"id": model_id, "name": name or model_id, "context": context}))
    _save()
    return {"provider": provider, "models": lst}


def remove_builtin_model(provider: str, model_id: str) -> bool:
    ext = get_builtin_ext()
    lst = ext.get(provider, [])
    kept = [m for m in lst if m.get("id") != model_id]
    if len(kept) == len(lst):
        return False
    ext[provider] = kept
    _save()
    return True


def list_builtin_ext_models(provider: str) -> List[dict]:
    # 过一遍 _norm_model:补齐 capabilities(含按 id 的推断),
    # 否则历史数据里没有该字段的模型既进不了生成候选,又会混进对话模型列表。
    out = []
    for m in get_builtin_ext().get(provider, []) or []:
        nm = _norm_model(m)
        if nm:
            out.append(nm)
    return out


def set_builtin_default_model(provider: str, model_id: str) -> dict:
    """设置内置厂商的默认模型。"""
    if provider not in BUILTIN_PROVIDERS:
        raise ValueError(f"未知内置厂商: {provider}")
    from app.api.chat import _PROVIDER_DEFAULT_MODELS
    all_models = [m.get("id") for m in _PROVIDER_DEFAULT_MODELS.get(provider, [])]
    ext_models = [m.get("id") for m in list_builtin_ext_models(provider)]
    if model_id not in all_models and model_id not in ext_models:
        raise ValueError("模型不存在")
    ext = get_builtin_ext()
    if provider not in ext:
        ext[provider] = []
    for m in ext[provider]:
        m["default"] = (m.get("id") == model_id)
    _save()
    return {"provider": provider, "model_id": model_id}


def invalidate() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None
