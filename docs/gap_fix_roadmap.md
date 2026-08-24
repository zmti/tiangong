# 智能体差距分析与修复路线图

> 生成时间: 2026-07-10  
> 分析范围: 核心架构 + 主流对标 (Claude/GPTs/Cursor/Devin)

---

## 📊 差距总览

| 类别 | 差距数 | 阻塞项 | 优先级分布 |
|------|--------|--------|------------|
| 核心架构 | 4 | 1 | P0×1, P1×2, P2×1 |
| 记忆系统 | 5 | 2 | P1×3, P2×2 |
| 工具系统 | 4 | 0 | P2×3, P3×1 |
| 安全合规 | 5 | 1 | P1×1, P2×4 |
| 可观测性 | 5 | 0 | P1×1, P2×4 |
| 用户体验 | 4 | 0 | P2×2, P3×2 |
| 工程化 | 4 | 0 | P2×3, P3×1 |

**总计**: 31项差距，其中阻塞性 4 项

---

## 🔴 P0 阻塞项（立即可修复）

### 1. 记忆冲突仲裁机制

**问题**: 多条矛盾记忆共存，无仲裁机制

**现状代码**:
```python
# app/memory/ltm.py - 仅追加，无合并
def save(self, content, keywords=None, ...):
    # 直接 INSERT，不检查是否已存在相似内容
```

**修复方案**:
1. 实现记忆相似度检测（余弦相似度 > 0.85 视为重复）
2. 新记忆与旧记忆冲突时，保留较新的并标记旧记忆为 deprecated
3. 添加记忆时效衰减字段 `decay_factor`

**预计工作量**: 2天

---

### 2. PII 数据泄露防护

**问题**: 无个人身份信息检测，可能导致隐私合规问题

**修复方案**:
```python
# app/core/pii_detector.py (新建)
import re
from presidio_analyzer import AnalyzerEngine

class PIIDetector:
    _patterns = {
        "email": r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
        "phone": r'[\+]?[0-9]{10,15}',
        "id_card": r'[0-9]{17}[0-9X]',  # 中国身份证
    }
    
    def detect(self, text: str) -> list[dict]:
        """返回检测到的 PII 列表"""
```

**预计工作量**: 1天

---

### 3. 性能监控 Dashboard

**问题**: 无实时监控面板，无法发现瓶颈

**修复方案**:
1. 集成 Prometheus metrics
2. 添加 FastAPI middleware 自动记录请求耗时
3. 创建 `/v1/metrics` 端点

```python
# app/api/metrics.py (增强)
from prometheus_client import Counter, Histogram, generate_latest

REQUEST_DURATION = Histogram('request_duration_seconds', '请求耗时')
TOKEN_USAGE = Counter('token_usage_total', 'Token 消耗', ['provider', 'model'])
```

**预计工作量**: 1.5天

---

## 🟡 P1 重要项（1-2周内）

### 4. 工具调用流式反馈

**问题**: 工具执行时用户看不到进度，只有等待最终结果

**现状代码**:
```python
# app/core/agent.py - tool 事件只在完成后 emit
state.steps.append({"step": step, "type": "tool", "tool": name, "result": result})
```

**修复方案**:
```python
# 在 tool 执行前 emit 开始事件
state.emit({"type": "tool_start", "tool": name, "args": args})
# 执行工具...
# 执行后 emit 完成事件
state.emit({"type": "tool_end", "tool": name, "duration": duration})
```

**预计工作量**: 1天

---

### 5. 自动化基准测试

**问题**: 无标准 benchmark，质量难以量化

**修复方案**:
1. 添加 HumanEval 测试（代码生成能力）
2. 添加 MMLU 子集（知识问答）
3. 添加 SWE-bench Lite（真实 bug 修复）

```python
# tests/benchmark/human_eval.py
from datasets import load_dataset

def run_human_eval():
    dataset = load_dataset("openai_humaneval")
    correct = 0
    for example in dataset["test"]:
        result = agent.run(f"实现函数: {example['prompt']}{example['entry_point']}")
        if test_function(example["test"], result.answer):
            correct += 1
    return correct / len(dataset["test"])
```

**预计工作量**: 3天

---

### 6. 滚动窗口上下文管理

**问题**: Token 预算超限时直接截断，可能丢失重要信息

**现状代码**:
```python
# app/core/context.py - 简单截断
if total_chars > context_budget:
    history = history[-max_turns:]  # 只保留最后N轮
```

**修复方案**:
1. 实现消息优先级评分（system > assistant > user > tool_result）
2. 保留关键消息，压缩冗余内容
3. 对长工具结果进行摘要

**预计工作量**: 2天

---

### 7. 记忆自动过期机制

**问题**: 旧记忆永久保留，存储持续增长

**修复方案**:
```python
# app/memory/ltm.py - 添加 TTL
class MemoryStore:
    def _should_expire(self, memory: dict) -> bool:
        age_days = (time.time() - memory["created_at"]) / 86400
        return age_days > self.ttl_days
    
    def cleanup_expired(self):
        """清理过期记忆"""
```

**预计工作量**: 1天

---

## 🟢 P2 改进项（1-2月内）

### 8-10. 多模态能力增强、部署优化、权限系统

详见 [`gap_analysis_v2.md`](gap_analysis_v2.md)

---

## 📈 当前优势保持

以下能力已处于主流水平，需继续保持：

| 能力 | 状态 | 说明 |
|------|------|------|
| 多 Provider 支持 | ✅ | DeepSeek/Qwen/Ollama/OpenAI |
| 图驱动架构 | ✅ | 清晰节点流转，易调试 |
| 分层记忆 | ✅ | working/semantic/episodic |
| 工具路由 | ✅ | 动态选子集降 token |
| 代码自愈 | ✅ | LLM 驱动修复失败 |
| Reflection | ✅ | 自我反思改进 |
| 多租户隔离 | ✅ | 数据库级隔离 |
| 成本追踪 | ✅ | Token/费用管理 |
| OpenTelemetry | ✅ | 完整 tracing 支持 |
| Prompt 缓存 | ✅ | 降低成本 50%+ |

---

## 🎯 修复路线

```
第1周: P0 阻塞项
├── Day 1-2: 记忆冲突仲裁
├── Day 3:   PII 检测
└── Day 4-5: 性能监控 Dashboard

第2周: P1 重要项
├── Day 1-2: 工具流式反馈
├── Day 3-5: 基准测试框架

第3-4周: P1 收尾 + P2 启动
├── Day 1-2: 上下文管理优化
├── Day 3:   记忆过期机制
└── Day 4-5: 多模态增强调研

第2月: P2 全面展开
├── 部署容器化
├── RBAC 权限
├── 可视化 Debug
└── 移动端适配
```

---

**下一步行动**: 按此路线图开始修复，每完成一项更新进度
