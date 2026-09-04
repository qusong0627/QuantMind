/**
 * 统一服务端口配置
 * 所有服务端口的唯一配置来源
 */
export const SERVICE_PORTS = {
  // 前端服务
  FRONTEND_DEV: 3000,

  // 后端服务 (统一通过网关 8000)
  API_GATEWAY: 8000,
  MARKET_DATA: 8000,    // 原 8002
  DATA_SERVICE: 8000,   // 原 8002
  USER_SERVICE: 8000,   // 原 8011
  AI_STRATEGY: 8000,    // 原 8007
  STOCK_QUERY: 8000,    // 原 8010
  TRADING: 8000,        // 原 8004
  QLIB_SERVICE: 8000, // Qlib快速回测服务（收敛至网关）

  // WebSocket服务
  WEBSOCKET_MARKET: 8003,

  // 数据库
  REDIS: 6379,
} as const;

const ENV: Record<string, any> = typeof import.meta !== 'undefined' ? (import.meta as any).env || {} : {};

// 动态服务器配置（桌面端用户设置）
let dynamicServerUrl: string | null = null;
const SERVER_URL_STORAGE_KEY = 'quantmind_server_url_v2';
const LEGACY_SERVER_URL_STORAGE_KEY = 'quantmind_server_url';

// Electron 桌面端兜底地址：OSS 本地 Docker 后端（api 网关 8000）
const DEFAULT_ELECTRON_API_BASE = 'http://127.0.0.1:8000';

function readPersistedServerUrl(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return localStorage.getItem(SERVER_URL_STORAGE_KEY)?.trim() || null;
  } catch {
    return null;
  }
}

function readLegacyPersistedServerUrl(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return localStorage.getItem(LEGACY_SERVER_URL_STORAGE_KEY)?.trim() || null;
  } catch {
    return null;
  }
}

function persistServerUrl(url: string | null): void {
  if (typeof window === 'undefined') return;
  try {
    if (url) {
      localStorage.setItem(SERVER_URL_STORAGE_KEY, url);
      localStorage.removeItem(LEGACY_SERVER_URL_STORAGE_KEY);
    } else {
      localStorage.removeItem(SERVER_URL_STORAGE_KEY);
    }
  } catch {
    // ignore storage failures
  }
}

/**
 * 检测是否为 Electron 桌面环境
 *
 * 仅当真正具备桌面端服务器配置能力（preload 暴露 getServerUrl）才算。
 * 注意: web 模式会经 electronCompat 注入兼容 stub（无 getServerUrl），
 * 若按 typeof electronAPI === 'object' 判断会把浏览器误认为桌面端，
 * 导致 API 基址落入 127.0.0.1:8000 兜底 → 跨源 CORS → 登录失败。
 */
export function isElectronEnv(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof (window as any).electronAPI === 'object' &&
    typeof (window as any).electronAPI?.getServerUrl === 'function'
  );
}

/**
 * 校验服务器地址是否可达（通过 /health 端点）
 */
export async function isServerReachable(url: string, timeoutMs = 8000): Promise<boolean> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const res = await fetch(`${url.replace(/\/+$/, '')}/health`, {
      signal: controller.signal,
      // 不携带凭据，仅做连通性探测
      cache: 'no-store',
    });
    clearTimeout(timer);
    return res.ok;
  } catch {
    return false;
  }
}

/**
 * 清理失效的服务器配置（本地缓存 + 桌面端配置文件）
 */
async function clearStaleServerUrl(reason: string): Promise<void> {
  console.warn(`[services] 服务器地址失效，清除缓存配置: ${reason}`);
  persistServerUrl(null);
  if (typeof window !== 'undefined' && (window as any).electronAPI?.setServerUrl) {
    try {
      await (window as any).electronAPI.setServerUrl('');
    } catch (e) {
      console.warn('[services] 清除桌面配置文件失败（忽略）:', e);
    }
  }
}

