/* briefing.js — Daily session summary dashboard */
(function () {
  'use strict';

  const $ = (sel, ctx) => (ctx || document).querySelector(sel);
  const api = () => window.App?.api;

  async function loadBriefing() {
    try {
      const resp = await api()('/v1/sessions');
      if (!resp || !resp.ok) return;
      const sessions = await resp.json();

      const today = new Date();
      today.setHours(0, 0, 0, 0);
      const todaySessions = sessions.filter(s => {
        const d = new Date(s.updated_at || s.created_at);
        return d >= today;
      });

      const totalMessages = sessions.reduce((sum, s) => sum + (s.message_count || 0), 0);
      const totalTokens = sessions.reduce((sum, s) => sum + (s.token_count || 0), 0);

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
              <span>${s.message_count || 0} 条消息</span>
            </div>
          `;
          elRecent.appendChild(item);
        });
      }
    } catch (e) {
      console.error('[briefing] Failed to load:', e);
    }
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function formatTime(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    const now = new Date();
    const diff = now - d;
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
