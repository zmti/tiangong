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

  /**
   * 渲染代码编辑变更卡片 (edit_file / apply_patch)
   * 显示: 文件路径 + 新增/删除行数统计 + unified diff 语法高亮
   */
  function renderCodeEdit(name, obj) {
    if (!obj || typeof obj !== 'object') return null;
    const path = obj.path || obj.file || '';
    const diff = String(obj.diff || '');
    const status = obj.status || 'ok';
    const risk = obj.risk || {};
    const riskLevel = risk.level || 'none';
    const riskLabel = { none: '低风险', low: '低', medium: '中', high: '高' }[riskLevel] || '未知';
    const riskIcon = { none: '🟢', low: '🟢', medium: '🟡', high: '🔴' }[riskLevel] || '⚪';

    // 统计新增/删除行数
    const addedLines = diff.split('\n').filter(l => l.startsWith('+') && !l.startsWith('+++')).length;
    const deletedLines = diff.split('\n').filter(l => l.startsWith('-') && !l.startsWith('---')).length;
    const delta = obj.change_delta != null ? obj.change_delta : (addedLines - deletedLines);
    const deltaStr = delta > 0 ? `+${delta}` : String(delta);

    // 解析 hunk 范围
    const hunkMatches = diff.match(/@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@/g) || [];

    // 构建 diff 行 HTML
    const rows = [];
    for (const raw of diff.split('\n')) {
      const line = raw.replace(/\r$/, '');
      if (line.startsWith('@@')) {
        rows.push(`<div class="ce-hunk">${App.escapeHtml(line)}</div>`);
      } else if (line.startsWith('---') || line.startsWith('+++')) {
        rows.push(`<div class="ce-file">${App.escapeHtml(line)}</div>`);
      } else if (line.startsWith('diff ')) {
        rows.push(`<div class="ce-file">${App.escapeHtml(line)}</div>`);
      } else if (line.startsWith('+')) {
        rows.push(`<div class="ce-line ce-add"><span class="ce-sign">+</span><span class="ce-text">${App.escapeHtml(line.slice(1))}</span></div>`);
      } else if (line.startsWith('-')) {
        rows.push(`<div class="ce-line ce-del"><span class="ce-sign">−</span><span class="ce-text">${App.escapeHtml(line.slice(1))}</span></div>`);
      } else if (line.startsWith(' ')) {
        rows.push(`<div class="ce-line ce-eq"><span class="ce-sign"> </span><span class="ce-text">${App.escapeHtml(line.slice(1))}</span></div>`);
      } else if (line.trim()) {
        rows.push(`<div class="ce-line ce-eq"><span class="ce-sign"></span><span class="ce-text">${App.escapeHtml(line)}</span></div>`);
      } else {
        rows.push(`<div class="ce-line ce-eq"><span class="ce-sign"></span></div>`);
      }
    }

    // 风险提示
    const riskHtml = riskLevel !== 'none'
      ? `<span class="ce-risk ce-risk-${riskLevel}">${riskIcon} ${riskLabel} 风险</span>`
      : '';

    const noteHtml = obj.note
      ? `<div class="ce-note">${App.escapeHtml(obj.note)}</div>`
      : '';

    const errorHtml = obj.error
      ? `<div class="ce-error">${App.escapeHtml(obj.error)}</div>`
      : '';

    // 头部信息
    const statusIcon = obj.status === 'ok' ? '✅' : obj.status === 'dry_run' ? '👁️' : '❌';
    const statusText = obj.status === 'ok' ? '已保存' : obj.status === 'dry_run' ? '预览模式' : '失败';
    const fileDisplay = path ? ` · ${App.escapeHtml(path)}` : '';

    return `
      <div class="ce-card">
        <div class="ce-head">
          <span class="ce-status">${statusIcon} ${App.escapeHtml(name)} — ${statusText}</span>
          <span class="ce-meta">${riskHtml}</span>
        </div>
        ${errorHtml}
        <div class="ce-stat">
          <span class="ce-stat-added">＋${addedLines} 行新增</span>
          <span class="ce-stat-del">－${deletedLines} 行删除</span>
          <span class="ce-stat-delta">净变化: ${deltaStr}</span>
          ${hunkMatches.length ? `<span class="ce-stat-hunks">${hunkMatches.length} 个 hunk</span>` : ''}
        </div>
        ${rows.length ? `<div class="ce-diff">${rows.join('')}</div>` : '<div class="ce-stat">无变更</div>'}
        ${noteHtml}
      </div>
    `;
  }

  /**
   * 渲染语法检查结果
   */
  function renderSyntaxCheck(obj) {
    if (!obj || typeof obj !== 'object') return null;
    const status = obj.status || 'ok';
    const icon = status === 'ok' ? '✅' : '❌';
    const label = status === 'ok' ? '语法正确' : '发现错误';
    const errors = obj.errors || [];

    const head = `<div class="term-head">${icon} 语法检查 — ${App.escapeHtml(obj.language || '未知语言')} · ${label}</div>`;
    if (!errors.length) return head + '<div class="term-out" style="color:var(--ok)">✓ 无语法错误</div>';

    const lines = errors.slice(0, 20).map(e =>
      `<div class="term-out term-err-out" style="margin:1px 0">${App.escapeHtml(e)}</div>`
    ).join('');
    return head + lines + (errors.length > 20 ? `<div class="term-out" style="color:var(--muted)">… 还有 ${errors.length - 20} 条错误</div>` : '');
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

  /**
   * 渲染代码执行结果 (run_code)
   * 注意: run_code 使用 output 字段(合并 stdout+stderr)，而非 stdout/stderr
   */
  function renderCodeRun(o) {
    const status = o.success
      ? '<span class="term-ok">✓ 成功</span>'
      : o.returncode === -1
        ? '<span class="term-err">⚠ 不支持的语言</span>'
        : '<span class="term-err">✗ 失败</span>';
    const lang = o.language || 'python';
    const meta = `exit ${o.returncode} · ${o.duration_s || 0}s · ${App.escapeHtml(o.mode || 'subprocess')}`;
    let html = `<div class="term-head">${status} <span class="term-meta">${meta}</span></div>`;
    // run_code 将 stdout+stderr 合并到 output 字段
    if (o.output) {
      const isErr = !o.success || (o.returncode && o.returncode !== 0);
      html += `<pre class="term-out${isErr ? ' term-err-out' : ''}">${App.escapeHtml(o.output)}</pre>`;
    }
    if (o.error) html += `<div class="term-error">${App.escapeHtml(o.error)}</div>`;
    if (o.timed_out) html += '<div class="term-error">⏱ 执行超时</div>';
    return html;
  }

  function renderToolVisual(name, obj) {
    if (!obj || typeof obj !== 'object') return null;
    try {
      // 代码编辑变更 (edit_file / apply_patch)
      if (name === 'edit_file' || name === 'apply_patch') return renderCodeEdit(name, obj);
      // 语法检查 (check_syntax)
      if (name === 'check_syntax') return renderSyntaxCheck(obj);
      // 代码执行 (run_code) - 使用 output 字段而非 stdout/stderr
      if (name === 'run_code') return renderCodeRun(obj);
      // 原有渲染器
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
  App.renderCodeEdit = renderCodeEdit;
  App.renderSyntaxCheck = renderSyntaxCheck;
})();
