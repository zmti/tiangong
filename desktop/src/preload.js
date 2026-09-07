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
});
