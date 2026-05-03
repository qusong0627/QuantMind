export const FACTOR_DICTIONARY_VERSION = '2.0.0';

export const FACTORS: Array<{ key: string; label: string; category: string; unit?: string }> = [
  // ============ 估值因子 ============
  { key: 'market_cap', label: '总市值', category: '估值因子', unit: '亿' },
  { key: 'float_mv', label: '流通市值', category: '估值因子', unit: '亿' },
  { key: 'pe', label: '市盈率PE(TTM)', category: '估值因子' },
  { key: 'pb', label: '市净率PB', category: '估值因子' },
  { key: 'roe', label: '净资产收益率ROE', category: '估值因子', unit: '%' },

  // ============ 价格因子 ============
  { key: 'close', label: '收盘价', category: '价格因子', unit: '元' },
  { key: 'pct_change', label: '当日涨跌幅', category: '价格因子', unit: '%' },
  { key: 'turnover_rate', label: '换手率', category: '价格因子', unit: '%' },

  // ============ 均线因子 ============
  { key: 'ma5', label: '5日均线', category: '均线因子', unit: '元' },
  { key: 'ma10', label: '10日均线', category: '均线因子', unit: '元' },
  { key: 'ma20', label: '20日均线', category: '均线因子', unit: '元' },
  { key: 'ma60', label: '60日均线', category: '均线因子', unit: '元' },
  { key: 'ma_gap_5', label: '5日均线偏离度', category: '均线因子', unit: '%' },
  { key: 'ma_gap_10', label: '10日均线偏离度', category: '均线因子', unit: '%' },
  { key: 'ma_gap_20', label: '20日均线偏离度', category: '均线因子', unit: '%' },

  // ============ 技术指标 ============
  { key: 'rsi_6', label: 'RSI(6)', category: '技术指标' },
  { key: 'rsi_14', label: 'RSI(14)', category: '技术指标' },
  { key: 'kdj_k', label: 'KDJ-K值', category: '技术指标' },
  { key: 'kdj_d', label: 'KDJ-D值', category: '技术指标' },
  { key: 'kdj_j', label: 'KDJ-J值', category: '技术指标' },
  { key: 'macd_dif', label: 'MACD-DIF', category: '技术指标' },
  { key: 'macd_dea', label: 'MACD-DEA', category: '技术指标' },
  { key: 'macd_hist', label: 'MACD柱', category: '技术指标' },

  // ============ 收益率因子 ============
  { key: 'return_1d', label: '近1日收益率', category: '收益率因子', unit: '%' },
  { key: 'return_3d', label: '近3日收益率', category: '收益率因子', unit: '%' },
  { key: 'return_5d', label: '近5日收益率', category: '收益率因子', unit: '%' },
  { key: 'return_10d', label: '近10日收益率', category: '收益率因子', unit: '%' },
  { key: 'return_20d', label: '近20日收益率', category: '收益率因子', unit: '%' },
  { key: 'return_60d', label: '近60日收益率', category: '收益率因子', unit: '%' },

  // ============ 波动率因子 ============
  { key: 'vol_std_5', label: '5日波动率', category: '波动率因子', unit: '%' },
  { key: 'vol_std_20', label: '20日波动率', category: '波动率因子', unit: '%' },
  { key: 'vol_std_60', label: '60日波动率', category: '波动率因子', unit: '%' },
  { key: 'vol_atr_14', label: '14日ATR', category: '波动率因子' },
  { key: 'beta_20', label: '20日Beta', category: '波动率因子' },

  // ============ 量能因子 ============
  { key: 'volume', label: '成交量', category: '量能因子' },
  { key: 'amount', label: '成交额', category: '量能因子', unit: '亿' },
  { key: 'volume_ratio_5', label: '5日量比', category: '量能因子' },
  { key: 'volume_ratio_20', label: '20日量比', category: '量能因子' },
  { key: 'volume_ma_5', label: '5日均量', category: '量能因子' },

  // ============ 指数成分 ============
  { key: 'idx_hs300', label: '沪深300成分', category: '指数成分' },
  { key: 'idx_zz500', label: '中证500成分', category: '指数成分' },
  { key: 'idx_zz1000', label: '中证1000成分', category: '指数成分' },
  { key: 'idx_chinext', label: '创业板指成分', category: '指数成分' },
  { key: 'idx_margin', label: '融资融券标的', category: '指数成分' },

  // ============ 概念标签 ============
  { key: 'concept_ai', label: 'AI概念', category: '概念标签' },
  { key: 'concept_chip', label: '芯片概念', category: '概念标签' },
  { key: 'concept_new_energy', label: '新能源概念', category: '概念标签' },
  { key: 'concept_ev', label: '电动车概念', category: '概念标签' },
  { key: 'concept_pv', label: '光伏概念', category: '概念标签' },
  { key: 'concept_lithium', label: '锂电概念', category: '概念标签' },
  { key: 'concept_semiconductor', label: '半导体概念', category: '概念标签' },
  { key: 'concept_military', label: '军工概念', category: '概念标签' },
  { key: 'concept_medical', label: '医药概念', category: '概念标签' },
  { key: 'concept_cyber', label: '网络安全概念', category: '概念标签' },
  { key: 'concept_fintech', label: '金融科技概念', category: '概念标签' },
  { key: 'concept_consumption', label: '消费概念', category: '概念标签' },
  { key: 'concept_real_estate', label: '地产概念', category: '概念标签' },
  { key: 'concept_infrastructure', label: '基建概念', category: '概念标签' },
  { key: 'concept_state_owned', label: '国企改革概念', category: '概念标签' },

  // ============ 其他因子 ============
  { key: 'is_st', label: 'ST标记', category: '其他' },
  { key: 'listed_days', label: '上市天数', category: '其他' },
  { key: 'limit_up_today', label: '当日涨停', category: '其他' },
  { key: 'limit_down_today', label: '当日跌停', category: '其他' },
  { key: 'consecutive_limit_up_days', label: '连续涨停天数', category: '其他' },
  { key: 'volume_trend_3d', label: '3日量能增强', category: '其他' },
  { key: 'industry', label: '所属行业', category: '其他' },
  { key: 'listing_market', label: '上市板块', category: '其他' },
];

