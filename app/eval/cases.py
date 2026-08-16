"""黄金用例回归集:衡量 Agent 工具调用与任务完成的真实准确率。

对标 BFCL 思路,但跑在本仓库真实注册器上;需要真实 LLM(联网)。
不并入 pytest 默认执行;用 `python -m app.eval.runner` 手动运行。

每个用例断言两类指标:
- expect_tool:    任务应调用的工具是否被正确选中并执行(工具路由核心指标)
- expect_keyword: 最终回答是否包含关键结果(端到端完成度)
- tool_after:     (可选)在 expect_tool 调用之后,还应继续调用的工具(多步任务)

用例覆盖:基础/记忆/联网/浏览器/数据库/图表/文件/代码/知识库/多智能体/规划。
"""
from __future__ import annotations

EVAL_CASES: list[dict] = [
    # ---- 基础 ----
    {
        "id": "time",
        "category": "basic",
        "user_input": "今天是几号?",
        "expect_tool": "get_current_time",
        "expect_keyword": "20",
    },
    {
        "id": "calc",
        "category": "basic",
        "user_input": "计算 23 乘以 7 等于多少",
        "expect_tool": "calculate",
        "expect_keyword": "161",
    },
    {
        "id": "days_between",
        "category": "basic",
        "user_input": "从今天到 2026 年 12 月 31 日还有多少天?",
        "expect_tool": "days_between",
        "expect_keyword": None,
    },
    # ---- 记忆 ----
    {
        "id": "memory",
        "category": "memory",
        "user_input": "请记住我喜欢的颜色是蓝色",
        "expect_tool": "memory_save",
        "expect_keyword": None,
    },
    {
        "id": "memory_search",
        "category": "memory",
        "user_input": "我在记忆里存过最喜欢的颜色,查一下是什么",
        "expect_tool": "memory_search",
        "expect_keyword": None,
    },
    # ---- 网络/浏览器 ----
    {
        "id": "web",
        "category": "web",
        "user_input": "抓取网页 https://example.com 的内容",
        "expect_tool": "fetch_webpage",
        "expect_keyword": None,
    },
    {
        "id": "browser",
        "category": "browser",
        "user_input": "打开浏览器访问 https://www.baidu.com",
        "expect_tool": "browser_open",
        "expect_keyword": None,
    },
    # ---- 数据库 ----
    {
        "id": "db",
        "category": "db",
        "user_input": "查询 data/app.db 里 users 表的前 5 条记录",
        "expect_tool": "query_sqlite",
        "expect_keyword": None,
    },
    # ---- 图表 ----
    {
        "id": "chart",
        "category": "chart",
        "user_input": "生成一张柱状图展示各月份销售额",
        "expect_tool": "generate_bar_chart",
        "expect_keyword": None,
    },
    # ---- 文件 ----
    {
        "id": "file_write",
        "category": "file",
        "user_input": "把内容 'hello agent' 写入文件 demo.txt",
        "expect_tool": "write_file",
        "expect_keyword": None,
    },
    {
        "id": "file_read",
        "category": "file",
        "user_input": "读取文件 README.md 并总结第一句话",
        "expect_tool": "read_document",
        "expect_keyword": None,
    },
    # ---- 代码 ----
    {
        "id": "code",
        "category": "code",
        "user_input": "用 Python 计算 1 到 100 所有偶数的和",
        "expect_tool": "run_code",
        "expect_keyword": "2550",
    },
    # ---- 知识库 ----
    {
        "id": "rag",
        "category": "rag",
        "user_input": "根据知识库,回答项目里定义了哪些智能体行为开关",
        "expect_tool": "rag_search",
        "expect_keyword": None,
    },
    # ---- 多智能体 ----
    {
        "id": "roles",
        "category": "multi_agent",
        "user_input": "团队里有哪些角色可以委派?列一下",
        "expect_tool": "list_roles",
        "expect_keyword": None,
    },
    {
        "id": "handoff",
        "category": "multi_agent",
        "user_input": "让研究员角色调研一下什么是 RAG(检索增强生成),一句话总结",
        "expect_tool": "handoff_to_role",
        "expect_keyword": None,
    },
    # ---- 规划(Plan-then-Execute) ----
    {
        "id": "plan",
        "category": "planning",
        "user_input": "先制定一个三步计划:读取 README、统计工具数量、总结用途",
        "expect_tool": "set_plan",
        "expect_keyword": None,
    },
]
