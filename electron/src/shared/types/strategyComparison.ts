/**
 * 策略对比类型定义
 * Strategy Comparison Types
 *
 * 支持多策略横向对比分析
 *
 * @author QuantMind Team
 * @date 2025-12-02
 */

/**
 * 策略来源
 */
export type StrategySource = 'personal' | 'community';

/**
 * 策略对比项
 */
export interface StrategyComparisonItem {
  strategy_id: string;
  strategy_name: string;
  strategy_type: string;
  source: StrategySource;
  source_label: string;          // "个人策略" | "社区策略"
  created_at: string;
  author?: string;               // 如果是社区策略，显示作者

  // 基础信息
  basic_info: {
    market: string[];
    style: string;
    tags: string[];
    risk_level?: string;
  };

  // 绩效指标
  performance: {
    total_return: number;         // 总收益
    annual_return: number;        // 年化收益率
    monthly_avg_return: number;   // 月均收益
    max_drawdown: number;         // 最大回撤
    volatility: number;           // 波动率
    sharpe_ratio: number;         // 夏普比率
    sortino_ratio: number;        // 索提诺比率
    calmar_ratio: number;         // 卡玛比率
    downside_risk?: number;       // 下行风险
  };

  // 交易统计
  trading_stats: {
    total_trades: number;          // 总交易次数
    win_rate: number;              // 胜率
    profit_loss_ratio: number;     // 盈亏比
    avg_holding_period: number;    // 平均持仓时间（天）
    max_consecutive_wins: number;  // 最大连续盈利
    max_consecutive_losses: number;// 最大连续亏损
    avg_win_amount?: number;       // 平均盈利金额
    avg_loss_amount?: number;      // 平均亏损金额
  };

  // 收益曲线数据
  equity_curve: {
    dates: string[];
    returns: number[];             // 累计收益率
  };
}

/**
 * 策略对比数据
 */
export interface StrategyComparison {
  strategies: StrategyComparisonItem[];
  comparison_id?: string;
  comparison_date: string;
  user_id: string;
}

/**
 * 最佳表现者
 */
export interface BestPerformers {
  highest_return: string;          // 最高收益的策略ID
  lowest_drawdown: string;         // 最小回撤的策略ID
  best_sharpe: string;             // 最佳夏普比率的策略ID
  best_win_rate: string;           // 最高胜率的策略ID
  lowest_volatility: string;       // 最低波动率的策略ID
  best_calmar: string;             // 最佳卡玛比率的策略ID
}

/**
 * 策略排名
 */
export interface StrategyRanking {
  strategy_id: string;
  strategy_name: string;
  rank: number;                    // 排名（1-N）
  score: number;                   // 综合评分（0-100）
  strengths: string[];             // 优势列表
  weaknesses: string[];            // 劣势列表
  recommendation: string;          // 推荐建议
}

/**
 * 对比结果
 */
export interface ComparisonResult {
  best_performers: BestPerformers;
  rankings: StrategyRanking[];
  recommendations: string[];        // 智能建议列表
  optimal_allocation?: {            // 最优配置建议
    strategy_id: string;
    weight: number;                 // 权重百分比
  }[];
}

/**
 * 对比维度
 */
export enum ComparisonDimension {
  BASIC_INFO = 'basic_info',       // 基础信息
  RETURN = 'return',               // 收益指标
  RISK = 'risk',                   // 风险指标
  RISK_ADJUSTED = 'risk_adjusted', // 风险调整收益
  TRADING = 'trading',             // 交易统计
  ALL = 'all',                     // 全部维度
}

/**
 * 对比指标配置
 */
export interface ComparisonMetric {
  key: string;
  label: string;
  dimension: ComparisonDimension;
  format: 'number' | 'percent' | 'currency' | 'date';
  precision?: number;
  higher_is_better: boolean;       // true=越高越好，false=越低越好
  weight?: number;                 // 在综合评分中的权重
}

/**
 * 默认对比指标配置
 */
export const DEFAULT_COMPARISON_METRICS: ComparisonMetric[] = [
  // 收益指标
  {
    key: 'performance.annual_return',
    label: '年化收益率',
    dimension: ComparisonDimension.RETURN,
    format: 'percent',
    precision: 2,
    higher_is_better: true,
    weight: 0.25,
  },
  {
    key: 'performance.total_return',
    label: '累计收益',
    dimension: ComparisonDimension.RETURN,
    format: 'percent',
    precision: 2,
    higher_is_better: true,
    weight: 0.15,
  },

  // 风险指标
  {
    key: 'performance.max_drawdown',
    label: '最大回撤',
    dimension: ComparisonDimension.RISK,
    format: 'percent',
    precision: 2,
    higher_is_better: false,
    weight: 0.20,
  },
  {
    key: 'performance.volatility',
    label: '波动率',
    dimension: ComparisonDimension.RISK,
    format: 'percent',
    precision: 2,
    higher_is_better: false,
    weight: 0.10,
  },

  // 风险调整收益
  {
    key: 'performance.sharpe_ratio',
    label: '夏普比率',
    dimension: ComparisonDimension.RISK_ADJUSTED,
    format: 'number',
    precision: 2,
    higher_is_better: true,
    weight: 0.15,
  },
  {
    key: 'performance.calmar_ratio',
    label: '卡玛比率',
    dimension: ComparisonDimension.RISK_ADJUSTED,
    format: 'number',
    precision: 2,
    higher_is_better: true,
    weight: 0.10,
  },

  // 交易统计
  {
    key: 'trading_stats.win_rate',
    label: '胜率',
    dimension: ComparisonDimension.TRADING,
    format: 'percent',
    precision: 2,
    higher_is_better: true,
    weight: 0.05,
  },
];

