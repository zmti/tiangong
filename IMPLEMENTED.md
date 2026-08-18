## 五、全部已实现改进（2025-07）

### ✅ P0: Tool Streaming（工具流式输出）
**实现文件**: `app/config.py`, `app/core/tool_executor.py`
**新增配置**:
- `AGENT_TOOL_STREAMING_ENABLED=True` — 开启工具流式输出
- `AGENT_TOOL_STREAMING_CHUNK_SIZE=1024` — 单次 chunk 字节上限

**行为变化**: 流式模式下，每个工具执行时按 chunk_size 分块推送 `tool_chunk` 事件（含 `tool_start`/`tool_done` 边界事件），LLM 可实时感知进度。非流式模式完全不受影响。

### ✅ P0: Structured Output 一致性
**实现文件**: `app/core/llm.py`
**新增配置**:
- `AGENT_STRUCTURED_OUTPUT_STRICT=True` — 强制 schema 校验
- `AGENT_STRUCTURED_OUTPUT_MAX_RETRIES=3` — 最大重试次数

**行为变化**: `chat_json_schema` 现在在所有 provider 上强制执行：
1. 优先尝试 `json_schema` + `strict: true`
2. 降级到 `json_object`
3. 应用层校验 `required` 字段
4. 校验失败时回灌错误并重试（最多 N 次）

