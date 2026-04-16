import React, { useEffect, useState } from 'react';
import { Minus, X, RefreshCw, Download, AlertCircle } from 'lucide-react';

type UpdateState = 'idle' | 'checking' | 'available' | 'downloading' | 'downloaded' | 'error';

export const TitleBar: React.FC = () => {
  const [platform, setPlatform] = useState<string>('');
  const [updateState, setUpdateState] = useState<UpdateState>('idle');
  const [downloadProgress, setDownloadProgress] = useState<number>(0);
  const [updateVersion, setUpdateVersion] = useState<string>('');
  const [errorMessage, setErrorMessage] = useState<string>('');
  const [isElectron, setIsElectron] = useState(false);

  useEffect(() => {
    const isElectronEnv = !!window.electronAPI;
    setIsElectron(isElectronEnv);
    
    if (!isElectronEnv) return;
    setPlatform(window.electronAPI.getPlatform());

    const cleanups: (() => void)[] = [];

    if (window.electronAPI.onUpdateAvailable) {
      cleanups.push(window.electronAPI.onUpdateAvailable((info) => {
        setUpdateVersion(info?.version || '');
        setUpdateState('available');
      }));
    }

    if (window.electronAPI.onUpdateDownloadProgress) {
      cleanups.push(window.electronAPI.onUpdateDownloadProgress((progress) => {
        setUpdateState('downloading');
        setDownloadProgress(Math.round(progress?.percent || 0));
      }));
    }

    if (window.electronAPI.onUpdateDownloaded) {
      cleanups.push(window.electronAPI.onUpdateDownloaded((info) => {
        setUpdateVersion(info?.version || updateVersion);
        setUpdateState('downloaded');
      }));
    }

    if (window.electronAPI.onUpdateError) {
      cleanups.push(window.electronAPI.onUpdateError((error) => {
        setErrorMessage(error?.message || '更新检查失败');
        setUpdateState('error');
        // 5秒后自动清除错误提示
        setTimeout(() => setUpdateState('idle'), 5000);
      }));
    }

    return () => cleanups.forEach(fn => fn());
  }, []);

  const isMac = platform === 'darwin';

  const handleMinimize = () => window.electronAPI?.minimizeWindow();
  const handleClose = () => window.electronAPI?.closeWindow();
  const handleRestartToUpdate = () => window.electronAPI?.installUpdate();

  const renderUpdateBadge = () => {
    switch (updateState) {
      case 'checking':
        // 静默处理：后台检查不在界面上展示，避免干扰用户
        return null;

      case 'available':
        return (
          <div className="flex items-center gap-1.5 px-3 h-6 mr-3 text-xs font-medium text-blue-600 bg-blue-50 rounded-full border border-blue-200">
            <Download className="w-3 h-3 animate-bounce" />
            正在下载{updateVersion ? ` v${updateVersion}` : '新版本'}
          </div>
        );

      case 'downloading':
        return (
          <div className="flex items-center gap-2 px-3 h-6 mr-3 min-w-[160px] text-xs font-medium text-blue-600 bg-blue-50 rounded-full border border-blue-200">
            <Download className="w-3 h-3 shrink-0" />
            <div className="relative flex-1 h-1.5 bg-blue-200 rounded-full overflow-hidden">
              <div
                className="absolute left-0 top-0 h-full bg-blue-500 rounded-full transition-all duration-300"
                style={{ width: `${downloadProgress}%` }}
              />
            </div>
            <span className="w-7 text-right shrink-0">{downloadProgress}%</span>
          </div>
        );

      case 'downloaded':
        return (
          <button
            onClick={handleRestartToUpdate}
            className="flex items-center justify-center gap-1.5 px-3 h-6 mr-3 text-xs font-medium text-white bg-gradient-to-r from-blue-500 to-indigo-500 rounded-full shadow-sm hover:from-blue-600 hover:to-indigo-600 transition-all animate-pulse"
            title={`新版本${updateVersion ? ` v${updateVersion}` : ''}已下载完成，点击重启安装`}
          >
            <RefreshCw className="w-3.5 h-3.5" />
            重启更新
          </button>
        );

      case 'error':
        return (
          <div
            className="flex items-center gap-1.5 px-3 h-6 mr-3 text-xs font-medium text-red-600 bg-red-50 rounded-full border border-red-200 cursor-default"
            title={errorMessage}
          >
            <AlertCircle className="w-3 h-3 shrink-0" />
            更新失败
          </div>
        );

      default:
        return null;
    }
  };

  // 非 Electron 环境（Web 部署）不渲染标题栏
  if (!isElectron) {
    return null;
  }

  // macOS：仅在有更新状态时渲染可点击区域，否则保持纯拖拽层
  if (isMac) {
    const badge = renderUpdateBadge();
    return (
      <div className="window-drag console-windowbar h-12 w-full absolute top-0 flex items-center justify-between px-4 z-[10000]">
        <div className="window-no-drag flex-1" />
        <div className="window-no-drag flex items-center console-windowbar-actions">
          {badge}
        </div>
      </div>
    );
  }

  return (
    <div className="window-drag console-windowbar h-12 w-full flex items-center justify-between pl-4 pr-0 bg-transparent absolute top-0 left-0 z-[10000]">
      <div className="window-no-drag flex-1" />

      {/* 右侧：更新状态 + 窗口控制按钮 */}
      <div className="window-no-drag flex items-center h-full console-windowbar-actions">
        {updateState !== 'idle' && updateState !== 'checking' && (
          <div className="flex items-center px-4 h-full"> 
            {renderUpdateBadge()}
          </div>
        )}

        {platform !== '' && (
          <div className="flex h-full">
            <button
              onClick={handleMinimize}
              className="console-window-control"
              title="最小化"
            >
              <Minus className="w-4 h-4" />
            </button>
            <button
              onClick={handleClose}
              className="console-window-control is-danger"
              title="关闭"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        )}
      </div>
    </div>
  );
};
