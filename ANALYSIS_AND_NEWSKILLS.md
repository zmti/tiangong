# 智能体功能差距分析与新增功能说明

## 一、现有能力 vs 主流框架对比

| 维度 | Agent Service 当前状态 | Claude Code / Devin / Cursor 等主流 | 差距评估 |
|------|----------------------|-------------------------------------|---------|
| **上下文工程** | Token级预算 + 三层压缩 | 动态窗口、语义分块、滑动摘要 | ⚠️ 中等 |
| **记忆系统** | SQLite LTM + 衰退清理 | 工作记忆+语义记忆分层(Arthur/Anthropic) | ⚠️ 中等 |
| **多智能体** | 6角色+handoff+expect契约 | 多代理编排(Swarm/AutoGen风格) | ✅ 基本对齐 |
| **工具调用** | Function calling + 路由 | 工具链组合(skill composition) | ⚠️ 缺失 |
| **用户画像** | 无 | 内置用户偏好注入(类Custom Instructions) | ❌ 缺失 |
| **成本预算** | 无会话级追踪 | 硬/软预算提示,自动中止 | ❌ 缺失 |
| **A/B评估** | eval runner(单次) | 持续回归+对照实验 | ⚠️ 部分缺失 |
| **Embedding缓存** | 无 | 本地/远程缓存避免重复API | ❌ 缺失 |
| **Skill/组合工具** | 单工具原子操作 | 可组合高级操作(workflows) | ❌ 缺失 |
| **记忆精炼** | 单条记忆读写 | 自动合并/提炼/去重 | ❌ 缺失 |

---

## 二、本次新增功能

### 2.1 MemoryConsolidator — 记忆自动精炼 (`app/memory/consolidator.py`)
对标 Anthropic 的"工作记忆→长期记忆"提炼流:
- 相似记忆按关键词重叠分组,由 LLM 合并为一条精炼记忆
- 提取用户偏好和技能领域作为"画像记忆"
- 触发方式:`POST /v1/memory/consolidate`(手动)或配置 `AGENT_CONSOLIDATE_ENABLED=true` 自动

### 2.2 UserProfiler — 用户画像持久化 (`app/memory/profile.py`)
- `UserProfile` 数据模型:语言偏好、代码风格、响应风格、擅长领域、技术栈、工作习惯、约束
- 每次对话前自动注入为系统提示的一部分(`build_profile_prompt`)
- API: `GET/PUT/DELETE /v1/user_profile/*`,支持 `/extract` 启发式提取技术关键词
- 配置项:`AGENT_USER_PROFILE_ENABLED=true`(默认)

### 2.3 SkillRegistry — 可组合技能系统 (`app/core/skills.py`)
- Skill 是多个工具的链式编排(如 `deploy_to_staging` = build→test→deploy)
- 4个开箱即用的内置技能:部署到staging、全量回归测试、生成PR描述、安全扫描
- 技能描述注入系统提示,让 LLM 在合适时机直接调用
- API: `GET /v1/skills`, `GET /v1/skills/{name}`, `POST /v1/skills/{name}/toggle`
- 开关:`AGENT_SKILLS_ENABLED=true`

### 2.4 SessionCostBudget — 会话级成本预算 (`app/core/budget.py`)
- 每个 session 维护累计 token 和美元成本(SQLite持久化)
- 请求级可设 `budget_usd` / `budget_tokens` 硬上限
- 软警告(≥80%预算)和硬限制(超预算自动中止)双轨
- API: `GET/POST /v1/budget/*`
- 在 `_node_llm` 中自动记录每次 LLM 调用的 token/成本

### 2.5 EmbeddingCache — 嵌入向量缓存 (`app/rag/cache.py`)
- 内存 dict + SQLite 文件两级缓存,跨进程重启有效
- `run_embed()` 先查缓存再调 API,避免相同文本重复计算
- TTL 默认24小时,可配置大小上限
- API: `GET /v1/embedding_cache/stats`, `POST /v1/embedding_cache/invalidate`

