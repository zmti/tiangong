"""
高级 DAG 工作流编排快速开始示例。

演示如何使用新的编排功能创建和执行复杂工作流。
"""
import asyncio
import json


async def demo_basic_workflow():
    """示例1: 基本工作流执行。"""
    from app.core.workflow.models import Workflow, WorkflowNode, WorkflowEdge, NodeType
    from app.core.workflow.executor import WorkflowExecutor

    # 创建简单工作流: start -> agent -> end
    workflow = Workflow(
        id="demo_basic",
        name="基础示例",
        nodes={
            "start": WorkflowNode(id="start", type=NodeType.START, name="开始"),
            "analyze": WorkflowNode(
                id="analyze",
                type=NodeType.AGENT,
                name="分析任务",
                agent_config={"input": "请分析以下文本: {{text}}"}
            ),
            "end": WorkflowNode(id="end", type=NodeType.END, name="结束")
        },
        edges=[
            WorkflowEdge(id="e1", source="start", target="analyze"),
            WorkflowEdge(id="e2", source="analyze", target="end"),
        ],
        entry_node="start",
    )

    print(f"工作流创建成功: {workflow.name}")
    print(f"节点数: {len(workflow.nodes)}, 边数: {len(workflow.edges)}")


async def demo_dynamic_routing():
    """示例2: 动态路由工作流。"""
    from app.core.workflow.models import Workflow, WorkflowNode, WorkflowEdge, NodeType
    from app.core.workflow.dynamic_router import DynamicEdgeEvaluator

    # 创建带动态路由的工作流
    workflow = Workflow(
        id="demo_route",
        name="动态路由示例",
        nodes={
            "start": WorkflowNode(id="start", type=NodeType.START, name="开始"),
            "classify": WorkflowNode(
                id="classify",
                type=NodeType.AGENT,
                name="分类",
                agent_config={"input": "判断任务类型: {{task_desc}}"}
            ),
            "simple": WorkflowNode(
                id="simple",
                type=NodeType.AGENT,
                name="简单处理",
                agent_config={"input": "处理简单任务"}
            ),
            "complex": WorkflowNode(
                id="complex",
                type=NodeType.AGENT,
                name="复杂处理",
                agent_config={"input": "处理复杂任务"}
            ),
            "end": WorkflowNode(id="end", type=NodeType.END, name="结束")
        },
        edges=[
            WorkflowEdge(id="e1", source="start", target="classify"),
            WorkflowEdge(
                id="e2",
                source="classify",
                target="simple",
                condition_expr="{{classify.output.answer contains '简单'}}"
            ),
            WorkflowEdge(
                id="e3",
                source="classify",
                target="complex",
                condition_expr="{{classify.output.answer contains '复杂'}}"
            ),
            WorkflowEdge(id="e4", source="simple", target="end"),
            WorkflowEdge(id="e5", source="complex", target="end"),
        ],
        entry_node="start",
    )

    # 演示动态路由评估
    evaluator = DynamicEdgeEvaluator(
        variables={},
        node_results={
            "classify": {
                "output": {"answer": "这是一个简单的查询任务"}
            }
        }
    )

    classify_edges = [e for e in workflow.edges if e.source == "classify"]
    targets = evaluator.find_active_targets("classify", classify_edges)
    print(f"分类结果路由到: {targets}")


async def demo_loop_workflow():
    """示例3: 循环工作流。"""
    from app.core.workflow.models import Workflow, WorkflowNode, WorkflowEdge, NodeType

    # 创建带循环的工作流
    workflow = Workflow(
        id="demo_loop",
        name="批量处理示例",
        nodes={
            "start": WorkflowNode(id="start", type=NodeType.START, name="开始"),
            "batch": WorkflowNode(
                id="batch",
                type=NodeType.LOOP,
                name="批量处理",
                loop_config={
                    "type": "foreach",
                    "iterable": "${items}",
                    "max_iterations": 100,
                    "batch_size": 5,
                    "subgraph_ids": ["process_item"],
                }
            ),
            "process_item": WorkflowNode(
                id="process_item",
                type=NodeType.AGENT,
                name="处理项目",
                agent_config={"input": "处理项: {{loop_item}}"}
            ),
            "end": WorkflowNode(id="end", type=NodeType.END, name="结束")
        },
        edges=[
            WorkflowEdge(id="e1", source="start", target="batch"),
            WorkflowEdge(id="e2", source="batch", target="end"),
        ],
        entry_node="start",
    )

    print(f"循环工作流创建成功: {workflow.name}")
    print(f"循环配置: {workflow.nodes['batch'].loop_config}")


async def demo_checkpoint_resume():
    """示例4: Checkpoint 断点续跑。"""
    import tempfile
    from app.core.workflow.checkpoint import WorkflowCheckpointManager

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/checkpoints.db"
        mgr = WorkflowCheckpointManager(db_path)

        # 模拟执行部分节点后保存 checkpoint
        mgr.save_checkpoint(
            run_id="run_001",
            node_id="node_1",
            status="success",
            variables={"step": 1},
            result={"output": "第一步完成"},
            duration=1.2,
        )
        mgr.save_checkpoint(
            run_id="run_001",
            node_id="node_2",
            status="success",
            variables={"step": 2},
            result={"output": "第二步完成"},
            duration=0.8,
        )
        # node_3 失败
        mgr.save_checkpoint(
            run_id="run_001",
            node_id="node_3",
            status="failed",
            variables={"step": 3},
            error="网络超时",
            duration=5.0,
        )

        # 恢复时跳过已成功的节点
        skippable = mgr.get_skippable_nodes("run_001")
        print(f"可跳过的节点: {skippable}")  # {'node_1', 'node_2'}

        # 获取执行历史
        all_checkpoints = mgr.get_all_checkpoints("run_001")
        for cp in all_checkpoints:
            print(f"  {cp.node_id}: {cp.status} (duration={cp.duration}s)")


