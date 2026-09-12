/* 灯箱 —— 从 common.js 的 eval 兼容层拆出为独立模块 */
/* 依赖: App.els.lbImg / App.els.lightbox / App.toast */
(function () {
  window.App = window.App || {};

  App.openLb = function openLb(src, name) {
    App.els.lbImg.src = src;
    App.els.lbImg.alt = name || '图片';
    App.els.lightbox.classList.add('open');
  };

  App.closeLb = function closeLb() {
    App.els.lightbox.classList.remove('open');
    App.els.lbImg.src = '';
  };

  App.downloadLb = function downloadLb() {
    const src = App.els.lbImg.src;
    if (!src) return;
    const a = document.createElement('a');
    a.href = src;
    a.download = 'image.png';
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  App.copyImage = async function copyImage(src) {
    try {
      if (src.startsWith('data:')) {
        const resp = await fetch(src);
        const blob = await resp.blob();
        await navigator.clipboard.write([new ClipboardItem({ [blob.type || 'image/png']: blob })]);
      } else {
        await navigator.clipboard.writeText(src);
      }
      App.toast('📋 已复制图片', 'ok');
    } catch (e) {
      App.toast('复制失败: ' + e.message, 'err');
    }
  };

  App.copyLb = function copyLb() { return App.copyImage(App.els.lbImg.src); };
})();
