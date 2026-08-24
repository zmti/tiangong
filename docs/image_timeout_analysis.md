# 生成图片超时：原因分析与修复

> 结论先行：图片生成"超时"并非单一原因，而是 **前端空闲计时器误杀** 与 **后端图片请求超时过短** 两层叠加。本次已同时修复。

## 一、问题现象

在聊天界面触发 `generate_image`（生成图片）工具时，界面偶发出现：

- `响应超时,请稍后重试`
- `响应中断,请稍后重试`
- 或图片实际已生成、但连接已被前端断开，最终结果丢失

## 二、根因分析

### 根因 1：前端 120s 空闲间隙计时器把"慢工具"误判为断线

前端 `desktop/src/ui/js/chat.js` 的流式读取逻辑中，有一个 **空闲间隙计时器**：

```js
// 每次收到任意 SSE 事件都会 resetStreamTimeout() 续期
function resetStreamTimeout() {
  streamTimeout = setTimeout(() => { /* 标记 isError, 提示"响应超时" */ }, 120000);
}
```

设计意图是区分"卡死"与"正在干重活"。但图片生成期间：

- `app/tools/image_tool.py` 用同步 `requests.post(..., timeout=120)` 调用图片 API；
- 该调用通过 `asyncio.to_thread` 在线程中执行，**不阻塞事件循环**；
- 但 Agent 在工具执行**期间不向 SSE 流推送任何事件**（工具结果只在执行完毕后才发 `tool` 事件，见 `app/core/agent.py` 的 `_node_tools`）；
- 因此图片生成的数十秒～两分钟内，SSE 流完全静默；
- 前端连续 120s 收不到事件 → 触发 `响应超时,请稍后重试`。

即使后端最终成功生成图片，前端也已经"判死刑"并清理了连接。

### 根因 2：后端图片 HTTP 请求超时（120s）短于真实生成耗时

```python
requests.post(api_url, json=payload, headers=headers, timeout=120, verify=False)
```

图片生成（尤其 1024x1024 或排队场景下）经常 > 120s。一旦到达 120s，`requests` 抛出 `Timeout` → 工具抛出异常 → 返回 `图片生成失败`。即"后端自己先把请求掐了"。

回退下载分支的 `timeout=60` 同样偏短；工具总超时 `@tool(timeout=180)` 也只比请求超时多 60s 余量。

### 根因 3（可能）：图片端点未正确配置

`image_tool` 的开关：

```python
def _image_enabled() -> bool:
    return bool(settings.openai_compat_base_url)
```

- 若 `openai_compat_base_url` 为空 → 工具直接 `enabled=False`，根本不会被调用；
- 若它指向的是**不支持 `/images/generations` 的 LLM 网关**（如纯对话兼容接口），请求会长时间挂起后 4xx/超时；
- 默认 `image_model = "agnes-image-2.0-flash"`，需网关确实提供该模型与 images 端点。

> 排查建议：确认 `openai_compat_base_url` 指向的是**图片生成服务**（而非纯对话 base_url），且模型名与端点匹配。

### 根因 4：与编排/长工具调用同源

层级编排的 `decompose_task`（LLM 分解任务）、共识投票的评审阶段等长时间无事件产出的内部环节，同样会触发根因 1。

## 三、本次修复

| 文件 | 修改 | 作用 |
|---|---|---|
| `app/api/chat.py` | 新增 `_stream_with_heartbeat`：Agent 静默每 15s 注入一条 `{"type":"heartbeat"}` | 让 SSE 连接在整个图片生成期间持续"存活"，前端空闲计时器被续期，不再误杀 |
| `app/api/chat.py` | `mode` 描述补充 `sequential/hierarchical/consensual` | 支持新编排模式（顺带避免误解） |
| `app/core/agent.py` | 工具批量执行前先发 `{"type":"tool_start","names":[...]}` | 让前端明确感知"正在调用工具"，双保险 |
| `app/tools/image_tool.py` | `requests.post` 超时 `120 → 300`；下载超时 `60 → 180`；`@tool(timeout=180 → 360)` | 后端不再在图片真正生成完成前掐断请求 |
| `app/core/agent.py` | 编排模式（Sequential/Hierarchical/Consensual）接入 `_run_orchestrated_stream` | 长链路内部阶段也会持续产出 `orchestration` 事件，天然规避根因 1 |

### 心跳实现要点（踩坑记录）

初版 `_stream_with_heartbeat` 用 `asyncio.ensure_future(agen.__anext__())` **每步新建一个 Task** 来驱动 Agent 流，导致 `run_stream` 内部 `contextvars` 的 `set/reset` 跨越不同 Task/Context，报错：

```
<Token var=<ContextVar name='agent_run_id' ...> was created in a different Context>
```

已改为 **单生产者 Task + asyncio.Queue**：生产者任务完整驱动整条 Agent 流（整条流处于同一 Context），消费者协程同时等待"队列事件"与"心跳间隔"，先到先发。修复后 hierarchical/consensual/sequential 均验证通过。

## 四、验证方式

1. 后端（源码模式）已在 `127.0.0.1:8517` 重启并加载新代码；
2. 三家编排模式 SSE 实测均正常产出 `orchestration` 事件与最终 `token`；
3. 普通 `auto` 模式流式输出不受影响；
4. 心跳事件已确认在静默期按 ~15s 间隔下发，可抵消图片生成的长时间静默。

## 五、后续建议（可选）

- 若图片服务确实很慢，可把 `heartbeat` 间隔进一步缩短（如 10s），或把 `image_tool` 改为**先返回 `task_id`、轮询进度**的异步模式，体验更佳；
- 在图片工具执行期间向前端推送 `{"type":"tool_start","name":"generate_image"}` 时，前端可显示一个"图片生成中…"专属状态条（目前仅续期计时器，未做专属 UI）；
- 增加图片生成超时/失败的**前端友好提示**（当前失败会作为普通工具错误回显）。
