/* 公共工具:命名空间 / API 封装 / Markdown 渲染 / 消息 DOM / 灯箱 */
window.App = {
  backendUrl: '',
  sessionId: 'desktop-' + Date.now().toString(36),
  streaming: false,
  abortController: null,
  selectedImageFile: null,
  els: {},
  _groupId: 0,
  _currentGroup: null,
  Group: {},
};

/* ---------- 请求分组:将一次对话的工具调用/思考/计划折叠为单个卡片 ---------- */
App.Group.start = function () {
  App._groupId++;
  const gid = App._groupId;
  const wrap = document.createElement('div');
  wrap.className = 'req-group';
  wrap.dataset.gid = gid;
  // 执行摘要行(初始显示"执行中...")
  const head = document.createElement('div');
  head.className = 'req-group-head';
  head.innerHTML = '<span class="rg-pulse">⏳</span><span class="rg-label">执行中…</span>';
  const body = document.createElement('div');
  body.className = 'req-group-body';
  body.style.display = 'none';
  wrap.appendChild(head);
  wrap.appendChild(body);
  App.els.messages.appendChild(wrap);
  App.els.messages.scrollTop = App.els.messages.scrollHeight;
  App._currentGroup = { gid, wrap, head, body, tools: [], thinking: null, plan: null, started: Date.now() };
  return gid;
};

App.Group.addTool = function (name, args, result, success) {
  if (!App._currentGroup) {
    // 降级:无分组时创建独立消息
    appendToolEvent(name, args, result);
    return;
  }
  const g = App._currentGroup;
  const ok = success !== true; // true param => failed; anything else => success
  g.tools.push({ name, args, result, ok, time: Date.now() });
  // 更新头部摘要
  const icons = { read: '📖', write: '✏️', exec: '▶️', search: '🔍', browser: '🌐', edit: '🔧', system: '⚙️', tool: '🔨' };
  const icon = Object.keys(icons).find(k => name.toLowerCase().includes(k)) || '🔨';
  g.head.innerHTML = `<span class="rg-pulse">${icon}</span><span class="rg-label">${name} ${ok ? '✓' : '✗'}</span>`;
  // 折叠显示:不展开详情直到结束
  if (!g._detailVisible) {
    const item = document.createElement('div');
    item.className = 'rg-tool-item' + (ok ? '' : ' rg-tool-fail');
    item.innerHTML = `<span class="rg-t-icon">${icon}</span><span class="rg-t-name">${escapeHtml(name)}</span><span class="rg-t-status">${ok ? '✓' : '✗'}</span>`;
    item.title = `工具: ${name}\n参数: ${(typeof args === 'string' ? args : JSON.stringify(args) || '').slice(0, 120)}`;
    g.body.appendChild(item);
    g._detailVisible = true;
    g.body.style.display = 'flex';
  } else {
    const item = document.createElement('div');
    item.className = 'rg-tool-item' + (ok ? '' : ' rg-tool-fail');
    item.innerHTML = `<span class="rg-t-icon">${icon}</span><span class="rg-t-name">${escapeHtml(name)}</span><span class="rg-t-status">${ok ? '✓' : '✗'}</span>`;
    g.body.appendChild(item);
  }
};

App.Group.setThinking = function (content) {
  if (!App._currentGroup) { appendThinking(content); return; }
  if (!App._currentGroup.thinking) {
    const el = document.createElement('div');
    el.className = 'rg-thinking';
    el.textContent = String(content).slice(0, 200);
    el.title = String(content).slice(0, 800);
    App._currentGroup.body.appendChild(el);
    App._currentGroup.thinking = el;
    if (!App._currentGroup._detailVisible) {
      App._currentGroup.body.style.display = 'flex';
      App._currentGroup._detailVisible = true;
    }
  }
};

App.Group.addPlanStep = function (step, total) {
  if (!App._currentGroup) { appendPlan({ plan: { steps: [step] } }); return; }
  const idx = App._currentGroup.planCount || 0;
  App._currentGroup.planCount = idx + 1;
  if (!App._currentGroup._detailVisible) {
    App._currentGroup.body.style.display = 'flex';
    App._currentGroup._detailVisible = true;
  }
};

App.Group.finish = function (answer, isError) {
  if (!App._currentGroup) return null;
  const g = App._currentGroup;
  const n = g.tools.length;
  const okCount = g.tools.filter(t => t.ok).length;
  const dur = Math.round((Date.now() - g.started) / 1000);
  let headHtml;
  if (isError) {
    headHtml = `<span class="rg-pulse">❌</span><span class="rg-label">执行失败</span>`;
  } else if (n === 0) {
    headHtml = `<span class="rg-pulse">💬</span><span class="rg-label">直接回复</span>`;
  } else {
    headHtml = `<span class="rg-pulse">✅</span><span class="rg-label">${n} 个工具 · ${okCount}/${n} 成功 · ${dur}s</span>`;
    const badge = document.createElement('span');
    badge.className = 'rg-toggle';
    badge.textContent = '▼';
    badge.addEventListener('click', () => {
      const open = g.body.style.display === 'block';
      g.body.style.display = open ? 'none' : 'block';
      badge.textContent = open ? '▼' : '▲';
    });
    g.head.appendChild(badge);
  }
  // 关闭按钮
  const closeBtn = document.createElement('span');
  closeBtn.className = 'rg-close';
  closeBtn.textContent = '✕';
  closeBtn.addEventListener('click', (e) => { e.stopPropagation(); g.wrap.remove(); });
  g.head.innerHTML = headHtml;
  g.head.appendChild(closeBtn);
  g.wrap.classList.add('req-group-done');
  App._currentGroup = null;
  return n;
};

/* ---------- HTML 转义 ---------- */
function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, '&quot;');
}

/* 仅允许安全 URL 协议,拦截 javascript:/file:/vbscript: 等注入 */
function safeUrl(u) {
  const s = String(u).trim();
  const m = s.match(/^([a-zA-Z][a-zA-Z0-9+.-]*):/);
  if (m) {
    const scheme = m[1].toLowerCase();
    if (!['http', 'https', 'data', 'mailto'].includes(scheme)) return '';
  }
  return s;
}

