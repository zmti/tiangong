"""工具执行共享常量:瞬态错误检测、测试失败特征、工具分类。

避免 agent.py 与 tool_executor.py 各自维护一份导致行为不一致。
新增工具分类请同步更新 app/tools/categories.py。
"""

# 瞬态错误特征(命中即判定为重试):网络/超时/限流/服务不可用
TRANSIENT_PATTERNS = (
    "timeout", "超时", "timed out", "connection", "connect error", "连接",
    "网络", "temporarily", "try again later", "rate limit", "限流", "429",
    "502", "503", "504", "service unavailable", "server error", "busy",
    "忙碌", "too many requests", "慢", "slow", "overloaded", "network",
    "temporarily unavailable", "try again",
)

# 测试/构建失败特征(用于给出定向修复提示)
TEST_FAIL_MARKERS = (
    "failed", "failures", "traceback", "assertionerror", "error:",
    "exit code", "npm err", "fatal error", "test failed", "x failed",
    "failures=", "tests failed", "cannot find", "undefined reference",
    "syntaxerror", "importerror", "nameerror", "typeerror", "valueerror",
)

# 不参与瞬态错误自动重试的工具:
# - run_terminal: 超时的终端命令不应被盲目重跑(可能有副作用)
# - 文件操作: 写操作非幂等,重试可能导致数据不一致
NO_RETRY_TOOLS = frozenset({
    "run_terminal",
    "read_file", "write_file", "edit_file",
})

# ---- 上下文超长错误的判定(agent.py 与 agent_impl.py 共用) ----
# 白名单式特征词,避免误伤其他错误。
CONTEXT_LENGTH_MARKERS = (
    "context_length", "context length", "context window", "context limit",
    "maximum context", "max context", "too long",
    "reduce the length", "maximum token", "token limit exceeded",
)

# 限流/配额类错误特征:命中即排除"上下文超长"判定。
# 上游 429 原文 "You've reached the API rate limit for free users. Upgrade to a
# Token Plan to unlock higher limits..." 同时含 "token" 与 "limit";旧写法
# `"token" in err and ("limit" in err or "exceed" in err)` 会把它误判成上下文超长
# → 白白修剪历史消息再打一次 LLM(2026-09-28 实测于 backend.log)。
RATE_LIMIT_MARKERS = (
    "rate limit", "429", "too many requests", "频率", "限流", "请求过快", "配额", "quota",
)


def is_context_length_error(error: str) -> bool:
    """判断是否为"上下文超长"错误。

    先排除限流/配额类错误(其文案里也可能出现 token/limit 字样),再做特征词匹配。
    """
    err = (error or "").lower()
    if any(m in err for m in RATE_LIMIT_MARKERS):
        return False
    return any(m in err for m in CONTEXT_LENGTH_MARKERS)

