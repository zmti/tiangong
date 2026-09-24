# 高级 DAG 工作流编排实现总结

## 新增模块

### 1. `app/core/workflow/dynamic_router.py` - 动态边路由
- **DynamicEdgeEvaluator**: 运行时动态评估边条件
  - 支持 Jinja2 风格模板变量: `{{node_id.output.field}}`
  - 安全表达式求值 (基于 AST 解析)
  - 默认分支路由
- **RouteRule**: 路由规则定义

### 2. `app/core/workflow/loop_controller.py` - 增强循环控制
- **LoopController**: 三种循环模式
  - `FOREACH`: 遍历集合，支持批量处理
  - `WHILE`: 条件为真时继续
  - `DO_UNTIL`: 至少执行一次，直到条件满足
- 支持循环上下文变量 (`loop_index`, `loop_item`, `loop_batch`)
- 支持批量执行 (`batch_size`)
- 支持进度 Checkpoint
- 支持取消检测

### 3. `app/core/workflow/checkpoint.py` - 节点级 Checkpoint
- **WorkflowCheckpointManager**: SQLite 持久化
  - 每个节点执行完成后自动保存
  - 断点续跑: 跳过已成功节点
  - 版本历史: 支持时间旅行查看检查点历史
  - 自动清理: 过期检查点自动删除
- **NodeCheckpoint**: 检查点数据结构

### 4. `app/core/workflow/saga.py` - SAGA 补偿事务
- **SagaOrchestrator**: 补偿事务编排
  - 正向操作和补偿操作注册
  - 失败时自动逆序补偿
  - 部分补偿 (从失败点向前回滚)
  - 补偿失败容错 (记录日志，不中断)
- **CompensationRegistry**: 全局补偿注册表

### 5. `app/core/workflow/templates.py` - 工作流模板系统
- **WorkflowTemplate**: 可复用子图定义
  - 输入输出 Schema 定义
  - 模板变量替换
  - 实例化生成独立工作流
- **WorkflowTemplateRegistry**: 模板注册表
   - 内置模板: `research_report`, `code_review`, `data_pipeline`, `customer_service`, `content_generation`, `multi_step_form`
  - 搜索功能
  - 自定义模板注册

### 6. `app/core/workflow/orchestrator.py` - 多工作流编排
- **WorkflowOrchestrator**: 协调多个工作流执行
  - 链式执行: 前一个输出作为后一个输入
  - 并行执行: 多工作流并发执行
  - 依赖执行: 拓扑排序后执行
  - 完成回调
  - 最大并发控制

### 7. `app/core/workflow/visualization.py` - 可视化数据生成
- **generate_dag_visualization**: 生成 DAG 节点/边布局数据
  - 自动层次布局
  - 节点坐标计算
- **generate_execution_snapshot**: 运行实时快照
  - 进度统计
  - 时间线
- **generate_performance_report**: 性能报告
  - 平均耗时
  - 最慢节点
  - 重试统计
  - 并行度

### 8. `app/core/workflow/executor.py` - 增强型执行引擎
- 集成所有新功能
- 动态边路由
- 增强循环控制
- 节点级 Checkpoint
- SAGA 补偿
- 断点续跑

## 新增 API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/workflows/{id}/visualize` | GET | DAG 可视化数据 |
| `/v1/workflows/{id}/runs/{run_id}/snapshot` | GET | 运行快照 |
| `/v1/workflows/{id}/runs/{run_id}/performance` | GET | 性能报告 |
| `/v1/workflows/templates` | GET | 列出模板 |
| `/v1/workflows/templates/search` | GET | 搜索模板 |
| `/v1/workflows/templates/{tid}` | GET | 模板详情 |
| `/v1/workflows/templates/{tid}/instantiate` | POST | 实例化模板 |
| `/v1/workflows/orchestrate/chain` | POST | 链式执行 |
| `/v1/workflows/orchestrate/parallel` | POST | 并行执行 |
| `/v1/workflows/orchestrate/dependencies` | POST | 依赖执行 |
| `/v1/workflows/orchestrate/dependencies/add` | POST | 添加依赖 |
| `/v1/workflows/orchestrate/status` | GET | 编排状态 |
| `/v1/workflows/{id}/runs/{run_id}/checkpoints` | GET | 检查点列表 |
| `/v1/workflows/{id}/runs/{run_id}/resume` | POST | 从检查点恢复 |
| `/v1/workflows/saga/status/{run_id}` | GET | SAGA 状态 |
| `/v1/workflows/cleanup/checkpoints` | POST | 清理检查点 |

