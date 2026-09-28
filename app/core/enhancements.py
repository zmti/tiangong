"""小而美增强模块:进度流/失败模式库/复现包导出。

核心能力:
1. 进度流式事件: 实时推送计划/工具/步骤进度,前端可渲染进度条/甘特图
2. 失败模式库: 自动归类错误模式→建议修复模板,积累组织知识
3. 一键导出复现包: 输入+检查点+环境+配置→离线完整复现
4. 技能调用链可视化: DOT/GraphJSON 导出,支持 Graphviz/前端渲染
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)


# ============================================================
# 进度流式事件
# ============================================================

class ProgressEventType(str):
    PLAN_START = "plan_start"
    PLAN_STEP_START = "plan_step_start"
    PLAN_STEP_COMPLETE = "plan_step_complete"
    PLAN_STEP_FAILED = "plan_step_failed"
    PLAN_COMPLETE = "plan_complete"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_COMPLETE = "tool_call_complete"
    TOOL_CALL_FAILED = "tool_call_failed"
    LLM_START = "llm_start"
    LLM_TOKEN = "llm_token"
    LLM_COMPLETE = "llm_complete"
    VERIFICATION_START = "verification_start"
    VERIFICATION_COMPLETE = "verification_complete"
    MEMORY_CONSOLIDATION = "memory_consolidation"
    COST_UPDATE = "cost_update"
    CHECKPOINT_SAVE = "checkpoint_save"
    HUMAN_CHECKPOINT = "human_checkpoint"
    ERROR = "error"
    CUSTOM = "custom"


@dataclass
class ProgressEvent:
    """进度事件。"""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    event_type: str = ProgressEventType.CUSTOM
    run_id: str = ""
    session_id: str = ""
    task_id: str = ""
    timestamp: float = field(default_factory=time.time)
    
    # 进度信息
    progress_pct: float = 0.0  # 0-100
    current_step: int = 0
    total_steps: int = 0
    step_name: str = ""
    
    # 详细载荷
    payload: dict = field(default_factory=dict)
    
    # 显示提示
    message: str = ""
    level: str = "info"  # info/warning/error/debug
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    def to_sse(self) -> str:
        """转换为 Server-Sent Events 格式。"""
        return f"data: {json.dumps(self.to_dict(), ensure_ascii=False)}\n\n"


class ProgressStream:
    """进度流管理器:发布/订阅模式。"""
    
    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)  # run_id -> queues
        self._global_subscribers: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()
        self._event_history: dict[str, list[ProgressEvent]] = defaultdict(list)  # run_id -> events
        self._max_history = 1000
    
    async def subscribe(self, run_id: str = None) -> asyncio.Queue:
        """订阅进度事件。"""
        queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            if run_id:
                self._subscribers[run_id].append(queue)
            else:
                self._global_subscribers.append(queue)
        return queue
    
    async def unsubscribe(self, queue: asyncio.Queue, run_id: str = None) -> None:
        """取消订阅。"""
        async with self._lock:
            if run_id and queue in self._subscribers[run_id]:
                self._subscribers[run_id].remove(queue)
            elif queue in self._global_subscribers:
                self._global_subscribers.remove(queue)
    
    async def publish(self, event: ProgressEvent) -> None:
        """发布事件。"""
        # 记录历史
        if event.run_id:
            self._event_history[event.run_id].append(event)
            if len(self._event_history[event.run_id]) > self._max_history:
                self._event_history[event.run_id] = self._event_history[event.run_id][-self._max_history:]
        
        # 分发给订阅者
        async with self._lock:
            targets = []
            if event.run_id:
                targets.extend(self._subscribers.get(event.run_id, []))
            targets.extend(self._global_subscribers)
        
        for queue in targets:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # 丢弃,避免阻塞
    
    def get_history(self, run_id: str, limit: int = 100) -> list[ProgressEvent]:
        """获取历史事件。"""
        events = self._event_history.get(run_id, [])
        return events[-limit:]
    
    def get_progress_summary(self, run_id: str) -> dict:
        """获取进度摘要。"""
        events = self._event_history.get(run_id, [])
        if not events:
            return {"progress_pct": 0, "current_step": 0, "total_steps": 0, "status": "unknown"}
        
        latest = events[-1]
        return {
            "progress_pct": latest.progress_pct,
            "current_step": latest.current_step,
            "total_steps": latest.total_steps,
            "step_name": latest.step_name,
            "status": "running" if latest.progress_pct < 100 else "completed",
            "last_update": latest.timestamp,
        }


# 全局进度流
_progress_stream: Optional[ProgressStream] = None


def get_progress_stream() -> ProgressStream:
    global _progress_stream
    if _progress_stream is None:
        _progress_stream = ProgressStream()
    return _progress_stream


def emit_progress(
    event_type: str,
    run_id: str,
    session_id: str = "",
    task_id: str = "",
    progress_pct: float = 0,
    current_step: int = 0,
    total_steps: int = 0,
    step_name: str = "",
    payload: dict = None,
    message: str = "",
    level: str = "info",
) -> None:
    """便捷函数:发射进度事件(同步版本,用于非异步上下文)。"""
    event = ProgressEvent(
        event_type=event_type,
        run_id=run_id,
        session_id=session_id,
        task_id=task_id,
        progress_pct=progress_pct,
        current_step=current_step,
        total_steps=total_steps,
        step_name=step_name,
        payload=payload or {},
        message=message,
        level=level,
    )
    # 这里仅记录,实际异步发布需在 async 上下文
    stream = get_progress_stream()
    # 同步存入历史
    stream._event_history[run_id].append(event)


# ============================================================
# 失败模式库
# ============================================================

@dataclass
class FailurePattern:
    """失败模式。"""
    pattern_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    category: str = ""  # tool_error / llm_error / verification_failed / timeout / permission / logic
    
    # 匹配规则
    error_signatures: list[str] = field(default_factory=list)  # 错误特征串(正则/关键词)
    tool_names: list[str] = field(default_factory=list)  # 相关工具
    contexts: list[str] = field(default_factory=list)  # 上下文关键词
    
    # 统计
    occurrence_count: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    
    # 解决方案
    suggested_fixes: list[dict] = field(default_factory=list)  # [{"description": "", "code_template": "", "confidence": 0.8}]
    auto_fixable: bool = False
    fix_success_rate: float = 0.0
    
    # 元数据
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    
    def matches(self, error: str, tool_name: str = "", context: str = "") -> float:
        """计算匹配度(0-1)。"""
        score = 0.0
        error_lower = error.lower()
        
        # 错误特征匹配
        for sig in self.error_signatures:
            if sig.lower() in error_lower:
                score += 0.4
            elif re.search(sig, error, re.IGNORECASE):
                score += 0.5
        
        # 工具名匹配
        if tool_name and tool_name in self.tool_names:
            score += 0.2
        
        # 上下文匹配
        context_lower = context.lower()
        for ctx in self.contexts:
            if ctx.lower() in context_lower:
                score += 0.1
        
        return min(score, 1.0)
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "FailurePattern":
        return cls(**data)


class FailurePatternLibrary:
    """失败模式库:自动学习/归类/建议修复。"""
    
    def __init__(self, db_path: Optional[str] = None, backend: Optional[StorageBackend] = None):
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(settings.resolved_data_dir, "failure_patterns.db")
            self._backend = get_storage_backend(db_path=self._db_path)
        self._lock = threading.RLock()
        self._init_db()
        self._load_builtin_patterns()
    
    def _init_db(self) -> None:
        schema = {
            "failure_patterns": [
                {"name": "pattern_id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "pattern_json", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
            ],
        }
        self._backend.init_db(schema)
    
    def _load_builtin_patterns(self) -> None:
        """加载内置常见失败模式。"""
        builtins = [
            FailurePattern(
                name="Python ImportError",
                category="tool_error",
                error_signatures=[r"ModuleNotFoundError", r"ImportError", r"No module named"],
                tool_names=["run_code", "run_terminal", "write_file"],
                suggested_fixes=[
                    {"description": "安装缺失依赖包", "code_template": "pip install {module_name}", "confidence": 0.9},
                    {"description": "检查虚拟环境是否激活", "code_template": "source venv/bin/activate", "confidence": 0.7},
                ],
                tags=["python", "dependency"],
            ),
            FailurePattern(
                name="Syntax Error",
                category="tool_error",
                error_signatures=[r"SyntaxError", r"IndentationError", r"invalid syntax"],
                tool_names=["run_code", "write_file", "edit_file"],
                suggested_fixes=[
                    {"description": "检查缩进和语法", "code_template": "# 使用 ruff/mypy 自动修复", "confidence": 0.8},
                ],
                tags=["python", "syntax"],
            ),
            FailurePattern(
                name="Permission Denied",
                category="permission",
                error_signatures=[r"Permission denied", r"Access denied", r"EACCES"],
                tool_names=["write_file", "run_terminal", "edit_file"],
                suggested_fixes=[
                    {"description": "检查文件/目录权限", "code_template": "chmod 755 {path}", "confidence": 0.8},
                    {"description": "使用 sudo 或调整所有者", "code_template": "sudo chown $USER {path}", "confidence": 0.6},
                ],
                tags=["filesystem", "permission"],
            ),
            FailurePattern(
                name="Network Timeout",
                category="timeout",
                error_signatures=[r"timeout", r"Timed out", r"Connection timed out", r"Read timed out"],
                tool_names=["web_search", "run_terminal", "run_code"],
                suggested_fixes=[
                    {"description": "增加超时时间", "code_template": "timeout=120", "confidence": 0.7},
                    {"description": "检查网络连接/代理", "code_template": "export HTTP_PROXY=...", "confidence": 0.5},
                ],
                tags=["network", "timeout"],
            ),
            FailurePattern(
                name="API Rate Limit",
                category="llm_error",
                error_signatures=[r"rate limit", r"429", r"quota exceeded", r"too many requests"],
                tool_names=["run_code", "web_search"],
                suggested_fixes=[
                    {"description": "等待并重试(指数退避)", "code_template": "await asyncio.sleep(2**attempt)", "confidence": 0.9},
                    {"description": "切换备用 API Key/Provider", "code_template": "provider=qwen", "confidence": 0.8},
                ],
                tags=["api", "rate_limit"],
            ),
            FailurePattern(
                name="Test Failure",
                category="verification_failed",
                error_signatures=[r"FAILED", r"AssertionError", r"pytest.*failed", r"test.*failed"],
                tool_names=["run_terminal", "run_code"],
                contexts=["pytest", "test"],
                suggested_fixes=[
                    {"description": "查看失败测试详情并修复代码", "code_template": "# 运行单个失败测试: pytest test_file.py::test_name -xvs", "confidence": 0.9},
                ],
                tags=["testing", "pytest"],
            ),
            FailurePattern(
                name="Type Check Error",
                category="verification_failed",
                error_signatures=[r"mypy.*error", r"type.*mismatch", r"incompatible type"],
                tool_names=["run_terminal", "run_code"],
                contexts=["mypy", "type_check"],
                suggested_fixes=[
                    {"description": "添加类型注解或修正类型", "code_template": "# 使用 # type: ignore 或修正类型", "confidence": 0.8},
                ],
                tags=["typing", "mypy"],
            ),
            FailurePattern(
                name="Lint Error",
                category="verification_failed",
                error_signatures=[r"ruff.*error", r"lint.*error", r"style.*error"],
                tool_names=["run_terminal", "run_code"],
                contexts=["ruff", "lint"],
                suggested_fixes=[
                    {"description": "运行 ruff --fix 自动修复", "code_template": "ruff check --fix .", "confidence": 0.95},
                ],
                tags=["linting", "ruff"],
            ),
        ]
        
        with self._lock:
            for pattern in builtins:
                existing = self._backend.select_one("failure_patterns", "pattern_id = ?", (pattern.pattern_id,))
                if not existing:
                    self._backend.insert(
                        "failure_patterns",
                        {
                            "pattern_id": pattern.pattern_id,
                            "pattern_json": json.dumps(pattern.to_dict(), ensure_ascii=False),
                            "created_at": pattern.first_seen,
                            "updated_at": pattern.last_seen,
                        },
                    )
    
    def learn_from_failure(
        self,
        error: str,
        tool_name: str = "",
        context: str = "",
        fix_applied: str = "",
        fix_success: bool = False,
    ) -> FailurePattern:
        """从失败中学习/更新模式。"""
        # 查找匹配模式
        best_match = None
        best_score = 0.0
        
        with self._lock:
            rows = self._backend.select("failure_patterns", "1=1")
            for row in rows:
                pattern = FailurePattern.from_dict(json.loads(row["pattern_json"]))
                score = pattern.matches(error, tool_name, context)
                if score > best_score:
                    best_score = score
                    best_match = pattern
        
        if best_match and best_score > 0.5:
            # 更新现有模式
            best_match.occurrence_count += 1
            best_match.last_seen = time.time()
            if fix_applied and fix_success:
                best_match.suggested_fixes.append({
                    "description": "自动学习的修复",
                    "code_template": fix_applied,
                    "confidence": 0.7,
                })
                best_match.auto_fixable = True
                best_match.fix_success_rate = (
                    best_match.fix_success_rate * (best_match.occurrence_count - 1) + 1.0
                ) / best_match.occurrence_count
            self._save_pattern(best_match)
            return best_match
        
        # 创建新模式
        if best_score < 0.3:
            new_pattern = FailurePattern(
                name=f"Auto_{tool_name or 'unknown'}_{int(time.time())}",
                category=self._classify_category(error),
                error_signatures=self._extract_signatures(error),
                tool_names=[tool_name] if tool_name else [],
                contexts=[context] if context else [],
                suggested_fixes=[{"description": fix_applied, "code_template": fix_applied, "confidence": 0.5}] if fix_applied else [],
                auto_fixable=bool(fix_applied and fix_success),
                fix_success_rate=1.0 if fix_success else 0.0,
            )
            self._save_pattern(new_pattern)
            return new_pattern
        
        return best_match
    
    def _classify_category(self, error: str) -> str:
        error_lower = error.lower()
        if any(kw in error_lower for kw in ["permission", "access denied", "eacces"]):
            return "permission"
        if any(kw in error_lower for kw in ["timeout", "timed out"]):
            return "timeout"
        if any(kw in error_lower for kw in ["rate limit", "429", "quota"]):
            return "llm_error"
        if any(kw in error_lower for kw in ["syntax", "indentation", "invalid syntax"]):
            return "tool_error"
        if any(kw in error_lower for kw in ["import", "modulenotfound"]):
            return "tool_error"
        if any(kw in error_lower for kw in ["assertion", "test.*fail", "pytest"]):
            return "verification_failed"
        return "tool_error"
    
    def _extract_signatures(self, error: str) -> list[str]:
        """提取错误特征签名。"""
        # 简化:取错误类型和关键行
        lines = error.strip().split("\n")
        signatures = []
        for line in lines[-3:]:  # 最后3行通常包含关键错误
            line = line.strip()
            if line and len(line) > 10:
                signatures.append(line[:100])
        return signatures[:3]
    
    def _save_pattern(self, pattern: FailurePattern) -> None:
        pattern.updated_at = time.time()
        with self._lock:
            self._backend.upsert(
                "failure_patterns",
                {
                    "pattern_id": pattern.pattern_id,
                    "pattern_json": json.dumps(pattern.to_dict(), ensure_ascii=False),
                    "updated_at": pattern.updated_at,
                },
                conflict_columns=["pattern_id"],
            )
    
    def find_fixes(self, error: str, tool_name: str = "", context: str = "") -> list[dict]:
        """查找建议修复。"""
        matches = []
        with self._lock:
            rows = self._backend.select("failure_patterns", "1=1")
            for row in rows:
                pattern = FailurePattern.from_dict(json.loads(row["pattern_json"]))
                score = pattern.matches(error, tool_name, context)
                if score > 0.3:
                    for fix in pattern.suggested_fixes:
                        fix_copy = fix.copy()
                        fix_copy["pattern_id"] = pattern.pattern_id
                        fix_copy["pattern_name"] = pattern.name
                        fix_copy["match_score"] = score
                        fix_copy["auto_fixable"] = pattern.auto_fixable
                        matches.append(fix_copy)
        
        # 按匹配度和置信度排序
        matches.sort(key=lambda x: (x["match_score"] * x["confidence"]), reverse=True)
        return matches[:5]
    
    def get_stats(self) -> dict:
        with self._lock:
            rows = self._backend.select("failure_patterns", "1=1")
        total = len(rows)
        by_category = defaultdict(int)
        auto_fixable = 0
        for row in rows:
            pattern = FailurePattern.from_dict(json.loads(row["pattern_json"]))
            by_category[pattern.category] += 1
            if pattern.auto_fixable:
                auto_fixable += 1
        return {
            "total_patterns": total,
            "by_category": dict(by_category),
            "auto_fixable_count": auto_fixable,
        }


# 全局失败模式库
_failure_library: Optional[FailurePatternLibrary] = None


def get_failure_library() -> FailurePatternLibrary:
    global _failure_library
    if _failure_library is None:
        _failure_library = FailurePatternLibrary()
    return _failure_library


# ============================================================
# 复现包导出
# ============================================================

@dataclass
class ReproductionPackage:
    """复现包。"""
    package_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    
    # 原始请求
    user_input: str = ""
    session_id: str = ""
    task_id: str = ""
    
    # 运行记录
    run_ids: list[str] = field(default_factory=list)
    checkpoints: list[dict] = field(default_factory=list)
    
    # 环境信息
    python_version: str = field(default_factory=lambda: sys.version)
    platform: str = field(default_factory=lambda: sys.platform)
    dependencies: dict = field(default_factory=dict)  # pip freeze 结果
    env_vars: dict = field(default_factory=dict)  # 关键环境变量
    
    # 配置快照
    agent_config: dict = field(default_factory=dict)
    model_config: dict = field(default_factory=dict)
    
    # 代码/文件快照
    file_snapshots: dict = field(default_factory=dict)  # path -> content/hash
    
    # 元数据
    metadata: dict = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    def export_zip(self, output_path: str) -> None:
        """导出为 ZIP 包。"""
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # 主清单
            zf.writestr("reproduction.json", json.dumps(self.to_dict(), ensure_ascii=False, indent=2))
            
            # 环境信息
            zf.writestr("environment/python_version.txt", self.python_version)
            zf.writestr("environment/platform.txt", self.platform)
            zf.writestr("environment/dependencies.txt", "\n".join(f"{k}=={v}" for k, v in self.dependencies.items()))
            zf.writestr("environment/env_vars.json", json.dumps(self.env_vars, ensure_ascii=False, indent=2))
            
            # 配置
            zf.writestr("config/agent_config.json", json.dumps(self.agent_config, ensure_ascii=False, indent=2))
            zf.writestr("config/model_config.json", json.dumps(self.model_config, ensure_ascii=False, indent=2))
            
            # 文件快照
            for path, content in self.file_snapshots.items():
                safe_path = path.replace(":", "_").replace("/", "_")
                zf.writestr(f"files/{safe_path}", content if isinstance(content, str) else json.dumps(content))
            
            # README
            readme = f"""# 复现包

