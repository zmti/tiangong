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
    try {
    let s = escapeHtml(line);
    // 段落内换行:单个 \n → <br>(GFM 软换行)
    s = s.replace(/\n/g, '<br>');
    // 图片
    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, u) => {
      const url = safeUrl(u);
      if (!url) return '';
      return `<img src="${escapeAttr(url)}" alt="${escapeAttr(alt || '图片')}" class="msg-img">`;
    });
    // 代码
    s = s.replace(/`([^`]+)`/g, (m, code) => `<code>${code}</code>`);
    // 加粗
    s = s.replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>');
    // 斜体
    s = s.replace(/([^*])\*([^*\s][\s\S]*?)\*(?!\*)/g, '$1<em>$2</em>');
    // 链接
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, t, u) => {
      const url = safeUrl(u);
      if (!url) return t;
      return `<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${t}</a>`;
    });
    return s;
    } catch (e) {
      console.warn('[renderInline] error:', e.message, 'line_preview:', line.slice(0, 100));
      return escapeHtml(line).replace(/\n/g, '<br>');
    }
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
// 简单缓存:避免相同内容重复渲染(流式过程中频繁调用)
let _mdCacheKey = '';
let _mdCacheHtml = '';
function renderMarkdown(md) {
    if (!md) return '';
    const text = String(md);
    if (!text.trim()) return '';
    if (text === _mdCacheKey) { console.log('[renderMarkdown] cache hit, len:', text.length); return _mdCacheHtml; }
    console.log('[renderMarkdown] start, len:', text.length, 'preview:', text.slice(0, 100));
    try {
    // 严格剥离thinking标签(多行、贪婪匹配)
    let cleaned = text.replace(/<thinking[\s\S]*?<\/thinking>/gi, '').trim();
    // 也剥离无对应关闭标签的残留 open/close tag
    cleaned = cleaned.replace(/<thinking[^>]*>/gi, '').replace(/<\/thinking>/gi, '').trim();
    // 剥离 LLM 内部 XML 标签块(不渲染)
    cleaned = cleaned.replace(/<system[\s\S]*?<\/system>/gi, '').trim();
    cleaned = cleaned.replace(/<plan[\s\S]*?<\/plan>/gi, '').trim();
    cleaned = cleaned.replace(/<phase[\s\S]*?<\/phase>/gi, '').trim();
    cleaned = cleaned.replace(/<analysis[\s\S]*?<\/analysis>/gi, '').trim();
    cleaned = cleaned.replace(/<reflection[\s\S]*?<\/reflection>/gi, '').trim();
    cleaned = cleaned.replace(/<context[\s\S]*?<\/context>/gi, '').trim();
    cleaned = cleaned.replace(/<instruction[\s\S]*?<\/instruction>/gi, '').trim();
    cleaned = cleaned.replace(/<output[\s\S]*?<\/output>/gi, '').trim();
    cleaned = cleaned.replace(/<reasoning[\s\S]*?<\/reasoning>/gi, '').trim();
    cleaned = cleaned.replace(/<tool_call[\s\S]*?<\/tool_call>/gi, '').trim();
    cleaned = cleaned.replace(/<function[\s\S]*?<\/function>/gi, '').trim();
    cleaned = cleaned.replace(/<parameter[\s\S]*?<\/parameter>/gi, '').trim();
    // 剥离残留的单个 open/close LLM 标签
    cleaned = cleaned.replace(/<\/?(?:system|plan|phase|analysis|reflection|context|instruction|output|reasoning|tool_call|function|parameter)[^>]*>/gi, '').trim();
    // 剥离 LLM 输出的 summary/details 等 HTML 标签(不渲染)
    cleaned = cleaned.replace(/<\/?summary[^>]*>/gi, '')
      .replace(/<\/?details[^>]*>/gi, '')
      .replace(/<detailsopen>/gi, '')
      .replace(/<detailsclosed>/gi, '')
      .trim();
    // thinking 块被移除后通常会前后各留一个空行, 先合并多余的空行(4+ → 2)
    cleaned = cleaned.replace(/\n[ \t]*\n[ \t]*\n[ \t]*\n+/g, '\n\n');
    // 合并"单个换行"为"双换行"会让段落之间多一个空行; 但绝不合并行首行尾空白 ——
    // 旧版此处 [ \t]+ → ' ' 会把整段(含代码块/缩进)的首尾空白全吃掉, 造成:
    //   ① 代码块内缩进/空行丢失 ② 段落被错误拆分 ③ 列表对齐错乱, 观感"乱码/看不懂"。
    cleaned = cleaned.replace(/\n{3,}/g, '\n\n').trim();
    // 展开 LLM 压缩表格: ||分隔的行展开为独立行(如 | A | B ||| C | D | → 两行)
    cleaned = cleaned.replace(/\|\|(\s*\|)/g, '\n|$1');
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
        try {
          if (typeof item === 'string') html += `<li>${renderInline(item)}</li>`;
          else html += `<li>${escapeHtml(String(item))}</li>`;
        } catch (e) {
          console.warn('[flushList] item error:', e.message);
          html += `<li>${escapeHtml(String(item || ''))}</li>`;
        }
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
    let paraBuf = [];  // 收集连续段落行,GFM 软换行合并为一个 <p>

    function flushPara() {
      if (!paraBuf.length) return;
      const text = paraBuf.join(' ');
      paraBuf = [];
      html += `<p>${renderInline(text)}</p>`;
    }

    while (i < lines.length) {
      const line = lines[i].replace(/\s+$/, '');
      if (line.startsWith('```')) {
        flushPara(); flushList();
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
        flushPara(); flushList();
        const level = line.match(/^#{1,6}/)[0].length;
        html += `<h${level}>${renderInline(line.replace(/^#{1,6}\s+/, ''))}</h${level}>`;
      } else if (/^\s*[-*+]\s+/.test(line)) {
        flushPara();
        listType = 'ul';
        listBuf.push(line.replace(/^\s*[-*+]\s+/, ''));
      } else if (/^\s*\d+[.)]\s+/.test(line)) {
        flushPara();
        listType = 'ol';
        listBuf.push(line.replace(/^\s*\d+[.)]\s+/, ''));
      } else if (/^\s*>\s?/.test(line)) {
        flushPara(); flushList();
        html += `<blockquote>${renderInline(line.replace(/^\s*>\s?/, ''))}</blockquote>`;
      } else if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|[\s:]*-+[\s:]*\|/.test(lines[i + 1])) {
        // GFM 表格:当前行是表头,下一行是分隔行
        flushPara(); flushList();
        const parseRow = (row) => row.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
        const headers = parseRow(line);
        const aligns = parseRow(lines[i + 1]).map(c => {
          if (/^:-+:$/.test(c)) return 'center';
          if (/^-+:$/.test(c)) return 'right';
          return 'left';
        });
        i += 2; // 跳过表头和分隔行
        let tableHtml = '<div class="md-table-wrap"><table><thead><tr>';
        headers.forEach((h, j) => {
          const align = aligns[j] || 'left';
          tableHtml += `<th style="text-align:${align}">${renderInline(h)}</th>`;
        });
        tableHtml += '</tr></thead><tbody>';
        while (i < lines.length && /^\s*\|/.test(lines[i])) {
          const cells = parseRow(lines[i]);
          tableHtml += '<tr>';
          cells.forEach((c, j) => {
            const align = aligns[j] || 'left';
            tableHtml += `<td style="text-align:${align}">${renderInline(c)}</td>`;
          });
          tableHtml += '</tr>';
          i++;
        }
        tableHtml += '</tbody></table></div>';
        html += tableHtml;
        continue; // while 循环的 i++ 已在上方跳过,这里 continue 防止多加
      } else if (/^\s*---+\s*$/.test(line)) {
        flushPara(); flushList();
        html += '<hr>';
      } else if (line.trim() === '') {
        flushPara(); flushList();
      } else {
        flushList();
        // GFM 软换行: 连续非空行合并为一个段落
        paraBuf.push(line);
      }
      i++;
    }
    flushPara(); flushList();
    if (inCode) flushCodeBlock();
    console.log('[renderMarkdown] done, html_len:', html.length, 'html_preview:', html.slice(0, 200));
    _mdCacheKey = text;
    _mdCacheHtml = html;
    return html;
    } catch (e) {
      console.error('[renderMarkdown] CRASH:', e.message, e.stack, 'input_len:', md.length, 'input_preview:', md.slice(0, 200));
      // 降级:至少把换行和基础转义保留
      return '<p style="white-space:pre-wrap;">' + md.replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</p>';
    }
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
