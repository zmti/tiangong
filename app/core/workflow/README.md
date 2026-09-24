# 高级 DAG 工作流编排使用指南

## 概述

本模块提供了企业级 DAG 工作流编排能力，包括：
- 动态边路由（运行时根据变量决定执行路径）
- 增强型循环控制（foreach/while/do-until）
- 节点级 Checkpoint（断点续跑）
- SAGA 补偿事务（失败自动回滚）
- 工作流模板系统（可复用子图）
- 多工作流编排（链式/并行/依赖执行）
- DAG 可视化（供前端渲染）

## 核心组件

### 1. DynamicEdgeEvaluator - 动态边路由

```python
from app.core.workflow.dynamic_router import DynamicEdgeEvaluator

# 创建评估器
evaluator = DynamicEdgeEvaluator(
    variables={"status": "approved", "score": 85},
    node_results={"classify": {"output": {"sentiment": "positive"}}}
)

# 评估条件
is_active = evaluator.evaluate("{{classify.output.sentiment == 'positive'}}")

# 找出激活的目标节点
targets = evaluator.find_active_targets(
    source_node_id="classify",
    edges=[edge1, edge2, edge3]
)
```

### 2. LoopController - 增强循环控制

```python
from app.core.workflow.loop_controller import LoopController, LoopControlType

# Foreach 循环配置
loop_config = {
    "type": "foreach",
    "iterable": ["item1", "item2", "item3"],
    "max_iterations": 100,
    "batch_size": 1,
    "subgraph_ids": ["process_node"],
    "continue_on_error": False,
}

# While 循环配置
loop_config = {
    "type": "while",
    "while_condition": "{{counter < 10}}",
    "max_iterations": 100,
    "subgraph_ids": ["increment_node"],
}

# Do-Until 循环配置
loop_config = {
    "type": "do_until",
    "until_condition": "{{result.success}}",
    "max_iterations": 50,
    "subgraph_ids": ["retry_node"],
}

controller = LoopController(
    loop_config=loop_config,
    workflow_nodes=workflow.nodes,
    variables=run.variables,
    node_results=node_results,
    execute_node_func=execute_node,
    save_checkpoint_func=save_checkpoint,
)
result = await controller.execute(evaluator)
```

### 3. WorkflowCheckpointManager - Checkpoint 管理

```python
from app.core.workflow.checkpoint import WorkflowCheckpointManager

mgr = WorkflowCheckpointManager("/path/to/checkpoints.db")

# 保存检查点
mgr.save_checkpoint(
    run_id="run_123",
    node_id="node_1",
    status="success",
    variables={"key": "value"},
    result={"output": {"answer": "hello"}},
    duration=1.5,
)

# 获取可跳过的节点
skippable = mgr.get_skippable_nodes("run_123")

# 获取运行历史检查点
versions = mgr.get_checkpoint_versions("run_123", "node_1")

# 清理过期检查点
deleted = mgr.cleanup_old_checkpoints(max_age_hours=24)
```

### 4. SagaOrchestrator - SAGA 补偿

```python
from app.core.workflow.saga import SagaOrchestrator

saga = SagaOrchestrator()

# 注册正向操作和补偿操作
async def create_order(**kwargs):
    # 创建订单逻辑
    return {"order_id": "123", "status": "created"}

async def cancel_order(**kwargs):
    # 取消订单（补偿）
    order_id = kwargs.get("order_id")
    print(f"Canceling order: {order_id}")

saga.register(
    node_id="create_order",
    action=create_order,
    compensation=cancel_order,
    timeout=30.0,
)

# 执行
result = await saga.execute("create_order", item="product", qty=1)

# 如果后续节点失败，进行补偿
await saga.compensate_up_to("")
```

### 5. WorkflowTemplateRegistry - 模板系统

```python
from app.core.workflow.templates import get_template_registry

registry = get_template_registry()

# 列出所有模板
templates = registry.list_all()

# 搜索模板
results = registry.search(keyword="research")

# 实例化模板
workflow = registry.instantiate(
    template_id="research_report",
    instance_id="inst_001",
    inputs={"topic": "AI", "depth": 3}
)
```

### 6. WorkflowOrchestrator - 多工作流编排

