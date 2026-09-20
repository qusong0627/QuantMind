import { SERVICE_URLS, API_PATHS } from '../config/services';

// API基础配置
export const API_CONFIG = {
  BASE_URL: SERVICE_URLS.DATA_SERVICE, // 使用统一配置的数据服务URL
  TIMEOUT: 30000,
  RETRY_ATTEMPTS: 3,
  RETRY_DELAY: 1000
};

// API端点定义
export const API_ENDPOINTS = {
  // 市场数据
  MARKET_OVERVIEW: `${API_PATHS.V1}/market/indices`,
  MARKET_REALTIME: `${API_PATHS.V1}/market/realtime`,

  // 用户数据
  USER_PROFILE: `${API_PATHS.V1}/users/me`,
  USER_DETAIL: `${API_PATHS.V1}/users/me/detail`,
  USER_PREFERENCES: `${API_PATHS.V1}/users/preferences`,

  // Security & Devices
  CHANGE_PASSWORD: `${API_PATHS.V1}/users/password`,
  AUDIT_LOGS: `${API_PATHS.V1}/audit/my-logs`,
  DEVICES: `${API_PATHS.V1}/devices`,

  // Phone Management
  PHONE_SEND_CODE: `${API_PATHS.V1}/users/me/phone/send-code`,
  PHONE_BIND: `${API_PATHS.V1}/users/me/phone/bind`,
  PHONE_CHANGE: `${API_PATHS.V1}/users/me/phone/change`,

  // 策略相关
  STRATEGIES: `${API_PATHS.V1}/strategies`,
  STRATEGY_DETAIL: (id: string) => `${API_PATHS.V1}/strategies/${id}`,
  STRATEGY_START: (id: string) => `${API_PATHS.V1}/strategies/${id}/start`,
  STRATEGY_STOP: (id: string) => `${API_PATHS.V1}/strategies/${id}/stop`,
  STRATEGY_GENERATE: `${API_PATHS.V1}/strategy/generate`,
  STRATEGY_BACKTEST: (id: string) => `${API_PATHS.V1}/strategies/${id}/backtest`,

  // 回测数据 (Note: Qlib service might need separate client if on different port, but assuming gateway proxies it)
  BACKTEST_HISTORY: `${API_PATHS.V1}/qlib/history`,
  BACKTEST_RESULTS: `${API_PATHS.V1}/qlib/results`,

  // 股票查询
  STOCKS_SEARCH: `${API_PATHS.V1}/stocks/search`,
  STOCKS_REALTIME: `${API_PATHS.V1}/stocks/realtime`,

  // 投资组合
  PORTFOLIOS: `${API_PATHS.V1}/portfolios`,
  PORTFOLIO_DETAIL: (id: string) => `${API_PATHS.V1}/portfolios/${id}`,
  PORTFOLIO_POSITIONS: (id: string) => `${API_PATHS.V1}/portfolios/${id}/positions`,
  PORTFOLIO_PERFORMANCE: (id: string) => `${API_PATHS.V1}/portfolios/${id}/performance`,
  PORTFOLIOS_PERFORMANCE: `${API_PATHS.V1}/portfolios/performance`, // 用户级别/全组合绩效
  PORTFOLIO_DISTRIBUTION: `${API_PATHS.V1}/portfolios/distribution`, // 持仓分布

  // 模拟盘
  SIMULATION_ACCOUNT: `${API_PATHS.V1}/simulation/account`,
  SIMULATION_SETTINGS: `${API_PATHS.V1}/simulation/settings`,
  SIMULATION_RESET: `${API_PATHS.V1}/simulation/reset`,
  SIMULATION_TRADES: `${API_PATHS.V1}/simulation/trades`, // 模拟成交记录（sim_trades）
  SIMULATION_TRADES_STATS: `${API_PATHS.V1}/simulation/trades/stats/summary`, // 模拟成交统计

  // 交易/订单
  TRADES: `${API_PATHS.V1}/trades`,
  ORDERS: `${API_PATHS.V1}/orders`,
  ORDER_DETAIL: (id: string) => `${API_PATHS.V1}/orders/${id}`,
  ORDER_CANCEL: (id: string) => `${API_PATHS.V1}/orders/${id}/cancel`,
  TRADING_STATS: `${API_PATHS.V1}/trades/stats/summary`, // 交易统计

  // 持仓预警（持仓哨兵：分数跌破/盘中利空/名单新增）
  HOLDING_ALERTS: `${API_PATHS.V1}/trading/holding-alerts`,
  HOLDING_ALERT_DISMISS: (id: number) => `${API_PATHS.V1}/trading/holding-alerts/${id}/dismiss`,
  HOLDING_ALERT_EXECUTED: (id: number) => `${API_PATHS.V1}/trading/holding-alerts/${id}/executed`,
  HOLDING_ALERT_CONFIG: `${API_PATHS.V1}/trading/holding-alerts/config`,
  HOLDING_ALERT_STATUS: `${API_PATHS.V1}/trading/holding-alerts/status`,

  // 通知中心
  NOTIFICATIONS: `${API_PATHS.V1}/notifications`,
  NOTIFICATION_READ: (id: number) => `${API_PATHS.V1}/notifications/${id}/read`,
  NOTIFICATIONS_READ_ALL: `${API_PATHS.V1}/notifications/read-all`,
  NOTIFICATIONS_CLEAR: `${API_PATHS.V1}/notifications/clear`,

  // 健康检查
  HEALTH_CHECK: '/health'
};

// 响应状态码
export const HTTP_STATUS = {
  OK: 200,
  CREATED: 201,
  BAD_REQUEST: 400,
  UNAUTHORIZED: 401,
  FORBIDDEN: 403,
  NOT_FOUND: 404,
  INTERNAL_SERVER_ERROR: 500,
  SERVICE_UNAVAILABLE: 503
} as const;
