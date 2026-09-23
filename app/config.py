import os
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 应用版本:随协议/接口变更递增;桌面端启动器据此识别旧版残留进程
APP_VERSION = "1.1.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("AGENT_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 模型 Provider ----
    agent_llm_provider: str = "deepseek"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"

    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "qwen2.5:7b"

    openai_compat_base_url: str = ""
    openai_compat_api_key: str = ""
    openai_compat_model: str = ""

    # ---- 图片 ----
    image_provider: str = ""  # 图片生成 provider;留空表示跟随 openai_compat
    image_model: str = "agnes-image-2.0-flash"  # 图片生成模型(可用 IMAGE_MODEL 覆盖)

    # ---- 视频 ----
    video_model: str = "agnes-video-v2.0"  # 视频生成模型(可用 VIDEO_MODEL 覆盖)

    # 说明:以上模型均可在 .env 里配置(字段名转大写即环境变量名,无 env_prefix);
    # 桌面端设置面板的覆盖值优先级更高,见 app/core/config_runtime.py。

    # ---- Embedding ----
    # local: 本地哈希嵌入(默认,无外部依赖) / api: 使用 OpenAI 兼容 Embeddings API
    embedding_backend: str = "local"
    # API 嵌入独立配置(不借用 LLM 的 openai_compat);留空则回退 openai_compat_* 与默认 provider
    embedding_api_base_url: str = ""
    embedding_api_key: str = ""
    embedding_api_model: str = ""  # 如 text-embedding-3-small / gte-qwen2-1.5b-instruct
    # 每次 embeddings 请求最多输入的文本数(防超限,自动分批)
    embedding_batch_size: int = 16
    # 嵌入 API 调用重试次数(网络/限流等瞬态错误)
    embedding_retries: int = 2
    # 是否对向量做 L2 归一化(余弦相似度需要;faiss 用 IP 距离依赖归一化)
    embedding_normalize: bool = True

    # ---- ANN 向量索引 ----
    # auto: 安装了 faiss 用 faiss(flat 精确,本地规模足够快),否则回退 SQLite 线性扫描
    # flat: 强制 faiss flat(精确,余弦);hnsw: faiss HNSW(近似,量大更快);off: 禁用 faiss
    vector_index_backend: str = "auto"
    # HNSW 图构建参数(M 邻居数、efConstruction 构建搜索范围、efSearch 查询搜索范围)
    hnsw_m: int = 16
    hnsw_ef_construction: int = 200
    hnsw_ef_search: int = 64

    # ---- 重排 ----
    # 检索候选经过重排器排序后再返回,提升 top-k 精度
    # none: 不做二次重排(仅混合打分) / rrf: 向量+关键词的 Reciprocal Rank Fusion / api: 调用 API rerank
    rerank_mode: str = "rrf"
    # API rerank 独立配置(如 dashscope gte-rerank、Jina reranker 等 OpenAI 兼容接口)
    rerank_api_base_url: str = ""
    rerank_api_key: str = ""
    rerank_api_model: str = ""
    # 重排时送入 API 的最大候选数
    rerank_top_n: int = 10

    # ---- 网络搜索 ----
    # 支持 serpapi / bing;留空则自动禁用 web_search 工具
    search_api_key: str = ""
    search_provider: str = "serpapi"

    # ---- 多模态 ----
    # 视觉能力:开启后,聊天附件里的图片以 data URL 走 image_url content part 进主链路,
    # 模型不支持视觉时自动降级(附文本提示"图片已上传但不能理解")
    multimodal_vision_enabled: bool = True
    # 语音音频转录(ASR)预留接口:OpenAI 兼容 /audio/transcriptions 端点;
    # 留空 = 不启用(语音询问时返回明确提示)
    asr_base_url: str = ""
    asr_api_key: str = ""
    asr_model: str = ""  # 如 whisper-1 / paraformer-realtime-v2
    asr_timeout: int = 120
    # 文字转语音(TTS):OpenAI 兼容 /audio/speech 端点;留空=不启用
    tts_base_url: str = ""
    tts_api_key: str = ""
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"

    # ---- 后台任务(长期自主运行) ----
    # 开启后可通过 /v1/tasks 创建后台持续运行的 Agent 任务(独立于请求),
    # 自主完成计划-执行-验证循环,完成后写入会话并可通过事件流订阅进度。
    agent_background_tasks: bool = True
    # 单个后台任务最大运行时长(分钟),超过自动终止
    agent_task_max_minutes: int = 60
    # 同时运行的后台任务数上限(防刷屏/资源挤占);0 = 无限制
    agent_task_max_concurrent: int = 4
    # 后台任务失败(LLM 瞬态错误等)自动重试次数;0 = 不重试
    agent_task_retries: int = 1
    # 重试基础等待(秒),按指数退避
    agent_task_retry_delay: float = 5.0
    # 单后台任务 token 预算上限(0=不限制);防止单个任务消耗过多 LLM 配额
    agent_task_max_tokens: int = 0

    # ---- 上下文配额硬控制 ----
    # 请求级 max_output_tokens 上限;0 = 不限制(由 provider 默认值决定)。
    # agnes-3.0-flash 推理模型的 thinking token 也计入此限制,
    # 设 16384 留足输出空间, 同时避免 provider 默认值过低导致回答截断。
    agent_default_max_output_tokens: int = 16384

    # ---- 工作流生成服务地址 ----
    # Agent 通过 create_workflow_from_desc 工具调用此地址生成工作流。
    # 默认 http://127.0.0.1:8517;容器化部署时请修改为实际服务地址。
    workflow_service_base_url: str = "http://127.0.0.1:8517"

    # ---- 自动记忆沉淀限制 ----
    # 每次对话最多沉淀的事实数(semantic 层)
    agent_auto_memory_max_facts: int = 5
    # 每次对话最多沉淀的情节数(episodic 层)
    agent_auto_memory_max_episodic: int = 3

    # ---- 语义记忆分层 ----
    # 开启后记忆注入分为 working(即时)/semantic(语义检索)/episodic(过往会话摘要)三层,
    # 以带强度标签的方式注入上下文;关闭则维持旧的单一检索注入。
    agent_layered_memory: bool = True

    # ---- 代理配置 ----
    # HTTP/HTTPS 代理地址(留空则使用系统环境变量 HTTP_PROXY/HTTPS_PROXY)
    http_proxy: str = ""
    https_proxy: str = ""
    # 代理排除列表(逗号分隔的域名,不走代理)
    no_proxy: str = ""

    # ---- 数据层后端 ----
    # 会话/记忆存储:sqlite(默认,零依赖) / redis(需 redis-py + REDIS_URL)
    storage_backend: str = "sqlite"
    redis_url: str = ""
    # 向量存储:auto(装了 psycopg 且配置 PGVECTOR_* 则用 pgvector,否则 SQLite+faiss)
    #   / pgvector(强制 pgvector) / sqlite
    vector_store_backend: str = "auto"
    pgvector_client_dsn: str = ""
    pgvector_client_table: str = "knowledge_vectors"

    # ---- 代码级自愈 ----
    # 开启后,run_code/run_terminal 等代码工具失败时,自动进入"分析-修复-重试"
    # 闭环:LLM 生成修复 patch,写回文件后重跑最多 agent_code_heal_rounds 轮。
    agent_code_self_heal: bool = True
    agent_code_heal_rounds: int = 2

    # ---- Agent ----
    agent_max_steps: int = 0
    # 0 = 不设步骤上限(由 agent_replan_max_times 收敛控制:重新规划耗尽后,
    # 同一工具连续失败 3 次仍 giveup;配合 _tool_failure 的"同参 3 连败"兜底,
    # 不会死循环)。设 >0(如 10)则按旧行为在第 N 轮强制 giveup。
    # 旧实现默认 10:复杂任务(计划 >5 步 / 多轮工具探索)经常在 10 轮内打不完,
    # 前端看到"无法完成任务:已尝试 10 步,仍未能得到正确结果"。现默认放开。
    # 同一 Agent 实例最大并发运行数(0=不限制);防止大量并发请求耗尽 LLM 配额和服务器资源
    agent_max_concurrent_runs: int = 5
    agent_temperature: float = 0.7
    agent_system_prompt: str = "你是一个智能助手,请用中文回答。"
    # 单条历史消息最大字符数(防止大工具结果/图片 base64 塞爆上下文)
    message_char_cap: int = 20000
    # 注入上下文的历史总字符预算(约 60k 字符 ≈ 15k~20k token,留足模型余量)
    # 注意: agnes-3.0-flash 上下文窗口 50k token, 需预留 ~15k 给工具结果和输出
    context_char_budget: int = 60000
    # Token 级上下文预算(>0 时用 estimate_tokens 精确裁剪,比字符预算更准确)
    # 设置为 30000 留足余量给工具调用和输出
    agent_token_budget: int = 30000

    # ---- 原生工具调用(function calling)----
    # 开启时走 LLM 原生 tool_calls(参数结构化、支持并行),失败自动降级为 JSON 文本解析
    agent_native_tools: bool = True
    # 单次 LLM 调用允许的最大工具数量(0 = 不限制)
    agent_max_tools: int = 0

    # ---- LLM 调用可靠性(重试 + 故障转移) ----
    # 单次 LLM API 调用超时(秒);超时触发重试+故障转移,避免永久挂起
    agent_llm_timeout: int = 60
    # LLM 调用失败(网络/限流/5xx 等瞬态错误)自动重试次数;0 = 不重试
    agent_llm_retries: int = 2
    # 重试基础延迟(秒),按 2^n 指数退避
    agent_llm_retry_delay: float = 0.5
    # 流式调用空闲超时(秒): 流内相邻 chunk 间隔超过该值视为上游卡死,立即中断
    # 并给出可操作提示;0 = 不限制(不建议,provider 中途停顿会导致前端"一直卡")
    agent_llm_stream_idle_timeout: int = 90
    # 流式调用总时长上限(秒): 从建连到流结束的整体硬超时,0 = 不限制
    agent_llm_stream_total_timeout: int = 600
    # 底层 HTTP 读超时(秒): 0 = 自动派生(max(空闲超时+30, 120))。
    # 必须大于 agent_llm_stream_idle_timeout,否则 httpx 会先于应用层空闲守卫
    # 抛原始 ReadTimeout 杀掉仍在正常输出的流(实测 2026-09-20 read=60 < idle=90)
    agent_llm_http_read_timeout: int = 0
    # 答案"没写完"时自动续写的最大次数(0 = 关闭)。网关常把 max_tokens 截断
    # 归一化成 finish_reason=stop,没有这层守卫时用户会看到半截答案
    agent_answer_continue_max: int = 2
    # 故障转移:主 provider 连续失败耗尽重试后,自动切换到备用 provider(留空 = 不切换)
    agent_llm_failover_provider: str = ""
    # 备用 provider 使用的模型(留空 = 该 provider 默认模型)
    agent_llm_failover_model: str = ""

    # ---- Provider 熔断(连续失败快速跳过) ----
    # 同一 provider 连续失败达到阈值后,进入冷却窗口,期间所有新调用直接跳过
    # 该 provider(不再等待重试超时),避免对故障上游反复发起请求拖慢整体。
    # 0 = 关闭熔断(仅靠重试+故障转移)
    agent_llm_circuit_threshold: int = 5
    # 熔断冷却时间(秒),冷却结束后计数清零,允许再次尝试
    agent_llm_circuit_cooldown: float = 30.0

    # ---- Prompt 缓存控制 ----
    # 在 system 消息注入 cache_control 断点(OpenAI 兼容厂商多支持自动前缀缓存)。
    # 开启后 tool 定义与 system 提示保持稳定前缀,命中缓存可大幅降本降延迟。
    agent_prompt_cache: bool = True

    # ---- Reasoning 模型适配 ----
    # 解析 deepseek-reasoner / o1 等推理模型返回的 reasoning_content 思维链,
    # 透出为 thinking 事件而不是混入正文;关闭时忽略该字段
    agent_reasoning_enabled: bool = True

    # ---- Git PR 工作流 ----
    # Git 平台(github/gitlab);留空=github
    agent_git_host: str = "github"
    # Git API Token(GITHUB_TOKEN 或 GITLAB_TOKEN 环境变量也可)
    agent_git_token: str = ""
    # GitLab 实例 URL(仅 gitlab 平台时需要)
    agent_gitlab_url: str = ""

    # ---- 长期记忆 ----
    # 对话前自动检索相关长期记忆并注入上下文;记忆由 memory_save 等工具写入
    agent_memory_enabled: bool = True
    # 每轮对话结束后自动抽取事实存入长期记忆(多一次 LLM 调用,默认关闭)
    agent_auto_memory: bool = False
    # 记忆精炼开关:定期合并相似记忆+提炼用户画像(需 LLM 可用)
    agent_consolidate_enabled: bool = False
    # 精炼最小间隔(秒),防止过于频繁调用 LLM
    agent_consolidate_min_interval: int = 3600
    # 用户画像开关:根据历史消息自动提取偏好并注入系统提示
    agent_user_profile_enabled: bool = True

    # ---- 会话摘要压缩 ----
    # 历史超过预算时,不再粗暴截断,而是用 LLM 把较早对话压成摘要再注入
    agent_summarize: bool = True
    # 历史字符数超过 context_char_budget*该比例时触发摘要压缩(降低阈值更早触发)
    agent_summary_threshold_ratio: float = 0.4
    # 喂给摘要模型的旧消息合并后最大字符数
    agent_summary_chars: int = 60000

    # ---- 子任务独立上下文强制化 ----
    # delegate/handoff 创建的子智能体上下文预算(字符),强制子任务聚焦、不回流父级历史。
    # 独立于主智能体 context_char_budget,子 Agent 只拿到自己指令+背景+工具结果。
    agent_subagent_context_budget: int = 60000
    # 子智能体 token 级预算(0 = 不启用 token 级压缩)
    agent_subagent_token_budget: int = 0

    # ---- 任务规划(Plan-then-Execute) ----
    # 开启时,复杂任务先由 LLM 生成执行计划,ReAct 循环内用 set_plan/update_plan 维护进度
    agent_planning: bool = True
    # 计划步骤数上限(增加到10以适应更复杂的任务)
    agent_plan_max_steps: int = 10
    # 计划失败后自动重新规划(replan):同一工具连续失败达到阈值时,
    # 基于"原始任务 + 失败原因"让 LLM 生成修订版计划并替换旧计划。
    # 关闭时仅注入反思提示(现状),开启时给出一条可执行的修订路线。
    agent_replan_enabled: bool = True
    # 连续失败多少次触发重新规划
    agent_replan_after_failures: int = 2
    # 单次运行最多重新规划次数(防止死循环)
    agent_replan_max_times: int = 3

    # ---- 工具执行重试(瞬态错误指数退避) ----
    # 工具返回超时/网络/限流等瞬态错误时,自动重试次数(0 = 不重试)
    agent_tool_retries: int = 2
    # 重试基础延迟(秒),按 2^n 指数退避
    agent_tool_retry_delay: float = 0.5

    # ---- 工具结果去重缓存 ----
    # 同一运行内,相同工具+相同参数的调用直接复用上一次结果(避免重复执行
    # 昂贵/有副作用风险的确定性调用,对齐 Claude Code 的 tool result caching)。
    # 注意:仅缓存成功结果,失败/带副作用工具不缓存。0 = 关闭
    agent_tool_cache_size: int = 50
    # 仅对这些工具启用缓存(逗号分隔);其余带副作用工具(文件/数据库/网络)不缓存
    # 避免缓存过期导致返回陈旧数据
    agent_tool_cache_safe_tools: str = "run_code,calculate,days_between,get_current_time,web_search"

    # ---- 搜索去重 ----
    # 同一主题最大搜索次数,超过后拦截重复搜索(防止搜索循环)
    agent_search_max_repeats: int = 3

    # ---- 最终答案事实校验(LLM-as-judge) ----
    # finalize 产出答案后,用评审模型交叉核对"答案中的断言 vs 工具结果",
    # 发现幻觉/未支撑断言时重写一次答案再返回。开启会多一次 LLM 调用
    agent_verify: bool = False  # 默认关闭,避免过度验证导致任务失败

    # ---- 反思循环(生成→自评→重写,Reflexion 式) ----
    # 关闭时保留现有"工具失败反思"(失败提示注入);开启后最终回答前先用 LLM
    # 按结构化 schema 自评,不合格且有预算则回灌评语要求模型重写(最多 agent_reflect_max_rounds 轮)
    # 默认关闭:每条实质性回答额外多 1~3 次 LLM 调用,在上游限流(agnes)环境下
    # 显著放大"限流杀流/答案重写闪烁"等不稳定现象。需要质量优先时可手动开启。
    agent_reflect: bool = False
    # 自评→重写最多轮数(每轮多一次 LLM 调用),增加到2轮以提高成功率
    agent_reflect_max_rounds: int = 2
    # critic/executor 双角色:开启后评审用独立模型(区别于执行模型),避免自评偏袒。
    # provider 留空 = 与执行模型同 provider
    agent_critic_provider: str = ""
    # critic 使用的模型(留空 = 该 provider 默认模型)
    agent_critic_model: str = ""

    # ---- 多模型协作模式(用户可经请求级 mode 覆盖) ----
    # auto: 跟随下方各开关(默认,保持现状)
    # single: 单模型直接做,不做评审/校验(最快最省)
    # review: 一个模型做,另一个模型测质量(强制开启 verify + 独立 critic)
    # ensemble: 多个模型并行做同一任务,评审模型选优
    agent_mode: str = "auto"
    # ensemble 模式默认参与的模型(逗号分隔 "provider:model" 或 "provider",留空=仅主模型)
    agent_ensemble_models: str = ""
    # ensemble 模式参与模型数量上限
    agent_ensemble_max: int = 3

    # ---- 图运行时 / Checkpoint(可持久化执行) ----
    # 开启后,每次运行在每个图节点边界把完整状态(消息/计划/步骤/路由)落盘
    # data/checkpoints.db,支持断点续跑 / 后台任务 / 运行记录查询
    agent_checkpoint_enabled: bool = True
    # checkpoint 每条消息最大字符数(防大消息撑爆 SQLite);0=不截断
    checkpoint_message_char_cap: int = 8000
    # 运行最大图节点数(兜底防死循环;llm 轮数上限仍由 agent_max_steps 控制)
    # 注: 此配置项已废弃,由 agent_max_steps 统一控制
    # agent_graph_max_iters: int = 300
    # 单个工具执行超时(秒);0=不限制
    agent_tool_timeout: int = 0
    # checkpoint 保留天数(0=不清理);超过天数的已完成/已取消 checkpoint 自动清理
    checkpoint_ttl_days: int = 7
    # 批量写入刷新间隔(秒);0 = 关闭批量写入(每节点逐条落盘)
    agent_checkpoint_batch_flush_interval: float = 2.0
    # 批量写入最大缓冲条数;超过则立即强制刷新最旧条目
    agent_checkpoint_batch_max_buffer: int = 10

    # ---- 模型路由(简单/复杂任务分流) ----
    agent_routing_enabled: bool = False
    router_fast_provider: str = ""  # 简单问答走便宜模型(留空=用主 provider)
    router_fast_model: str = ""
    router_strong_provider: str = ""  # 复杂分析走强模型(留空=用主 provider)
    router_strong_model: str = ""

    # ---- 工具路由(工具集预选) ----
    # 开启时,每轮对话前由 LLM 从全量工具里挑出相关子集再执行,
    # 避免 100+ 工具全量注入导致调用精度下降(BFCL 建议的 tiered approach)。
    # 路由失败自动回退为全量工具。
    agent_tool_router_enabled: bool = True
    # 预选工具数上限(系统工具 set_plan/update_plan/memory_* 常驻不计入)
    agent_tool_router_max_tools: int = 10

    # ---- 可观测性 ----
    # 记录 trace(JSONL 文件 data/traces/<date>.jsonl + 内存缓冲),供 /v1/traces 查询
    agent_tracing_enabled: bool = True

    # OTLP 导出(可选):配置端点后,事件同步转发到 OTLP 兼容后端
    # (Langfuse / OpenTelemetry Collector / 自建网关)。留空则仅本地 JSONL+内存。
    otel_exporter_otlp_endpoint: str = ""
    # 是否带 token/成本属性的 LLM 调用细节(大流量时可关)
    otel_export_llm_details: bool = True

    # ---- 安全 ----
    # 非空时所有 API 路由需携带 Authorization: Bearer <key>
    agent_api_key: str = ""
    # 开启后,危险工具(run_terminal/run_code 等)需请求 approve_tools 字段确认后才执行
    agent_tool_confirm: bool = True
    # 危险工具清单(逗号分隔)
    agent_dangerous_tools: str = "run_terminal,run_code,send_email,delete_email"
    # 每个危险工具每分钟最大调用次数(0=不限制)
    agent_dangerous_tool_rate_limit: int = 10

    # ---- 多租户 ----
    # 启用多租户:每个 API Key 归属一个租户,支持独立的用量统计和数据隔离
    default_tenant_enabled: bool = False
    # tenant_fallback_to_single_key: bool = True  # 已废弃,由 tenant.py 硬编码处理

    # ---- 服务 ----
    agent_host: str = "0.0.0.0"
    agent_port: int = 8080

    data_dir: str = "data"

    # ---- RAG 检索质量阈值 ----
    # 向量相关性最低分数阈值;低于此值的 chunk 不注入答案上下文(0.0=不过滤)
    rag_min_relevance: float = 0.3
    # rag_max_context_chars: int = 4000  # 已废弃,由 retriever.py 硬编码
    # RAG 结果是否附带引用溯源(每段附 doc_id + chunk_index + 来源页码)
    rag_return_refs: bool = True

    # ---- 图片多模态模式 ----
    # 图片附件最大字节数(超过则拒绝,由代码硬编码 10MB)
    # multimodal_image_mode: str = "server_url"  # 已废弃
    # multimodal_public_base_url: str = ""  # 已废弃
    # multimodal_max_image_bytes: int = 10 * 1024 * 1024  # 已废弃

    # ---- 上下文重要性感知裁剪 ----
    # 开启后,超预算时按重要性优先级裁剪
    # context_preserve_priority: bool = True  # 已废弃,由 _prepare_history 硬编码处理

    # 长期记忆沉淀过滤由 ltm.py 内部处理,不再使用独立配置项
    # memory_auto_filter_enabled: bool = True  # 已废弃
    # memory_min_length: int = 15  # 已废弃
    # memory_skip_keywords: str = ""  # 已废弃

    # ---- Guardrail 语义级检测 ----
    # 开启后,除关键词匹配外,额外调用 LLM 做语义级 Prompt 注入/提示词泄露检测
    # (性能敏感场景建议关闭,会增加约 1 次 LLM 调用)
    guardrail_semantic_enabled: bool = False

    # ---- 文件写权限 ----
    # write_file 工具允许写入的根目录白名单(逗号分隔,可用相对路径);空则仅允许数据目录
    allowed_write_dirs: str = ""

    # ---- Embedding 缓存 ----
    # 开启后对嵌入结果做内存+磁盘两级缓存,避免重复调用 API
    agent_embedding_cache_enabled: bool = True
    # 持久化缓存跨进程重启有效(内存缓存 + SQLite 文件)
    agent_embedding_cache_persistent: bool = True
    # agent_embedding_cache_max_size: int = 10000  # 已废弃,由代码硬编码
    # agent_embedding_cache_ttl: int = 86400  # 已废弃,由代码硬编码

    # ---- 会话成本预算 ----
    # agent_cost_budget_enabled: bool = True  # 已废弃,成本记录由 metrics.py 独立处理

    # ---- 代码沙箱 ----
    # run_code 沙箱使用的 Python 解释器路径(打包后 sys.executable 是应用 exe,不可用作解释器);
    # 留空则自动探测:当前解释器(开发) -> PATH 中的 python -> 常见安装位置
    sandbox_python: str = ""

    # ---- 终端命令 ----
    # run_terminal 使用的 shell: auto(按平台自动) / cmd / powershell / bash
    terminal_shell: str = "auto"
    # run_terminal 默认工作目录;空则用当前工作目录
    terminal_cwd: str = ""

    # ---- 工具流式输出 ----
    # 开启后,工具执行过程中会按块推送 tool_chunk 事件,让 LLM 实时感知进度
    agent_tool_streaming_enabled: bool = True
    # 工具 chunk 单次最大字节数(超过则拆分),默认 1024
    agent_tool_streaming_chunk_size: int = 1024

    # ---- 结构化输出严格模式 ----
    # 开启后,chat_json_schema 在所有 provider 上强制执行 schema 校验 + 重试回灌
    agent_structured_output_strict: bool = True
    # 结构化输出校验失败时的最大重试次数
    agent_structured_output_max_retries: int = 3

    # ---- 多智能体委派 ----
    # 子智能体最大委派深度(0=主 Agent,1=直接子 Agent,2=孙 Agent)
    agent_delegate_max_depth: int = 2
    # 子智能体执行超时(秒)
    agent_subagent_timeout: int = 300

    # ---- 会话长期存储与自动清理 ----
    # 会话超过此字符数时自动归档(压缩为摘要 JSON + 删除活跃记录)
    session_archive_chars: int = 50000
    # 会话超过此天数未更新时自动清理(归档 + 删除)
    session_retention_days: int = 90
    # 后台自动清理循环间隔(秒);0 = 关闭后台任务,仅手动触发
    session_cleanup_interval_seconds: int = 3600

    # ---- 安全门禁 ----
    # 工具执行前的强制人工确认(高风险操作必须用户批准,模型无法绕过)
    agent_confirm_enabled: bool = True
    # 等待用户确认的超时秒数;超时按拒绝处理(阻塞轮询模式)
    agent_confirm_timeout: int = 300
    # 确认流采用 interrupt 式(对齐 LangGraph interrupt 语义):
    # 遇到需确认的工具时,图暂停并落盘,请求立即返回控制权(run_id + 待确认项);
    # 用户批准后通过 resume(run_id, resume_value) 从原节点恢复执行,不再占用请求阻塞轮询。
    agent_confirm_interrupt: bool = True

    # ---- Guardrail 护栏(输入/输出并行校验 + Prompt 注入防护) ----
    # 开启后,进入主循环前对用户消息做注入/越权检测,返回前对答案做敏感信息检测。
    # 拦截时返回固定安全文案;违规记录在 steps(type=guardrail)。
    agent_guardrails_enabled: bool = True
    # 禁止输出的词汇列表(逗号分隔),用于敏感词过滤
    # guardrail_blocked_terms: str = ""  # 已废弃,由 guardrails.py 硬编码

    # ---- Webhook 回调 ----
    # WEBHOOK_URLS 在 .env 中配置(逗号分隔多个 URL);此处保留默认空值
    # 由 app.core.webhook._load_config() 从环境变量直接读取
    webhook_urls: str = ""
    webhook_secret: str = ""
    webhook_timeout: float = 10.0

    # ---- RAG 多路召回(M5) ----
    # 开启后启用 BM25 + 查询扩展 + RRF 融合;关闭则回退旧向量+关键词混合
    rag_multi_path_enabled: bool = True
    # 是否通过 LLM 生成查询扩展(消耗额外 token,关闭时用本地策略)
    rag_query_expand_llm: bool = False
    # BM25 召回权重(与向量分独立,RRF 融合时各有排名)
    rag_bm25_enabled: bool = True
    # 多路召回最小相关度阈值
    rag_multi_min_relevance: float = 0.0
    # rerank_top_n: int = 10  # 已废弃,由 reranker.py 硬编码

    # ---- 长时自主循环 ----
    # 最大并发长时任务数
    agent_max_concurrent_long_running: int = 3
    # agent_long_running_default_minutes: int = 120  # 已废弃,由 tasks.py 硬编码
    # agent_long_running_checkpoint_interval: int = 10  # 已废弃,由 long_running.py 硬编码
    webhook_retries: int = 2
    webhook_events: str = ""  # 逗号分隔事件类型,留空=全部

    # ---- MCP 连接管理 ----
    # MCP 自动重连最大退避间隔(秒),防止无限增长
    mcp_reconnect_max_delay: float = 60.0
    # MCP 连续失败停止重连的阈值
    mcp_reconnect_max_failures: int = 10

    # 计算实际数据目录:优先使用可写的工作目录(开发=项目根,打包=exe 旁),
    # 避免 PyInstaller 临时解包目录(_MEIPASS)导致数据丢失
    @property
    def resolved_data_dir(self) -> str:
        import os
        import sys

        if getattr(sys, "frozen", False):
            # 打包环境:exe 同目录
            base = os.path.dirname(os.path.abspath(sys.argv[0]))
        else:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # app/ 上一级
        path = os.path.join(base, self.data_dir)
        os.makedirs(path, exist_ok=True)
        return path


