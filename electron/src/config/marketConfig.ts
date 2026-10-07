/**
 * 多市场配置
 *
 * 为各页面提供市场特定的默认值（Qlib region、股票池、基准指数等）
 */

import type { AppMarket } from '../store/slices/uiSlice';

export interface MarketConfig {
  /** 市场显示名称 */
  label: string;
  /** Qlib region 参数 */
  qlibRegion: string;
  /** Qlib provider URI */
  qlibProviderUri: string;
  /** 默认股票池名称 */
  defaultUniverse: string;
  /** 基准指数代码 */
  benchmark: string;
  /** 基准指数名称 */
  benchmarkName: string;
  /** 货币符号 */
  currency: string;
  /** 交易日历标识 */
  calendar: string;
  /** 后端 market adapter ID */
  adapterId: string;
}

export const MARKET_CONFIGS: Record<AppMarket, MarketConfig> = {
  CN: {
    label: 'A股',
    qlibRegion: 'cn',
    // 统一固定缓存目录（与后端 qlib_paths 解析一致）；后端对历史值会做归一化。
    qlibProviderUri: '/data/qlib/cn_data',
    defaultUniverse: 'csi300',
    benchmark: 'SH000300',
    benchmarkName: '沪深300',
    currency: 'CNY',
    calendar: 'SSE',
    adapterId: 'a_share',
  },
  HK: {
    label: '港股',
    qlibRegion: 'cn',
    qlibProviderUri: '/data/quanthk/.qlib_cache/hk_data',
    defaultUniverse: 'all',
    benchmark: 'HSI',
    benchmarkName: '恒生指数',
    currency: 'HKD',
    calendar: 'HKEX',
    adapterId: 'hong_kong',
  },
  US: {
    label: '美股',
    qlibRegion: 'us',
    qlibProviderUri: '/data/quantus/.qlib_cache/us_data',
    defaultUniverse: 'all',
    benchmark: 'SPX',
    benchmarkName: '标普500',
    currency: 'USD',
    calendar: 'NYSE',
    adapterId: 'us_stock',
  },
  CRYPTO: {
    label: '区块链',
    qlibRegion: 'cn',
    qlibProviderUri: '/data/quantbc/.qlib_cache/bc_data',
    defaultUniverse: 'all',
    benchmark: 'BTC',
    benchmarkName: '比特币',
    currency: 'USDT',
    calendar: '24/7',
    adapterId: 'crypto',
  },
  FUTURES: {
    label: '期货',
    qlibRegion: 'cn',
    qlibProviderUri: '/data/quantfutures/.qlib_cache/futures_data',
    defaultUniverse: 'all',
    benchmark: 'CL.FUT',
    benchmarkName: 'WTI原油',
    currency: 'USD',
    calendar: 'CME',
    adapterId: 'futures',
  },
};

/**
 * 自定义数据市场（CUSTOM）：训练页专用，不进全局 AppMarket。
 *
 * 它不是「市场」而是自传 parquet 的训练数据域（`data/quantcustom/6_ml_datasets/`，
 * 如 rd_mined 挖掘因子库）。行情/交易语义全部沿用 A 股口径（OHLCV 由同库
 * l1_factors 或库内行情补给），所以展示值镜像 CN；但绝不允许写进全局市场
 * 切换器（dashboard/交易时段等处没有它的语义）。详见 trainingUtils 的
 * TrainingMarket 类型与 resolveTrainingMarket。
 */
export const CUSTOM_DATA_MARKET_CONFIG: MarketConfig = {
  label: '自定义市场',
  qlibRegion: 'cn',
  // qlibProviderUri 对 CUSTOM 无实际消费方：CUSTOM 不走 qlib 二进制缓存
  // （后端 QlibDataBuilder.for_market 对未知市场直接抛错），保留此占位仅为
  // 满足 MarketConfig 形状；展示时请不要引用它。
  qlibProviderUri: '/data/quantcustom/.qlib_cache/custom_data',
  defaultUniverse: 'all',
  benchmark: 'SH000300',
  benchmarkName: '沪深300',
  currency: 'CNY',
  calendar: 'SSE',
  adapterId: 'a_share',
};

