"""视频工具:视频生成(基于 agnes /v1/videos 异步任务)。

创建视频任务后轮询 agnesapi 直到完成,返回视频地址。
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx
import requests
from pydantic import Field

from app.config import settings
from app.core.config_runtime import resolve, resolve_value
from app.tools.registry import tool


def _video_enabled() -> bool:
    """无任意生成端点时自动禁用视频工具(按 provider 路由后,compat/自定义均可)。"""
    from app.api.config_models import _any_gen_base_url
    return _any_gen_base_url()


def _video_endpoint(model: str = "") -> tuple[str, str]:
    """按模型反查其 provider 端点(base_url, api_key),查不到回退 compat。

    实现「按 provider 路由」:选了某 provider 下的视频模型,请求发到该 provider
    的端点,而不再硬编码 openai_compat_*(apihub)。
    """
    from app.api.config_models import gen_endpoint_for_model
    model = (model or "").strip() or resolve_value("video_model", settings.video_model)
    ep = gen_endpoint_for_model(model, "video")
    return ep["base_url"], (ep["api_key"] or "")


def _headers(api_key: str = "") -> dict:
    if not api_key:
        # 兜底:取当前生效视频模型的 provider 端点 key
        api_key = _video_endpoint("")[1] or "sk-placeholder"
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _videos_dir() -> str:
    path = os.path.join(settings.resolved_data_dir, "videos")
    os.makedirs(path, exist_ok=True)
    return path


def _save_video(data: bytes) -> str:
    """保存视频字节到本地,返回相对文件名。"""
    name = f"{uuid.uuid4().hex}.mp4"
    with open(os.path.join(_videos_dir(), name), "wb") as f:
        f.write(data)
    return name


def _video_url(name: str) -> str:
    """返回相对 URL,前端经 App.backendUrl 拼接完整地址(与图片工具一致)。

    不拼绝对地址,避免 agent_host/agent_port 配置与实际运行端口不一致时
    (如 Electron 打包后固定 8517 端口)前端加载视频 404。
    """
    return f"/videos/{name}"


# ---------------------------------------------------------------------------
# 上游视频接口的「模型契约」
#
# Agnes /v1/videos 对不同模型有各自独立的字段白名单(实测:传错字段会被网关
# 逐个点名 400 invalid_request),所以**不能一套参数打通所有模型**:
#
#   agnes-video-2.5 / agnes-video-2.5-flash —— OpenAI Videos 兼容的 Flash 契约
#       ✅ model / prompt / mode(text|keyframe|reference) / seconds("4"~"12")
#          / size(必须是 "720P") / aspect_ratio / n(必须 1)
#       ✅ 图:mode=reference + images[] (≤5) ;mode=keyframe + first_frame/last_frame
#       ❌ width / height / frame_rate / num_frames / duration 一律不收
#       官方文档:https://www.agnes-ai.com/en/docs/agnes-video-25-flash
#
#   agnes-video-v2.0 —— 旧契约
#       ✅ model / prompt (+ image 裸 base64;容忍 width/height/num_frames/
#          frame_rate/duration/size,但这些字段基本被忽略)
#       ❌ 不认 mode
#
# 2026-09-22 的故障根因:用户把「视频生成模型」改成 agnes-video-2.5-flash 后,
# 工具仍按 v2.0 的参数发 width/height/frame_rate → 网关逐个拒绝,
# generate_video 全线 400(证据:会话 desktop-muc7dv38 第 145 条工具返回)。
# ---------------------------------------------------------------------------
FLASH_VIDEO_MODELS = ("agnes-video-2.5-flash", "agnes-video-2.5")
ASPECT_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")

# 上游"忙"的状态码:重试有意义(429 限流 / 503 video_queue_full 等)
_RETRY_CODES = frozenset({429, 500, 502, 503, 504})
_CREATE_ROUNDS = 4        # 创建任务最多整体重试几轮
_CREATE_RETRY_WAIT = 20   # 每轮之间等待秒数(工具 timeout=700,留足出片时间)


def _is_flash_contract(model: str) -> bool:
    m = (model or "").strip().lower()
    return m in FLASH_VIDEO_MODELS or m.startswith("agnes-video-2.5")


def _flash_image_ref(image_src: str, image_b64: str) -> str:
    """Flash 契约的图片入参:公网 URL 原样传,本地图转成 data URI 内联。

    官方文档写的是"图片 URL 必须公网可访问";本地文件(如 /images/xxx.png)
    Agnes 取不到,这里用 data URI 内联一试——不行会拿到 400 校验错误,
    由候选契约链自动降级为 mode=text(纯文生)。
    """
    s = (image_src or "").strip()
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return "data:image/png;base64," + image_b64


def _content_urls(task_id: str, model: str = "") -> list[str]:
    """经 API 主机取视频本体(OpenAI Videos 兼容的 /content 端点)。

    为什么必须优先走它:上游完成响应给的下载链接在 CDN 上
    (`platform-outputs.agnes-ai.space`),而实测该域从本机**完全不可达**:
        curl -I https://platform-outputs.agnes-ai.space/...  -> http=000
    老的镜像域 `cos-platform-outputs.agnes-ai.cn` 可达(200),但上面**没有**
    agnes-video-2.5 的新任务(404)。结果就是:能出片,却下不回来、
    前端拿到一个打不开的远程链接。
    而 GET {base}/videos/{id}/content 由 apihub 直接吐字节(实测 200 video/mp4),
    完全绕开 CDN —— 这是目前唯一稳定可用的取片方式。
    """
    base = _video_endpoint(model)[0].rstrip("/")
    urls = [f"{base}/videos/{task_id}/content"]
    if model:
        urls.append(f"{base}/videos/{task_id}/content?model_name={model}")
    return urls


async def _download_video(url: str, timeout: float = 180) -> bytes:
    """下载远程视频字节。

    带上鉴权头:/content 端点需要 Bearer;CDN 链接不需要但多带无害。
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("GET", url, headers=_headers()) as resp:
            resp.raise_for_status()
            return await resp.aread()


