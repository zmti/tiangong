"""生成模型配置接口:.env 提供默认值,桌面端可覆盖(覆盖值立即生效、无需重启)。

GET  /v1/config/models  —— 查看图片/视频模型的生效值、来源、代码默认值
POST /v1/config/models  —— 设置覆盖值;传空字符串表示清除覆盖,回退到 .env/默认值
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config import settings
from app.core import config_runtime as rt

router = APIRouter(prefix="/v1/config", tags=["config"])

# 允许桌面端覆盖的配置键(白名单,避免把任意设置暴露出去)
OVERRIDABLE = ("image_model", "video_model")


class ModelOverride(BaseModel):
    image_model: str = Field(default="", description="图片模型;空字符串=清除覆盖")
    video_model: str = Field(default="", description="视频模型;空字符串=清除覆盖")


def _current() -> dict:
    out = {}
    for key in OVERRIDABLE:
        # image_model 为空时回退到 openai_compat_model(与 image_tool 的取值逻辑一致)
        if key == "image_model":
            base = settings.image_model or settings.openai_compat_model
        else:
            base = getattr(settings, key, "") or ""
        value, source = rt.resolve(key, base)
        out[key] = {
            "value": value,
            "source": source,  # override / env / default
            "code_default": rt.code_default(key),
            "overridden": source == "override",
        }
    return out


def _provider_display(pid: str) -> str:
    """provider id -> 界面友好名,用于下拉的分组标题。"""
    from app.api.chat import _PROVIDER_GROUPS
    from app.core.provider_registry import get_custom

    for g in _PROVIDER_GROUPS:
        if pid in (g.get("providers") or []):
            return g.get("name") or pid
    if pid.startswith("custom-"):
        entry = get_custom(pid)
        if entry:
            return entry.get("name") or pid
    return pid


def _provider_base_url(pid: str) -> str:
    """provider id -> 该提供商实际使用的 base_url(与 app/core/llm.py::_build 保持一致)。

    用于判断某个候选模型「能不能被当前生成端点调到」:
    图片/视频工具固定使用 openai_compat_base_url + openai_compat_api_key,
    模型挂在别的 provider 名下时,请求仍然会打到 openai_compat 那个地址。
    """
    from app.core.provider_registry import get_custom

    if pid.startswith("custom-"):
        entry = get_custom(pid) or {}
        return str(entry.get("base_url") or "")
    return {
        "deepseek": settings.deepseek_base_url,
        "qwen": settings.qwen_base_url,
        "ollama": settings.ollama_base_url,
        "compat": settings.openai_compat_base_url,
        "openai": "https://api.openai.com/v1",
    }.get(pid, "")


def _provider_api_key(pid: str) -> str:
    """provider id -> 该提供商实际使用的 api_key(与 _provider_base_url 对应)。"""
    from app.core.provider_registry import get_custom

    if pid.startswith("custom-"):
        entry = get_custom(pid) or {}
        return str(entry.get("api_key") or "")
    return {
        "deepseek": settings.deepseek_api_key,
        "qwen": settings.qwen_api_key,
        "ollama": "",
        "compat": settings.openai_compat_api_key,
        "openai": "",  # 无独立配置字段
    }.get(pid, "")


def _provider_can_serve(pid: str, capability: str) -> bool:
    """某 provider 能否实际承接该能力的生成请求。

    不仅看 base_url 是否存在,还看契约是否匹配:
      - 需要 key 的内置 provider(compat/qwen/deepseek/openai)缺 key 则不可用;
        ollama / 自定义 provider 不强制 key(本地网关可能免鉴权)。
      - 视频:上游 /v1/videos 是 agnes 专属契约,仅 compat 与自定义 agnes 网关支持;
        内置 qwen/deepseek/ollama 没有该端点,路由过去必 404,故标记不可用。
      - 图片:OpenAI 兼容 /images/generations,凡是有 base_url 的 provider 都尝试。
    """
    pbase = _provider_base_url(pid)
    if not pbase:
        return False
    needs_key = pid in ("deepseek", "qwen", "compat", "openai")
    if needs_key and not _provider_api_key(pid):
        return False
    if capability == "video":
        return pid == "compat" or pid.startswith("custom-")
    return True


def gen_endpoint_for_model(model_id: str, capability: str = "image") -> dict:
    """按模型 id 反查其所属 provider,返回该 provider 实际使用的生成端点。

    用于图片/视频工具「按 provider 路由」(取代原先硬编码 openai_compat_* 的逻辑):
    选了某 provider 下的模型,请求就发到该 provider 的 base_url + api_key。
    模型不在候选列表(用户手填/旧配置)时回退到 compat(apihub),保持历史行为。
    """
    model_id = (model_id or "").strip()
    for c in _candidates(capability):
        if c["id"] == model_id:
            pid = c["provider"]
            return {
                "provider": pid,
                "provider_name": c["provider_name"],
                "base_url": (c["provider_base_url"] or "").rstrip("/"),
                "api_key": _provider_api_key(pid),
            }
    return {
        "provider": "compat",
        "provider_name": "自定义端点",
        "base_url": (settings.openai_compat_base_url or "").rstrip("/"),
        "api_key": settings.openai_compat_api_key or "",
    }


def _any_gen_base_url() -> bool:
    """是否存在任意一个可用于生成的端点(供工具 enabled 判断)。"""
    from app.core.provider_registry import list_custom

    if settings.openai_compat_base_url:
        return True
    if settings.qwen_base_url:
        return True
    if any((c.get("base_url") for c in list_custom())):
        return True
    return False


def _norm_url(u: str) -> str:
    return (u or "").strip().rstrip("/").lower()


def _endpoint_info() -> dict:
    """图片/视频生成请求实际发往的端点,以及该端点归属的 provider 友好名。"""
    base = (settings.openai_compat_base_url or "").rstrip("/")
    name = "自定义端点"
    for pid in ("compat", "qwen", "deepseek", "ollama"):
        if base and _norm_url(_provider_base_url(pid)) == _norm_url(base):
            name = _provider_display(pid)
            break
    return {"base_url": base, "provider_name": name}


def _candidates(capability: str) -> list[dict]:
    """列出具备指定能力(image / video)的模型,供生成模型下拉选择。

    来源与「模型设置」完全同一套列表:
      1. provider 内置模型里带 capabilities 标记的
      2. 用户在设置页追加的扩展模型里带 capabilities 标记的
      3. 自定义提供商(custom-*)下带 capabilities 标记的
    这样生成模型不再手填字符串,而是从项目已有的模型清单里挑。
    返回项带 provider / provider_name(前端按 provider_name 分 optgroup),
    以及 provider_base_url / usable —— 后者表示该模型能否被当前的生成端点调到。
    """
    from app.api.chat import _PROVIDER_DEFAULT_MODELS
    from app.core.provider_registry import list_builtin_ext_models, list_custom

    out: list[dict] = []
    seen: set[str] = set()

    def pick(items, provider: str):
        pbase = _provider_base_url(provider)
        # 该模型能否被选中后真正调到:取决于其 provider 是否真能承接该能力
        # (base_url + key + 契约匹配),而不再是"是否等于 compat 端点"。
        usable = _provider_can_serve(provider, capability)
        for m in items or []:
            if capability not in (m.get("capabilities") or []):
                continue
            mid = m.get("id")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            out.append({
                "id": mid,
                "name": m.get("name") or mid,
                "provider": provider,
                "provider_name": _provider_display(provider),
                "provider_base_url": pbase,
                "usable": usable,
            })

    # 扫所有 provider:用户可以把生成模型挂在任意一个下面
    for pid, models in _PROVIDER_DEFAULT_MODELS.items():
        pick(models, pid)
        pick(list_builtin_ext_models(pid), pid)
    for c in list_custom():
        pick(c.get("models"), c.get("id") or "")
    return out


def _with_candidates(models: dict) -> dict:
    for key, cap in (("image_model", "image"), ("video_model", "video")):
        cands = _candidates(cap)
        # 保证当前生效值一定可选(可能是 .env 里配的、不在列表中的模型)
        cur = (models.get(key) or {}).get("value") or ""
        if cur and not any(c["id"] == cur for c in cands):
            # 生效值本来就是当前端点在用的,标 usable=True 免得误报警示
            cands.insert(0, {
                "id": cur,
                "name": cur + "（当前,列表外）",
                "provider": "compat",
                "provider_name": "当前端点",
                "provider_base_url": (settings.openai_compat_base_url or "").rstrip("/"),
                "usable": True,
            })
        models[key]["candidates"] = cands
    return models


@router.get("/models")
async def get_models() -> dict:
    return {
        "models": _with_candidates(_current()),
        "overrides": rt.get_all(),
        "gen_endpoint": _endpoint_info(),
    }


@router.post("/models")
async def set_models(req: ModelOverride) -> dict:
    # 只处理调用方显式传入的字段,避免未传的字段被默认值 "" 误清除
    payload = req.model_dump(exclude_unset=True)
    pairs = {k: v for k, v in payload.items() if k in OVERRIDABLE}
    rt.set_many(pairs)
    return {
        "ok": True,
        "models": _with_candidates(_current()),
        "overrides": rt.get_all(),
        "gen_endpoint": _endpoint_info(),
    }