/**
 * 初始化动态服务器配置（桌面端启动时调用）
 * 优先级：持久化配置 > Electron 配置文件 > 桌面端默认本地地址
 *
 * 关键约定：健康检查失败**绝不删除**用户已保存的服务器地址。
 * 后端可能正处于重启/冷启动/网络抖动，探测失败只打日志、保留配置并继续使用，
 * 避免“隔段时间保存的 IP 丢失、需重新配置”的问题。
 */
export async function initDynamicServerUrl(): Promise<void> {
  // 1. 持久化配置：若探测可达则采用；若确认不可达，清除缓存并回退本机默认，
  //    避免换 IP/克隆部署到新机器后始终连旧地址导致“验证身份”卡死。
  const persisted = readPersistedServerUrl();
  if (persisted) {
    const ok = await isServerReachable(persisted);
    if (ok) {
      dynamicServerUrl = persisted;
      return;
    }
    console.warn(`[services] 服务器 ${persisted} 探测未通过，清除配置并回退本地默认后端`);
    await clearStaleServerUrl(`换 IP 后旧地址 ${persisted} 不可达`);
  }

  // 2. 旧 key（quantmind_server_url）遗留缓存迁移：可达才采用，失效仅清旧 key（不动新 key）
  const legacy = readLegacyPersistedServerUrl();
  if (legacy) {
    const ok = await isServerReachable(legacy);
    if (ok) {
      dynamicServerUrl = legacy;
      persistServerUrl(legacy);
      return;
    }
    try {
      localStorage.removeItem(LEGACY_SERVER_URL_STORAGE_KEY);
    } catch { /* ignore */ }
    console.warn(`[services] 旧版服务器地址失效，清除缓存: ${legacy}`);
  }

  // 3. Electron 配置文件：同样采用 + 后台探测日志，不清除
  if (isElectronEnv()) {
    try {
      const url = await (window as any).electronAPI.getServerUrl();
      if (url && typeof url === 'string') {
        const normalized = url.replace(/\/+$/, '');
        dynamicServerUrl = normalized;
        persistServerUrl(normalized);
        void isServerReachable(normalized).then((ok) => {
          if (!ok) console.warn(`[services] 配置文件服务器 ${normalized} 探测未通过（可能暂不可达），保留并继续使用`);
        });
        return;
      }
    } catch (e) {
      console.warn('[services] Failed to get server URL from config:', e);
    }

    // 4. 兜底：本地 OSS Docker 后端
    if (!dynamicServerUrl) {
      const ok = await isServerReachable(DEFAULT_ELECTRON_API_BASE);
      if (ok) {
        dynamicServerUrl = DEFAULT_ELECTRON_API_BASE;
        persistServerUrl(DEFAULT_ELECTRON_API_BASE);
      }
    }
  }
}

/**
 * 设置动态服务器配置（用户设置后调用）
 */
export function setDynamicServerUrl(url: string): void {
  dynamicServerUrl = url ? url.replace(/\/+$/, '') : null;
  persistServerUrl(dynamicServerUrl);
}

/**
 * 获取当前动态服务器配置
 */
export function getDynamicServerUrl(): string | null {
  return dynamicServerUrl || readPersistedServerUrl();
}

const HOST = ENV.VITE_SERVICE_HOST || '';
const HTTP_PROTOCOL = ENV.VITE_HTTP_PROTOCOL || 'http';
const WS_PROTOCOL = HTTP_PROTOCOL === 'https' ? 'wss' : 'ws';

export function normalizeBaseUrl(url: string): string {
  if (!url) return url;
  let normalized = url.replace(/\/+$/, '');
  if (normalized.endsWith('/api/v1')) {
    normalized = normalized.slice(0, -'/api/v1'.length);
  }
  return normalized;
}

const API_BASE = normalizeBaseUrl(ENV.VITE_API_BASE_URL || '');

/**
 * 获取基础 URL（优先使用动态配置）
 */
