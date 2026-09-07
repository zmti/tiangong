"""SAGA 补偿事务编排器。

为工作流节点提供补偿能力,支持:
- 正向操作和补偿操作的注册
- 失败时自动逆序补偿已执行的节点
- 部分补偿(从失败点向前回滚)
- 补偿失败的处理(记录日志,不中断)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class SagaAction:
    """SAGA 事务动作。"""
    node_id: str
    action: Callable  # 正向操作: async def execute(**kwargs) -> dict
    compensation: Optional[Callable] = None  # 补偿操作: async def compensate(**kwargs) -> None
    timeout: float = 60.0
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "description": self.description,
            "has_compensation": self.compensation is not None,
            "timeout": self.timeout,
        }


class SagaOrchestrator:
    """SAGA 补偿事务编排器。

    使用方式:
        saga = SagaOrchestrator()
        saga.register("node_1", execute_fn, compensate_fn)
        result = await saga.execute("node_1", **kwargs)
        # 如果后续节点失败,调用 compensate_up_to("node_1")
    """

    def __init__(self):
        self._actions: dict[str, SagaAction] = {}
        self._executed: list[str] = []  # 已执行节点 ID 列表
        self._results: dict[str, dict] = {}  # 执行结果缓存
        self._compensating: bool = False

    def register(
        self,
        node_id: str,
        action: Callable,
        compensation: Optional[Callable] = None,
        timeout: float = 60.0,
        description: str = "",
    ) -> None:
        """注册节点的正向操作和补偿操作。

        Args:
            node_id: 节点 ID
            action: 正向操作函数 async def execute(**kwargs) -> dict
            compensation: 补偿操作函数 async def compensate(**kwargs) -> None
            timeout: 操作超时时间(秒)
            description: 操作描述
        """
        self._actions[node_id] = SagaAction(
            node_id=node_id,
            action=action,
            compensation=compensation,
            timeout=timeout,
            description=description,
        )

    def unregister(self, node_id: str) -> None:
        """注销节点。"""
        self._actions.pop(node_id, None)

    async def execute(self, node_id: str, **kwargs) -> dict:
        """执行节点的正向操作。

        Args:
            node_id: 节点 ID
            **kwargs: 传递给操作函数的参数

        Returns:
            操作结果

        Raises:
            RuntimeError: 操作失败或未注册的节点
        """
        action = self._actions.get(node_id)
        if not action:
            raise RuntimeError(f"未注册节点动作: {node_id}")

        try:
            result = await asyncio.wait_for(
                action.action(**kwargs),
                timeout=action.timeout,
            )
            self._executed.append(node_id)
            self._results[node_id] = result
            logger.info(f"SAGA 执行成功: {node_id}")
            return result
        except Exception as e:
            logger.error(f"SAGA 执行失败: {node_id}, error={e}")
            raise

    async def compensate_up_to(self, failed_node_id: str) -> list[str]:
        """从失败节点向前补偿所有已执行节点。

        Args:
            failed_node_id: 失败的节点 ID(不包含在补偿中)

        Returns:
            成功补偿的节点 ID 列表
        """
        if self._compensating:
            logger.warning("已有补偿操作在进行中,跳过")
            return []

        self._compensating = True
        compensated = []
        failed = []

        try:
            # 找到失败节点的索引
            if failed_node_id in self._executed:
                idx = self._executed.index(failed_node_id)
                # 补偿该节点之前的所有节点(逆序)
                nodes_to_compensate = list(reversed(self._executed[:idx]))
            else:
                nodes_to_compensate = list(reversed(self._executed))

            for node_id in nodes_to_compensate:
                action = self._actions.get(node_id)
                if not action or not action.compensation:
                    logger.warning(f"节点 {node_id} 没有补偿操作,跳过")
                    continue

                try:
                    await asyncio.wait_for(
                        action.compensation(**self._results.get(node_id, {})),
                        timeout=action.timeout,
                    )
                    compensated.append(node_id)
                    logger.info(f"SAGA 补偿成功: {node_id}")
                except Exception as e:
                    failed.append(node_id)
                    logger.error(f"SAGA 补偿失败: {node_id}, error={e}")

        finally:
            self._compensating = False

        if failed:
            logger.warning(f"部分补偿失败: {failed}, 已补偿: {compensated}")
        else:
            logger.info(f"SAGA 补偿完成: {compensated}")

        return compensated

    def get_executed_nodes(self) -> list[str]:
        """获取已执行的节点 ID 列表。"""
        return list(self._executed)

    def get_results(self, node_id: str) -> Optional[dict]:
        """获取节点执行结果。"""
        return self._results.get(node_id)

    def reset(self) -> None:
        """重置 SAGA 状态(新运行开始时调用)。"""
        self._executed.clear()
        self._results.clear()
        self._compensating = False

    def to_dict(self) -> dict:
        """序列化为字典。"""
        return {
            "registered_actions": [a.to_dict() for a in self._actions.values()],
            "executed_nodes": list(self._executed),
            "compensating": self._compensating,
        }


class CompensationRegistry:
    """补偿操作注册表(全局单例)。

    用于注册跨工作流的通用补偿操作。
    """

    def __init__(self):
        self._registries: dict[str, SagaOrchestrator] = {}

    def get_or_create(self, run_id: str) -> SagaOrchestrator:
        """获取或创建运行级别的 SAGA 编排器。"""
        if run_id not in self._registries:
            self._registries[run_id] = SagaOrchestrator()
        return self._registries[run_id]

    def remove(self, run_id: str) -> None:
        """移除运行级别的编排器。"""
        self._registries.pop(run_id, None)

    def list_active(self) -> list[str]:
        """列出活跃的运行 ID。"""
        return list(self._registries.keys())


# 全局补偿注册表
_compensation_registry: Optional[CompensationRegistry] = None


def get_compensation_registry() -> CompensationRegistry:
    """获取全局补偿注册表。"""
    global _compensation_registry
    if _compensation_registry is None:
        _compensation_registry = CompensationRegistry()
    return _compensation_registry