/* ---------- 轻量 Markdown 渲染(无外部依赖) ---------- */
function renderInline(line) {
  let s = escapeHtml(line);
  // 图片 ![alt](url) —— 无内联 onerror,加载失败由容器事件委托处理
  s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, u) => {
    const url = safeUrl(u);
    if (!url) return '';
    return `<img src="${escapeAttr(url)}" alt="${escapeAttr(alt || '图片')}" class="msg-img">`;
  });
  // 代码 `code`
  s = s.replace(/`([^`]+)`/g, (m, code) => `<code>${code}</code>`);
  // 加粗 **text**
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // 斜体 *text*
  s = s.replace(/(^|[^*])\*([^*\s][^*]*?)\*(?!\*)/g, '$1<em>$2</em>');
  // 链接 [text](url)
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, t, u) => {
    const url = safeUrl(u);
    if (!url) return `${t}`;
    return `<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${t}</a>`;
  });
  return s;
}

/* ---------- 代码语法高亮(轻量词法,无外部依赖) ---------- */
const CODE_KEYWORDS = {
  py: ['def', 'class', 'import', 'from', 'return', 'if', 'elif', 'else', 'for', 'while', 'try', 'except', 'finally', 'with', 'as', 'lambda', 'pass', 'break', 'continue', 'and', 'or', 'not', 'in', 'is', 'raise', 'yield', 'async', 'await', 'global', 'nonlocal', 'del', 'print', 'None', 'True', 'False', 'self', 'super', 'assert', 'match', 'case'],
  js: ['function', 'const', 'let', 'var', 'return', 'if', 'else', 'for', 'while', 'do', 'class', 'new', 'import', 'export', 'default', 'from', 'async', 'await', 'try', 'catch', 'finally', 'throw', 'switch', 'case', 'break', 'continue', 'typeof', 'instanceof', 'this', 'null', 'undefined', 'true', 'false', 'of', 'in', 'yield', 'delete', 'void'],
  ts: ['function', 'const', 'let', 'var', 'return', 'if', 'else', 'for', 'while', 'do', 'class', 'interface', 'type', 'enum', 'implements', 'extends', 'new', 'import', 'export', 'default', 'from', 'async', 'await', 'try', 'catch', 'finally', 'throw', 'switch', 'case', 'break', 'continue', 'typeof', 'instanceof', 'this', 'null', 'undefined', 'true', 'false', 'of', 'in', 'readonly', 'public', 'private', 'protected', 'static', 'abstract', 'yield', 'delete', 'void'],
  java: ['public', 'private', 'protected', 'class', 'interface', 'enum', 'extends', 'implements', 'static', 'final', 'void', 'int', 'long', 'double', 'float', 'boolean', 'char', 'byte', 'short', 'String', 'new', 'return', 'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'break', 'continue', 'try', 'catch', 'finally', 'throw', 'throws', 'import', 'package', 'this', 'super', 'abstract', 'volatile', 'synchronized', 'default', 'true', 'false', 'null'],
  go: ['func', 'package', 'import', 'return', 'if', 'else', 'for', 'range', 'switch', 'case', 'default', 'break', 'continue', 'go', 'defer', 'select', 'var', 'const', 'type', 'struct', 'interface', 'map', 'chan', 'len', 'cap', 'make', 'new', 'true', 'false', 'nil', 'panic', 'recover'],
  rs: ['fn', 'let', 'mut', 'const', 'struct', 'enum', 'impl', 'trait', 'pub', 'mod', 'use', 'match', 'if', 'else', 'for', 'while', 'loop', 'return', 'break', 'continue', 'move', 'ref', 'async', 'await', 'dyn', 'Self', 'self', 'super', 'where', 'type', 'static', 'true', 'false', 'None', 'Some', 'Ok', 'Err'],
  css: ['@media', '@import', '@keyframes', 'color', 'background', 'background-color', 'margin', 'padding', 'border', 'display', 'position', 'top', 'right', 'bottom', 'left', 'width', 'height', 'flex', 'grid', 'font-size', 'font-family', 'line-height', 'text-align', 'overflow', 'opacity', 'z-index', 'transition', 'transform', 'cursor', '!important'],
  c: ['int', 'char', 'float', 'double', 'void', 'struct', 'union', 'enum', 'typedef', 'static', 'extern', 'const', 'return', 'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'break', 'continue', 'goto', 'sizeof', 'unsigned', 'long', 'short', 'include', 'define'],
  cpp: ['int', 'char', 'float', 'double', 'void', 'bool', 'struct', 'class', 'namespace', 'using', 'template', 'typename', 'public', 'private', 'protected', 'static', 'const', 'constexpr', 'return', 'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'break', 'continue', 'new', 'delete', 'this', 'virtual', 'override', 'final', 'auto', 'include', 'define'],
  sql: ['select', 'from', 'where', 'insert', 'into', 'values', 'update', 'set', 'delete', 'join', 'left', 'right', 'inner', 'outer', 'on', 'group', 'by', 'order', 'having', 'limit', 'offset', 'create', 'table', 'drop', 'alter', 'add', 'column', 'index', 'primary', 'key', 'foreign', 'references', 'and', 'or', 'not', 'null', 'distinct', 'count', 'sum', 'avg', 'min', 'max', 'union', 'all', 'between', 'in', 'like', 'exists', 'case', 'when', 'then', 'else', 'end', 'desc', 'asc', 'database', 'default', 'unique'],
  sh: ['if', 'then', 'else', 'elif', 'fi', 'for', 'in', 'do', 'done', 'while', 'until', 'case', 'esac', 'function', 'select', 'export', 'local', 'return', 'break', 'continue', 'echo', 'cd', 'source', 'alias', 'read', 'set', 'unset', 'shift', 'exit', 'declare'],
  bash: ['if', 'then', 'else', 'elif', 'fi', 'for', 'in', 'do', 'done', 'while', 'until', 'case', 'esac', 'function', 'select', 'export', 'local', 'return', 'break', 'continue', 'echo', 'cd', 'source', 'alias', 'read', 'set', 'unset', 'shift', 'exit', 'declare'],
  rb: ['def', 'end', 'class', 'module', 'require', 'include', 'if', 'elsif', 'else', 'unless', 'case', 'when', 'for', 'while', 'until', 'do', 'return', 'yield', 'break', 'next', 'redo', 'retry', 'begin', 'rescue', 'ensure', 'attr_accessor', 'puts', 'nil', 'true', 'false', 'self', 'new', 'each', 'map', 'select', 'reject'],
  swift: ['func', 'let', 'var', 'class', 'struct', 'enum', 'protocol', 'extension', 'import', 'guard', 'if', 'else', 'switch', 'case', 'for', 'while', 'repeat', 'return', 'break', 'continue', 'in', 'where', 'self', 'super', 'true', 'false', 'nil', 'throws', 'throw', 'async', 'await', 'try', 'catch', 'defer'],
  json: [],
  yaml: ['true', 'false', 'null', 'yes', 'no'],
  yml: ['true', 'false', 'null', 'yes', 'no'],
  html: [],
  xml: [],
};

const LANG_ALIAS = {
  python: 'py', javascript: 'js', jsx: 'js', mjs: 'js', cjs: 'js',
  typescript: 'ts', tsx: 'ts',
  ruby: 'rb', 'ruby solution': 'rb',
  rust: 'rs',
  golang: 'go',
  shell: 'sh', zsh: 'sh',
  'c++': 'cpp', 'objective-c': 'c',
  yaml: 'yaml', yml: 'yml',
};

function normLang(lang) {
  const l = String(lang).toLowerCase().trim();
  return LANG_ALIAS[l] || l;
}

function commentStartIndex(lang, line) {
  const l = normLang(lang);
  if (['py', 'rb', 'yml', 'yaml', 'toml', 'sh', 'bash', 'makefile', 'dockerfile'].includes(l)) return line.indexOf('#');
  if (['sql'].includes(l)) return line.indexOf('--');
  if (['html', 'xml'].includes(l)) return -1;
  return line.indexOf('//');
}

function highlightCode(lang, code) {
  const kw = new Set((CODE_KEYWORDS[normLang(lang)] || []).map((k) => k.toLowerCase()));
  const tokenRe = /("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\\n]|\\.)*`|\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?\b|\b[A-Za-z_]\w*\b)/g;
  return String(code).split('\n').map((raw) => {
    const line = raw.replace(/\r$/, '');
    const ci = commentStartIndex(lang, line);
    const head = ci >= 0 ? line.slice(0, ci) : line;
    const comment = ci >= 0 ? line.slice(ci) : '';
    let html = '';
    let last = 0;
    tokenRe.lastIndex = 0;
    let m;
    while ((m = tokenRe.exec(head)) !== null) {
      if (m.index > last) html += escapeHtml(head.slice(last, m.index));
      const tok = m[0];
      if (tok[0] === '"' || tok[0] === "'" || tok[0] === '`') {
        html += `<span class="tok-str">${escapeHtml(tok)}</span>`;
      } else if (/^\d/.test(tok)) {
        html += `<span class="tok-num">${escapeHtml(tok)}</span>`;
      } else {
        html += kw.has(tok.toLowerCase()) ? `<span class="tok-kw">${escapeHtml(tok)}</span>` : escapeHtml(tok);
      }
      last = m.index + tok.length;
    }
    if (last < head.length) html += escapeHtml(head.slice(last));
    if (comment) html += `<span class="tok-cm">${escapeHtml(comment)}</span>`;
    return html;
  }).join('\n');
}

