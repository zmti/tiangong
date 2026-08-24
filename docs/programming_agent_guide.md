"""具有编程能力的智能体实现指南。

本文件描述从零搭建一个类似 Claude Code / Devin 的编程 Agent 的完整架构，
以及如何在当前 agent-service 代码基础上扩展。

核心思路:
    用户问题 → [规划] → 工具调用循环 (ReAct) → [反思/验证] → 最终答案
                     ↑
                代码执行 / 文件编辑 / 构建测试
"""

# ==============================================================================
# 一、系统架构总览
# ==============================================================================
#
# ┌─────────────────────────────────────────────────────────────────┐
# │                        User Input                               │
# └─────────────────────┬───────────────────────────────────────────┘
#                       ▼
# ┌─────────────────────────────────────────────────────────────────┐
# │ ① Prepare (路由 + 记忆注入 + 工具预选)                          │
# │    - LLM 判断任务复杂度，生成计划                                 │
# │    - 检索长期记忆 + RAG 知识库                                   │
# │    - 从 120+ 工具中预选相关子集                                   │
# └─────────────────────┬───────────────────────────────────────────┘
#                       ▼
# ┌─────────────────────────────────────────────────────────────────┐
# │ ② LLM (大模型推理)                                              │
# │    - 输入: 系统提示 + 历史消息 + 工具列表 + 上下文               │
# │    - 输出: 工具调用 或 最终答案                                  │
# └─────────────────────┬───────────────────────────────────────────┘
#                       ▼
# ┌─────────────────────────────────────────────────────────────────┐
# │ ③ Tools (工具执行)                                              │
# │    编程能力核心:                                                │
# │    ├─ run_code      : 沙箱执行 Python                           │
# │    ├─ run_terminal  : 本地终端执行命令                          │
# │    ├─ edit_file     : 原子化局部编辑                            │
# │    ├─ write_file    : 新建/覆写文件                             │
# │    ├─ code_search   : 全文 grep 搜索                           │
# │    ├─ code_symbols  : AST 符号提取                             │
# │    ├─ lsp_hover     : 悬停查类型/文档                           │
# │    ├─ lsp_references: 查所有引用位置                            │
# │    ├─ run_build     : 自动检测并执行构建                         │
# │    ├─ run_test      : 自动检测并执行测试                         │
# │    └─ git_*         : 提交/分支/PR 工作流                       │
# └─────────────────────┬───────────────────────────────────────────┘
#                       ▼
# ┌─────────────────────────────────────────────────────────────────┐
# │ ④ Finalize (最终答案生成)                                       │
# │    - 整合所有工具结果，生成最终回答                               │
# │    - 可选: 反思(reflect) + 事实校验(verify)                      │
# └─────────────────────────────────────────────────────────────────┘

# ==============================================================================
# 二、核心组件详解
# ==============================================================================

"""
## 2.1 工具设计原则

每个编程工具需要满足:

1. **幂等性**: 相同输入产生相同输出（除写入操作外）
2. **错误可恢复**: 失败时返回结构化错误，Agent 能据此调整策略
3. **沙箱隔离**: 代码执行不污染宿主环境
4. **结果截断**: 大输出需截断 + 提示，避免撑爆上下文
5. **超时报错**: 防止死循环阻塞

当前系统的工具已经满足这些要求:
- run_code: Docker/进程沙箱，超时 kill，输出截断 6000 字
- edit_file: 原子性（多个操作同时成功或全部回滚）
- code_search: 跳过 .git/node_modules，限制文件大小
"""


# ==============================================================================
# 三、编程任务典型流程
# ==============================================================================
"""
以 "帮我把项目里的所有 print() 改成 logging.info()" 为例:

Step 1: [LLM] 规划
  → 识别任务类型: 代码修改
  → 决定工具: code_search → edit_file → run_test

Step 2: [LLM] 调用 code_search(query="print(")
  → 返回 50 个匹配位置

Step 3: [LLM] 分析结果，决定修改策略
  → 方案 A: 逐个文件用 edit_file
  → 方案 B: 生成脚本用 run_code 批量替换

Step 4: [Agent] 执行 edit_file 或 run_code

Step 5: [LLM] 调用 run_test 验证修改未破坏测试

Step 6: [LLM] 调用 git diff 确认变更内容

Step 7: [Finalize] 汇总结果，输出报告
"""

