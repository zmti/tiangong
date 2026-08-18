# Agent Service 智能体系统 — 深度分析报告

> 分析范围：`E:\dev\agent-service` 全量源码（104 个 Python 文件，~12,000 行核心逻辑）
> 分析日期：2026-07

---

## 一、系统架构全景

```
用户请求 (HTTP / SSE / MCP)
        │
        ▼
  ┌─────────────┐
  │  API 层     │  FastAPI 路由（/v1/chat, /v1/chat/stream, /v1/tasks…）
  │  chat.py    │
  └──────┬──────┘
         │
  ┌──────▼──────┐
  │  Agent 核心 │  app/core/agent.py（~1900 行）
  │  graph.run  │  Graph/DAG 驱动，节点：prepare→llm→tools→finalize→END
  └──────┬──────┘
         │
  ┌──────▼──────┐      ┌──────────────┐   ┌────────────────┐
  │ LLM Client  │      │ Graph Engine │   │ Tool Registry  │
  │ llm.py      │      │ runtime/     │   │ tools/registry │
  │ ·多Provider │      │ ·checkpoint  │   │ ·装饰器注册    │
  │ ·重试/熔断  │      │ ·interrupt   │   │ ·工具路由      │
  │ ·结构化输出 │      │ ·Send 并发   │   │ ·参数校验      │
  └──────┬──────┘      └──────────────┘   └────────────────┘
         │
  ┌──────▼──────┐      ┌──────────────┐   ┌────────────────┐
  │ Memory 层   │      │ RAG 层       │   │ Multi-Agent    │
  │ session.py  │      │ rag/         │   │ agents/        │
  │ ltm.py      │      │ ·嵌入/索引   │   │ ·角色分工      │
  │ layers.py   │      │ ·混合检索    │   │ ·handoff 委派  │
  └─────────────┘      └──────────────┘   └────────────────┘
```

---

## 二、核心优势（技术亮点）

### 2.1 图运行时（Graph/DAG 编排）— 行业领先的架构选型

| 能力 | 说明 |
|------|------|
| **零依赖自研引擎** | 对标 LangGraph，但无第三方依赖（不依赖 langgraph/langchain），部署轻量 |
| **节点级 Checkpoint** | 每个节点边界落盘 SQLite，支持进程重启断点续跑（`Agent.resume()`） |
| **interrupt/resume 语义** | 对齐 LangGraph interrupt 模型：危险工具执行前暂停→持久化→外部批准→原节点恢复，**不阻塞 HTTP 请求** |
| **动态扇出 Send** | 节点返回 `[Send(branch, payload), …]` 实现并行子分支，合并后继续主流程 |
| **条件边 + 时间旅行** | `add_conditional_edge` 实现路由；`versions(run_id)` + `rollback` 支持版本历史回滚 |

**优势本质**：这不是简单的 ReAct 循环，而是把 Agent 运行建模为有向无环图（DAG），每一步都是可观测、可恢复、可重放的独立节点。这是生产级 Agent 的基础设施水位线。

---

### 2.2 多 Provider LLM 接入 — 高可用与成本优化的工程实践

```
主 Provider ──→ 重试 (AGENT_LLM_RETRIES=2, 指数退避)
    │
    ├── 连续失败 ≥ 阈值 ──→ 熔断 (CIRCUIT_BREAKER)
    │                           冷却期内自动跳过，不拖慢主线程
    │
    └── 故障转移 ──→ 备用 Provider (AGENT_LLM_FAILOVER_PROVIDER)
                         自动切换到备用模型
```

**亮点**：
- **Prompt Cache 断点注入**：system 消息自动加 `cache_control: ephemeral`，命中 DeepSeek/Qwen 自动缓存，降本降延迟
- **Reasoning 模型适配**：deepseek-reasoner / o1 / o3 的 `reasoning_content` 自动分离为 `thinking` 事件透出
- **结构化输出 + 校验回灌**：`chat_json_schema` 支持 JSON Schema 强校验，失败自动重试，解决 LLM 输出不稳定的核心痛点
- **成本追踪**：每次 LLM 调用记录真实 usage（含缓存命中 token），跨会话聚合出成本报告

---

### 2.3 三层上下文工程 — 长对话的关键保障

| 层级 | 策略 | 作用 |
|------|------|------|
| **Token 预算** | 启发式估算（CJK 1 token/字，ASCII 4 char/token） | 精确控制 LLM 输入大小 |
| **渐进摘要** | 最早 40% 合并为摘要，中段 60% 抽结构化事实，最近 N 条原文保留 | 避免信息丢失比粗暴截断更优 |
| **优先级感知裁剪** | user/assistant 消息优先，工具中间结果超长可截断 | 保留决策链路关键信息 |
| **输出 Token 硬限制** | `max_output_tokens` 请求级配额，超限截断并附提示 | 防止单条回复撑爆预算 |