/* ---------- 轻量语法检查(括号配对 + 块注释跨行) ---------- */
const SYNTAX_PAIR = { '(': ')', '[': ']', '{': '}' };
const SYNTAX_OPEN = new Set(['(', '[', '{']);
const SYNTAX_CLOSE = new Set([')', ']', '}']);

function stripSyntaxNoise(lang, line, state) {
  const l = normLang(lang);
  let s = String(line).replace(/\r$/, '');
  // 先剔除字符串字面量
  s = s.replace(/("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\\n]|\\.)*`)/g, (m) => ' '.repeat(m.length));
  // 行注释(块注释未开启时)
  if (!state.inBlock) {
    let ci = -1;
    if (['py', 'rb', 'yml', 'yaml', 'toml', 'sh', 'bash', 'makefile', 'dockerfile'].includes(l)) ci = s.indexOf('#');
    else if (['sql'].includes(l)) ci = s.indexOf('--');
    else if (!['html', 'xml'].includes(l)) ci = s.indexOf('//');
    if (ci >= 0) s = s.slice(0, ci);
  }
  // 块注释处理
  if (['html', 'xml'].includes(l)) {
    // <!-- --> 跨行
    while (true) {
      const o = s.indexOf('<!--');
      const c = s.indexOf('-->', o >= 0 ? o + 4 : 0);
      if (o < 0) break;
      if (c > o) {
        s = s.slice(0, o) + s.slice(c + 3);
      } else {
        state.inBlock = true;
        s = s.slice(0, o);
        break;
      }
    }
    if (state.inBlock) {
      const c = s.indexOf('-->');
      if (c >= 0) { state.inBlock = false; s = s.slice(c + 3); }
      else return ' '.repeat(s.length);
    }
  } else if (state.inBlock) {
    const c = s.indexOf('*/');
    if (c >= 0) { state.inBlock = false; s = s.slice(c + 2); }
    else return '';
  } else {
    const o = s.indexOf('/*');
    if (o >= 0) {
      const rest = s.slice(o + 2);
      const c = rest.indexOf('*/');
      if (c >= 0) {
        s = s.slice(0, o) + rest.slice(c + 2);
      } else {
        state.inBlock = true;
        s = s.slice(0, o);
      }
    }
  }
  return s;
}

function checkSyntax(lang, code) {
  const issues = [];
  const stack = [];
  const state = { inBlock: false };
  const lines = String(code).replace(/\r\n/g, '\n').split('\n');
  for (let i = 0; i < lines.length; i++) {
    const clean = stripSyntaxNoise(lang, lines[i], state);
    for (const ch of clean) {
      if (SYNTAX_OPEN.has(ch)) {
        stack.push({ ch, line: i + 1 });
      } else if (SYNTAX_CLOSE.has(ch)) {
        const top = stack.pop();
        if (!top) {
          issues.push({ line: i + 1, msg: `多余的闭合符号「${ch}」` });
        } else if (SYNTAX_PAIR[top.ch] !== ch) {
          issues.push({ line: i + 1, msg: `「${top.ch}」与「${ch}」不匹配(起始于第 ${top.line} 行)` });
        }
      }
    }
  }
  while (stack.length) {
    const top = stack.pop();
    issues.push({ line: top.line, msg: `缺少闭合符号「${SYNTAX_PAIR[top.ch]}」` });
  }
  return issues.slice(0, 5);
}

/* ---------- 表格 ---------- */
function isTableRow(line) {
  const t = String(line).trim();
  return t.startsWith('|') && t.endsWith('|') && t.length > 2;
}

function splitTableRow(line) {
  let t = String(line).trim();
  if (t.startsWith('|')) t = t.slice(1);
  if (t.endsWith('|')) t = t.slice(0, -1);
  return t.split('|').map((c) => c.trim());
}

function isTableSeparator(cells) {
  return cells.length > 0 && cells.every((c) => /^:?-{2,}:?$/.test(String(c).replace(/[ \t]/g, '')));
}

function renderTable(rows) {
  const parsed = rows.map(splitTableRow);
  if (!parsed.length) return '';
  const header = parsed[0] || [];
  let body = parsed.slice(1);
  const aligns = [];
  if (body.length && isTableSeparator(body[0])) {
    body[0].forEach((c, i) => {
      const cc = String(c).replace(/ /g, '');
      aligns[i] = cc.startsWith(':') && cc.endsWith(':') ? 'center' : cc.endsWith(':') ? 'right' : cc.startsWith(':') ? 'left' : '';
    });
    body = body.slice(1);
  }
  const alignAttr = (a) => (a ? ` style="text-align:${a}"` : '');
  const thead = `<thead><tr>${header.map((c, i) => `<th${alignAttr(aligns[i])}>${renderInline(c)}</th>`).join('')}</tr></thead>`;
  const tbody = body.map((r) => `<tr>${r.map((c, i) => `<td${alignAttr(aligns[i])}>${renderInline(c)}</td>`).join('')}</tr>`).join('');
  return `<div class="md-table-wrap"><table>${thead}<tbody>${tbody}</tbody></table></div>`;
}

/* ---------- 任务列表( - [ ] / - [x] ) ---------- */
function renderTaskItem(item) {
  const m = item.match(/^\[( |x|X)\]\s+(.*)$/s);
  if (!m) return item;
  const checked = m[1].toLowerCase() === 'x';
  return `<label class="task-item"><input type="checkbox" disabled${checked ? ' checked' : ''}><span>${renderInline(m[2])}</span></label>`;
}

function renderMarkdown(md) {
  if (!md) return '';
  const lines = String(md).replace(/\r\n/g, '\n').split('\n');
  let html = '';
  let inCode = false;
  let codeLang = '';
  let codeBuf = [];
  let listBuf = [];
  let listType = 'ul';

  function flushList() {
    if (!listBuf.length) return;
    html += `<${listType}>`;
    for (const item of listBuf) {
      if (typeof item === 'string') html += `<li>${renderInline(item)}</li>`;
      else html += `<li>${item}</li>`;
    }
    html += `</${listType}>`;
    listBuf = [];
  }

  function flushCodeBlock() {
    const lang = codeLang;
    const codeText = codeBuf.join('\n');
    const highlighted = highlightCode(lang, codeText);
    const issues = checkSyntax(lang, codeText);
    let badge = '';
    if (issues.length) {
      const tip = issues.slice(0, 4).map((x) => `L${x.line}: ${x.msg}`).join('\n');
      badge = `<span class="syntax-warn" title="${escapeAttr(tip + (issues.length > 4 ? `\n… 共 ${issues.length} 处` : ''))}">⚠ ${issues.length}</span>`;
    }
    html += `<div class="code-block${issues.length ? ' has-issue' : ''}"><button class="code-copy" title="复制代码">复制</button>${badge}<pre><code${lang ? ` class="lang-${escapeAttr(lang)}"` : ''}>${highlighted}</code></pre></div>`;
  }

  let i = 0;
  while (i < lines.length) {
    const line = lines[i].replace(/\s+$/, '');
    if (line.startsWith('```')) {
      flushList();
      if (!inCode) {
        inCode = true;
        codeLang = line.slice(3).trim();
        codeBuf = [];
      } else {
        flushCodeBlock();
        codeBuf = [];
        inCode = false;
      }
      i++;
      continue;
    }
    if (inCode) { codeBuf.push(line); i++; continue; }

    // 表格:首行 + 紧跟的分隔行才按表格渲染
    if (isTableRow(line)) {
      const rows = [];
      let j = i;
      while (j < lines.length && isTableRow(lines[j].replace(/\s+$/, ''))) {
        rows.push(lines[j].replace(/\s+$/, ''));
        j++;
      }
      if (rows.length >= 2 && isTableSeparator(splitTableRow(rows[1]))) {
        flushList();
        html += renderTable(rows);
        i = j;
        continue;
      }
      flushList();
      html += `<p>${renderInline(line)}</p>`;
      i++;
      continue;
    }

    if (/^\s{0,3}#{1,6}\s/.test(line)) {
      flushList();
      const level = line.match(/^#{1,6}/)[0].length;
      html += `<h${level}>${renderInline(line.replace(/^#{1,6}\s+/, ''))}</h${level}>`;
    } else if (/^\s*[-*+]\s+/.test(line)) {
      listType = 'ul';
      listBuf.push(renderTaskItem(line.replace(/^\s*[-*+]\s+/, '')));
    } else if (/^\s*\d+[.)]\s+/.test(line)) {
      listType = 'ol';
      listBuf.push(line.replace(/^\s*\d+[.)]\s+/, ''));
    } else if (/^\s*>\s?/.test(line)) {
      flushList();
      html += `<blockquote>${renderInline(line.replace(/^\s*>\s?/, ''))}</blockquote>`;
    } else if (/^\s*---+\s*$/.test(line)) {
      flushList();
      html += '<hr>';
    } else if (line.trim() === '') {
      flushList();
    } else {
      flushList();
      html += `<p>${renderInline(line)}</p>`;
    }
    i++;
  }
  flushList();
  if (inCode) flushCodeBlock();
  return html;
}

/* 事件委托:代码块复制按钮。渲染进程 DOM 生成后全局委托,流式重渲染也不丢失。 */
function copyCodeFromButton(btn) {
  const block = btn.closest('.code-block');
  const codeEl = block && block.querySelector('pre code');
  const text = codeEl ? codeEl.textContent : '';
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    const old = btn.textContent;
    btn.textContent = '✓ 已复制';
    setTimeout(() => { btn.textContent = old; }, 1200);
  }).catch(() => {
    btn.textContent = '复制失败';
    setTimeout(() => { btn.textContent = '复制'; }, 1200);
  });
}

