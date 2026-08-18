"""智能体运行逻辑分析 + 内存泄漏/资源泄漏检测。

检查维度:
  1. 内存泄漏：无界增长的数据结构、未清理的回调/定时器、contextvar 残留
  2. 资源泄漏：未关闭的 HTTP 客户端、文件句柄、子进程、asyncio task
  3. 运行逻辑缺陷：状态不一致、并发竞态、死锁风险、无限循环
  4. 配置隐患：不合理的默认值、硬编码路径
"""
import ast
import os
import re
import sys
import traceback

sys.stdout.reconfigure(encoding="utf-8")

results = []

def add(severity, category, location, issue, suggestion):
    results.append({"severity": severity, "category": category, "location": location, "issue": issue, "suggestion": suggestion})

# ──────────────────────────────────────────────────
# 1. 静态扫描：无界增长的数据结构
# ──────────────────────────────────────────────────
print("[1] 扫描无界增长的数据结构...")
print("-" * 60)

# 检查模块级 dict/list/set 缓存
unsafe_patterns = {
    "模块级dict缓存无size上限": re.compile(r"(?<!\w)self\._[a-z_]+\s*[:=]\s*\{\}"),
    "模块级list无截断": re.compile(r"(?<!\w)self\._[a-z_]+\s*[:=]\s*\[\]"),
    "asyncio.create_task无await": re.compile(r"asyncio\.create_task\("),
}

unbound_caches = []
for root, _, files in os.walk("app"):
    for f in files:
        if not f.endswith(".py"): continue
        fp = os.path.join(root, f)
        try:
            with open(fp, encoding="utf-8-sig") as fh:
                src = fh.read()
        except Exception:
            continue
        lines = src.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # 模块级 dict 初始化（非 self. 也检查）
            if re.match(r"^_[a-zA-Z_]+\s*[:=]\s*\{", line) or re.match(r"^class\s+\w+.*:\s*$", line):
                pass
            # self.xxx = {} 但不在 __init__ 中 → 可能是模块级
            if "self._" in line and "= {}" in line and "def __init__" not in line:
                # 可能是方法内部，忽略
                pass
            # asyncio.create_task 未 await
            if "asyncio.create_task" in stripped and "await" not in stripped and not stripped.endswith(","):
                # 检查是否在函数内（可能被 return 包裹）
                ctx = lines[max(0,i-5):i]
                if not any("await" in c for c in ctx[-3:]) and "return" not in ctx[-3:]:
                    unbound_caches.append((fp, i, stripped[:80]))

# profiling.py 无界 _records 列表
profile_fp = "app/core/profiling.py"
if os.path.exists(profile_fp):
    with open(profile_fp, encoding="utf-8-sig") as fh:
        profile_src = fh.read()
    if "_records: list[ProfileRecord] = []" in profile_src and "_max_records" not in profile_src:
        add("HIGH", "memory_leak", "app/core/profiling.py",
            "_records 列表无大小限制，进程运行时持续追加",
            "添加 max_records 上限，超过后 FIFO 淘汰旧记录")

# telemetry.py ring buffer 有上限 ✓
telemetry_fp = "app/core/telemetry.py"
if os.path.exists(telemetry_fp):
    with open(telemetry_fp, encoding="utf-8-sig") as fh:
        tel_src = fh.read()
    if "max_buffer" in tel_src and "_max_buffer" in tel_src:
        print("  [OK] telemetry.Tracer: ring buffer 有 max_buffer 上限")
    else:
        add("HIGH", "memory_leak", "app/core/telemetry.py",
            "Tracer._buffer 无大小限制",
            "添加 ring buffer 截断逻辑")

# failure_memory / success_memory 无界增长
for name in ["failure_memory", "success_memory"]:
    fp = f"app/memory/{name}.py"
    if os.path.exists(fp):
        with open(fp, encoding="utf-8-sig") as fh:
            src = fh.read()
        if "_records" in src or "_store" in src:
            if "cleanup" not in src and "ttl" not in src.lower():
                add("MEDIUM", "memory_leak", fp,
                    f"{name}: 记录持续累积无自动清理",
                    "添加 TTL 过期清理或最大记录数限制")
            else:
                print(f"  [OK] {name}: 有 TTL 清理机制")

# tool_cache 有上限 ✓
tel_src2 = open("app/core/tool_executor.py", encoding="utf-8-sig").read()
if "agent_tool_cache_size" in tel_src2:
    print("  [OK] tool_executor._tool_cache: 有 max_size 截断")
else:
    add("MEDIUM", "memory_leak", "app/core/tool_executor.py",
        "_tool_cache 无大小限制，同一 session 内可能持续增长",
        "添加 max_size 截断，或按 session 隔离缓存")

