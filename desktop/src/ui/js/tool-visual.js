/* 工具结果可视化 */
(function () {
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

  function renderToolVisual(name, obj) {
    if (!obj || typeof obj !== 'object') return null;
    try {
      if (name === 'run_terminal') return renderTerminal(obj);
      if (name === 'git_diff') return renderGitDiff(obj);
      if (name === 'git_status') return renderGitStatus(obj);
      if (name === 'git_log') return '<div class="term-head">📜 提交历史</div><pre class="term-out">' + App.escapeHtml(obj.log || '') + '</pre>';
      if (name === 'git_branch') return '<div class="term-head">🌿 分支</div><pre class="term-out">' + App.escapeHtml((obj.branches || []).join('\n')) + '</pre>';
      if (name === 'code_search') return renderCodeSearch(obj);
      if (name === 'generate_image') return null;
      if (name === 'generate_video') return `<div class="term-head">🎬 视频已生成</div><div class="term-out">${App.escapeHtml(obj.duration ? '时长: ' + obj.duration + 's' : (obj.format || ''))}</div>`;
    } catch (_) { return null; }
    return null;
  }

  window.App = window.App || {};
  App.renderToolVisual = renderToolVisual;
})();