export function getMarketConfig(market: AppMarket | 'CUSTOM'): MarketConfig {
  if (market === 'CUSTOM') return CUSTOM_DATA_MARKET_CONFIG;
  return MARKET_CONFIGS[market] || MARKET_CONFIGS.CN;
}

// ---------------------------------------------------------------------------
// 交易时段（T-P3-07）：与后端 shared/market_sessions.py 同口径
// **时间一律为市场本地时钟**（A股=北京、美股=美东、港股=香港），
// 调度与校验由后端按市场时区换算；前端仅做展示与区间提示。
// ---------------------------------------------------------------------------

export interface MarketSessionConfig {
  /** enabled_sessions 词表 → [开始, 结束]（市场本地钟点） */
  sessions: Record<string, [string, string]>;
  /** 时段按钮显示名 */
  sessionLabels: Record<string, string>;
  /** 时钟口径提示（表单展示） */
  timezoneLabel: string;
}

export const MARKET_SESSIONS_UI: Record<AppMarket, MarketSessionConfig> = {
  CN: {
    sessions: {
      AM: ['09:30', '11:30'],
      PM: ['13:00', '15:00'],
      AFTER_HOURS: ['15:05', '15:30'],
    },
    sessionLabels: { AM: '上午', PM: '下午', AFTER_HOURS: '盘后' },
    timezoneLabel: '北京时间',
  },
  HK: {
    sessions: { AM: ['09:30', '12:00'], PM: ['13:00', '16:00'] },
    sessionLabels: { AM: '早盘', PM: '午盘' },
    timezoneLabel: '香港时间',
  },
  US: {
    sessions: { AM: ['09:30', '16:00'], AFTER_HOURS: ['16:00', '20:00'] },
    sessionLabels: { AM: '常规', AFTER_HOURS: '盘后' },
    timezoneLabel: '美东时间',
  },
  CRYPTO: {
    sessions: { AM: ['00:00', '23:59'] },
    sessionLabels: { AM: '全天' },
    timezoneLabel: '7×24',
  },
  FUTURES: {
    sessions: { AM: ['09:00', '15:00'], NIGHT: ['21:00', '02:30'] },
    sessionLabels: { AM: '日盘', NIGHT: '夜盘' },
    timezoneLabel: '北京时间',
  },
};

/** 会话 → 默认买卖时点（市场本地钟点；切换时段按钮时的预填） */
export const MARKET_SESSION_DEFAULTS: Record<AppMarket, Record<string, { sell_time: string; buy_time: string }>> = {
  CN: {
    // 与后端时段校验（A 股连续竞价 AM 09:30–11:30）一致；09:00 会被后端 400
    AM: { sell_time: '09:30', buy_time: '09:35' },
    PM: { sell_time: '14:30', buy_time: '14:45' },
    AFTER_HOURS: { sell_time: '15:05', buy_time: '15:10' },
  },
  HK: {
    AM: { sell_time: '11:45', buy_time: '11:50' },
    PM: { sell_time: '15:45', buy_time: '15:50' },
  },
  US: {
    AM: { sell_time: '15:45', buy_time: '15:50' },
    AFTER_HOURS: { sell_time: '19:50', buy_time: '19:58' },
  },
  CRYPTO: {
    AM: { sell_time: '12:00', buy_time: '12:05' },
  },
  FUTURES: {
    AM: { sell_time: '14:45', buy_time: '14:50' },
    NIGHT: { sell_time: '01:30', buy_time: '01:35' },
  },
};

export function getMarketSessions(market: AppMarket | string | undefined): MarketSessionConfig {
  const key = String(market || 'CN').toUpperCase() as AppMarket;
  return MARKET_SESSIONS_UI[key] || MARKET_SESSIONS_UI.CN;
}

export function getMarketSessionDefaults(market: AppMarket | string | undefined): Record<string, { sell_time: string; buy_time: string }> {
  const key = String(market || 'CN').toUpperCase() as AppMarket;
  return MARKET_SESSION_DEFAULTS[key] || MARKET_SESSION_DEFAULTS.CN;
}
