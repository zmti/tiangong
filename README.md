# Agent Service 智能体服务

独立部署的通用对话式智能体，支持多模型 Provider、图运行时(DAG)编排与可持久化 Checkpoint、工具调用、RAG 文档问答、安全代码执行、流式输出、会话记忆、多智能体协作。

## 快速开始

### 1. 安装依赖

```bash
cd agent-service
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
```

### 2. 配置模型

复制 `.env.example` 为 `.env`，任选一个 Provider 填写：

| Provider | 配置项 | 说明 |
|----------|--------|------|
| DeepSeek | `AGENT_LLM_PROVIDER=deepseek` + `DEEPSEEK_API_KEY` | 国内，便宜 |
| Qwen | `AGENT_LLM_PROVIDER=qwen` + `QWEN_API_KEY` | 阿里云百炼 |
| Ollama | `AGENT_LLM_PROVIDER=ollama` | 本地免费，需装 Ollama |
| 任意 OpenAI 兼容 | `AGENT_LLM_PROVIDER=compat` + 三项 OPENAI_COMPAT_* | 通用 |

### 3. 启动服务

```bash
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 4. 测试接口

```bash
curl http://localhost:8080/health

# 对话(工具调用)
curl -X POST http://localhost:8080/v1/chat -H "Content-Type: application/json" \
  -d '{"message":"今天是几号?","session_id":"test1"}'

# 流式对话(SSE，浏览器 EventSource 兼容)
curl -N -X POST http://localhost:8080/v1/chat/stream -H "Content-Type: application/json" \
  -d '{"message":"计算 23*7 等于多少"}'

# 流式对话简化端点(支持 GET)
curl -N "http://localhost:8080/v1/stream?message=你好&session_id=s1"

# 上传文档建立知识库
curl -X POST http://localhost:8080/v1/documents/upload -F "file=@README.md"

# 查看知识库
curl http://localhost:8080/v1/documents/stats
```

### Docker 部署

生产环境推荐用 Docker 一键部署(多阶段镜像，含 faiss ANN 与真实 Embedding 支持)：

```bash
# 1. 配置环境变量(推荐)，secret 不进入镜像
set AGENT_LLM_PROVIDER=deepseek
set DEEPSEEK_API_KEY=sk-xxx
set EMBEDDING_BACKEND=api
set EMBEDDING_API_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
set EMBEDDING_API_KEY=sk-xxx
set EMBEDDING_API_MODEL=text-embedding-v4

# 2. 构建并启动
docker compose up -d --build