---

### 2.4 多智能体协作 — 角色化团队与契约化委派

- **6 个开箱即用角色**：researcher / coder / code_reviewer / analyst / writer / security_reviewer，各有专属工具子集和系统提示
- **Handoff 委派**：`handoff_to_role` 按角色派发子智能体，独立上下文、独立模型覆盖、深度限 2 层防嵌套爆炸
- **Delegate 并行**：`delegate_task` 支持多个子任务并行执行，结果聚合返回
- **输出契约（expect）**：子智能体结果必须满足 JSON Schema，否则返回 `contract_failed`，主智能体据此重试或调整策略

---

### 2.5 RAG 检索 — 混合策略降低幻觉

```
查询 → 向量嵌入 → Faiss ANN 召回（top_k*4 候选）
              ↓
       向量分(0.6) + 关键词覆盖分(0.4) = 混合打分
              ↓
       可选 Rerank（RRF 融合 / API 重排）
              ↓
       过滤低相关片段（rag_min_relevance 阈值）→ 注入上下文
```

**设计合理之处**：纯向量检索对同义词/缩写不敏感，纯关键词检索对语义不敏感，RRF 融合两者；阈值过滤防止噪声注入。

---

### 2.6 安全机制 — 纵深防御

| 层级 | 机制 | 覆盖范围 |
|------|------|---------|
| 输入护栏 | 关键词注入检测 + 路径越权检测 | 用户消息进入前拦截 |
| 输出护栏 | 密钥正则脱敏 + 提示词泄露检测 | 答案返回前过滤 |
| 危险工具确认 | interrupt 式人工批准流 | run_terminal / run_code 等高风险操作 |
| Guardrail 网关 | 可选 LLM-as-judge 语义级注入检测 | 绕过关键词的变体攻击 |

---

### 2.7 代码级自愈（Code Self-Heal）

- `run_code` / `run_terminal` 失败时自动触发「分析 → 修复 → 重跑」闭环
- LLM 生成 patch 并验证，最多重试 `AGENT_CODE_HEAL_ROUNDS=2` 轮
- 比传统反思提示更直接，减少无效重试

---

### 2.8 工具生态 — 丰富的开箱即用工具集

| 分类 | 工具数 | 代表工具 |
|------|--------|---------|
| 基础 | 5+ | get_current_time, calculate, days_between, web_search |
| 文件 | 4+ | read_file, write_file, apply_patch, check_syntax |
| 代码 | 10+ | run_code, run_terminal, code_search, git_tool |
| 知识库 | 2+ | rag_search, document_upload |
| 图表 | 5+ | generate_bar/line/pie_chart |
| 多模态 | 3+ | generate_image, image_ocr, screenshot_ocr |
| 记忆 | 3+ | memory_save, memory_search, memory_delete |
| 协作 | 2+ | delegate_task, handoff_to_role |
| 成本 | 3+ | cost_summary, cost_report, cost_daily_trend |
| MCP | 1 | mcp_call（兼容 MCP 协议） |

---

## 三、劣势（设计缺陷与潜在风险）

### 3.1 Prompt 工程质量低 — 系统提示过于简陋

```python
# app/core/prompts.py — 完整的系统提示
SYSTEM_PROMPT = """你是一个智能体(Agent),通过调用工具来完成任务。
请严格按照以下规则工作:
1. 当需要外部信息时,调用工具获取。
2. 工具调用使用 JSON 格式输出,单独一行:
   {{"tool": "工具名", "arguments": {{...}}}}
3. 每次只调用一个工具,等待工具结果后再继续。  ← ⚠️ 与实际行为矛盾
...
"""
```

**问题**：
- **第 3 条规则与原生 function calling 冲突**：实际支持并行工具调用（一次返回多个 tool_calls），但 prompt 写"每次只调用一个"，引导模型走低效的单步模式
- **缺少核心指令**：没有"何时停止调用工具"的明确信号，没有"工具失败时如何处理"的指引，没有输出格式的约束
- **缺少安全边界**：系统提示本身未做安全隔离，完全依赖外部 Guardrail 兜底

**影响**：在复杂场景下 LLM 可能过度调用工具（无限循环），或对并行工具调用表现不一致。

---

### 3.2 工具路由的"激进裁剪"导致漏调

- 默认从 100+ 工具中只选 10 个（`AGENT_TOOL_ROUTER_MAX_TOOLS=10`）
- 虽有降级兜底（已在上次改进中增强），但基础逻辑仍存在风险
- **问题**：部分业务工具（如 `read_file`、`write_file`）在代码编辑类任务中几乎必用，却被路由漏掉，导致"工具不存在"错误