### ✅ P1: OpenAI Assistants API 兼容层
**实现文件**: `app/api/assistants.py`（新建，442 行）
**注册路由**:
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/assistants` | 创建助手 |
| GET | `/v1/assistants` | 列表 |
| GET | `/v1/assistants/{id}` | 查看 |
| POST | `/v1/threads` | 创建对话线程 |
| POST | `/v1/threads/{id}/messages` | 发送消息 |
| GET | `/v1/threads/{id}/messages` | 获取消息列表 |
| POST | `/v1/threads/{id}/runs` | 执行 run |
| GET | `/v1/threads/{id}/runs` | 列表 runs |
| GET | `/v1/threads/{id}/runs/{run_id}` | 查看 run |
| POST | `/v1/threads/{id}/runs/{run_id}/tool_outputs` | 提交工具结果 |

底层复用现有 Agent + Chat 接口，assistant 配置持久化到 SQLite。

### ✅ P1: 多智能体结构化消息协议
**实现文件**: `app/agents/message_protocol.py`（新建，137 行）
**核心能力**:
- `AgentMessage` 数据类：携带 sender/receiver/task_id/status/content/result/error 元数据
- `AgentMessageQueue`：支持 task_id 等待、complete 通知、广播
- `typed_handoff` 工具：使用结构化协议委派任务，返回可等待的 task_id
- `get_task_result` 工具：查询任务执行结果和状态
- 与现有 `handoff_to_role` 共存，新工具不破坏已有行为

**新增配置**:
- `AGENT_TYPED_MESSAGES_ENABLED=True` — 开启结构化消息
- `AGENT_MAX_CONCURRENT_DELEGATES=5` — 并发委派上限

### ✅ P2: 增量 RAG 索引
**实现文件**: `app/rag/documents.py`
**机制**: 每次上传时计算文件 SHA-256 hash，持久化到 `data/doc_hashes.db`。同名文件内容未变则跳过 re-embed，直接返回 cached 标记。

**API 响应变化**:
```json
{
  "doc_id": "...", "filename": "report.md", "chunks": 12,
  "hash": "a3f2b8c1", "cached": false
}
// 重复上传相同内容:
{
  "doc_id": "...", "filename": "report.md", "chunks": 0,
  "note": "内容未变更,跳过索引", "cached": true
}
```

### ✅ P2: pgvector 生产级增强
**实现文件**: `app/rag/store_pg.py`
**新增能力**:
- `search(tenant_id=...)` — 多租户向量隔离过滤
- `search(exclude_expired=True)` — TTL 过期自动过滤
- `add(metadata={"ttl_seconds": 86400})` — chunk 级 TTL 设置
- `set_ttl(doc_id, seconds)` — 整文档 TTL 批量设置
- `cleanup_expired()` — 手动清理过期 chunk

### ✅ P2: Observability Metrics（Prometheus）
**实现文件**: `app/api/metrics.py`（新建，145 行）
**端点**:
- `GET /v1/metrics?format=prometheus` — Prometheus text format（被 scrape）
- `GET /v1/metrics?format=json` — 结构化 JSON
- `GET /v1/metrics/json` — 快捷 JSON 端点

**指标类型**:
- `agent_llm_calls_total` — 累计 LLM 调用次数
- `agent_tool_calls_total` — 累计工具调用次数
- `agent_llm_errors_total` — LLM 错误次数
- `agent_total_tokens` — prompt/completion/total token
- `agent_cache_hit_rate` — embedding 缓存命中率
- `agent_cost_usd_total` — 累计成本
- `agent_rag_chunks_total` — 向量库总 chunk 数
- `agent_embedding_cache_size` — 缓存条目数
- `agent_llm_calls_per_min` — 1 分钟 LLM 调用速率
- `agent_active_tasks_total` — 当前活跃任务数

### ✅ P3: 调度器精细化
**实现文件**: `app/core/scheduler.py`
**新增能力**:
- `depends_on=["task_id_1", "task_id_2"]` — 前置任务依赖链
- `retry_max=3` — 失败后指数退避重试（基础 2s，最多 5 次）
- 轮询精度提升至 0.1s（原 1s）
- `_exec_log` — 最近 10 条执行记录（状态/耗时/错误）

### ✅ P3: 会话长期存储
**实现文件**: `app/core/session_archive.py`（新建，115 行）
**API**:
- `POST /v1/sessions/{id}/archive` — 触发归档（超阈值自动压缩）
- `GET /v1/sessions/{id}/export` — JSONL 导出
- `GET /v1/sessions/{id}/export/download` — 下载导出文件
- `POST /v1/sessions/{id}/import?file_path=...` — JSONL 导入
- `GET /v1/sessions/archives` — 列出归档
- `POST /v1/sessions/auto-cleanup?force=true` — 触发自动清理(归档大会话 + 删除过期)
- `GET /v1/sessions/auto-cleanup/status` — 查看清理状态
- 后台每 3600s 自动执行清理循环

### ✅ M5: RAG 多路召回
**实现文件**: `app/rag/multi_retriever.py`（新建）、`app/rag/retriever.py`（增强）
**三路召回**:
1. **向量召回**: 现有 embedder + store.search (faiss/pgvector)
2. **BM25 召回**: 本地词频统计,精确匹配专业术语/代码片段
3. **查询扩展召回**: 同义改写 + 子问题拆分(可配置 LLM 扩展)
**RRF 融合**: 三路结果按倒数排名融合,跨路去重
**元数据过滤**: `filters={"source": "xxx", "tenant_id": "yyy"}` 支持
**新增配置**:
- `RAG_MULTI_PATH_ENABLED=True` — 开启多路召回
- `RAG_QUERY_EXPAND_LLM=False` — 是否用 LLM 生成扩展查询
- `RAG_BM25_ENABLED=True` — 是否启用 BM25 召回

### ✅ M5: 长时自主循环
**实现文件**: `app/core/long_running.py`（新建，300 行）、`app/api/long_running.py`（新建）
**核心能力**:
- `POST /v1/long-running` — 创建任务(最长 1440 分钟,默认 120)
- `GET /v1/long-running` — 列表 + 统计
- `GET /v1/long-running/{id}` — 详情
- `POST /v1/long-running/{id}/cancel` — 取消
- `DELETE /v1/long-running/{id}` — 删除
- 后台 asyncio 循环(2s 轮询),并发上限可配
- 独立 session_id,消息落盘到 session store
- 超时自动失败 + 指数退避重试
- 完整进度追踪(progress dict)
**新增配置**:
- `AGENT_MAX_CONCURRENT_LONG_RUNNING=3` — 最大并发数
- `AGENT_LONG_RUNNING_DEFAULT_MINUTES=120` — 默认时长
- `AGENT_LONG_RUNNING_CHECKPOINT_INTERVAL=10` — checkpoint 间隔

### ✅ M5: 分层记忆深度
**实现文件**: `app/memory/decay.py`（增强）、`app/core/memory_manager.py`（增强）
**增强内容**:
- `auto_dedup_and_evict()`: 保存前自动检查语义相似性(阈值 0.85),命中则合并内容而非重复存储
- TTL 自动驱逐: 每次保存前扫描即将过期的记忆(时间衰减 < min_score),自动删除
- MemoryDecay 参数接入: `LTM_TTL_HOURS` / `LTM_DECAY_RATE` / `LTM_MIN_SCORE`
- MemoryCluster.merge_similar() 保持可用(手动合并入口)
**效果**: 避免相同事实重复存储、自动淘汰过时记忆、降低 memory.db 膨胀

**新增配置**:
- `SESSION_ARCHIVE_CHARS=50000` — 触发归档的字符数阈值
- `SESSION_RETENTION_DAYS=90` — 归档保留天数

### ✅ P3: Multi-Tenant 完整支持
**实现文件**: `app/core/tenant.py`（新建，280 行）、`app/core/tenant_context.py`（新建，55 行）、`app/api/tenants.py`（新建，180 行）
**核心能力**:
- API Key → 租户绑定: SHA-256 哈希存储，验证时返回 tenant_id + role
- 数据隔离: sessions / memories / vectors 三表均加 `tenant_id` 列，默认 `'default'`（向后兼容）
- RBAC: admin（管理+用量查看）/ user（正常使用）/ readonly（仅查询）
- 用量统计: 每租户独立 llm_calls / token / cost_usd，支持按月重置
- 上下文注入: `TenantContext` contextvar，所有存储操作自动过滤当前租户

**API 端点**:
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/tenants` | 列表（admin） |
| POST | `/v1/tenants` | 创建租户（admin） |
| GET | `/v1/tenants/{id}` | 详情+用量 |
| PUT | `/v1/tenants/{id}` | 更新（admin） |
| DELETE | `/v1/tenants/{id}` | 删除（admin） |
| GET | `/v1/tenants/{id}/keys` | 列出 API Keys |
| POST | `/v1/tenants/{id}/keys` | 生成新 Key（返回明文，仅一次） |
| DELETE | `/v1/tenants/{id}/keys/{prefix}` | 吊销 Key |
| GET | `/v1/tenants/{id}/usage` | 用量统计 |
| POST | `/v1/tenants/{id}/usage/reset` | 重置用量 |
| GET | `/v1/tenants/me` | 当前请求租户信息 |

**认证模式切换**:
- `AGENT_API_KEY` 非空 → 单 Key 模式（向后兼容，原有行为不变）
- `DEFAULT_TENANT_ENABLED=true` → 多租户模式，每个 Key 归属一个租户
- `TENANT_FALLBACK_TO_SINGLE_KEY=true` → 多租户模式下未匹配 Key 时回退单 Key