# 3. 验证
curl http://localhost:8080/health
```

- 数据(会话/知识库/记忆/图片/traces)持久化在 Docker volume `agent-data`
- 默认宿主端口 8080，可用 `AGENT_PORT` 覆盖
- 生产请务必设置 `AGENT_API_KEY`；`AGENT_TOOL_CONFIRM=true` 开启危险工具确认流

## 核心功能

### 图运行时与持久化
- **Graph/DAG 编排**：自研零依赖图引擎，Agent 由 `prepare → llm → tools/finalize` 节点组成；支持并行分支、条件边、动态扇出、interrupt/resume 中断恢复、时间旅行(版本回滚)
- **逐节点 Checkpoint**：每节点执行完把完整状态 upsert 到 `data/checkpoints.db`；支持 `Agent.resume(run_id)` 断点续跑、`Agent.checkpoints()` 查询记录、`Agent.versions(run_id)` 版本历史、`Agent.rollback(run_id, version)` 时间旅行

### 多模型协作
- **四种模式**：`auto`(默认跟随全局)/ `single`(单模型最快)/ `review`(执行+独立评审)/ `ensemble`(多模型并行+评审选优)
- **请求级覆盖**：`mode`/`critic_provider`/`critic_model`/`models` 字段按需指定
- **独立 critic 模型**：`AGENT_CRITIC_PROVIDER` 配置评审专用模型，避免自评偏袒

### 可靠性与质量
- **原生 Function Calling**：优先走 `tool_calls`(并行执行)，失败自动降级 JSON 文本解析
- **工具失败反思与自愈**：工具报错注入反思提示，连续失败工具自动禁用防死循环
- **反思循环(Reflexion)**：最终回答前 LLM 按 Schema 自评(1-10分+问题清单)，不合格回灌重写
- **最终答案事实校验**：`AGENT_VERIFY` 开启后用评审模型交叉核对，发现幻觉注入证据重写一次
- **计划失败重规划**：同一工具连续失败达阈值基于"原任务+失败原因"生成修订版计划
- **工具连续失败追踪**：同一工具+参数连续失败3次强制 finalize，防止死循环重试
- **工具结果去重缓存**：同运行内相同工具+参数直接复用成功结果
- **LLM 调用可靠性**：瞬态错误指数退避重试、主 provider 耗尽故障转移备用、连续失败熔断冷却

### 安全与护栏
- **API 鉴权**：`AGENT_API_KEY` Bearer token
- **危险工具确认流**：`AGENT_TOOL_CONFIRM=true` 需 `approve_tools` 获批；`AGENT_CONFIRM_INTERRUPT=true` 非阻塞 interrupt 式(暂停→`/v1/chat/resume` 批准/拒绝)
- **Guardrail**：输入 Prompt 注入/越权检测，输出密钥泄漏/系统提示词泄露脱敏
- **终端命令多层拦截**：shell元字符注入、系统文件重定向、解释器嵌套执行、base64/powershell 编码绕过检测
- **环境变量脱敏**：终端工具继承用户环境但剥离 API_KEY/SECRET/TOKEN 等敏感变量
- **文件锁**：防止多 Agent 并发修改同一文件冲突
- **审计日志**：`AGENT_AUDIT_LOG_ENABLED=true` 记录敏感操作/配置变更/数据访问，落盘 `data/audit.log`
- **多租户隔离**：租户级数据隔离、配额、配置继承，`X-Tenant-ID` 头传递

### 记忆与上下文
- **长期记忆(LTM)**：SQLite 持久化 global/session 两级，6个管理工具，可开启自动沉淀
- **记忆精炼**：相似记忆合并+用户画像提取，对话后自动运行
- **用户画像**：语言偏好/代码风格/擅长领域/技术栈持久化，自动注入系统提示
- **上下文智能裁剪**：Token 级预算(`agent_token_budget>0`)触发三层压缩(近期原文+中段关键事实+早期摘要)
- **子智能体独立上下文**：delegate/handoff 创建的子 Agent 显式收紧预算，不回流父级历史

### 多智能体协作
- **角色化团队**：6个默认角色(researcher/coder/code_reviewer/analyst/writer/security_reviewer)
- **委派与交接**：`handoff_to_role` 按角色委派(独立上下文、角色级系统提示与工具子集、深度限2层)
- **输出契约**：`expect` JSON Schema 约束子智能体结果，不满足返回 `contract_failed` 触发重试
- **编排模式**：`sequential`(流水线)/`hierarchical`(分工协作)/`consensual`(擂台赛)

### 工具系统
- `@tool` 装饰器注册、JSON Schema 参数校验、执行超时、启停控制
- **内置工具**：时间查询、数学计算、日期差、联网搜索(免费 DuckDuckGo 回退)、代码执行、知识库检索、长期记忆、文件操作、终端命令、图片 OCR、结构化代码编辑

### RAG 与搜索
- 真实 Embedding(OpenAI 兼容)或零依赖哈希嵌入
- faiss ANN 索引加速，向量+关键词 RRF 融合重排
- 回答附 chunk/doc_id 引用

### 可观测性
- **Trace 记录**：LLM 调用含 token/成本/耗时、工具执行、运行边界，落盘 `data/traces/<date>.jsonl`
- **查询接口**：`/v1/traces`/`/v1/traces/runs`/`/v1/traces/stats`(含缓存命中率)
- **OTLP 导出**：`OTEL_EXPORTER_OTLP_ENDPOINT` 指向 Langfuse/OTel Collector，不可达自动禁用
- **性能剖析**：`/v1/profiling/tools`(工具耗时排行)、`/v1/profiling/runs/{id}`(单次轨迹)、`/v1/profiling/daily`(日汇总)

### 评测与回归
- `python -m app.eval.runner` 跑真实 LLM 用例集，量化工具调用精度与任务完成率
- `--json` 落盘 + `--baseline` 回归门禁(CI 退出码 1=回归)
- `--judge` 启用 LLM-as-Judge 多维质量打分(correctness/faithfulness/helpfulness)

### 扩展能力
- **MCP Server 模式**：`/mcp/call`(JSON-RPC)、`/mcp/tools`、`/mcp/chat`、`/mcp/cost`，Claude Code/Cursor 可直接调用
- **成本追踪**：`cost_summary`/`cost_report`/`cost_daily_trend` 跨运行聚合
- **跨会话失败记忆**：自动记录工具失败模式注入下次会话
- **Webhook 回调**：`WEBHOOK_URLS` 配置后异步推送完成事件，支持 HMAC 签名
- **Prompt 缓存控制**：system 消息注入 `cache_control` 断点，命中厂商前缀缓存降本降延迟
- **Reasoning 模型适配**：解析 deepseek-reasoner/o1 等 `reasoning_content` 透出为 `thinking` 事件
- **模型路由/工具路由**：简单任务路由到轻量模型；每轮预选相关工具子集避免全量注入
- **桌面端**：Electron 打包，支持模型选择器、协作模式配置、自定义提供商管理

## 智能体行为开关(`.env`)

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `AGENT_NATIVE_TOOLS` | `true` | 启用原生 function calling |
| `AGENT_AUTO_MEMORY` | `false` | 对话结束自动沉淀事实进长期记忆 |
| `AGENT_MEMORY_AUTO_INJECT` | `true` | 每次对话自动注入相关长期记忆 |
| `AGENT_ROUTING_ENABLED` | `false` | 模型路由开关 |
| `AGENT_TOOL_ROUTER_ENABLED` | `true` | 工具路由开关(每轮预选相关工具子集) |
| `AGENT_TOOL_ROUTER_MAX_TOOLS` | `10` | 预选工具数上限(系统工具常驻) |
| `AGENT_LLM_RETRIES` | `2` | LLM 瞬态错误自动重试次数 |
| `AGENT_LLM_FAILOVER_PROVIDER` | 空 | 主 provider 失败后故障转移 |
| `AGENT_LLM_CIRCUIT_THRESHOLD` | `3` | 连续失败触发熔断(0=关闭) |
| `AGENT_LLM_CIRCUIT_COOLDOWN` | `60` | 熔断冷却时间(秒) |
| `AGENT_PROMPT_CACHE` | `true` | system 消息注入 cache_control 断点 |
| `AGENT_REASONING_ENABLED` | `true` | 解析推理模型 reasoning_content |
| `AGENT_REFLECT` | `true` | Reflexion 自评→重写循环 |
| `AGENT_REFLECT_MAX_ROUNDS` | `1` | 自评→重写最多轮数 |
| `AGENT_VERIFY` | `true` | 最终答案事实校验(LLM-as-judge) |
| `AGENT_TOOL_CACHE_SIZE` | `50` | 工具结果去重缓存条数(0=关闭) |
| `AGENT_CRITIC_PROVIDER` | 空 | 独立评审模型 provider |
| `AGENT_CRITIC_MODEL` | 空 | 评审模型 |
| `AGENT_MODE` | `auto` | 全局默认协作模式 |
| `AGENT_ENSEMBLE_MODELS` | 空 | ensemble 默认参与模型 |
| `AGENT_ENSEMBLE_MAX` | `3` | ensemble 模型数量上限 |
| `AGENT_REPLAN_ENABLED` | `true` | 工具连续失败触发重规划 |
| `AGENT_REPLAN_AFTER_FAILURES` | `2` | 触发 replan 的连续失败次数 |
| `AGENT_REPLAN_MAX_TIMES` | `2` | 单次运行最多重规划次数 |
| `AGENT_CHECKPOINT_ENABLED` | `true` | 图运行时逐节点 checkpoint |
| `AGENT_GRAPH_MAX_ITERS` | `300` | 运行最大图节点数 |
| `AGENT_TOKEN_BUDGET` | `0` | Token 级上下文预算(0=关闭) |
| `AGENT_SUBAGENT_CONTEXT_BUDGET` | `60000` | 子智能体独立上下文字符预算 |
| `AGENT_API_KEY` | 空 | 非空时所有 API 需 Bearer 鉴权 |
| `AGENT_TOOL_CONFIRM` | `false` | 危险工具需人工确认 |
| `AGENT_DANGEROUS_TOOLS` | `run_terminal,run_code` | 危险工具清单 |
| `AGENT_CONFIRM_INTERRUPT` | `true` | 危险工具 interrupt 式确认 |
| `AGENT_GUARDRAILS_ENABLED` | `true` | 输入/输出 Guardrail |
| `AGENT_SANDBOX_FORCE_DOCKER` | `false` | 强制沙箱使用 Docker 容器模式(生产建议开启) |
| `AGENT_AUDIT_LOG_ENABLED` | `false` | 启用审计日志记录敏感操作 |

## API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/v1/chat` | POST | 非流式对话(危险工具中断返回 `interrupted`/`interrupt`) |
| `/v1/chat/resume` | POST | 从 checkpoint 恢复(interrupt 批准/拒绝) |
| `/v1/chat/stream` | POST | 流式对话(SSE) |
| `/v1/stream` | GET/POST | 流式简化端点(query 参数) |
| `/v1/chat/history` | GET | 查看会话历史 |
| `/v1/chat/clear` | POST | 清空会话 |
| `/v1/memory` | GET | 查看长期记忆 |
| `/v1/tools` | GET | 查看已注册工具 |
| `/v1/documents/upload` | POST | 上传文档(form-data) |
| `/v1/documents/stats` | GET | 知识库统计 |
| `/v1/traces` | GET | 最近 trace 事件 |
| `/v1/traces/stats` | GET | 聚合统计(token/成本/缓存命中率) |
| `/v1/profiling/tools` | GET | 工具耗时排行 |
| `/v1/failure-memory` | GET | 跨会话失败记忆 |
| `/v1/webhook/status` | GET | Webhook 状态 |

