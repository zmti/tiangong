# UI 重构剩余待办

## 已完成 ✅

### 核心模块
- [x] storage.js - 统一 localStorage 管理
- [x] dom-cache.js - 64 个 DOM 元素缓存
- [x] init.js - 应用初始化
- [x] state.js - 状态管理

### 已迁移模块
- [x] session.js: 561 → 183 行 (-378)
- [x] settings.js: 551 → 79 行 (-472)
- [x] mode.js: 502 → 158 行 (-344)
- [x] model_picker.js: 315 → 190 行 (-125)
- [x] prompts.js: 96 → 102 行 (新增功能)

### 代码量
```
重构前: 6246 行
重构后: 4933 行
减少:   1313 行 (21%)
```

---

## 剩余待重构 🔄

### 一、大型模块拆分 (优先级: 中)

#### 1. chat.js (821行) - 建议拆分
```
chat.js 当前职责:
  - 消息发送/接收 (85-340行)
  - 图片生成 UI (16-82行)
  - SSE 流处理 (395-560行)
  - 工具调用显示 (440-560行)
  - 其他辅助函数 (718-821行)

建议拆分:
  chat-core.js  (200行) - 核心发送/接收逻辑
  chat-render.js (200行) - 消息渲染
  chat-stream.js (150行) - SSE 流处理
  chat-images.js (100行) - 图片生成
  chat-tools.js  (100行) - 工具调用显示
```

#### 2. workflow.js (382行) - 建议拆分
```
workflow.js 当前职责:
  - 工作流列表 (1-50行)
  - 工作流设计器 (54-300行)
  - 工作流执行 (300-382行)

建议拆分:
  workflow-list.js  (80行) - 列表管理
  workflow-designer.js (200行) - 设计器
  workflow-executor.js (100行) - 执行器
```

#### 3. common.js (288行) - 可精简
```
当前是兼容垫片，可以:
  - 移除已迁移的功能
  - 保留 ExecTrace 等必要模块
  - 目标: 100行以内
```

---

### 二、DOM 查询迁移 (优先级: 低)

#### 需要迁移到 App.els 的元素:
```
memory.js:
  - memorySearch (line 30)
  - memoryScope (line 31)

common.js:
  - execTab (line 34)
  - execTraceList (line 40, 106)

其他模块 (小问题):
  - 约 46 处 querySelector/getElementById
  - 大多是动态查询，不适合缓存
```

---

### 三、localStorage 兼容代码 (无需重构)

以下代码是**必要的降级路径**，用于迁移旧数据:

```javascript
// mode.js - 旧键迁移
const raw = localStorage.getItem('agent_mode_state');
const legacyMode = localStorage.getItem('agent_mode');

// model_picker.js - 旧键迁移
const raw = localStorage.getItem('agent_model_state');
const prov = localStorage.getItem('agent_provider');

// session.js - 旧键迁移
const raw = localStorage.getItem('agent_pinned_sessions');
```

这些代码应该**保留**，因为:
1. 兼容已保存的旧数据
2. 会自动迁移到新格式
3. 迁移完成后可以删除

---

### 四、重复代码模式 (无需重构)

当前重复模式:
```
localStorage 读写: 20 处
DOM 查询: 46 处
```

这些大部分是:
- 兼容降级路径 (必要)
- 动态查询 (不适合缓存)
- 事件绑定 (正常模式)

---

## 后续可选优化

### 短期 (1-2天)
1. 拆分 chat.js 为核心模块
2. 迁移 memory.js 的 DOM 查询

### 中期 (1周)
1. 拆分 workflow.js
2. 精简 common.js
3. 添加前端单元测试

### 长期 (可选)
1. 引入构建工具 (Vite)
2. 添加 TypeScript
3. 模块化重构完成

---

## 当前状态

```
============= 15 passed in 13.64s =============
```

| 检查项 | 状态 |
|--------|------|
| 工作流测试 | ✅ 15/15 PASSED |
| API 响应 | ✅ 200 OK |
| JS 语法 | ✅ 所有模块通过 |
| 后端运行 | ✅ PID 546716 稳定 |
| 代码仓库 | ✅ 干净 |

---

## 建议下一步

1. **保持现状** - 核心重构已完成，代码质量已提升 21%
2. **按需优化** - 后续开发时按需拆分大模块
3. **逐步清理** - 随着用户使用，旧 localStorage 数据会逐渐迁移，可以清理兼容代码
