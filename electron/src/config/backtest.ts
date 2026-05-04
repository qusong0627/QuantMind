export const BACKTEST_CONFIG = {
  ENGINE: 'qlib' as const,
  SUPPORTED_ENGINES: ['qlib'] as const,

  QLIB: {
    PROVIDER_URI: 'db/qlib_data',
    REGION: 'cn',
    // 数据锁定范围 (2016-2025)
    DATA_START: '2016-01-01',
    DATA_END: '2025-12-31',
    // 默认回测范围 (2025年)
    DEFAULT_START: '2025-01-01',
    DEFAULT_END: '2025-12-31',

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
    ]
  }
} as const;