### 2.6 A/B 评估对比 (`app/eval/ab.py`)
- 同一数据集上并行跑两组配置,输出通过率/成本/统计显著性
- 支持按用例名/分类筛选,JSON 落盘
- API: `POST /v1/eval/ab/run`, `GET /v1/eval/ab/report`

---

## 三、新增 API 端点汇总

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET`  | `/v1/user_profile` | 查看用户画像 |
| `PUT`  | `/v1/user_profile` | 更新用户画像 |
| `DELETE` | `/v1/user_profile` | 删除用户画像 |
| `GET`  | `/v1/user_profile/list` | 列出所有画像 |
| `POST` | `/v1/user_profile/extract` | 从消息推断画像 |
| `GET`  | `/v1/budget/status?session_id=` | 查询预算状态 |
| `POST` | `/v1/budget/set` | 设置预算上限 |
| `POST` | `/v1/budget/record` | 记录token成本 |
| `POST` | `/v1/budget/reset` | 重置预算 |
| `GET`  | `/v1/budget/sessions` | 列出所有会话预算 |
| `POST` | `/v1/memory/consolidate` | 触发记忆精炼 |
| `GET`  | `/v1/memory/consolidate/status` | 精炼状态 |
| `GET`  | `/v1/embedding_cache/stats` | 嵌入缓存统计 |
| `POST` | `/v1/embedding_cache/invalidate` | 清除嵌入缓存 |
| `GET`  | `/v1/skills` | 列出所有技能 |
| `GET`  | `/v1/skills/{name}` | 查看技能详情 |
| `POST` | `/v1/skills/{name}/toggle` | 启用/禁用技能 |
| `POST` | `/v1/eval/ab/run` | 运行 A/B 对比评估 |
| `GET`  | `/v1/eval/ab/report` | 查询最近 A/B 报告 |

Chat 接口新增字段:
- `user_id`:用户ID(用于画像查找)
- `budget_usd`/`budget_tokens`:本次会话预算上限
- 响应中新增 `budget` 字段(当前预算状态)

---

## 四、新增配置项 (`.env`)

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `AGENT_CONSOLIDATE_ENABLED` | `false` | 开启后每次对话结束后自动触发记忆精炼 |
| `AGENT_CONSOLIDATE_MIN_INTERVAL` | `3600` | 精炼最小间隔(秒) |
| `AGENT_USER_PROFILE_ENABLED` | `true` | 启用用户画像注入 |
| `AGENT_SKILLS_ENABLED` | `false` | 启用高级技能系统 |
| `AGENT_EMBEDDING_CACHE_ENABLED` | `true` | 开启嵌入缓存 |
| `AGENT_EMBEDDING_CACHE_PERSISTENT` | `true` | 跨进程持久化嵌入缓存 |
| `AGENT_EMBEDDING_CACHE_MAX_SIZE` | `10000` | 缓存最大条目数 |
| `AGENT_EMBEDDING_CACHE_TTL` | `86400` | 缓存TTL(秒) |
| `AGENT_COST_BUDGET_ENABLED` | `true` | 开启会话成本追踪 |

---

## 五、文件变更清单

新增文件:
- `app/core/skills.py` — Skill 注册器与内置技能
- `app/core/budget.py` — 会话成本预算追踪器
- `app/memory/profile.py` — 用户画像存储
- `app/memory/consolidator.py` — 记忆精炼引擎
- `app/rag/cache.py` — Embedding 缓存
- `app/eval/ab.py` — A/B 评估对比
- `app/api/budget.py` — 预算 API 路由
- `app/api/user_profile.py` — 用户画像 API 路由
- `app/api/advanced.py` — 记忆精炼/嵌入缓存/技能/A-B评估 API

修改文件:
- `app/config.py` — 新增7个配置项
- `app/core/agent.py` — 集成画像注入/技能描述/预算记录/自动精炼触发
- `app/rag/embeddings.py` — 嵌入调用接入缓存
- `app/api/chat.py` — 新增 user_id/budget 请求字段与 budget 响应
- `app/main.py` — 注册新路由