def _resolve_image_base64(src: str) -> str:
    """把图片输入统一转成裸 base64 字符串(上游只接受 base64,见实测结论)。

    实测(apihub /v1/videos):传 URL 会得到
        "image base64 decode failed: Only base64 data is allowed"
    即上游**只认 base64**,不认 URL。而本地产物(/images/xxx.png → 127.0.0.1)
    上游在公网根本取不到,所以必须在本地读文件编码后再传。

    支持四种输入:
      1. 相对 URL   /images/xxx.png、/videos/xxx.mp4(后者自动抽首帧)
      2. 本地路径   绝对路径或相对 data 目录的路径
      3. 远程 URL   http(s)://...(先下载再编码)
      4. 裸 base64  直接透传
    """
    import base64 as _b64

    src = (src or "").strip()
    if not src:
        return ""

    raw = b""

    def _read(path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    # 1) 相对静态 URL:/images/xxx.png、/videos/xxx.mp4
    if src.startswith("/images/") or src.startswith("/videos/"):
        rel = src.lstrip("/").replace("/", os.sep)
        path = os.path.join(settings.resolved_data_dir, rel)
        raw = _read(path)
    # 2) 本地路径
    elif os.path.isfile(src):
        raw = _read(src)
    elif src.startswith("http://") or src.startswith("https://"):
        # 3) 远程 URL:下载后编码(上游拿不到我们的内网地址,只能由本地代取)
        r = requests.get(src, timeout=180, verify=False)
        r.raise_for_status()
        raw = r.content
    else:
        # 4) 看起来就是裸 base64,直接透传(去掉可能的 data URI 前缀)
        if "," in src and src.split(",", 1)[0].startswith("data:"):
            src = src.split(",", 1)[1]
        return src

    # 视频输入:上游无 video 字段(实测被忽略),退化为"取首帧 → 图生视频"
    if raw[:4] in (b"\x00\x00\x00\x18", b"\x00\x00\x00\x20") or src.lower().endswith(".mp4"):
        raw = _extract_first_frame(raw)

    return _b64.b64encode(raw).decode("ascii")


def _extract_first_frame(video_bytes: bytes) -> bytes:
    """从视频字节里抽第一帧,返回 PNG 字节(用于"视频续写"降级为图生视频)。"""
    import subprocess
    import tempfile

    tmp_in = os.path.join(tempfile.gettempdir(), f"_frame_in_{uuid.uuid4().hex}.mp4")
    tmp_out = os.path.join(tempfile.gettempdir(), f"_frame_out_{uuid.uuid4().hex}.png")
    try:
        with open(tmp_in, "wb") as f:
            f.write(video_bytes)
        subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_in, "-frames:v", "1", tmp_out],
            capture_output=True,
            timeout=60,
        )
        if os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > 0:
            with open(tmp_out, "rb") as f:
                return f.read()
        raise RuntimeError("ffmpeg 抽首帧失败")
    finally:
        for p in (tmp_in, tmp_out):
            try:
                os.remove(p)
            except OSError:
                pass


