/* briefing.js — Daily session summary dashboard */
(function () {
  'use strict';

  const $ = (sel, ctx) => (ctx || document).querySelector(sel);

  /* 修复"每日简报不可用": 旧实现调 GET /v1/sessions(裸, 404),
     后端实际端点是 /v1/chat/sessions(返回 {sessions:[...]}),
     且字段是 message_count(无 token_count), 时间戳单位是秒。
     现改走 App.apiJSON + 正确端点 + 字段适配。 */
  async function loadBriefing() {
    let sessions = [];
    try {
      const res = window.App?.apiJSON ? await window.App.apiJSON('/v1/chat/sessions') : null;
      if (res && res.success) {
        sessions = res.data?.sessions || [];
      } else {
        /* 回退旧端点 */
        const resp = await window.App?.api?.('/v1/sessions');
        if (resp && resp.ok) {
          const data = await resp.json();
          sessions = Array.isArray(data) ? data : (data.sessions || []);
        }
      }
    } catch (e) {
      console.error('[briefing] Failed to load:', e);
    }

    /* 时间戳兼容: 秒(后端) vs 毫秒(前端默认) */
    const toMs = (ts) => (ts ? (ts < 1e12 ? ts * 1000 : ts) : 0);

    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const todaySessions = sessions.filter(s => {
      const d = new Date(toMs(s.updated_at || s.created_at));
      return !isNaN(d) && d >= today;
    });

    const totalMessages = sessions.reduce((sum, s) => sum + (s.messages || s.message_count || 0), 0);
    const totalTokens = sessions.reduce((sum, s) => sum + (s.token_count || s.tokens || 0), 0);

    const elCount = $('#bfSessionCount');
    const elMsg = $('#bfMessageCount');
    const elTokens = $('#bfTokenUsage');
    const elRecent = $('#bfRecentList');

    if (elCount) elCount.textContent = todaySessions.length;
    if (elMsg) elMsg.textContent = totalMessages;
    if (elTokens) elTokens.textContent = totalTokens > 1000 ? (totalTokens / 1000).toFixed(1) + 'k' : totalTokens;

    if (elRecent) {
      elRecent.innerHTML = '';
      const recent = sessions.slice(0, 10);
      if (recent.length === 0) {
        elRecent.innerHTML = '<div style="padding:16px;text-align:center;color:var(--muted);">暂无会话记录</div>';
        return;
      }
      recent.forEach(s => {
        const item = document.createElement('div');
        item.className = 'session-item';
        item.style.cssText = 'margin-bottom:8px;';
        item.innerHTML = `
          <div class="s-title">${escapeHtml(s.title || '新会话')}</div>
          <div class="s-meta">
            <span>${formatTime(s.updated_at || s.created_at)}</span>
            <span>${s.messages || s.message_count || 0} 条消息</span>
          </div>
        `;
        elRecent.appendChild(item);
      });
    }
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function formatTime(ts) {
    const ms = ts ? (ts < 1e12 ? ts * 1000 : ts) : 0;
    if (!ms) return '';
    const d = new Date(ms);
    const now = Date.now();
    const diff = now - ms;
    if (diff < 60000) return '刚刚';
    if (diff < 3600000) return Math.floor(diff / 60000) + ' 分钟前';
    if (diff < 86400000) return Math.floor(diff / 3600000) + ' 小时前';
    return d.toLocaleDateString('zh-CN');
  }

  // Auto-refresh when briefing tab is shown
  const observer = new MutationObserver(() => {
    const tab = document.querySelector('#tabBriefing');
    if (tab && tab.classList.contains('active')) {
      loadBriefing();
    }
  });

  document.addEventListener('DOMContentLoaded', () => {
    const tab = document.querySelector('#tabBriefing');
    if (tab) observer.observe(tab, { attributes: true, attributeFilter: ['class'] });

    const refreshBtn = $('#bfRefreshBtn');
    if (refreshBtn) refreshBtn.addEventListener('click', loadBriefing);
  });

  window.App = window.App || {};
  window.App.Briefing = { load: loadBriefing };
})();
