/** 行情回测的市场展示口径（唯一出处）。 */
import type { Freq, LabMarket } from './api';

export const FREQ_NAME: Record<Freq, string> = {
  '1d': '日线',
  '5m': '5分钟',
  '1m': '1分钟',
};

/** 分钟线时间轴的墙钟偏移（后端返回的是市场墙钟字符串，前端按 UTC 解析后加回偏移）。 */
export const MARKET_TZ_OFFSET: Record<LabMarket, number> = {
  cn: 8,
  hk: 8,
  us: 0,      // 美股暂无分钟线
  futures: 8,
  crypto: 0,
};

export const MARKET_NAME: Record<LabMarket, string> = {
  cn: 'A股',
  hk: '港股',
  us: '美股',
  futures: '期货',
  crypto: '区块链',
};

/** 数据源名（页头副标题用） */
export const MARKET_SOURCE: Record<LabMarket, string> = {
  cn: 'quantdb',
  hk: 'QuantHK',
  us: 'QuantUS',
  futures: 'QuantFutures',
  crypto: 'QuantBC',
};

export const DEFAULT_SYMBOL: Record<LabMarket, string> = {
  cn: '600309.SH',
  hk: '0700.HK',
  us: 'AAPL',
  futures: 'RB0.CN',
  crypto: 'BTCUSDT',
};

/** 搜索框提示（代码 / 名称示例），按市场给本市场的例子 */
export const SEARCH_HINT: Record<LabMarket, string> = {
  cn: '600309 / 万华化学',
  hk: '0700 / 腾讯控股',
  us: 'AAPL / 苹果',
  futures: 'RB0.CN / 螺纹钢',
  crypto: 'BTCUSDT',
};

/** 涨跌配色口径：A股/港股/期货红涨绿跌；美股/加密绿涨红跌。 */
export const MARKET_UP_COLOR: Record<LabMarket, string> = {
  cn: '#e2373b',
  hk: '#e2373b',
  futures: '#e2373b',
  us: '#1a9e5c',
  crypto: '#1a9e5c',
};

export const MARKET_DOWN_COLOR: Record<LabMarket, string> = {
  cn: '#1a9e5c',
  hk: '#1a9e5c',
  futures: '#1a9e5c',
  us: '#e2373b',
  crypto: '#e2373b',
};
