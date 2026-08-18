# 智能体功能差距分析

> 对比 Claude Computer Use / OpenAI Assistants API / AutoGen / CrewAI / LangGraph
> 基于 `E:\dev\agent-service` 代码库的实际实现状态

---

## 一、已实现（对标主流的基础能力）

| 能力 | 实现状态 |
|------|---------|
| 图式编排引擎 (LangGraph-like) | ✅ 自研零依赖 Graph/DAG 运行时 |
| Checkpoint/断点续跑 | ✅ SQLite 持久化，支持 resume/rollback |
| 多 Provider LLM | ✅ DeepSeek / Qwen / Ollama / OpenAI-compatible |
| 并行 Tool Calling | ✅ 原生 function calling，一次调用多个工具 |
| RAG 混合检索 | ✅ 向量 + 关键词 + RRF 重排 |
| 分层记忆 | ✅ Working / Semantic / Episodic 三层 |
| 多智能体角色委派 | ✅ 6 个预设角色 (researcher/coder/reviewer/analyst/writer/security) |
| MCP 协议 | ✅ 可对接远程 MCP 服务 |
| 安全护栏 | ✅ 关键词 + 语义级注入/泄露检测 |
| 人类确认流 (Human-in-the-loop) | ✅ 危险工具 interrupt/resume |
| 反馈闭环 | ✅ Like/Dislike → 画像 → 记忆权重调整 |
| Prompt 自优化 | ✅ 版本管理 + A/B 测试 + 失败案例驱动 |
| Webhook 通知 | ✅ 持久化重试队列 + HMAC 签名 |
| 成本预算控制 | ✅ 会话级 token/美元双上限 |
| 代码沙箱 | ✅ Docker 隔离 + Subprocess 降级 |
| 浏览器自动化 | ✅ Playwright，支持截图/点击/导航 |
| 技能系统 (Skill) | ✅ 多步骤预定义流程，run_skill 工具 |
| 任务调度器 | ✅ cron/schedule 任务，文件持久化 |
| 速率限制 | ✅ 4 种策略 (固定/滑动窗口、令牌桶、漏桶) |
| 异步后台任务 | ✅ 可查询状态/取消/流式输出 |

---

## 二、与主流智能体的核心差距

### 差距 1: 无 Tool Streaming（最重要）
**现状**: 工具调用是"完整等待 → 一次性返回结果"模式
**主流**: Claude Computer Use / Cursor 的 tool_result 可以流式推送，LLM 能实时看到工具输出

```python
# 当前: 工具执行完后才给 LLM 完整结果
# 缺失: stream_tool_result 机制，让 LLM 边执行边看到中间输出

# 影响场景:
# - 终端命令输出几百行 → LLM 等全部读完才知道发生了什么
# - 代码编译过程 → 无法根据中间错误即时调整
# - 长文件读取 → 浪费上下文空间
```

### 差距 2: 无 Structured Output 强制校验
**现状**: `chat_json_schema` 使用 OpenAI 的 `response_format`，但部分 provider 不支持（如 Ollama）
**主流**: Anthropic 的 `tool_result` 天然结构化，OpenAI 的 `strict: true` 保证 schema 合规

```python
# 当前: 降级策略是 fallback 到普通 chat + 手动 JSON 解析
# 缺失: strict mode 在所有 provider 上的一致性保障

# 影响场景:
# - 自动任务规划输出非预期格式 → 整个流程崩掉
# - 多步骤技能的参数注入 → 类型错误静默发生
```

### 差距 3: 多智能体通信过于原始
**现状**: 仅通过 `handoff_tool` 单跳委派，子 Agent 返回结果给父 Agent
**主流**: CrewAI 的 Task → Agent 层次传递，AutoGen 的消息队列，typed agent-to-agent protocol

```python
# 当前: 只有一个 handoff_to_role 工具，结果以文本形式传回
# 缺失:
#   1. 多轮协商（子 Agent 向父 Agent 提问/澄清）
#   2. 结构化消息协议（task_id + status + data 分离）
#   3. 子 Agent 之间的横向通信（coder → reviewer 直接对话）
#   4. 委派任务的超时/优先级/并发控制

# 影响场景:
# - coder 写完代码后需要 reviewer 立刻介入 → 只能等父 Agent 决策
# - 无法实现 "先并行调研再综合结论" 的复杂编排
```

### 差距 4: 无 OpenAI Assistants API 兼容层
**现状**: 只有自定义 REST API (`/v1/chat`, `/v1/tasks`)
**主流**: OpenAI Assistants API (threads, messages, runs, tool_outputs) 已成为事实标准

```python
# 当前接口:
POST /v1/chat          # 单次对话
POST /v1/chat/stream   # 流式对话
POST /v1/tasks         # 后台任务

# 缺失的 OpenAI 兼容接口:
POST /v1/assistants    # 创建助手（带专属 system prompt + 工具集）
POST /v1/threads       # 创建对话线程
POST /v1/threads/{id}/messages   # 发送消息
POST /v1/threads/{id}/runs       # 执行/run
POST /v1/threads/{id}/runs/{id}/tool_outputs  # 提交工具结果

# 影响:
# - 无法被 LangChain/LlamaIndex/AutoGen 等框架直接接入
# - 前端必须自己实现 thread/message 管理逻辑
```

### 差距 5: 内存模型不够深
**现状**: SQLite + faiss（本地扁平索引），无分布式后端
**主流**: Pinecone / Weaviate / pgvector / Milvus，支持高维 ANN + 元数据过滤

