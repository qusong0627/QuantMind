import { app, BrowserWindow, shell, nativeTheme, Menu, MenuItemConstructorOptions, ipcMain, dialog, Notification } from 'electron';
import path from 'path';
import fs from 'fs';
import { spawnSync } from 'child_process';
import log from 'electron-log';
import { autoUpdater } from 'electron-updater';
import { registerAuthHandlers } from './ipc/handlers/auth.handler';
import { configService } from './services/config_service';

console.log('[main] main.ts loaded!');
console.log('[main] process.env.VITE_DEV:', process.env.VITE_DEV);

// 检测是否处于开发模式
const isDev = (process.env.VITE_DEV === '1');

console.log('[main] isDev:', isDev);

// 检测 Windows 版本
function getWindowsVersion(): { isWin11: boolean; buildNumber: number } {
  if (process.platform !== 'win32') {
    return { isWin11: false, buildNumber: 0 };
  }
  try {
    const result = spawnSync('cmd', ['/c', 'ver'], { encoding: 'utf8' });
    const output = result.stdout || '';
    const match = output.match(/(\d+)\.(\d+)\.(\d+)/);
    if (match) {
      const buildNumber = parseInt(match[3], 10);
      return { isWin11: buildNumber >= 22000, buildNumber };
    }
  } catch {
    // 忽略错误
  }
  return { isWin11: false, buildNumber: 0 };
}

const windowsVersion = getWindowsVersion();
console.log('[main] Windows version:', windowsVersion);

// 修复EPIPE错误：捕获未处理的异常和警告
process.on('uncaughtException', (error) => {
  console.error('[main] Uncaught Exception:', error);
});

process.on('unhandledRejection', (reason, promise) => {
  console.error('[main] Unhandled Rejection at:', promise, 'reason:', reason);
});

// 重写console方法来捕获EPIPE错误
const originalConsoleWarn = console.warn;
const originalConsoleError = console.error;

console.warn = (...args: any[]) => {
  try {
    originalConsoleWarn(...args);
  } catch (error) {
    // 忽略EPIPE错误，通常发生在进程关闭时
    if ((error as any).code !== 'EPIPE') {
      // 尝试使用其他方式记录错误
      try {
        originalConsoleError('[console.warn error]', error);
      } catch {
        // 完全静默，避免进程崩溃
      }
    }
  }
};

console.error = (...args: any[]) => {
  try {
    originalConsoleError(...args);
  } catch (error) {
    // 忽略EPIPE错误，通常发生在进程关闭时
    if ((error as any).code !== 'EPIPE') {
      // 尝试使用其他方式记录错误
      try {
        originalConsoleError('[console.error error]', error);
      } catch {
        // 完全静默，避免进程崩溃
      }
    }
  }
};

let mainWindow: BrowserWindow | null = null;


/**
 * 创建应用程序菜单
 */
