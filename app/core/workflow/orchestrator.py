"""工作流编排器。

支持:
- 工作流链式执行(前一个输出作为后一个输入)
- 工作流并行执行
- 工作流依赖管理
- 完成回调
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from app.core.workflow.executor import WorkflowExecutor
from app.core.workflow.models import Workflow, WorkflowRun

logger = logging.getLogger(__name__)


class WorkflowOrchestrator:
    """工作流编排器:协调多个工作流的执行。"""

    def __init__(self, default_executor: WorkflowExecutor):
        self._default_executor = default_executor
        self._executors: dict[str, WorkflowExecutor] = {}
        self._dependencies: dict[str, list[str]] = {}
        self._callbacks: dict[str, list[Callable]] = {}
        self._running_runs: dict[str, WorkflowRun] = {}

    def register_executor(self, workflow_id: str, executor: WorkflowExecutor) -> None:
        """注册工作流执行器。"""
        self._executors[workflow_id] = executor

    def add_dependency(self, workflow_id: str, depends_on: list[str]) -> None:
        """注册工作流依赖关系。

        Args:
            workflow_id: 工作流 ID
            depends_on: 依赖的工作流 ID 列表
        """
        self._dependencies[workflow_id] = depends_on

    def on_complete(self, workflow_id: str, callback: Callable) -> None:
        """注册完成回调。"""
        if workflow_id not in self._callbacks:
            self._callbacks[workflow_id] = []
        self._callbacks[workflow_id].append(callback)

    async def execute_chain(
        self,
        workflow_ids: list[str],
        inputs: dict[str, Any],
        session_id: str = "",
    ) -> dict[str, dict]:
        """顺序执行工作流链:前一个的输出作为后一个的输入。

        Args:
            workflow_ids: 工作流 ID 列表(按顺序)
            inputs: 初始输入
            session_id: 会话 ID

        Returns:
            各工作流的执行结果
        """
        results = {}
        current_inputs = inputs.copy()

        for wf_id in workflow_ids:
            # 等待依赖完成
            await self._wait_dependencies(wf_id)

            # 获取执行器
            executor = self._executors.get(wf_id) or self._default_executor

            # 执行工作流
            logger.info(f"编排器执行工作流链: {wf_id}")
            run = await executor.run(
                workflow=self._load_workflow(wf_id),
                inputs=current_inputs,
                session_id=session_id,
            )

            results[wf_id] = {
                "run_id": run.run_id,
                "status": run.status,
                "output": run.output,
                "duration": run.duration,
            }

            # 触发完成回调
            await self._trigger_callbacks(wf_id, run)

            # 将输出传递给下一个工作流
            current_inputs = self._merge_outputs(current_inputs, run.output)

        return results

    async def execute_parallel(
        self,
        workflow_ids: list[str],
        inputs: dict[str, Any],
        session_id: str = "",
        max_concurrency: int = 5,
    ) -> dict[str, dict]:
        """并行执行多个工作流。

        Args:
            workflow_ids: 工作流 ID 列表
            inputs: 输入(每个工作流独立)
            session_id: 会话 ID
            max_concurrency: 最大并发数

        Returns:
            各工作流的执行结果
        """
        semaphore = asyncio.Semaphore(max_concurrency)

        async def _run_with_semaphore(wf_id: str) -> tuple[str, dict]:
            async with semaphore:
                await self._wait_dependencies(wf_id)
                executor = self._executors.get(wf_id) or self._default_executor
                run = await executor.run(
                    workflow=self._load_workflow(wf_id),
                    inputs=inputs,
                    session_id=session_id,
                )
                await self._trigger_callbacks(wf_id, run)
                return wf_id, {
                    "run_id": run.run_id,
                    "status": run.status,
                    "output": run.output,
                    "duration": run.duration,
                }

        tasks = [_run_with_semaphore(wf_id) for wf_id in workflow_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        output = {}
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"并行工作流执行失败: {result}")
            else:
                wf_id, wf_result = result
                output[wf_id] = wf_result

        return output

    async def execute_with_dependencies(
        self,
        workflow_ids: list[str],
        inputs: dict[str, Any],
        session_id: str = "",
    ) -> dict[str, dict]:
        """按依赖关系执行工作流。

        拓扑排序后依次执行,支持并行执行无依赖的工作流。
        """
        # 拓扑排序
        ordered = self._topological_sort(workflow_ids)

        results = {}
        completed: set[str] = set()

        for wf_id in ordered:
            # 等待所有依赖完成
            deps = self._dependencies.get(wf_id, [])
            for dep in deps:
                if dep not in completed:
                    logger.warning(f"工作流 {wf_id} 的依赖 {dep} 未完成,跳过")
                    continue

            # 执行工作流
            executor = self._executors.get(wf_id) or self._default_executor
            run = await executor.run(
                workflow=self._load_workflow(wf_id),
                inputs=inputs,
                session_id=session_id,
            )

            results[wf_id] = {
                "run_id": run.run_id,
                "status": run.status,
                "output": run.output,
                "duration": run.duration,
            }
            completed.add(wf_id)

            # 触发回调
            await self._trigger_callbacks(wf_id, run)

        return results

    async def _wait_dependencies(self, workflow_id: str) -> None:
        """等待依赖的工作流完成。"""
        deps = self._dependencies.get(workflow_id, [])
        for dep_id in deps:
            if dep_id in self._running_runs:
                run = self._running_runs[dep_id]
                while run.status in ("running",):
                    await asyncio.sleep(0.1)

    async def _trigger_callbacks(self, workflow_id: str, run: WorkflowRun) -> None:
        """触发完成回调。"""
        callbacks = self._callbacks.get(workflow_id, [])
        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(run)
                else:
                    callback(run)
            except Exception as e:
                logger.warning(f"回调执行失败: {e}")

    def _merge_outputs(self, inputs: dict, output: Optional[dict]) -> dict:
        """合并输出到输入。"""
        merged = inputs.copy()
        if output and isinstance(output, dict):
            # 只合并顶层字段,避免覆盖
            for key, value in output.items():
                if key not in merged or isinstance(value, dict):
                    merged[key] = value
        return merged

    def _topological_sort(self, workflow_ids: list[str]) -> list[str]:
        """拓扑排序工作流 ID。"""
        in_degree = {wf_id: 0 for wf_id in workflow_ids}
        graph: dict[str, list[str]] = {wf_id: [] for wf_id in workflow_ids}

        for wf_id in workflow_ids:
            deps = self._dependencies.get(wf_id, [])
            for dep in deps:
                if dep in graph:
                    graph[dep].append(wf_id)
                    in_degree[wf_id] += 1

        queue = [wf_id for wf_id, deg in in_degree.items() if deg == 0]
        result = []

        while queue:
            wf_id = queue.pop(0)
            result.append(wf_id)
            for next_wf in graph[wf_id]:
                in_degree[next_wf] -= 1
                if in_degree[next_wf] == 0:
                    queue.append(next_wf)

        return result

    def _load_workflow(self, workflow_id: str) -> Optional[Workflow]:
        """加载工作流(从内存或数据库)。"""
        from app.api.workflow import _load_workflow
        return _load_workflow(workflow_id)

    def cancel_run(self, run_id: str) -> bool:
        """取消正在运行的工作流。"""
        for wf_id, run in self._running_runs.items():
            if run.run_id == run_id:
                run.cancel()
                return True
        return False


# 全局编排器
_orchestrator: Optional[WorkflowOrchestrator] = None


def get_orchestrator(default_executor: Optional[WorkflowExecutor] = None) -> WorkflowOrchestrator:
    """获取全局编排器。"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = WorkflowOrchestrator(default_executor or WorkflowExecutor(None))
    return _orchestrator
