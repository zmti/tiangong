"""工具执行共享常量:瞬态错误检测、测试失败特征。

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