```python
from app.core.workflow.orchestrator import get_orchestrator

orchestrator = get_orchestrator()

# 添加依赖关系
orchestrator.add_dependency("wf_2", ["wf_1"])
orchestrator.add_dependency("wf_3", ["wf_1", "wf_2"])

# 注册完成回调
orchestrator.on_complete("wf_1", lambda run: print(f"wf_1 completed"))

# 链式执行
results = await orchestrator.execute_chain(
    workflow_ids=["wf_1", "wf_2", "wf_3"],
    inputs={"query": "test"},
    session_id="sess_1"
)

# 并行执行
results = await orchestrator.execute_parallel(
    workflow_ids=["wf_a", "wf_b", "wf_c"],
    inputs={"data": "test"},
    max_concurrency=5
)
```

## API 端点

### DAG 可视化

```
GET /v1/workflows/{workflow_id}/visualize
```

返回 DAG 节点和边的布局数据，供前端渲染。

### 运行快照

```
GET /v1/workflows/{workflow_id}/runs/{run_id}/snapshot
```

返回运行实时状态，用于进度展示。

### 性能报告

```
GET /v1/workflows/{workflow_id}/runs/{run_id}/performance
```

返回性能统计数据（耗时、重试、并行度等）。

### 模板管理

```
GET  /v1/workflows/templates              # 列出所有模板
GET  /v1/workflows/templates/search?q=xx  # 搜索模板
GET  /v1/workflows/templates/{template_id} # 获取模板详情
POST /v1/workflows/templates/{template_id}/instantiate  # 实例化模板
```

### 工作流编排

```
POST /v1/workflows/orchestrate/chain         # 链式执行
POST /v1/workflows/orchestrate/parallel      # 并行执行
POST /v1/workflows/orchestrate/dependencies  # 按依赖执行
POST /v1/workflows/orchestrate/dependencies/add  # 添加依赖
GET  /v1/workflows/orchestrate/status        # 查看编排状态
```

### Checkpoint 管理

```
GET  /v1/workflows/{workflow_id}/runs/{run_id}/checkpoints  # 列出检查点
POST /v1/workflows/{workflow_id}/runs/{run_id}/resume       # 从检查点恢复
POST /v1/workflows/cleanup/checkpoints                      # 清理过期检查点
```

### SAGA 补偿

```
GET /v1/workflows/saga/status/{run_id}  # 查看补偿状态
```

## 工作流定义示例

### 带有动态路由的工作流

```json
{
  "id": "wf_dynamic_route",
  "name": "动态路由工作流",
  "nodes": [
    {"id": "start", "type": "start", "name": "开始"},
    {
      "id": "classify",
      "type": "agent",
      "name": "分类任务",
      "agent_config": {"input": "判断这个任务的类型"}
    },
    {
      "id": "simple_task",
      "type": "agent",
      "name": "简单任务处理",
      "agent_config": {"input": "处理简单任务"}
    },
    {
      "id": "complex_task",
      "type": "agent",
      "name": "复杂任务处理",
      "agent_config": {"input": "处理复杂任务"}
    },
    {"id": "end", "type": "end", "name": "结束"}
  ],
  "edges": [
    {"source": "start", "target": "classify"},
    {
      "source": "classify",
      "target": "simple_task",
      "condition_expr": "{{classify.output.answer contains '简单'}}"
    },
    {
      "source": "classify",
      "target": "complex_task",
      "condition_expr": "{{classify.output.answer contains '复杂'}}"
    },
    {"source": "simple_task", "target": "end"},
    {"source": "complex_task", "target": "end"}
  ],
  "entry_node": "start"
}
```

### 带有循环的工作流

```json
{
  "id": "wf_loop",
  "name": "批量处理工作流",
  "nodes": [
    {"id": "start", "type": "start", "name": "开始"},
    {
      "id": "batch_process",
      "type": "loop",
      "name": "批量处理",
      "loop_config": {
        "type": "foreach",
        "iterable": "${items}",
        "max_iterations": 50,
        "batch_size": 5,
        "subgraph_ids": ["process_item"]
      }
    },
    {"id": "process_item", "type": "agent", "name": "处理单条"},
    {"id": "end", "type": "end", "name": "结束"}
  ],
  "edges": [
    {"source": "start", "target": "batch_process"},
    {"source": "batch_process", "target": "end"}
  ],
  "entry_node": "start"
}
```