# ==============================================================================
# 四、关键设计决策
# ==============================================================================
"""
## 4.1 代码执行隔离层级

| 场景          | 隔离方式          | 适用模型要求              |
|--------------|------------------|-------------------------|
| 学习/演示      | subprocess 本地   | 任何模型                  |
| 生产环境       | Docker 容器       | 需安装 Docker             |
| 强隔离（推荐）  | 虚拟机/K8s Pod   | 需 kubectl 配置           |

当前系统: Docker 优先，本地降级。配置项: AGENT_SANDBOX_MODE

## 4.2 工具路由（避免上下文爆炸）

120 个工具全量注入会耗尽上下文且降低调用精度。

解决方案: 每轮对话前用轻量 LLM 从工具列表中预选 Top-K:
  - "代码任务" → 只选 code_*/lsp_*/git_*
  - "数据分析" → 只选 run_code/query_sqlite
  - "文件操作" → 只选 search_files/read_document/edit_file

当前实现: app/tools/categories.py + AGENT_TOOL_ROUTER_ENABLED

## 4.3 错误自愈（Self-Heal）

代码执行失败时的标准处理流程:

  失败
    ↓
  [LLM] 分析错误原因（语法错误？依赖缺失？路径错误？）
    ↓
  [LLM] 生成修复补丁
    ↓
  [Agent] 应用补丁 + 重新执行
    ↓
  重试 N 次后放弃

当前实现: AGENT_CODE_SELF_HEAL=true, AGENT_CODE_HEAL_ROUNDS=2

## 4.4 上下文预算控制

编程任务容易触发大量工具调用:

  - code_search 返回 500 行匹配
  - run_terminal 输出 8000 字符
  - edit_file 返回完整 diff

解决方案: 三层截断
  1. 工具输出截断: MAX_OUTPUT_CHARS = 8000
  2. 工具结果缓存: 相同调用复用结果（AGENT_TOOL_CACHE_SIZE=50）
  3. 上下文压缩: 超预算时用 LLM 摘要旧消息（AGENT_SUMMARIZE=true）
"""

# ==============================================================================
# 五、新增工具建议
# ==============================================================================
"""
若要进一步提升编程能力，建议补充以下工具:

### 5.1 代码审阅 (code_review)
  输入: 文件路径 或 git diff
  输出: 结构化 Review 意见（缺陷/性能/安全/风格）

  实现思路:
    - 调用 LLM 对代码做静态分析
    - 按严重程度分级（critical/warning/style）
    - 支持行号定位

### 5.2 依赖管理 (manage_dependencies)
  输入: 操作类型 + 包名列表
  输出: 安装/卸载/更新结果

  支持:
    - pip install/uninstall/update
    - npm install/uninstall/update
    - poetry add/remove
    - go mod tidy

### 5.3 代码生成脚手架 (project_scaffold)
  输入: 项目类型 + 框架 + 语言
  输出: 生成完整项目结构

  实现: 模板目录 + Jinja2 渲染

### 5.4 PR 自动化 (automate_pr)
  输入: 任务描述
  输出: 自动 commit → push → 创建 PR → 填写标题和描述

  当前已有: git_push_and_create_pr（可扩展为端到端自动化）

### 5.5 调试辅助 (debug_assist)
  输入: 错误信息 + 相关文件
  输出: 可能的根因分析 + 修复建议

  实现:
    - 解析 traceback，定位出错行
    - 搜索相关代码上下文
    - 调用 LLM 分析根因
"""


# ==============================================================================
# 六、最小实现示例
# ==============================================================================
"""
以下是一个极简编程 Agent 的骨架（约 200 行）:
"""