// 因子同义词映射（用于自然语言解析）
export const SYNONYMS: Record<string, string> = {
  // 估值
  市值: 'market_cap',
  总市值: 'market_cap',
  流通市值: 'float_mv',
  PE: 'pe',
  PE_TTM: 'pe',
  市盈率: 'pe',
  PB: 'pb',
  市净率: 'pb',
  ROE: 'roe',
  净资产收益率: 'roe',

  // 价格
  收盘价: 'close',
  涨跌幅: 'pct_change',
  涨跌: 'pct_change',
  换手率: 'turnover_rate',

  // 均线
  MA5: 'ma5',
  MA10: 'ma10',
  MA20: 'ma20',
  MA60: 'ma60',
  五日线: 'ma5',
  十日线: 'ma10',
  二十日线: 'ma20',
  六十日线: 'ma60',

  // 技术指标
  RSI: 'rsi_14',
  RSI6: 'rsi_6',
  RSI14: 'rsi_14',
  KDJ: 'kdj_k',
  MACD: 'macd_hist',

  // 收益率
  一日收益: 'return_1d',
  三日收益: 'return_3d',
  五日收益: 'return_5d',
  六十日收益: 'return_60d',
  近期收益: 'return_5d',

  // 波动率
  波动率: 'vol_std_20',
  二十日波动率: 'vol_std_20',
  六十日波动率: 'vol_std_60',
  ATR: 'vol_atr_14',
  Beta: 'beta_20',

  // 量能
  成交量: 'volume',
  成交额: 'amount',
  量比: 'volume_ratio_5',

  // 指数
  沪深300: 'idx_hs300',
  中证500: 'idx_zz500',
  中证1000: 'idx_zz1000',
  创业板: 'idx_chinext',
  创业板指: 'idx_chinext',
  两融: 'idx_margin',
  融资融券: 'idx_margin',

  // 概念
  AI: 'concept_ai',
  人工智能: 'concept_ai',
  芯片: 'concept_chip',
  新能源: 'concept_new_energy',
  电动车: 'concept_ev',
  光伏: 'concept_pv',
  锂电: 'concept_lithium',
  半导体: 'concept_semiconductor',
  军工: 'concept_military',
  医药: 'concept_medical',
  网络安全: 'concept_cyber',
  金融科技: 'concept_fintech',
  消费: 'concept_consumption',
  地产: 'concept_real_estate',
  基建: 'concept_infrastructure',
  国企: 'concept_state_owned',

  // 其他
  ST: 'is_st',
  涨停: 'limit_up_today',
  跌停: 'limit_down_today',
  量能增强: 'volume_trend_3d',
  三日量能增强: 'volume_trend_3d',
  行业: 'industry',
  所属行业: 'industry',
  板块: 'listing_market',
  上市板块: 'listing_market',
};

// 按类别分组的因子（用于UI展示）
export const FACTORS_BY_CATEGORY: Record<string, Array<{ key: string; label: string; unit?: string }>> = {
  '估值因子': FACTORS.filter(f => f.category === '估值因子').map(f => ({ key: f.key, label: f.label, unit: f.unit })),
  '价格因子': FACTORS.filter(f => f.category === '价格因子').map(f => ({ key: f.key, label: f.label, unit: f.unit })),
  '均线因子': FACTORS.filter(f => f.category === '均线因子').map(f => ({ key: f.key, label: f.label, unit: f.unit })),
  '技术指标': FACTORS.filter(f => f.category === '技术指标').map(f => ({ key: f.key, label: f.label, unit: f.unit })),
  '收益率因子': FACTORS.filter(f => f.category === '收益率因子').map(f => ({ key: f.key, label: f.label, unit: f.unit })),
  '波动率因子': FACTORS.filter(f => f.category === '波动率因子').map(f => ({ key: f.key, label: f.label, unit: f.unit })),
  '量能因子': FACTORS.filter(f => f.category === '量能因子').map(f => ({ key: f.key, label: f.label, unit: f.unit })),
  '指数成分': FACTORS.filter(f => f.category === '指数成分').map(f => ({ key: f.key, label: f.label })),
  '概念标签': FACTORS.filter(f => f.category === '概念标签').map(f => ({ key: f.key, label: f.label })),
  '其他': FACTORS.filter(f => f.category === '其他').map(f => ({ key: f.key, label: f.label })),
};