/* ---------- 消息渲染 ---------- */
function makeAvatar(type) {
  const a = document.createElement('div');
  a.className = 'avatar';
  a.textContent = type === 'user' ? '我' : 'AI';
  return a;
}

function currentTime() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* 用户/助手消息返回 .bubble(便于流式填充);工具/错误消息返回外层容器。
 * 结构: [avatar] [ msg-body > bubble / msg-actions / msg-time ],避免操作条挤压气泡。 */
function appendMessage(text, type = 'bot') {
  const wrap = document.createElement('div');
  wrap.className = `msg ${type}`;

  if (type === 'user' || type === 'bot') {
    const avatar = makeAvatar(type);
    const body = document.createElement('div');
    body.className = 'msg-body';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    const act = document.createElement('div');
    act.className = 'msg-actions';
    const timeEl = document.createElement('div');
    timeEl.className = 'msg-time';
    timeEl.textContent = currentTime();

    if (type === 'bot') {
      if (text) bubble.innerHTML = renderMarkdown(text);
      const mk = (title, label, fn) => {
        const b = document.createElement('button');
        b.className = 'msg-action';
        b.title = title;
        b.textContent = label;
        b.addEventListener('click', fn);
        act.appendChild(b);
      };
      mk('复制回复', '复制', () => {
        const txt = (bubble.textContent || '').trim();
        if (txt) navigator.clipboard.writeText(txt).catch(() => {});
      });
      mk('重新生成', '重新生成', () => { if (App.Chat) App.Chat.regenerate(wrap); });
      mk('朗读该回复', '🔊', () => {
        const txt = (bubble.textContent || '').trim();
        if (txt) App.speak(txt);
      });
    } else {
      bubble.textContent = text;
      const mk = (title, label, fn) => {
        const b = document.createElement('button');
        b.className = 'msg-action';
        b.title = title;
        b.textContent = label;
        b.addEventListener('click', fn);
        act.appendChild(b);
      };
      mk('复制这条消息', '复制', () => {
        const txt = (bubble.textContent || '').trim();
        if (txt) navigator.clipboard.writeText(txt).catch(() => {});
      });
      mk('编辑这条消息并重新发送', '编辑', () => { if (App.Chat) App.Chat.editMessage(wrap, bubble); });
    }

    body.appendChild(bubble);
    body.appendChild(act);
    body.appendChild(timeEl);
    wrap.appendChild(avatar);
    wrap.appendChild(body);
    App.els.messages.appendChild(wrap);
    App.els.messages.scrollTop = App.els.messages.scrollHeight;
    // 确保欢迎页隐藏
    document.body.classList.add('has-messages');
    return bubble;
  }

  if (type === 'bot' && text) {
    wrap.innerHTML = renderMarkdown(text);
  } else {
    wrap.textContent = text;
  }
  App.els.messages.appendChild(wrap);
  App.els.messages.scrollTop = App.els.messages.scrollHeight;
  // 确保欢迎页隐藏
  document.body.classList.add('has-messages');
  return wrap;
}

function appendToolEvent(name, args, result) {
  const div = document.createElement('div');
  div.className = 'msg tool';

  const label = document.createElement('div');
  label.className = 'tool-label';
  label.textContent = `🔧 工具调用: ${name}`;
  div.appendChild(label);

  if (args) {
    let argsText = args;
    try {
      const obj = typeof args === 'string' ? JSON.parse(args) : args;
      if (obj && typeof obj === 'object') argsText = JSON.stringify(obj, null, 2);
    } catch (_) { /* 非 JSON 保持文本 */ }
    if (argsText && argsText !== '{}' && argsText !== '""') {
      const argsEl = document.createElement('div');
      argsEl.className = 'tool-args';
      argsEl.textContent = String(argsText).slice(0, 200);
      div.appendChild(argsEl);
    }
  }

  // 工具结果可能为 JSON:尝试解析出图片 URL,展示图片
  let imgUrl = '';
  let text = result || '';
  let parsedObj = null;
  try {
    const obj = typeof result === 'string' ? JSON.parse(result) : result;
    if (obj && typeof obj === 'object') {
      parsedObj = obj;
      if (obj.image_url) imgUrl = String(obj.image_url);
      const copy = { ...obj };
      if (typeof copy.image_url === 'string' && copy.image_url.startsWith('data:image')) {
        copy.image_url = `[图片已生成 ${Math.round(copy.image_url.length / 1.33)} 字节]`;
      }
      text = JSON.stringify(copy, null, 2).slice(0, 600);
    }
  } catch (_) { /* 非 JSON,按文本展示 */ }

  if (imgUrl) {
    const img = document.createElement('img');
    img.src = imgUrl;
    img.className = 'msg-img';
    img.alt = '生成图片';
    img.addEventListener('click', () => App.openLb(imgUrl, '生成图片'));
    div.appendChild(img);
    const actions = document.createElement('div');
    actions.className = 'tool-actions';
    const mkBtn = (txt, fn) => {
      const b = document.createElement('button');
      b.textContent = txt;
      b.className = 'lb-btn';
      b.addEventListener('click', fn);
      actions.appendChild(b);
    };
    mkBtn('🔍 放大', () => App.openLb(imgUrl, '生成图片'));
    mkBtn('📋 复制', () => App.copyImage(imgUrl));
    div.appendChild(actions);
  }

  const visual = renderToolVisual(name, parsedObj);
  if (visual) {
    const body = document.createElement('div');
    body.className = 'tool-result';
    body.innerHTML = visual;
    div.appendChild(body);
  } else if (text) {
    const body = document.createElement('div');
    body.className = 'tool-result';
    body.textContent = text.slice(0, 400);
    div.appendChild(body);
  }

  appendVersionButton(div, result);

  App.els.messages.appendChild(div);
  App.els.messages.scrollTop = App.els.messages.scrollHeight;
  return div;
}

