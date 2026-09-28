"""生成模型配置接口:.env 提供默认值,桌面端可覆盖(覆盖值立即生效、无需重启)。

GET  /v1/config/models  —— 查看图片/视频模型的生效值、来源、代码默认值
POST /v1/config/models  —— 设置覆盖值;传空字符串表示清除覆盖,回退到 .env/默认值

两项能力(2026-09-24 新增):
  1. 跟随对话模型:生成模型「没配置」时,若当前对话所用的模型自身支持该能力
     (模型清单里带 capabilities: image / video),就直接用对话模型生成 —— 用户
     在「模型选择器」里换了个多模态模型,生成侧自动跟上,不用再来设置页配一遍。
     也可以显式选择「跟随对话模型」这一项(覆盖值记为 SESSION_MODEL_MARK)。
  2. 逐能力"改用会话模型":image_enabled / video_enabled,关掉后**不再**使用专门
     配置的生成模型,改用当前对话所选的模型来出图/出片;默认两者都是启用。
     注意:这不是禁用能力 —— 工具照常注册,AI 依然能生成,只是换了模型。
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.config import settings
from app.core import config_runtime as rt

router = APIRouter(prefix="/v1/config", tags=["config"])

# 模型选择键 / 能力开关键,合成允许桌面端覆盖的白名单(避免把任意设置暴露出去)
MODEL_KEYS = ("image_model", "video_model")
OVERRIDABLE = MODEL_KEYS + ("image_enabled", "video_enabled")
# 逐能力的开关键;"0" = 关闭,缺省 = 启用
ENABLED_KEYS = {"image": "image_enabled", "video": "video_enabled"}
# 「跟随对话模型」在覆盖值里的哨兵值。用 @ 开头,不可能与真实模型 id 撞车
SESSION_MODEL_MARK = "@session"


class ModelOverride(BaseModel):
    image_model: str = Field(default="", description="图片模型;空字符串=清除覆盖")
    video_model: str = Field(default="", description="视频模型;空字符串=清除覆盖")
    # None = 本次不改动(避免误用默认值 False 把开关悄悄关掉)
    image_enabled: bool | None = Field(default=None, description="是否使用专门配的图片生成模型;关闭则改用当前对话模型")
    video_enabled: bool | None = Field(default=None, description="是否使用专门配的视频生成模型;关闭则改用当前对话模型")


def _current(session_model: str = "") -> dict:
    out = {}
    for key in MODEL_KEYS:
        capability = key.split("_", 1)[0]
        # image_model 为空时回退到 openai_compat_model(与 image_tool 的取值逻辑一致)
        if key == "image_model":
            base = settings.image_model or settings.openai_compat_model
        else:
            base = getattr(settings, key, "") or ""
        value, source = resolve_gen_model(capability, base, session_model=session_model)
        can_inherit = can_inherit_session(session_model, capability)
        enabled = gen_enabled(capability)
        out[key] = {
            "value": value,
            "source": source,  # session / override / env / default
            "code_default": rt.code_default(key),
            "overridden": source == "override",
            # 显式选了「跟随对话模型」(哨兵值覆盖)
            "inherit_session": (rt.get_all().get(key) or "").strip() == SESSION_MODEL_MARK,
            "enabled": enabled,
            # 开关关闭、生效值已改用当前对话模型(关掉后不再是"专门配的那个")
            "disabled_fallback": (not enabled) and source == "session",
            "session_model": session_model,
            # 该能力能否真的继承到对话模型(对话模型不支持 / 未提供时为 False)
            "can_inherit": can_inherit[0],
            # 继承依据:tagged=清单标了能力 / inferred=按 id 推断出能力 / ""=不能继承
            "inherit_reason": can_inherit[1],
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


def _model_provider(model_id: str) -> str:
    """按模型 id 反查其所属 provider(不要求该模型带 capabilities 标记)。

    _candidates() 只返回"带能力标记"的模型,而绝大多数对话模型(如 agnes-3.0-flash)
    压根没有这个标记,所以判断"对话模型能不能拿来生成"时得单独反查归属。
    """
    mid = (model_id or "").strip()
    if not mid:
        return ""
    from app.api.chat import _PROVIDER_DEFAULT_MODELS
    from app.core.provider_registry import list_builtin_ext_models, list_custom

    for pid, models in _PROVIDER_DEFAULT_MODELS.items():
        if any((m.get("id") or "").strip() == mid for m in models or []):
            return pid
        if any((m.get("id") or "").strip() == mid for m in list_builtin_ext_models(pid) or []):
            return pid
    for c in list_custom():
        if any((m.get("id") or "").strip() == mid for m in c.get("models") or []):
            return c.get("id") or ""
    return ""


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
    # 没打能力标记的对话模型(多模态模型被继承为生成模型时常见):
    # 按归属 provider 路由,而不是一律丢给 compat,否则 qwen 下的模型会打错端点。
    pid = _model_provider(model_id)
    if pid:
        return {
            "provider": pid,
            "provider_name": _provider_display(pid),
            "base_url": (_provider_base_url(pid) or "").rstrip("/"),
            "api_key": _provider_api_key(pid),
        }
    return {
        "provider": "compat",
        "provider_name": "自定义端点",
        "base_url": (settings.openai_compat_base_url or "").rstrip("/"),
        "api_key": settings.openai_compat_api_key or "",
    }


def any_gen_enabled() -> bool:
    """图片/视频是否至少还有一个在用"专门配的生成模型"(供前端提示用)。"""
    return gen_enabled("image") or gen_enabled("video")


def gen_enabled(capability: str) -> bool:
    """该能力是否仍使用"专门配的生成模型"。默认启用,只有显式存了 "0" 才算关闭。

    关闭 ≠ 禁用能力:它表示"不指定专门的生成模型,改用当前对话所选的模型"
    (见 resolve_gen_model)。工具照常注册,AI 依然可以出图/出片。
    """
    key = ENABLED_KEYS.get(capability)
    if not key:
        return True
    return (rt.get_all().get(key) or "").strip() != "0"


def has_capability(model_id: str, capability: str) -> bool:
    """模型是否支持该生成能力(看模型清单里的 capabilities 标记)。"""
    mid = (model_id or "").strip()
    if not mid:
        return False
    return any(c["id"] == mid for c in _candidates(capability))


def can_inherit_session(model_id: str, capability: str) -> tuple[bool, str]:
    """对话模型能否被继承为该能力的生成模型,返回 (能否, 依据)。

    依据有两种:
      - "tagged"   : 模型清单里显式标了 capabilities(最可靠);
      - "inferred" : 清单没标,但按模型 id 推断出了该能力
                     (如 agnes-image-2.5-flash,见 provider_registry._infer_capabilities),
                     且其所属 provider 能承接这项生成能力。
      - ""         : 不能继承。

    ⚠️ 这里判的是「**这个模型**能不能生成」,不是「它所在的端点存不存在」。
    曾经按"端点可用性"放宽(provider 有 base_url 就算能承接),结果把
    agnes-3.0-flash 这类**纯对话模型**当成生图模型发给 /images/generations,
    上游直接 400: "Model agnes-3.0-flash is a chat model. Use /v1/chat/completions."
    多模态对话模型的"多模态"是能**看懂**图(vision 输入),不是能**生成**图。
    生成必须用专用模型(agnes-image-* / agnes-video-*)。
    """
    mid = (model_id or "").strip()
    if not mid:
        return False, ""
    if has_capability(mid, capability):
        return True, "tagged"
    from app.core.provider_registry import _infer_capabilities

    if capability not in _infer_capabilities(mid):
        return False, ""
    pid = _model_provider(mid)
    if pid and _provider_can_serve(pid, capability):
        return True, "inferred"
    return False, ""


def _fallback_gen_model(capability: str, base: str) -> tuple[str, str]:
    """对话模型不能继承时,回落到"专门配的生成模型"(.env / 代码默认)。

    覆盖值可能是「跟随对话模型」哨兵 —— 它本身就表示"没有专门配生成模型",
    再把它当模型名返回会把 "@session" 发给上游,故哨兵一律视作未配置。
    """
    key = f"{capability}_model"
    val, source = rt.resolve(key, base)
    if not (val or "").strip() or val.strip() == SESSION_MODEL_MARK:
        val = base or ""
        source = "env" if val != rt.code_default(key) else "default"
    return val, source


def resolve_gen_model(
    capability: str,
    app_default: str,
    session_model: str | None = None,
) -> tuple[str, str]:
    """解析某生成能力实际该用的模型,返回 (模型 id, 来源)。

    优先级(高 → 低):
      0. 该能力被用户在设置页关闭 → 优先用对话模型 ("session")
         ★ 开关的语义是"不指定专门的生成模型,改用当前对话所选的模型",**不是**
           禁用能力:工具照常注册,用户关掉开关后依然可以让 AI 出图/出片,
           只是生成的模型优先换成会话里选的那个。
      1. 桌面端显式选了某个模型       → (该模型, "override")
      2. 桌面端显式选了「跟随对话模型」 → 用对话模型 ("session"),用户意图优先
      3. 桌面端没配 + 对话模型可继承该能力 → (对话模型, "session")  ★ 需求的核心:
                                        生成模型没配置时,默认跟随用户当前选的对话模型。
      4. .env/环境变量 / 代码默认值     → (原解析结果, "env" 或 "default")

    ★ 0 / 2 / 3 三条"用对话模型"的分支都要先过 can_inherit_session() :
    **能跟随才跟随,跟随不了就回落到第 4 条的专用生成模型**,绝不把纯对话模型
    发给 /images/generations、/v1/videos —— 那必然被上游 400 拒
    ("Model xxx is a chat model. Use /v1/chat/completions."),生成能力等于废掉。

    session_model 为 None 时自动从会话上下文读取(工具调用侧常用)。
    """
    key = f"{capability}_model"
    base = str(app_default or "")
    if session_model is None:
        from app.core.session_model_context import get_session_model
        session_model = get_session_model()

    ov = (rt.get_all().get(key) or "").strip()
    can_inherit, _reason = can_inherit_session(session_model, capability)

    # 开关关闭 = 不用专门配的生成模型,优先改用当前对话所选的模型;
    # 但会话模型真出不了图/片时回落专用模型,而不是必然失败。
    if not gen_enabled(capability):
        if session_model and can_inherit:
            return session_model, "session"
        return _fallback_gen_model(capability, base)

    if ov and ov != SESSION_MODEL_MARK:
        return ov, "override"
    # 显式选了「跟随对话模型」:用户意图优先,但该模型确实不能生成时仍要回落,
    # 否则选中即等于"这项能力永久不可用"。
    if ov == SESSION_MODEL_MARK:
        if session_model and can_inherit:
            return session_model, "session"
        return _fallback_gen_model(capability, base)
    if can_inherit:
        return session_model, "session"
    # 走到这里:没配且对话模型不能继承。必须确保不会把哨兵当模型名返回。
    return rt.resolve(key, base)


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
async def get_models(
    session_model: str = Query(default="", description="当前对话所用的模型 id;用于展示「跟随对话模型」的实际生效值"),
) -> dict:
    models = _current(session_model=(session_model or "").strip())
    return {
        "models": _with_candidates(models),
        "overrides": rt.get_all(),
        "gen_endpoint": _endpoint_info(),
        "session_model": (session_model or "").strip(),
    }


@router.post("/models")
async def set_models(
    req: ModelOverride,
    session_model: str = Query(default="", description="当前对话所用的模型 id;用于回显「跟随对话模型」的实际生效值"),
) -> dict:
    # 只处理调用方显式传入的字段,避免未传的字段被默认值 "" 误清除
    payload = req.model_dump(exclude_unset=True)
    pairs = {k: v for k, v in payload.items() if k in OVERRIDABLE}
    # 开关是 bool,config_runtime 只收字符串:
    #   关闭 → "0"(真实写进文件);恢复 → ""(set_many 用空串删除覆盖,回到默认启用),
    #   这样 config_overrides.json 里只会留下"被关掉"的那一条,不会堆一堆 "1"。
    for capability, key in ENABLED_KEYS.items():
        if key in pairs:
            pairs[key] = "" if pairs[key] else "0"
    rt.set_many(pairs)
    # 保存后回显:沿用本次请求带的对话模型(若有),让「当前生效」显示真实继承结果
    _session = (session_model or "").strip()
    return {
        "ok": True,
        "models": _with_candidates(_current(session_model=_session)),
        "overrides": rt.get_all(),
        "gen_endpoint": _endpoint_info(),
        "session_model": _session,
    }
