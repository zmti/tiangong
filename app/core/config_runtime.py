"""运行时配置覆盖:桌面端设置面板修改的值,优先级高于 .env/环境变量与代码默认值。

优先级(高 → 低):
    1. 桌面端覆盖   —— 本模块,持久化在 data/config_overrides.json,改完立即生效、无需重启
    2. .env/环境变量 —— pydantic-settings(字段名大写即环境变量名,无 env_prefix)
    3. 代码默认值   —— app/config.py 里的字段 default

覆盖值置空 = 删除该覆盖,自动回退到下一层(.env → 代码默认)。
"""
from __future__ import annotations

import json
import logging
import os
import threading

from app.config import settings

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_cache: dict[str, str] | None = None


def _path() -> str:
    d = settings.resolved_data_dir
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "config_overrides.json")


def _load() -> dict[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
            _cache = data if isinstance(data, dict) else {}
    except FileNotFoundError:
        _cache = {}
    except Exception as e:  # noqa: BLE001
        logger.warning("读取配置覆盖失败(按空处理): %s", e)
        _cache = {}
    return _cache


def _save(data: dict[str, str]) -> None:
    global _cache
    with _lock:
        _cache = data
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _path())


def code_default(key: str) -> str:
    """取 app/config.py 里该字段的代码默认值(可能与 settings 当前值不同)。"""
    field = settings.model_fields.get(key)
    d = getattr(field, "default", None) if field else None
    return str(d) if d is not None else ""


def get_all() -> dict[str, str]:
    return dict(_load())


def set_many(pairs: dict[str, str]) -> dict[str, str]:
    """批量写入覆盖;值为空字符串表示删除该覆盖(回退到 .env/默认值)。"""
    data = dict(_load())
    for k, v in pairs.items():
        v = (v or "").strip()
        if v:
            data[k] = v
        else:
            data.pop(k, None)
    _save(data)
    return dict(data)


def resolve(key: str, from_settings: str) -> tuple[str, str]:
    """返回 (生效值, 来源)。来源: override / env / default。

    from_settings 传入 settings 上的当前值(已含 .env 覆盖),用于区分 env 与 default:
    settings 值与代码默认值不同 → 说明被 .env 或环境变量改过。
    """
    ov = _load().get(key)
    if ov:
        return ov, "override"
    val = str(from_settings or "")
    if val != code_default(key):
        return val, "env"
    return val, "default"


def resolve_value(key: str, from_settings: str) -> str:
    """只取生效值(不关心来源)。"""
    return resolve(key, from_settings)[0]