/* 思考过程:折叠卡片(点击标题展开/收起) */
function appendThinking(content) {
  const div = document.createElement('div');
  div.className = 'msg thinking';
  const label = document.createElement('div');
  label.className = 'think-label';
  label.textContent = '💭 思考过程';
  const body = document.createElement('div');
  body.className = 'think-body';
  body.textContent = String(content).slice(0, 800);
  label.addEventListener('click', () => div.classList.toggle('open'));
  div.appendChild(label);
  div.appendChild(body);
  App.els.messages.appendChild(div);
  App.els.messages.scrollTop = App.els.messages.scrollHeight;
  return div;
}

/* ---------- 任务计划卡片 ---------- */
const PLAN_ICON = { pending: '○', in_progress: '◐', done: '✓', failed: '✕', skipped: '—' };

function planStatusText(s) {
  return { pending: '待办', in_progress: '进行中', done: '已完成', failed: '失败', skipped: '跳过' }[s] || s;
}

function appendPlan(evt) {
  const plan = evt.plan;
  if (!plan) return;
  let div;
  if (evt.action === 'create' || !App._planCard) {
    div = document.createElement('div');
    div.className = 'msg plan';
    const label = document.createElement('div');
    label.className = 'plan-label';
    label.textContent = '📋 任务计划';
    div.appendChild(label);
    App.els.messages.appendChild(div);
    App._planCard = div;
  } else {
    div = App._planCard;
  }
  App.renderPlanCard(div, plan);
  App.els.messages.scrollTop = App.els.messages.scrollHeight;
}

function renderPlanCard(div, plan) {
  div.querySelectorAll('.plan-steps').forEach((el) => el.remove());
  const list = document.createElement('div');
  list.className = 'plan-steps';
  (plan.steps || []).forEach((s, i) => {
    const row = document.createElement('div');
    row.className = `plan-step ${s.status}`;
    const icon = document.createElement('span');
    icon.className = 'plan-icon';
    icon.textContent = PLAN_ICON[s.status] || '○';
    const txt = document.createElement('span');
    txt.className = 'plan-text';
    txt.innerHTML = `<strong>${i + 1}. ${App.escapeHtml(s.title || '')}</strong>`
      + (s.description ? `<span class="plan-desc">${App.escapeHtml(s.description)}</span>` : '');
    const st = document.createElement('span');
    st.className = 'plan-status';
    st.textContent = planStatusText(s.status);
    row.appendChild(icon);
    row.appendChild(txt);
    row.appendChild(st);
    list.appendChild(row);
  });
  if (plan.completed) {
    const ok = document.createElement('div');
    ok.className = 'plan-done';
    ok.textContent = '✅ 计划全部完成';
    list.appendChild(ok);
  }
  div.appendChild(list);
}