def _env_path() -> str:
    return os.environ.get("AGENT_ENV_FILE", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


def get_env_value(key: str) -> str | None:
    """读取 .env 文件中指定 key 的值(未配置返回 None)。"""
    path = _env_path()
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    return None


def get_httpx_proxy():
    """获取 httpx 代理配置。优先使用配置项,回退到环境变量。返回单个代理URL字符串。"""
    try:
        from app.config import settings
        https_proxy = settings.https_proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        http_proxy = settings.http_proxy or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        # 优先使用 HTTPS 代理
        if https_proxy:
            return https_proxy
        if http_proxy:
            return http_proxy
    except Exception:
        pass
    # 回退到环境变量
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if https_proxy:
        return https_proxy
    if http_proxy:
        return http_proxy
    return None


def set_env_value(key: str, value: str) -> None:
    """更新或写入 .env 文件中的 key=value。"""
    path = _env_path()
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    key_stripped = key.strip()
    value_stripped = value.strip()
    replaced = False
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, v = stripped.partition("=")
            if k.strip() == key_stripped:
                new_lines.append(f"{k}={value_stripped}\n")
                replaced = True
                continue
        new_lines.append(line)
    if not replaced:
        new_lines.append(f"{key_stripped}={value_stripped}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


settings = Settings()