function createMenu() {
  const template: MenuItemConstructorOptions[] = [
    {
      label: '文件',
      submenu: [
        { label: '撤销', accelerator: 'CmdOrCtrl+Z', role: 'undo' },
        { label: '重做', accelerator: 'CmdOrCtrl+Y', role: 'redo' },
        { type: 'separator' },
        { label: '剪切', accelerator: 'CmdOrCtrl+X', role: 'cut' },
        { label: '复制', accelerator: 'CmdOrCtrl+C', role: 'copy' },
        { label: '粘贴', accelerator: 'CmdOrCtrl+V', role: 'paste' },
        { label: '删除', role: 'delete' },
        { label: '全选', accelerator: 'CmdOrCtrl+A', role: 'selectAll' }
      ]
    },
    {
      label: '编辑',
      submenu: [
        { label: '撤销', accelerator: 'CmdOrCtrl+Z', role: 'undo' },
        { label: '重做', accelerator: 'Shift+CmdOrCtrl+Z', role: 'redo' },
        { type: 'separator' },
        { label: '剪切', accelerator: 'CmdOrCtrl+X', role: 'cut' },
        { label: '复制', accelerator: 'CmdOrCtrl+C', role: 'copy' },
        { label: '粘贴', accelerator: 'CmdOrCtrl+V', role: 'paste' }
      ]
    },
    {
      label: '视图',
      submenu: [
        { label: '重新加载', accelerator: 'CmdOrCtrl+R', click: () => mainWindow?.reload() },
        { label: '强制重新加载', accelerator: 'CmdOrCtrl+Shift+R', click: () => mainWindow?.webContents.reloadIgnoringCache() },
        { label: '开发者工具', accelerator: process.platform === 'darwin' ? 'Cmd+Option+I' : 'Ctrl+Shift+I', click: () => mainWindow?.webContents.toggleDevTools() },
        { type: 'separator' },
        { label: '实际大小', accelerator: 'CmdOrCtrl+0', role: 'resetZoom' },
        { label: '放大', accelerator: 'CmdOrCtrl+Plus', role: 'zoomIn' },
        { label: '缩小', accelerator: 'CmdOrCtrl+-', role: 'zoomOut' },
        { type: 'separator' },
        { label: '切换全屏', accelerator: 'F11', role: 'togglefullscreen' }
      ]
    },
    {
      label: '工具',
      submenu: [
        {
          label: '导出数据',
          accelerator: 'CmdOrCtrl+Shift+E',
          click: () => {
            if (mainWindow) {
              console.log('[main] menu: export-data clicked');
              mainWindow.webContents.send('menu-export-data');
            }
          }
        },
        { type: 'separator' }
      ]
    },
    {
      label: '窗口',
      submenu: [
        { label: '最小化', accelerator: 'CmdOrCtrl+M', role: 'minimize' },
        { label: '关闭', accelerator: 'CmdOrCtrl+W', role: 'close' }
      ]
    },
    {
      label: '帮助',
      submenu: [
        {
          label: '关于 QuantMind',
          click: () => shell.openExternal('https://github.com/your-repo/quantmind')
        }
      ]
    }
  ];

  const menu = Menu.buildFromTemplate(template);
  Menu.setApplicationMenu(menu);
}

/**
 * 尝试加载开发服务器URL，支持重试
 * @param win 浏览器窗口实例
 * @param url 要加载的URL
 * @param retries 重试次数
 * @param delay 重试间隔（毫秒）
 * @returns 是否加载成功
 */
async function tryLoadDevURL(win: BrowserWindow, url: string, retries = 20, delay = 500) {
  for (let i = 1; i <= retries; i++) {
    try {
      await win.loadURL(url);
      console.log(`[main] Dev server loaded after attempt ${i}`);
      return true;
    } catch (err) {
      console.warn(`[main] loadURL failed attempt ${i}/${retries}: ${(err as Error).message}`);
      await new Promise(r => setTimeout(r, delay));
    }
  }
  return false;
}

function resolveBundledIndexPath(): { foundPath: string | null; candidates: string[] } {
  const candidates = [
    // Electron 打包目录内常见路径
    path.join(app.getAppPath(), 'dist-react', 'index.html'),
    // 打包后常见路径：.../app.asar/dist-electron/electron -> ../../dist-react/index.html
    path.join(__dirname, '..', '..', 'dist-react', 'index.html'),
    // 兼容历史路径
    path.join(__dirname, '..', 'dist-react', 'index.html'),
    path.join(process.resourcesPath, 'app.asar', 'dist-react', 'index.html'),
    path.join(process.resourcesPath, 'app', 'dist-react', 'index.html'),
  ];

  const foundPath = candidates.find((candidate) => fs.existsSync(candidate)) || null;
  return { foundPath, candidates };
}

/**
 * 创建主浏览器窗口
 */
