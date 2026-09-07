/**
 * 现代化聊天界面增强
 * 功能: 消息渲染优化、流式状态、工具调用展示
 */

App.ChatUI = App.ChatUI || {};

// ========== 工具方法 ==========
App.ChatUI.escapeHtml = function(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
};

// ========== 消息渲染优化 ==========
App.ChatUI.renderMessage = function(role, content, options = {}) {
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  
  const avatar = role === 'user' ? '👤' : '🤖';
  const time = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  
  wrap.innerHTML = `
    <div class="avatar">${avatar}</div>
    <div class="msg-body">
      <div class="msg-time">${time}</div>
      <div class="answer-doc">${content}</div>
      <div class="msg-actions">
        <button class="msg-action" data-action="copy" title="复制">📋 复制</button>
        <button class="msg-action" data-action="reply" title="回复">💬 回复</button>
        ${role === 'bot' ? '<button class="msg-action" data-action="regenerate" title="重新生成">🔄 重新生成</button>' : ''}
      </div>
    </div>
  `;
  
  return wrap;
};

// ========== 工具调用展示 ==========
App.ChatUI.renderToolCall = function(toolName, input, output) {
  const container = document.createElement('div');
  container.className = 'tr-card';
  container.innerHTML = `
    <div class="tr-card-head">
      <span>🔧 ${App.ChatUI.escapeHtml(toolName)}</span>
      <span style="font-size:11px;color:var(--text-muted)">执行中...</span>
    </div>
    <div class="tr-card-body">
      <div style="font-family:var(--mono);font-size:11px;color:var(--text-muted);margin-bottom:6px">输入参数:</div>
      <pre style="margin:0;font-size:11px">${App.ChatUI.escapeHtml(JSON.stringify(input, null, 2))}</pre>
    </div>
  `;
  
  return container;
};

App.ChatUI.updateToolCall = function(container, success, output) {
  const header = container.querySelector('.tr-card-head span:last-child');
  if (header) {
    header.textContent = success ? '✓ 完成' : '✗ 失败';
    header.style.color = success ? 'var(--ok)' : 'var(--err)';
  }
  
  if (output) {
    const body = container.querySelector('.tr-card-body');
    if (body) {
      body.innerHTML += `
        <div style="font-family:var(--mono);font-size:11px;color:var(--text-muted);margin:6px 0 4px">输出结果:</div>
        <pre style="margin:0;font-size:11px;max-height:200px;overflow:auto">${App.ChatUI.escapeHtml(typeof output === 'string' ? output : JSON.stringify(output, null, 2))}</pre>
      `;
    }
  }
};

// ========== 流式状态指示器 ==========
App.ChatUI.showStreaming = function() {
  const wrap = document.createElement('div');
  wrap.className = 'msg bot streaming';
  wrap.id = 'streamingMessage';
  
  wrap.innerHTML = `
    <div class="avatar">🤖</div>
    <div class="msg-body">
      <div class="msg-time">思考中...</div>
      <div class="answer-doc">
        <div class="typing-indicator">
          <span></span>
          <span></span>
          <span></span>
        </div>
      </div>
    </div>
  `;
  
  document.getElementById('messages')?.appendChild(wrap);
  App.ChatUI.scrollToBottom();
};

App.ChatUI.hideStreaming = function() {
  const el = document.getElementById('streamingMessage');
  if (el) {
    el.remove();
  }
};

// ========== 滚动到底部 ==========
App.ChatUI.scrollToBottom = function() {
  const container = document.getElementById('messages');
  if (container) {
    container.scrollTop = container.scrollHeight;
  }
};

// ========== 进度条 ==========
App.ChatUI.showProgress = function(percent) {
  let bar = document.getElementById('streamProgress');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'streamProgress';
    bar.style.cssText = `
      position: fixed;
      top: 0;
      left: 0;
      height: 2px;
      background: linear-gradient(90deg, var(--primary), var(--accent));
      z-index: 1000;
      transition: width 0.3s ease;
    `;
    document.body.appendChild(bar);
  }
  
  bar.style.width = `${percent}%`;
};

App.ChatUI.hideProgress = function() {
  const bar = document.getElementById('streamProgress');
  if (bar) {
    bar.remove();
  }
};

// ========== 初始化 ==========
App.ChatUI.init = function() {
  // 事件委托处理消息操作
  document.addEventListener('click', (e) => {
    const msg = e.target.closest('.msg');
    if (!msg) return;
    
    // 复制按钮
    if (e.target.closest('[data-action="copy"]')) {
      const content = msg.querySelector('.answer-doc');
      if (content) {
        const text = content.textContent || '';
        navigator.clipboard.writeText(text).then(() => {
          App.UI?.Toast?.success('已复制到剪贴板');
        }).catch(() => {
          App.UI?.Toast?.error('复制失败');
        });
      }
    }
    
    // 回复按钮
    if (e.target.closest('[data-action="reply"]')) {
      const content = msg.querySelector('.answer-doc');
      if (content) {
        const text = content.textContent?.substring(0, 100) || '';
        App.Chat?.setReplyQuote(text + (content.textContent?.length > 100 ? '...' : ''));
        document.getElementById('input')?.focus();
        App.UI?.Toast?.info('已设置为回复引用');
      }
    }
    
    // 重新生成按钮
    if (e.target.closest('[data-action="regenerate"]')) {
      App.Chat?.regenerate(msg);
    }
  });
  
  console.log('[ChatUI] 聊天界面增强已加载');
};

// 页面加载完成后初始化
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => App.ChatUI.init());
} else {
  App.ChatUI.init();
}
