const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('agentApp', {
  getBackendUrl: () => ipcRenderer.invoke('get-backend-url'),
  getBackendStatus: () => ipcRenderer.invoke('get-backend-status'),
  getBackendApiKey: () => ipcRenderer.invoke('get-backend-api-key'),
  captureScreen: () => ipcRenderer.invoke('capture-screen'),
  onBackendStatus: (cb) => {
    const listener = (_e, data) => cb(data);
    ipcRenderer.on('backend-status', listener);
    return () => ipcRenderer.removeListener('backend-status', listener);
  },
  restartBackend: () => ipcRenderer.invoke('restart-backend'),
  openFolderDialog: () => ipcRenderer.invoke('open-folder-dialog'),
  // 用系统程序打开本地文件(会话附件"打开"按钮):主进程白名单校验 + shell.openPath
  openLocalFile: (filePath) => ipcRenderer.invoke('shell-open-file', filePath),
});