```python
# 当前: VectorStore 完全本地，数据量超过百万条时检索变慢
# 缺失:
#   - pgvector (PostgreSQL 原生向量搜索，生产级)
#   - 分布式向量数据库集成
#   - 向量分 TTL 自动过期
#   - 多租户向量隔离

# 影响:
# - 知识库超过 10 万文档时 search() 明显变慢
# - 无法在 Kubernetes 多副本部署中共享向量索引
```

### 差距 6: 会话管理缺乏长期 Persistence
**现状**: 每次对话历史保存在 SQLite sessions 表，无自动清理/归档
**主流**: OpenAI Threads (自动 ttl)、Cursor (本地 SQLite + 自动压缩)

```python
# 当前: session 只存 messages，没有:
#   - 按时间自动归档冷数据到对象存储
#   - 会话级别的 token 预算管理
#   - 跨会话的主题聚类/自动分组
#   - 会话导出/导入（JSONL 格式）

# 影响:
# - 长时间使用后 sessions.db 膨胀
# - 无法"恢复上周的某个会话继续聊"
```

### 差距 7: 无文件/内容增量上传
**现状**: 每次上传整个文件，全量重新 embed
**主流**: Notion AI、Claude Projects 支持 incremental indexing

```python
# 当前: POST /v1/documents/upload 必须传完整文件
# 缺失:
#   - git diff 增量索引（只 embed 变更部分）
#   - 文件哈希对比，跳过未变更 chunk
#   - 大文件分片上传进度回调

# 影响:
# - 频繁修改的代码库每次重传都很慢
# - 不支持 WebDAV/SMB 等网络文件系统
```

### 差距 8: 调度器粒度粗（1 秒轮询）
**现状**: `_run_loop` 每 1 秒检查一次，cron 表达式解析但不精确
**主流**: APScheduler、Celery Beat，支持秒级/毫秒级精度

```python
# 当前调度器缺陷:
#   - 1 秒间隔，无法做 <1s 的定时任务
#   - cron 表达式只是字符串，无校验
#   - 任务失败后无 exponential backoff 重试
#   - 不支持任务依赖图（A 完成后触发 B）

# 影响:
# - 高频监控场景（每 500ms 检查一次状态）无法实现
# - 复杂工作流编排（A→B→C 依赖链）做不到
```

### 差距 9: 无真正的 Multi-Tenant 支持
**现状**: `session_id` 是字符串，无用户认证体系
**主流**: Claude Team、Cursor Teams、Vercel AI SDK 的多用户隔离

```python
# 当前: 所有 session 共享同一套工具/模型配置
# 缺失:
#   - 用户级 API Key 隔离
#   - 角色权限（reader/writer/admin）
#   - 用量统计 per-user
#   - 租户级 RAG 索引隔离

# 影响:
# - 无法作为 SaaS 产品对外提供多客户部署
# - 不同用户的知识文档互相可见
```

### 差距 10: 无 Observability Metrics 端点
**现状**: telemetry 只记录 trace 到内存 buffer
**主流**: OpenTelemetry metrics + Prometheus + Grafana

```python
# 当前: 只能查最近 10 条 trace
# 缺失:
#   - /metrics (Prometheus format): LLM latency p99, 工具调用成功率, 错误率
#   - /traces (OpenTelemetry OTLP): 完整 trace export
#   - 告警规则: 连续 3 次 LLM 失败 → 告警
#   - 性能热力图: 哪个工具最慢/最容易失败

# 影响:
# - 生产环境无法接入 Grafana 监控看板
# - 故障定位只能靠人工查日志
```

---

## 三、差距优先级排序

| 优先级 | 差距 | 工作量 | 收益 |
|--------|------|--------|------|
| P0 | Tool Streaming | 中 | 用户体验质变，LLM 实时感知工具输出 |
| P0 | Structured Output 一致性 | 小 | 可靠性大幅提升，减少静默错误 |
| P1 | Assistants API 兼容层 | 大 | 生态接入，可用 LangChain/AutoGen 等框架 |
| P1 | 多智能体消息协议 | 中 | 支持更复杂的编排模式 |
| P2 | 增量 RAG 索引 | 中 | 大知识库场景实用 |
| P2 | pgvector 后端 | 小 | 生产级向量搜索 |
| P2 | Observability Metrics | 中 | 运维能力 |
| P3 | Multi-Tenant | 大 | SaaS 化必需 |
| P3 | 调度器精细化 | 小 | 精确定时任务 |
| P3 | 会话长期存储 | 中 | 大规模使用场景 |

---

## 四、技术栈建议

### 立即可做的改进（P0/P1，工作量小）
1. **Tool Streaming**: 在 `tool_executor._execute_tool_once` 中增加 `yield` 输出，`state.emit({"type": "tool_chunk", "content": chunk})`
2. **Structured Output**: 增加 `strict_mode` 配置，对所有 provider 统一走 `response_format` fallback 校验
3. **Assistants API**: 新增 `app/api/assistants.py`，映射到现有 `/v1/chat` 接口

### 中期改进（P1/P2）
4. **多智能体消息**: 设计 `AgentMessage` 协议（task_id, sender, receiver, data, status）
5. **pgvector 后端**: 补充 `app/rag/store_pg.py` 的完整实现
6. **Observability**: 暴露 `/metrics` 端点，输出 Prometheus 格式

### 长期改进（P2/P3）
7. **Multi-Tenant**: 在 `session_id` 之上加 `tenant_id`，工具/模型配置租户级隔离
8. **增量 RAG**: 在 `index_document` 中增加 hash 对比，只 embed 新/改动的 chunk
