"""工具执行器:支持并行调用、超时隔离、瞬态错误重试、工具路由与确认流。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from app.config import settings
from app.core.llm import ToolCall
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 瞬态错误特征(命中即判定为重试)
_TRANSIENT_PATTERN = (
    "timeout", "timed out", "rate limit", "429", "overloaded",
    "connection", "network", "500", "502", "503", "504",
    "temporarily unavailable", "try again",
)
# 测试/构建失败特征(用于给出定向修复提示)
_TEST_FAIL_MARKERS = (
    "failed", "error", "assertionerror", "traceback", "syntaxerror",
    "importerror", "namerror", "typeerror", "valueerror",
)
# 计划工具(不由工具执行器处理)
PLAN_TOOLS = {"set_plan", "update_plan", "get_plan"}
# 不重试工具
NO_RETRY_TOOLS = {"read_file", "write_file", "edit_file"}
# 系统常驻工具
SYSTEM_TOOLS = {"read_file", "write_file", "edit_file", "run_code", "run_terminal"}

_has_profiling = False
_has_failure_memory = False
_has_success_memory = False
try:
    from app.core.profiling import get_profile_manager, ProfileRecord  # noqa: F401
    _has_profiling = True
except Exception:  # noqa: BLE001
    pass
try:
    from app.memory.failure_memory import get_failure_memory  # noqa: F401
    _has_failure_memory = True
except Exception:  # noqa: BLE001
    pass
try:
    from app.memory.success_memory import get_success_memory  # noqa: F401
    _has_success_memory = True
except Exception:  # noqa: BLE001
    pass


class ToolExecutor:
    """工具执行器。"""

    def __init__(
        self,
        registry: ToolRegistry,
        session_id: str = "",
        approve_tools: Optional[list[str]] = None,
        cancel_check=None,
    ):
        self.registry = registry
        self.session_id = session_id
        self.approve_tools = list(approve_tools or [])
        self._cancel_check = cancel_check
        # 工具路由:预选后只把相关工具子集暴露给模型;None=未路由(全量)
        self._active_tools: Optional[list[str]] = None
        self._always_tools = list(SYSTEM_TOOLS)
        # 工具结果去重缓存:同一运行内 (tool, args) -> {"result": str, "ok": bool}
        self._tool_cache: dict[tuple[str, str], dict] = {}
        # interrupt 式确认流:本次运行已由用户批准的工具名(runtime 运行时集合)
        self._runtime_approved: set[str] = set()
        # 危险工具集合(来自配置)
        self._dangerous_tools = {
            s.strip() for s in settings.agent_dangerous_tools.split(",") if s.strip()
        }
        # 危险工具频率限制:每个工具名 -> 最近 N 秒内的调用时间戳列表
        self._dangerous_tool_calls: dict[str, list[float]] = {}
        # 每分钟危险工具最大调用次数(按工具名分别计数)
        self._dangerous_tool_rate_limit = int(settings.agent_dangerous_tool_rate_limit or 10)

    def get_schemas(self, registry: Optional[ToolRegistry] = None) -> list[dict]:
        """获取当前活跃工具的 OpenAI Schema 列表。"""
        reg = registry or self.registry
        if self._active_tools is None:
            tools = reg.list()
        else:
            active = set(self._active_tools) | set(self._always_tools)
            tools = [t for t in reg.list() if t.name in active]
        return [t.to_openai_schema() for t in tools]

    @staticmethod
    def is_tool_error(result_str: str) -> bool:
        try:
            data = json.loads(result_str)
            if isinstance(data, dict):
                if data.get("error"):
                    return True
                if data.get("success") is False or data.get("blocked") is True:
                    return True
        except (json.JSONDecodeError, TypeError):
            pass
        return False

    @staticmethod
    def _normalize_cache_key(arguments: str) -> str:
        """标准化参数为缓存键。"""
        if not arguments or not arguments.strip():
            return arguments or ""
        s = arguments.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, (dict, list)):
                    return json.dumps(parsed, sort_keys=True, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                pass
        import hashlib
        return "h:" + hashlib.md5(s.encode()).hexdigest()[:16]

    @classmethod
    def _is_transient_error(cls, result_str: str) -> bool:
        """判断工具结果是否为可重试的瞬态错误。"""
        try:
            data = json.loads(result_str)
            if isinstance(data, dict) and data.get("error"):
                text = str(data["error"]).lower()
            else:
                return False
        except (json.JSONDecodeError, TypeError):
            return False
        return any(pat in text for pat in _TRANSIENT_PATTERN)

    @classmethod
    def _looks_like_test_failure(cls, name: str, result_str: str) -> bool:
        """判断工具结果是否为测试/构建失败输出。"""
        if name != "run_terminal":
            return False
        try:
            data = json.loads(result_str)
            if not isinstance(data, dict):
                return False
        except (json.JSONDecodeError, TypeError):
            return False
        out = str(data.get("stdout", "")) + "\n" + str(data.get("stderr", ""))
        low = out.lower()
        return any(m in low for m in _TEST_FAIL_MARKERS)

    def _check_cancel(self) -> None:
        """检查是否被取消。"""
        if self._cancel_check and self._cancel_check():
            raise asyncio.CancelledError("Agent 运行已被外部取消")

    async def execute_tool(self, name: str, arguments: str) -> str:
        """执行单个工具(带瞬态错误重试/退避)。"""
        t0 = time.time()
        if name in PLAN_TOOLS:
            return json.dumps({"error": "计划工具应由 Agent 层处理"}, ensure_ascii=False)
        if name in NO_RETRY_TOOLS:
            result = await self._execute_tool_once(name, arguments)
            self._profile_tool(name, t0, result)
            return result
        retries = max(0, int(settings.agent_tool_retries))
        for attempt in range(retries + 1):
            result = await self._execute_tool_once(name, arguments)
            if attempt < retries and self._is_transient_error(result):
                await asyncio.sleep(settings.agent_tool_retry_delay * (2 ** attempt))
                continue
            self._profile_tool(name, t0, result)
            return result
        return result

    async def execute_tool_streaming(
        self, name: str, arguments: str,
        on_chunk: Optional[callable] = None,
    ) -> str:
        """流式执行单个工具。"""
        from app.config import settings as _cfg
        chunk_size = int(getattr(_cfg, "agent_tool_streaming_chunk_size", 1024) or 1024)
        result = await self.execute_tool(name, arguments)
        if on_chunk and chunk_size > 0 and result:
            for i in range(0, len(result), chunk_size):
                chunk = result[i:i + chunk_size]
                on_chunk(chunk)
        return result

    def _profile_tool(self, name: str, t0: float, result: str) -> None:
        """记录工具执行性能数据 + 失败经验。"""
        if _has_profiling:
            try:
                from app.core.profiling import get_profile_manager, ProfileRecord
                pm = get_profile_manager()
                pm.record(ProfileRecord(
                    run_id="",
                    session_id=self.session_id,
                    kind="tool", name=name,
                    duration=time.time() - t0,
                    success=not self.is_tool_error(result),
                    error=(result[:200] if self.is_tool_error(result) else ""),
                ))
            except Exception:
                pass
        if _has_failure_memory and self.is_tool_error(result):
            try:
                from app.core.telemetry import _current_run_id as _cri
                rid = _cri.get() or ""
                from app.memory.failure_memory import get_failure_memory
                store = get_failure_memory()
                err_text = result[:300] if isinstance(result, str) else str(result)[:300]
                store.record_failure(name, err_text, run_id=rid, session_id=self.session_id)
            except Exception:
                pass
        if _has_success_memory and not self.is_tool_error(result) and result.strip():
            try:
                from app.core.telemetry import _current_run_id as _cri
                rid = _cri.get() or ""
                from app.memory.success_memory import get_success_memory
                store = get_success_memory()
                try:
                    args_preview = json.loads(arguments)[:200] if arguments.strip().startswith("{") else arguments[:200]
                except Exception:
                    args_preview = arguments[:200]
                preview = result[:200] if isinstance(result, str) else str(result)[:200]
                store.record_success(name, args_preview, preview, run_id=rid, session_id=self.session_id)
            except Exception:
                pass

    async def _execute_tool_once(self, name: str, arguments: str) -> str:
        self._check_cancel()
        # 工具路由裁剪
        if self._active_tools is not None and name not in self._active_tools and name not in self._always_tools:
            return json.dumps({"error": f"工具 {name} 未在当前任务启用"}, ensure_ascii=False)
        # 人工确认流
        if settings.agent_tool_confirm and name in self._dangerous_tools and name not in self.approve_tools \
                and name not in self._runtime_approved:
            return json.dumps({
                "error": f"工具 {name} 需要用户确认后才能执行"
            }, ensure_ascii=False)
        t0 = time.time()
        tool = self.registry.get(name)
        if tool is None:
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)

        # 去重缓存
        if self._tool_cache is not None:
            key = (name, self._normalize_cache_key(arguments))
            cache_entry = self._tool_cache.get(key)
            if cache_entry is not None:
                cached_result = cache_entry["result"]
                cached_ok = cache_entry.get("ok", True)
                return cached_result

        # 安全门禁
        if settings.agent_confirm_enabled and name not in self.approve_tools \
                and name not in self._runtime_approved:
            from app.core.security import decide
            decision = decide(name, arguments)
            if decision.confirm:
                if settings.agent_confirm_interrupt:
                    return json.dumps({
                        "error": f"操作已被拦截: {decision.reason}(等待用户批准)",
                        "blocked": True,
                        "confirmation_required": True,
                    }, ensure_ascii=False)
                allowed, status = await self._await_confirmation(name, arguments, decision.reason)
                if not allowed:
                    return json.dumps({
                        "error": f"操作已被拦截: {decision.reason}(用户未批准)",
                        "blocked": True,
                        "confirmation_required": True,
                        "status": status,
                    }, ensure_ascii=False)

        try:
            tool_timeout = max(0, int(settings.agent_tool_timeout) or 0)
            if tool_timeout > 0:
                result = await asyncio.wait_for(tool.run(arguments), timeout=tool_timeout)
            else:
                result = await tool.run(arguments)
        except asyncio.TimeoutError:
            err_msg = f"工具 {name} 执行超时({tool_timeout}s)"
            return json.dumps({"error": err_msg}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        
        # 缓存成功结果
        if self._tool_cache is not None and not self.is_tool_error(result):
            key = (name, self._normalize_cache_key(arguments))
            if key not in self._tool_cache:
                max_size = int(settings.agent_tool_cache_size or 0)
                if max_size > 0:
                    if len(self._tool_cache) >= max_size:
                        self._tool_cache.pop(next(iter(self._tool_cache)))
                    self._tool_cache[key] = {"result": result, "ok": True}
        return result

    async def _await_confirmation(self, name: str, arguments: str, reason: str) -> tuple[bool, str]:
        """等待用户确认(阻塞直到确认或拒绝)。"""
        from app.tools.collaboration_tool import _get_store
        store = _get_store()
        rec = store.create(action=name, description=reason, params=json.loads(arguments) if arguments.strip().startswith("{") else {"raw": arguments})
        for _ in range(60):  # 最多等60秒
            await asyncio.sleep(0.5)
            self._check_cancel()
            updated = store.get(rec["id"])
            if updated and updated.get("status") in ("approved", "rejected"):
                return updated.get("status") == "approved", updated.get("status", "")
        return False, "timeout"

    async def execute_batch(
        self, tool_calls: list[ToolCall], steps: list[dict], step: int,
        stream: bool = False, state=None,
    ) -> list[tuple[ToolCall, str, bool]]:
        """并行执行一批工具调用,记录执行步骤。"""
        # 危险工具频率限制:超出速率直接拒绝,防止滥用
        if self._dangerous_tool_rate_limit > 0:
            now = time.time()
            for tc in tool_calls:
                if tc.name in self._dangerous_tools:
                    calls = self._dangerous_tool_calls.setdefault(tc.name, [])
                    # 清理超过 60s 的旧记录
                    self._dangerous_tool_calls[tc.name] = [t for t in calls if now - t < 60]
                    if len(self._dangerous_tool_calls[tc.name]) >= self._dangerous_tool_rate_limit:
                        msg = (
                            f"工具 {tc.name} 触发频率限制:"
                            f" 最近 60 秒内已调用 {self._dangerous_tool_rate_limit} 次,请稍后重试"
                        )
                        logger.warning(msg)
                        return [(tc, json.dumps({"error": msg, "blocked": True}, ensure_ascii=False), True)]
                    self._dangerous_tool_calls[tc.name].append(now)

        from app.tools.memory_tool import _current_session
        from app.tools.delegation_tool import set_delegate_context, _delegate_parent

        token = _current_session.set(self.session_id)
        d_token = set_delegate_context(None, self.session_id, self._active_tools, self.approve_tools)
        try:
            if stream and settings.agent_tool_streaming_enabled:
                results = []
                for tc in tool_calls:
                    chunks: list[str] = []
                    def _make_on_chunk(chunks_list=chunks):
                        def _on_chunk(chunk: str) -> None:
                            chunks_list.append(chunk)
                        return _on_chunk
                    # 推送工具开始事件
                    if state:
                        state.emit({"type": "tool_start", "tool": tc.name, "arguments": tc.arguments})
                    steps.append({
                        "step": step + 1,
                        "type": "tool_start",
                        "tool": tc.name,
                        "arguments": tc.arguments,
                    })
                    result = await self.execute_tool_streaming(tc.name, tc.arguments, on_chunk=_make_on_chunk())
                    results.append(result)
                    # 分块推送中间结果
                    for c in chunks:
                        if state:
                            state.emit({"type": "tool_chunk", "tool": tc.name, "chunk": c})
                        steps.append({
                            "step": step + 1,
                            "type": "tool_chunk",
                            "tool": tc.name,
                            "chunk": c,
                            "index": len(chunks),
                        })
                    # 推送工具完成事件
                    is_err = self.is_tool_error(result)
                    if state:
                        state.emit({"type": "tool_done", "tool": tc.name, "result": result, "result_len": len(result), "error": is_err})
                    steps.append({
                        "step": step + 1,
                        "type": "tool_done",
                        "tool": tc.name,
                        "result": result,
                        "result_len": len(result),
                        "error": is_err,
                    })
                batch = [
                    (tc, r, self.is_tool_error(r))
                    for tc, r in zip(tool_calls, results)
                ]
            else:
                timeout = int(getattr(settings, "agent_tool_timeout", 0) or 0)
                # 推送所有工具开始事件
                for tc in tool_calls:
                    if state:
                        state.emit({"type": "tool_start", "tool": tc.name, "arguments": tc.arguments})
                    steps.append({
                        "step": step + 1,
                        "type": "tool_start",
                        "tool": tc.name,
                        "arguments": tc.arguments,
                    })
                tasks = [self.execute_tool(tc.name, tc.arguments) for tc in tool_calls]
                if timeout > 0:
                    results = []
                    for tc, task in zip(tool_calls, tasks):
                        try:
                            result = await asyncio.wait_for(task, timeout=timeout)
                        except asyncio.TimeoutError:
                            result = json.dumps(
                                {"error": f"工具 {tc.name} 执行超时({timeout}s)"},
                                ensure_ascii=False,
                            )
                        results.append(result)
                        # 推送工具完成事件
                        is_err = self.is_tool_error(result)
                        if state:
                            state.emit({"type": "tool_done", "tool": tc.name, "result": result, "result_len": len(result), "error": is_err})
                        steps.append({
                            "step": step + 1,
                            "type": "tool_done",
                            "tool": tc.name,
                            "result": result,
                            "result_len": len(result),
                            "error": is_err,
                        })
                else:
                    results = await asyncio.gather(
                        *tasks, return_exceptions=True
                    )
                    results = [
                        r if isinstance(r, str) else json.dumps(
                            {"error": f"工具 {tc.name} 执行异常: {r}"},
                            ensure_ascii=False,
                        )
                        for tc, r in zip(tool_calls, results)
                    ]
                    # 推送所有工具完成事件
                    for tc, result_str in zip(tool_calls, results):
                        is_err = self.is_tool_error(result_str)
                        if state:
                            state.emit({"type": "tool_done", "tool": tc.name, "result": result_str, "result_len": len(result_str), "error": is_err})
                        steps.append({
                            "step": step + 1,
                            "type": "tool_done",
                            "tool": tc.name,
                            "result": result_str,
                            "result_len": len(result_str),
                            "error": is_err,
                        })
                batch = []
                for tc, result_str in zip(tool_calls, results):
                    is_err = self.is_tool_error(result_str)
                    steps.append({
                        "step": step + 1,
                        "type": "tool",
                        "tool": tc.name,
                        "arguments": tc.arguments,
                        "result": result_str,
                    })
                    batch.append((tc, result_str, is_err))
        finally:
            _current_session.reset(token)
            _delegate_parent.reset(d_token)
        return batch

    def scan_pending_confirmations(self, tool_calls: list[ToolCall]) -> list[tuple[ToolCall, object]]:
        """预扫描一批工具调用,返回需要人工确认且未批准的。"""
        from app.core.security import decide, Decision
        pending: list[tuple[ToolCall, object]] = []
        for tc in tool_calls:
            if tc.name in self.approve_tools or tc.name in self._runtime_approved:
                continue
            if settings.agent_tool_confirm and tc.name in self._dangerous_tools:
                pending.append((tc, Decision(
                    confirm=True, reason=f"工具 {tc.name} 属于危险操作,需要用户确认",
                )))
                continue
            if settings.agent_confirm_enabled:
                decision = decide(tc.name, tc.arguments)
                if decision.confirm:
                    pending.append((tc, decision))
        return pending

    def create_confirm_requests(self, pending: list[tuple[ToolCall, object]]) -> list[dict]:
        """为待确认的工具调用创建持久化确认请求。"""
        from app.core.security import describe
        from app.tools.collaboration_tool import _get_store
        store = _get_store()
        requests = []
        for tc, decision in pending:
            try:
                params = json.loads(tc.arguments) if isinstance(tc.arguments, str) and tc.arguments.strip() else {}
            except (json.JSONDecodeError, TypeError):
                params = {}
            if not isinstance(params, dict):
                params = {"raw": str(params)}
            rec = store.create(
                action=tc.name,
                description=f"{getattr(decision, 'reason', '') or ''}。\n详情: {describe(tc.name, tc.arguments)}",
                params=params,
            )
            requests.append({
                "confirmation_id": rec["id"],
                "tool": tc.name,
                "arguments": tc.arguments,
                "reason": getattr(decision, "reason", "") or "",
            })
        return requests

    def is_tool_approved_by_resume(self, tc: ToolCall, state) -> bool:
        """恢复运行时,判断该工具是否已被用户批准。"""
        resume = (state.interrupt or {}).get("resume")
        if isinstance(resume, list):
            return tc.name in resume
        resume = resume or {}
        requests = (state.interrupt or {}).get("value", {}).get("requests") or []
        for req in requests:
            if req.get("tool") != tc.name:
                continue
            return bool(req.get("approved", False))
        return False

    def build_reflection_hint(self, messages: list[dict], batch: list[tuple], failures: dict) -> Optional[str]:
        """根据失败模式构建反思提示,如果命中直接返回终止工具名。"""
        if not failures:
            return None
        from collections import Counter
        failing = [tc.name for tc, _, err in batch if err]
        counts = Counter(failing)
        for name, cnt in counts.items():
            if cnt >= 2 and name in ("run_code", "run_terminal"):
                return name
        return None