async def demo_template_instantiation():
    """示例5: 模板实例化。"""
    from app.core.workflow.templates import get_template_registry

    registry = get_template_registry()

    # 列出所有模板
    templates = registry.list_all()
    print("可用模板:")
    for t in templates:
        print(f"  - {t.id}: {t.name} ({len(t.tags)} tags)")

    # 搜索模板
    research_templates = registry.search("research")
    print(f"\n搜索 'research': {len(research_templates)} 个结果")

    # 实例化模板
    workflow = registry.instantiate(
        template_id="research_report",
        instance_id="inst_001",
        inputs={"topic": "大语言模型", "depth": 3}
    )
    print(f"\n实例化工作流: {workflow.name}")
    print(f"节点数: {len(workflow.nodes)}, 边数: {len(workflow.edges)}")


async def demo_orchestration():
    """示例6: 多工作流编排。"""
    from app.core.workflow.orchestrator import get_orchestrator

    orchestrator = get_orchestrator()

    # 添加依赖关系
    orchestrator.add_dependency("wf_report", ["wf_research"])
    orchestrator.add_dependency("wf_summary", ["wf_report"])

    print("工作流依赖关系:")
    for wf_id, deps in orchestrator._dependencies.items():
        print(f"  {wf_id} <- {deps}")

    # 注册回调
    def on_complete(run):
        print(f"工作流完成: {run.workflow_id}, 状态: {run.status}")

    orchestrator.on_complete("wf_research", on_complete)
    print("\n回调已注册")


async def demo_visualization():
    """示例7: DAG 可视化。"""
    from app.core.workflow.models import Workflow, WorkflowNode, WorkflowEdge, NodeType
    from app.core.workflow.visualization import generate_dag_visualization, generate_execution_snapshot

    workflow = Workflow(
        id="viz_demo",
        name="可视化示例",
        nodes={
            "start": WorkflowNode(id="start", type=NodeType.START, name="开始"),
            "fetch": WorkflowNode(id="fetch", type=NodeType.TOOL, name="获取数据"),
            "analyze": WorkflowNode(id="analyze", type=NodeType.AGENT, name="分析"),
            "report": WorkflowNode(id="report", type=NodeType.AGENT, name="报告"),
            "notify": WorkflowNode(id="notify", type=NodeType.HTTP, name="通知"),
            "end": WorkflowNode(id="end", type=NodeType.END, name="结束"),
        },
        edges=[
            WorkflowEdge(id="e1", source="start", target="fetch"),
            WorkflowEdge(id="e2", source="fetch", target="analyze"),
            WorkflowEdge(id="e3", source="analyze", target="report"),
            WorkflowEdge(id="e4", source="report", target="notify"),
            WorkflowEdge(id="e5", source="notify", target="end"),
        ],
        entry_node="start",
    )

    # 生成 DAG 可视化数据
    viz = generate_dag_visualization(workflow)
    print(f"DAG 可视化: {len(viz['nodes'])} 个节点, {len(viz['edges'])} 条边")
    print(f"布局类型: {viz['layout']}")

    # 模拟运行快照
    from app.core.workflow.models import WorkflowRun, NodeStatus
    run = WorkflowRun(
        run_id="run_viz_001",
        workflow_id="viz_demo",
        node_states={
            "start": {"status": NodeStatus.SUCCESS.value, "duration": 0.01},
            "fetch": {"status": NodeStatus.SUCCESS.value, "duration": 1.5},
            "analyze": {"status": NodeStatus.RUNNING.value, "duration": 0},
            "report": {"status": NodeStatus.PENDING.value, "duration": 0},
            "notify": {"status": NodeStatus.PENDING.value, "duration": 0},
            "end": {"status": NodeStatus.PENDING.value, "duration": 0},
        },
        history=[
            {"node_id": "start", "action": "success", "timestamp": 1.0, "duration": 0.01},
            {"node_id": "fetch", "action": "success", "timestamp": 2.5, "duration": 1.5},
        ],
    )
    run.status = "running"
    run.started_at = 1.0

    snapshot = generate_execution_snapshot(run)
    print(f"\n运行快照: 进度 {snapshot['progress']:.1%}")
    print(f"已完成: {snapshot['completed_nodes']}/{snapshot['total_nodes']} 个节点")


async def main():
    """运行所有示例。"""
    print("=" * 60)
    print("高级 DAG 工作流编排 - 快速开始示例")
    print("=" * 60)

    print("\n--- 示例1: 基本工作流 ---")
    await demo_basic_workflow()

    print("\n--- 示例2: 动态路由 ---")
    await demo_dynamic_routing()

    print("\n--- 示例3: 循环工作流 ---")
    await demo_loop_workflow()

    print("\n--- 示例4: Checkpoint 断点续跑 ---")
    await demo_checkpoint_resume()

    print("\n--- 示例5: 模板实例化 ---")
    await demo_template_instantiation()

    print("\n--- 示例6: 多工作流编排 ---")
    await demo_orchestration()

    print("\n--- 示例7: DAG 可视化 ---")
    await demo_visualization()

    print("\n" + "=" * 60)
    print("所有示例运行完成!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