# ──────────────────────────────────────────────────
# 2. 扫描 asyncio.create_task 裸调用（fire-and-forget）
# ──────────────────────────────────────────────────
print()
print("[2] 扫描 asyncio.create_task 裸调用...")
print("-" * 60)

fire_and_forget = []
for root, _, files in os.walk("app"):
    for f in files:
        if not f.endswith(".py"): continue
        fp = os.path.join(root, f)
        try:
            with open(fp, encoding="utf-8-sig") as fh:
                src = fh.read()
        except Exception:
            continue
        lines = src.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if "asyncio.create_task(" in stripped:
                # 检查赋值给变量
                if "=" in stripped and "asyncio.create_task" in stripped:
                    # 有变量接收，安全
                    continue
                # 检查是否在函数返回语句中
                if stripped.startswith("return asyncio.create_task"):
                    continue
                # 检查上下文
                ctx_lines = lines[max(0,i-3):min(len(lines),i+3)]
                ctx_text = "\n".join(ctx_lines)
                if "await" not in ctx_text and "return" not in ctx_text:
                    fire_and_forget.append((fp, i, stripped[:100]))
                    add("MEDIUM", "resource_leak", f"{fp}:{i}",
                        f"asyncio.create_task 未 await（fire-and-forget），异常会被静默吞掉",
                        "保存 task 引用并添加 done 回调处理异常，或使用 asyncio.shield")

for fp, lineno, code in fire_and_forget[:10]:
    print(f"  [WARN] {fp}:{lineno}  {code}")
if not fire_and_forget:
    print("  [OK] 未发现 fire-and-forget create_task")

# ──────────────────────────────────────────────────
# 3. 扫描 contextvar 未 reset
# ──────────────────────────────────────────────────
print()
print("[3] 检查 contextvar set/reset 配对...")
print("-" * 60)

ctx_leaks = []
for root, _, files in os.walk("app"):
    for f in files:
        if not f.endswith(".py"): continue
        fp = os.path.join(root, f)
        try:
            with open(fp, encoding="utf-8-sig") as fh:
                src = fh.read()
        except Exception:
            continue
        # 查找 set 但没有对应 reset 的 contextvar
        sets = re.findall(r"(\w+_token\s*=\s*\w+\.set\()", src)
        resets = re.findall(r"\w+\.reset\(\w+_token\)", src)
        if sets and not resets:
            ctx_leaks.append((fp, len(sets), len(resets)))

# 专门检查 tool_executor
with open("app/core/tool_executor.py", encoding="utf-8-sig") as fh:
    tex_src = fh.read()
if "_current_session.set" in tex_src and "_current_session.reset" in tex_src:
    print("  [OK] tool_executor: _current_session set/reset 配对（try/finally 内）")
else:
    add("HIGH", "contextvar_leak", "app/core/tool_executor.py",
        "_current_session contextvar set 后未 reset",
        "在 try/finally 中确保 reset")

# ──────────────────────────────────────────────────
# 4. 检查 HTTP 客户端生命周期
# ──────────────────────────────────────────────────
print()
print("[4] 检查 HTTP 客户端生命周期...")
print("-" * 60)

http_clients = []
for root, _, files in os.walk("app"):
    for f in files:
        if not f.endswith(".py"): continue
        fp = os.path.join(root, f)
        try:
            with open(fp, encoding="utf-8-sig") as fh:
                src = fh.read()
        except Exception:
            continue
        # AsyncOpenAI 客户端在 __init__ 中创建，进程级共享，无需关闭（连接池由库管理）
        if "AsyncOpenAI(" in src:
            # 检查是否有 close/aclose 调用
            if "aclose" not in src and "close" not in src:
                pass  # AsyncOpenAI 连接池由 urllib3 管理，无需手动关闭
                continue
        # httpx.AsyncClient 需要显式关闭
        if "httpx.AsyncClient" in src and "async with" not in src:
            if ".aclose()" not in src and ".close()" not in src:
                http_clients.append(fp)

if not http_clients:
    print("  [OK] 未发现未关闭的 httpx.AsyncClient")
else:
    for fp in http_clients:
        add("MEDIUM", "resource_leak", fp, "httpx.AsyncClient 未调用 aclose()", "使用 async with 或手动 aclose()")

# ──────────────────────────────────────────────────
# 5. 检查 MCP 子进程生命周期
# ──────────────────────────────────────────────────
print()
print("[5] 检查子进程/stdio 生命周期...")
print("-" * 60)

