import React from 'react';
import ReactDOM from 'react-dom/client';
import { Provider } from 'react-redux';
import { HashRouter } from 'react-router-dom';
import App from './App';
import store from './store';

// Electron API 兼容层 (Web 模式自动降级)
import './utils/electronCompat';

// 错误上报逻辑已移除，由 ErrorBoundary 统一处理


import './monaco/setup';
import { preloadAiIdeResources } from './features/auth/utils/lazyLoad';

import './styles/global.css';
import './styles/mac-theme.css';
import './styles/ai-strategy-theme.css';

// 启动日志：明确本地服务连接
const serviceHost = import.meta.env.VITE_SERVICE_HOST || 'localhost:8000';
console.log(
  `%c[QuantMind OSS]%c 已连接本地服务: %c${serviceHost}`,
  'color: #10b981; font-weight: bold; font-size: 14px;',
  'color: #6b7280; font-size: 14px;',
  'color: #3b82f6; font-weight: bold; text-decoration: underline; font-size: 14px;'
);
console.log(
  `%c[System]%c 协议: %c${import.meta.env.VITE_HTTP_PROTOCOL || 'http'}%c | 版本: %cOSS Edition`,
  'color: #f59e0b; font-weight: bold;',
  'color: #6b7280;',
  'color: #10b981; font-weight: bold;',
  'color: #6b7280;',
  'color: #6366f1; font-weight: bold;'
);

const platform = window.electronAPI?.getPlatform?.();
if (platform) {
  document.documentElement.classList.add(`platform-${platform}`);
  
  if (platform === 'win32') {
    const version = window.electronAPI?.getSystemVersion?.() || '';
    // Windows 11 内部版本号从 10.0.22000 开始
    // version 格式: "10.0.22621" -> 提取最后一段作为 buildNumber
    const parts = version.split('.');
    const buildNumber = parseInt(parts[parts.length - 1] || '0', 10);
    if (buildNumber >= 22000) {
      document.documentElement.classList.add('os-win11');
    } else {
      document.documentElement.classList.add('os-win10');
    }
  }
}
document.documentElement.classList.add('qm-rounded');

// 启动即预加载 AI-IDE 资源（不等待路由/登录页）
void preloadAiIdeResources();

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <Provider store={store}>
      <HashRouter>
        <App />
      </HashRouter>
    </Provider>
  </React.StrictMode>
);
