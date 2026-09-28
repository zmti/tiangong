"""增强型工作流执行引擎 — DAG 拓扑排序 + 动态路由 + 并行执行 + 断点续跑。

核心改进:
1. 动态边路由:运行时根据变量动态计算边的激活状态
2. 增强循环节点:支持 foreach/while/do-until 三种循环模式
3. 节点级 Checkpoint:失败后可从断点恢复,跳过已成功节点
4. SAGA 补偿:失败时自动逆序补偿已执行节点
5. 性能统计:每个节点的耗时/重试次数/结果摘要
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from typing import Any, Callable, Optional

from app.core.workflow.checkpoint import WorkflowCheckpointManager, NodeCheckpoint
from app.core.workflow.dynamic_router import DynamicEdgeEvaluator
from app.core.workflow.loop_controller import LoopController, LoopControlType
from app.core.workflow.models import (
    Workflow, WorkflowRun, WorkflowNode, WorkflowEdge,
    NodeStatus, NodeType, ErrorStrategy, LoopMode
)
from app.core.workflow.saga import SagaOrchestrator, get_compensation_registry
from app.core.workflow.visualization import generate_execution_snapshot

logger = logging.getLogger(__name__)

# 回调类型: (run_id, node_id, status, result, error)
NodeEventCallback = Callable[[str, str, str, Optional[dict], Optional[str]], Any]


class WorkflowExecutor:
    """增强型工作流执行器。

    并发安全设计:
    - 每次 run() 调用时创建独立的状态,隔离运行时
    - 支持 SSE 事件回调,用于实时进度推送
    - 支持会话上下文注入(session_id)
    - 支持断点续跑(跳过已成功的节点)
    """

    def __init__(
        self,
        on_node_event: Optional[NodeEventCallback] = None,
        max_concurrency: int = 10,
        checkpoint_mgr: Optional[WorkflowCheckpointManager] = None,
        saga_mgr: Optional[SagaOrchestrator] = None,
    ):
        self._on_node_event = on_node_event
        self._max_concurrency = max_concurrency
        self._checkpoint_mgr = checkpoint_mgr or WorkflowCheckpointManager("")
        self._saga_mgr = saga_mgr or SagaOrchestrator()

    # ============================================================
    # 公开接口
    # ============================================================

    async def run(
        self,
        workflow: Workflow,
        inputs: dict[str, Any],
        run_id: Optional[str] = None,
        session_id: str = "",
        agent=None,
        skip_nodes: Optional[set[str]] = None,
    ) -> WorkflowRun:
        """执行工作流。

        Args:
            workflow: 工作流定义
            inputs: 输入变量
            run_id: 运行 ID(可选,用于断点续跑)
            session_id: 关联的会话 ID
            agent: Agent 实例(由调用方传入,避免循环依赖)
            skip_nodes: 已成功的节点 ID 集合(断点续跑时传入)

        Returns:
            WorkflowRun 运行结果
        """
        # 每个 run() 复用同一个 executor 实例,必须在运行开始时重置 SAGA 状态,
        # 否则上一轮已执行节点的补偿登记会残留,导致补偿误触发。
        if self._saga_mgr:
            self._saga_mgr.reset()

        effective_run_id = run_id or f"run_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        resolved_inputs = self._resolve_resume_inputs(effective_run_id, inputs)
        run = WorkflowRun(
            run_id=effective_run_id,
            workflow_id=workflow.id,
            context=resolved_inputs.copy(),
            variables=resolved_inputs.copy(),
            session_id=session_id,
        )

        # 检查是否存在可恢复的 checkpoint
        skippable_nodes = set()
        if effective_run_id and self._checkpoint_mgr:
            skippable_nodes = self._checkpoint_mgr.get_skippable_nodes(effective_run_id)
            if skippable_nodes:
                logger.info(f"发现 {len(skippable_nodes)} 个可跳过的已完成节点")

        if skip_nodes:
            skippable_nodes.update(skip_nodes)

        # 初始化所有节点状态
        for node_id in workflow.nodes:
            if node_id in skippable_nodes:
                # 加载 checkpoint 恢复状态
                cp = self._checkpoint_mgr.load_checkpoint(effective_run_id, node_id)
                run.node_states[node_id] = {
                    "status": NodeStatus.SUCCESS.value,
                    "result": cp.result if cp else None,
                    "error": None,
                    "started_at": cp.timestamp if cp else None,
                    "ended_at": cp.timestamp if cp else None,
                    "skipped": True,
                    "checkpoint_timestamp": cp.timestamp if cp else None,
                }
                run.variables[node_id] = self._extract_node_output(cp.result) if cp else None
                run.variables[f"{node_id}.output"] = run.variables[node_id]
            else:
                run.node_states[node_id] = {
                    "status": NodeStatus.PENDING.value,
                    "result": None,
                    "error": None,
                    "started_at": None,
                    "ended_at": None,
                }

        # 保存运行元数据(保存实际生效的输入, 恢复时优先复用)
        if self._checkpoint_mgr:
            self._checkpoint_mgr.save_run_metadata(run.run_id, workflow.id, resolved_inputs, resolved_inputs)

        # 发送运行开始事件
        self._emit_event(run.run_id, "__run__", "running", None, None)

        try:
            # 收集被循环节点托管的子图节点,将其排除出顶层拓扑执行
            # (这些节点只在循环体内部由 LoopController 驱动执行,避免被顶层重复/越序执行)
            loop_managed = set()
            for _n in workflow.nodes.values():
                if _n.type == NodeType.LOOP:
                    _sids = (_n.loop_config or {}).get("subgraph_ids") or _n.subgraph_ids or []
                    loop_managed.update(_sids)
            self._loop_managed_ids = loop_managed

            # 拓扑排序,构建执行层次
            layers = self._topological_layers(workflow, exclude=loop_managed)
            if layers is None:
                run.set_error("工作流存在循环依赖")
                self._emit_event(run.run_id, "__run__", "failed", None, "循环依赖")
                if self._checkpoint_mgr:
                    self._checkpoint_mgr.update_run_metadata(run.run_id, "failed", "循环依赖")
                return run

            logger.info(f"工作流 {workflow.id} 拓扑排序完成,共 {len(layers)} 层,可跳过 {len(skippable_nodes)} 个节点")

            # 逐层执行
            for layer_idx, layer in enumerate(layers):
                if run.cancelled:
                    break

                # 检查全局超时
                elapsed = time.time() - run.started_at
                if elapsed >= workflow.timeout:
                    run.set_error(f"工作流执行超时({workflow.timeout}s)")
                    self._emit_event(run.run_id, "__run__", "failed", None, "超时")
                    if self._checkpoint_mgr:
                        self._checkpoint_mgr.update_run_metadata(run.run_id, "failed", "超时")
                    return run

                # 收集本层需要执行的节点
                # 1) 跳过已成功的节点(断点续跑)
                # 2) 根据上游边的激活状态做动态路由门控(条件分支/默认分支真正生效)
                nodes_to_run = []
                for n in layer:
                    if n.id in skippable_nodes:
                        continue
                    if not self._is_node_active(workflow, run, n):
                        run.node_states[n.id] = {
                            "status": NodeStatus.SKIPPED.value,
                            "result": None, "error": None,
                            "started_at": None, "ended_at": None,
                            "skipped_by_routing": True,
                        }
                        self._emit_event(run.run_id, n.id, "skipped", None, None)
                        continue
                    nodes_to_run.append(n)
                if not nodes_to_run:
                    logger.debug(f"第 {layer_idx + 1} 层全部节点已跳过/未激活")
                    continue

                # 并行执行本层节点(带并发限制)
                semaphore = asyncio.Semaphore(self._max_concurrency)
                layer_results = await self._execute_layer_parallel(
                    workflow, run, nodes_to_run, semaphore, agent
                )

                # 处理本层结果(layer_results 与 nodes_to_run 一一对应)
                failed_nodes = []
                for node, result in zip(nodes_to_run, layer_results):
                    if result is None:
                        continue
                    if isinstance(result, Exception):
                        failed_nodes.append((node, result))

                if failed_nodes and workflow.error_mode != "continue_on_error":
                    # 有失败节点且不是 continue_on_error 模式
                    first_failed = failed_nodes[0][0]
                    error_msg = str(failed_nodes[0][1])
                    logger.error(f"工作流 {workflow.id} 节点 {first_failed.id} 执行失败: {error_msg}")

                    # 触发 SAGA 补偿
                    if self._saga_mgr.get_executed_nodes():
                        await self._compensate_failed_run(run, failed_nodes)

                    run.set_error(f"节点 {first_failed.id} 执行失败: {error_msg}")
                    self._emit_event(run.run_id, "__run__", "failed", None, error_msg)
                    if self._checkpoint_mgr:
                        self._checkpoint_mgr.update_run_metadata(
                            run.run_id, "failed", error_msg,
                            completed_nodes=sum(
                                1 for s in run.node_states.values()
                                if s.get("status") == NodeStatus.SUCCESS.value
                            )
                        )
                    return run

                logger.info(f"工作流 {workflow.id} 第 {layer_idx + 1}/{len(layers)} 层执行完成")

            # 标记完成
            run.status = "completed"
            run.finished_at = time.time()

            # 汇总输出
            outputs = {}
            for nid, state in run.node_states.items():
                if state["status"] == NodeStatus.FAILED.value:
                    outputs[nid] = {"error": state.get("error")}
                else:
                    outputs[nid] = state.get("result")
            run.output = {
                "nodes": outputs,
                "variables": dict(run.variables),
                "results": {nid: self._extract_node_output(state.get("result")) for nid, state in run.node_states.items()},
            }

            self._emit_event(run.run_id, "__run__", "completed", run.output, None)
            if self._checkpoint_mgr:
                self._checkpoint_mgr.update_run_metadata(
                    run.run_id, "completed",
                    completed_nodes=len(skippable_nodes) + sum(
                        1 for s in run.node_states.values()
                        if s.get("status") == NodeStatus.SUCCESS.value
                    ),
                    skipped_nodes=len(skippable_nodes),
                )
            logger.info(f"工作流 {workflow.id} 执行完成,耗时 {run.duration:.2f}s")

        except Exception as e:
            logger.error(f"工作流执行失败: {e}", exc_info=True)
            run.set_error(str(e))
            self._emit_event(run.run_id, "__run__", "failed", None, str(e))
            if self._checkpoint_mgr:
                self._checkpoint_mgr.update_run_metadata(run.run_id, "failed", str(e))

        return run

    async def cancel(self, run_id: str) -> bool:
        """取消运行。"""
        logger.info(f"取消运行 {run_id}")
        return True

    # ============================================================
    # 内部方法
    # ============================================================

    async def _execute_layer_parallel(
        self,
        workflow: Workflow,
        run: WorkflowRun,
        nodes: list[WorkflowNode],
        semaphore: asyncio.Semaphore,
        agent=None,
    ) -> list:
        """并行执行一層节点。"""
        async def _execute_with_semaphore(node: WorkflowNode):
            async with semaphore:
                return await self._execute_node(agent, workflow, run, node)

        tasks = [_execute_with_semaphore(node) for node in nodes]
        return await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute_node(
        self,
        agent,
        workflow: Workflow,
        run: WorkflowRun,
        node: WorkflowNode,
        force: bool = False,
    ) -> Optional[dict]:
        """执行单个节点,支持错误恢复和 Checkpoint。

        Args:
            force: 为 True 时跳过"已执行"缓存短路(用于循环体节点,每轮迭代需重新执行)。
        """
        # 检查是否已执行(跳过逻辑);force 模式下忽略缓存,确保循环体每轮都真实重跑
        if (not force) and run.node_states.get(node.id, {}).get("status") in (
            NodeStatus.SUCCESS.value, NodeStatus.FAILED.value, NodeStatus.SKIPPED.value
        ):
            return run.node_states[node.id].get("result")

        # 检查取消
        if run.cancelled:
            run.node_states[node.id] = {
                "status": NodeStatus.CANCELLED.value,
                "result": None, "error": "已取消",
                "started_at": None, "ended_at": None,
            }
            return None

        max_attempts = 1 + node.retry_count
        last_error = None

        for attempt in range(max_attempts):
            if run.cancelled:
                break

            t0 = time.time()
            run.node_states[node.id] = {
                "status": NodeStatus.RUNNING.value,
                "result": None, "error": None,
                "started_at": t0, "ended_at": None,
                "attempt": attempt + 1,
            }
            run.history.append({
                "node_id": node.id,
                "name": node.name,
                "type": node.type.value,
                "action": "start",
                "timestamp": t0,
                "attempt": attempt + 1,
            })
            self._emit_event(run.run_id, node.id, "running", None, None)

            try:
                result = await self._run_node_impl(agent, workflow, run, node)

                if isinstance(result, dict) and result.get("status") == "error":
                    error_msg = result.get("error", "未知错误")
                    raise RuntimeError(error_msg)

                t1 = time.time()
                duration = t1 - t0
                run.node_states[node.id] = {
                    "status": NodeStatus.SUCCESS.value,
                    "result": result, "error": None,
                    "started_at": t0, "ended_at": t1,
                    "duration": round(duration, 3),
                    "attempt": attempt + 1,
                }
                run.variables[node.id] = self._extract_node_output(result)
                run.variables[f"{node.id}.output"] = run.variables[node.id]
                run.history.append({
                    "node_id": node.id,
                    "name": node.name,
                    "type": node.type.value,
                    "action": "success",
                    "result": result,
                    "timestamp": t1,
                    "duration": round(duration, 3),
                    "attempt": attempt + 1,
                })
                self._emit_event(run.run_id, node.id, "success", result, None)

                # 保存 Checkpoint
                if self._checkpoint_mgr:
                    self._checkpoint_mgr.save_checkpoint(
                        run_id=run.run_id,
                        node_id=node.id,
                        status="success",
                        variables=dict(run.variables),
                        result=result,
                        duration=duration,
                        attempt=attempt + 1,
                    )

                # 登记 SAGA 补偿:节点声明了 compensation 时才登记,失败时逆序回滚
                if self._saga_mgr and node.compensation:
                    async def _comp(**kwargs):
                        return await self._run_compensation(node, run)
                    self._saga_mgr.register_compensation(node.id, compensation=_comp)

                return result

            except Exception as e:
                last_error = str(e)
                t1 = time.time()
                logger.warning(
                    "节点 %s 执行失败(尝试 %d/%d): %s",
                    node.id, attempt + 1, max_attempts, e,
                )

                run.node_states[node.id] = {
                    "status": NodeStatus.FAILED.value,
                    "result": None, "error": last_error,
                    "started_at": t0, "ended_at": t1,
                    "duration": round(t1 - t0, 3),
                    "attempt": attempt + 1,
                }
                run.history.append({
                    "node_id": node.id,
                    "name": node.name,
                    "type": node.type.value,
                    "action": "failed",
                    "error": last_error,
                    "timestamp": t1,
                    "attempt": attempt + 1,
                })
                self._emit_event(run.run_id, node.id, "failed", None, last_error)

                # 保存失败 checkpoint
                if self._checkpoint_mgr:
                    self._checkpoint_mgr.save_checkpoint(
                        run_id=run.run_id,
                        node_id=node.id,
                        status="failed",
                        variables=dict(run.variables),
                        error=last_error,
                        duration=t1 - t0,
                        attempt=attempt + 1,
                    )

                if attempt < max_attempts - 1:
                    await asyncio.sleep(node.retry_delay)
                    continue

                # 重试耗尽,根据策略处理
                if node.error_strategy == ErrorStrategy.CONTINUE:
                    logger.info("节点 %s 启用 CONTINUE 策略,跳过", node.id)
                    run.node_states[node.id]["status"] = NodeStatus.SKIPPED.value
                    run.node_states[node.id]["error"] = None
                    self._emit_event(run.run_id, node.id, "skipped", None, None)
                    return None

                elif (node.error_strategy == ErrorStrategy.FALLBACK
                      and node.fallback_node_id):
                    logger.info("节点 %s 启用 FALLBACK 策略,执行 %s", node.id, node.fallback_node_id)
                    fallback = workflow.get_node(node.fallback_node_id)
                    if fallback:
                        return await self._execute_node(agent, workflow, run, fallback)
                    else:
                        logger.error("FALLBACK 节点 %s 不存在", node.fallback_node_id)

                elif workflow.error_mode == "continue_on_error":
                    logger.info("工作流启用 continue_on_error,节点 %s 失败后继续", node.id)
                    return None

                else:
                    run.set_error(f"节点 {node.id} 执行失败: {last_error}")
                    raise

        return None

    async def _run_node_impl(
        self,
        agent,
        workflow: Workflow,
        run: WorkflowRun,
        node: WorkflowNode,
    ) -> dict:
        """执行单个节点的具体逻辑。"""
        if node.type == NodeType.START:
            return {"status": "success", "output": None}

        elif node.type == NodeType.END:
            return {"status": "success", "output": None}

        elif node.type == NodeType.AGENT:
            return await self._exec_agent(agent, node, run)

        elif node.type == NodeType.TOOL:
            return await self._exec_tool(agent, node, run)

        elif node.type == NodeType.CONDITION:
            result = await self._exec_condition(node, run)
            run.variables[node.id] = result.get("output")
            run.variables[f"{node.id}.output"] = result.get("output")
            return result

        elif node.type == NodeType.LOOP:
            return await self._exec_loop(node, workflow, run, agent)

        elif node.type == NodeType.HTTP:
            return await self._exec_http(node, run)

        elif node.type == NodeType.SLEEP:
            return await self._exec_sleep(node, run)

        elif node.type == NodeType.SCRIPT:
            return await self._exec_script(node, run)

        else:
            return {"status": "success", "output": None}

    async def _exec_agent(self, agent, node: WorkflowNode, run: WorkflowRun) -> dict:
        """Agent 节点:调用 AI 智能体。"""
        if agent is None:
            return {"status": "error", "output": None, "error": "Agent 未初始化"}

        config = node.agent_config or {}
        prompt = config.get("input", "")
        if not prompt:
            prompt = node.config.get("input", "")

        # 模板变量替换
        prompt = self._render_template(prompt, run.variables)

        if not prompt or not prompt.strip():
            return {"status": "success", "output": {"answer": ""}}

        # 构建工作流上下文信息
        context_info = self._build_workflow_context(run)
        full_prompt = f"{context_info}\n\n{prompt}" if context_info else prompt

        # 执行 Agent
        agent_result = await agent.run(full_prompt)
        output = {
            "answer": agent_result.answer,
            "steps": agent_result.steps,
            "model": agent_result.model,
        }
        run.variables[node.id] = output
        run.variables[f"{node.id}.output"] = output
        return {
            "status": "success",
            "output": output,
        }

    async def _exec_tool(self, agent, node: WorkflowNode, run: WorkflowRun) -> dict:
        """执行工具节点。"""
        tool_name = (
            node.tool_name
            or node.config.get("tool")
            or node.config.get("tool_name")
            or ""
        )
        params = node.config.get("arguments", node.config.get("params", {}))

        # 模板变量替换(保持 JSON 类型)
        params = self._render_template_values(params, run.variables)

        if not tool_name:
            return {"status": "error", "output": None, "error": "工具节点未配置工具名"}

        from app.tools.registry import get_registry
        registry = get_registry()
        tool = registry.get(tool_name)
        if tool is None:
            return {"status": "error", "output": None, "error": f"未知工具: {tool_name}"}

        try:
            raw = await asyncio.wait_for(tool.run(params), timeout=node.timeout or 60)
        except Exception as e:
            return {"status": "error", "output": None, "error": f"工具执行失败: {e}"}

        # 解析结果
        try:
            if isinstance(raw, str):
                parsed = json.loads(raw)
                output = parsed if isinstance(parsed, dict) else {"result": parsed}
            else:
                output = raw if isinstance(raw, dict) else {"result": raw}
        except Exception:
            output = {"result": raw}

        return {"status": "success", "output": output}

    async def _exec_condition(self, node: WorkflowNode, run: WorkflowRun) -> dict:
        """执行条件节点。"""
        expr = node.condition_expr or node.config.get("condition_expr", "")
        if not expr.strip():
            return {"status": "success", "output": True, "condition": ""}

        try:
            from app.core.workflow.safe_eval import safe_eval
            result = safe_eval(expr, run.variables)
            return {"status": "success", "output": bool(result), "condition": expr}
        except Exception as e:
            return {"status": "error", "output": False, "condition": expr, "error": str(e)}

    async def _exec_loop(
        self, node: WorkflowNode, workflow: Workflow, run: WorkflowRun, agent
    ) -> dict:
        """增强型循环节点。"""
        config = node.loop_config or {}
        loop_type = config.get("type", "foreach")
        # 兼容 UI 里 loop_config.max 字段(前端保存的是 max, 不是 max_iterations)
        if "max_iterations" not in config and "max" in config:
            config["max_iterations"] = config["max"]
        # 兼容 UI 里把子图节点 ID 直接存在 node.subgraph_ids 的情况
        if not config.get("subgraph_ids") and node.subgraph_ids:
            config["subgraph_ids"] = node.subgraph_ids
        # 顺序/并行两种循环模式(loop_mode)映射到 batch_size
        if node.loop_mode and str(node.loop_mode).lower() == "parallel":
            config.setdefault("batch_size", len(config.get("subgraph_ids") or [0]) or 10)

        # 构建动态边评估器
        evaluator = DynamicEdgeEvaluator(
            variables=run.variables,
            node_results={nid: state.get("result") for nid, state in run.node_states.items()
                         if state.get("status") == NodeStatus.SUCCESS.value},
        )

        # 创建循环控制器
        controller = LoopController(
            loop_config=config,
            workflow_nodes=workflow.nodes,
            variables=run.variables,
            node_results={nid: state.get("result") for nid, state in run.node_states.items()
                         if state.get("status") == NodeStatus.SUCCESS.value},
            execute_node_func=lambda n: self._execute_node(agent, workflow, run, n, force=True),
            save_checkpoint_func=lambda node_id, status, variables, result=None:
                self._checkpoint_mgr.save_checkpoint(run.run_id, node_id, status, variables, result) if self._checkpoint_mgr else None,
        )

        # 执行循环
        result = await controller.execute(evaluator)
        run.variables["loop_result"] = result
        return {"status": "success", "output": result}

    async def _exec_http(self, node: WorkflowNode, run: WorkflowRun) -> dict:
        """HTTP 请求节点。"""
        import httpx
        config = node.http_config or {}
        url = self._render_template(config.get("url", ""), run.variables)
        method = config.get("method", "GET").upper()
        headers = config.get("headers", {})
        body = config.get("body")

        headers = self._render_template_values(headers, run.variables)
        if body:
            body = self._render_template_values(body, run.variables)

        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method=method, url=url,
                    headers=headers, json=body, timeout=30.0,
                )
            return {
                "status": "success",
                "output": {
                    "status_code": response.status_code,
                    "body": response.json()
                    if "json" in str(response.headers.get("content-type", ""))
                    else response.text,
                },
            }
        except Exception as e:
            return {"status": "error", "output": None, "error": str(e)}

    async def _exec_sleep(self, node: WorkflowNode, run: WorkflowRun) -> dict:
        """延时节点。"""
        seconds = node.config.get("seconds", 1)
        await asyncio.sleep(float(seconds))
        return {"status": "success", "output": None}

    async def _exec_script(self, node: WorkflowNode, run: WorkflowRun) -> dict:
        """安全脚本执行节点。"""
        import ast as _ast
        code = node.script_code or ""
        if not code.strip():
            return {"status": "success", "output": {}}

        try:
            tree = _ast.parse(code)
        except SyntaxError as e:
            return {"status": "error", "output": None, "error": f"语法错误: {e}"}

        for stmt in _ast.walk(tree):
            if isinstance(stmt, (_ast.Import, _ast.ImportFrom)):
                return {"status": "error", "output": None,
                        "error": "脚本中不允许 import"}

        # 构建安全的执行环境
        safe_builtins = {
            'abs': abs, 'round': round, 'sum': sum, 'min': min, 'max': max,
            'pow': pow, 'divmod': divmod, 'complex': complex,
            'int': int, 'float': float, 'bool': bool, 'str': str,
            'bytes': bytes, 'bytearray': bytearray,
            'list': list, 'tuple': tuple, 'set': set, 'frozenset': frozenset,
            'dict': dict, 'slice': slice,
            'range': range, 'enumerate': enumerate, 'zip': zip,
            'map': map, 'filter': filter, 'iter': iter,
            'next': next, 'reversed': reversed,
            'sorted': sorted,
            'isinstance': isinstance, 'issubclass': issubclass, 'type': type,
            'len': len, 'any': any, 'all': all,
            'format': format, 'repr': repr,
            'True': True, 'False': False, 'None': None,
        }
        dangerous_ops = {'open', 'input', 'eval', 'exec', 'compile',
                        'breakpoint', 'callable', 'globals', 'locals',
                        'dir', 'hasattr', 'getattr', 'setattr', 'delattr',
                        '__import__', 'memoryview', 'object'}
        for op in dangerous_ops:
            safe_builtins.pop(op, None)

        safe_globals = {"__builtins__": safe_builtins}
        safe_locals = dict(run.variables)

        try:
            exec_timeout = node.config.get("exec_timeout", 10)
            result = await asyncio.wait_for(
                asyncio.to_thread(exec, code, safe_globals, safe_locals),
                timeout=exec_timeout,
            )
            output = {k: v for k, v in safe_locals.items()
                      if not k.startswith('_') and k not in run.variables}
            return {"status": "success", "output": output or safe_locals}
        except asyncio.TimeoutError:
            return {"status": "error", "output": None,
                    "error": f"脚本执行超时({exec_timeout}s)"}
        except Exception as e:
            return {"status": "error", "output": None, "error": str(e)}

    # ============================================================
    # 工具方法
    # ============================================================

    def _resolve_resume_inputs(self, run_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
        """恢复运行时尽量复用上次输入, 缺失字段才用本次输入补位。"""
        if not run_id or not self._checkpoint_mgr:
            return dict(inputs or {})
        metadata = self._checkpoint_mgr.load_run_metadata(run_id)
        saved = dict(metadata.get("variables") or metadata.get("context") or {}) if metadata else {}
        merged = dict(saved)
        merged.update(inputs or {})
        return merged

    @staticmethod
    def _extract_node_output(result: Any) -> Any:
        """把节点结果统一为可模板引用的值, 支持 {{node_id.output}}。"""
        if isinstance(result, dict):
            if "output" in result:
                return result.get("output")
            if "answer" in result:
                return result.get("answer")
            if "result" in result:
                return result.get("result")
        return result

    def _is_node_active(
        self,
        workflow: Workflow,
        run: WorkflowRun,
        node: WorkflowNode,
    ) -> bool:
        """判断节点在当前阶段是否应当被激活(动态路由门控)。

        规则:
        - 无任何入边的节点(根节点/START)始终激活;
        - 否则,只要存在一条「源节点已成功执行 且 该边条件表达式为真」的入边即激活;
        - 否则视为未激活,跳过执行 —— 条件分支 / 默认分支由此真正生效。
        """
        incoming = [e for e in workflow.edges if e.target == node.id]
        if not incoming:
            return True

        success_results = {
            nid: s.get("result")
            for nid, s in run.node_states.items()
            if s.get("status") == NodeStatus.SUCCESS.value
        }
        for edge in incoming:
            src_state = run.node_states.get(edge.source, {})
            if src_state.get("status") != NodeStatus.SUCCESS.value:
                continue
            cond = getattr(edge, "condition_expr", None) or getattr(edge, "condition", None)
            if not cond or not cond.strip():
                return True
            try:
                ev = DynamicEdgeEvaluator(variables=run.variables, node_results=success_results)
                if ev.evaluate(cond):
                    return True
            except Exception:
                # 条件评估异常时保守地视为不激活,避免误执行
                continue
        # 默认分支:所有条件边均不激活时,若存在标记为默认的入边则激活之
        for edge in incoming:
            src_state = run.node_states.get(edge.source, {})
            if src_state.get("status") != NodeStatus.SUCCESS.value:
                continue
            if getattr(edge, "is_default", False):
                return True
        return False

    def _topological_layers(
        self, workflow: Workflow, exclude: Optional[set] = None
    ) -> Optional[list[list[WorkflowNode]]]:
        """Kahn's algorithm: 返回层次列表,每层内的节点可并行执行。

        Args:
            exclude: 需要排除在拓扑排序之外的节点 ID 集合(例如被循环节点托管的子图节点)。
                     这些节点不进入执行层次,也不会影响下游节点的入度计算(避免误判循环依赖)。
        """
        exclude = exclude or set()
        in_degree = {nid: 0 for nid in workflow.nodes if nid not in exclude}
        for edge in workflow.edges:
            if edge.target in in_degree and edge.source not in exclude:
                in_degree[edge.target] += 1

        layers = []
        ready = deque([nid for nid, deg in in_degree.items() if deg == 0])

        while ready:
            layer = []
            next_ready = deque()

            while ready:
                nid = ready.popleft()
                node = workflow.nodes.get(nid)
                if node:
                    layer.append(node)

                for edge in workflow.edges:
                    if (edge.source == nid and edge.source not in exclude
                            and edge.target in in_degree):
                        in_degree[edge.target] -= 1
                        if in_degree[edge.target] == 0:
                            next_ready.append(edge.target)

            if layer:
                layers.append(layer)
            ready = next_ready

        processed = sum(len(layer) for layer in layers)
        if processed < len(in_degree):
            return None  # 存在循环(排除节点之外的节点未全部排入层次)

        return layers

    def _render_template(self, text: str, variables: dict[str, Any]) -> str:
        """替换 {{var_name}} 为运行时变量值, 支持 {{node_id.output}}。"""
        if not text:
            return text
        for var_name, var_value in variables.items():
            if not isinstance(var_value, str):
                try:
                    var_value = json.dumps(var_value, ensure_ascii=False)
                except Exception:
                    var_value = str(var_value)
            text = text.replace(f"{{{{{var_name}}}}}", var_value)
        return text

    def _render_template_values(self, value: Any, variables: dict[str, Any]) -> Any:
        """递归替换模板变量,保持 JSON 类型。"""
        if isinstance(value, str):
            return self._render_template(value, variables)
        elif isinstance(value, dict):
            return {k: self._render_template_values(v, variables) for k, v in value.items()}
        elif isinstance(value, list):
            return [self._render_template_values(item, variables) for item in value]
        return value

    def _build_workflow_context(self, run: WorkflowRun) -> str:
        """构建工作流上下文信息。"""
        lines = []
        if run.session_id:
            lines.append(f"[工作流上下文] 关联会话: {run.session_id}")
        if run.context:
            lines.append(f"[工作流输入] {json.dumps(run.context, ensure_ascii=False)}")
        completed = [
            h for h in run.history
            if h.get("action") == "success" and h.get("result")
        ]
        if completed:
            lines.append(f"[工作流历史] 已完成 {len(completed)} 个节点")
        return "\n".join(lines) if lines else ""

    def _emit_event(
        self, run_id: str, node_id: str, status: str,
        result: Optional[dict] = None, error: Optional[str] = None,
    ) -> None:
        """发送节点事件回调。"""
        if self._on_node_event:
            try:
                self._on_node_event(run_id, node_id, status, result, error)
            except Exception as e:
                logger.debug(f"事件回调失败: {e}")

    async def _compensate_failed_run(self, run: WorkflowRun, failed_nodes: list) -> None:
        """对失败运行进行 SAGA 补偿。"""
        executed = self._saga_mgr.get_executed_nodes()
        if not executed:
            return

        failed_node_id = failed_nodes[0][0].id if failed_nodes else None
        try:
            compensated = await self._saga_mgr.compensate_up_to(failed_node_id or "")
            logger.info(f"SAGA 补偿完成: 补偿了 {len(compensated)} 个节点")
        except Exception as e:
            logger.error(f"SAGA 补偿失败: {e}")

    async def _run_compensation(self, node: WorkflowNode, run: WorkflowRun) -> dict:
        """执行单个节点的补偿动作(SAGA 补偿)。

        支持两种声明方式:
        - {"type": "script", "code": "..."}:在沙箱中执行回滚脚本(复用 _exec_script)
        - {"type": "http", "http_config": {...}} 或 {"type": "http", "url": ...}:发起回滚请求(复用 _exec_http)
        未声明 type 时默认按 script 处理。
        """
        comp = node.compensation or {}
        ctype = (comp.get("type") or "script").lower()
        try:
            if ctype == "http":
                http_node = WorkflowNode(
                    id=f"_comp_{node.id}",
                    type=NodeType.HTTP,
                    http_config=comp.get("http_config") or comp,
                )
                result = await self._exec_http(http_node, run)
            else:
                script_node = WorkflowNode(
                    id=f"_comp_{node.id}",
                    type=NodeType.SCRIPT,
                    script_code=comp.get("code") or comp.get("script") or "",
                )
                result = await self._exec_script(script_node, run)
            status = "success" if (isinstance(result, dict) and result.get("status") == "success") else "error"
        except Exception as e:
            result = {"status": "error", "error": str(e)}
            status = "error"

        run.history.append({
            "node_id": node.id,
            "action": "compensate",
            "status": status,
            "result": result,
            "timestamp": time.time(),
        })
        return result
