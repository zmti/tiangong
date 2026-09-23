/* 操作确认卡片(安全门禁) —— 恢复自 common.js 模块化拆分时丢失的实现(原 8b3651b common.js:1393)
 *
 * 后端在危险工具(run_terminal/run_code/send_email/delete_email 等)执行前会
 * 预扫描并创建持久化确认请求,然后以 interrupted 状态暂停运行(GraphInterrupt);
 * 这里负责渲染批准/拒绝卡片,用户操作后写回结果并自动续跑。
 *
 * 此前该模块在 common.js 拆分时丢失,chat.js 里的引用
 * (if (App.Confirmation && App.Confirmation.show))被守卫静默跳过,
 * 导致运行暂停后界面什么都不显示——用户看到的就是"输出中断",且无法恢复。
 */
(function () {
  window.App = window.App || {};

  /* 自建容器:旧版依赖 index.html 里的 #confirmBar(已不存在),改为按需创建 */
    /* 续跑期间的空闲看门狗上限:连续这么久**没有任何事件**才判定断线。
     *
     * 旧实现是固定 5 分钟硬 abort —— 但续跑本身经常要十几分钟(多轮 LLM + 工具),
     * 于是正常的续跑被前端掐断,界面跳出红条"续跑超时,请稍后在运行记录面板重试"
     * (2026-09-20 截图),而服务端其实还在继续跑。现在改成:
     *   - 有事件(含后端每 30s 的心跳)就继续等,不设总时长上限;
     *   - 只有"超过 IDLE_LIMIT_MS 一个字都没来"才断开。
     */
  const RESUME_IDLE_LIMIT_MS = 180000;
  // 老后端(非流式 resume)全程没有任何事件,只能用总时长兜底
  const RESUME_JSON_LIMIT_MS = 1800000;

  App.Confirmation = {
    shown: {},       // confirmation_id -> card DOM
    _container: null,
    _interrupts: {}, // confirmation_id -> { run_id, session_id }
    _batches: {},    // run_id -> [confirmation_id, ...] 同一轮 interrupt 的确认集合
    _decisions: {},  // run_id -> { confirmation_id: { approved: bool } }
    _lastRunId: '',  // 最近一次中断的 run_id:轮询渲染的卡片没有它,批准时用它兜底续跑
    _pollTimer: null,
    _responded: {},  // confirmation_id -> 'approved'/'rejected' 已响应记录(防重复提交)
    _inFlight: {},   // confirmation_id -> true 正在请求中(防并发重复点击)

    _getContainer() {
      if (this._container && this._container.parentNode) return this._container;
      let bar = document.getElementById('confirmBar');
      if (!bar) {
        bar = document.createElement('div');
        bar.id = 'confirmBar';
        bar.style.cssText = [
          'position:fixed', 'right:16px', 'bottom:96px', 'z-index:9990',
          'display:none', 'flex-direction:column', 'gap:8px',
          'width:360px', 'max-width:calc(100vw - 32px)',
          'font-family:inherit'
        ].join(';');
        document.body.appendChild(bar);
      }
      this._container = bar;
      return bar;
    },

    /* 轮询兜底:UI 重载/重启后错过的 interrupt,可从持久化存储恢复卡片 */
    async poll() {
      let res;
      try {
        res = await App.api('/v1/confirmations/pending', { timeout: 5000 });
      } catch (_) { return; }
      if (!res.ok) return;
      let data;
      try { data = await res.json(); } catch (_) { return; }
      const pending = data.pending || [];
      const known = new Set(pending.map((c) => c.id));
      for (const id of Object.keys(this.shown)) {
        if (!known.has(id)) this.remove(id);
      }
      for (const c of pending) {
        if (!this.shown[c.id]) this.render(c);
      }
    },

    _startPoll() {
      if (this._pollTimer) return;
      this._pollTimer = setInterval(() => { this.poll().catch(() => {}); }, 3000);
    },

    render(c) {
      // 去重:同一确认可能同时由 SSE 中断事件(show)与 3s 轮询(poll)提交渲染,
      // 不守卫会出现两张同 ID 卡片 → 用户点两次 → 第二次报"确认请求不存在或已处理"。
      if (this.shown[c.id]) return;
      const bar = this._getContainer();
      const card = document.createElement('div');
      card.className = 'confirm-card';
      card.dataset.id = c.id;
      card.style.cssText = [
        'background:#252531', 'border:1px solid #f59e0b55', 'border-radius:10px',
        'padding:12px 14px', 'box-shadow:0 8px 24px rgba(0,0,0,.45)', 'color:#e8e8ef',
        'font-size:13px', 'line-height:1.5'
      ].join(';');

      const head = document.createElement('div');
      head.style.cssText = 'display:flex;align-items:center;gap:8px;font-weight:600;color:#f59e0b;margin-bottom:6px;';
      head.textContent = '⚠️ 需要人工确认的操作';
      const act = document.createElement('span');
      act.style.cssText = 'color:#c9c9d6;font-weight:500;';
      act.textContent = c.action || '';
      head.appendChild(act);
      const close = document.createElement('span');
      close.style.cssText = 'margin-left:auto;cursor:pointer;color:#8a8a99;font-size:14px;padding:0 4px;';
      close.textContent = '✕';
      close.title = '忽略(按拒绝处理,不自动续跑)';
      close.addEventListener('click', () => this.dismiss(c.id));
      head.appendChild(close);

      const desc = document.createElement('div');
      desc.style.cssText = 'white-space:pre-wrap;word-break:break-word;max-height:180px;overflow:auto;margin-bottom:10px;color:#c9c9d6;';
      desc.textContent = (c.description || '') + (c.created_at ? ` (${new Date(c.created_at * (c.created_at < 1e12 ? 1000 : 1)).toLocaleTimeString()})` : '');

      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:8px;';
      const approve = document.createElement('button');
      approve.style.cssText = 'flex:1;padding:6px 0;border:none;border-radius:6px;cursor:pointer;background:#22c55e22;color:#4ade80;font-weight:600;';
      approve.textContent = '✅ 批准';
      approve.addEventListener('click', () => this.respond(c.id, true));
      const reject = document.createElement('button');
      reject.style.cssText = 'flex:1;padding:6px 0;border:none;border-radius:6px;cursor:pointer;background:#ef444422;color:#f87171;font-weight:600;';
      reject.textContent = '⛔ 拒绝';
      reject.addEventListener('click', () => this.respond(c.id, false));
      actions.appendChild(approve);
      actions.appendChild(reject);

      card.appendChild(head);
      card.appendChild(desc);
      card.appendChild(actions);
      bar.appendChild(card);
      bar.style.display = 'flex';
      this.shown[c.id] = card;
    },

    async respond(id, approved, opts = {}) {
      // 重入保护:卡片要 1.5s 后才移除,期间按钮仍可点(双击/点两次)→
      // 第二次请求会命中后端"已处理"分支。已响应或在途的直接忽略。
      if (this._inFlight[id]) return;
      if (this._responded[id] && !opts.force) return;
      this._inFlight[id] = true;
      // 立即禁用按钮并给出处理中反馈,避免用户重复点击
      const _card = this.shown[id];
      const _act = _card && _card.querySelector('.confirm-actions');
      if (_act) {
        _act.querySelectorAll('button').forEach((b) => { b.disabled = true; b.style.opacity = '.5'; b.style.cursor = 'default'; });
        _act.insertAdjacentHTML('beforeend', '<span style="align-self:center;color:#8a8a99;">处理中…</span>');
      }
      let failed = '';
      try {
        const res = await App.api(`/v1/confirmations/${encodeURIComponent(id)}/respond`, {
          method: 'POST',
          body: JSON.stringify({ approved, result: '' }),
          timeout: 10000,
        });
        const data = await res.json().catch(() => null);
        // 幂等:后端对已处理请求返回 ok + already,不视为失败
        if (data && data.status === 'error' && data.error && !data.already) failed = data.error;
        if (data && data.already) approved = (data.confirmation && data.confirmation.status) === 'approved';
      } catch (e) {
        failed = e.message;
      }
      delete this._inFlight[id];
      this._responded[id] = approved ? 'approved' : 'rejected';
      if (failed) App.appendMessage(`确认处理失败: ${failed}`, 'error');
      const card = this.shown[id];
      if (card) {
        const act = card.querySelector('.confirm-actions');
        if (act) {
          act.innerHTML = `<span style="color:${approved ? '#4ade80' : '#f87171'};">${approved ? '✅ 已批准' : '⛔ 已拒绝'}</span>`;
        }
        // 注意:不能在这里 delete this.shown[id],否则定时器里的 remove() 拿不到卡片,
        // DOM 中的卡片会永远残留(render() 又因 shown 已删除而拒绝重绘)。
        setTimeout(() => this.remove(id), 1500);
      }
      if (!opts.noResume) await this._maybeResume(id, approved);
    },

    /* 流式中断时即时展示所有待确认卡片(替代轮询等待) */
    show(requests, runId) {
      if (!requests || !requests.length) return;
      if (runId) this._lastRunId = runId;
      for (const req of requests) {
        if (!req || !req.confirmation_id) continue;
        const c = {
          id: req.confirmation_id,
          action: req.tool || '',
          description: req.reason || '',
        };
        this.render(c);
        this._interrupts[req.confirmation_id] = { run_id: runId, session_id: App.sessionId || '' };
        if (runId) {
          if (!this._batches[runId]) this._batches[runId] = [];
          if (!this._batches[runId].includes(req.confirmation_id)) {
            this._batches[runId].push(req.confirmation_id);
          }
        }
      }
    },

    /* 记录某次 interrupt 对应的确认集合,用于批准后自动续跑对话 */
    trackInterrupt(doneEvt) {
      const intr = (doneEvt && doneEvt.interrupt) || {};
      const runId = doneEvt.run_id || intr.run_id || '';
      if (runId) this._lastRunId = runId;
      const requests = intr.requests || [];
      if (!requests.length) return;
      const ids = [];
      for (const r of requests) {
        if (r && r.confirmation_id) {
          this._interrupts[r.confirmation_id] = { run_id: runId, session_id: App.sessionId || '' };
          ids.push(r.confirmation_id);
        }
      }
      if (runId && ids.length) this._batches[runId] = ids;
    },

    /* 忽略:按拒绝写回并移除卡片,但不自动续跑,可在运行记录面板稍后继续 */
    async dismiss(id) {
      await this.respond(id, false, { noResume: true });
      this._untrack(id);
    },

    _untrack(id) {
      const info = this._interrupts[id];
      if (!info) return;
      delete this._interrupts[id];
      const runId = info.run_id;
      const rest = (this._batches[runId] || []).filter((cid) => cid !== id);
      if (rest.length) this._batches[runId] = rest;
      else delete this._batches[runId];
      if (this._decisions[runId]) {
        delete this._decisions[runId][id];
        if (!Object.keys(this._decisions[runId]).length) delete this._decisions[runId];
      }
    },

    async _maybeResume(id, approved) {
      // 卡片可能来自 3s 轮询兜底(没有 run_id 记录),此时回退到最近一次
      // 已知的中断 run_id,否则批准后会静默无下文(用户观感:点了批准没反应)。
      const info = this._interrupts[id] || {};
      const runId = info.run_id || this._lastRunId;
      const sessionId = info.session_id || App.sessionId || '';
      if (!runId) return;
      this._decisions[runId] = this._decisions[runId] || {};
      this._decisions[runId][id] = { approved };
      const tracked = this._batches[runId] || [];
      const batch = tracked.length ? tracked : [id];
      const decided = Object.keys(this._decisions[runId]);
      // 同一轮 interrupt 若有多个确认卡片,等全部决定后再一次续跑
      if (!batch.every((cid) => decided.indexOf(cid) >= 0)) return;
      const resumeValue = {};
      for (const cid of batch) {
        resumeValue[cid] = this._decisions[runId][cid];
        delete this._interrupts[cid];
      }
      delete this._decisions[runId];
      delete this._batches[runId];
      await this._resumeRun(runId, sessionId, resumeValue);
    },

    async _resumeRun(runId, sessionId, resumeValue) {
      // 阈值可从 App.Confirmation 上覆盖(便于测试按秒级验证看门狗行为)
      const IDLE_LIMIT = this._idleLimitMs || RESUME_IDLE_LIMIT_MS;
      const TICK = this._watchdogTickMs || 15000;
      if (App.Chat && App.Chat.setStreaming) App.Chat.setStreaming(true);
      if (App.ExecTrace && App.ExecTrace.start) App.ExecTrace.start();
      // 立即给反馈:旧实现点完"批准"后界面几十分钟没有任何动静(用户观感=卡顿)
      App.appendMessage('▶ 已批准,继续执行…', 'system');
      if (App.showTyping) App.showTyping();
      const ctl = new AbortController();
      let lastEventAt = Date.now();
      let sawAnyEvent = false;
      const watchdog = setInterval(() => {
        // 空闲看门狗:只在"连续一段时间没有任何事件"时断开
        if (Date.now() - lastEventAt > IDLE_LIMIT) ctl.abort();
      }, TICK);

      /* —— 续跑过程中的增量渲染 ——
       * 旧实现的 SSE 分支把 token 全攒在局部变量里,循环结束才 appendMessage:
       * 续跑期间界面只有打字指示器,十几分钟不动 → 观感就是"会话卡顿"。
       * 这里改成每来一批 token 就节流渲染一次,工具/思考/状态也即时上报。 */
      let answer = '';
      let answerDiv = null;
      let mdTimer = null;
      const runningSteps = {};
      const ensureAnswerDiv = () => {
        if (answerDiv) return answerDiv;
        if (App.createBotMessage) {
          const botMsg = App.createBotMessage();
          answerDiv = botMsg && botMsg._answer;
          if (botMsg) App._currentBotMsg = botMsg;
          if (answerDiv) answerDiv.style.display = '';
        }
        return answerDiv;
      };
      const flushMd = () => {
        mdTimer = null;
        if (!answerDiv) return;
        const md = App.stripThinkingTagsStream ? App.stripThinkingTagsStream(answer) : answer;
        try {
          answerDiv.innerHTML = App.renderMarkdown ? App.renderMarkdown(md) : (App.escapeHtml ? App.escapeHtml(md) : md);
        } catch (_) {
          answerDiv.innerHTML = App.escapeHtml ? App.escapeHtml(md) : md;
        }
      };
      const scheduleMd = () => {
        if (mdTimer) return;
        mdTimer = setTimeout(flushMd, 120);
      };
      const stopMdTimer = () => { if (mdTimer) { clearTimeout(mdTimer); mdTimer = null; } };

      try {
        const res = await App.api('/v1/chat/resume', {
          method: 'POST',
          body: JSON.stringify({ run_id: runId, session_id: sessionId, resume_value: resumeValue, stream: true }),
          signal: ctl.signal,
        });
        const _ctype = (res.headers && res.headers.get('content-type')) || '';
        if (!res.ok || !res.body) {
          const errText = await res.text().catch(() => '');
          App.appendMessage(`续跑失败: ${res.status} ${errText}`, 'error');
        } else if (_ctype.indexOf('application/json') >= 0) {
          // 老后端:resume 不流式(agent.resume 返回 AgentResult),响应是 JSON。
          // 这类后端全程没有任何事件,只能靠总时长兜底 + 提示"仍在执行"。
          lastEventAt = Date.now() + (RESUME_JSON_LIMIT_MS - IDLE_LIMIT);
          const hint = setInterval(() => {
            if (App._currentBotMsg && App.updateRailStatus) {
              App.updateRailStatus(App._currentBotMsg, 'thinking', '续跑执行中(该版本后端不支持流式进度)…');
            }
          }, 20000);
          let data = null;
          try {
            data = await res.json();
          } finally {
            clearInterval(hint);
          }
          const ans = (data && (data.answer || data.content)) || '';
          for (const st of ((data && data.steps) || [])) {
            if (st && st.type === 'tool' && App.ExecTrace && App.ExecTrace.addStep) {
              App.ExecTrace.addStep(st.tool || st.name || 'tool', st.arguments || '', 'done');
            }
          }
          if (data && (data.interrupted || data.interrupt)) {
            App.appendMessage(`⏸ 运行续跑后再次暂停: ${((data.interrupt || {}).message) || '等待确认'}`, 'bot');
            if (data.interrupt) this.trackInterrupt({ run_id: runId, interrupt: data.interrupt });
          } else if (ans) {
            const div = App.appendMessage(ans, 'bot');
            if (div && App.renderMarkdown) div.innerHTML = App.renderMarkdown(ans);
          } else {
            App.appendMessage('(续跑完成,无输出)', 'system');
          }
        } else {
          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let buf = '';
          let interrupted = false;
          let interruptData = null;
          let gotCancelled = false;
          let lastErrMsg = '';
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            lastEventAt = Date.now();
            sawAnyEvent = true;
            buf += decoder.decode(value, { stream: true });
            const parts = buf.split('\n\n');
            buf = parts.pop();
            for (const part of parts) {
              const line = part.trim();
              if (!line.startsWith('data: ')) continue;
              let evt = null;
              try { evt = JSON.parse(line.slice(6)); } catch (_) { continue; }
              if (!evt || !evt.type) continue;
              lastEventAt = Date.now();
              if (evt.type === 'token') {
                ensureAnswerDiv();
                answer += evt.content || '';
                scheduleMd();
              } else if (evt.type === 'answer') {
                answer = evt.content || evt.answer || answer;
                ensureAnswerDiv();
                stopMdTimer();
                flushMd();
              } else if (evt.type === 'answer_reset') {
                // 限流重试/重写:上一版残段作废,清缓冲等新内容
                answer = '';
                stopMdTimer();
                if (answerDiv) answerDiv.innerHTML = '';
              } else if (evt.type === 'thinking') {
                if (App._currentBotMsg && App.addThinking) App.addThinking(App._currentBotMsg, evt.content || '');
              } else if (evt.type === 'status') {
                if (App._currentBotMsg && App.updateRailStatus) {
                  App.updateRailStatus(App._currentBotMsg, 'thinking', evt.content || '处理中…');
                }
              } else if (evt.type === 'tool_start' || evt.type === 'tool_call_start') {
                const list = evt.tools && evt.tools.length ? evt.tools : [evt];
                for (const t of list) {
                  const nm = (t && (t.name || t.tool)) || 'tool';
                  runningSteps[nm] = true;
                  if (App.ExecTrace && App.ExecTrace.addStep) App.ExecTrace.addStep(nm, (t && t.arguments) || '', 'running');
                }
              } else if (evt.type === 'tool') {
                const nm = evt.name || evt.tool || 'tool';
                if (App.ExecTrace) {
                  if (runningSteps[nm] && App.ExecTrace.updateStep) App.ExecTrace.updateStep(nm, 'done', '');
                  else if (App.ExecTrace.addStep) App.ExecTrace.addStep(nm, evt.arguments || '', 'done');
                }
                delete runningSteps[nm];
              } else if (evt.type === 'confirmation') {
                // 续跑中又遇到需要确认的工具:直接显示确认卡片
                if (evt.requests && this.show) this.show(evt.requests, runId);
              } else if (evt.type === 'guardrail_overwrite') {
                answer = evt.content || '';
                ensureAnswerDiv();
                stopMdTimer();
                flushMd();
              } else if (evt.type === 'heartbeat') {
                // 仅用于重置看门狗:后端长工具/长思考期间会定时发,证明连接还活着
              } else if (evt.type === 'error') {
                const m = evt.message || '续跑出错';
                if (m !== lastErrMsg) { lastErrMsg = m; App.appendMessage(m, 'error'); }
              } else if (evt.type === 'cancelled') {
                gotCancelled = true;
              } else if (evt.type === 'done') {
                interrupted = !!(evt.interrupted || evt.interrupt);
                interruptData = evt.interrupt || null;
                for (const st of (evt.steps || [])) {
                  if (st && st.type === 'tool' && App.ExecTrace && App.ExecTrace.addStep
                      && !runningSteps[st.tool || st.name]) {
                    App.ExecTrace.addStep(st.tool || st.name || 'tool', st.arguments || '', 'done');
                  }
                }
              }
            }
          }
          stopMdTimer();
          if (interrupted) {
            if (answer) flushMd();
            App.appendMessage(`⏸ 运行续跑后再次暂停: ${(interruptData && (interruptData.message || interruptData.type)) || '等待确认'}`, 'bot');
            // 嵌套确认:记录新的确认集合,后续批准/拒绝卡片可继续自动续跑
            if (interruptData) {
              this.trackInterrupt({ run_id: runId, interrupt: interruptData });
            }
          } else if (gotCancelled) {
            if (answer) flushMd();
            App.appendMessage('⏹ 续跑已被取消', 'system');
          } else if (answer) {
            ensureAnswerDiv();
            flushMd();
          } else {
            // 无答案也无中断:显示提示避免空消息
            App.appendMessage('(续跑完成,无输出)', 'system');
          }
        }
      } catch (e) {
        stopMdTimer();
        if (e.name !== 'AbortError') {
          App.appendMessage(`续跑失败: ${e.message}`, 'error');
        } else {
          // 只有"长时间零事件"才会走到这里。服务端由独立后台任务执行,断开 SSE 不影响它,
          // 所以这里不能说"失败",要告诉用户结果不会丢、怎么取回。
          const mins = Math.max(1, Math.round(IDLE_LIMIT / 60000));
          App.appendMessage(
            sawAnyEvent
              ? `⏸ 已断开实时刷新(超过 ${mins} 分钟没有新进展)。后端任务可能仍在执行:稍后刷新会话即可看到结果。`
              : `⏸ 续跑等待超时(超过 ${mins} 分钟没有任何事件)。后端可能仍在执行,可刷新会话或到运行记录面板继续。`,
            'error'
          );
        }
      } finally {
        clearInterval(watchdog);
        stopMdTimer();
        if (App.ExecTrace && App.ExecTrace.finish) {
          try { App.ExecTrace.finish(answerDiv ? (answerDiv.textContent || '') : null); } catch (_) {}
        }
        if (App.removeTyping) App.removeTyping();
        if (App.Chat && App.Chat.setStreaming) App.Chat.setStreaming(false);
      }
    },

    remove(id) {
      const card = this.shown[id];
      if (card && card.parentNode) card.parentNode.removeChild(card);
      delete this.shown[id];
      const bar = this._container;
      if (bar && bar.children.length === 0) bar.style.display = 'none';
    },

    /* 发送新消息(doSend)时调用:清掉上一轮残留的确认卡片与跟踪状态,
     * 避免旧的 interrupted 卡片在新对话里继续停留或误触发续跑。 */
    clear() {
      // 1) 移除 DOM 中所有卡片
      for (const id of Object.keys(this.shown)) {
        const card = this.shown[id];
        if (card && card.parentNode) card.parentNode.removeChild(card);
      }
      // 2) 重置跟踪状态(保留 _container 与 _pollTimer,轮询兜底继续工作)
      this.shown = {};
      this._interrupts = {};
      this._batches = {};
      this._decisions = {};
      this._responded = {};
      this._inFlight = {};
      const bar = this._container;
      if (bar) bar.style.display = 'none';
    },
  };

  // 启动轮询兜底(轻量:3s 一次,仅在有 pending 时渲染)
  if (App.Confirmation._startPoll) App.Confirmation._startPoll();
})();