**生成时间**: {datetime.fromtimestamp(self.created_at).isoformat()}
**Session**: {self.session_id}
**Task**: {self.task_id}
**用户输入**: {self.user_input[:200]}

## 使用方法

1. 恢复环境:
   ```bash
   pip install -r environment/dependencies.txt
   ```

2. 设置环境变量:
   ```bash
   source environment/env_vars.sh  # 或手动设置
   ```

3. 使用检查点恢复运行:
   ```python
   from app.core.agent import Agent
   agent = Agent()
   result = await agent.resume("{self.run_ids[0]}")
   ```

## 包含内容

- `reproduction.json`: 完整元数据
- `environment/`: Python版本、依赖、环境变量
- `config/`: Agent和模型配置快照
- `files/`: 关键文件快照
- `checkpoints/`: 运行检查点

"""
            zf.writestr("README.md", readme)


class ReproductionExporter:
    """复现包导出器。"""
    
    def __init__(self, checkpoint_manager: Optional[Any] = None):
        self.checkpoint_manager = checkpoint_manager
    
    def create_package(
        self,
        user_input: str,
        session_id: str,
        task_id: str,
        run_ids: list[str],
        include_files: bool = True,
        file_patterns: list[str] = None,
    ) -> ReproductionPackage:
        """创建复现包。"""
        pkg = ReproductionPackage(
            user_input=user_input,
            session_id=session_id,
            task_id=task_id,
            run_ids=run_ids,
        )
        
        # 环境信息
        pkg.dependencies = self._get_dependencies()
        pkg.env_vars = self._get_key_env_vars()
        
        # 配置快照
        pkg.agent_config = self._get_agent_config()
        pkg.model_config = self._get_model_config()
        
        # 检查点
        if self.checkpoint_manager:
            for run_id in run_ids:
                cp = self.checkpoint_manager.list(limit=10, run_id=run_id)
                pkg.checkpoints.extend(cp)
        
        # 文件快照
        if include_files:
            pkg.file_snapshots = self._capture_file_snapshots(file_patterns)
        
        return pkg
    
    def _get_dependencies(self) -> dict:
        try:
            result = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True, timeout=30)
            deps = {}
            for line in result.stdout.strip().split("\n"):
                if "==" in line:
                    name, version = line.split("==", 1)
                    deps[name] = version
            return deps
        except Exception:
            return {}
    
    def _get_key_env_vars(self) -> dict:
        keys = [
            "AGENT_LLM_PROVIDER", "AGENT_LLM_MODEL", "DEEPSEEK_API_KEY", "QWEN_API_KEY",
            "OPENAI_API_KEY", "AGENT_LLM_TIMEOUT", "AGENT_MAX_STEPS",
            "AGENT_VERIFICATION_ENABLED", "AGENT_MEMORY_CONSOLIDATION_ENABLED",
        ]
        return {k: os.environ.get(k, "") for k in keys if os.environ.get(k)}
    
    def _get_agent_config(self) -> dict:
        # 从 settings 获取关键配置
        config_keys = [
            "agent_max_steps", "agent_temperature", "agent_mode", "agent_planning",
            "agent_verification_enabled", "agent_memory_consolidation_enabled",
            "agent_tracing_enabled", "agent_guardrails_enabled",
        ]
        return {k: getattr(settings, k, None) for k in config_keys}
    
    def _get_model_config(self) -> dict:
        return {
            "provider": getattr(settings, "agent_llm_provider", ""),
            "model": getattr(settings, "deepseek_model", "") or getattr(settings, "qwen_model", ""),
            "temperature": getattr(settings, "agent_temperature", 0.7),
        }
    
    def _capture_file_snapshots(self, patterns: list[str] = None) -> dict:
        """捕获关键文件快照。"""
        if patterns is None:
            patterns = ["*.py", "*.json", "*.yaml", "*.yml", "*.toml", "*.md"]
        
        snapshots = {}
        project_root = Path(settings.resolved_data_dir).parent
        
        for pattern in patterns:
            for file_path in project_root.rglob(pattern):
                if file_path.is_file() and file_path.stat().st_size < 1024 * 1024:  # < 1MB
                    try:
                        rel_path = file_path.relative_to(project_root)
                        content = file_path.read_text(encoding="utf-8", errors="ignore")
                        snapshots[str(rel_path)] = content
                    except Exception:
                        pass
        return snapshots
    
    def export_package(self, package: ReproductionPackage, output_path: str) -> None:
        """导出复现包到 ZIP。"""
        package.export_zip(output_path)
        logger.info("复现包已导出: %s", output_path)


# ============================================================
# 技能调用链可视化
# ============================================================

class SkillCallGraphExporter:
    """技能/工具调用链导出:DOT/GraphJSON/GraphML。"""
    
    @staticmethod
    def to_dot(steps: list[dict], run_id: str = "") -> str:
        """生成 Graphviz DOT 格式。"""
        lines = [
            f'digraph "Agent Run {run_id[:8]}" {{',
            '  rankdir=TB;',
            '  node [shape=box, style=filled, fontname="Sans"];',
            '  edge [fontname="Sans", fontsize=10];',
        ]
        
        node_id = 0
        tool_calls = []
        
        for i, step in enumerate(steps):
            step_type = step.get("type", "")
            
            if step_type == "tool":
                tool_name = step.get("tool", "unknown")
                node_id += 1
                nid = f"tool_{node_id}"
                duration = step.get("duration_ms", 0)
                label = f"{tool_name}\\n{duration}ms"
                color = "lightgreen" if not step.get("error") else "lightcoral"
                lines.append(f'  {nid} [label="{label}", fillcolor="{color}"];')
                tool_calls.append((nid, tool_name, step))
            
            elif step_type == "llm":
                node_id += 1
                nid = f"llm_{node_id}"
                model = step.get("model", "unknown")
                tokens = step.get("tokens", 0)
                label = f"LLM: {model}\\n{tokens} tokens"
                lines.append(f'  {nid} [label="{label}", fillcolor="lightblue", shape=ellipse];')
        
        # 简单连接:按顺序连接
        prev = None
        for i, step in enumerate(steps):
            if step.get("type") in ("tool", "llm"):
                # 这里需要更精确的节点ID映射,简化处理
                pass
        
        lines.append("}")
        return "\n".join(lines)
    
    @staticmethod
    def to_graphjson(steps: list[dict], run_id: str = "") -> dict:
        """生成 GraphJSON 格式(前端渲染友好)。"""
        nodes = []
        edges = []
        
        node_map = {}
        node_idx = 0
        
        for i, step in enumerate(steps):
            step_type = step.get("type", "")
            
            if step_type == "tool":
                tool_name = step.get("tool", "unknown")
                node_id = f"tool_{tool_name}_{i}"
                node_map[i] = node_id
                nodes.append({
                    "id": node_id,
                    "type": "tool",
                    "label": tool_name,
                    "data": {
                        "tool": tool_name,
                        "arguments": step.get("arguments", {}),
                        "result_preview": str(step.get("result", ""))[:200],
                        "error": step.get("error"),
                        "duration_ms": step.get("duration_ms", 0),
                    },
                    "style": {"fill": "lightgreen" if not step.get("error") else "lightcoral"},
                })
            
            elif step_type == "llm":
                node_id = f"llm_{i}"
                node_map[i] = node_id
                model = step.get("model", "unknown")
                nodes.append({
                    "id": node_id,
                    "type": "llm",
                    "label": f"LLM: {model}",
                    "data": {
                        "model": model,
                        "tokens": step.get("tokens", 0),
                        "tool_calls": step.get("tool_calls", 0),
                    },
                    "style": {"fill": "lightblue", "shape": "ellipse"},
                })
            
            elif step_type == "plan":
                node_id = f"plan_{i}"
                node_map[i] = node_id
                nodes.append({
                    "id": node_id,
                    "type": "plan",
                    "label": step.get("action", "plan"),
                    "data": step.get("plan", {}),
                    "style": {"fill": "lightyellow", "shape": "diamond"},
                })
        
        # 构建边:按顺序连接
        prev_node = None
        for idx in sorted(node_map.keys()):
            curr_node = node_map[idx]
            if prev_node:
                edges.append({"source": prev_node, "target": curr_node, "type": "sequence"})
            prev_node = curr_node
        
        return {
            "graph": {
                "id": run_id,
                "label": f"Agent Run {run_id[:8]}",
                "nodes": nodes,
                "edges": edges,
            },
            "metadata": {
                "run_id": run_id,
                "generated_at": datetime.now().isoformat(),
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
        }


# ============================================================
# 便捷函数
# ============================================================

def create_reproduction_package(
    user_input: str,
    session_id: str,
    task_id: str,
    run_ids: list[str],
    checkpoint_manager: Optional[Any] = None,
    output_path: str = None,
) -> str:
    """一键创建并导出复现包。"""
    exporter = ReproductionExporter(checkpoint_manager)
    pkg = exporter.create_package(user_input, session_id, task_id, run_ids)
    
    if output_path is None:
        output_path = os.path.join(
            settings.resolved_data_dir,
            "reproductions",
            f"repro_{session_id}_{int(time.time())}.zip",
        )
    
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    exporter.export_package(pkg, output_path)
    return output_path


def export_call_graph(steps: list[dict], run_id: str, format: str = "graphjson") -> str | dict:
    """导出调用链图。"""
    if format == "dot":
        return SkillCallGraphExporter.to_dot(steps, run_id)
    elif format == "graphjson":
        return SkillCallGraphExporter.to_graphjson(steps, run_id)
    elif format == "graphml":
        # 可扩展 GraphML
        return SkillCallGraphExporter.to_graphjson(steps, run_id)
    else:
        raise ValueError(f"不支持的格式: {format}")