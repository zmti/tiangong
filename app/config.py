import os

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    image_model: str = "agnes-image-2.0-flash"  # 图片生成模型

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

    # ---- 上下文配额硬控制 ----
    # 请求级 max_output_tokens 上限;0 = 不限制。达到上限后停止生成并以截断提示结束。
    agent_default_max_output_tokens: int = 0

    # ---- 语义记忆分层 ----
    # 开启后记忆注入分为 working(即时)/semantic(语义检索)/episodic(过往会话摘要)三层,
    # 以带强度标签的方式注入上下文;关闭则维持旧的单一检索注入。
    agent_layered_memory: bool = True

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
    agent_max_steps: int = 10
    agent_temperature: float = 0.7
    agent_system_prompt: str = "你是一个智能助手,请用中文回答。"
    # 单条历史消息最大字符数(防止大工具结果/图片 base64 塞爆上下文)
    message_char_cap: int = 20000
    # 注入上下文的历史总字符预算(约 100k 字符 ≈ 35k~50k token,留足模型余量)
    context_char_budget: int = 100000
    # Token 级上下文预算(0 = 关闭,沿用字符预算;>0 时用 estimate_tokens 精确裁剪)
    agent_token_budget: int = 0

    # ---- 原生工具调用(function calling)----
    # 开启时走 LLM 原生 tool_calls(参数结构化、支持并行),失败自动降级为 JSON 文本解析
    agent_native_tools: bool = True

    # ---- LLM 调用可靠性(重试 + 故障转移) ----
    # LLM 调用失败(网络/限流/5xx 等瞬态错误)自动重试次数;0 = 不重试
    agent_llm_retries: int = 2
    # 重试基础延迟(秒),按 2^n 指数退避
    agent_llm_retry_delay: float = 0.5
    # 故障转移:主 provider 连续失败耗尽重试后,自动切换到备用 provider(留空 = 不切换)
    agent_llm_failover_provider: str = ""
    # 备用 provider 使用的模型(留空 = 该 provider 默认模型)
    agent_llm_failover_model: str = ""

    # ---- Provider 熔断(连续失败快速跳过) ----
    # 同一 provider 连续失败达到阈值后,进入冷却窗口,期间所有新调用直接跳过
    # 该 provider(不再等待重试超时),避免对故障上游反复发起请求拖慢整体。
    # 0 = 关闭熔断(仅靠重试+故障转移)
    agent_llm_circuit_threshold: int = 3
    # 熔断冷却时间(秒),冷却结束后计数清零,允许再次尝试
    agent_llm_circuit_cooldown: float = 60.0

    # ---- Prompt 缓存控制 ----
    # 在 system 消息注入 cache_control 断点(OpenAI 兼容厂商多支持自动前缀缓存)。
    # 开启后 tool 定义与 system 提示保持稳定前缀,命中缓存可大幅降本降延迟。
    agent_prompt_cache: bool = True

    # ---- Reasoning 模型适配 ----
    # 解析 deepseek-reasoner / o1 等推理模型返回的 reasoning_content 思维链,
    # 透出为 thinking 事件而不是混入正文;关闭时忽略该字段
    agent_reasoning_enabled: bool = True

    # ---- 长期记忆 ----
    # 对话前自动检索相关长期记忆并注入上下文;记忆由 memory_save 等工具写入
    agent_memory_enabled: bool = True
    # 每轮对话结束后自动抽取事实存入长期记忆(多一次 LLM 调用,默认关闭)
    agent_auto_memory: bool = False

    # ---- 会话摘要压缩 ----
    # 历史超过预算时,不再粗暴截断,而是用 LLM 把较早对话压成摘要再注入
    agent_summarize: bool = True
    # 历史字符数超过 context_char_budget*该比例时触发摘要压缩
    agent_summary_threshold_ratio: float = 0.6
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
    # 计划步骤数上限
    agent_plan_max_steps: int = 6
    # 计划失败后自动重新规划(replan):同一工具连续失败达到阈值时,
    # 基于"原始任务 + 失败原因"让 LLM 生成修订版计划并替换旧计划。
    # 关闭时仅注入反思提示(现状),开启时给出一条可执行的修订路线。
    agent_replan_enabled: bool = True
    # 连续失败多少次触发重新规划
    agent_replan_after_failures: int = 2
    # 单次运行最多重新规划次数(防止死循环)
    agent_replan_max_times: int = 2

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

    # ---- 最终答案事实校验(LLM-as-judge) ----
    # finalize 产出答案后,用评审模型交叉核对"答案中的断言 vs 工具结果",
    # 发现幻觉/未支撑断言时重写一次答案再返回。开启会多一次 LLM 调用
    agent_verify: bool = True

    # ---- 反思循环(生成→自评→重写,Reflexion 式) ----
    # 关闭时保留现有"工具失败反思"(失败提示注入);开启后最终回答前先用 LLM
    # 按结构化 schema 自评,不合格且有预算则回灌评语要求模型重写(最多 agent_reflect_max_rounds 轮)
    agent_reflect: bool = True
    # 自评→重写最多轮数(每轮多一次 LLM 调用)
    agent_reflect_max_rounds: int = 1
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
    # 运行最大图节点数(兜底防死循环;llm 轮数上限仍由 agent_max_steps 控制)
    agent_graph_max_iters: int = 300

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
    agent_tool_confirm: bool = False
    # 危险工具清单(逗号分隔)
    agent_dangerous_tools: str = "run_terminal,run_code"

    # ---- 服务 ----
    agent_host: str = "0.0.0.0"
    agent_port: int = 8080

    data_dir: str = "data"

    # ---- 文件写权限 ----
    # write_file 工具允许写入的根目录白名单(逗号分隔,可用相对路径);空则仅允许数据目录
    allowed_write_dirs: str = ""

    # ---- 代码沙箱 ----
    # run_code 沙箱使用的 Python 解释器路径(打包后 sys.executable 是应用 exe,不可用作解释器);
    # 留空则自动探测:当前解释器(开发) -> PATH 中的 python -> 常见安装位置
    sandbox_python: str = ""

    # ---- 终端命令 ----
    # run_terminal 使用的 shell: auto(按平台自动) / cmd / powershell / bash
    terminal_shell: str = "auto"
    # run_terminal 默认工作目录;空则用当前工作目录
    terminal_cwd: str = ""

    # ---- 安全门禁 ----
    # 工具执行前的强制人工确认(高风险操作必须用户批准,模型无法绕过)
    agent_confirm_enabled: bool = True
    # 等待用户确认的超时秒数;超时按拒绝处理(阻塞轮询模式)
    agent_confirm_timeout: int = 300
    # 确认流采用 interrupt 式(对齐 LangGraph interrupt 语义):
    # 遇到需确认的工具时,图暂停并落盘,请求立即返回控制权(run_id + 待确认项);
    # 用户批准后通过 resume(run_id, resume_value) 从原节点恢复执行,不再占用请求阻塞轮询。
    agent_confirm_interrupt: bool = False

    # ---- Guardrail 护栏(输入/输出并行校验 + Prompt 注入防护) ----
    # 开启后,进入主循环前对用户消息做注入/越权检测,返回前对答案做敏感信息检测。
    # 拦截时返回固定安全文案;违规记录在 steps(type=guardrail)。
    agent_guardrails_enabled: bool = True

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


settings = Settings()
