# 编排模式配置 UI 与图片生成进度 UI

本次在「多智能体编排（顺序/层级/共识）」落地的基础上，新增两项可交互能力。

## 一、编排模式的 UI 可配置项

### 后端（`app/api/chat.py` + `app/core/agent.py`）

`ChatRequest` 新增两个字段：

- `orch_subtasks: int = 0` —— 层级编排（hierarchical）下期望的子任务数量上限；`0` 表示由 LLM 自主决定。
- `orch_candidates: list[str] = []` —— 参与编排的「执行体（执行模式）」清单，取值为 `single / auto / review / ensemble` 的子集；为空则由模式默认。

`Agent.__init__` 接收并保存 `orch_subtasks` / `orch_candidates`，在三种编排分支中生效：

- **sequential**：若 `orch_candidates` 提供且长度 ≥ 2，则用它覆盖默认的两阶段流水线（`auto → review`），依次串行执行。
- **hierarchical**：
  - `_register_orch_agents` 仅注册 `orch_candidates` 中的执行模式（为空则注册全部四种）；
  - `decompose_task` 的提示词注入「建议拆成 N 个子任务」（`N = orch_subtasks`），子任务分配时只从已注册清单里挑执行体。
- **consensual**：若 `orch_candidates` 提供，则用它作为并行候选执行体集合（默认 `single/auto/review` 或 `single/auto/ensemble`），投票择优逻辑复用 ensemble 的 `_judge_ensemble`。

`decompose_task` 新增 `num_subtasks` 参数，控制任务分解的粒度提示。

### 前端（`desktop/src/ui/`）

- `index.html`：在输入区上方新增 `.orch-config` 面板（隐藏，仅编排类模式显示）：
  - 「子任务数」数字输入框（`orchSubtasks`）；
  - 候选执行体多选（`orchCandidates`）：单模型 / 自动 / 评审 / 多模型择优 四个勾选 chip。
- `renderer.js`：绑定新元素；监听 `modeSelect` 变化，仅当模式 ∈ `{sequential, hierarchical, consensual}` 时显示该面板。
- `chat.js`：`App.Chat.modeParams()` 在编排模式下把 `orch_subtasks` / `orch_candidates` 并入请求体。

## 二、图片生成专属「生成中…」进度 UI

### 触发链（后端事件 → 前端）

后端在工具开始执行前（`app/core/agent.py` `_node_tools`）发出：

```json
{ "type": "tool_start", "names": ["generate_image"] }
```

图片工具通过 `asyncio.to_thread` 同步调用外部 `/images/generations`，耗时较长（已放宽超时：post 300s / 下载 180s / 工具 360s）。在此期间后端静默，由 `_stream_with_heartbeat` 每 15s 注入 `heartbeat` 事件保持连接存活。

工具完成后发出：

```json
{ "type": "tool", "name": "generate_image", "result": "{'format':'local_url','image_url':'/images/xxx.png', ...}" }
```

### 前端（`desktop/src/ui/js/chat.js` + `style.css`）

- `showImageGenerating()`：创建带旋转 spinner 的「🖼️ 正在生成图片…」气泡卡片（`App._imgGenCard`）。
- 触发时机（三选一，取最先到达者，带幂等保护）：
  - `tool_start` 且 `names` 含 `generate_image`；
  - `tool_call_delta` 且 `name === 'generate_image'`；
  - `tool_route` 且 `tools` 含 `generate_image`。
- `finishImageGenerating(resultStr)`：从结果串中提取图片 URL（正则兼容 http(s) 绝对路径、本地 `/images/...` 相对路径，以及 JSON 的 `image_url` 字段），将 spinner 卡片替换为 `<img>`。
- `clearImageGenerating()`：在 `done` / `error` / 空闲超时 / 新一轮发送时清理残留卡片，避免「幽灵」卡片。
- `style.css`：新增 `.orch-config`、`.img-gen-bubble`、`.img-spinner`（旋转动画）、`.img-gen-result` 样式，继承主题变量（`--accent` / `--border` / `--text` / `--muted`）。

## 三、验证

- 后端源码模式已重启（端口 8518 用于验证，客户端实际连 8517）。
- `hierarchical` + `orch_subtasks=2` + `orch_candidates=["auto","review"]`：仅在该清单内分配子任务，阶段事件正常。
- `consensual` + `orch_candidates=["single","review"]`：并行候选与投票结果随之变化。
- `auto` 模式普通对话流式输出不受影响；心跳事件持续注入，长耗时工具不再被前端误判超时。
- 图片生成：`tool_start`（或 `tool_call_delta`/`tool_route`）已确认在 `generate_image` 调用时发出，`tool` 结果携带本地图片 URL，前端进度卡链路打通。
