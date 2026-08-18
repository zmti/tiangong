# Agent Service 智能体服务

独立部署的通用对话式智能体,支持多模型 Provider、图运行时(DAG)编排与可持久化 Checkpoint、工具调用、RAG 文档问答、安全代码执行、流式输出、会话记忆、多智能体协作。

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

复制 `.env.example` 为 `.env`,任选一个 Provider 填写:

| Provider | 配置项 | 说明 |
|----------|--------|------|
| DeepSeek | `AGENT_LLM_PROVIDER=deepseek` + `DEEPSEEK_API_KEY` | 国内,便宜 |
| Qwen | `AGENT_LLM_PROVIDER=qwen` + `QWEN_API_KEY` | 阿里云百炼 |
| Ollama | `AGENT_LLM_PROVIDER=ollama` | 本地免费,需装 Ollama |
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

# 流式对话(SSE,浏览器 EventSource 兼容)
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

生产环境推荐用 Docker 一键部署(多阶段镜像,含 faiss ANN 与真实 Embedding 支持):

```bash
# 1. 配置环境变量(推荐),secret 不进入镜像
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
- 默认宿主端口 8080,可用 `AGENT_PORT` 覆盖
- 生产请务必设置 `AGENT_API_KEY`;`AGENT_TOOL_CONFIRM=true` 开启危险工具确认流
- 也支持将完整配置写入 `.env` 后只挂载,或直接 build 镜像:

```bash
docker build -t agent-service .
docker run -d --name agent-service -p 8080:8080 \
  -v agent-data:/srv/agent-service/data \
  -e AGENT_API_KEY=xxx \
  agent-service