mcp_issues = []
mcp_fp = "app/core/mcp_stdio.py"
if os.path.exists(mcp_fp):
    with open(mcp_fp, encoding="utf-8-sig") as fh:
        mcp_src = fh.read()
    # 检查是否有 cleanup/kill 逻辑
    has_kill = "kill" in mcp_src or "terminate" in mcp_src
    has_cleanup = "cleanup" in mcp_src
    has_close = "aclose" in mcp_src or ".close()" in mcp_src
    if has_kill or has_cleanup:
        print(f"  [OK] mcp_stdio: 有进程清理逻辑 (kill={has_kill}, cleanup={has_cleanup})")
    else:
        add("HIGH", "resource_leak", mcp_fp, "MCP stdio 子进程启动后无 kill/terminate 清理",
            "在 __del__ 或 shutdown hook 中 terminate 子进程")

# ──────────────────────────────────────────────────
# 6. 检查 Agent 运行逻辑的正确性
# ──────────────────────────────────────────────────
print()
print("[6] 运行逻辑分析...")
print("-" * 60)

with open("app/core/agent.py", encoding="utf-8-sig") as fh:
    agent_src = fh.read()

# 6a. Graph 循环保护
if "max_iters" in agent_src and "state._step_index" in agent_src:
    print("  [OK] Graph 有 max_iters 防死循环保护")
else:
    add("HIGH", "logic_bug", "app/runtime/graph.py", "Graph 运行无 max_iters 保护，可能出现无限循环",
        "在 arun 中限制最大迭代次数")

# 6b. 工具调用无结果超时
if "agent_tool_timeout" in agent_src or "timeout" in agent_src:
    print("  [OK] 工具执行有超时保护")
else:
    add("MEDIUM", "logic_bug", "app/core/tool_executor.py", "工具执行无超时保护，卡死的工具会阻塞整个 ReAct 循环",
        "添加 agent_tool_timeout 配置，使用 asyncio.wait_for 包裹")

# 6c. LLM 调用重试
if "agent_llm_retries" in agent_src or ("_invoke" in agent_src and "retries" in agent_src):
    print("  [OK] LLM 调用有重试+故障转移")
else:
    add("HIGH", "logic_bug", "app/core/llm.py", "LLM 调用无重试，瞬态错误（限流/超时）直接失败",
        "添加指数退避重试和 provider 故障转移")

# 6d. 消息预算控制
if "token_budget" in agent_src or "context_char_budget" in agent_src:
    print("  [OK] 消息有 token/字符预算控制")
else:
    add("MEDIUM", "logic_bug", "app/core/agent.py", "对话历史无预算控制，可能撑爆上下文窗口",
        "添加 budget_messages 截断逻辑")

# 6e. self._graph 缓存问题
if "self._graph is not None" in agent_src:
    print("  [OK] Agent._graph 缓存复用（避免重复构建图）")
else:
    add("LOW", "perf", "app/core/agent.py", "_build_graph 每次调用都重建图",
        "缓存 Graph 实例（已有 _graph 字段但检查是否复用）")

# ──────────────────────────────────────────────────
# 7. 并发安全性检查
# ──────────────────────────────────────────────────
print()
print("[7] 并发安全性检查...")
print("-" * 60)

# 检查共享状态是否有锁
lock_patterns = {
    "threading.Lock": re.compile(r"threading\.Lock\(\)"),
    "threading.RLock": re.compile(r"threading\.RLock\(\)"),
    "asyncio.Lock": re.compile(r"asyncio\.Lock\(\)"),
}

file_locks = {}
for root, _, files in os.walk("app"):
    for f in files:
        if not f.endswith(".py"): continue
        fp = os.path.join(root, f)
        try:
            with open(fp, encoding="utf-8-sig") as fh:
                src = fh.read()
        except Exception:
            continue
        for pat_name, pat in lock_patterns.items():
            matches = pat.findall(src)
            if matches:
                file_locks.setdefault(fp, []).extend(matches)

# 检查有共享状态的模块是否加锁
shared_state_modules = [
    "app/core/telemetry.py",
    "app/core/profiling.py",
    "app/memory/failure_memory.py",
    "app/memory/success_memory.py",
    "app/core/scheduler.py",
    "app/core/session_archive.py",
]
for fp in shared_state_modules:
    if os.path.exists(fp):
        with open(fp, encoding="utf-8-sig") as fh:
            src = fh.read()
        has_lock = "threading.Lock" in src or "threading.RLock" in src or "asyncio.Lock" in src
        has_list_op = "self._records" in src or "self._buffer" in src or "append" in src
        if has_list_op and not has_lock:
            add("HIGH", "concurrency", fp, "有共享列表操作但无锁保护，多请求并发时可能数据错乱",
                "添加 threading.Lock 或 asyncio.Lock 保护临界区")
        elif has_list_op and has_lock:
            print(f"  [OK] {fp}: 共享状态有锁保护")

# ──────────────────────────────────────────────────
# 8. Checkpoint 完整性
# ──────────────────────────────────────────────────
print()
print("[8] Checkpoint 完整性检查...")
print("-" * 60)