function createWindow() {
  const preloadPath = path.join(__dirname, 'preload.js');
  console.log('[main] __dirname:', __dirname);
  console.log('[main] preloadPath:', preloadPath);
  console.log('[main] preload file exists:', fs.existsSync(preloadPath));

  mainWindow = new BrowserWindow({
    show: false,
    width: 1440,
    height: 1000,
    minWidth: 1440,
    minHeight: 1000,
    maxWidth: 1440,
    maxHeight: 1000,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    minimizable: true,
    closable: true,
    frame: false, // 全平台使用无边框，Windows 依赖自定义 TitleBar
    // Windows 11 使用系统圆角，需要 transparent: false 和 backgroundColor
    // Windows 10 使用 CSS 圆角，需要 transparent: true
    transparent: process.platform === 'win32' && !windowsVersion.isWin11,
    roundedCorners: true,
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'hidden', // macOS 保持 hiddenInset，其他平台使用 hidden
    // trafficLightPosition 仅在 macOS 生效，其他平台设置会产生警告
    ...(process.platform === 'darwin' && { trafficLightPosition: { x: 14, y: 14 } }),
    // 背景色设置
    backgroundColor: process.platform === 'win32' 
      ? (windowsVersion.isWin11 ? '#f1f5f9' : undefined)
      : (nativeTheme.shouldUseDarkColors ? '#1e1e1e' : '#f8fafc'),
    // macOS 使用 .icns，Windows 使用 .ico，Linux 使用 .png
    icon: process.platform === 'darwin'
      ? path.join(__dirname, 'logo.icns')
      : process.platform === 'win32'
        ? path.join(__dirname, 'logo.png')
        : path.join(__dirname, 'logo.png'),
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webSecurity: true,
      webviewTag: false
    }
  });

  // 隐藏原生应用菜单/菜单栏（保留窗口最小化与关闭按钮）
  Menu.setApplicationMenu(null);
  mainWindow.setMenuBarVisibility(false);

  // [Optimization] Windows 恢复强制刷新功能 (Ctrl+R / Ctrl+Shift+R)
  // 即使禁用了原生菜单，我们仍允许开发/调试时的刷新操作
  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.control && input.key.toLowerCase() === 'r') {
      if (input.shift) {
        console.log('[main] Triggering forced reload (ignoring cache)');
        mainWindow?.webContents.reloadIgnoringCache();
      } else {
        console.log('[main] Triggering reload');
        mainWindow?.webContents.reload();
      }
      event.preventDefault();
    }
  });

  if (isDev) {
    console.log('[main] Starting in development mode');
    const port = process.env.VITE_PORT || '3000';
    // 支持多个端口，处理端口冲突情况
    const devUrls = [
      `http://127.0.0.1:${port}`,
      'http://127.0.0.1:3001',
      'http://127.0.0.1:3002',
      'http://127.0.0.1:3003',
      'http://127.0.0.1:5173'  // Vite默认端口
    ];

    handleDevMode(devUrls);
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  } else {
    console.log('[main] Starting in production mode');
    const { foundPath, candidates } = resolveBundledIndexPath();
    try {
      console.log('[main] __dirname:', __dirname);
      console.log('[main] Checking bundled index candidates:', candidates);
      if (!foundPath) {
        console.error('[main] Production index.html not found. Checked paths:', candidates);
        const pathsHtml = candidates.map((p) => `<li><code>${p}</code></li>`).join('');
        const msg = `<!doctype html><html><head><meta charset="utf-8"><title>Missing index.html</title></head><body style="font-family:Arial,Helvetica,sans-serif;padding:24px;"><h1>Missing index.html</h1><p>The application failed to find the bundled <code>index.html</code> in production mode.</p><p>Checked paths:</p><ul>${pathsHtml}</ul><p>Run <code>npm run build:react</code> in the electron folder to generate the build artifacts, or start in dev mode.</p></body></html>`;
        const dataUrl = 'data:text/html;charset=utf-8,' + encodeURIComponent(msg);
        mainWindow.loadURL(dataUrl).catch(e => console.error('[main] Loading diagnostic page failed:', e));
      } else {
        mainWindow.loadFile(foundPath).then(() => console.log('[main] Loaded bundled index.html from', foundPath)).catch((error) => {
          console.error('[main] Failed to load production index.html:', error);
        });
      }
    } catch (e) {
      console.error('[main] Error while attempting to load production index.html:', e);
    }
  }

  // 在新窗口中打开外部链接
  mainWindow.webContents.setWindowOpenHandler((details) => {
    shell.openExternal(details.url);
    return { action: 'deny' };
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  let isWindowReadyToShow = false;
  let isRendererLoaded = false;
  const tryShowWindow = () => {
    if (!mainWindow) return;
    if (isWindowReadyToShow && isRendererLoaded) {
      mainWindow.show();
    }
  };

  mainWindow.once('ready-to-show', () => {
    isWindowReadyToShow = true;
    tryShowWindow();
  });

  mainWindow.webContents.once('did-finish-load', () => {
    isRendererLoaded = true;
    tryShowWindow();
  });

}

/**
 * 处理开发模式下的URL加载
 * @param devUrls 开发服务器URL列表
 */
async function handleDevMode(devUrls: string[]) {
  console.log('[main] Attempting to load dev URLs:', devUrls);
  let loaded = false;
  for (const url of devUrls) {
    if (!loaded) {
      console.log(`[main] Trying to load: ${url}`);
      loaded = await tryLoadDevURL(mainWindow!, url);
      if (loaded) {
        console.log(`[main] Successfully loaded: ${url}`);
      }
    }
  }

  if (!loaded) {
    console.error('[main] Failed to load dev server after retries, attempting to fall back to bundled index.html');
    const { foundPath, candidates } = resolveBundledIndexPath();
    try {
      console.log('[main] __dirname:', __dirname);
      console.log('[main] Checking bundled index candidates:', candidates);
      if (!foundPath) {
        console.error('[main] Fallback index.html not found. Checked paths:', candidates);
        // Load a diagnostic HTML so the window is not just about:blank and shows useful info
        const pathsHtml = candidates.map((p) => `<li><code>${p}</code></li>`).join('');
        const msg = `<!doctype html><html><head><meta charset="utf-8"><title>Missing index.html</title></head><body style="font-family:Arial,Helvetica,sans-serif;padding:24px;"><h1>Missing index.html</h1><p>The application failed to load the dev server and the bundled <code>index.html</code> was not found.</p><p>Checked paths:</p><ul>${pathsHtml}</ul><p>Check that you ran <code>npm run build:react</code> or started the dev server before launching Electron.</p></body></html>`;
        const dataUrl = 'data:text/html;charset=utf-8,' + encodeURIComponent(msg);
        await mainWindow?.loadURL(dataUrl);
      } else {
        await mainWindow?.loadFile(foundPath);
        console.log('[main] Loaded bundled index.html from', foundPath);
      }
    } catch (e) {
      console.error('[main] Fallback load failed with error:', e);
    }
  }
}

// 增加 GPU 内存限制以避免 "tile memory limits exceeded" 警告
app.commandLine.appendSwitch('force-gpu-mem-available-mb', '2048');
app.commandLine.appendSwitch('enable-gpu-rasterization');
app.commandLine.appendSwitch('enable-zero-copy');

// Electron 应用准备就绪后执行
app.whenReady().then(() => {
  console.log('[main] app.whenReady() called');

  // 注册 Auth IPC handlers (本地离线认证支持)
  registerAuthHandlers();

  createMenu();
  createWindow();
  // 设置文件导出处理器
  setupExportHandlers();
  // 设置自动更新
  setupAutoUpdater();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// 所有窗口关闭时退出应用
app.on('window-all-closed', () => {
  // 优雅关闭，避免EPIPE错误
  try {
    console.log('[main] All windows closed, quitting app');
    app.quit();
  } catch (error) {
    // 忽略关闭过程中的错误，避免EPIPE
    if ((error as any).code !== 'EPIPE') {
      // 只有非EPIPE错误才记录
      try {
        console.error('[main] Error during app quit:', error);
      } catch {
        // 完全静默
      }
    }
  }
});

// 应用即将退出前的清理工作
app.on('before-quit', (event) => {
  try {
    console.log('[main] App is about to quit');
    // 这里可以添加清理逻辑
  } catch (error) {
    // 忽略EPIPE错误
    if ((error as any).code !== 'EPIPE') {
      try {
        console.error('[main] Error during before-quit:', error);
      } catch {
        // 静默处理
      }
    }
  }
});

// 处理进程信号
process.on('SIGTERM', () => {
  try {
    console.log('[main] Received SIGTERM');
    app.quit();
  } catch (error) {
    // 忽略EPIPE错误
    if ((error as any).code !== 'EPIPE') {
      try {
        console.error('[main] Error handling SIGTERM:', error);
      } catch {
        // 静默处理
      }
    }
  }
});

process.on('SIGINT', () => {
  try {
    console.log('[main] Received SIGINT');
    app.quit();
  } catch (error) {
    // 忽略EPIPE错误
    if ((error as any).code !== 'EPIPE') {
      try {
        console.error('[main] Error handling SIGINT:', error);
      } catch {
        // 静默处理
      }
    }
  }
});
// --- 窗口控制 IPC 处理器 ---
ipcMain.handle('window:minimize', () => {
  mainWindow?.minimize();
});

ipcMain.handle('window:maximize', () => {
  if (mainWindow?.isMaximized()) {
    mainWindow?.unmaximize();
  } else {
    mainWindow?.maximize();
  }
});

ipcMain.handle('window:close', () => {
  mainWindow?.close();
});

ipcMain.handle('window:status', () => {
  return {
    isMaximized: mainWindow?.isMaximized(),
    isMinimized: mainWindow?.isMinimized(),
    isFocused: mainWindow?.isFocused(),
  };
});

// 通知处理
ipcMain.handle('notification:show', async (event, { title, body }) => {
  try {
    const n = new Notification({ title: title || '通知', body: body || '' });
    n.show();
    return { success: true };
  } catch (error) {
    console.error('通知显示失败:', error);
    return { success: false, error: error instanceof Error ? error.message : '未知错误' };
  }
});

// 使用外部系统浏览器打开路径
ipcMain.handle('shell:open-path', async (event, path) => {
  try {
    const error = await shell.openPath(path);
    if (error) {
      console.error('打开路径失败:', error);
      return { success: false, error };
    }
    return { success: true };
  } catch (error) {
    console.error('shell:open-path 异常:', error);
    return { success: false, error: error instanceof Error ? error.message : '未知错误' };
  }
});

ipcMain.handle('shell:open-external', async (_event, url: string) => {
  try {
    if (!url || typeof url !== 'string') {
      throw new Error('Invalid url');
    }
    await shell.openExternal(url);
    return { success: true };
  } catch (error) {
    console.error('shell:open-external 异常:', error);
    return { success: false, error: error instanceof Error ? error.message : '未知错误' };
  }
});

// 导出文件处理（供 preload -> renderer 调用 exportSaveFile 使用）
function setupExportHandlers() {
  ipcMain.handle('export:save-file', async (event, options) => {
    try {
      const { data, filename, fileType } = options;

      const result = await dialog.showSaveDialog(mainWindow!, {
        defaultPath: filename,
        filters: [{ name: fileType, extensions: [fileType] }]
      });

      if (!result.canceled && result.filePath) {
        // data may be a Buffer-like object
        const buffer = Buffer.isBuffer(data) ? data : Buffer.from(data);
        try {
          fs.writeFileSync(result.filePath, buffer);
        } catch (writeError) {
          const err = writeError as NodeJS.ErrnoException;
          if (err.code === 'EACCES') {
            return { success: false, error: '权限不足，无法写入该路径，请选择其他位置' };
          } else if (err.code === 'EBUSY') {
            // Windows 上文件被其他进程占用
            return { success: false, error: '文件正被其他程序使用，请关闭后重试' };
          }
          throw writeError; // 其他错误交由外层 catch 处理
        }
        return { success: true, path: result.filePath };
      }

      return { success: false, canceled: true };
    } catch (error) {
      console.error('导出文件失败:', error);
      return { success: false, error: error instanceof Error ? error.message : '未知错误' };
    }
  });
}

function setupAutoUpdater() {
  log.transports.file.level = 'debug';
  autoUpdater.logger = log;

  // OSS Edition - 自动更新已禁用
  // 如需启用，请设置 UPDATE_SERVER_URL 环境变量
  const resolveUpdateServerUrl = (): string => {
    const normalize = (value?: string): string => String(value || '').trim().replace(/\/+$/, '');
    const platformUrl = process.platform === 'win32'
      ? normalize(process.env.UPDATE_SERVER_URL_WIN)
      : process.platform === 'darwin'
        ? normalize(process.env.UPDATE_SERVER_URL_MAC)
        : normalize(process.env.UPDATE_SERVER_URL_LINUX);
    return platformUrl || normalize(process.env.UPDATE_SERVER_URL) || '';
  };

  const updateServerUrl = resolveUpdateServerUrl();
  
  if (!updateServerUrl) {
    log.info('OSS Edition: 自动更新已禁用');
    return;
  }
  
  log.info(`自动更新源(${process.platform}): ${updateServerUrl}`);
  autoUpdater.setFeedURL({
    provider: 'generic',
    url: updateServerUrl
  });

  // 静默后台下载，退出时自动安装
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  // 统一发送 IPC 给渲染进程的辅助函数
  const sendToWindow = (channel: string, data?: any) => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send(channel, data);
    }
  };

  autoUpdater.on('error', (err) => {
    log.error('更新检查发生错误：', err);
    // 过滤"基础设施未就绪"类错误（服务器未配置/网络不通/文件不存在），
    // 这类错误属于正常现象，不应打扰用户；只有真正的更新下载/安装故障才提示。
    const msg: string = err.message || '';
    const isBenign =
      /ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|ERR_NETWORK|ERR_FAILED/i.test(msg) ||
      /404|not found|no releases|HttpError/i.test(msg) ||
      /net::/i.test(msg);
    if (!isBenign) {
      sendToWindow('update-error', { message: msg || '更新安装失败，请重试' });
    }
  });

  autoUpdater.on('checking-for-update', () => {
    log.info('正在检查更新...');
    sendToWindow('update-checking');
  });

  autoUpdater.on('update-available', (info) => {
    log.info('发现新版本：', info);
    sendToWindow('update-available', info);
  });

  autoUpdater.on('update-not-available', (info) => {
    log.info('当前为最新版本。');
    sendToWindow('update-not-available', info);
  });

  autoUpdater.on('download-progress', (progressObj) => {
    log.info(`下载进度: ${progressObj.percent.toFixed(1)}% (${progressObj.transferred}/${progressObj.total} bytes)`);
    sendToWindow('update-download-progress', progressObj);
  });

  autoUpdater.on('update-downloaded', (info) => {
    log.info('新版本下载完毕，准备提示重启更新！');
    sendToWindow('update-downloaded', info);
  });

  ipcMain.handle('app:install-update', () => {
    autoUpdater.quitAndInstall(false, true);
  });

  // 手动触发检查更新的 IPC 接口（供渲染进程"检查更新"按钮调用）
  ipcMain.handle('app:check-for-update', async () => {
    try {
      if (isDev && process.env.TEST_UPDATE !== '1') {
        return { checking: false, reason: 'dev-mode' };
      }
      await autoUpdater.checkForUpdatesAndNotify();
      return { checking: true };
    } catch (e: any) {
      log.error('手动检查更新失败:', e);
      return { checking: false, error: e.message };
    }
  });

  // 延时5秒后启动首次检查，避免影响应用启动速度
  setTimeout(() => {
    if (!isDev || process.env.TEST_UPDATE === '1') {
      log.info('开始请求检查更新');
      autoUpdater.checkForUpdatesAndNotify().catch(e => {
        log.error('触发检查更新报错:', e);
      });

      // 每4小时定时后台检查，确保长期运行的应用能发现新版本
      setInterval(() => {
        log.info('定时检查更新...');
        autoUpdater.checkForUpdatesAndNotify().catch(e => {
          log.error('定时检查更新报错:', e);
        });
      }, 4 * 60 * 60 * 1000);
    }
  }, 5000);
}

// 服务器配置 IPC 处理器
ipcMain.handle('config:get-server-url', () => {
  return configService.get('serverUrl') || '';
});

ipcMain.handle('config:set-server-url', (_event, url: string) => {
  try {
    configService.set('serverUrl', url);
    return { success: true };
  } catch (error) {
    console.error('[main] Failed to set server URL:', error);
    return { success: false, error: error instanceof Error ? error.message : '未知错误' };
  }
});