async def _create_video(
    prompt: str,
    seconds: int,
    aspect_ratio: str = "16:9",
    image_src: str = "",
    image_b64: str = "",
) -> tuple[str, str, dict, str]:
    """创建视频任务。

    返回 (task_id, 上游接单的模型名, 创建响应原文, 本次命中的契约描述)。

    按模型契约组织候选请求体,依次尝试;只有 400「校验不过」才换下一个
    (官方说明:校验失败直接 400,不建任务、不产生费用),429/5xx 直接抛出让上层看到。
    """
    # 模型可配置:桌面端覆盖 > .env/VIDEO_MODEL > 代码默认值 agnes-video-v2.0
    model = resolve_value("video_model", settings.video_model)
    # 按 provider 路由:请求发到该模型所属 provider 的端点(不再硬编码 compat)
    base_url, api_key = _video_endpoint(model)
    if not base_url:
        raise RuntimeError(f"视频生成失败: 模型 {model} 所属端点未配置 base_url")

    payloads: list[tuple[str, dict]] = []
    image_ref = _flash_image_ref(image_src, image_b64) if image_b64 else ""
    if _is_flash_contract(model):
        # Flash 契约:mode 必填;seconds 是字符串;size 只能 "720P";n 只能 1
        common = {
            "model": model,
            "prompt": prompt,
            "size": "720P",
            "aspect_ratio": aspect_ratio if aspect_ratio in ASPECT_RATIOS else "16:9",
            "seconds": str(min(12, max(4, int(seconds)))),
            "n": 1,
        }
        if image_ref:
            # 参考图:优先 images[] 参考模式,退到 keyframe 首帧模式
            payloads.append(("flash/reference", {**common, "mode": "reference", "images": [image_ref]}))
            payloads.append(("flash/keyframe", {**common, "mode": "keyframe", "first_frame": image_ref}))
        # 兜底:纯文生(参考图不被接受时不至于整次失败)
        payloads.append(("flash/text", {**common, "mode": "text"}))
    else:
        # 旧契约(v2.0 等):只认 model/prompt,图片放 image 且必须是裸 base64
        legacy = {"model": model, "prompt": prompt}
        if image_b64:
            legacy["image"] = image_b64
        payloads.append(("legacy", legacy))

    # base_url 可能已含 /v1(如 https://apihub.agnes-ai.cn/v1),避免拼出 /v1/v1/videos
    if base_url.endswith("/v1"):
        api = base_url[: -len("/v1")] + "/v1/videos"
    else:
        api = base_url + "/videos"

    last_err = ""
    async with httpx.AsyncClient(timeout=60) as client:
        # 外层轮次:整条契约链都只是"上游忙"时,整体退避后重来
        for attempt in range(_CREATE_ROUNDS):
            busy = False
            for desc, payload in payloads:
                resp = await client.post(api, json=payload, headers=_headers(api_key))
                if resp.status_code == 400:
                    # 该契约不被这个模型接受,换下一个候选
                    last_err = f"[{desc}] {resp.status_code} {resp.text[:200]}"
                    continue
                if resp.status_code in _RETRY_CODES:
                    # 429 限流 / 503 video_queue_full("视频队列已满,请稍后重试")
                    # —— 上游容量问题,不是参数问题;换契约也没用,退避后重试。
                    # 实测 2026-09-22 连续踩到 503 video_queue_full,原来直接抛错,
                    # 等于把用户 700s 的等待时间白白浪费掉。
                    busy = True
                    last_err = f"[{desc}] {resp.status_code} {resp.text[:200]}"
                    continue
                if resp.status_code >= 400:
                    # 其它 4xx(参数/鉴权)换契约或重试都无意义,直接抛出原样呈现
                    raise RuntimeError(f"视频任务创建失败({desc}): {resp.status_code} {resp.text[:200]}")
                body = resp.json()
                task_id = body.get("id") or body.get("task_id") or body.get("video_id") or ""
                # 上游会在响应里回显真正接单的模型;优先用它,便于排查"我选的模型没生效"
                return task_id, (body.get("model") or model), body, desc
            if not busy or attempt == _CREATE_ROUNDS - 1:
                break
            await asyncio.sleep(_CREATE_RETRY_WAIT)
    raise RuntimeError(f"视频任务创建失败(已按模型契约逐个尝试): {last_err}")