# from typing import Optional
# import json
# import subprocess
# import tempfile
# import os
#
# class SimpleCodingAgent:
#     """极简编程 Agent 骨架"""
#
#     def __init__(self, llm_client, max_steps=15):
#         self.llm = llm_client
#         self.max_steps = max_steps
#         self.history: list[dict] = []
#         self.tool_results: dict[str, str] = {}  # step_id -> result
#
#     def run(self, task: str) -> str:
#         self.history.append({"role": "user", "content": task})
#
#         for step in range(self.max_steps):
#             # 1. LLM 决定下一步
#             response = self.llm.chat(self.history)
#
#             # 2. 解析工具调用
#             tool_call = self._parse_tool_call(response)
#             if tool_call is None:
#                 # 无工具调用 = 最终答案
#                 return response
#
#             # 3. 执行工具
#             result = self._execute_tool(tool_call)
#             step_id = f"step_{step}"
#             self.tool_results[step_id] = result
#
#             # 4. 记录工具结果
#             self.history.append({
#                 "role": "tool",
#                 "tool_call_id": step_id,
#                 "content": result,
#             })
#
#         return f"达到最大步数上限({self.max_steps})，未完成任务"
#
#     def _parse_tool_call(self, text: str) -> Optional[dict]:
#         """从 LLM 输出解析工具调用（简化版，实际用 JSON Schema 解析）"""
#         try:
#             # 假设 LLM 输出 JSON: {"tool": "...", "args": {...}}
#             return json.loads(text)
#         except json.JSONDecodeError:
#             return None
#
#     def _execute_tool(self, tool_call: dict) -> str:
#         """执行工具（简化版）"""
#         tool_name = tool_call.get("tool", "")
#         args = tool_call.get("args", {})
#
#         if tool_name == "run_code":
#             return self._run_python_code(args.get("code", ""))
#         elif tool_name == "read_file":
#             return self._read_file(args.get("path", ""))
#         elif tool_name == "write_file":
#             return self._write_file(args.get("path", ""), args.get("content", ""))
#         else:
#             return f"未知工具: {tool_name}"
#
#     def _run_python_code(self, code: str) -> str:
#         """在沙箱中执行 Python 代码"""
#         with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
#             f.write(code)
#             tmp_path = f.name
#         try:
#             result = subprocess.run(
#                 ['python', tmp_path],
#                 capture_output=True,
#                 text=True,
#                 timeout=30,
#             )
#             return f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\nreturncode: {result.returncode}"
#         finally:
#             os.unlink(tmp_path)
#
#     def _read_file(self, path: str) -> str:
#         try:
#             with open(path, 'r', encoding='utf-8') as f:
#                 return f.read()[:5000]
#         except Exception as e:
#             return f"读取失败: {e}"
#
#     def _write_file(self, path: str, content: str) -> str:
#         try:
#             os.makedirs(os.path.dirname(path), exist_ok=True)
#             with open(path, 'w', encoding='utf-8') as f:
#                 f.write(content)
#             return f"已写入: {path} ({len(content)} 字符)"
#         except Exception as e:
#             return f"写入失败: {e}"


# ==============================================================================
# 七、测试策略
# ==============================================================================
"""
编程 Agent 的测试比通用 Agent 更严格，因为输出需要可验证:

## 7.1 单元测试（工具层面）
  - run_code: 传入正确代码 → 验证输出正确
  - edit_file: 传入有效的 old/new → 验证文件内容改变
  - code_search: 传入已知关键词 → 验证返回文件列表

## 7.2 集成测试（端到端）
  - "修复这个 bug" → 验证代码被修改且测试通过
  - "实现一个斐波那契函数" → 验证文件被创建且可运行

## 7.3 LLM 评测（质量层面）
  - HumanEval / MBPP: 代码生成准确率
  - SWE-bench: 真实 GitHub Issue 修复率

## 7.4 安全测试
  - 注入攻击: 用户输入包含恶意指令时是否被拦截
  - 权限越界: 是否能写到 /etc 或系统目录
  - 资源耗尽: 死循环代码是否被超时终止
"""

# ==============================================================================
# 八、当前系统的不足与改进方向
# ==============================================================================
"""
## 8.1 当前不足

1. **代码理解停留在正则层面**
   - code_symbols 用正则提取函数名，无法区分重载/泛型/闭包
   - 改进: 集成 tree-sitter 做真正的 AST 解析

2. **无 LSP 支持**
   - 无法跳转定义、查引用、获取类型信息
   - 改进: 实现 LSP 客户端（已新增 lsp_tool.py）

3. **编辑是文本替换，非结构感知**
   - edit_file 的 old/new 匹配失败会导致改坏代码
   - 改进: 基于 AST 的结构性编辑（如 libcst）

4. **无构建感知**
   - Agent 修改代码后不会自动 build/test
   - 改进: 新增 build_tool.py（已完成）

5. **无版本控制语义**
   - 不知道当前在哪个分支、是否有未提交的改动
   - 改进: 扩展 git 工具链（已新增 git_pr_tool.py）

## 8.2 改进优先级

P0（本周可完成）:
  ✓ 增强 code_symbols（多行签名/注释过滤）
  ✓ 新增 LSP 工具（hover/references/definition）
  ✓ 新增构建感知工具（detect_build_system/run_build/run_test）

P1（下周可完成）:
  - 集成 tree-sitter 做真正的 AST 解析
  - 实现 AST 级别的代码编辑
  - 添加代码review工具

P2（中长期）:
  - 多 Agent 协作（一个负责写代码，一个负责测试）
  - 支持 VS Code 插件集成（直接操作编辑器）
  - 实现 Web UI 实时预览代码变更 diff
"""
