/* 语音朗读 —— 从 common.js 的 eval 兼容层拆出为独立模块 */
/* 依赖: App.api / App.appendMessage */
(function () {
  window.App = window.App || {};

  let _audioEl = null;

  App.stopSpeak = function stopSpeak() {
    if (_audioEl) { try { _audioEl.pause(); } catch (_) {} _audioEl = null; }
  };

  App.speak = async function speak(text) {
    App.stopSpeak();
    const clean = String(text).replace(/[^a-zA-Z0-9\u4e00-\u9fa5\s]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 2000);
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
          try { const d = await res.json(); lastErr = d.detail || 'HTTP ' + res.status; } catch (_) { lastErr = 'HTTP ' + res.status; }
          await new Promise(r => setTimeout(r, 500));
          continue;
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        _audioEl = new Audio(url);
        _audioEl.play().catch(() => { });
        _audioEl.onended = () => { URL.revokeObjectURL(url); _audioEl = null; };
        _audioEl.onerror = () => { URL.revokeObjectURL(url); _audioEl = null; };
        // 页面卸载时释放 URL
        const cleanup = () => { if (_audioEl) { URL.revokeObjectURL(url); _audioEl = null; } };
        window.addEventListener('beforeunload', cleanup, { once: true });
        return;
      } catch (e) {
        lastErr = e.message;
        await new Promise(r => setTimeout(r, 500));
      }
    }
    App.appendMessage('朗读失败: ' + lastErr, 'error');
  };
})();