async def _poll_video(task_id: str, model: str = "", timeout: float = 600) -> dict:
    """轮询视频任务,直到 completed 或 failed,返回最终结果。

    查询端点依次尝试,命中哪个就固定用哪个(避免每轮都试错):
      1. GET {base}/videos/{task_id}?model_name={model}  —— OpenAI 风格主端点
      2. GET {base}/videos/{task_id}                     —— 不带 model_name
      3. GET {host}/agnesapi?video_id=..&model_name=..   —— 旧 BFF 端点
      4. GET {host}/agnesapi?video_id=..                 —— 旧 BFF 不带 model_name
    官方文档要求非 text 模式查询必须带 model_name(Flash 契约的 reference/keyframe),
    所以主端点先带 model_name 试,再退到不带。
    """
    base_url, _ = _video_endpoint(model)
    if base_url.endswith("/v1"):
        host = base_url[: -len("/v1")] or base_url
    else:
        host = base_url
    q = f"model_name={model}" if model else ""
    endpoints = [
        f"{base_url}/videos/{task_id}" + (f"?{q}" if q else ""),
        f"{base_url}/videos/{task_id}",
        f"{host}/agnesapi?video_id={task_id}" + (f"&{q}" if q else ""),
        f"{host}/agnesapi?video_id={task_id}",
    ]
    start = time.time()
    last_progress = -1
    sticky = 0  # 已确认可用的端点下标
    flaky = 0  # 连续失败次数(网络抖动 / 5xx 允许重试)
    async with httpx.AsyncClient(timeout=30) as client:
        while time.time() - start < timeout:
            data = None
            err = ""
            order = [sticky] + [i for i in range(len(endpoints)) if i != sticky]
            for idx in order:
                try:
                    resp = await client.get(endpoints[idx], headers=_headers())
                except Exception as e:  # noqa: BLE001
                    # 网络抖动(实测轮询 10 次里会掉 2~3 次)。一次查询失败不代表
                    # 任务没了,直接抛会把跑了 5 分钟的生成判死 —— 这里只记数重试。
                    err = f"{type(e).__name__}: {e}"
                    continue
                if resp.status_code >= 400:
                    err = f"{resp.status_code} {resp.text[:200]}"
                    # 401/403/404 = 任务确实查不到,重试无意义;429/5xx 才值得等
                    if resp.status_code in (401, 403, 404):
                        raise RuntimeError(f"视频查询失败: {err}")
                    continue
                try:
                    data = resp.json()
                except Exception as e:  # noqa: BLE001
                    err = f"{resp.status_code} 非 JSON 响应: {e}"
                    continue
                sticky = idx
                break
            if data is None:
                flaky += 1
                if flaky >= 5:
                    raise RuntimeError(f"视频查询连续失败 {flaky} 次: {err}")
                await asyncio.sleep(5)
                continue
            flaky = 0
            status = data.get("status") or data.get("internal_status")
            progress = data.get("progress") or data.get("internal_progress") or 0
            if progress != last_progress:
                last_progress = progress
            if status == "completed" or data.get("completed_at"):
                return data
            if status == "failed" or data.get("error"):
                raise RuntimeError(f"视频生成失败: {data.get('error') or status}")
            await asyncio.sleep(5)
    raise RuntimeError(f"视频生成超时({int(timeout)}s),请稍后重试")


