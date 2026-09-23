/* 规范的智能体消息渲染模块
 * 设计理念: 简洁友好、结果优先、JSON/HTML 智能渲染
 * 结构: 思考(折叠) → 步骤列表(序号+名称+状态) → 结果(语法高亮) → 回答
 */
(function () {
  const TOOL_ICONS = {
    read: '📖', write: '✏️', edit: '🔧', exec: '▶️',
    search: '🔍', browser: '🌐', file: '📄', code: '💻',
    python: '🐍', shell: '⚡', plan: '📋',
    read_file: '📖', write_file: '✏️', edit_file: '🔧',
    run_terminal: '⚡', run_code: '💻', git: '📦',
    code_search: '🔎', file_search: '🔍',
    fetch_content: '🌐', generate_image: '🖼️',
    delegate_task: '🔀', save_memory: '🧠', search_memory: '🔎',
  };

  function getToolIcon(name) {
    const n = (name || '').toLowerCase();
    for (const [k, v] of Object.entries(TOOL_ICONS)) {
      if (n.includes(k)) return v;
    }
    return '🔨';
  }

  // ── JSON 语法高亮 ──
  function highlightJson(text) {
    if (!text) return '';
    try {
      const obj = JSON.parse(text);
      const pretty = JSON.stringify(obj, null, 2);
      let html = '';
      let inString = false;
      let escape = false;
      for (let i = 0; i < pretty.length; i++) {
        const ch = pretty[i];
        if (escape) { html += esc(ch); escape = false; continue; }
        if (ch === '\\') { escape = true; html += esc(ch); continue; }
        if (ch === '"') {
          inString = !inString;
          html += inString ? '<span class="json-str">"' : '"</span>';
          continue;
        }
        if (inString) { html += esc(ch); continue; }
        if (': ' === pretty.slice(i, i + 2)) {
          html += '<span class="json-kw">:</span> ';
          i++; continue;
        }
        if (ch === ',' || ch === '{' || ch === '}' || ch === '[' || ch === ']') {
          html += `<span class="json-br">${esc(ch)}</span>`; continue;
        }
        if (/[\d\-]/.test(ch) && i > 0 && pretty[i-1] === ':') {
          let num = ch; i++;
          while (i < pretty.length && /[\d.eE+\-]/.test(pretty[i])) { num += pretty[i]; i++; }
          i--;
          html += `<span class="json-num">${esc(num)}</span>`; continue;
        }
        if (/^[a-zA-Z]$/.test(ch)) {
          let word = ch; i++;
          while (i < pretty.length && /[a-zA-Z_]/.test(pretty[i])) { word += pretty[i]; i++; }
          i--;
          if (word === 'true' || word === 'false') html += `<span class="json-bool">${esc(word)}</span>`;
          else if (word === 'null') html += `<span class="json-null">${esc(word)}</span>`;
          else html += esc(word);
          continue;
        }
        html += esc(ch);
      }
      return html;
    } catch (_) {
      return esc(text);
    }
  }

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ── HTML 预览渲染 ──
  function renderHtmlPreview(htmlText) {
    if (!htmlText) return '';
    // 检测是否是 HTML 片段
    if (/^\s*<[^>]+>/.test(htmlText) && !/^\s*<!DOCTYPE|<script|<style/i.test(htmlText)) {
      // 安全渲染: 先转义再尝试提取结构
      const escaped = esc(htmlText);
      // 如果包含常见 HTML 结构，显示为代码块
      if (escaped.includes('&lt;') && escaped.includes('&gt;')) {
        return `<pre class="result-html-code">${escaped}</pre>`;
      }
    }
    return `<pre class="result-text">${esc(htmlText)}</pre>`;
  }

  // ── 检测结果类型 ──
  function detectResultType(text) {
    if (!text) return 'text';
    const t = text.trim();
    try { JSON.parse(t); return 'json'; } catch (_) {}
    if (/^\s*<[^>]+>[\s\S]*<\/[^>]+>\s*$/.test(t)) return 'html';
    if (/^\s*<!DOCTYPE|<html|<body/i.test(t)) return 'html';
    return 'text';
  }

  // ── 结果内容预处理:还原 JSON 字符串中的转义字符 ──
  function unescapeResult(text) {
    if (!text) return '';
    let s = String(text);
    // 尝试 JSON 解析:如果整个结果是 JSON 字符串，先解析还原
    try {
      const parsed = JSON.parse(s);
      if (typeof parsed === 'string') {
        s = parsed; // JSON.parse 已自动还原 \n \t \" 等
      } else if (parsed && typeof parsed === 'object' && parsed.content) {
        // 结果是对象且包含 content 字段:直接使用 content 并还原转义
        s = String(parsed.content);
      }
    } catch (_) {
      // 非 JSON:手动还原常见转义序列
      s = s.replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/\\"/g, '"').replace(/\\'/g, "'");
    }
    return s;
  }

  // ── 渲染结果内容 ──
  function renderResultContent(result, type) {
    if (!result) return '';
    if (type === 'json') {
      // 先尝试还原转义再高亮
      const unescaped = unescapeResult(result);
      // 如果还原后包含换行,说明是多行内容,用代码块展示
      if (unescaped.includes('\n') && unescaped.split('\n').length > 3) {
        return `<pre class="step-result-pre">${esc(unescaped)}</pre>`;
      }
      return highlightJson(result);
    }
    if (type === 'html') {
      return renderHtmlPreview(result);
    }
    // 纯文本:先还原转义
    const unescaped = unescapeResult(result);
    if (unescaped.includes('\n') && unescaped.split('\n').length > 3) {
      return `<pre class="step-result-pre">${esc(unescaped)}</pre>`;
    }
    return esc(unescaped);
  }

  // ── 消息包装 ──
  function createMessageWrap(type) {
    const wrap = document.createElement('div');
    wrap.className = `msg msg-${type}`;
    return wrap;
  }

  function createAvatar(type) {
    const a = document.createElement('div');
    a.className = 'avatar';
    a.textContent = type === 'user' ? '👤' : '🤖';
    return a;
  }

  function createActionBtn(label, title, onClick) {
    const btn = document.createElement('button');
    btn.className = 'msg-action';
    btn.textContent = label;
    btn.title = title;
    btn.addEventListener('click', async () => {
      const wasCopied = await onClick(btn);
      if (wasCopied) {
        btn.textContent = '✓已复制';
        btn.classList.add('copied');
        setTimeout(() => {
          btn.textContent = label;
          btn.classList.remove('copied');
        }, 2000);
      }
    });
    return btn;
  }

  // ── 用户消息 ──
  function appendUserMessage(text) {
    const wrap = createMessageWrap('user');
    wrap.appendChild(createAvatar('user'));
    const content = document.createElement('div');
    content.className = 'msg-content';
    const bubble = document.createElement('div');
    bubble.className = 'answer-doc';
    // 用 innerHTML + 转义, 把 \n 转成 <br> 让多行在气泡里逐行显示(与复制内容一致,
    // 解决"多行信息看着折叠成一行、复制出来却是一坨"的观感错位)。
    // 保留原始 text(含换行)供复制, 不做 trim —— 复制出的就是用户输入的原样。
    bubble.innerHTML = App.escapeHtml ? App.escapeHtml(text).replace(/\n/g, '<br>') : text;
    content.appendChild(bubble);
    const footer = document.createElement('div');
    footer.className = 'msg-footer';
    const actions = document.createElement('div');
    actions.className = 'msg-actions';
    actions.appendChild(createActionBtn('复制', '复制', async (btn) => {
      try {
        // 复制用户"输入的原样文本"(含多行换行), 而不是从 DOM textContent 反推
        await navigator.clipboard.writeText(text);
        return true;
      } catch (_) {
        // 剪贴板 API 不可用时(如非安全上下文)回退 execCommand
        try {
          const ta = document.createElement('textarea');
          ta.value = text;
          ta.style.position = 'fixed'; ta.style.left = '-9999px';
          document.body.appendChild(ta); ta.select();
          document.execCommand('copy'); ta.remove();
          return true;
        } catch (__) { return false; }
      }
    }));
    actions.appendChild(createActionBtn('引用回复', '引用此消息', () => {
      App.setReplyQuote && App.setReplyQuote(text);
    }));
    footer.appendChild(actions);
    footer.appendChild(createTime());
    content.appendChild(footer);
    wrap.appendChild(content);
    App.els.messages.appendChild(wrap);
    scrollBottom();
    if (App.refreshWelcomePage) App.refreshWelcomePage();
    return wrap;
  }

  // ── Bot 消息 ──
  function createBotMessage() {
    const wrap = createMessageWrap('bot');
    wrap.appendChild(createAvatar('bot'));
    const content = document.createElement('div');
    content.className = 'msg-content';
    wrap._content = content;

    // 状态栏
    const header = document.createElement('div');
    header.className = 'msg-header';
    const rail = document.createElement('div');
    rail.className = 'process-rail';
    rail.innerHTML = '<span class="rail-summary">⏳ 思考中...</span>';
    header.appendChild(rail);
    wrap._rail = rail;
    wrap._railSummary = rail.querySelector('.rail-summary');
    header.appendChild(createTime());
    content.appendChild(header);

    // 思考区域
    const thinkingDiv = document.createElement('div');
    thinkingDiv.className = 'thinking-section';
    const tLabel = document.createElement('div');
    tLabel.className = 'thinking-label';
    tLabel.textContent = '💭 思考中';
    const tBody = document.createElement('div');
    tBody.className = 'thinking-body';
    thinkingDiv.appendChild(tLabel);
    thinkingDiv.appendChild(tBody);
    tLabel.addEventListener('click', () => thinkingDiv.classList.toggle('open'));
    content.appendChild(thinkingDiv);
    wrap._thinkingSection = thinkingDiv;
    wrap._thinkingBody = tBody;

    // 步骤序号
    const stepCounter = document.createElement('div');
    stepCounter.className = 'step-counter';
    stepCounter.textContent = '0';
    content.appendChild(stepCounter);
    wrap._stepCounter = stepCounter;
    wrap._stepCount = 0;

    // 步骤列表
    const stepsFlow = document.createElement('div');
    stepsFlow.className = 'steps-flow';
    content.appendChild(stepsFlow);
    wrap._stepsFlow = stepsFlow;

    // 答案区域
    const answer = document.createElement('div');
    answer.className = 'answer-doc';
    answer.innerHTML = '';
    content.appendChild(answer);
    wrap._answer = answer;

    // 操作按钮（放在 answer-doc 外部，用 CSS 让背景延伸覆盖）
    const footer = document.createElement('div');
    footer.className = 'msg-footer';
    const actions = document.createElement('div');
    actions.className = 'msg-actions';
    actions.appendChild(createActionBtn('复制', '复制回答', async (btn) => {
      const txt = (answer.textContent || '').trim();
      if (!txt) return false;
      try {
        await navigator.clipboard.writeText(txt);
        return true;
      } catch (_) {
        // navigator.clipboard 在 file:// 非安全上下文不可用时, 回退 execCommand
        try {
          const ta = document.createElement('textarea');
          ta.value = txt;
          ta.style.position = 'fixed'; ta.style.left = '-9999px';
          document.body.appendChild(ta); ta.select();
          document.execCommand('copy'); ta.remove();
          return true;
        } catch (__) { return false; }
      }
    }));
    actions.appendChild(createActionBtn('引用回复', '引用此回复', () => {
      const txt = (answer.textContent || '').trim();
      App.setReplyQuote && App.setReplyQuote(txt);
    }));
    actions.appendChild(createActionBtn('重新生成', '重新生成', () => {
      if (App.Chat) App.Chat.regenerate(wrap);
    }));
    actions.appendChild((function () {
      const btn = createActionBtn('🔊', '朗读', (b) => {
        // 播放/停止切换:播放中显示 ⏹,点一下停止;否则开始朗读
        if (App.isSpeaking && App.isSpeaking()) {
          App.stopSpeak();
          b.textContent = '🔊'; b.title = '朗读';
          return;
        }
        const txt = (answer.textContent || '').trim();
        if (!txt) return;
        b.textContent = '⏹'; b.title = '停止朗读';
        App.speak(txt);
        // 轮询:自然播完/出错后还原按钮
        const iv = setInterval(() => {
          if (!(App.isSpeaking && App.isSpeaking())) {
            clearInterval(iv);
            b.textContent = '🔊'; b.title = '朗读';
          }
        }, 200);
      });
      return btn;
    })());
    footer.appendChild(actions);
    footer.appendChild(createTime());
    content.appendChild(footer);

    wrap.appendChild(content);
    App.els.messages.appendChild(wrap);
    scrollBottom();
    return wrap;
  }

  function scrollBottom() {
    App.scrollMessagesToBottom();
  }

  function createTime() {
    const el = document.createElement('span');
    el.className = 'msg-time';
    const d = new Date();
    el.textContent = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    return el;
  }

  // ── 步骤渲染 ──
  // stepId: 全局唯一 ID(前端自增),用于区分同名工具的多次调用
  let _stepIdCounter = 0;
  function _nextStepId() { return ++_stepIdCounter; }

  function addStep(wrap, name, args, status, result, stepId) {
    if (!wrap || !wrap._stepsFlow) return null;
    // 用 stepId 而非工具名去重:同名工具多次调用各自独立显示
    const uniqueKey = stepId || `__${_nextStepId()}`;
    let existing = wrap._stepsFlow.querySelector(`.step-row[data-uid="${CSS.escape(uniqueKey)}"]`);
    // 兜底匹配:tool_call_start 预建的 running 行没有 step_id,done 事件到达时
    // 按"同名工具的最后一个 running 行"回退匹配,就地更新而不是另起新行,
    // 修复"同一工具出现 running/done 两行、running 行永远卡在 ⏳"的重复渲染。
    if (!existing && (status === 'done' || status === 'fail')) {
      const runningRows = wrap._stepsFlow.querySelectorAll(
        `.step-row[data-tool="${CSS.escape(name)}"].step-running`);
      if (runningRows.length > 0) existing = runningRows[runningRows.length - 1];
    }
    if (existing) {
      // 更新已有步骤状态(不重新计数)
      if (status === 'done' || status === 'fail') {
        existing.classList.remove('step-running');
        existing.classList.add(status === 'fail' ? 'step-fail' : 'step-done');
        const icon = existing.querySelector('.step-icon');
        if (icon) icon.textContent = status === 'fail' ? '✗' : '✓';
        const detail = existing.querySelector('.step-detail');
        let resultEl = detail ? detail.querySelector('.step-result') : null;
        if (!resultEl && result) {
          resultEl = document.createElement('div');
          resultEl.className = 'step-result';
          if (detail) detail.appendChild(resultEl);
          else existing.appendChild(resultEl);
        }
        if (resultEl && result) {
          const imgUrl = App.parseImageUrl(result);
          const videoUrl = App.parseVideoUrl ? App.parseVideoUrl(result) : '';
          if (name === 'generate_image' && imgUrl) {
            resultEl.innerHTML = `<img class="step-img-result" src="${App.escapeHtml(imgUrl)}" alt="生成图片">`;
          } else if ((name === 'generate_video' || name === 'enhance_video') && videoUrl) {
            resultEl.innerHTML = App.buildVideoHtml(videoUrl);
          } else {
            const type = detectResultType(result);
            resultEl.innerHTML = renderResultContent(result, type);
            resultEl.dataset.resultType = type;
          }
        }
        if (detail) {
          let argsEl = detail.querySelector('.step-args-content');
          if (!argsEl && args) {
            argsEl = document.createElement('pre');
            argsEl.className = 'step-args-content';
            const previewEl = existing.querySelector('.step-args-preview');
            if (previewEl) previewEl.parentNode.insertBefore(argsEl, previewEl.nextSibling);
            else existing.appendChild(argsEl);
          }
          if (argsEl && args) {
            const argsType = detectResultType(args);
            argsEl.innerHTML = renderResultContent(args, argsType);
            argsEl.dataset.resultType = argsType;
          }
        }
      }
      updateRail(wrap);
      return existing;
    }

    // 新建步骤:只在此处递增计数
    wrap._stepCount++;
    wrap._stepCounter.textContent = wrap._stepCount;
    wrap._stepCounter.style.display = 'flex';

    const row = document.createElement('div');
    row.className = `step-row step-${status}`;
    row.dataset.tool = name;
    row.dataset.uid = uniqueKey;  // 唯一标识,用于去重

    const num = document.createElement('span');
    num.className = 'step-num';
    num.textContent = wrap._stepCount;
    row.appendChild(num);

    const icon = document.createElement('span');
    icon.className = 'step-icon';
    icon.textContent = status === 'running' ? '⏳' : status === 'fail' ? '✗' : '✓';
    row.appendChild(icon);

    const label = document.createElement('span');
    label.className = 'step-label';
    label.textContent = name;
    row.appendChild(label);

    // 详情区域
    const detail = document.createElement('div');
    detail.className = 'step-detail';

    // 参数
    if (args) {
      const argsWrap = document.createElement('div');
      argsWrap.className = 'step-args';
      const argsLabel = document.createElement('span');
      argsLabel.className = 'step-args-label';
      argsLabel.textContent = '参数';
      const argsPreview = document.createElement('div');
      argsPreview.className = 'step-args-preview';
      argsPreview.innerHTML = highlightJsonSummary(args);
      argsWrap.appendChild(argsLabel);
      argsWrap.appendChild(argsPreview);
      detail.appendChild(argsWrap);
    }

    // 结果
    if (result && status !== 'running') {
      const resultEl = document.createElement('div');
      resultEl.className = 'step-result';
      const imgUrl = App.parseImageUrl(result);
      const videoUrl = App.parseVideoUrl ? App.parseVideoUrl(result) : '';
      if (name === 'generate_image' && imgUrl) {
        resultEl.innerHTML = `<img class="step-img-result" src="${App.escapeHtml(imgUrl)}" alt="生成图片">`;
      } else if ((name === 'generate_video' || name === 'enhance_video') && videoUrl) {
        resultEl.innerHTML = App.buildVideoHtml(videoUrl);
      } else {
        const type = detectResultType(result);
        resultEl.dataset.resultType = type;
        if (type === 'html') resultEl.classList.add('result-html-code');
        resultEl.innerHTML = renderResultContent(result, type);
      }
      detail.appendChild(resultEl);
    }

    row.appendChild(detail);

    // 点击展开/收起
    row.addEventListener('click', (e) => {
      if (e.target.closest('.step-result') || e.target.closest('.step-args')) return;
      row.classList.toggle('step-open');
    });

    wrap._stepsFlow.appendChild(row);
    // 步骤上限:最多显示 MAX_STEPS 条,超出时移除最早的步骤
    const MAX_STEPS = 50;
    const allRows = wrap._stepsFlow.querySelectorAll('.step-row');
    while (allRows.length > MAX_STEPS) {
      allRows[0].remove();
    }
    scrollBottom();
    updateRail(wrap);
    return row;
  }

  // JSON 摘要预览 (单行)
  function highlightJsonSummary(jsonStr) {
    try {
      const obj = JSON.parse(jsonStr);
      if (obj && typeof obj === 'object') {
        const keys = Object.keys(obj);
        if (keys.length === 0) return esc(jsonStr.slice(0, 100));
        const summary = keys.slice(0, 3).map(k => {
          const v = obj[k];
          const vStr = typeof v === 'object' ? JSON.stringify(v).slice(0, 30) : String(v).slice(0, 40);
          return `<span class="json-k">${esc(k)}</span>: <span class="json-v">${esc(vStr)}</span>`;
        }).join('  ');
        return keys.length > 3 ? summary + `  …(+${keys.length - 3})` : summary;
      }
    } catch (_) {}
    return esc(String(jsonStr).slice(0, 100));
  }

  function updateRail(wrap) {
    if (!wrap || !wrap._railSummary) return;
    const total = wrap._stepCount || 0;
    const running = wrap._stepsFlow?.querySelectorAll?.('.step-running')?.length || 0;
    const done = total - running;
    if (running > 0) {
      wrap._railSummary.textContent = `⚡ 执行中 ${running} 步, 已完成 ${done}`;
    } else if (total > 0) {
      wrap._railSummary.textContent = `✅ 共 ${total} 步, 全部完成`;
    } else {
      wrap._railSummary.textContent = '⏳ 思考中...';
    }
  }

  // ── 思考内容 ──
  function addThinking(wrap, content) {
    if (!wrap || !wrap._thinkingBody) {
      console.warn('[addThinking] wrap or _thinkingBody missing');
      return;
    }
    wrap._thinkingSection.style.display = 'block';
    // 无论内容是否为空,始终添加 open class 使思考区域可见
    wrap._thinkingSection.classList.add('open');
    const body = wrap._thinkingBody;
    let cleanContent = content || '';
    // ⚠️ 流式分块必须用不 trim 的版本:stripThinkingTags 末尾的 .trim() 会把
    // "独占一个 chunk 的 \n\n"(LLM 段落分隔的常见形态)整块吃掉,
    // 每个分块的首尾换行全丢 → 思考区挤成一整面没有换行的文字墙。
    if (App.stripThinkingTagsStream) {
      cleanContent = App.stripThinkingTagsStream(cleanContent);
    } else if (App.stripThinkingTags) {
      cleanContent = App.stripThinkingTags(cleanContent);
    } else {
      cleanContent = cleanContent.replace(/<thinking[\s\S]*?<\/thinking>/gi, '')
        .replace(/<\/?thinking[^>]*>/gi, '');
    }
    // ⚠️ 判空不能用 .trim():后端逐字符发射 token,换行符独占一个 chunk,
    // '\n'.trim() === '' 会把所有换行 chunk 整块丢掉 → 思考区挤成无换行的文字墙
    if (!cleanContent) return;
    // 累积原始文本到 _thinkingRaw,Markdown 渲染节流到每帧一次
    // (逐 chunk 全量渲染开销大;textContent 会让 **加粗**/###/列表等语法裸露)
    wrap._thinkingRaw = (wrap._thinkingRaw || '') + cleanContent;
    const MAX_THINKING = 4000;
    if (wrap._thinkingRaw.length > MAX_THINKING) {
      wrap._thinkingRaw = wrap._thinkingRaw.slice(-MAX_THINKING);
    }
    if (!wrap._thinkingRenderPending) {
      wrap._thinkingRenderPending = true;
      // 时间片节流:思考同样逐字符到达,rAF(约 16ms)等于每秒 60 次全量
      // Markdown 解析 + innerHTML 替换,是长思考卡顿的主因之一。改为 150ms 一次。
      const nowMs = Date.now();
      const elapsed = nowMs - (wrap._thinkingLastRenderAt || 0);
      const delay = Math.max(0, 150 - elapsed);
      setTimeout(() => {
        wrap._thinkingRenderPending = false;
        wrap._thinkingLastRenderAt = Date.now();
        const body2 = wrap._thinkingBody;
        if (!body2) return;
        const raw = wrap._thinkingRaw || '';
        if (App.renderMarkdown) {
          try {
            body2.classList.add('md');
            body2.innerHTML = App.renderMarkdown(raw);
            return;
          } catch (e) {
            console.warn('[addThinking] markdown render failed, fallback to text:', e);
          }
        }
        body2.textContent = raw;
      }, delay);
    }
    if (wrap._railSummary) {
      wrap._railSummary.textContent = '💭 思考中...';
    }
  }

  function updateRailStatus(wrap, status, text) {
    if (!wrap || !wrap._railSummary) return;
    const icons = { thinking: '💭', running: '⚡', done: '✅', error: '❌' };
    wrap._railSummary.textContent = `${icons[status] || '⏳'} ${text || status}`;
  }

  function showAnswer(wrap, markdown) {
    if (!wrap || !wrap._answer) return;
    if (App.renderMarkdown) {
      try {
        wrap._answer.innerHTML = App.renderMarkdown(markdown);
      } catch (e) {
        console.error('[showAnswer] renderMarkdown failed, falling back:', e);
        wrap._answer.innerHTML = '<p style="white-space:pre-wrap;">' + App.escapeHtml(markdown) + '</p>';
      }
    } else {
      wrap._answer.textContent = markdown;
    }
  }

  window.App = window.App || {};
  App.appendUserMessage = appendUserMessage;
  App.createBotMessage = createBotMessage;
  App.addToolStep = addStep;
  App.addToolStepIfNew = addStep;
  App.addThinking = addThinking;
  App.updateRailStatus = updateRailStatus;
  App.showAnswer = showAnswer;
  App.updateRail = updateRail;
  App.detectResultType = detectResultType;
  App.highlightJson = highlightJson;
  App.highlightJsonSummary = highlightJsonSummary;
})();
