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

  /* ── 未闭合围栏救援(2026-09-20 "内容嵌套格式样式"修复) ──
   * 现象:模型常把"正文/计划"塞进一个 ``` 围栏里(如 ```text)却忘了写闭合 ```,
   * 于是围栏之后的所有内容(##、**、反引号等 markdown 语法)被整体丢进代码块,
   * 用户看到的是一整墙"原始 markdown + 语法高亮"的看不懂的代码。
   * 处理:若围栏未闭合,判断围栏体到底是"代码"还是"正文/markdown":
   *   ① 完全没有代码特征(无缩进/运算符/关键字,且以中文散文为主)→ 视为误开栏,
   *      直接丢弃该围栏标记,整段按 markdown 渲染;
   *   ② 前段有代码特征、后段(空行 + markdown 结构行,且中文占比够高)是正文
   *      → 在分界处补一个闭合 ```,前段留代码块、后段按 markdown 渲染;
   *   ③ 看起来就是代码(流式写代码中的常态)→ 原样不动,交给正常路径。
   */
  const MD_STRUCT_RE = /^\s{0,3}(?:#{1,6}\s|[-*+]\s|\d{1,3}[.)、]\s|>\s|\*\*)/;

  function hasCjk(s) { return /[\u4e00-\u9fff]/.test(s); }

  function cjkRatio(s) {
    const m = s.match(/[\u4e00-\u9fff]/g);
    return s.length ? (m ? m.length : 0) / s.length : 0;
  }

  // 是否具备"代码特征":缩进行,或运算符/关键字/标签(含中文的行一律不算,散文优先)
  function hasCodeSignal(lines) {
    return lines.some((l) => {
      const t = l.trim();
      if (!t || hasCjk(t)) return false;
      if (/^\s{2,}\S/.test(l)) return true;
      return /[=;{}]|->|=>|\bdef\b|\bclass\b|\bimport\b|\breturn\b|\bconst\b|\bfunction\b|<\/?[a-z][\w-]*>/.test(t);
    });
  }

  // 返回:0=整段是正文(围栏误开) / n>0=从 n 起是正文 / -1=整段当代码
  function findFenceRescue(body) {
    const head = body.slice(0, 8); // 开头若干行是否已有代码特征
    if (!hasCodeSignal(head)) return 0;
    for (let i = 1; i < body.length; i++) {
      if (body[i - 1].trim() !== '') continue; // 只在空行后找分界
      let j = i;
      while (j < body.length && !body[j].trim()) j++;
      if (j >= body.length) break;
      if (!MD_STRUCT_RE.test(body[j])) continue;
      const rest = body.slice(j);
      if (rest.filter((l) => MD_STRUCT_RE.test(l)).length < 2) continue;
      if (cjkRatio(rest.join('\n')) < 0.2) continue; // 尾段几乎无中文 → 更像代码
      return i;
    }
    return -1;
  }

  function autoCloseStrayFence(text) {
    const lines = text.split('\n');
    let open = -1;
    for (let i = 0; i < lines.length; i++) {
      if (!lines[i].startsWith('```')) continue;
      open = open < 0 ? i : -1;
    }
    if (open < 0) return text; // 成对 → 不动
    const body = lines.slice(open + 1);
    const split = findFenceRescue(body);
    if (split < 0) return text;
    if (split === 0) return lines.slice(0, open).concat(body).join('\n'); // 丢弃误开的围栏行
    const out = lines.slice(0, open + 1).concat(body.slice(0, split));
    out.push('```');
    const tail = body.slice(split).join('\n');
    if (tail.trim()) out.push('', tail);
    return out.join('\n');
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
    // 日志全部走 App.DEBUG 门控(默认关):流式渲染每帧都会调用本函数,
    // 常开会把控制台刷爆。排查时执行 App.setDebug(true) 即可恢复。
    if (text === _mdCacheKey) {
      if (window.App && App.DEBUG) console.log('[renderMarkdown] cache hit, len:', text.length);
      return _mdCacheHtml;
    }
    if (window.App && App.DEBUG) console.log('[renderMarkdown] start, len:', text.length, 'preview:', text.slice(0, 100));
    try {
    // 严格剥离thinking标签(多行、贪婪匹配)
    // 兼容模型私有变体:<thinking>/<ant:thinking>/<antThinking>/<|thinking|> 等,
    // 变体不被识别时整段思考原文会随答案一起渲染(历史回放无流式过滤,必须在这里兜底)。
    let cleaned = text
      .replace(/<(?:\|)?\/?(?:\|)?(?:[a-zA-Z][\w.:-]*)?thinking[\w.:-]*(?:\|)?[^>]*>[\s\S]*?<(?:\|)?\/?(?:\|)?(?:[a-zA-Z][\w.:-]*)?thinking[\w.:-]*(?:\|)?[^>]*>/gi, '')
      .replace(/<(?:\|)?\/?(?:\|)?(?:[a-zA-Z][\w.:-]*)?thinking[\w.:-]*(?:\|)?[^>]*>/gi, '')
      .trim();
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
    // 行中标题拆分: "……结论。##解题思路" → 标题独占一行。
    // LLM 偶尔把标题直接接在句号后(换行丢失),不拆则 ## 永远混在段落正文中。
    // 仅在 CJK 句读标点后触发,避免误伤英文/代码场景;# 后接字母数字(如 "#1")不算标题。
    cleaned = cleaned.replace(/([。！？；])(\s*)(#{1,6}(?:\s|[^\s#a-zA-Z0-9]))/g, '$1\n$2$3');
    // 未闭合围栏救援:见 autoCloseStrayFence 注释(必须在拆行前处理,它只会补/删围栏行)
    cleaned = autoCloseStrayFence(cleaned);
    const lines = cleaned.replace(/\r\n/g, '\n').split('\n');
    let html = '';
    let inCode = false;
    let codeLang = '';
    let codeBuf = [];
    let listBuf = [];   // [{ indent, ordered, num, text }] —— 由 flushList 组装成嵌套列表

    /* 列表渲染(2026-09-20 "内容嵌套格式样式"修复):
     * 旧实现把 "1. xxx" 与其下缩进的 "- yyy" 全部平铺进同一个 <ul>,
     * 且后出现的 "- " 会把整个列表的 listType 改成 ul(有序号丢失) —— 嵌套层级全丢,
     * 观感是"子项和父项一样平"。这里按缩进递归建树:
     *   缩进更深 → 作为上一项的嵌套子列表;同级但标记类型变了 → 另起一个列表。 */
    function buildList(items, start) {
      const indent = items[start].indent;
      const ordered = items[start].ordered;
      const nodes = [];
      let i = start;
      while (i < items.length) {
        const it = items[i];
        if (it.indent < indent) break;
        if (it.indent === indent && it.ordered !== ordered) break;
        if (it.indent > indent) break; // 防御:不该出现
        const node = { text: it.text, kids: [] };
        i++;
        while (i < items.length && items[i].indent > indent) {
          const sub = buildList(items, i);
          node.kids.push(sub.html);
          i = sub.next;
        }
        nodes.push(node);
      }
      const tag = ordered ? 'ol' : 'ul';
      const startAttr = (ordered && items[start].num > 1) ? ` start="${items[start].num}"` : '';
      let h = `<${tag}${startAttr}>`;
      for (const n of nodes) {
        h += `<li>${renderInline(n.text)}${n.kids.join('')}</li>`;
      }
      h += `</${tag}>`;
      return { html: h, next: i };
    }

    function flushList() {
      if (!listBuf.length) return;
      const items = listBuf;
      listBuf = [];
      try {
        let html2 = '';
        let i = 0;
        while (i < items.length) {
          const r = buildList(items, i);
          html2 += r.html;
          i = r.next > i ? r.next : i + 1;
        }
        html += html2;
      } catch (e) {
        console.warn('[flushList] nested build error:', e.message);
        const tag = items[0] && items[0].ordered ? 'ol' : 'ul';
        html += `<${tag}>` + items.map((it) => `<li>${renderInline(it.text || '')}</li>`).join('') + `</${tag}>`;
      }
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
      // LLM 输出普遍用"单个换行"分隔块级元素(标题/列表前后无空行)。
      // 旧实现 join(' ') 把连续行硬合并成一个巨型段落 → "##标题""1.列表"全部挤进正文,
      // 观感为一整坨密集文字。改为 join('\n')(renderInline 会把 \n 转为 <br>),
      // 使单换行保留为视觉换行;真正的块级结构仍由下方各分支(标题/列表)接管。
      const text = paraBuf.join('\n');
      paraBuf = [];
      html += `<p>${renderInline(text)}</p>`;
    }

    while (i < lines.length) {
      const line = lines[i].replace(/\s+$/, '');
      try { // 单行隔离:任何一行渲染异常只降级该行,绝不让整篇答案退化为"原始文本墙"
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

      // 标题解析(容错增强):
      // ① 崩溃修复: 旧实现 line.match(/^#{1,6}/) 在带缩进标题(如 "  ### 标题")时返回 null,
      //    随后 [0].length 抛 TypeError → 整篇 markdown 渲染失败,降级为"原始文本墙"
      //    (字面 ###/##/反引号/** 全部漏出 —— 实时输出"一大坨看不懂"的主要成因)。
      // ② LLM 容错: 标题被 ** 包裹("**## 标题**")或 # 后漏空格("###标题")时同样按标题渲染。
      let hLine = line;
      // ③ "**## 标题**：正文..." 加粗包裹标题且后跟内容:拆成 <hN>标题</hN> + <p>正文</p>
      //    (旧逻辑只处理整行恰好是 "**## 标题**" 的情况,带尾随内容时 ## 字面漏出)
      const bmh = hLine.match(/^\s*\*\*(#{1,6})\s*([^*]+?)\s*\*\*([\s\S]*)$/);
      if (bmh) {
        flushPara(); flushList();
        html += `<h${bmh[1].length}>${renderInline(bmh[2])}</h${bmh[1].length}>`;
        const rest = bmh[3].replace(/^[\s:：]+/, '');
        if (rest) html += `<p>${renderInline(rest)}</p>`;
        i++;
        continue;
      }
      const boldWrap = hLine.match(/^\s*\*\*(.+?)\*\*\s*:?\s*$/);
      if (boldWrap && /^\s{0,3}#{1,6}/.test(boldWrap[1])) hLine = boldWrap[1];
      const mh = hLine.match(/^\s{0,3}(#{1,6})(?:\s+|[^\s#a-zA-Z0-9])/);
      if (mh) {
        flushPara(); flushList();
        html += `<h${mh[1].length}>${renderInline(hLine.replace(/^\s{0,3}#{1,6}\s*/, ''))}</h${mh[1].length}>`;
      } else if (/^\s*[-*+]\s+/.test(line)) {
        flushPara();
        const indent = (line.match(/^\s*/) || [''])[0].replace(/\t/g, '    ').length;
        listBuf.push({ indent, ordered: false, num: 0, text: line.replace(/^\s*[-*+]\s+/, '') });
      } else if (/^\s*\d{1,3}[.)、]\s*(?!\d)/.test(line)) {
        // LLM 容错: 中文列表常写 "1.先分三类"(点后无空格) 或 "1、xxx",同样按有序列表渲染;
        // (?!\d) 排除 "3.14 是 pi" 这类小数/版本号被误判为列表
        flushPara();
        const indent = (line.match(/^\s*/) || [''])[0].replace(/\t/g, '    ').length;
        const num = parseInt(line.replace(/^\s*(\d{1,3})[.)、].*$/, '$1'), 10);
        listBuf.push({ indent, ordered: true, num: isNaN(num) ? 0 : num, text: line.replace(/^\s*\d{1,3}[.)、]\s*/, '') });
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
      } catch (lineErr) {
        console.error('[renderMarkdown] line isolated (render skipped for this line):',
          lineErr && lineErr.message, '| line_preview:', line.slice(0, 120));
        paraBuf = []; listBuf = [];
        html += '<p>' + escapeHtml(line) + '</p>';
        i++;
      }
    }
    flushPara(); flushList();
    if (inCode) flushCodeBlock();
    if (window.App && App.DEBUG) console.log('[renderMarkdown] done, html_len:', html.length, 'html_preview:', html.slice(0, 200));
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
