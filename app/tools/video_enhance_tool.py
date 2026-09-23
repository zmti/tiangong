"""视频增强工具:为已生成的视频做后期处理。

能力(均可组合):
  - 配音(dub):用 edge-tts 把文字合成中文语音,混入/替换原音轨
  - 字幕(subtitle):把配音文本烧录为硬字幕(中文 CJK 字体)
  - 水印(watermark):在右下角叠加半透明文字水印
  - 原音保留:可选择保留并降低原音音量,与配音混合

输入 `video_path` 支持:
  - 本地文件绝对/相对路径
  - "/videos/xxx.mp4" 相对 URL(TianGong 本地静态资源)
  - "http(s)://host:port/videos/xxx.mp4" 远程 URL(自动下载)

输出统一保存到 data/videos/,返回 "/videos/{name}" 相对 URL(与图片工具一致,
避免 Electron 打包后端口不一致导致 404)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid

from pydantic import Field

from app.config import settings
from app.tools.registry import tool


logger = logging.getLogger(__name__)

# 与 app/api/tts.py 保持一致的音色表(中文名 -> edge-tts 音色)
VOICES = {
    "晓晓": "zh-CN-XiaoxiaoNeural",
    "云希": "zh-CN-YunxiNeural",
    "云健": "zh-CN-YunjianNeural",
    "晓伊": "zh-CN-XiaoyiNeural",
    "小宁": "zh-CN-XiaoxiaoNeural",
    "男声-云希": "zh-CN-YunxiNeural",
}

# 中文字幕/水印字体(Windows 自带,fontconfig 可解析)
SUB_FONT_NAME = "Microsoft YaHei"
WATERMARK_FONT_NAME = "Microsoft YaHei"


# ── 路径/目录辅助 ────────────────────────────────────────────────
def _videos_dir() -> str:
    path = os.path.join(settings.resolved_data_dir, "videos")
    os.makedirs(path, exist_ok=True)
    return path


def _video_url(name: str) -> str:
    """返回相对 URL,前端经 App.backendUrl 拼接完整地址(与图片工具一致)。"""
    return f"/videos/{name}"


def _find_cjk_fontfile() -> str | None:
    """定位一个含 CJK 字形的字体文件,供 drawtext 的 fontfile 使用。

    优先返回 .ttf(单文件,FreeType 直接加载最稳);.ttc 集合也能用,但 .ttf 更省心。
    注意:不要用 fontconfig 字体族名,本机 fontconfig 无配置文件会直接崩。
    """
    candidates = [
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simsun.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


# ── 输入解析 ─────────────────────────────────────────────────────
def _http_download(url: str, dest: str) -> None:
    import requests

    with requests.get(url, stream=True, timeout=120, verify=False) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)


def _resolve_input(video_path: str) -> str:
    """把各种形式的 video_path 解析为本地文件路径。"""
    if video_path.startswith(("http://", "https://")):
        dest = os.path.join(_videos_dir(), f"_src_{uuid.uuid4().hex}.mp4")
        _http_download(video_path, dest)
        return dest
    if video_path.startswith("/videos/"):
        return os.path.join(_videos_dir(), os.path.basename(video_path))
    if os.path.exists(video_path):
        return video_path
    raise FileNotFoundError(f"找不到视频文件: {video_path}")


# ── ffmpeg 探测 ──────────────────────────────────────────────────
def _probe(path: str) -> dict:
    """返回 {duration: float, has_audio: bool}。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-show_entries", "stream=index,codec_type",
        "-of", "json", path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    info = json.loads(out.stdout or "{}")
    dur = 0.0
    try:
        dur = float(info.get("format", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        dur = 0.0
    has_audio = any(
        s.get("codec_type") == "audio" for s in info.get("streams", [])
    )
    return {"duration": dur, "has_audio": has_audio}


# ── 字幕生成 ─────────────────────────────────────────────────────
def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;\n])", text)
    return [p.strip() for p in parts if p.strip()]


