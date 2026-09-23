/* 语音朗读 —— 从 common.js 的 eval 兼容层拆出为独立模块 */
/* 依赖: App.api / App.appendMessage */
(function () {
  window.App = window.App || {};

  let _audioEl = null;
  let _speaking = false;
  let _done = null; // 当前播放的清理回调,手动停止时复用

  App.isSpeaking = function () {
    return _speaking;
  };

  App.stopSpeak = function stopSpeak() {
    if (_audioEl) {
      const el = _audioEl;
      // 先复位模块态,再彻底停住音频。_done 内部会 resolve 挂起的 Promise(还原按钮)
      _audioEl = null;
      _speaking = false;
      try {
        el.pause();
        el.removeAttribute('src');   // 清源,防残留播放
        el.load();
      } catch (_) {}
      try { if (_done) _done(); } catch (_) {}
    }
    _speaking = false;
  };

  App.speak = async function speak(text) {
    App.stopSpeak();
    const clean = String(text)
      .replace(/[^a-zA-Z0-9一-龥\s]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 2000);
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
        _speaking = true;
        let settled = false;
        let pendingResolve = null;
        const done = () => {
          if (settled) return;
          settled = true;
          URL.revokeObjectURL(url);
          _audioEl = null;
          _speaking = false;
          _done = null;
          if (pendingResolve) { pendingResolve(); pendingResolve = null; }
        };
        _done = done;
        _audioEl.play()
          .then(() => {
            // 真正开始播,按钮的还原时机交给 audio 事件
            _audioEl.addEventListener('pause', done);
            _audioEl.addEventListener('ended', done);
            _audioEl.addEventListener('error', done);
          })
          .catch(() => { done(); });
        // 页面卸载时释放 URL
        window.addEventListener('beforeunload', done, { once: true });
        // 让调用方在"播完 / 手动停止 / 出错"时收到 resolve,再还原按钮
        return new Promise((resolve) => {
          pendingResolve = resolve;
          // 兜底:极端情况(事件未触发)最多等 30 分钟
          setTimeout(() => { if (!settled) done(); else resolve(); }, 30 * 60 * 1000);
        });
      } catch (e) {
        lastErr = e.message;
        await new Promise(r => setTimeout(r, 500));
      }
    }
    App.appendMessage('朗读失败: ' + lastErr, 'error');
  };
})();
