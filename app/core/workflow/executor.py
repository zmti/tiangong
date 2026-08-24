"""工作流执行引擎。"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from app.core.workflow.models import (
    Workflow, WorkflowRun, WorkflowNode, NodeStatus, NodeType
)
from app.core.agent import Agent

logger = logging.getLogger(__name__)


class WorkflowExecutor:
    """工作流执行器。"""
    
    def __init__(self, agent: Agent):
        self.agent = agent
        self._running_runs: dict[str, WorkflowRun] = {}
    
    async def run(
        self,
        workflow: Workflow,
        inputs: dict[str, Any],
        run_id: Optional[str] = None
    ) -> WorkflowRun:
        """执行工作流。"""
        run = WorkflowRun(
            run_id=run_id or f"run_{int(time.time()*1000)}",
            workflow_id=workflow.id,
            context=inputs.copy(),
            variables=inputs.copy()
        )
        
        self._running_runs[run.run_id] = run
        
        try:
            # 初始化节点状态
            for node_id in workflow.nodes:
                run.node_states[node_id] = {
                    "status": NodeStatus.PENDING.value,
                    "result": None,
                    "error": None,
                    "started_at": None,
                    "ended_at": None
                }
            
            # 从入口节点开始执行
            entry_node = workflow.get_node(workflow.entry_node)
            if not entry_node:
                run.set_error("入口节点不存在")
                return run
            
            # 执行工作流
            await self._execute_node(workflow, run, entry_node)
            
            # 标记完成
            run.status = "completed"
            run.finished_at = time.time()
            
        except Exception as e:
            logger.error(f"工作流执行失败: {e}", exc_info=True)
            run.set_error(str(e))
        
        finally:
            del self._running_runs[run.run_id]
        
        return run
    
    async def _execute_node(
        self,
        workflow: Workflow,
        run: WorkflowRun,
        node: WorkflowNode
    ) -> None:
        """执行单个节点。"""
        node_start = time.time()
        
        # 更新节点状态
        run.update_node_state(node.id, {
            "status": NodeStatus.RUNNING.value,
            "started_at": node_start
        })
        
        try:
            # 根据节点类型执行
            if node.type == NodeType.AGENT:
                result = await self._execute_agent_node(node, run)
            elif node.type == NodeType.TOOL:
                result = await self._execute_tool_node(node, run)
            elif node.type == NodeType.CONDITION:
                result = await self._execute_condition_node(node, run)
            elif node.type == NodeType.LOOP:
                result = await self._execute_loop_node(node, run)
            elif node.type == NodeType.HTTP:
                result = await self._execute_http_node(node, run)
            elif node.type == NodeType.SLEEP:
                result = await self._execute_sleep_node(node, run)
            elif node.type == NodeType.SCRIPT:
                result = await self._execute_script_node(node, run)
            else:
                result = {"status": "unknown", "output": None}
            
            # 记录结果
            node_end = time.time()
            run.variables[node.id] = result.get("output")
            run.update_node_state(node.id, {
                "status": NodeStatus.SUCCESS.value,
                "result": result,
                "ended_at": node_end,
                "duration": node_end - node_start
            })
            
            # 执行下一个节点
            next_nodes = workflow.get_next_nodes(node.id, run.variables)
            for next_node_id in next_nodes:
                next_node = workflow.get_node(next_node_id)
                if next_node:
                    await self._execute_node(workflow, run, next_node)
        
        except Exception as e:
            logger.error(f"节点 {node.id} 执行失败: {e}", exc_info=True)
            node_end = time.time()
            run.update_node_state(node.id, {
                "status": NodeStatus.FAILED.value,
                "error": str(e),
                "ended_at": node_end
            })
            run.set_error(f"节点 {node.id} 执行失败: {e}")
    
    async def _execute_agent_node(
        self, node: WorkflowNode, run: WorkflowRun
    ) -> dict:
        """执行 Agent 节点。"""
        config = node.agent_config or {}
        user_input = config.get("input", "")
        
        # 替换模板变量
        for var_name, var_value in run.variables.items():
            user_input = user_input.replace(f"{{{{{var_name}}}}}", str(var_value))
        
        # 调用 Agent
        result = await self.agent.run(user_input)
        
        return {
            "status": "success",
            "output": {
                "answer": result.answer,
                "steps": result.steps,
                "model": result.model
            }
        }
    
    async def _execute_tool_node(
        self, node: WorkflowNode, run: WorkflowRun
    ) -> dict:
        """执行工具调用节点。"""
        tool_name = node.tool_name or ""
        params = node.config.get("params", {})
        
        # 替换参数模板
        for var_name, var_value in run.variables.items():
            if isinstance(params, dict):
                params[var_name] = var_value
        
        # 这里需要接入工具执行器
        # 暂时返回模拟结果
        return {
            "status": "success",
            "output": {
                "tool": tool_name,
                "params": params,
                "result": f"Tool {tool_name} executed"
            }
        }
    
    async def _execute_condition_node(
        self, node: WorkflowNode, run: WorkflowRun
    ) -> dict:
        """执行条件分支节点。"""
        condition = node.condition_expr or ""
        
        try:
            # 评估条件
            result = eval(condition, {"__builtins__": {}}, run.variables)
            return {
                "status": "success",
                "output": bool(result),
                "condition": condition
            }
        except Exception as e:
            return {
                "status": "error",
                "output": None,
                "error": str(e)
            }
    
    async def _execute_loop_node(
        self, node: WorkflowNode, run: WorkflowRun
    ) -> dict:
        """执行循环节点。"""
        config = node.loop_config or {}
        iterable = config.get("iterable", [])
        max_iterations = config.get("max", 10)
        
        results = []
        for i, item in enumerate(iterable[:max_iterations]):
            run.variables[f"loop_item_{i}"] = item
            results.append(item)
        
        return {
            "status": "success",
            "output": {
                "items": results,
                "count": len(results)
            }
        }
    
    async def _execute_http_node(
        self, node: WorkflowNode, run: WorkflowRun
    ) -> dict:
        """执行 HTTP 请求节点。"""
        import httpx
        
        config = node.http_config or {}
        url = config.get("url", "")
        method = config.get("method", "GET").upper()
        headers = config.get("headers", {})
        body = config.get("body")
        
        # 替换 URL 模板变量
        for var_name, var_value in run.variables.items():
            url = url.replace(f"{{{{{var_name}}}}}", str(var_value))
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=body,
                    timeout=30.0
                )
            
            return {
                "status": "success",
                "output": {
                    "status_code": response.status_code,
                    "body": response.json() if response.headers.get("content-type") == "application/json" else response.text
                }
            }
        except Exception as e:
            return {
                "status": "error",
                "output": None,
                "error": str(e)
            }
    
    async def _execute_sleep_node(
        self, node: WorkflowNode, run: WorkflowRun
    ) -> dict:
        """执行延时节点。"""
        seconds = node.config.get("seconds", 1)
        await asyncio.sleep(seconds)
        return {"status": "success", "output": None}
    
    async def _execute_script_node(
        self, node: WorkflowNode, run: WorkflowRun
    ) -> dict:
        """执行脚本节点。"""
        code = node.script_code or ""
        
        # 安全执行（限制内置函数）
        safe_globals = {"__builtins__": {}}
        safe_locals = run.variables.copy()
        
        try:
            exec(code, safe_globals, safe_locals)
            return {
                "status": "success",
                "output": safe_locals
            }
        except Exception as e:
            return {
                "status": "error",
                "output": None,
                "error": str(e)
            }
    
    async def cancel(self, run_id: str) -> bool:
        """取消运行。"""
        run = self._running_runs.get(run_id)
        if not run:
            return False
        
        run.status = "cancelled"
        run.finished_at = time.time()
        return True
