/* Markdown 渲染:轻量实现，无外部依赖 */
(function () {
  // HTML 转义
  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function escapeAttr(s) {
    return escapeHtml(s).replace(/"/g, '&quot;');
  }

  // 安全 URL 协议白名单
  function safeUrl(u) {
    const s = String(u).trim();
    const m = s.match(/^([a-zA-Z][a-zA-Z0-9+.-]*):/);
    if (m) {
      const scheme = m[1].toLowerCase();
      if (!['http', 'https', 'data', 'mailto'].includes(scheme)) return '';
    }
    return s;
  }

  // 内联渲染
  function renderInline(line) {
    let s = escapeHtml(line);
    // 图片
    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, u) => {
      const url = safeUrl(u);
      if (!url) return '';
      return `<img src="${escapeAttr(url)}" alt="${escapeAttr(alt || '图片')}" class="msg-img">`;
    });
    // 代码
    s = s.replace(/`([^`]+)`/g, (m, code) => `<code>${code}</code>`);
    // 加粗
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // 斜体
    s = s.replace(/([^*])\*([^*\s][^*]*?)\*(?!\*)/g, '$1<em>$2</em>');
    // 链接
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, t, u) => {
      const url = safeUrl(u);
      if (!url) return t;
      return `<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${t}</a>`;
    });
    return s;
  }

  // 代码高亮关键词表
  const CODE_KEYWORDS = {
    py: ['def', 'class', 'import', 'from', 'return', 'if', 'elif', 'else', 'for', 'while', 'try', 'except', 'finally', 'with', 'as', 'lambda', 'pass', 'break', 'continue', 'and', 'or', 'not', 'in', 'is', 'raise', 'yield', 'async', 'await', 'global', 'nonlocal', 'del', 'print', 'None', 'True', 'False', 'self', 'super', 'assert', 'match', 'case'],
    js: ['function', 'const', 'let', 'var', 'return', 'if', 'else', 'for', 'while', 'do', 'class', 'new', 'import', 'export', 'default', 'from', 'async', 'await', 'try', 'catch', 'finally', 'throw', 'switch', 'case', 'break', 'continue', 'typeof', 'instanceof', 'this', 'null', 'undefined', 'true', 'false', 'of', 'in', 'yield', 'delete', 'void'],
    ts: ['function', 'const', 'let', 'var', 'return', 'if', 'else', 'for', 'while', 'do', 'class', 'interface', 'type', 'enum', 'implements', 'extends', 'new', 'import', 'export', 'default', 'from', 'async', 'await', 'try', 'catch', 'finally', 'throw', 'switch', 'case', 'break', 'continue', 'typeof', 'instanceof', 'this', 'null', 'undefined', 'true', 'false', 'of', 'in', 'readonly', 'public', 'private', 'protected', 'static', 'abstract', 'yield', 'delete', 'void'],
  };

  const LANG_ALIAS = {
    python: 'py', javascript: 'js', jsx: 'js', mjs: 'js', cjs: 'js',
    typescript: 'ts', tsx: 'ts',
  };

  function normLang(lang) {
    const l = String(lang).toLowerCase().trim();
    return LANG_ALIAS[l] || l;
  }

  // 轻量语法高亮
  function highlightCode(lang, code) {
    const kw = new Set((CODE_KEYWORDS[normLang(lang)] || []).map(k => k.toLowerCase()));
    const tokenRe = /("(?:[^"\\n]|\\.)*"|'(?:[^'\\n]|\\.)*'|`(?:[^`\\n]|\\.)*`|\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?\b|\b[A-Za-z_]\w*\b)/g;
    return String(code).split('\n').map((raw) => {
      const line = raw.replace(/\r$/, '');
      let html = '';
      let last = 0;
      tokenRe.lastIndex = 0;
      let m;
      while ((m = tokenRe.exec(line)) !== null) {
        if (m.index > last) html += escapeHtml(line.slice(last, m.index));
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
      if (last < line.length) html += escapeHtml(line.slice(last));
      return html;
    }).join('\n');
  }

  // highlight.js 入口（优先使用）
  function hljsHighlight(lang, code) {
    const H = window.hljs;
    if (H) {
      try {
        const l = String(lang || '').toLowerCase().trim();
        if (l && H.getLanguage(l)) return H.highlight(code, { language: l, ignoreIllegals: true }).value;
        const alias = LANG_ALIAS[l];
        if (alias && H.getLanguage(alias)) return H.highlight(code, { language: alias, ignoreIllegals: true }).value;
        if (!l) return H.highlightAuto(code).value;
      } catch (_) {}
    }
    return highlightCode(lang, code);
  }

// Markdown 渲染
function renderMarkdown(md) {
    if (!md) return '';
    const text = String(md);
    if (!text.trim()) return '';
    const cleaned = text.replace(/<thinking[^>]*>[\s\S]*?<\/thinking>/gi, '').trim();
    const lines = cleaned.replace(/\r\n/g, '\n').split('\n');
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
      const highlighted = hljsHighlight(lang, codeText);
      const lineCount = codeBuf.length;
      const foldable = lineCount > 30;
      html += `<div class="code-block${foldable ? ' foldable collapsed' : ''}">`
        + `<div class="code-head">`
        + `<span class="code-lang">${escapeHtml(normLang(lang) || 'text')}</span>`
        + `<button class="code-copy" type="button" title="复制代码">复制</button>`
        + `</div>`
        + `<div class="code-body"><pre><code${lang ? ` class="lang-${escapeAttr(lang)}"` : ''}>${highlighted}</code></pre></div>`
        + (foldable ? `<button class="code-fold-toggle" type="button">⤵ 展开全部 ${lineCount} 行</button>` : '')
        + `</div>`;
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

      if (/^\s{0,3}#{1,6}\s/.test(line)) {
        flushList();
        const level = line.match(/^#{1,6}/)[0].length;
        html += `<h${level}>${renderInline(line.replace(/^#{1,6}\s+/, ''))}</h${level}>`;
      } else if (/^\s*[-*+]\s+/.test(line)) {
        listType = 'ul';
        listBuf.push(line.replace(/^\s*[-*+]\s+/, ''));
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

  // 复制到剪贴板
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

  window.App = window.App || {};
  App.escapeHtml = escapeHtml;
  App.escapeAttr = escapeAttr;
  App.renderInline = renderInline;
  App.renderMarkdown = renderMarkdown;
  App.copyCodeFromButton = copyCodeFromButton;
})();
