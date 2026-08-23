/* 视频上传模块 */
App.Video = {};

App.Video.init = function () {
  const videoBtn = document.getElementById('videoBtn');
  if (!videoBtn) return;

  videoBtn.addEventListener('click', () => {
    const input = document.getElementById('videoInput');
    if (input) input.click();
  });

  const input = document.getElementById('videoInput');
  if (input) {
    input.addEventListener('change', async (e) => {
      if (e.target.files.length > 0) {
        await App.Video.upload(e.target.files[0]);
        e.target.value = '';
      }
    });
  }
};

App.Video.upload = async function (file) {
  if (!file.type.startsWith('video/')) {
    App.toast('请选择视频文件', 'err');
    return;
  }

  const fd = new FormData();
  fd.append('file', file);
  
  try {
    App.toast('正在分析视频...', 'info');
    const res = await App.api('/v1/multimodal/video/summarize', {
      method: 'POST',
      body: fd,
    });
    const d = await res.json();
    if (res.ok) {
      App.toast(`视频分析完成: ${d.frames} 帧`, 'ok');
      // 可以在这里显示视频摘要
      if (d.summary) {
        const input = document.getElementById('input');
        if (input) {
          input.value = `根据视频内容: ${d.summary}\n\n请分析这个视频:`;
          App.Chat.autoResize();
        }
      }
    } else {
      App.toast('视频分析失败: ' + (d.detail || '未知错误'), 'err');
    }
  } catch (e) {
    App.toast('视频分析失败: ' + e.message, 'err');
  }
};