## 使用示例

```python
# 1. 从模板创���工作流
import requests
resp = requests.post(
    "http://localhost:8080/v1/workflows/templates/research_report/instantiate",
    json={"instance_id": "inst_001", "inputs": {"topic": "AI"}}
)
wf_id = resp.json()["id"]

# 2. 获取 DAG 可视化数据
viz = requests.get(f"http://localhost:8080/v1/workflows/{wf_id}/visualize").json()

# 3. 执行工作流
run = requests.post(
    f"http://localhost:8080/v1/workflows/{wf_id}/run",
    json={"inputs": {}}
).json()

# 4. 获取运行快照
snapshot = requests.get(
    f"http://localhost:8080/v1/workflows/{wf_id}/runs/{run['run_id']}/snapshot"
).json()

# 5. 获取性能报告
perf = requests.get(
    f"http://localhost:8080/v1/workflows/{wf_id}/runs/{run['run_id']}/performance"
).json()

# 6. 编排多个工作流
orch = requests.post(
    "http://localhost:8080/v1/workflows/orchestrate/chain",
    json={"workflow_ids": ["wf_1", "wf_2", "wf_3"], "inputs": {}}
).json()
```

## 文件清单

```
app/core/workflow/
├── __init__.py          # 模块入口
├── models.py            # 数据模型 (原有，未修改)
├── executor.py          # 增强执行引擎 (重写)
├── dynamic_router.py    # 动态边路由 (新增)
├── loop_controller.py   # 循环控制器 (新增)
├── checkpoint.py        # Checkpoint 管理 (新增)
├── saga.py              # SAGA 补偿 (新增)
├── templates.py         # 模板系统 (新增)
├── orchestrator.py      # 编排器 (新增)
├── visualization.py     # 可视化 (新增)
├── README.md            # 使用文档 (新增)
└── examples.py          # 快速开始示例 (新增)

app/tests/
└── test_workflow_advanced.py  # 单元测试 (新增)

app/api/workflow.py      # API 端点 (扩展)
```

## 架构演进

```
                    原有架构                      增强架构
    ┌─────────────────────────────┐      ┌─────────────────────────────┐
    │        WorkflowExecutor     │      │      WorkflowExecutor       │
    │  • 静态条件边               │      │  • 动态边路由                │
    │  • 固定次数循环             │      │  • 增强循环控制              │
    │  • 整体重跑                 │      │  • 节点级 Checkpoint         │
    │  • 无补偿                   │      │  • SAGA 补偿                 │
    └─────────────────────────────┘      │  • 模板系统                  │
    │                                   │  • 多工作流编排              │
    │                                   │  • 可视化支持                │
    └─────────────────────────────┘      └─────────────────────────────┘
```

## 向后兼容性

- 所有原有 API 端点保持不变
- 原有工作流定义格式完全兼容
- 新功能通过新端点暴露，不影响现有功能
- 工作流自动启用新特性（如 Checkpoint 自动保存）

## 下一步计划

1. **前端集成**: 提供 DAG 可视化编辑器组件
2. **工作流编辑器**: Web IDE 支持拖拽创建
3. **更多内置模板**: 覆盖常见业务场景
4. **性能优化**: 批量 Checkpoint 写入
5. **监控告警**: 工作流失败自动通知