def _srt_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _build_srt(text: str, duration: float, path: str) -> None:
    sents = _split_sentences(text)
    if not sents:
        return
    if len(sents) == 1:
        cues = [(0.0, max(duration, 1.0), sents[0])]
    else:
        total_chars = sum(len(s) for s in sents) or 1
        cues = []
        t = 0.0
        for s in sents:
            d = max(0.5, duration * len(s) / total_chars)
            cues.append((t, t + d, s))
            t += d
    with open(path, "w", encoding="utf-8") as f:
        for i, (a, b, txt) in enumerate(cues, 1):
            f.write(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{txt}\n\n")


# ── TTS(edge-tts) ────────────────────────────────────────────────
def _rate_string(speed: float) -> str:
    pct = int(round((speed - 1.0) * 100))
    if pct >= 0:
        return f"+{pct}%"
    return f"{pct}%"  # 负数自带负号,如 -20%


async def _tts_to_file(text: str, voice: str, rate: str, out_path: str) -> bool:
    """用 edge-tts 合成语音到 mp3。成功返回 True,失败(如离线)返回 False。"""
    try:
        import edge_tts
    except ImportError:
        logger.warning("edge-tts 未安装,跳过配音")
        return False
    try:
        comm = edge_tts.Communicate(text, voice, rate=rate)
        with open(out_path, "wb") as f:
            async for chunk in comm.stream():
                if chunk.get("type") == "audio":
                    f.write(chunk["data"])
        if os.path.getsize(out_path) == 0:
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("TTS 合成失败(可能需要联网): %s", e)
        return False


# ── ffmpeg 封装 ──────────────────────────────────────────────────
def _quote(v: str) -> str:
    """ffmpeg 过滤器单引号包裹,内部单引号替换为弯引号避免语法错误。

    同时把 Windows 反斜杠路径转为正斜杠——ffmpeg 过滤器解析器把反斜杠当转义符,
    会导致 'No option name near ...' 错误。
    """
    v = v.replace("\\", "/")
    return "'" + v.replace("'", "’") + "'"


def _run_ffmpeg(cmd: list[str], cwd: str | None = None) -> None:
    logger.info("ffmpeg: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败({proc.returncode}): {proc.stderr[-800:]}")


# ── 主工具 ───────────────────────────────────────────────────────
@tool(
    "enhance_video",
    "增强已生成的视频:支持中文配音(edge-tts)、硬字幕烧录、文字水印、保留并混合原音轨。"
    "可组合使用,适合给 AI 生成的短视频加解说、字幕和品牌水印。",
    timeout=300,
    enabled=True,
    terminal=True,
)
async def enhance_video(
    video_path: str = Field(description="源视频:本地路径 / '/videos/xxx.mp4' 相对URL / 'http(s)://...' 远程URL"),
    dub_text: str = Field(default="", description="要配音/加字幕的文本(留空=不配音不加字幕,只做水印等处理)"),
    voice: str = Field(default="zh-CN-XiaoxiaoNeural", description="TTS 音色:zh-CN-XiaoxiaoNeural / zh-CN-YunxiNeural / zh-CN-YunjianNeural / 或用中文名(晓晓/云希/云健/晓伊)"),
    speed: float = Field(default=1.0, ge=0.5, le=2.0, description="配音语速(0.5x~2.0x,1.0=原速)"),
    subtitle: bool = Field(default=True, description="是否把 dub_text 烧录为硬字幕(需与 dub_text 配合使用)"),
    watermark: str = Field(default="", description="文字水印(如 '天工AI'),留空=不加"),
    keep_original_audio: bool = Field(default=True, description="是否保留原视频音轨(降低音量后与配音混合)"),
    original_volume: float = Field(default=0.3, ge=0.0, le=1.0, description="保留原音的音量(0~1)"),
) -> dict:
    """增强视频:配音 + 字幕 + 水印 + 音轨混合。"""
    tmp = tempfile.mkdtemp(prefix="vh_enh_")
    try:
        src = _resolve_input(video_path)
        probe = _probe(src)
        has_audio = probe["has_audio"]
        video_dur = probe["duration"] or 0.0

        # 解析音色
        if voice in VOICES:
            voice = VOICES[voice]

        applied: dict = {
            "dub": False,
            "subtitle": False,
            "watermark": bool(watermark),
            "keep_original_audio": keep_original_audio,
        }

        tts_path: str | None = None
        if dub_text.strip():
            tts_path = os.path.join(tmp, "tts.mp3")
            ok = await _tts_to_file(dub_text, voice, _rate_string(speed), tts_path)
            if ok:
                applied["dub"] = True
                # 若有配音,字幕时长以配音音频时长为准更贴合
                if subtitle:
                    try:
                        video_dur = _probe(tts_path)["duration"] or video_dur
                    except Exception:  # noqa: BLE001
                        pass

        # 字幕文件
        sub_path: str | None = None
        if subtitle and dub_text.strip():
            sub_path = os.path.join(tmp, "sub.srt")
            _build_srt(dub_text, video_dur, sub_path)
            applied["subtitle"] = True

        # 构建过滤器图(各视频滤镜收集到列表,用逗号连接,避免空滤镜)
        vfilters: list[str] = []
        if sub_path:
            # 字幕文件名用相对名 + cwd=tmp 规避 Windows 盘符冒号被过滤器解析器
            # 误判为选项分隔符的问题(绝对路径 C:/... 会触发 'original_size' 报错)。
            style = (
                f"FontName={SUB_FONT_NAME},FontSize=28,"
                f"PrimaryColour=&H00FFFFFF,OutlineColour=&H0000000000,Outline=2"
            )
            vfilters.append(
                f"subtitles={_quote(os.path.basename(sub_path))}:force_style={_quote(style)}"
            )
        if watermark:
            # 水印字体:把 CJK 字体文件拷到临时目录,用相对名 + cwd=tmp 引用,
            # 这样 drawtext 走 FreeType 直接加载,既不触发 Windows 盘符冒号解析错误,
            # 也避开本机 fontconfig 无配置导致崩溃。
            wm = f"text={_quote(watermark)}"
            cjk_font = _find_cjk_fontfile()
            if cjk_font:
                ext = os.path.splitext(cjk_font)[1] or ".ttf"
                wm_font = os.path.join(tmp, "wmfont" + ext)
                shutil.copyfile(cjk_font, wm_font)
                wm += f":fontfile={_quote('wmfont' + ext)}"
            else:
                wm += f":font={_quote(WATERMARK_FONT_NAME)}"
            wm += (
                ":fontcolor=white:fontsize=28:"
                "x=w-tw-24:y=h-th-24:box=1:boxcolor=black@0.4:boxborderw=6"
            )
            vfilters.append(f"drawtext={wm}")
        video_chain = "[0:v]" + ",".join(vfilters) + "[vout]"

        # 音频图
        audio_graph: str | None = None
        shortest = False
        if tts_path and applied["dub"]:
            if has_audio and keep_original_audio and original_volume > 0:
                audio_graph = (
                    f"[0:a]volume={original_volume}[a0];"
                    f"[1:a]volume=1[a1];"
                    f"[a0][a1]amix=inputs=2:duration=first:dropout_transition=2[aout]"
                )
            else:
                audio_graph = "[1:a]anull[aout]"
                shortest = True  # 纯配音:以视频长度为准截断
        elif has_audio:
            audio_graph = "[0:a]anull[aout]"

        fc_parts = [video_chain]
        if audio_graph:
            fc_parts.append(audio_graph)
        filter_complex = ";".join(fc_parts)

        # 组装命令
        out_name = f"enhanced_{uuid.uuid4().hex}.mp4"
        out_path = os.path.join(_videos_dir(), out_name)
        cmd = ["ffmpeg", "-y", "-i", src]
        if tts_path and applied["dub"]:
            cmd += ["-i", tts_path]
        cmd += ["-filter_complex", filter_complex]
        cmd += ["-map", "[vout]"]
        if audio_graph:
            cmd += ["-map", "[aout]"]
        if shortest:
            cmd += ["-shortest"]
        cmd += [
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            out_path,
        ]
        _run_ffmpeg(cmd, cwd=tmp)

        return {
            "format": "local_url",
            "video_url": _video_url(out_name),
            "duration": round(video_dur, 2),
            "message": "视频增强完成",
            "applied": applied,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # 清理远程下载的临时源文件
        if video_path.startswith(("http://", "https://")) and "src" in locals():
            try:
                os.remove(src)
            except Exception:  # noqa: BLE001
                pass
