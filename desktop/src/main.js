const { app, BrowserWindow, ipcMain, dialog, Tray, Menu, globalShortcut, nativeImage, desktopCapturer, screen } = require('electron');
const { spawn, execFileSync } = require('child_process');
const path = require('path');
const fs = require('fs');

// 稳定性:禁用 GPU 硬件加速/代理检测并关闭沙箱,规避 VM/RDP/杀软环境下
// Chromium network service / renderer 进程反复崩溃导致 fetch 失效与窗口卡死
app.disableHardwareAcceleration();
app.commandLine.appendSwitch('disable-gpu');
app.commandLine.appendSwitch('disable-gpu-compositing');
app.commandLine.appendSwitch('no-proxy-server');
app.commandLine.appendSwitch('no-sandbox');
app.commandLine.appendSwitch('remote-debugging-port', '9222');

const BACKEND_PORT = 8517;
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`;

let mainWindow = null;
let backendProcess = null;
let backendApiKey = ''; // 后端 AGENT_API_KEY(.env 中配置了鉴权时,渲染进程请求需携带)
let tray = null;
let isQuitting = false;
let backendRestartCount = 0; // 连续崩溃自动重启次数
let backendRestartTimer = null;
let backendUpTimer = null;
let backendGaveUp = false; // 已放弃自动重启,需手动重启

// 全局异常兜底:防止主进程因未捕获异常直接退出
process.on('uncaughtException', (err) => {
  console.error('[main] uncaughtException:', err);
});
process.on('unhandledRejection', (reason) => {
  console.error('[main] unhandledRejection:', reason);
});

// 程序化生成托盘图标(16x16 紫色圆角方块,中心白点),避免依赖外部 .ico 资源
function createTrayIcon() {
  const size = 16;
  const buf = Buffer.alloc(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const i = (y * size + x) * 4;
      const inCircle = (x - 5.5) * (x - 5.5) + (y - 5.5) * (y - 5.5) <= 2.5 * 2.5;
      const inSquare = x >= 2 && x <= 13 && y >= 2 && y <= 13;
      const inInner = x >= 4 && x <= 11 && y >= 4 && y <= 11;
      if (inInner) {
        buf[i] = 255; buf[i + 1] = 255; buf[i + 2] = 255; buf[i + 3] = 255; // 中心白点
      } else if (inSquare) {
        buf[i] = 109; buf[i + 1] = 93; buf[i + 2] = 252; buf[i + 3] = 255; // 紫色方块
      } else {
        buf[i + 3] = 0; // 透明
      }
      if (inCircle && !inInner) {
        buf[i] = 109; buf[i + 1] = 93; buf[i + 2] = 252; buf[i + 3] = 255;
      }
    }
  }
  const img = nativeImage.createFromBuffer(buf, { width: size, height: size });
  return img.resize({ width: 16, height: 16 });
}

// 从后端 .env 读取 AGENT_API_KEY,注入渲染进程的请求头(后端未配置则留空 = 无鉴权)
function readBackendApiKey() {
  // 开发模式:从项目根 .env 读取
  const projectRoot = path.join(__dirname, '..', '..');
  const devEnv = path.join(projectRoot, '.env');
  const candidates = [devEnv];
  // 生产模式:从 exe 同目录 .env 读取
  const exe = getBackendExecutable(true);
  if (exe) candidates.push(path.join(path.dirname(exe), '.env'));
  for (const envFile of candidates) {
    try {
      const txt = fs.readFileSync(envFile, 'utf-8');
      const line = txt.split(/\r?\n/).find((l) => /^\s*AGENT_API_KEY\s*=/.test(l));
      if (line) {
        return line.replace(/^\s*AGENT_API_KEY\s*=\s*/, '').replace(/^["']|["']$/g, '').trim();
      }
    } catch (_) { /* .env 不存在时无鉴权 */ }
  }
  return '';
}

function getBackendExecutable(silent = false) {
  // 同时兼容 onedir(dist/backend/agent-backend/)与 onefile(单 exe)两种布局
  const candidates = [];
  if (process.resourcesPath) {
    candidates.push(path.join(process.resourcesPath, 'backend', 'agent-backend', 'agent-backend.exe'));
    candidates.push(path.join(process.resourcesPath, 'backend', 'agent-backend.exe'));
  }
  candidates.push(path.join(__dirname, '..', 'backend', 'dist', 'agent-backend', 'agent-backend.exe'));
  candidates.push(path.join(__dirname, '..', 'backend', 'dist', 'agent-backend.exe'));
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  if (silent) return null;
  // 未找到:记录全部候选路径,便于定位是哪份副本缺后端
  const list = candidates.map((c) => `  - ${c}`).join('\n');
  console.error(`未找到后端 agent-backend.exe,请先打包 Python 后端。已检查:\n${list}`);
  try {
    dialog.showErrorBox('未找到后端程序', `以下路径均不存在 agent-backend.exe:\n${list}\n\n请确认启动的是完整发行目录(win-unpacked / AgentDesktop-v*),或运行 build_app.ps1 重新打包。`);
  } catch (_) { /* app.ready 之前无 dialog */ }
  return null;
}

async function checkHealthFast() {
  try {
    const res = await fetch(`${BACKEND_URL}/health`, { signal: AbortSignal.timeout(1500) });
    return res.ok;
  } catch (_) {
    return false;
  }
}

// 端口上有服务,且是带 version 标识的新版(无 version = 旧版残留,不可复用)
async function isCompatibleBackend() {
  try {
    const res = await fetch(`${BACKEND_URL}/health`, { signal: AbortSignal.timeout(1500) });
    if (!res.ok) return false;
    const data = await res.json();
    return Boolean(data && data.version);
  } catch (_) {
    return false;
  }
}

// 清掉所有残留的 agent-backend.exe(上次未退干净/其他副本遗留的旧版进程)
function killStaleBackends() {
  try {
    execFileSync('taskkill', ['/IM', 'agent-backend.exe', '/F'], {
      windowsHide: true,
      stdio: 'ignore',
    });
    console.log('[backend] 已清理残留的 agent-backend.exe');
  } catch (_) { /* 没有残留进程 */ }
}

// 找到当前正监听指定 TCP 端口的进程 PID(跨平台用 netstat -ano 解析)
function getPidListeningOnPort(port) {
  try {
    const out = execFileSync('netstat', ['-ano', '-p', 'tcp'], {
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    }).toString();
    for (const line of out.split(/\r?\n/)) {
      const m = line.trim().match(/^TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)/i);
      if (m && parseInt(m[1], 10) === port) {
        return parseInt(m[2], 10);
      }
    }
  } catch (_) { /* netstat 不可用 */ }
  return null;
}

// 强杀占用指定端口的进程(含子进程树),无论它是 agent-backend.exe 还是
// 其它进程(如开发期残留的 python.exe 源码后端)。仅按端口释放,不动其它端口。
// 返回是否真的杀掉了某个进程。
async function freeBackendPort(port) {
  const pid = getPidListeningOnPort(port);
  if (!pid) return false;
  try {
    execFileSync('taskkill', ['/pid', String(pid), '/F', '/T'], {
      windowsHide: true,
      stdio: 'ignore',
    });
    console.log(`[backend] 已释放端口 ${port} 占用进程 pid=${pid}`);
    return true;
  } catch (_) {
    return false;
  }
}

// 等待端口释放(进程被杀后 TIME_WAIT/句柄释放需要片刻)
async function waitForPortFree(timeoutMs = 8000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (!(await checkHealthFast())) return true;
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

function startBackend() {
  // 优先使用项目 .venv 的 Python 后端（开发阶段），避免 PyInstaller 打包问题
  // 注意：打包后 __dirname 指向 asar 包内路径，需要使用 app.getPath('exe') 获取可执行文件路径
  const { app } = require('electron');
  let projectRoot;
  if (app.isPackaged) {
    // 打包模式：从可执行文件所在目录向上查找项目根目录
    projectRoot = path.join(path.dirname(app.getPath('exe')), '..');
  } else {
    // 开发模式：从源码目录向上查找
    projectRoot = path.join(__dirname, '..', '..');
  }
  const pythonExe = path.join(projectRoot, '.venv', 'Scripts', 'python.exe');
  const mainPy = path.join(projectRoot, 'app', 'main.py');

  if (fs.existsSync(pythonExe) && fs.existsSync(mainPy)) {
    // 开发模式:使用项目 .venv 的 Python 源码后端
    const args = ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(BACKEND_PORT)];
    console.log('[backend] 使用 Python 源码后端:', pythonExe);
    backendProcess = spawn(pythonExe, args, {
      cwd: projectRoot,
      windowsHide: false,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } else {
    // 生产模式:使用预编译 exe
    const exe = getBackendExecutable();
    if (!exe) {
      console.error('未找到后端程序 (Python 或 exe)，请先打包 Python 后端');
      return false;
    }
    console.log('[backend] 使用预编译后端:', exe);
    const args = ['--port', String(BACKEND_PORT), '--host', '127.0.0.1'];
    backendProcess = spawn(exe, args, {
      cwd: path.dirname(exe),
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  }

  backendProcess.stdout.on('data', (d) => console.log('[backend]', d.toString().trim()));
  backendProcess.stderr.on('data', (d) => console.error('[backend-err]', d.toString().trim()));
  backendProcess.on('exit', (code) => {
    backendProcess = null;
    console.log('[backend] exited', code);
    notifyRenderer('backend-status', { running: false, restarts: backendRestartCount });
    // 正常退出(托盘退出流程)不再拉起;异常退出则自动重启,连续多次失败后放弃
    if (isQuitting || backendGaveUp) return;
    if (backendRestartCount >= 5) {
      console.error('[backend] 连续重启失败,停止自动拉起');
      notifyRenderer('backend-status', { running: false, restarts: backendRestartCount, gaveUp: true });
      backendGaveUp = true;
      return;
    }
    clearTimeout(backendRestartTimer);
    // 退避: 每次重启等待时间递增 (800ms, 1600ms, 3200ms, ...)
    const backoff = Math.min(800 * Math.pow(2, backendRestartCount), 10000);
    backendRestartTimer = setTimeout(async () => {
      console.log(`[backend] 检测到异常退出,自动重启中... (第${backendRestartCount + 1}次, 退避${backoff}ms)`);
      const started = startBackend();
      if (!started) {
        notifyRenderer('backend-status', { running: false, restarts: backendRestartCount, gaveUp: true });
        backendGaveUp = true;
        return;
      }
      backendRestartCount++;
      const ok = await waitForBackend(25000);
      if (ok) {
        backendRestartCount = 0; // 重启成功后重置计数
        backendGaveUp = false;
        armUptimeReset();
        notifyRenderer('backend-status', { running: true, restarts: backendRestartCount, restarted: true });
      } else {
        console.error(`[backend] 重启后无法连接,等待${backoff}ms后重试`);
        // 不重置计数,也不放弃,继续尝试(最多5次)
        notifyRenderer('backend-status', { running: false, restarts: backendRestartCount, gaveUp: false });
        // 安排下一次重试(使用退避)
        const nextBackoff = Math.min(800 * Math.pow(2, backendRestartCount), 10000);
        backendRestartTimer = setTimeout(async () => {
          if (isQuitting || backendGaveUp) return;
          console.log(`[backend] 重试重启... (第${backendRestartCount + 1}次, 退避${nextBackoff}ms)`);
          const started = startBackend();
          if (!started) {
            backendGaveUp = true;
            notifyRenderer('backend-status', { running: false, restarts: backendRestartCount, gaveUp: true });
            return;
          }
          const ok = await waitForBackend(25000);
          if (ok) {
            backendRestartCount = 0;
            backendGaveUp = false;
            armUptimeReset();
            notifyRenderer('backend-status', { running: true, restarts: backendRestartCount, restarted: true });
          } else {
            backendRestartCount++;
            if (backendRestartCount >= 5) {
              backendGaveUp = true;
              notifyRenderer('backend-status', { running: false, restarts: backendRestartCount, gaveUp: true });
            }
          }
        }, nextBackoff);
      }
    }, backoff);
  });
  return true;
}

// 进程存活超过 60s 视为稳定,重置连续重启计数
function armUptimeReset() {
  clearTimeout(backendUpTimer);
  backendUpTimer = setTimeout(() => {
    backendRestartCount = 0;
  }, 60000);
}

// 向渲染进程推送后端状态(用于自动重启提示)
function notifyRenderer(event, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    try { mainWindow.webContents.send(event, payload); } catch (_) { /* 窗口未就绪 */ }
  }
}

function stopBackend() {
  if (backendProcess) {
    try {
      // PyInstaller onefile 会派生子进程,kill() 只杀启动器;taskkill /T 杀掉整棵进程树
      const { execFileSync } = require('child_process');
      execFileSync('taskkill', ['/pid', String(backendProcess.pid), '/T', '/F'], {
        windowsHide: true,
        stdio: 'ignore',
      });
    } catch (_) {
      /* 进程可能已退出 */
    }
    backendProcess = null;
  }
}

async function waitForBackend(timeoutMs = 20000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(`${BACKEND_URL}/health`);
      if (res.ok) return true;
    } catch (_) {
      // 未就绪,继续等待
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 750,
    minWidth: 700,
    minHeight: 500,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
    autoHideMenuBar: true,
    title: '智能体助手',
  });

  // 聊天 UI 优先;UI 不存在时退回加载后端页面
  const uiPath = path.join(__dirname, 'ui', 'index.html');
  if (fs.existsSync(uiPath)) {
    mainWindow.loadFile(uiPath);
  } else {
    mainWindow.loadURL(`${BACKEND_URL}/`);
  }

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // // 自动打开 DevTools
  mainWindow.webContents.on('did-finish-load', () => { mainWindow.webContents.openDevTools({ mode: 'detach' }); });
  // F12 打开 DevTools 便于调试
  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.key === 'F12') {
      event.preventDefault();
      mainWindow.webContents.openDevTools({ mode: 'detach' });
    }
  });

  // 点关闭按钮 → 隐藏到托盘(后台常驻),真正退出走托盘菜单/全局退出
  mainWindow.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });

  // 渲染进程崩溃恢复:OOM/JS 异常导致 renderer 挂掉时自动重建窗口
  mainWindow.webContents.on('render-process-gone', (event, details) => {
    console.error('[renderer] 进程崩溃, reason:', details.reason, 'exitCode:', details.exitCode);
    if (isQuitting) return;
    // 2 秒后自动重建窗口,避免用户看到空白
    setTimeout(() => {
      if (isQuitting) return;
      if (!mainWindow || mainWindow.isDestroyed()) {
        createWindow();
        showWindow();
      }
    }, 2000);
  });
}

function toggleWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createWindow();
    return;
  }
  if (mainWindow.isVisible() && !mainWindow.isMinimized()) {
    mainWindow.hide();
  } else {
    showWindow();
  }
}

function showWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createWindow();
    return;
  }
  mainWindow.show();
  mainWindow.focus();
  if (mainWindow.isMinimized()) mainWindow.restore();
}

function createTray() {
  tray = new Tray(createTrayIcon());
  tray.setToolTip('智能体助手');
  const menu = Menu.buildFromTemplate([
    { label: '打开智能体助手', click: () => showWindow() },
    { type: 'separator' },
    { label: '退出', click: () => { isQuitting = true; app.quit(); } },
  ]);
  tray.setContextMenu(menu);
  tray.on('click', () => toggleWindow());
}

// 单实例锁:二次启动时唤起已运行实例而不是再起一份(主流桌面应用行为)
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    showWindow();
  });
}

app.whenReady().then(async () => {
  backendApiKey = readBackendApiKey();

  // 系统托盘(后台常驻入口)
  createTray();

  // 全局快捷键:Alt+Shift+Space 唤出/隐藏(占位图标足够,不依赖焦点)
  const GLOBAL_HOTKEY = 'Alt+Shift+Space';
  const registered = globalShortcut.register(GLOBAL_HOTKEY, () => toggleWindow());
  if (registered) {
    console.log(`[hotkey] 已注册全局快捷键 ${GLOBAL_HOTKEY}`);
  } else {
    console.warn(`[hotkey] 全局快捷键 ${GLOBAL_HOTKEY} 注册失败(可能被其他程序占用)`);
  }

  // 窗口先行加载 UI, 后端并行启动
  createWindow();

  // 端口已有可用后端时:新版(带 version)直接复用;旧版残留则清掉换成当前资源目录里的版本
  const alreadyUp = await checkHealthFast();
  if (alreadyUp && (await isCompatibleBackend())) {
    console.log('[backend] 检测到兼容后端已在运行,直接复用');
  } else {
    if (alreadyUp) {
      // 端口被占用但不是兼容的新版(可能是旧版残留或开发期 python.exe 源码后端):
      // 先按端口强杀占用进程(进程名未必是 agent-backend.exe,故不能只靠 killStaleBackends),
      // 再兜底清理同名旧 exe,最后等待端口释放。
      await freeBackendPort(BACKEND_PORT);
      killStaleBackends();
      await waitForPortFree();
    }
    const started = startBackend();
    if (!started) {
      dialog.showErrorBox('启动失败', '未找到后端程序,请重新安装或运行 build.bat 构建');
      app.quit();
      return;
    }
  }

  const ok = await waitForBackend(25000);
  if (!ok) {
    dialog.showErrorBox('启动失败', '后端服务启动超时,请查看日志');
    app.quit();
    return;
  }

  // 兜底看护:复用外部后端(backendProcess 为 null)异常死亡时,尝试自行拉起
  setInterval(async () => {
    if (isQuitting || backendGaveUp) return;
    const up = await checkHealthFast();
    if (up) {
      if (backendRestartCount > 0) {
        backendRestartCount = 0;
        backendGaveUp = false;
        armUptimeReset();
      }
      return;
    }
    if (backendProcess) return; // 自有进程的异常退出由 exit 处理器负责拉起
    if (backendRestartCount >= 5) return;
    backendRestartCount++;
    const started = startBackend();
    if (started) {
      const alive = await waitForBackend(25000);
      if (alive) {
        backendRestartCount = 0;
        backendGaveUp = false;
        armUptimeReset();
        notifyRenderer('backend-status', { running: true, restarts: backendRestartCount, restarted: true });
      } else {
        notifyRenderer('backend-status', { running: false, restarts: backendRestartCount, gaveUp: false });
      }
    }
  }, 15000);

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  // 关闭窗口仅隐藏到托盘,不退出;真正退出走托盘菜单或 isQuitting 标记
  if (isQuitting) {
    stopBackend();
    app.quit();
  }
});

app.on('before-quit', () => {
  isQuitting = true;
  globalShortcut.unregisterAll();
});

// IPC: 提供后端地址给渲染进程
ipcMain.handle('get-backend-url', () => BACKEND_URL);

// IPC: 手动重启后端(设置页入口)
ipcMain.handle('restart-backend', async () => {
  // 清除之前的重启计时器
  clearTimeout(backendRestartTimer);
  // 重置状态
  backendRestartCount = 0;
  backendGaveUp = false;
  if (!backendProcess) {
    return { ok: false, reason: '后端由其他实例托管,请先退出后在重启' };
  }
  stopBackend();
  backendRestartCount = 0;
  const started = startBackend();
  if (!started) return { ok: false, reason: '未找到后端程序' };
  notifyRenderer('backend-status', { running: false, restarts: 0 });
  const ok = await waitForBackend(25000);
  if (ok) {
    armUptimeReset();
    notifyRenderer('backend-status', { running: true, restarts: 0 });
    return { ok: true };
  }
  return { ok: false, reason: '后端启动超时,请查看日志' };
});
ipcMain.handle('get-backend-status', () => (backendProcess ? 'running' : 'stopped'));
ipcMain.handle('get-backend-api-key', () => backendApiKey);

// IPC: 打开文件夹选择对话框
ipcMain.handle('open-folder-dialog', async () => {
  try {
    const result = await dialog.showOpenDialog({
      properties: ['openDirectory'],
      title: '选择项目文件夹',
    });
    if (result.canceled || !result.filePaths.length) {
      return { canceled: true };
    }
    return { path: result.filePaths[0] };
  } catch (e) {
    console.error('[open-folder-dialog]', e);
    return { error: e.message };
  }
});

// IPC: 截屏整块屏幕(主显示),返回 PNG dataURL 供渲染进程直接做图片识别
ipcMain.handle('capture-screen', async () => {
  try {
    const disp = screen.getPrimaryDisplay();
    const scale = disp.scaleFactor || 1;
    const sources = await desktopCapturer.getSources({
      types: ['screen'],
      thumbnailSize: {
        width: Math.round(disp.size.width * scale),
        height: Math.round(disp.size.height * scale),
      },
    });
    const src = sources.find((s) => s.display_id === disp.id.toString()) || sources[0];
    if (!src) return null;
    return src.thumbnail.toDataURL();
  } catch (e) {
    console.error('[capture-screen]', e);
    return null;
  }
});