/* ---------- 文件版本历史 / diff 视图 ---------- */
function lineDiff(aText, bText) {
  const a = String(aText || '').split('\n');
  const b = String(bText || '').split('\n');
  // LCS DP(行级)
  const n = a.length, m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { ops.push({ t: '=', text: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push({ t: '-', text: a[i] }); i++; }
    else { ops.push({ t: '+', text: b[j] }); j++; }
  }
  while (i < n) ops.push({ t: '-', text: a[i++] });
  while (j < m) ops.push({ t: '+', text: b[j++] });
  return ops;
}

function buildDiffHtml(ops) {
  const counts = { add: 0, del: 0 };
  ops.forEach((op) => { if (op.t === '+') counts.add++; if (op.t === '-') counts.del++; });
  const rows = ops.map((op) => {
    const cls = op.t === '+' ? 'diff-add' : op.t === '-' ? 'diff-del' : 'diff-eq';
    const sign = op.t === '+' ? '+' : op.t === '-' ? '-' : ' ';
    return `<div class="diff-line ${cls}"><span class="diff-sign">${sign}</span><span class="diff-text">${App.escapeHtml(op.text)}</span></div>`;
  }).join('');
  const stat = counts.add || counts.del
    ? `<div class="diff-stat">+${counts.add} / -${counts.del} 行</div>` : '<div class="diff-stat">无差异</div>';
  return stat + `<div class="diff-body">${rows || '<div class="diff-empty">(空文件)</div>'}</div>`;
}

/* ---------- 工具结果可视化:终端 / git / 代码搜索 ---------- */
function renderTerminal(o) {
  const status = o.success
    ? '<span class="term-ok">✓ 成功</span>'
    : o.blocked
      ? '<span class="term-blocked">⛔ 已拦截</span>'
      : '<span class="term-err">✗ 失败</span>';
  const meta = `exit ${o.returncode} · ${o.duration_s || 0}s` + (o.cwd ? ` · ${App.escapeHtml(o.cwd)}` : '');
  let html = `<div class="term-head">${status} <span class="term-meta">${meta}</span></div>`;
  if (o.error) html += `<div class="term-error">${App.escapeHtml(o.error)}</div>`;
  if (o.stdout) html += `<pre class="term-out">${App.escapeHtml(o.stdout)}</pre>`;
  if (o.stderr) html += `<pre class="term-out term-err-out">${App.escapeHtml(o.stderr)}</pre>`;
  if (o.timed_out) html += '<div class="term-error">⏱ 执行超时,已终止进程树</div>';
  return html;
}

function renderGitDiff(o) {
  const lines = String(o.diff || '').split('\n');
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  const rows = [];
  for (const raw of lines) {
    const line = raw.replace(/\r$/, '');
    if (line.startsWith('@@')) {
      rows.push(`<div class="diff-hunk">${App.escapeHtml(line)}</div>`);
    } else if (line.startsWith('diff ') || line.startsWith('index ') || line.startsWith('---') || line.startsWith('+++')) {
      rows.push(`<div class="diff-file">${App.escapeHtml(line)}</div>`);
    } else if (line.startsWith('+')) {
      rows.push(`<div class="diff-line diff-add"><span class="diff-sign">+</span><span class="diff-text">${App.escapeHtml(line.slice(1))}</span></div>`);
    } else if (line.startsWith('-')) {
      rows.push(`<div class="diff-line diff-del"><span class="diff-sign">-</span><span class="diff-text">${App.escapeHtml(line.slice(1))}</span></div>`);
    } else if (line.startsWith('\\')) {
      rows.push(`<div class="diff-line diff-eq"><span class="diff-sign"> </span><span class="diff-text">${App.escapeHtml(line)}</span></div>`);
    } else if (line.trim()) {
      rows.push(`<div class="diff-line diff-eq"><span class="diff-sign"> </span><span class="diff-text">${App.escapeHtml(line)}</span></div>`);
    } else {
      rows.push('<div class="diff-line diff-eq"><span class="diff-sign"> </span></div>');
    }
  }
  const head = `<div class="term-head">📑 ${App.escapeHtml(o.repo || '')}${o.staged ? ' · 已暂存' : ''}${o.path ? ' · ' + App.escapeHtml(o.path) : ''}</div>`;
  return head + (rows.length ? `<div class="diff-body">${rows.join('')}</div>` : '<div class="diff-stat">无差异</div>');
}

function renderGitStatus(o) {
  const head = `<div class="term-head">📍 ${App.escapeHtml(o.branch || '')}</div>`;
  const parts = [];
  if (o.staged_count) {
    parts.push(`<div class="gs-area">已暂存 (${o.staged_count})</div>`);
    parts.push(o.staged.map((s) => `<div class="gs-line gs-add">${App.escapeHtml(s)}</div>`).join(''));
  }
  if (o.unstaged_count) {
    parts.push(`<div class="gs-area">未暂存 (${o.unstaged_count})</div>`);
    parts.push(o.unstaged.map((s) => `<div class="gs-line">${App.escapeHtml(s)}</div>`).join(''));
  }
  if (o.untracked_count) {
    parts.push(`<div class="gs-area">未跟踪 (${o.untracked_count})</div>`);
    parts.push(o.untracked.map((s) => `<div class="gs-line gs-untracked">${App.escapeHtml(s)}</div>`).join(''));
  }
  if (!parts.length) return head + '<div class="term-error">无改动</div>';
  return head + parts.join('');
}

function renderCodeSearch(o) {
  const res = o.results || [];
  const head = `<div class="term-head">🔍 ${App.escapeHtml(o.query || '')} · ${res.length} 处${o.truncated ? ' (已截断)' : ''}</div>`;
  const items = res.slice(0, 40).map((r) => {
    let html = `<div class="cs-item"><span class="cs-loc">${App.escapeHtml(r.file)}:${r.line}</span> <span class="cs-text">${App.escapeHtml(r.text)}</span></div>`;
    if (r.context && r.context.length) {
      html += r.context.map((c) => `<div class="cs-ctx">${App.escapeHtml(c)}</div>`).join('');
    }
    return html;
  }).join('');
  return head + '<div class="cs-list">' + (items || '<div class="cs-empty">无匹配</div>') + '</div>';
}

function renderCodeSymbols(o) {
  const syms = o.symbols || [];
  const head = `<div class="term-head">🧩 ${syms.length} 个符号${o.truncated ? ' (已截断)' : ''}</div>`;
  return head + syms.slice(0, 120).map((s) => {
    const k = s.kind || '';
    const cls = k === 'class' ? 'cs-sym-class' : k === 'function' ? 'cs-sym-fn' : k === 'method' ? 'cs-sym-method' : k === 'interface' || k === 'type' ? 'cs-sym-intf' : 'cs-sym-other';
    return `<div class="cs-sym ${cls}"><span class="cs-loc">${App.escapeHtml(s.file)}:${s.line}</span> <span class="cs-sym-name">${App.escapeHtml(s.name)}</span> <span class="cs-sym-kind">${App.escapeHtml(k)}</span></div>`;
  }).join('');
}

function renderToolVisual(name, obj) {
  if (!obj || typeof obj !== 'object') return null;
  try {
    if (name === 'run_terminal') return renderTerminal(obj);
    if (name === 'git_diff') return renderGitDiff(obj);
    if (name === 'git_status') return renderGitStatus(obj);
    if (name === 'git_log') return '<div class="term-head">📜 提交历史</div><pre class="term-out">' + App.escapeHtml(obj.log || '') + '</pre>';
    if (name === 'git_branch') return '<div class="term-head">🌿 分支</div><pre class="term-out">' + App.escapeHtml((obj.branches || []).join('\n')) + '</pre>';
    if (name === 'code_search') return renderCodeSearch(obj);
    if (name === 'code_symbols') return renderCodeSymbols(obj);
    if (name === 'code_context') return '<div class="term-head">📄 ' + App.escapeHtml(obj.path || '') + ` L${obj.start_line}-${obj.end_line} / ${obj.total_lines}</div><pre class="term-out">` + App.escapeHtml(obj.content || '') + '</pre>';
    if (name === 'project_tree') return '<div class="term-head">🌳 项目结构 · ' + App.escapeHtml((obj.files || 0) + ' 文件 / ' + (obj.total_lines || 0) + ' 行') + '</div><pre class="term-out">' + App.escapeHtml(obj.tree || '') + '</pre>';
    if (name === 'code_semantic_search') return renderCodeSearch({ query: obj.query, results: (obj.results || []).map((r) => ({ file: r.file, line: '', text: `相关度 ${r.score}` })), truncated: false });
  } catch (_) { return null; }
  return null;
}

async function openVersionPanel(path) {
  const modal = document.createElement('div');
  modal.className = 'modal-mask';
  const panel = document.createElement('div');
  panel.className = 'ver-panel';
  panel.innerHTML = `
    <div class="ver-head">
      <strong>🕘 版本历史</strong>
      <span class="ver-path">${App.escapeHtml(path)}</span>
      <button class="ver-close">✕</button>
    </div>
    <div class="ver-body">
      <div class="ver-list"></div>
      <div class="ver-detail">
        <div class="ver-detail-empty">选择左侧版本查看变更差异,或恢复该版本</div>
      </div>
    </div>`;
  modal.appendChild(panel);
  document.body.appendChild(modal);
  modal.addEventListener('click', (e) => { if (e.target === modal) modal.remove(); });
  panel.querySelector('.ver-close').addEventListener('click', () => modal.remove());

  const listEl = panel.querySelector('.ver-list');
  const detailEl = panel.querySelector('.ver-detail');

  const renderDetail = async (ver) => {
    let cur = '';
    try {
      const r = await App.api(`/v1/files/content?path=${encodeURIComponent(path)}`);
      if (r.ok) cur = (await r.json()).content || '';
    } catch (_) { /* 忽略 */ }
    let vtext = '';
    try {
      const r = await App.api(`/v1/files/version?version_id=${ver.id}`);
      if (r.ok) vtext = (await r.json()).content || '';
    } catch (_) { /* 忽略 */ }
    const ops = lineDiff(cur, vtext);
    detailEl.innerHTML = `
      <div class="ver-title">版本 #${ver.id} (${ver.action === 'create' ? '创建前' : '写入前'}) ${new Date(ver.created_at * 1000).toLocaleString()}</div>
      <div class="ver-desc">当前文件 → 该版本</div>
      ${buildDiffHtml(ops)}
      <div class="ver-actions">
        <button class="lb-btn ver-restore">↩ 恢复此版本</button>
        <button class="lb-btn ver-undo">⏪ 撤销上次写入</button>
      </div>`;
    detailEl.querySelector('.ver-restore').addEventListener('click', async () => {
      try {
        const r = await App.api('/v1/files/restore', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path, version_id: ver.id }),
        });
        const d = await r.json();
        if (r.ok) { toast(`已恢复到版本 #${ver.id}`, 'ok'); await App.openVersionPanel(path); modal.remove(); }
        else toast(`恢复失败: ${d.detail || ''}`, 'err');
      } catch (e) { toast(`恢复失败: ${e.message}`, 'err'); }
    });
    detailEl.querySelector('.ver-undo').addEventListener('click', async () => {
      try {
        const r = await App.api('/v1/files/undo', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path }),
        });
        const d = await r.json();
        if (r.ok) { toast(`已撤销写入: ${d.action || 'restored'}`, 'ok'); await App.openVersionPanel(path); modal.remove(); }
        else toast(`撤销失败: ${d.detail || ''}`, 'err');
      } catch (e) { toast(`撤销失败: ${e.message}`, 'err'); }
    });
  };

  try {
    const res = await App.api(`/v1/files/history?path=${encodeURIComponent(path)}`);
    if (!res.ok) { listEl.innerHTML = '<div class="ver-empty">无法获取版本历史</div>'; return; }
    const data = await res.json();
    const versions = data.versions || [];
    if (!versions.length) { listEl.innerHTML = '<div class="ver-empty">该文件暂无版本历史</div>'; return; }
    listEl.innerHTML = '';
    for (const v of versions) {
      const item = document.createElement('div');
      item.className = 'ver-item';
      item.innerHTML = `<span class="ver-idx">#${v.id}</span>
        <span class="ver-meta">${v.action === 'create' ? '创建' : '覆盖'} · ${(v.size || 0)}B</span>
        <span class="ver-time">${new Date(v.created_at * 1000).toLocaleString()}</span>`;
      item.addEventListener('click', () => {
        listEl.querySelectorAll('.ver-item').forEach((el) => el.classList.remove('active'));
        item.classList.add('active');
        renderDetail(v);
      });
      listEl.appendChild(item);
    }
    listEl.querySelector('.ver-item').classList.add('active');
    renderDetail(versions[0]);
  } catch (e) {
    listEl.innerHTML = `<div class="ver-empty">加载失败: ${App.escapeHtml(e.message)}</div>`;
  }
}