---

### 3.3 Eval 回归测试严重不足

- 仅 16 个基础用例（已扩展至 24 个），缺少：
  - 多步链式任务（A 工具的输出是 B 工具的输入）
  - 边界条件（空结果、超大数据、编码异常）
  - 安全对抗用例（prompt injection 变体、越狱尝试）
  - 负例（期望不调用某工具）
- **问题**：更换模型或修改 prompt 后，无法可靠发现退化

---

### 3.4 长期记忆沉淀无去重与质量门控

- `AGENT_AUTO_MEMORY=true` 时每轮都调用 LLM 抽取事实
- 之前的问题（短回复/客套话被沉淀）已在本次改进中修复
- **剩余问题**：
  - **无去重**：同一事实多次重复存入，检索命中率下降
  - **无合并**：多条相似记忆不合并，上下文污染
  - **无衰减**：旧记忆的检索权重不随时间下降

---

### 3.5 Guardrail 关键词库维护成本高

- `_INJECTION_PATTERNS`（53 条）、`_SECRET_PATTERNS`（5 条）、`_SENSITIVE_PATH_PATTERNS`（8 条）全部硬编码
- 新型注入变种（如 unicode 混淆、base64 编码指令）无法被现有规则覆盖
- **问题**：安全防护靠规则堆砌，扩展性差；语义级检测（LLM-as-judge）虽已实现但默认关闭

---

### 3.6 成本估算依赖静态价格表

- `TOKEN_PRICES` 仅 5 个模型的硬编码价格，新模型返回 `$0`
- **问题**：实际账单与报告偏差大，用户无法做真实预算控制

---

### 3.7 后台任务缺乏资源隔离

- 并发后台任务上限 `AGENT_TASK_MAX_CONCURRENT=4`，但未限制单任务的 token 用量
- **问题**：单个后台任务可能消耗大量 LLM 调用，挤占其他任务资源
- 无优先级队列，先到先服务

---

### 3.8 图片 base64 内联导致上下文膨胀

- 默认 `data_url` 模式将图片以 base64 内联进 messages
- **问题**：5MB 图片 ≈ 6.7M 字符 ≈ 1.7M token，直接撑爆上下文
- 虽然已新增 `server_url` 模式，但默认仍为 `data_url`

---

### 3.9 Session 历史硬截断（非 LLM 摘要）

- `SessionStore` 默认 `max_messages=60`，超出后直接截断最老的消息
- **问题**：关键历史信息（如用户偏好）可能被截断丢失，而非被摘要保留

---

### 3.10 多模态 ASR 预留但未真正集成

- `transcribe_audio` 预留了 OpenAI 兼容 `/audio/transcriptions` 接口
- **问题**：未配置时静默返回"无法识别语音"，无自动 fallback（如本地 whisper）

---

## 四、现存问题清单

### 4.1 已修复的问题（本次改进）

| # | 问题 | 修复文件 | 修复内容 |
|---|------|---------|---------|
| 1 | RAG 低相关片段无过滤 | `app/rag/retriever.py` | 增加 `rag_min_relevance` 阈值过滤 |
| 2 | Resume 接口不支持流式 | `app/api/chat.py` | `ChatResumeRequest` 增加 `stream` 字段 |
| 3 | 成本估算未知模型返回 $0 | `app/core/telemetry.py` | 增加兜底价格 + 缓存折扣估算 |
| 4 | 工具路由过激导致漏调 | `app/core/agent.py` | 结果 < 3 时自动追加高频兜底工具 |
| 5 | 记忆沉淀无质量过滤 | `app/core/agent.py` | 短回复/客套话跳过沉淀 |
| 6 | 后台任务无状态查询 | `app/api/tasks.py` | 新增 `GET /tasks/{id}/status` |
| 7 | RAG 无引用溯源 | `app/rag/retriever.py` + `documents.py` | `format_context_with_refs()` + `refs` 字段 |
| 8 | 图片 base64 撑爆上下文 | `app/api/chat.py` + `attachments.py` | `server_url` 模式 + 图片大小限制 |
| 9 | Eval 用例不足 | `app/eval/cases.py` + `runner.py` | 16→24 个用例，增加负例/安全用例 |
| 10 | 上下文裁剪无优先级 | `app/core/agent.py` | `context_preserve_priority` 优先级感知 |
| 11 | Guardrail 仅有关键词 | `app/core/guardrails.py` | 新增 LLM-as-judge 语义级检测 |
| 12 | 子智能体契约弱校验 | `app/tools/delegation_tool.py` | required 字段校验 + 重试提升至 2 |