## 生产化建议

- 会话存储替换为 Redis(`app/memory/session.py`)
- 向量库替换为 Milvus/pgvector
- 哈希嵌入替换为真实 Embedding API(`EMBEDDING_BACKEND=api`)
- 沙箱必须启用 Docker 容器模式
- 可观测性接入 Langfuse/OTel Collector(`OTEL_EXPORTER_OTLP_ENDPOINT`)
- Docker 化部署：`docker compose up -d --build`

## 近期新增/修复 (v0.5.x)

### 稳定性
- ✅ 工具连续失败追踪：同一工具+参数连续失败3次强制终止
- ✅ 流式截断检测：finish_reason追踪 + 工具参数JSON完整性校验
- ✅ 空答案兜底链：reasoning内容 → LLM补调 → 友好错误提示
- ✅ LLM流式修复：reasoning_content条件判断修正，原生流中途失败不再降级重发

### 安全
- ✅ 终端命令多层拦截：shell注入/系统文件重定向/解释器嵌套/base64绕过检测
- ✅ 环境变量脱敏：继承用户环境但剥离API_KEY/SECRET/TOKEN等敏感变量
- ✅ 原生调用校验：chat_with_tools callable检查，避免NoneType运行时错误

### 流式处理
- ✅ SSE队列管理：区分普通/终结事件，队列满时防阻塞，30s上限兜底
- ✅ 客户端断开取消：用户停止时正确取消后台agent任务
- ✅ thinking去重：流式模式下避免内容重复显示

### 工具路由
- ✅ 路由结果兜底：不足3个时自动补充核心工具(read_file/write_file/edit_file等)
- ✅ 错误引导增强：失败时指导LLM先确认路径文件内容再重试

### 工作流
- ✅ 变量模板：支持`{{node_id.output}}`引用节点输出
- ✅ 恢复输入复用：checkpoint恢复时保留上次输入，缺失字段才补新值

### 测试覆盖
- ✅ 新增6大类测试：流卡死、工具参数截断、流空闲超时、JSON解析健壮性、环境隔离、存储后端隔离、终端命令阻塞、SSE哨兵不丢、工具调用参数截断、工作流布局

## 扩展自定义工具

```python
# app/tools/my_tool.py
from pydantic import Field
from app.tools.registry import tool

@tool("query_weather", "根据城市查询天气", timeout=15)
def query_weather(city: str = Field(description="城市名"), days: int = Field(1, description="天数")) -> str:
    return {"city": city, "weather": "晴"}

# 在 app/tools/registry.py 的 get_registry() 中 import 该模块
```