### 带错误恢复的工作流

```json
{
  "id": "wf_error_recovery",
  "name": "容错工作流",
  "nodes": [
    {"id": "start", "type": "start", "name": "开始"},
    {
      "id": "unreliable_api",
      "type": "http",
      "name": "调用外部API",
      "http_config": {"url": "https://api.example.com/data", "method": "GET"},
      "error_strategy": "retry",
      "retry_count": 3,
      "retry_delay": 1.0
    },
    {
      "id": "fallback",
      "type": "agent",
      "name": "备用方案",
      "agent_config": {"input": "使用备用数据源"}
    },
    {"id": "end", "type": "end", "name": "结束"}
  ],
  "edges": [
    {"source": "start", "target": "unreliable_api"},
    {"source": "unreliable_api", "target": "end"}
  ],
  "entry_node": "start"
}
```

## 内置模板

### research_report - 研究分析报告

```python
# 实例化
import requests
resp = requests.post(
    "http://localhost:8080/v1/workflows/templates/research_report/instantiate",
    json={"instance_id": "inst_001", "inputs": {"topic": "大语言模型", "depth": 3}}
)
workflow_id = resp.json()["id"]

# 执行
resp = requests.post(
    f"http://localhost:8080/v1/workflows/{workflow_id}/run",
    json={"inputs": {}}
)
```

### code_review - 代码审查

```python
resp = requests.post(
    "http://localhost:8080/v1/workflows/templates/code_review/instantiate",
    json={"instance_id": "inst_002", "inputs": {"code": "def hello(): pass", "language": "python"}}
)
```

### data_pipeline - 数据预处理管道

```python
resp = requests.post(
    "http://localhost:8080/v1/workflows/templates/data_pipeline/instantiate",
    json={"instance_id": "inst_003", "inputs": {"data_source": "data.csv"}}
)
```

### customer_service - 客服流程

```python
resp = requests.post(
    "http://localhost:8080/v1/workflows/templates/customer_service/instantiate",
    json={"instance_id": "inst_004", "inputs": {"customer_message": "我的产品无法连接到WiFi", "customer_id": "user123"}}
)
```

### content_generation - 内容生成

```python
resp = requests.post(
    "http://localhost:8080/v1/workflows/templates/content_generation/instantiate",
    json={"instance_id": "inst_005", "inputs": {"topic": "Python编程最佳实践", "content_type": "文章", "target_audience": "开发者"}}
)
```

### multi_step_form - 多步骤表单处理

```python
resp = requests.post(
    "http://localhost:8080/v1/workflows/templates/multi_step_form/instantiate",
    json={"instance_id": "inst_006", "inputs": {"form_data": {"username": "testuser", "email": "test@example.com"}, "form_type": "registration", "user_id": "user456"}}
)
```

## 最佳实践

### 1. 断点续跑策略

对于长时工作流，建议：
- 启用节点级 Checkpoint（默认开启）
- 设置合理的 `retry_count` 和 `retry_delay`
- 关键节点设置 `error_strategy: "fallback"`

### 2. 循环优化

- 使用 `batch_size` 批量处理，减少重复调用
- 设置 `max_iterations` 防止无限循环
- 在循环体中定期保存进度 checkpoint

### 3. 动态路由设计

- 条件表达式尽量简洁，避免复杂计算
- 使用 `safe_eval` 支持的安全操作符
- 为每个条件分支设置默认目标

### 4. SAGA 补偿

- 为每个有副作用的节点注册补偿操作
- 补偿操作应幂等（可重复执行）
- 记录补偿日志便于审计

## 架构对比

| 特性 | 原有实现 | 增强实现 |
|------|---------|---------|
| 边路由 | 静态表达式 | 动态模板 + 运行时评估 |
| 循环 | 固定次数迭代 | foreach/while/do-until |
| 故障恢复 | 整体重跑 | 节点级 checkpoint + SAGA 补偿 |
| 复用性 | 无 | 模板系统 + 实例化 |
| 编排 | 单工作流 | 链式/并行/依赖编排 |
| 可视化 | 无 | DAG 布局 + 运行快照 |
