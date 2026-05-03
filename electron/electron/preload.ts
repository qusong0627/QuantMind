import { contextBridge, ipcRenderer } from 'electron';

console.log('[preload] preload.ts loaded!');
console.log('[preload] contextBridge:', typeof contextBridge);
console.log('[preload] ipcRenderer:', typeof ipcRenderer);

/**
 * @module Preload
 * @description 预加载脚本，用于在渲染进程中安全地暴露Node.js和Electron API。
 * 通过 contextBridge, 我们可以选择性地将主进程的功能暴露给渲染进程，
 * 同时保持上下文隔离，以增强安全性。
 */

console.log('[preload] Exposing electronAPI...');

// 暴露给渲染进程的API
contextBridge.exposeInMainWorld('electronAPI', {
  /**
   * 获取应用版本号
   * @returns {string} 应用版本号
   */
  getAppVersion: () => process.env.npm_package_version || '1.0.0',

  /**
   * 最小化窗口
   */
  minimizeWindow: () => ipcRenderer.invoke('window:minimize'),

  /**
   * 最大化/还原窗口
   */
  maximizeWindow: () => ipcRenderer.invoke('window:maximize'),

  /**
   * 关闭窗口
   */
  closeWindow: () => ipcRenderer.invoke('window:close'),

  /**
   * 获取当前操作系统平台
   * @returns {string} 操作系统平台 (e.g., 'win32', 'darwin')
   */
  getPlatform: () => process.platform,

  /**
   * 获取系统区域设置
   * @returns {string} 区域设置字符串 (e.g., 'en-US', 'zh-CN')
   */
  getLocale: () => navigator.language || 'zh-CN',

  /**
   * 显示系统通知
   * @param {string} title - 通知标题
   * @param {string} body - 通知内容
   * @returns {Promise<void>}
   */
  showNotification: (title: string, body: string) =>
    ipcRenderer.invoke('notification:show', { title, body }),

  /**
   * 导出并保存文件
   * @param {any} options - 导出选项
   * @returns {Promise<any>} 保存操作的结果
   */
  exportSaveFile: (options: any) => ipcRenderer.invoke('export:save-file', options),

  /**
   * 使用系统默认应用打开路径
   * @param {string} path - 文件或文件夹路径
   */
  openPath: (path: string) => ipcRenderer.invoke('shell:open-path', path),
  /**
   * 使用系统默认浏览器打开外部 URL
   * @param {string} url - 外部链接
   */
  openExternal: (url: string) => ipcRenderer.invoke('shell:open-external', url),

  /**
   * 监听菜单导出事件
   */
  onMenuExportData: (callback: () => void) => {
    const subscription = (_event: any) => callback();
    ipcRenderer.on('menu-export-data', subscription);
    return () => ipcRenderer.removeListener('menu-export-data', subscription);
  },

  /**
   * 监听更新可用事件
   */
  onUpdateAvailable: (callback: (info: any) => void) => {
    const subscription = (_event: any, info: any) => callback(info);
    ipcRenderer.on('update-available', subscription);
    return () => ipcRenderer.removeListener('update-available', subscription);
  },

  /**
   * 监听更新进度事件
   */
  onUpdateDownloadProgress: (callback: (progress: any) => void) => {
    const subscription = (_event: any, progress: any) => callback(progress);
    ipcRenderer.on('update-download-progress', subscription);
    return () => ipcRenderer.removeListener('update-download-progress', subscription);
  },

  /**
   * 监听更新下载完成事件
   */
  onUpdateDownloaded: (callback: (info: any) => void) => {
    const subscription = (_event: any, info: any) => callback(info);
    ipcRenderer.on('update-downloaded', subscription);
    return () => ipcRenderer.removeListener('update-downloaded', subscription);
  },

  /**
   * 监听更新错误事件
   */
  onUpdateError: (callback: (error: any) => void) => {
    const subscription = (_event: any, error: any) => callback(error);
    ipcRenderer.on('update-error', subscription);
    return () => ipcRenderer.removeListener('update-error', subscription);
  },

  /**
   * 安装更新并重启
   */
  installUpdate: () => ipcRenderer.invoke('app:install-update'),

  /**
   * 手动检查更新
   */
  checkForUpdates: () => ipcRenderer.invoke('app:check-for-update'),

  /**
   * 获取系统详细版本号 (用于区分 Win10/Win11)
   */
  getSystemVersion: () => process.getSystemVersion(),

  /**
   * 获取服务器配置地址
   */
  getServerUrl: () => ipcRenderer.invoke('config:get-server-url'),

  /**
   * 设置服务器配置地址
   */
  setServerUrl: (url: string) => ipcRenderer.invoke('config:set-server-url', url),
});

console.log('[preload] electronAPI exposed');

console.log('[preload] All APIs exposed successfully!');

// 为window对象添加TypeScript类型定义，以便在渲染器中获得智能提示
declare global {
  interface Window {
    electronAPI: {
      getAppVersion: () => string;
      minimizeWindow: () => Promise<void>;
      maximizeWindow: () => Promise<void>;
      closeWindow: () => Promise<void>;
      getPlatform: () => string;
      getSystemVersion: () => string;
      getLocale: () => string;
      showNotification: (title: string, body: string) => Promise<void>;
      exportSaveFile: (options: any) => Promise<any>;
      openPath: (path: string) => Promise<any>;
      openExternal: (url: string) => Promise<{ success: boolean; error?: string }>;
      onMenuExportData: (callback: () => void) => () => void;
      onUpdateAvailable: (callback: (info: any) => void) => () => void;
      onUpdateDownloadProgress: (callback: (progress: { percent: number; transferred: number; total: number; bytesPerSecond: number }) => void) => () => void;
      onUpdateDownloaded: (callback: (info: any) => void) => () => void;
      onUpdateError: (callback: (error: { message: string }) => void) => () => void;
      installUpdate: () => Promise<void>;
      checkForUpdates: () => Promise<{ checking: boolean; reason?: string; error?: string }>;
      getServerUrl: () => Promise<string>;
      setServerUrl: (url: string) => Promise<{ success: boolean; error?: string }>;
    };
  }
}
