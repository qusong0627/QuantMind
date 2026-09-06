/**
 * 动态默认回测日期（月度随时间推进）
 *
 * 规则（以 2026-08-31 为例）：
 *   - DEFAULT_END   = 今天 - 7 天（如 2026-08-24），确保日线数据已落盘稳定
 *   - DEFAULT_START = DEFAULT_END 前推 1 年（如 2025-08-24）
 */
function toISODate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

export function getDefaultBacktestEndDate(): string {
  const d = new Date();
  d.setDate(d.getDate() - 7);
  return toISODate(d);
}

export function getDefaultBacktestStartDate(): string {
  const d = new Date();
  d.setDate(d.getDate() - 7);
  d.setFullYear(d.getFullYear() - 1);
  return toISODate(d);
}

export const BACKTEST_CONFIG = {
  ENGINE: 'qlib' as const,
  SUPPORTED_ENGINES: ['qlib'] as const,

  QLIB: {
    PROVIDER_URI: 'db/qlib_data',
    REGION: 'cn',
    // 数据覆盖范围（起始固定；结束动态=一周前，与 QuantDB 每日更新节奏对齐）
    DATA_START: '2016-01-01',
    DATA_END: getDefaultBacktestEndDate(),
    // 默认回测范围（动态：结束=一周前，开始=结束前推1年）
    DEFAULT_START: getDefaultBacktestStartDate(),
    DEFAULT_END: getDefaultBacktestEndDate(),

    TRADING_DAYS: 2430,
    TOTAL_STOCKS: 6015,

    // 真实数据范围锁定
    AVAILABLE_RANGES: {
      FULL: { start: '2016-01-01', end: '2025-12-31', days: 2430 },
      YEAR_2023: { start: '2023-01-01', end: '2023-12-31', days: 242 },
      YEAR_2026: { start: '2026-01-01', end: '2026-12-31', days: 242 },
      YEAR_2025: { start: '2025-01-01', end: '2025-12-31', days: 243 },
    },

    // 交易费用费率配置（参考 docs/费用.md）
    //
    // 费用计算公式：
    //   买入费用 = 成交金额 × buy_cost
    //   卖出费用 = 成交金额 × sell_cost
    //
    // A股真实费用结构：
    //   券商佣金：0.025% (万2.5，买卖双向，最低5元) - 用户可调整
    //   过户费：  0.001% (万0.1，买卖双向) - 固定，随政策更新
    //   印花税：  0.05%  (万5，仅卖出) - 固定，随政策更新
    //
    // 综合费率：
    //   买入 = 佣金 + 过户费
    //   卖出 = 佣金 + 过户费 + 印花税
    //
    // 示例（佣金2.5，交易10万元）：
    //   买入费用 = 100,000 × 0.00026 = 26元
    //   卖出费用 = 100,000 × 0.00076 = 76元
    //   总费用 = 102元 (占本金0.102%)
    TRADING_COSTS: {
      // 固定费率（随政策调整，软件自动更新）
      TRANSFER_FEE_RATE: 0.00001,         // 过户费费率：0.001% (万0.1)
      STAMP_TAX_RATE: 0.0005,             // 印花税费率：0.05% (万5，仅卖出)

      // 用户可配置
      DEFAULT_COMMISSION_RATE: 0.00025,   // 默认券商佣金：0.025% (万2.5)
      MIN_COMMISSION: 5,                  // 最低佣金：5元/笔

      // 综合费率（自动计算）
      // buy_cost = commission + transfer_fee
      // sell_cost = commission + transfer_fee + stamp_tax
      calculateBuyCost: (commissionRate: number) => commissionRate + 0.00001,
      calculateSellCost: (commissionRate: number) => commissionRate + 0.00001 + 0.0005,
    },

    STRATEGIES: {
      TOPK_DROPOUT: {
        name: 'TopkDropoutStrategy',
        params: {
          topk: { default: 50, min: 10, max: 200 },
          n_drop: { default: 10, min: 1, max: 20 },
          drop_thresh: { default: 0.5, min: 0, max: 1 },
          buy_cost: { default: 0.00026, min: 0, max: 0.01 },  // 买入费率
          sell_cost: { default: 0.00076, min: 0, max: 0.01 }, // 卖出费率
        }
      }
    },

    BENCHMARKS: [
      { code: 'SH000300', name: '沪深300' },
      { code: 'SH000905', name: '中证500' },
      { code: 'SH000852', name: '中证1000' },
    ],

    MARKET_BENCHMARKS: {
      CN: [
        { code: 'SH000300', name: '沪深300' },
        { code: 'SH000905', name: '中证500' },
        { code: 'SH000852', name: '中证1000' },
      ],
      HK: [
        { code: 'HSI', name: '恒生指数' },
        { code: 'HSCEI', name: '恒生国企' },
        { code: 'HSTECH', name: '恒生科技' },
      ],
      US: [
        { code: 'SPX', name: '标普500' },
        { code: 'NDX', name: '纳斯达克100' },
        { code: 'DJI', name: '道琼斯30' },
      ],
      CRYPTO: [
        { code: 'BTC', name: '比特币' },
        { code: 'ETH', name: '以太坊' },
      ],
    } as Record<string, { code: string; name: string }[]>
  }
} as const;
