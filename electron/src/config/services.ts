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
 * 初始化动态服务器配置（桌面端启动时调用）
 * 优先级：持久化配置 > 旧版缓存 > Electron 配置文件 > 桌面端默认本地地址
 *
 * 关键约定：健康检查失败**绝不删除**用户已保存的服务器地址。
 * 后端可能正处于重启/冷启动/网络抖动，探测失败只打日志、保留配置并继续使用，
 * 避免“隔段时间保存的 IP 丢失、需重新配置”的问题。删除配置只能由用户手动操作。
 *
 * 新增策略：持久化地址探测不可达时，不直接采用它，而是继续回退探测其它候选地址
 * （旧版缓存 / 配置文件 / 本地默认），若找到可达地址则自动采用并迁移持久化配置，
 * 解决“服务器换 IP 后客户端仍连旧地址导致卡在登录/加载”的问题。
 */
export async function initDynamicServerUrl(): Promise<void> {
  const persisted = readPersistedServerUrl();
  const legacy = readLegacyPersistedServerUrl();

  // 1. 持久化配置：可达则采用；不可达则继续探测其它候选，不立即采用
  if (persisted) {
    const persistedOk = await isServerReachable(persisted);
    if (persistedOk) {
      dynamicServerUrl = persisted;
      return;
    }
    console.warn(`[services] 持久化服务器 ${persisted} 当前不可达，继续探测其它候选地址`);
  } else if (legacy) {
    // 2. 旧 key 遗留缓存：可达则采用并迁移到新 key；不可达则继续探测
    const legacyOk = await isServerReachable(legacy);
    if (legacyOk) {
      dynamicServerUrl = legacy;
      persistServerUrl(legacy);
      return;
    }
    console.warn(`[services] 旧版服务器地址当前不可达，保留缓存: ${legacy}`);
  }

  // 3. 候选地址集合：配置文件 + 本地默认（去重、剔除已确认不可达的持久化/旧缓存）
  const candidates: string[] = [];
  if (isElectronEnv()) {
    try {
      const url = await (window as any).electronAPI.getServerUrl();
      if (url && typeof url === 'string') {
        candidates.push(url.replace(/\/+$/, ''));
      }
    } catch (e) {
      console.warn('[services] Failed to get server URL from config:', e);
    }
  }
  candidates.push(DEFAULT_ELECTRON_API_BASE);

  // 4. 逐个探测候选，取第一个可达地址；找到后自动迁移持久化配置
  for (const url of [...new Set(candidates)]) {
    const ok = await isServerReachable(url);
    if (ok) {
      dynamicServerUrl = url;
      persistServerUrl(url);
      console.warn(`[services] 已自动切换到可达服务器 ${url}`);
      return;
    }
  }

  // 5. 全部不可达：保留原持久化配置继续使用（后端可能重启中），不删除
  dynamicServerUrl = persisted || legacy || candidates[0] || DEFAULT_ELECTRON_API_BASE;
  if (persisted) persistServerUrl(persisted);
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
 * Web 端（非 Electron）强制返回空字符串以走相对路径 `/api/v1`，经 Nginx 反代到 quantmind:8000，
 * 避免构建时 VITE_* 固化为 localhost 导致外网 IP 无法登录；桌面端不受影响
 */
function getBaseUrl(): string {
  // Web 浏览器走相对路径，不受构建时环境变量影响
  if (!isElectronEnv()) {
    return '';
  }
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
  return DEFAULT_ELECTRON_API_BASE;
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
  get API_GATEWAY() { return !isElectronEnv() ? '' : (normalizeBaseUrl(ENV.VITE_API_GATEWAY_URL) || getBaseUrl()); },  get MARKET_DATA() { return !isElectronEnv() ? '' : (normalizeBaseUrl(ENV.VITE_MARKET_DATA_API_URL) || getBaseUrl()); },
  get DATA_SERVICE() { return !isElectronEnv() ? '' : (normalizeBaseUrl(ENV.VITE_DATA_SERVICE_API_URL) || getBaseUrl()); },
  get USER_SERVICE() { return !isElectronEnv() ? '' : (normalizeBaseUrl(ENV.VITE_USER_API_URL) || getBaseUrl()); },
  get AI_STRATEGY() { return !isElectronEnv() ? '' : (normalizeBaseUrl(ENV.VITE_AI_STRATEGY_API_URL) || getBaseUrl()); },
  get STOCK_QUERY() { return !isElectronEnv() ? '' : (normalizeBaseUrl(ENV.VITE_STOCK_QUERY_API_URL) || getBaseUrl()); },
  get TRADING() { return !isElectronEnv() ? '' : (normalizeBaseUrl(ENV.VITE_TRADING_API_URL) || getBaseUrl()); },
  get QLIB_SERVICE() { return !isElectronEnv() ? '' : (normalizeBaseUrl(ENV.VITE_QLIB_SERVICE_URL) || getBaseUrl()); },
  get ENGINE_SERVICE() { return !isElectronEnv() ? '' : (normalizeBaseUrl(ENV.VITE_ENGINE_SERVICE_URL) || getBaseUrl()); },
  get WEBSOCKET_MARKET() { return getWebSocketUrl(); },
} as const;

/**
 * Web 安全的服务 base 解析：Web（非 Electron）一律返回相对路径 fallback（通常是 `/api/v1`），
 * 忽略构建时固化的 VITE_* 绝对地址与 localStorage 旧缓存，避免外网 IP 下直连 127.0.0.1:8000；
 * 桌面端保持原逻辑（VITE_* > 共享配置）。
 */
export function resolveWebSafeServiceBase(envVal: string | undefined, fallback: string): string {
  if (!isElectronEnv()) return fallback;
  return normalizeBaseUrl(envVal || '') || fallback;
}

// API路径配置
export const API_PATHS = {
  V1: '/api/v1',
  HEALTH: '/health',  STRATEGIES: '/strategies',
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