function getBaseUrl(): string {
  // 桌面端优先使用用户配置的服务器地址
  if (dynamicServerUrl) {
    return dynamicServerUrl;
  }
  const persisted = readPersistedServerUrl();
  if (persisted) {
    return persisted;
  }
  if (API_BASE) {
    return API_BASE;
  }
  // Electron 桌面端兜底：本地 OSS Docker 后端（避免 file:// 下相对路径请求全部失败）
  if (isElectronEnv()) {
    return DEFAULT_ELECTRON_API_BASE;
  }
  return API_BASE;
}

// WebSocket URL 构建
const getWebSocketUrl = () => {
  const persisted = getDynamicServerUrl();
  // 桌面端使用动态配置
  if (persisted) {
    return `${persisted.replace(/^http/, 'ws')}/api/v1/ws/market`;
  }
  const gateway = getBaseUrl();
  if (gateway) {
    return `${gateway.replace(/^http/, 'ws')}/api/v1/ws/market`;
  }
  // Web 部署使用相对路径，通过 Nginx 代理
  if (typeof window !== 'undefined') {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}/ws/api/v1/ws/market`;
  }
  // 最后才回退到环境变量，避免开发环境配置压过用户保存的服务器地址
  if (ENV.VITE_WS_BASE_URL || ENV.VITE_WEBSOCKET_MARKET_URL) {
    return ENV.VITE_WS_BASE_URL || ENV.VITE_WEBSOCKET_MARKET_URL;
  }
  return '';
};

export const SERVICE_URLS = {
  get API_GATEWAY() { return normalizeBaseUrl(ENV.VITE_API_GATEWAY_URL) || getBaseUrl(); },
  get MARKET_DATA() { return normalizeBaseUrl(ENV.VITE_MARKET_DATA_API_URL) || getBaseUrl(); },
  get DATA_SERVICE() { return normalizeBaseUrl(ENV.VITE_DATA_SERVICE_API_URL) || getBaseUrl(); },
  get USER_SERVICE() { return normalizeBaseUrl(ENV.VITE_USER_API_URL) || getBaseUrl(); },
  get AI_STRATEGY() { return normalizeBaseUrl(ENV.VITE_AI_STRATEGY_API_URL) || getBaseUrl(); },
  get STOCK_QUERY() { return normalizeBaseUrl(ENV.VITE_STOCK_QUERY_API_URL) || getBaseUrl(); },
  get TRADING() { return normalizeBaseUrl(ENV.VITE_TRADING_API_URL) || getBaseUrl(); },
  get QLIB_SERVICE() { return normalizeBaseUrl(ENV.VITE_QLIB_SERVICE_URL) || getBaseUrl(); },
  get ENGINE_SERVICE() { return normalizeBaseUrl(ENV.VITE_ENGINE_SERVICE_URL) || getBaseUrl(); },
  get WEBSOCKET_MARKET() { return getWebSocketUrl(); },
} as const;

// API路径配置
export const API_PATHS = {
  V1: '/api/v1',
  HEALTH: '/health',
  STRATEGIES: '/strategies',
  MARKET_DATA: '/market-data',
  USER: '/user',
  FILES: '/files',
} as const;

// 完整的服务端点配置
export const SERVICE_ENDPOINTS = {
  get API_GATEWAY() { return `${SERVICE_URLS.API_GATEWAY}${API_PATHS.V1}`; },
  get AI_STRATEGY() { return `${SERVICE_URLS.AI_STRATEGY}${API_PATHS.V1}`; },
  get DATA_SERVICE() { return `${SERVICE_URLS.DATA_SERVICE}${API_PATHS.V1}`; },
  get USER_SERVICE() { return `${SERVICE_URLS.USER_SERVICE}${API_PATHS.V1}`; },
  get QLIB_SERVICE() { return `${SERVICE_URLS.QLIB_SERVICE}${API_PATHS.V1}`; },
  get STOCK_QUERY() { return `${SERVICE_URLS.STOCK_QUERY}${API_PATHS.V1}`; },
  get TRADING() { return `${SERVICE_URLS.TRADING}${API_PATHS.V1}`; },
} as const;

export default {
  PORTS: SERVICE_PORTS,
  URLS: SERVICE_URLS,
  PATHS: API_PATHS,
  ENDPOINTS: SERVICE_ENDPOINTS,
};