@tool(
    "generate_video",
    "根据文字描述生成一段短视频,支持文生视频与图生视频。"
    "传入 image 即以该图片为首帧/参考图生成视频(图片续写);不传则为纯文生视频。"
    "注意:视频生成需要几分钟,请耐心等待结果",
    timeout=700,
    enabled=_video_enabled(),
    terminal=True,
)
def generate_video(
    prompt: str = Field(description="视频内容描述,越具体越好(主体/动作/场景/镜头/光线/风格)"),
    duration: int = Field(5, description="视频时长(秒)。agnes-video-2.5-flash 只支持 4~12(超出会自动收敛到该区间)"),
    image: str = Field(
        default="",
        description=(
            "可选。参考图片(图生视频):公网 http(s) 图片链接最稳;"
            "也支持本地产物 '/images/xxx.png'、本地文件路径或裸 base64(会以内联图片提交)。"
            "传 '/videos/xxx.mp4' 时会自动取该视频首帧作为参考图。"
            "留空 = 纯文生视频。"
        ),
    ),
    aspect_ratio: str = Field(
        default="16:9",
        description="画面比例,支持 21:9 / 16:9 / 4:3 / 1:1 / 3:4 / 9:16(仅 agnes-video-2.5* 生效)",
    ),
) -> str:
    duration = max(4, min(12, duration))

    # 图生视频:把各种形态的图片输入统一成上游要的形态
    # (Flash 契约用 URL/data URI;旧契约用裸 base64)
    # 注意:_image_warning 必须先初始化——不传图(纯文生视频)时不会进入下面的分支,
    # 若只在该分支内赋值,后面读取会抛 UnboundLocalError。
    image_b64 = ""
    _image_warning = ""
    if image and image.strip():
        try:
            image_b64 = _resolve_image_base64(image)
        except Exception as e:  # noqa: BLE001
            # 参考图解析失败不应让整次生成失败:降级为文生视频,并在结果里说明
            image_b64 = ""
            _image_warning = f"参考图解析失败,已降级为文生视频: {e}"
        else:
            # 没抛错但也没产出 base64(如空文件/不支持的格式),同样降级并提示。
            # 注意:try/except 后面只能跟 else/finally,不能接 elif。
            if not image_b64:
                _image_warning = "参考图未能转成 base64,已降级为文生视频"

    task_id, model, created, contract = asyncio.run(
        _create_video(prompt, duration, aspect_ratio=aspect_ratio, image_src=image, image_b64=image_b64)
    )
    # 参考图被丢弃(契约降级为纯文生)时补个说明,别让用户以为首帧还在
    if image_b64 and contract.endswith("/text") and not _image_warning:
        _image_warning = "参考图未被该模型接受,已按纯文生视频生成"
    data = asyncio.run(_poll_video(task_id, model=model))

    _model_source = resolve("video_model", settings.video_model)[1]
    # 时长/尺寸以上游为准:创建响应里有(seconds/size),完成响应里也可能有
    _upstream_seconds = data.get("seconds") or created.get("seconds")
    _upstream_size = data.get("size") or created.get("size")
    # ⚠ 实测(2026-09-22,agnes-video-2.5-flash):完成响应里**没有**顶层 video_url,
    # MP4 链接藏在 metadata.url 里 —— 只取顶层字段会拿到空串,
    # 前端显示"视频已生成"却播不出东西。这里按优先级全部扫一遍。
    _meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    _created_meta = created.get("metadata") if isinstance(created.get("metadata"), dict) else {}
    video_url = (
        data.get("video_url")
        or _meta.get("url")
        or _meta.get("video_url")
        or data.get("url")
        or data.get("file_url")
        or _created_meta.get("url")
        or ""
    )

    # 本地兜底:尝试把 mp4 下载到本地 data/videos,经 /videos 静态路由提供,
    # 避免外部 CDN 不可达导致视频无法播放;下载失败则回退原始远程 URL。
    # 顺序:先走 API 的 /content(实测唯一稳定可达),再退到上游给的 CDN 直链。
    _dl_urls = _content_urls(task_id, model) + ([video_url] if video_url else [])
    if task_id or video_url:
        try:
            raw = b""
            for _u in _dl_urls:
                try:
                    raw = asyncio.run(_download_video(_u))
                except Exception:  # noqa: BLE001
                    raw = b""
                if raw:
                    break
            if raw:
                local_name = _save_video(raw)
                res = {
                    "format": "local_url",
                    "duration": duration,
                    "model": model,
                    "model_source": _model_source,
                    "upstream_seconds": _upstream_seconds,
                    "upstream_size": _upstream_size,
                    "task_id": task_id,
                    "video_url": _video_url(local_name),
                    "remote_url": video_url,
                    "message": "视频已生成",
                }
                if _image_warning:
                    res["image_warning"] = _image_warning
                if image_b64:
                    res["request_contract"] = contract
                return res
        except Exception:
            pass  # 远程不可达/下载失败,回退原始链接

    res = {
        "format": "url",
        "duration": duration,
        "model": model,
        "model_source": _model_source,
        "upstream_seconds": _upstream_seconds,
        "upstream_size": _upstream_size,
        "task_id": task_id,
        "video_url": video_url,
        "message": "视频已生成",
    }
    if _image_warning:
        res["image_warning"] = _image_warning
    # 契约降级会**静默改变语义**(reference → keyframe → 纯文生),
    # 把命中的契约带出来,排查时不用再猜"我给的参考图到底用没上"
    if image_b64:
        res["request_contract"] = contract
    return res