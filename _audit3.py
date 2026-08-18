"""Third round deep audit: find remaining bugs."""
import os, re, ast

issues = []

# 1. stop_webhook_queue_drainer: 不 await 取消
path = 'app/core/webhook.py'
with open(path) as f:
    src = f.read()
if 'def stop_webhook_queue_drainer' in src:
    # 检查是否有 await _drain_task
    if 'await' not in src[src.find('def stop_webhook_queue_drainer'):src.find('def stop_webhook_queue_drainer')+200]:
        issues.append(f"[中危] {path}: stop_webhook_queue_drainer 不 await 取消，任务可能泄漏")

# 2. MCPManager.reset() 不关闭已有客户端
path = 'app/core/mcp_client.py'
with open(path) as f:
    src = f.read()
if 'def reset' in src:
    idx = src.find('def reset')
    end = src.find('\n    def ', idx+1)
    body = src[idx:end]
    if 'close' not in body and 'terminate' not in body:
        issues.append(f"[中危] {path}: MCPManager.reset() 不清理已有客户端后台任务")
    if 'remove_client' in body:
        pass
# Check remove_client too
idx2 = src.find('def remove_client')
if idx2 > 0:
    end2 = src.find('\n    def ', idx2+1)
    body2 = src[idx2:end2]
    if 'close' not in body2:
        issues.append(f"[中危] {path}: MCPManager.remove_client() 不清理客户端后台任务")

# 3. MCP reconnect: close() 未设 auto_reconnect=False
path = 'app/core/mcp_stdio.py'
with open(path) as f:
    src = f.read()
close_start = src.find('async def close')
close_end = src.find('async def _reconnect_loop', close_start)
close_body = src[close_start:close_end]
if 'auto_reconnect' not in close_body:
    issues.append(f"[中危] {path}: close() 未设 auto_reconnect=False，重连循环可能竞态")

# 4. arun_stream: yield 后无 cancel 检查(低危)
path = 'app/runtime/graph.py'
with open(path) as f:
    src = f.read()
# 检查 arun_stream 中 yield 之后是否紧接着有 cancel check
arun_stream = src[src.find('async def arun_stream'):src.find('async def aresume')]
if 'yield e' in arun_stream:
    # 看 yield 之后是否有 cancel check
    lines = arun_stream.split('\n')
    for i, line in enumerate(lines):
        if 'yield e' in line:
            # 检查后续几行有没有 cancel
            rest = '\n'.join(lines[i+1:i+5])
            if 'cancel_check' not in rest:
                issues.append(f"[低危] {path}: arun_stream yield 后无 cancel 检查，延迟响应取消")
            break

# 5. Webhook _sign 使用 hmac.new (py3.13 兼容)
path = 'app/core/webhook.py'
with open(path) as f:
    src = f.read()
if 'hmac.new(' in src.lower() or "hmac.new(" in src:
    issues.append(f"[兼容] {path}: hmac.new 在 py3.13 应确认大小写正确")

# 6. 检查 Agent.run_stream 中 cancel check 时机
path = 'app/core/agent.py'
with open(path) as f:
    src = f.read()
run_stream = src[src.find('async def run_stream'):src.find('async def resume', src.find('async def run_stream'))]
# 检查 arun_stream 调用前后是否有 cancel
if 'arun_stream' in run_stream:
    # 应该在 arun_stream 内部检查 cancel，而 agent.run_stream 没有额外检查
    issues.append(f"[信息] {path}: run_stream 依赖 graph.arun_stream 的 cancel check，无双重检查")

# 7. tasks.py _drive: cancel 检查只在外层
path = 'app/core/tasks.py'
with open(path) as f:
    src = f.read()
# 检查 _drive 中 cancel_flag 的使用
if '_cancel_flags[task_id].is_set()' in src:
    issues.append(f"[信息] {path}: _drive 中 cancel 检查仅在外层循环，LLM 调用期间不响应取消")

# 8. 检查 memory store 中 sqlite 连接是否用 with
for root, dirs, files in os.walk('app/memory'):
    for f in files:
        if not f.endswith('.py'): continue
        path = os.path.join(root, f)
        with open(path) as fh:
            src = fh.read()
        if 'sqlite3.connect' in src:
            # 检查是否有 with 语句
            conn_lines = [l for l in src.split('\n') if 'sqlite3.connect' in l]
            for cl in conn_lines:
                if 'with ' not in cl and 'conn.close()' not in cl:
                    # 可能是 with 块内的
                    pass

# 9. 检查全局单例线程安全
for root, dirs, files in os.walk('app'):
    for f in files:
        if not f.endswith('.py'): continue
        path = os.path.join(root, f)
        with open(path) as fh:
            src = fh.read()
        # 查找无锁的全局 dict 写入
        matches = re.findall(r'^_instance\s*=\s*...', src, re.MULTILINE)
        for m in matches:
            if 'lock' not in src[src.find(m)-200:src.find(m)+len(m)+100]:
                issues.append(f"[低危] {path}: 全局单例 {_instance} 可能无锁保护")

print("=" * 60)
print(f"发现 {len(issues)} 个问题:")
print("=" * 60)
for i, iss in enumerate(issues, 1):
    print(f"  {i}. {iss}")
