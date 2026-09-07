/* 语音输入模块:录音转文字 */
App.Voice = {
  mediaRecorder: null,
  audioChunks: [],
  isRecording: false,
  stream: null,
  waves: null,
};

App.Voice.init = function () {
  // 录音按钮通过附件菜单触发，无需独立按钮
  // 但在 Composer 上保留一个独立的录音按钮（如果 UI 有）
  const micBtn = document.getElementById('micBtn');
  if (micBtn) {
    micBtn.addEventListener('click', () => {
      if (App.Voice.isRecording) App.Voice.stopRecording();
      else App.Voice.startRecording();
    });
  }
};

App.Voice.startRecording = async function () {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    App.Voice.stream = stream;
    App.Voice.mediaRecorder = new MediaRecorder(stream);
    App.Voice.audioChunks = [];

    App.Voice.mediaRecorder.addEventListener('dataavailable', (event) => {
      App.Voice.audioChunks.push(event.data);
    });

    App.Voice.mediaRecorder.addEventListener('stop', async () => {
      await App.Voice.transcribe();
    });

    App.Voice.mediaRecorder.start();
    App.Voice.isRecording = true;
    
    const micBtn = document.getElementById('micBtn');
    if (micBtn) {
      micBtn.classList.add('recording');
      micBtn.title = '点击停止录音';
    }
    
    App.toast('正在录音...', 'info');
  } catch (e) {
    App.toast('无法访问麦克风: ' + e.message, 'err');
  }
};

App.Voice.stopRecording = function () {
  if (App.Voice.mediaRecorder && App.Voice.isRecording) {
    App.Voice.mediaRecorder.stop();
    if (App.Voice.stream) {
      App.Voice.stream.getTracks().forEach(track => track.stop());
    }
    App.Voice.isRecording = false;
    
    const micBtn = document.getElementById('micBtn');
    if (micBtn) {
      micBtn.classList.remove('recording');
      micBtn.title = '点击开始录音';
    }
  }
};

App.Voice.transcribe = async function () {
  const audioBlob = new Blob(App.Voice.audioChunks, { type: 'audio/webm' });
  const fd = new FormData();
  fd.append('file', audioBlob, 'recording.webm');
  
  try {
    const res = await App.api('/v1/multimodal/transcribe', {
      method: 'POST',
      body: fd,
    });
    const d = await res.json();
    if (res.ok && d.text) {
      // 将转录文字插入输入框
      const input = document.getElementById('input');
      if (input) {
        const val = input.value;
        input.value = val ? val + ' ' + d.text : d.text;
        App.Chat.autoResize();
      }
      App.toast('语音识别完成', 'ok');
    } else {
      App.toast('语音识别失败: ' + (d.detail || '未知错误'), 'err');
    }
  } catch (e) {
    App.toast('语音识别失败: ' + e.message, 'err');
  }
  App.Voice.audioChunks = [];
};
