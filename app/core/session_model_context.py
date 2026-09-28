"""会话模型上下文:让工具在运行时知道「当前对话用的是哪个模型」。

用途:生成模型(图片/视频)支持「跟随对话模型」——用户在设置里没有指定具体生成
模型(或显式选了"跟随对话模型")时,若当前对话所用的模型本身就带 image/video
能力(模型清单里的 capabilities 标记),就直接用它来生成,省得用户在两个地方
各配一遍、还容易配得不一致。

实现与 app/tools/project_context.py 同模式:ContextVar + 每次工具调用前重新 set。
为什么不做成工具参数一路透传:工具函数签名是扁平的(prompt / size / image ...),
这些参数值由 LLM 决定;中间再塞业务参数会污染工具的 JSON Schema,LLM 可能学着
瞎传。ContextVar 不进入入参契约,最干净。
"""
from __future__ import annotations

import contextvars

_session_provider: contextvars.ContextVar[str] = contextvars.ContextVar(
    "session_provider", default=""
)
_session_model: contextvars.ContextVar[str] = contextvars.ContextVar(
    "session_model", default=""
)


def set_session_model(provider: str = "", model: str = "") -> None:
    """记录本次对话实际使用的 provider / model。"""
    _session_provider.set(provider or "")
    _session_model.set(model or "")


def get_session_model() -> str:
    """当前对话的模型 id(未设置返回空串)。"""
    return _session_model.get()


def get_session_provider() -> str:
    """当前对话的 provider id(未设置返回空串)。"""
    return _session_provider.get()


def session_model_ref() -> str:
    """直观表示 "provider/model"(给日志/结果里看是哪个模型)。"""
    p = get_session_provider()
    m = get_session_model()
    return f"{p}/{m}" if (p and m) else (m or "")