async function appendVersionButton(div, result) {
  let obj = null;
  try { obj = typeof result === 'string' ? JSON.parse(result) : result; } catch (_) { obj = null; }
  if (!obj || typeof obj !== 'object' || !obj.path || obj.error) return;
  const isWrite = obj.action === 'created' || obj.action === 'overwritten';
  if (!isWrite) return;
  const actions = document.createElement('div');
  actions.className = 'tool-actions';
  const btn = document.createElement('button');
  btn.className = 'lb-btn';
  btn.textContent = `🕘 版本历史 (${obj.action === 'created' ? '新建' : '已备份'})`;
  btn.addEventListener('click', () => App.openVersionPanel(obj.path));
  actions.appendChild(btn);
  div.appendChild(actions);
}

function showTyping() {
  const wrap = document.createElement('div');
  wrap.className = 'msg bot';
  wrap.id = 'typing';
  const avatar = makeAvatar('bot');
  const body = document.createElement('div');
  body.className = 'msg-body';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = '<div class="typing-indicator"><span></span><span></span><span></span></div>';
  body.appendChild(bubble);
  wrap.appendChild(avatar);
  wrap.appendChild(body);
  App.els.messages.appendChild(wrap);
  App.els.messages.scrollTop = App.els.messages.scrollHeight;
}

function removeTyping() {
  const t = document.getElementById('typing');
  if (t) t.remove();
  const w = document.getElementById('welcome');
  if (w) w.style.display = 'none';
}

/* ---------- 灯箱 ---------- */
function openLb(src, name) {
  App.els.lbImg.src = src;
  App.els.lbImg.alt = name || '图片';
  App.els.lightbox.classList.add('open');
}

function closeLb() {
  App.els.lightbox.classList.remove('open');
  App.els.lbImg.src = '';
}

function downloadLb() {
  const src = App.els.lbImg.src;
  if (!src) return;
  const a = document.createElement('a');
  a.href = src;
  a.download = 'image.png';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function copyImage(src) {
  try {
    if (src.startsWith('data:')) {
      const resp = await fetch(src);
      const blob = await resp.blob();
      await navigator.clipboard.write([new ClipboardItem({ [blob.type || 'image/png']: blob })]);
    } else {
      await navigator.clipboard.writeText(src);
    }
    toast('📋 已复制图片', 'ok');
  } catch (e) {
    toast(`复制失败: ${e.message}`, 'err');
  }
}

function copyLb() { return copyImage(App.els.lbImg.src); }

/* ---------- 语音朗读 (TTS) ---------- */
let _audioEl = null;
function stopSpeak() {
  if (_audioEl) { try { _audioEl.pause(); } catch (_) {} _audioEl = null; }
}
async function speak(text) {
  stopSpeak();
  const clean = String(text).replace(/[#*`>|~\-_]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 2000);
  if (!clean) return;
  let lastErr = '';
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const res = await App.api('/v1/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: clean }),
      });
      if (!res.ok) {
        try { const d = await res.json(); lastErr = d.detail || `HTTP ${res.status}`; } catch (_) { lastErr = `HTTP ${res.status}`; }
        await new Promise((r) => setTimeout(r, 500));
        continue;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      _audioEl = new Audio(url);
      _audioEl.play();
      _audioEl.onended = () => { URL.revokeObjectURL(url); _audioEl = null; };
      _audioEl.onerror = () => { URL.revokeObjectURL(url); _audioEl = null; };
      return;
    } catch (e) {
      lastErr = e.message;
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  App.appendMessage(`朗读失败: ${lastErr}`, 'error');
}

/* ---------- 全局 toast(面板操作反馈) ---------- */
let _toastWrap = null;
function toast(msg, type = 'info') {
  if (!_toastWrap) {
    _toastWrap = document.createElement('div');
    _toastWrap.className = 'toast-wrap';
    document.body.appendChild(_toastWrap);
  }
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  _toastWrap.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 350);
  }, 3200);
}

/* ---------- 应用内确认弹窗(替代原生 confirm) ---------- */
function confirmDialog(message, opts = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    const card = document.createElement('div');
    card.className = 'confirm-dialog';
    const body = document.createElement('div');
    body.className = 'cd-body';
    body.textContent = message;
    const actions = document.createElement('div');
    actions.className = 'cd-actions';
    const cancel = document.createElement('button');
    cancel.className = 'lb-btn cd-cancel';
    cancel.textContent = '取消';
    const ok = document.createElement('button');
    ok.className = 'lb-btn cd-ok' + (opts.danger ? ' danger' : '');
    ok.textContent = opts.okText || '确定';
    actions.appendChild(cancel);
    actions.appendChild(ok);
    card.appendChild(body);
    card.appendChild(actions);
    mask.appendChild(card);
    document.body.appendChild(mask);

    function done(val) {
      if (settled) return;
      settled = true;
      document.removeEventListener('keydown', onKey);
      mask.remove();
      resolve(val);
    }
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); done(false); }
    }
    cancel.addEventListener('click', () => done(false));
    ok.addEventListener('click', () => done(true));
    mask.addEventListener('click', (e) => { if (e.target === mask) done(false); });
    document.addEventListener('keydown', onKey);
    ok.focus();
  });
}

/* ---------- API 封装(带超时保护) ---------- */
async function api(path, opts = {}) {
  opts = opts || {};
  const headers = opts.headers || {};
  if (App.backendApiKey) headers['Authorization'] = 'Bearer ' + App.backendApiKey;
  opts.headers = headers;
  // 默认 30s 超时,避免请求永久挂起
  if (!opts.signal) {
    const ctl = new AbortController();
    opts.signal = ctl.signal;
    setTimeout(() => ctl.abort(), 30000);
  }
  return fetch(App.backendUrl + path, opts);
}

/* ---------- 操作确认卡片(安全门禁) ----------
 * 后端在高风险工具执行前会创建待确认记录并阻塞等待;
 * 这里每 1.5s 轮询 pending 接口,渲染批准/拒绝卡片,用户操作后写回结果。
 */