```

## 功能

- **多 Provider LLM**:DeepSeek / Qwen / Ollama / OpenAI 兼容,`.env` 一键切换
- **图运行时(Graph/DAG 编排)**:`app/runtime/` 自研零依赖图引擎,Agent 不再是手写 ReAct 循环,而是由 `prepare → llm → tools/finalize` 节点组成的图;支持并行分支(ParallelNode)、条件边、节点返回值覆盖静态路由;**interrupt/resume 中断恢复**(`Graph.interrupt` 暂停落盘、`Graph.aresume` 从原节点恢复)、**动态扇出 Send 语义**(节点返回 `[Send(...)]` 并发执行子分支)、**时间旅行**(checkpoint 按版本加载/分支回滚);`Graph.run` 与 `run_stream` 语义兼容原 `Agent.run/run_stream`,API 与既有测试不变
- **可持久化 Checkpoint**:每节点执行完把完整状态(消息/计划/步骤/路由指针)upsert 到 `data/checkpoints.db`;`Agent.resume(run_id)` 断点续跑、`Agent.checkpoints()` 查询运行记录、`Agent.versions(run_id)` 查看版本历史、`Agent.rollback(run_id, version)` 时间旅行回滚,支持后台任务与进程重启恢复
- **多智能体协作**:`app/agents/` 定义角色化团队(6 个默认角色:researcher/coder/code_reviewer/analyst/writer/security_reviewer);`list_roles` 查看团队、`handoff_to_role` 按角色委派(独立上下文、角色级系统提示与工具子集、可覆盖模型、委派深度限 2 层),角色子智能体复用同一图运行时;`delegate_task`/`handoff_to_role` 支持 `expect` 输出契约(JSON Schema):子智能体结果不满足契约时返回 `contract_failed`,主智能体据此重试或调整策略,防止"摘要式交差"
- **多模型协作模式(请求级 `mode`)**:`auto`(跟随全局开关,默认)/ `single`(单模型直接做,不做评审,最快最省)/ `review`(一个模型做、另一个模型测质量:强制开启最终答案事实校验 + 独立 critic 评审,评审模型可用请求级 `critic_provider`/`critic_model` 指定)/ `ensemble`(多个模型并行做同一任务,再用评审模型对全部回答打分选优,参与模型经 `models` 数组指定,如 `["deepseek:deepseek-chat","qwen:qwen-plus"]`,留空用全局 `AGENT_ENSEMBLE_MODELS`);`single`/`review` 属纯配置切换,`ensemble` 走独立的并行执行+评审编排
- **原生 Function Calling**:优先走原生 `tool_calls`(支持一次返回多个、并行执行),失败自动降级为 JSON 文本解析(ReAct)
- **并行工具调用**:一次推理返回多个工具请求,`asyncio.gather` 并发执行
- **工具失败反思**:工具报错时注入反思提示,LLM 可自我修正重试;连续失败 2 次的工具会被禁用以防死循环
- **反思循环(Reflexion 式)**:最终回答前 LLM 按结构化 schema 自评(1-10 分 + 问题清单),不合格时回灌评语要求重写(`AGENT_REFLECT`,最多 `AGENT_REFLECT_MAX_ROUNDS` 轮);结果记入 steps(`type=reflect`);支持 critic/executor 双角色——配置 `AGENT_CRITIC_PROVIDER` 用独立模型评审,避免自评偏袒
- **最终答案事实校验(LLM-as-judge)**:finalize 后用评审模型交叉核对"答案断言 vs 工具结果",发现幻觉/未支撑断言时注入证据重写一次再返回(`AGENT_VERIFY`),结果记入 steps(`type=verify`/`type=verify_rewrite`)
- **计划失败重规划**:同一工具连续失败达阈值时,基于"原始任务 + 失败原因"让 LLM 生成修订版计划替换旧计划(`AGENT_REPLAN_ENABLED`),而不是只注入反思提示
- **工具结果去重缓存**:同一运行内相同工具+相同参数直接复用上次成功结果,避免重复执行昂贵/确定性调用(`AGENT_TOOL_CACHE_SIZE`,对齐 Claude Code tool result caching)
- **LLM 调用可靠性**:瞬态错误(网络/限流/5xx)指数退避自动重试(`AGENT_LLM_RETRIES`);主 provider 重试耗尽自动故障转移到备用 provider(`AGENT_LLM_FAILOVER_PROVIDER`);连续失败达阈值触发熔断,冷却窗口内直接跳过故障 provider(`AGENT_LLM_CIRCUIT_THRESHOLD`/`AGENT_LLM_CIRCUIT_COOLDOWN`)
- **Prompt 缓存控制与观测**:system 消息注入 `cache_control` 断点,稳定前缀命中厂商自动缓存,降本降延迟(`AGENT_PROMPT_CACHE`);`/v1/traces/stats` 新增 `cache_hit_tokens`/`cache_hit_rate` 聚合,监控缓存命中率
- **Reasoning 模型适配**:解析 deepseek-reasoner / o1 等推理模型的 `reasoning_content` 思维链,透出为 `thinking` 事件而非混入正文(`AGENT_REASONING_ENABLED`)
- **严格结构化输出**:`chat_json_schema` 绑 JSON Schema + 校验回灌重试(非法输出把错误回喂模型修正)
- **工具系统**:`@tool` 装饰器注册、JSON Schema 参数校验、执行超时、启停控制
- **长期记忆(LTM)**:SQLite 持久化 global/session 两级记忆,`memory_save/search/update/forget/list/stats` 六个工具;可开启自动沉淀(`AGENT_AUTO_MEMORY=true`)
- **记忆精炼(MemoryConsolidator)**:`POST /v1/memory/consolidate` 触发相似记忆合并与用户画像提取,对标 Anthropic 工作记忆→长期记忆的提炼流;配置 `AGENT_CONSOLIDATE_ENABLED=true` 每次对话后自动运行
- **用户画像(UserProfiler)**:`app/memory/profile.py` 持久化语言偏好/代码风格/擅长领域/技术栈等;每次对话前自动注入为系统提示;支持 `GET/PUT/DELETE /v1/user_profile/*` 管理;配置 `AGENT_USER_PROFILE_ENABLED=true`(默认)
- **高级技能(SkillRegistry)**:`app/core/skills.py` 把多个工具链式组合成高级语义操作(如 `deploy_to_staging` = build→test→deploy);4个内置技能开箱即用;配置 `AGENT_SKILLS_ENABLED=true` 后技能描述注入系统提示
- **会话成本预算(SessionCostBudget)**:`app/core/budget.py` 按会话追踪 token 和美元成本,请求级可设 `budget_usd`/`budget_tokens` 硬上限,超预算自动中止;API: `GET/POST /v1/budget/*`
- **Embedding 缓存(EmbeddingCache)**:`app/rag/cache.py` 内存 dict + SQLite 文件两级缓存,避免相同文本重复调嵌入 API;TTL 默认24h;API: `GET /v1/embedding_cache/stats`
- **A/B 评估对比**:`app/eval/ab.py` 同一数据集上并行跑两组配置,输出通过率/成本/统计显著性;API: `POST /v1/eval/ab/run`, `GET /v1/eval/ab/report`
- **上下文智能裁剪与分层**:启发式 token 估算(中文≈1 token/字,ASCII≈4 字符/token),`agent_token_budget>0` 时按 token 级预算裁剪历史;超预算触发三层压缩(`layered_compress`):最近消息原文保留、中段消息由 LLM 抽结构化"关键事实"、最早消息合并为摘要——比单一摘要更贴近主流 agent 的上下文工程;字符预算(`context_char_budget`)继续作为默认兜底
- **子任务独立上下文强制化**:`delegate_task`/`handoff_to_role` 创建的子智能体显式收紧上下文预算(`AGENT_SUBAGENT_CONTEXT_BUDGET`),只拿到指令+背景+工具结果,不回流父级历史
- **computer-use 视觉驾驶**:`browser_drive` 工具循环"截图→多模态 LLM 决策→点击坐标/输入/滚动→再截图",像人类一样看着页面完成网页任务(配 `browser_screenshot` 等浏览器工具链)
- **模型路由**:简单问答与复杂任务(工具/RAG/多步推理)可路由到不同模型(`AGENT_ROUTING_ENABLED=true`)
- **工具路由**:每轮对话前由 LLM 从 100+ 工具中预选相关子集再注入,避免全量注入导致调用精度下降(BFCL tiered approach);路由失败自动回退全量,系统工具(`memory_*`/`set_plan`/`update_plan`)常驻不裁剪
- **子智能体**:`delegate_task` 工具把子任务委派给独立上下文的子 Agent(继承父模型/会话、可裁剪工具子集、深度限 2 层、只回传摘要);`delegate_parallel` 并行扇出多个互不依赖的子任务(map-reduce 式,一次最多 10 项,结果按序合并);角色化版本见 `handoff_to_role`;均支持 `expect` 输出契约,确保交回的结果可被主智能体可靠消费
- **可观测性**:内置 trace(JSONL 落盘 + 内存缓冲),`/v1/traces`、`/v1/traces/runs`、`/v1/traces/stats` 查询 token 用量/耗时/成本/错误;可配置 OTLP 端点转发到 Langfuse/OpenTelemetry Collector/自建网关(`OTEL_EXPORTER_OTLP_ENDPOINT`),端点不可达自动禁用不影响本地
- **性能剖析(Profiling)**:`/v1/profiling/tools` 工具耗时排行、`/v1/profiling/runs/{run_id}` 单次运行完整轨迹、`/v1/profiling/daily` 日汇总;自动记录每次 LLM 调用与工具执行的耗时/token/成本,帮助定位瓶颈
- **跨会话失败记忆(Self-Improvement)**:`app/memory/failure_memory.py`,自动记录每个工具的失败模式并聚合;新会话启动时检索相关失败经验注入 system prompt,格式 `[失败经验] 工具 xxx 曾失败 N 次,原因:..., 建议: ...`;API: `GET /v1/failure-memory`、`GET /v1/failure-memory/tools/{name}`、`POST /v1/failure-memory/hints/{id}`
- **任务完成 Webhook 回调**:`WEBHOOK_URLS` 配置一个或多个 HTTP 端点,对话完成/后台任务完成/确认流结束时异步推送事件;支持 HmacSHA256 签名(`WEBHOOK_SECRET`)和事件过滤(`WEBHOOK_EVENTS`);API: `GET /v1/webhook/status`
- **文件锁(File Locking)**:`app/core/file_lock.py`,防止多 Agent/进程同时修改同一文件导致冲突;支持超时自动释放和持久化恢复;API: `GET /v1/file-locks`、`POST /v1/file-locks/cleanup`
- **安全**:可选 `AGENT_API_KEY` Bearer 鉴权;`AGENT_TOOL_CONFIRM` 开启后危险工具(`run_terminal`/`run_code`)需请求 `approve_tools` 获批才执行,`AGENT_CONFIRM_INTERRUPT=true` 时改为非阻塞 interrupt 式确认(暂停→`/v1/chat/resume` 批准/拒绝后从原节点恢复);`AGENT_GUARDRAILS_ENABLED` 开启输入/输出 Guardrail(Prompt 注入、敏感路径越权、密钥泄漏、系统提示词泄露检测)
- **黄金用例回归**:`python -m app.eval.runner` 跑真实 LLM 用例集,量化工具调用精度与任务完成率,支持路由开/关对比、`--json` 落盘与 `--baseline` 回归门禁;`--judge` 启用 LLM-as-Judge 多维质量打分(correctness/faithfulness/helpfulness)
- **RAG 文档问答**:真实 Embedding(OpenAI 兼容 API)或零依赖哈希嵌入、faiss ANN 索引加速、向量+关键词 RRF 融合重排、回答附 chunk/doc_id 引用
- **免费网络搜索回退**:`web_search` 工具在配置 `SEARCH_API_KEY` 时使用 SerpAPI/Bing 付费搜索;未配置时自动降级到 DuckDuckGo 免费 HTML 搜索,无需任何 API Key 即可使用
- **结构化代码编辑(code_file)**:`read_file`/`edit_file`/`apply_patch`/`check_syntax`/`count_lines`/`find_functions`——补丁式精确替换(需原文匹配防误改)、unified diff 批量应用、语法检查(Python/JS/TS/Go/Rust 等)
- **成本追踪(cost_tracker)**:`cost_summary`/`cost_report`/`cost_daily_trend`——按会话/模型/天聚合 token 用量与美元估算,输出人类可读报告
- **MCP Server 模式**:`/mcp/call`(JSON-RPC)、`/mcp/tools`(列出工具)、`/mcp/chat`(对话简写)、`/mcp/cost`(成本查询);外部 MCP 客户端(Claude Code/Cursor 等)可直接调用本服务的 Agent 能力
- **图片 OCR(image_ocr)**:`image_ocr`/`screenshot_ocr`——优先使用 Tesseract 本地 OCR,无 Tesseract 时自动回退 LLM 多模态能力
- **安全代码执行**:沙箱隔离运行 Python 代码(参考 OpenHands 思路)
- **会话记忆**:按 session_id 隔离,消息条数限制,过期清理
- **流式输出**:SSE 实时返回推理/工具/回答事件
- **内置工具**:时间查询、数学计算、日期差、联网搜索、代码执行、知识库检索、长期记忆

### 多模型协作模式(mode)

请求级 `mode` 字段控制多个模型如何分工,选项:`auto`(默认,跟随全局配置)/ `single` / `review` / `ensemble`:

| mode | 行为 | 成本 | 适合 |
|------|------|------|------|
| `auto`(默认) | 跟随全局配置(AGENT_REFLECT/AGENT_VERIFY/AGENT_CRITIC_PROVIDER) | 由配置决定 | 无特殊要求 |
| `single` | 单模型直接做,不做评审/校验 | 最低 | 简单问答、求快求省 |
| `review` | 一个模型做,另一个模型测质量(强制开启 verify + 独立 critic) | 中(+2 次左右小调用) | 需要质量把关、防幻觉 |
| `ensemble` | 多个模型并行做同一任务,评审选优 | 最高(N 倍产出 + 1 次评审) | 精品文案/方案,追求上限 |

```bash
# review:一个模型做,另一个模型测质量
curl -X POST http://localhost:8080/v1/chat -H "Content-Type: application/json" \
  -d '{"message":"帮我写一段代码","mode":"review","critic_provider":"qwen","critic_model":"qwen-plus"}'

# ensemble:多模型并行 + 评审选优
curl -X POST http://localhost:8080/v1/chat -H "Content-Type: application/json" \
  -d '{"message":"写一段产品介绍","mode":"ensemble","models":["deepseek:deepseek-chat","qwen:qwen-plus"]}'
```

- `review` 评审模型:请求级 `critic_provider`/`critic_model` 优先,否则全局 `AGENT_CRITIC_PROVIDER`/`AGENT_CRITIC_MODEL`,再否则复用执行模型。
- `ensemble` 参与模型:请求级 `models` 数组优先,否则全局 `AGENT_ENSEMBLE_MODELS`(逗号分隔 `provider:model`),上限 `AGENT_ENSEMBLE_MAX`(默认 3);评审选优用 `critic_provider`/`critic_model`(未指定则主模型)。
- 结果记录在 steps:`verify`/`reflect`(review)、`ensemble_member`/`ensemble_judge`(ensemble)。

### 桌面端使用

输入栏模型选择器右侧新增**协作模式**下拉框:`自动` / `单模型` / `评审` / `多模型择优`(对应 `auto`/`single`/`review`/`ensemble`),选择即对本会话生效并持久化。

| 模式 | 含义 |
|------|------|
| 自动 | 跟随后端全局配置,默认 |
| 单模型 | 一个模型直接做,最快最省 |
| 评审 | 一个模型做、另一个模型测质量(强制开启事实校验 + 独立评审) |
| 多模型择优 | 多个模型并行做同一任务,评审选优 |

⚙️ **设置面板**可配置(本地持久化):
- **评审模型(critic)**:格式 `provider:model`,如 `qwen:qwen-plus`。用于评审/多模型择优模式的质量把关;留空则用后端全局 `AGENT_CRITIC_PROVIDER`。
- **多模型择优清单**:逗号分隔 `provider:model`,如 `deepseek:deepseek-chat,qwen:qwen-plus`。多模型择优模式并行参与的模型;留空则用后端全局 `AGENT_ENSEMBLE_MODELS`。

> 注:桌面端需要重新打包后端(含 `mode` 接口支持)与前端 UI 才生效,详见 `desktop/build.bat` / `build_app.ps1`。

### 智能体行为开关(`.env`)

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `AGENT_NATIVE_TOOLS` | `true` | 启用原生 function calling,关闭则强制 JSON 文本解析 |
| `AGENT_AUTO_MEMORY` | `false` | 对话结束后自动把事实沉淀进长期记忆 |
| `AGENT_MEMORY_AUTO_INJECT` | `true` | 每次对话自动注入相关长期记忆作为上下文 |
| `AGENT_SUMMARIZE_MAX_MESSAGES` | `20` | 超过该条数时触发摘要压缩 |
| `AGENT_SUMMARIZE_ENABLED` | `true` | 会话摘要开关 |
| `AGENT_SUMMARIZE_MODEL` | 空 | 指定摘要模型,默认复用主模型 |
| `AGENT_ROUTING_ENABLED` | `false` | 模型路由开关 |
| `AGENT_SIMPLE_MODEL` | 空 | 简单任务模型(路由开启时生效),默认复用主模型 |
| `AGENT_TOOL_ROUTER_ENABLED` | `true` | 工具路由开关(每轮预选相关工具子集) |
| `AGENT_TOOL_ROUTER_MAX_TOOLS` | `10` | 预选工具数上限(系统工具常驻不计入) |
| `AGENT_LLM_RETRIES` | `2` | LLM 瞬态错误自动重试次数(0=不重试) |
| `AGENT_LLM_RETRY_DELAY` | `0.5` | LLM 重试基础延迟(指数退避) |
| `AGENT_LLM_FAILOVER_PROVIDER` | 空 | 主 provider 失败后故障转移到的备用 provider |
| `AGENT_LLM_CIRCUIT_THRESHOLD` | `3` | 连续失败达到该次数触发 provider 熔断(0=关闭) |
| `AGENT_LLM_CIRCUIT_COOLDOWN` | `60` | 熔断冷却时间(秒),冷却后自动恢复尝试 |
| `AGENT_PROMPT_CACHE` | `true` | system 消息注入 cache_control 断点(命中厂商前缀缓存) |
| `AGENT_REASONING_ENABLED` | `true` | 解析推理模型 reasoning_content,透出 thinking 事件 |
| `AGENT_REFLECT` | `true` | Reflexion 式自评→重写循环 |
| `AGENT_REFLECT_MAX_ROUNDS` | `1` | 自评→重写最多轮数 |
| `AGENT_VERIFY` | `true` | 最终答案事实校验(LLM-as-judge,幻觉重写一次) |
| `AGENT_TOOL_CACHE_SIZE` | `50` | 工具结果去重缓存条数(0=关闭) |
| `AGENT_CRITIC_PROVIDER` | 空 | 独立评审模型 provider(critic/executor 双角色;留空=用执行模型自评) |
| `AGENT_CRITIC_MODEL` | 空 | 评审模型(留空=该 provider 默认) |
| `AGENT_MODE` | `auto` | 全局默认协作模式:`auto`/`single`/`review`/`ensemble`(请求级 `mode` 可覆盖) |
| `AGENT_ENSEMBLE_MODELS` | 空 | ensemble 默认参与模型(逗号分隔 `provider:model`,请求级 `models` 可覆盖) |
| `AGENT_ENSEMBLE_MAX` | `3` | ensemble 参与模型数量上限 |
| `AGENT_REPLAN_ENABLED` | `true` | 工具连续失败达阈值时重新规划计划 |
| `AGENT_REPLAN_AFTER_FAILURES` | `2` | 触发 replan 的连续失败次数 |
| `AGENT_REPLAN_MAX_TIMES` | `2` | 单次运行最多重新规划次数 |
| `AGENT_CHECKPOINT_ENABLED` | `true` | 图运行时逐节点 checkpoint(落盘 `data/checkpoints.db`,支持断点续跑) |
| `AGENT_GRAPH_MAX_ITERS` | `300` | 运行最大图节点数(兜底防死循环) |
| `AGENT_TOKEN_BUDGET` | `0` | Token 级上下文预算(0=关闭沿用字符预算;>0 用启发式 token 估算,超预算触发三层压缩) |
| `AGENT_SUBAGENT_CONTEXT_BUDGET` | `60000` | 子智能体(delegate/handoff)独立上下文字符预算 |
| `AGENT_TRACING_ENABLED` | `true` | 可观测性:记录 trace(JSONL + 内存) |
| `AGENT_API_KEY` | 空 | 非空时所有 API 需 `Authorization: Bearer <key>` |
| `AGENT_TOOL_CONFIRM` | `false` | 危险工具需请求 `approve_tools` 获批才执行 |
| `AGENT_DANGEROUS_TOOLS` | `run_terminal,run_code` | 危险工具清单(逗号分隔) |
| `AGENT_CONFIRM_INTERRUPT` | `true` | 危险工具确认改为非阻塞 interrupt 式(暂停→resume 批准/拒绝) |
| `AGENT_GUARDRAILS_ENABLED` | `true` | 输入/输出 Guardrail(注入/越权/密钥泄漏/提示词泄露检测) |
| `AGENT_CONSOLIDATE_ENABLED` | `false` | 对话结束后自动触发记忆精炼(合并相似记忆+提炼画像) |
| `AGENT_CONSOLIDATE_MIN_INTERVAL` | `3600` | 精炼最小间隔(秒),防止过于频繁 |
| `AGENT_USER_PROFILE_ENABLED` | `true` | 启用用户画像注入系统提示 |
| `AGENT_SKILLS_ENABLED` | `false` | 启用高级技能(可组合工具链)系统 |
| `AGENT_EMBEDDING_CACHE_ENABLED` | `true` | 嵌入向量内存+磁盘缓存 |
| `AGENT_EMBEDDING_CACHE_PERSISTENT` | `true` | 跨进程持久化嵌入缓存 |
| `AGENT_EMBEDDING_CACHE_MAX_SIZE` | `10000` | 缓存最大条目数 |
| `AGENT_EMBEDDING_CACHE_TTL` | `86400` | 缓存TTL(秒) |
| `AGENT_COST_BUDGET_ENABLED` | `true` | 开启会话级 token/成本追踪 |
| `WEBHOOK_URLS` | 空 | 任务/对话完成后异步推送 HTTP 回调的端点列表(逗号分隔);留空关闭 |
| `WEBHOOK_SECRET` | 空 | Webhook HmacSHA256 签名密钥;留空不签名 |
| `WEBHOOK_TIMEOUT` | `10` | Webhook 请求超时(秒) |
| `WEBHOOK_RETRIES` | `2` | Webhook 失败重试次数 |
| `WEBHOOK_EVENTS` | 空 | 过滤的事件类型(逗号分隔),留空=全部:`task_done,chat_done,confirmation_resolved` |

## 可观测性

每次 Agent 运行自动记录 trace 事件(LLM 调用含 token/成本估算、工具执行、运行边界),落盘 `data/traces/<date>.jsonl`:

```bash
curl http://localhost:8080/v1/traces            # 最近事件
curl http://localhost:8080/v1/traces/runs       # 最近运行摘要(每轮 LLM 调用数/token/耗时)
curl http://localhost:8080/v1/traces/stats      # 聚合:token 总量、缓存命中率(cache_hit_rate)、估算成本、错误数
```

## 安全

- **API 鉴权**:`.env` 设置 `AGENT_API_KEY` 后,所有 `/v1` 接口要求 `Authorization: Bearer <key>`;桌面本地部署留空即可。
- **危险工具确认流**:设置 `AGENT_TOOL_CONFIRM=true` 后,`run_terminal`/`run_code` 等危险工具在被调用时返回"需要用户确认",只有请求的 `approve_tools` 数组带上该工具名才执行。桌面端可在用户确认后带上获批工具重发请求。
- **interrupt 式确认(推荐)**:`AGENT_CONFIRM_INTERRUPT=true`(默认)时,危险工具命中后 Agent 不再阻塞轮询,而是通过 `Graph.interrupt` 暂停执行并持久化确认请求,`/v1/chat` 响应返回 `interrupted: true` + `interrupt` 载荷;调用方展示确认后调 `POST /v1/chat/resume {run_id, resume_value}` 批准(`[工具名,...]`)或拒绝(`{confirmation_id:{approved:false}}`),Agent 从原节点恢复继续。
- **Guardrail 护栏**:`AGENT_GUARDRAILS_ENABLED=true` 时,输入经 Prompt 注入/敏感路径越权检测,命中直接拦截;输出经密钥泄漏(自动脱敏)/系统提示词泄露检测,命中以脱敏文本返回。

## 黄金用例回归(eval)

`app/eval/` 内置一组针对真实注册器的用例,可量化工具调用精度与任务完成度:

```bash
python -m app.eval.runner                 # 跑全部用例(需真实 LLM + 联网)
python -m app.eval.runner calc,time       # 只跑指定用例
python -m app.eval.runner --category multi_agent   # 只跑某分类
AGENT_TOOL_ROUTER_ENABLED=false python -m app.eval.runner   # 对比关掉工具路由的准确率
python -m app.eval.runner --json out.json --baseline app/eval/baseline.json  # CI:落盘 + 回归门禁(退出码 1 = 回归)
python -m app.eval.runner --judge --judge-model deepseek-chat # LLM-as-Judge 质量打分(correctness/faithfulness/helpfulness)
```

内置 16 个用例,覆盖 11 个分类(basic/memory/web/browser/db/chart/file/code/rag/multi_agent/planning)。每个用例断言硬指标:`expect_tool`(应正确选中的工具)、`expect_keyword`(最终答案关键词),多步任务用 `tool_after` 断言"先调 A 再调 B";参考 BFCL 思路。`--judge` 额外用 LLM 对回答质量做多维打分(success/correctness/faithfulness/helpfulness),衡量"做得好不好"而非"有没有做";judge 模型不可用时自动降级为 None,不影响硬断言。新增用例直接编辑 `app/eval/cases.py`。仓库根 `.github/workflows/agent-service.yml` 提供 CI:单元测试 + 有 API Key 时跑 eval 回归对比基线。

## 图运行时与 Checkpoint(架构)

`app/runtime/` 是自研的零依赖图执行引擎,对标 LangGraph 抽象:

- `Graph` / `Node` / `ParallelNode` / `Send` / `END`:`add_node`/`add_edge`/`add_conditional_edge`/`set_entry` 声明式搭图,`node()` 装饰器直接注册节点;并行分支自动合并后继续;节点返回 `[Send(branch, payload), ...]` 时按 payload 并发执行子分支并把 steps/events 合并回主状态
- `Graph.interrupt(value)` / `aresume`:节点可调用 `graph.interrupt(state, value)` 暂停执行并落盘(状态置 `interrupted`、`node` 停留在原节点),`arun` 立即返回;后续 `aresume(state, value)` 回填 resume 值后重入原节点继续
- `AgentState`:`run_id`/`session_id`/`messages`/`steps`/`plan`/`node`/`status`/`interrupt`,完全 JSON 可序列化,是 checkpoint 的持久化载体;`node` 语义=最近成功节点的下一个节点(恢复起点)
- `CheckpointManager`:SQLite upsert(`data/checkpoints.db`),`save/load/list/delete/exists/count/clear`;每次 save 追加一份版本快照,`versions(run_id)` 查看历史、`load_version(run_id, version)` 时间旅行加载、`rollback(run_id, version)` 重置最新指针并分支;每节点边界自动保存

Agent 主循环即是这张图(`prepare → llm → tools|finalize|giveup`),计划、反思/自愈、interrupt 危险工具确认流、Guardrail、记忆注入、trace 都挂在节点回调上。断点续跑与时间旅行:

```python
from app.core.agent import Agent
a = Agent()
result = await a.run("帮我完成一个多步任务", session_id="s1")   # 正常运行
# 进程重启后:
result2 = await a.resume(run_id, new_input="继续")              # 从最后成功节点继续
runs = a.checkpoints(status="running")                          # 查询未完成运行
versions = a.versions(run_id)                                   # 时间旅行:查看版本历史
result3 = await a.rollback(run_id, version=2)                   # 回滚到版本 2 重新分叉执行
```

多智能体角色(`app/agents/`)与主 Agent 共享同一图运行时与 checkpoint 命名空间,`handoff_to_role` 委派的子任务也逐节点落盘、可单独续跑。

## 代码执行(沙箱)

智能体具备"写代码 → 执行 → 看结果 → 修正"的闭环能力。直接对话即可:

```text
用户:用 Python 计算 1 到 100 所有偶数的和
Agent:自动调用 run_code 工具 → 沙箱执行 → 返回结果并组织回答
```

### 沙箱隔离机制

参考 [OpenHands](https://github.com/OpenHands/OpenHands) 的沙箱思路,分两级:

| 模式 | 隔离强度 | 说明 |
|------|---------|------|
| **Docker 容器** | 强(推荐生产) | 一次性容器、内存 256MB、CPU 1 核、`--network none` 断网、PID 限制、超时自动 kill、`--rm` 无残留 |
| **subprocess 降级** | 弱(本地演示) | 独立临时目录、超时 kill、禁 PYTHONPATH 防导入业务代码 |

**自动降级**:启动时检测 Docker daemon,可用则用容器模式,否则自动降级 subprocess。

- 启用 Docker:启动 Docker Desktop,daemon 运行后自动生效
- 强制指定模式:在对话中让 Agent 传 `use_docker=true/false` 参数
- 自定义镜像:修改 `app/tools/code_sandbox.py` 中的 `SANDBOX_IMAGE`

### 能力边界

- 无网络访问(容器模式)
- 无法访问宿主机文件(容器模式)
- 输出截断 6000 字符
- 超时上限 60 秒
- 代码在独立临时目录执行,不污染业务环境

## API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/v1/chat` | POST | 非流式对话(危险工具确认中断时返回 `interrupted`/`interrupt` 字段) |
| `/v1/chat/resume` | POST | 从 checkpoint 恢复执行(interrupt 式确认的批准/拒绝,body 含 `run_id`/`resume_value`) |
| `/v1/chat/stream` | POST | 流式对话(SSE) |
| `/v1/stream` | GET/POST | 流式对话简化端点(query 参数,EventSource 兼容) |
| `/v1/chat/history?session_id=` | GET | 查看会话历史 |
| `/v1/chat/clear?session_id=` | POST | 清空会话 |
| `/v1/memory` | GET | 查看长期记忆 |
| `/v1/memory/clear` | POST | 清空长期记忆 |
| `/v1/tools` | GET | 查看已注册工具(含 run_code、memory_*) |
| `/v1/documents/upload` | POST | 上传文档(必须 form-data + File 类型) |
| `/v1/documents/stats` | GET | 知识库统计 |
| `/v1/documents/all` | DELETE | 清空知识库 |

## 知识库使用

上传文档后,**无需手动切换**,直接对话即可,Agent 会自动调用 `rag_search` 检索:

```text
用户:根据我上传的文档,XX 是什么?
Agent:自动检索知识库 → 基于检索内容回答
```

验证知识库是否生效:

```bash
# 1. 确认上传成功
curl http://localhost:8080/v1/documents/stats   # {"documents":1,"chunks":6,...}

# 2. 确认 rag_search 工具存在
curl http://localhost:8080/v1/tools

# 3. 实际提问
curl -X POST http://localhost:8080/v1/chat -H "Content-Type: application/json" \
  -d '{"message":"我上传的知识库里讲了什么?","session_id":"kb_test"}'
```

注意:
- 上传接口必须用 `multipart/form-data`,字段名 `file`,且类型为 **File**(Postman 中把 Text 切到 File)
- 重复上传同名文件会覆盖旧内容
- `EMBEDDING_BACKEND=api` 时使用真实 Embedding API(需配置 `EMBEDDING_API_BASE_URL/API_KEY/MODEL`,留空自动回退 openai_compat 或当前 LLM provider),语义检索显著优于哈希;切换后端会改变向量维度,需清空知识库/记忆重新写入
- 默认 `VECTOR_INDEX_BACKEND=auto`:安装 faiss 后自动用 ANN 索引加速(flat 精确 / hnsw 近似),未安装自动回退 SQLite 线性扫描;查询时返回字段含 `vector_score`/`keyword_score`/`rrf_score`
- 默认 `RERANK_MODE=rrf`:向量+关键词的 Reciprocal Rank Fusion 二次排序;`RERANK_MODE=api` 可接入外部 Rerank 模型(dashscope gte-rerank / Jina 等)

## 扩展自定义工具

在 `app/tools/` 下新建文件:

```python
from pydantic import Field
from app.tools.registry import tool

@tool("query_weather", "根据城市查询天气", timeout=15)
def query_weather(city: str = Field(description="城市名"), days: int = Field(1, description="天数")) -> str:
    return {"city": city, "weather": "晴"}

# 导入注册:在 app/tools/registry.py 的 get_registry() 中 import 该模块
```

## 生产化建议

- 会话存储替换为 Redis(`app/memory/session.py`)
- 向量库替换为 Milvus/pgvector(`app/rag/store.py` 当前为 faiss 内存索引 + SQLite 持久化,已有 faiss/hnsw 后端;量大可换分布式向量库)
- 哈希嵌入替换为真实 Embedding API(`EMBEDDING_BACKEND=api` + `EMBEDDING_API_*` 配置)
- 多租户隔离、审计日志
- 沙箱必须启用 Docker 容器模式,生产禁止 subprocess 降级
- 可观测性:已内置 OTLP 导出(`OTEL_EXPORTER_OTLP_ENDPOINT`),指向 Langfuse/OTel Collector 即可接入外部平台(字段已含 run_id/token/成本)
- Docker 化部署:`docker compose up -d --build`(Dockerfile + docker-compose.yml 已就绪,数据持久化在 volume)

## 已实现的安全/可观测性清单

- `AGENT_API_KEY` Bearer 鉴权(`app/api/security.py`)
- `AGENT_TOOL_CONFIRM` 危险工具人工确认流(`run_terminal`/`run_code`)
- 图运行时(DAG)编排 + 逐节点 checkpoint(`app/runtime/`,落盘 `data/checkpoints.db`,`Agent.resume`/`Agent.checkpoints`)
- 多智能体角色团队与交接(`app/agents/`,`list_roles`/`handoff_to_role`)
- 上下文 token 级预算(`app/memory/context.py`,`agent_token_budget`)
- trace 记录:`/v1/traces`、`/v1/traces/runs`、`/v1/traces/stats`
- OTLP 导出通道:`app/core/telemetry.py`(`OTEL_EXPORTER_OTLP_ENDPOINT`,端点不可达自动禁用)
- eval 回归门禁:`app/eval/runner.py --baseline` + CI workflow
- LLM-as-Judge 质量打分:`app/eval/runner.py --judge`(correctness/faithfulness/helpfulness)
- **MCP Server 端点**:`/mcp/call`(JSON-RPC)、`/mcp/tools`(工具列表)、`/mcp/chat`(对话)、`/mcp/cost`(成本)
- **成本追踪工具**:`cost_summary`/`cost_report`/`cost_daily_trend`(跨运行 token 用量与美元估算)
- **免费搜索回退**:`web_search` 无 API Key 时自动走 DuckDuckGo HTML 搜索
- **结构化代码编辑**:`read_file`/`edit_file`/`apply_patch`/`check_syntax`(补丁式精确替换+语法校验)
- **图片 OCR**:`image_ocr`/`screenshot_ocr`(Tesseract 优先,LLM 多模态回退)
- **性能剖析**:`/v1/profiling/tools`(工具耗时排行)、`/v1/profiling/runs/{id}`(单次运行轨迹)、`/v1/profiling/daily`(日汇总);数据落盘 `data/profiling.jsonl`
- **跨会话失败记忆**:`app/memory/failure_memory.py`,自动记录工具失败模式并注入下次会话;API:`GET /v1/failure-memory`、`GET /v1/failure-memory/tools/{name}`、`POST /v1/failure-memory/hints/{id}`
- **任务 Webhook 回调**:`WEBHOOK_URLS` 配置后,对话/后台任务完成异步推送 HTTP 通知;支持 HMAC 签名
- **文件锁**:`app/core/file_lock.py`,防止多 Agent 并发修改同一文件;API:`GET /v1/file-locks`、`POST /v1/file-locks/cleanup`
