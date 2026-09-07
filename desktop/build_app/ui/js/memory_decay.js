/* 记忆衰退可视化面板 */
App.MemoryDecay = {};

App.MemoryDecay.load = async function() {
  const el = document.getElementById('decayList');
  if (!el) return;
  try {
    const res = await App.api('/v1/memory');
    if (!res.ok) { el.innerHTML = '<div class="session-empty">加载失败</div>'; return; }
    const d = await res.json();
    const mems = d.memories || [];
    el.innerHTML = '';
    if (!mems.length) {
      el.innerHTML = '<div class="session-empty">暂无记忆</div>';
      return;
    }
    const now = Date.now() / 1000;
    // 计算每条记忆的生存分
    const TTL_HOURS = 720;
    const scored = mems.map(m => {
      const ageHours = (now - (m.created_at || now)) / 3600;
      const score = Math.pow(0.95, ageHours / TTL_HOURS) + Math.min(0.2, Math.log1p(m.hit_count || 0) * 0.05);
      return { ...m, score: Math.max(0, Math.min(1, score)), ageHours };
    });
    scored.sort((a, b) => a.score - b.score);
    const criticial = scored.filter(m => m.score < 0.3);
    const warning = scored.filter(m => m.score >= 0.3 && m.score < 0.6);
    const healthy = scored.filter(m => m.score >= 0.6);

    if (criticial.length) {
      el.appendChild(App.MemoryDecay.buildSection('即将过期', criticial.slice(0, 5), 'expired'));
    }
    if (warning.length) {
      el.appendChild(App.MemoryDecay.buildSection('需注意', warning.slice(0, 5), 'warning'));
    }
    if (healthy.length) {
      el.appendChild(App.MemoryDecay.buildSection('健康', healthy.slice(0, 5), 'healthy'));
    }
  } catch (_) {
    el.innerHTML = '<div class="session-empty">加载失败</div>';
  }
};

App.MemoryDecay.buildSection = function(title, items, cls) {
  const section = document.createElement('div');
  section.className = `decay-section ${cls}`;
  const h = document.createElement('div');
  h.className = 'decay-section-title';
  h.textContent = `${title} (${items.length})`;
  section.appendChild(h);
  for (const m of items) {
    const item = document.createElement('div');
    item.className = `decay-item decay-${cls}`;
    const bar = document.createElement('div');
    bar.className = 'decay-bar';
    bar.style.width = Math.round(m.score * 100) + '%';
    const txt = document.createElement('div');
    txt.className = 'decay-text';
    txt.textContent = (m.content || '').slice(0, 60) + ((m.content || '').length > 60 ? '…' : '');
    item.appendChild(bar);
    item.appendChild(txt);
    section.appendChild(item);
  }
  return section;
};

App.MemoryDecay.runCleanup = async function(dryRun) {
  try {
    const res = await App.api('/v1/memory/decay/cleanup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: !!dryRun }),
    });
    if (res.ok) {
      const d = await res.json();
      App.toast(dryRun ? `清理预览: ${d.evicted} 条将被清除` : `已清理 ${d.evicted} 条记忆`, 'ok');
      App.MemoryDecay.load();
    } else {
      App.toast('清理失败', 'err');
    }
  } catch (e) {
    App.toast(`清理失败: ${e.message}`, 'err');
  }
};