App.Confirmation = {
  shown: {},
  _container: null,
  _interrupts: {}, // confirmation_id -> { run_id, session_id }
  _batches: {},    // run_id -> [confirmation_id, ...] 同一轮 interrupt 的确认集合
  _decisions: {},  // run_id -> { confirmation_id: { approved: bool } }

  _getContainer() {
    if (this._container) return this._container;
    this._container = document.getElementById('confirmBar');
    return this._container;
  },

  async poll() {
    let res;
    try {
      res = await App.api('/v1/confirmations/pending');
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

  render(c) {
    const bar = this._getContainer();
    if (!bar) return;
    const card = document.createElement('div');
    card.className = 'confirm-card';
    card.dataset.id = c.id;

    const head = document.createElement('div');
    head.className = 'confirm-head';
    head.textContent = '⚠️ 需要人工确认的操作';
    const act = document.createElement('span');
    act.className = 'confirm-tool';
    act.textContent = c.action || '';
    head.appendChild(act);
    const close = document.createElement('span');
    close.className = 'confirm-close';
    close.textContent = '✕';
    close.title = '忽略(按拒绝处理,不自动续跑)';
    close.addEventListener('click', () => this.dismiss(c.id));
    head.appendChild(close);

    const desc = document.createElement('div');
    desc.className = 'confirm-desc';
    desc.textContent = (c.description || '') + (c.created_at ? ` (${new Date(c.created_at).toLocaleTimeString()})` : '');

    const actions = document.createElement('div');
    actions.className = 'confirm-actions';
    const approve = document.createElement('button');
    approve.className = 'confirm-approve';
    approve.textContent = '✅ 批准';
    approve.addEventListener('click', () => this.respond(c.id, true));
    const reject = document.createElement('button');
    reject.className = 'confirm-reject';
    reject.textContent = '⛔ 拒绝';
    reject.addEventListener('click', () => this.respond(c.id, false));
    actions.appendChild(approve);
    actions.appendChild(reject);

    card.appendChild(head);
    card.appendChild(desc);
    card.appendChild(actions);
    bar.appendChild(card);
    bar.style.display = 'block';
    this.shown[c.id] = card;
  },

  async respond(id, approved, opts = {}) {
    let failed = '';
    try {
      const res = await App.api(`/v1/confirmations/${encodeURIComponent(id)}/respond`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approved, result: '' }),
      });
      const data = await res.json();
      if (data && data.status === 'error' && data.error) failed = data.error;
    } catch (e) {
      failed = e.message;
    }
    if (failed) App.appendMessage(`确认处理失败: ${failed}`, 'error');
    const card = this.shown[id];
    if (card) {
      card.querySelector('.confirm-actions').innerHTML =
        `<span class="confirm-result">${approved ? '✅ 已批准' : '⛔ 已拒绝'}</span>`;
      // 注意:不能在这里 delete this.shown[id],否则定时器里的 remove() 拿不到卡片,
      // DOM 中的卡片会永远残留(render() 又因 shown 已删除而拒绝重绘)。
      setTimeout(() => this.remove(id), 1500);
    }
    if (!opts.noResume) await this._maybeResume(id, approved);
  },

  /* 记录某次 interrupt 对应的确认集合,用于批准后自动续跑对话 */
  trackInterrupt(doneEvt) {
    const intr = (doneEvt && doneEvt.interrupt) || {};
    const runId = doneEvt.run_id || intr.run_id || '';
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
    const info = this._interrupts[id];
    if (!info || !info.run_id) return;
    this._decisions[info.run_id] = this._decisions[info.run_id] || {};
    this._decisions[info.run_id][id] = { approved };
    const batch = this._batches[info.run_id] || [id];
    const decided = Object.keys(this._decisions[info.run_id]);
    // 同一轮 interrupt 若有多个确认卡片,等全部决定后再一次续跑
    if (!batch.every((cid) => decided.indexOf(cid) >= 0)) return;
    const resumeValue = {};
    for (const cid of batch) {
      resumeValue[cid] = this._decisions[info.run_id][cid];
      delete this._interrupts[cid];
    }
    delete this._decisions[info.run_id];
    delete this._batches[info.run_id];
    await this._resumeRun(info.run_id, info.session_id, resumeValue);
  },

  async _resumeRun(runId, sessionId, resumeValue) {
    App.showTyping();
    App.Chat.setStreaming(true);
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 300000); // 5 分钟兜底
    try {
      const res = await App.api('/v1/chat/resume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: runId, session_id: sessionId, resume_value: resumeValue }),
        signal: ctl.signal,
      });
      const data = await res.json();
      if (!res.ok) {
        App.appendMessage(`续跑失败: ${data.detail || res.status}`, 'error');
      } else if (data.interrupted) {
        App.appendMessage(`⏸ 运行续跑后再次暂停: ${(data.interrupt && (data.interrupt.message || data.interrupt.type)) || '等待确认'}`, 'bot');
        // 嵌套确认:记录新的确认集合,后续批准/拒绝卡片可继续自动续跑
        if (data.interrupt && App.Confirmation) {
          App.Confirmation.trackInterrupt({ run_id: runId, interrupt: data.interrupt });
        }
      } else if (data.answer) {
        const div = App.appendMessage(data.answer, 'bot');
        div.innerHTML = App.renderMarkdown(data.answer);
      }
    } catch (e) {
      if (e.name !== 'AbortError') {
        App.appendMessage(`续跑失败: ${e.message}`, 'error');
      } else {
        App.appendMessage('续跑超时,请稍后在运行记录面板重试', 'error');
      }
    } finally {
      clearTimeout(timer);
      App.removeTyping();
      App.Chat.setStreaming(false);
    }
  },

  remove(id) {
    const card = this.shown[id];
    if (card && card.parentNode) card.parentNode.removeChild(card);
    delete this.shown[id];
    const bar = this._getContainer();
    if (bar && bar.children.length === 0) bar.style.display = 'none';
  },
};

function selectedProvider() {
  return App.els.providerSelect ? App.els.providerSelect.value : '';
}

/* ---------- 状态 ---------- */
function setStatus(ok, text) {
  const dot = App.els.statusDot;
  const txt = App.els.statusText;
  if (ok) {
    dot.className = 'status-dot ok';
    txt.textContent = '已连接';
  } else {
    dot.className = 'status-dot err';
    txt.textContent = text || '未连接';
  }
}

async function pingBackend() {
  try {
    const res = await fetch(`${App.backendUrl}/health`, { signal: AbortSignal.timeout(3000) });
    return res.ok;
  } catch (_) {
    return false;
  }
}

/* 挂载到命名空间 */
App.escapeHtml = escapeHtml;
App.escapeAttr = escapeAttr;
App.renderInline = renderInline;
App.renderMarkdown = renderMarkdown;
App.appendMessage = appendMessage;
App.appendToolEvent = appendToolEvent;
App.showTyping = showTyping;
App.removeTyping = removeTyping;
App.openLb = openLb;
App.closeLb = closeLb;
App.downloadLb = downloadLb;
App.copyImage = copyImage;
App.copyLb = copyLb;
App.speak = speak;
App.stopSpeak = stopSpeak;
App.appendThinking = appendThinking;
App.appendPlan = appendPlan;
App.renderPlanCard = renderPlanCard;
App.openVersionPanel = openVersionPanel;
App.lineDiff = lineDiff;
App.buildDiffHtml = buildDiffHtml;
App.api = api;
App.selectedProvider = selectedProvider;
App.setStatus = setStatus;
App.pingBackend = pingBackend;
App.copyCodeFromButton = copyCodeFromButton;
App.confirm = confirmDialog;
App.toast = toast;