/**
 * 工具函数：获取嵌套属性值
 */
export function getNestedValue(obj: any, path: string): any {
  return path.split('.').reduce((current, key) => current?.[key], obj);
}

/**
 * 工具函数：格式化指标值
 */
export function formatMetricValue(
  value: number,
  format: 'number' | 'percent' | 'currency' | 'date',
  precision: number = 2
): string {
  if (value === null || value === undefined || isNaN(value)) {
    return '-';
  }

  switch (format) {
    case 'percent':
      return `${value.toFixed(precision)}%`;
    case 'currency':
      return `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: precision, maximumFractionDigits: precision })}`;
    case 'number':
      return value.toFixed(precision);
    case 'date':
      return new Date(value).toLocaleDateString('zh-CN');
    default:
      return String(value);
  }
}

/**
 * 工具函数：判断是否为最佳值
 */
export function isBestValue(
  value: number,
  allValues: number[],
  higherIsBetter: boolean,
  metricKey: string = ''
): boolean {
  if (allValues.length === 0) return false;

  const normalizedKey = metricKey.toLowerCase();
  if (normalizedKey.includes('max_drawdown') || normalizedKey.includes('drawdown')) {
    return Math.abs(value) === Math.min(...allValues.map((v) => Math.abs(v)));
  }

  if (higherIsBetter) {
    return value === Math.max(...allValues);
  } else {
    return value === Math.min(...allValues);
  }
}

/**
 * 工具函数：计算综合评分
 */
export function calculateComprehensiveScore(
  strategy: StrategyComparisonItem,
  metrics: ComparisonMetric[] = DEFAULT_COMPARISON_METRICS
): number {
  let totalScore = 0;
  let totalWeight = 0;

  metrics.forEach(metric => {
    if (!metric.weight) return;

    const value = getNestedValue(strategy, metric.key);
    if (value === null || value === undefined) return;

    // 标准化到0-100分
    let normalizedScore = 0;

    // 这里简化处理，实际应该基于所有策略的值域来标准化
    if (metric.higher_is_better) {
      normalizedScore = Math.max(0, Math.min(100, value * 100));
    } else {
      normalizedScore = Math.max(0, Math.min(100, (1 - Math.abs(value)) * 100));
    }

    totalScore += normalizedScore * metric.weight;
    totalWeight += metric.weight;
  });

  return totalWeight > 0 ? totalScore / totalWeight : 0;
}

/**
 * 工具函数：生成比较结论
 *
 * 合规约束（开源发行）：只陈述本页口径下的比较结果，不得出现「建议优先考虑」
 * 「适合稳健型投资者」「推荐组合配置」这类**投资建议 / 适当性结论** —— 本工具
 * 不是持牌投顾，无权对投资者做适当性判断。
 */
export function generateRecommendations(
  strategies: StrategyComparisonItem[],
  result: ComparisonResult
): string[] {
  const recommendations: string[] = [];

  if (strategies.length === 0) return recommendations;

  // 找出综合排名第一的策略
  const topStrategy = result.rankings[0];
  if (topStrategy) {
    recommendations.push(
      `${topStrategy.strategy_name}综合评分最高（${topStrategy.score.toFixed(1)}分，按本页比较口径计算）。`
    );
  }

  // 风险收益比：只陈述筛选口径，不落到投资者适当性结论上
  const highReturnLowRisk = strategies.filter(s =>
    s.performance.annual_return > 10 && Math.abs(s.performance.max_drawdown) < 15
  );

  if (highReturnLowRisk.length > 0) {
    recommendations.push(
      `${highReturnLowRisk.map(s => s.strategy_name).join('、')}在本页口径下年化收益高于10%且最大回撤小于15%（历史样本区间，不代表未来）。`
    );
  }

  // 组合权重（不是「推荐配置」：配置决策由用户自己做）
  if (strategies.length >= 2 && result.optimal_allocation) {
    const allocation = result.optimal_allocation
      .map(a => {
        const strategy = strategies.find(s => s.strategy_id === a.strategy_id);
        return `${strategy?.strategy_name}(${a.weight}%)`;
      })
      .join(' + ');

    recommendations.push(`组合权重（按比较口径计算）：${allocation}`);
  }

  return recommendations;
}