---

### 4.1 已修复的 BUG（本轮）

| # | 问题 | 严重程度 | 修复内容 |
|---|------|---------|---------|
| A1 | `message_builder._build_user_content` 是 staticmethod，model 永远为空，导致所有图片被降级为文字提示，视觉模型无法真正接收图片 | 🔴 高 | 改为实例方法，从 `llm.info["model"]` 获取模型名用于 vision 判断 |
| A2 | `_plan_task` 异常时返回 `None`（语义错误），虽当前短路能工作，但类型不一致 | 🟡 中 | 改为返回 `False`，与正常短路路径一致 |
| A3 | `_prepare_attachments` 生成图片 URL 使用 `/v1/images/` 路径，但 FastAPI 静态挂载在 `/images`，浏览器无法直接加载 | 🟡 中 | 改为 `{base}/images/{att_id}_{name}`，匹配静态挂载路径 |

| # | 问题 | 严重程度 | 建议方案 |
|---|------|---------|---------|
| ~~A1~~ | ~~系统提示词"每次只调用一个工具"与实际并行调用矛盾~~ | ~~🔴 高~~ | ~~已修复：重写 SYSTEM_PROMPT，明确支持并行 tool_calls~~ |
| ~~A2~~ | ~~Eval 缺乏多步链式任务用例~~ | 🟡 中 | ~~已修复：新增 chain_search_answer / chain_code_verify~~ |
| ~~A3~~ | ~~长期记忆无去重/合并/衰减~~ | 🟡 中 | ✅ decay.py 已实现 MemoryDecay + MemoryCluster，通过 `/v1/memory/decay/cleanup` 触发 |
| ~~A5~~ | ~~图片默认 base64 模式不安全~~ | 🟡 中 | ~~已修复：默认改为 server_url~~ |
| ~~A6~~ | ~~安全对抗用例仅 1 个（关键词级）~~ | 🟡 中 | ~~已修复：增至 4 个，含 roleplay/敏感路径/API Key 泄露~~ |
| ~~A8~~ | ~~后台任务无单任务 token 上限~~ | 🟢 低 | ~~已修复：新增 agent_task_max_tokens 配置项，超过即终止~~ |
| ~~A9~~ | ~~RAG 切换 embedding 后端后未自动清理旧索引~~ | 🟢 低 | ~~已修复：新增 POST /v1/documents/reset 端点~~ |
| ~~A10~~ | ~~缺少健康检查的指标细化~~ | 🟢 低 | ~~已修复：/health 返回 LLM/DB/磁盘/运行时/任务五维指标~~ |
| **B1** | Session 历史 60 条硬截断 | 🟢 低 | 改用 LLM 摘要替代截断，或增加滚动摘要 |
| **B2** | Guardrail 语义检测默认关闭 | 🟢 低 | 生产环境建议手动开启 `guardrail_semantic_enabled=true` |
| **B3** | ASR 未配置时无 fallback | 🟢 低 | 预留本地 whisper 降级接口 |

---

## 五、总结评分

| 维度 | 评分 | 说明 |
|------|------|------|
| **架构成熟度** | ⭐⭐⭐⭐☆ | 图运行时 + checkpoint + interrupt，达到生产级水位 |
| **工具生态** | ⭐⭐⭐⭐☆ | 100+ 工具，覆盖文件/代码/RAG/多模态/协作 |
| **可靠性工程** | ⭐⭐⭐⭐☆ | 重试 + 熔断 + 故障转移 + 自愈，基础设施完善 |
| **安全防御** | ⭐⭐⭐☆☆ | 护栏机制完整，但 Prompt 工程质量待提升 |
| **可观测性** | ⭐⭐⭐⭐☆ | Trace + 成本追踪 + OTLP 导出，观测链路完整 |
| **测试覆盖** | ⭐⭐⭐☆☆ | 24 个用例覆盖主要场景，但缺少复杂多步任务 |
| **长对话支持** | ⭐⭐⭐⭐☆ | 三层压缩 + 优先级感知，优于大多数竞品 |
| **易用性** | ⭐⭐⭐⭐☆ | `.env` 配置驱动，Docker 一键部署，文档齐全 |

**综合评估**：这是一个**工程质量较高、功能完整的生产级 Agent 框架**，在图运行时、可靠性工程、长对话管理等核心基础设施上达到了行业主流水平。主要短板在于 Prompt 工程（系统提示质量）、测试覆盖深度、以及长期记忆的自组织能力，这些属于"精细化运营"层面的问题，不影响基础功能但会影响复杂场景下的表现稳定性。
