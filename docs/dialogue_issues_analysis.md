# 对话问题分析：输入超时 & 不能实时显示

> 分析对象：`desktop/src/ui/js/chat.js`、`desktop/src/ui/js/common.js`、`app/api/chat.py`、`app/core/agent.py`
> 结论先行：**后端是逐 token 真流式（已核实），两个问题都出在前端对话渲染/超时控制逻辑。**

---

## 一、问题一：输入信息超时

### 1.1 连接建立阶段完全没有超时保护（最严重）

`common.js` 的 `api()` 自带 60s 兜底超时，但逻辑是**“没传 signal 才加”**：

```js
// common.js:1227-1231
if (!opts.signal) {
  const ctl = new AbortController();
  opts.signal = ctl.signal;
  setTimeout(() => ctl.abort(), 60000);
}
```

而 `chat.js` 里发流时**必然传入** `App.abortController.signal`：

```js
// chat.js:159-176
res = await App.api('/v1/chat/stream', {
  method: 'POST',
  ...
  signal,            // ← 传入后,api() 的 60s 超时被跳过
});
```

结果：`/v1/chat/stream` 的 **TCP 建连 + 首字节响应** 阶段**没有任何超时**，只能靠用户手动点“停止”。
典型场景：Python 后端子进程崩溃/还在启动、8517 端口未就绪、后端卡死 → 用户发消息后页面**永久转圈、无提示、无法自愈**。这正是“输入信息超时”的最常见表现。

### 1.2 120s 流超时是“静默间隙”超时，会误杀正常长任务

```js
// chat.js:196-206
let streamTimeout = setTimeout(() => {
  if (!done && !isError) {
    done = true; isError = true;
    cleanupStream();
    App.appendMessage('响应超时,请稍后重试', 'error');
  }
}, 120000); // 120s 整体超时
```

- 该计时器只在 `res.body.getReader()` **之后**才启动，覆盖不到 1.1 的连接阶段。
- 它衡量的是“任意两次 SSE 事件之间超过 120s 无数据”，但**无法区分“后端卡死”和“后端正在干重活”**。
- 对 Agent 场景（多步工具链、代码执行、慢搜索、长 LLM 思考）一次间隔很容易超过 120s，于是**前端误判超时、强制断开、丢弃已收到的部分结果**（用户看到“响应超时”，后台其实还在跑）。

### 1.3 首 token 前无独立超时 / 无进度反馈

首 token 到达前只显示“正在思考中…”（`chat.js:144`）。若 LLM 首 token 延迟高（如 reasoning 模型、长上下文），用户面对静止界面，体感等同“超时/卡死”，但此时 120s 计时尚未启动、60s 也已被旁路，**完全无兜底**。

---

## 二、问题二：不能实时（正确）显示信息

> 先澄清：文本**内容**是实时增量出现的（每个 token 触发 `requestAnimationFrame` 渲染），所以“完全不显示”不成立；真正的问题是**显示出来的内容是错误的、且不纠正**。

### 2.1 切片式 Markdown 渲染破坏块级结构（核心缺陷）

增量渲染只取**新切片**单独渲染后 `insertAdjacentHTML` 追加：

```js
// chat.js:214-227
function scheduleRender() {
  if (renderTimer !== null) return;
  renderTimer = requestAnimationFrame(() => {
    ...
    const newBuf = App._mdBuf.slice(_renderedLen);   // ← 只取增量切片
    const newHtml = App.renderMarkdown(newBuf);       // ← 切片独立解析
    answerDiv.insertAdjacentHTML('beforeend', newHtml);
    _renderedLen = App._mdBuf.length;
  });
}
```

`renderMarkdown`（`common.js:393-493`）是**逐行、无跨调用状态**的。任何跨切片边界的块级结构都会被拆坏：

- **代码块**：```` ```python ```` 在切片 A 开、```` ``` ```` 在切片 B 关 → A 渲染成高亮代码块，B 因不以代码模式开始而被渲染成普通 `<p>`，最终**代码块断裂成“代码段 + 乱序段落”**。
- **列表**：`- a`（切片 A）与 `- b`（切片 B）各自成 `<ul>`，变成两个独立列表、间距错乱。
- **表格 / 引用 / 标题** 同理，跨边界即失效。

更糟的是 `done` 事件**也只补渲染剩余切片**，从不把整段 `_mdBuf` 重新按完整文档解析：

```js
// chat.js:309-312
if (answerDiv && _renderedLen < App._mdBuf.length) {
  answerDiv.insertAdjacentHTML('beforeend', App.renderMarkdown(App._mdBuf.slice(_renderedLen)));
  _renderedLen = App._mdBuf.length;
}
```

→ 流式期间的渲染错乱**会被永久保留在最终消息里**，用户看到的是一份“实时生成但格式错误、且再也无法自愈”的回答。这是“不能实时显示信息（正确内容）”的根本原因。

### 2.2 首 token 前界面静止

配合 1.3：纯生成（无工具调用）时，首 token 前界面只有“正在思考中…”，长延迟下信息“看似不显示”。

### 2.3 `requestAnimationFrame` 在窗口失焦时暂停

`scheduleRender` 依赖 rAF，Electron 窗口切到后台时 rAF 不触发，渲染暂停；回到前台才批量补刷。多窗口/后台长任务时信息更新会“卡住”。

---

## 三、后端核对（排除法）

- `app/api/chat.py:212-224`：`async for event in agent.run_stream(...)` 逐事件 `yield f"data: {json.dumps(event)}…"`，并设置 `Cache-Control: no-cache`、`X-Accel-Buffering: no`。
- `app/core/agent.py:245/561/599`：`yield {"type": "token", "content": ch}` 逐字符产出。
- 结论：**后端是真流式、无缓冲**。两个问题均归属前端。

---

## 四、修复建议（概要）

| 问题 | 建议 |
|---|---|
| 1.1 连接阶段无超时 | `doSend` 在调用 `api()` 前**自行加一个建连超时**（如 30s 的 AbortController），或让 `api()` 的 timeout 与传入 signal 共存（二者都触发才 abort）。 |
| 1.2 120s 误杀长任务 | 把“无数据间隙超时”改为**可重置/更长**（如首次事件后 180s），或按 `thinking/tool` 事件类型动态放宽；并在超时前提示“仍在处理”而非直接丢弃。 |
| 1.3 首 token 无兜底 | 加“首 token 超时”（如 60s）独立提示；首 token 前显示可取消的进度态。 |
| 2.1 渲染错乱（核心） | 放弃“切片独立解析”。改为**每帧对整段 `_mdBuf` 重新渲染到同一容器**（可接受 O(n²) 时做长度阈值/增量 DOM diff）；或维护跨切片的 `inCode` 等解析状态，保证块级结构连续。 |
| 2.3 后台暂停 | 用 `setTimeout` 队列替代/补充 rAF，确保后台也按节奏刷新（或窗口 `blur/focus` 时强制刷一帧）。 |

> 优先级：**2.1 > 1.1 > 1.2 > 2.3 ≈ 1.3**。2.1 直接影响“信息是否正确显示”，1.1 直接影响“输入是否超时卡死”。
