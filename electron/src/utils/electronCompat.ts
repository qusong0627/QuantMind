// Web 环境下 Electron API 兼容层
// 当不在 Electron 环境时，提供空实现

const isElectron = typeof window !== 'undefined' && Boolean((window as any).process?.type);

const electronAPI = {
  // 平台信息
  getPlatform: () => isElectron ? (window as any).electronAPI?.getPlatform?.() : 'web',
  getSystemVersion: () => isElectron ? (window as any).electronAPI?.getSystemVersion?.() : navigator.userAgent,
  
  // 窗口控制 (Web 环境下忽略)
  minimizeWindow: () => {
    if (isElectron) (window as any).electronAPI?.minimizeWindow?.();
  },
  closeWindow: () => {
    if (isElectron) (window as any).electronAPI?.closeWindow?.();
  },
  
  // 自动更新 (Web 环境下忽略)
  onUpdateAvailable: (callback: any) => {
    if (isElectron) return (window as any).electronAPI?.onUpdateAvailable?.(callback);
    return () => {};
  },
  onUpdateDownloadProgress: (callback: any) => {
    if (isElectron) return (window as any).electronAPI?.onUpdateDownloadProgress?.(callback);
    return () => {};
  },
  onUpdateDownloaded: (callback: any) => {
    if (isElectron) return (window as any).electronAPI?.onUpdateDownloaded?.(callback);
    return () => {};
  },
  onUpdateError: (callback: any) => {
    if (isElectron) return (window as any).electronAPI?.onUpdateError?.(callback);
    return () => {};
  },
  installUpdate: () => {
    if (isElectron) (window as any).electronAPI?.installUpdate?.();
  },
  
  // 外部链接
  openExternal: async (url: string) => {
    if (isElectron) return (window as any).electronAPI?.openExternal?.(url);
    window.open(url, '_blank');
    return { success: true };
  },
  
  // 文件导出 (Web 环境使用下载)
  exportSaveFile: async (options: { defaultPath?: string; content?: string; data?: Blob }) => {
    if (isElectron) return (window as any).electronAPI?.exportSaveFile?.(options);
    
    // Web 环境使用 Blob 下载
    const blob = options.data || new Blob([options.content || ''], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = options.defaultPath || 'download.txt';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    return { success: true };
  },
  
  // 通知
  showNotification: (title: string, body: string) => {
    if (isElectron) {
      (window as any).electronAPI?.showNotification?.(title, body);
    } else if ('Notification' in window && Notification.permission === 'granted') {
      new Notification(title, { body });
    } else if ('Notification' in window) {
      Notification.requestPermission().then(permission => {
        if (permission === 'granted') {
          new Notification(title, { body });
        }
      });
    }
  },
  
  // 菜单事件
  onMenuExportData: (callback: any) => {
    if (isElectron) return (window as any).electronAPI?.onMenuExportData?.(callback);
    return () => {};
  },
  
  // AI IDE 相关 (Web 环境下忽略)
  keepAliveAIIDEBackend: async () => {
    if (isElectron) return (window as any).electronAPI?.keepAliveAIIDEBackend?.();
  },
  ensureDefaultAIIDEWorkspace: async () => {
    if (isElectron) return (window as any).electronAPI?.ensureDefaultAIIDEWorkspace?.();
  },
  getAIIDERuntimeStatus: async () => {
    if (isElectron) return (window as any).electronAPI?.getAIIDERuntimeStatus?.();
    return { running: false };
  },
};

// 注入到 window
if (typeof window !== 'undefined' && !(window as any).electronAPI) {
  try {
    (window as any).electronAPI = electronAPI;
  } catch {
    // window.electronAPI 可能已被设为只读属性，跳过注入
  }
}

export default electronAPI;