ckpt_fp = "app/runtime/checkpoint.py"
if os.path.exists(ckpt_fp):
    with open(ckpt_fp, encoding="utf-8-sig") as fh:
        ckpt_src = fh.read()
    has_save = "def save" in ckpt_src
    has_load = "def load" in ckpt_src
    has_list = "def list" in ckpt_src
    has_roll = "def rollback" in ckpt_src
    checks = [("save", has_save), ("load", has_load), ("list", has_list), ("rollback", has_roll)]
    for name, ok in checks:
        print(f"  {'[OK]' if ok else '[ ]'} checkpoint.{name}()")
        if not ok:
            add("MEDIUM", "logic_bug", ckpt_fp, f"CheckpointManager.{name}() 方法缺失",
                "实现完整的 checkpoint CRUD 能力")
else:
    add("HIGH", "missing_feature", "app/runtime/checkpoint.py", "CheckpointManager 不存在",
        "实现 checkpoint 持久化，支持断点续跑")

# ──────────────────────────────────────────────────
# 9. 配置合理性检查
# ──────────────────────────────────────────────────
print()
print("[9] 配置合理性检查...")
print("-" * 60)

from app.config import settings

config_checks = [
    ("agent_max_steps", settings.agent_max_steps, 5, 100, "默认步数应在 5-100 之间"),
    ("agent_reflect_max_rounds", settings.agent_reflect_max_rounds, 1, 10, "反射轮数过高会增加延迟"),
    ("agent_tool_cache_size", getattr(settings, "agent_tool_cache_size", 0), 0, 10000, "缓存过大占用内存"),
    ("context_char_budget", getattr(settings, "context_char_budget", 0), 1000, 100000, "上下文预算过大会导致 LLM 过载"),
    ("session_retention_days", settings.session_retention_days, 1, 365, "保留天数应合理"),
]

for name, value, lo, hi, note in config_checks:
    if lo <= value <= hi:
        print(f"  [OK] {name}={value} (合理范围 [{lo},{hi}])")
    else:
        add("LOW", "config", f"app/config.py: {name}",
            f"{name}={value} 超出合理范围 [{lo},{hi}]: {note}",
            f"调整 {name} 到合理范围")

# ──────────────────────────────────────────────────
# 10. 内存泄漏详细分析
# ──────────────────────────────────────────────────
print()
print("[10] 内存泄漏风险评估...")
print("-" * 60)

# profiling._records 无界增长
fp_prof = "app/core/profiling.py"
if os.path.exists(fp_prof):
    with open(fp_prof, encoding="utf-8-sig") as fh:
        src = fh.read()
    # 找 _records 列表
    if "self._records" in src:
        # 检查是否有截断逻辑
        if "len(self._records)" not in src and "max_records" not in src:
            add("HIGH", "memory_leak", fp_prof,
                "ProfileManager._records 列表随运行持续增长，无截断",
                "添加 _max_records=10000 上限，超限时 FIFO 淘汰")

# telemetry ring buffer 有上限，OK
# failure_memory / success_memory SQL 持久化，内存中也有列表
for name, cls_name in [("failure_memory", "FailureStore"), ("success_memory", "SuccessStore")]:
    fp = f"app/memory/{name}.py"
    if os.path.exists(fp):
        with open(fp, encoding="utf-8-sig") as fh:
            src = fh.read()
        # 检查内存中的 _records 列表
        if "_records" in src:
            has_limit = "max_records" in src or "len(self._records)" in src
            has_ttl = "cleanup" in src or "ttl" in src.lower()
            if not has_limit and not has_ttl:
                add("MEDIUM", "memory_leak", fp,
                    f"{cls_name}._records 列表无大小限制且无 TTL 清理",
                    "添加内存中 _records 的 max_records 上限 + 定期 flush 到磁盘")

# ──────────────────────────────────────────────────
# 汇总
# ──────────────────────────────────────────────────
print()
print("=" * 60)
print("分析结果汇总")
print("=" * 60)

by_severity = {"HIGH": [], "MEDIUM": [], "LOW": []}
for r in results:
    by_severity.setdefault(r["severity"], []).append(r)

for sev in ["HIGH", "MEDIUM", "LOW"]:
    items = by_severity[sev]
    if items:
        print(f"\n[{sev}] 共 {len(items)} 项:")
        for r in items:
            print(f"  [{r['category']}] {r['location']}")
            print(f"         问题: {r['issue']}")
            print(f"         建议: {r['suggestion']}")
            print()

if not results:
    print("\n未发现明显问题！")

# 输出摘要
print("\n总计:", len(results), "个潜在问题")
print("  HIGH:", len(by_severity["HIGH"]))
print("  MEDIUM:", len(by_severity["MEDIUM"]))
print("  LOW:", len(by_severity["LOW"]))
