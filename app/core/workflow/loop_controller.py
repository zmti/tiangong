"""增强型循环控制器。

支持三种循环模式:
- FOREACH: 遍历集合
- WHILE: 条件为真时继续
- DO_UNTIL: 至少执行一次,直到条件满足

每种模式都支持批量执行、进度 Checkpoint、取消检测。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from app.core.workflow.dynamic_router import DynamicEdgeEvaluator

logger = logging.getLogger(__name__)


class LoopControlType(str, Enum):
    """循环控制类型。"""
    FOREACH = "foreach"       # 遍历集合
    WHILE = "while"           # 条件为真时继续
    DO_UNTIL = "do_until"     # 至少执行一次,直到条件满足


@dataclass
class LoopContext:
    """循环执行上下文。"""
    loop_type: LoopControlType
    iterable: list = field(default_factory=list)
    max_iterations: int = 100
    batch_size: int = 1
    while_condition: str = ""
    until_condition: str = ""
    body_nodes: list[str] = field(default_factory=list)
    subgraph_id: Optional[str] = None

    # 运行时状态
    current_index: int = 0
    current_item: Any = None
    current_batch: list = field(default_factory=list)
    results: list = field(default_factory=list)
    cancelled: bool = False


class LoopController:
    """增强型循环控制器。"""

    def __init__(
        self,
        loop_config: dict,
        workflow_nodes: dict,
        variables: dict[str, Any],
        node_results: dict[str, Any],
        execute_node_func: Callable,
        save_checkpoint_func: Optional[Callable] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ):
        """
        Args:
            loop_config: 循环配置
            workflow_nodes: 工作流节点字典
            variables: 工作流变量
            node_results: 已执行节点结果
            execute_node_func: 执行单个节点的异步函数
            save_checkpoint_func: 保存检查点的异步函数
            cancel_check: 外部取消检查(如 run.cancelled)。循环每轮都会调用,
                置位即停止迭代,避免工作流级取消后循环空转整个 iterable。
        """
        self.config = loop_config
        self.workflow_nodes = workflow_nodes
        self.variables = variables
        self.node_results = node_results
        self.execute_node = execute_node_func
        self.save_checkpoint = save_checkpoint_func
        self.cancel_check = cancel_check

        # 解析循环控制类型
        loop_type_str = loop_config.get("type", "foreach")
        self.loop_type = LoopControlType(loop_type_str)

        # 解析可迭代对象
        self.iterable = self._resolve_iterable(loop_config.get("iterable", []))
        self.max_iterations = loop_config.get("max_iterations", 100)
        self.batch_size = loop_config.get("batch_size", 1)

        # 条件表达式
        self.while_condition = loop_config.get("while_condition", "")
        self.until_condition = loop_config.get("until_condition", "")

        # 子图节点 ID 列表
        self.subgraph_ids = loop_config.get("subgraph_ids", [])

        # 执行状态
        self._results = []
        self._cancelled = False
        self._iteration = 0

    @property
    def cancelled(self) -> bool:
        return self._is_cancelled()

    def _is_cancelled(self) -> bool:
        """综合判断取消:自身 cancel() 或外部 cancel_check(如 run.cancelled)。"""
        if self._cancelled:
            return True
        if self.cancel_check is not None:
            try:
                return bool(self.cancel_check())
            except Exception:
                return False
        return False

    async def execute(self, evaluator: DynamicEdgeEvaluator) -> dict:
        """执行循环。

        Args:
            evaluator: 动态边评估器(用于条件评估)

        Returns:
            循环执行结果
        """
        if self.loop_type == LoopControlType.FOREACH:
            return await self._execute_foreach()
        elif self.loop_type == LoopControlType.WHILE:
            return await self._execute_while(evaluator)
        elif self.loop_type == LoopControlType.DO_UNTIL:
            return await self._execute_do_until(evaluator)
        else:
            raise ValueError(f"不支持的循环类型: {self.loop_type}")

    async def _execute_foreach(self) -> dict:
        """Foreach 循环:遍历集合执行子图。"""
        total = len(self.iterable)
        batch_count = (total + self.batch_size - 1) // self.batch_size

        for i in range(0, total, self.batch_size):
            if self._is_cancelled():
                break

            batch = self.iterable[i:i + self.batch_size]
            self._iteration = i // self.batch_size

            # 设置循环上下文变量
            self._set_loop_variables(self._iteration, total, batch, batch[0] if len(batch) == 1 else batch)

            # 执行循环体
            try:
                body_result = await self._execute_body()
                self._results.append(body_result)
            except Exception as e:
                logger.warning("循环体执行失败 (iteration %d): %s", self._iteration, e)
                self._results.append({"error": str(e), "iteration": self._iteration})

                # 检查是否有错误恢复策略
                if not self._should_continue_on_error():
                    break

            # 定期保存 checkpoint
            if self.save_checkpoint and (i + len(batch)) % 10 == 0:
                await self._save_progress_checkpoint(i + len(batch), total)

        return {
            "loop_type": "foreach",
            "iterations": len(self._results),
            "total": total,
            "results": self._results,
            "success": not self._is_cancelled(),
        }

    async def _execute_while(self, evaluator: DynamicEdgeEvaluator) -> dict:
        """While 循环:条件为真时继续执行。"""
        max_iter = min(self.max_iterations, 1000)

        for i in range(max_iter):
            if self._is_cancelled():
                break

            # 先评估条件
            should_continue = self._evaluate_condition(self.while_condition, evaluator)
            if not should_continue:
                break

            # 设置循环变量
            self._iteration = i
            self._set_loop_variables(i, max_iter, [i], i)

            # 执行循环体
            try:
                body_result = await self._execute_body()
                self._results.append(body_result)
            except Exception as e:
                logger.warning("While 循环体执行失败 (iteration %d): %s", i, e)
                self._results.append({"error": str(e), "iteration": i})
                if not self._should_continue_on_error():
                    break

        return {
            "loop_type": "while",
            "iterations": len(self._results),
            "condition": self.while_condition,
            "results": self._results,
            "success": not self._is_cancelled(),
        }

    async def _execute_do_until(self, evaluator: DynamicEdgeEvaluator) -> dict:
        """Do-Until 循环:至少执行一次,直到条件满足。"""
        max_iter = min(self.max_iterations, 1000)

        for i in range(max_iter):
            if self._is_cancelled():
                break

            # 先执行循环体
            self._iteration = i
            self._set_loop_variables(i, max_iter, [i], i)

            try:
                body_result = await self._execute_body()
                self._results.append(body_result)
            except Exception as e:
                logger.warning("Do-Until 循环体执行失败 (iteration %d): %s", i, e)
                self._results.append({"error": str(e), "iteration": i})
                if not self._should_continue_on_error():
                    break

            # 再评估退出条件
            should_stop = self._evaluate_condition(self.until_condition, evaluator)
            if should_stop:
                break

        return {
            "loop_type": "do_until",
            "iterations": len(self._results),
            "condition": self.until_condition,
            "results": self._results,
            "success": not self._is_cancelled(),
        }

    async def _execute_body(self) -> dict:
        """执行循环体(子图)。

        每轮结果会回写到 node_results / variables,使 while/until 条件能够
        观测到循环体本轮产生的值(否则条件读到的是循环开始前的旧快照,
        循环永远无法自然终止,只能靠 max_iterations 兜底)。

        关键:variables 的回写必须按执行器约定归一化(取 output/answer/result
        字段)。旧实现直接 `self.variables.update(results)`,把
        {"status": "success", "output": ...} 原始 dict 塞进 run.variables,
        覆盖掉 _execute_node 已写好的归一化值,导致模板 {{node.output}} 与
        条件表达式读到整包结果而非 output 内容。
        """
        if not self.subgraph_ids:
            # 没有子图,返回当前迭代值
            return {"iteration": self._iteration, "item": self.current_item}

        # 执行子图节点
        results = {}
        for node_id in self.subgraph_ids:
            if node_id not in self.workflow_nodes:
                continue
            node = self.workflow_nodes[node_id]
            result = await self.execute_node(node)
            results[node_id] = result

        # 回写:让下一轮的条件评估能看到本轮产出。
        # node_results 保留原始结果;_execute_node 已把归一化输出写进
        # run.variables(与 controller 共享同一引用),这里不再覆盖 variables。
        self.node_results.update(results)

        return results

    def _evaluate_condition(self, condition: str, evaluator: DynamicEdgeEvaluator) -> bool:
        """评估循环条件。"""
        if not condition or not condition.strip():
            return True  # 无条件则一直执行
        return evaluator.evaluate(condition)

    def _set_loop_variables(
        self,
        index: int,
        total: int,
        batch: list,
        item: Any,
    ) -> None:
        """设置循环相关的变量。"""
        self.variables["loop_index"] = index
        self.variables["loop_total"] = total
        self.variables["loop_batch"] = batch
        self.variables["loop_item"] = item
        self.current_index = index
        self.current_batch = batch
        self.current_item = item

    def _resolve_iterable(self, raw: Any) -> list:
        """解析可迭代对象。"""
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
            return [raw]
        return [raw]

    def _should_continue_on_error(self) -> bool:
        """检查是否应在错误时继续。"""
        return self.config.get("continue_on_error", False)

    async def _save_progress_checkpoint(self, completed: int, total: int) -> None:
        """保存进度 checkpoint。"""
        if self.save_checkpoint:
            try:
                await self.save_checkpoint(
                    node_id=f"loop_progress",
                    status="running",
                    variables=dict(self.variables),
                    result={"completed": completed, "total": total},
                )
            except Exception as e:
                logger.debug("保存循环进度 checkpoint 失败: %s", e)

    def cancel(self) -> None:
        """取消循环。"""
        self._cancelled = True
