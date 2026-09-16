/**
 * Qlib 策略模板类型定义 + 轻量离线 Fallback
 *
 * 主数据源：后端 GET /api/v1/strategies/templates（动态加载）
 * 本文件中的 QLIB_STRATEGY_TEMPLATES 仅作离线 / 后端不可用时的兜底展示。
 * 如需动态获取最新模板，请使用 strategyTemplateService.getTemplates()。
 */

export interface StrategyTemplate {
  id: string;
  /** 展示排序，默认 Top-K 为 1 */
  sort?: number;
  name: string;
  description: string;
  category: 'basic' | 'advanced' | 'risk_control';
  difficulty: 'beginner' | 'intermediate' | 'advanced';
  code: string;
  params: {
    name: string;
    description: string;
    default: number | string;
    min?: number;
    max?: number;
  }[];
  /** 适用市场标记（a_share/hong_kong/us_stock/crypto）；缺省=历史 A 股模板 */
  markets?: string[];
  execution_defaults?: Record<string, unknown>;
  live_defaults?: Record<string, unknown>;
  live_config_tips?: string[];
  /** AI-IDE 虚拟目录/文件夹（如 "A股策略/01_宽基多因子"） */
  dir?: string;
}

/**
 * 按市场过滤模板（与服务端 template_applies_to_market 语义一致）。
 * - market 缺省或未知 → 不过滤（向后兼容全量）
 * - CN/A股：无 markets 标记的历史模板 + 显式 a_share
 * - HK/US/CRYPTO：仅含对应显式标记的模板
 */
export function filterTemplatesByMarket(
  templates: StrategyTemplate[],
  market?: string
): StrategyTemplate[] {
  const mkt = String(market || '').toUpperCase();
  if (mkt !== 'CN' && mkt !== 'US' && mkt !== 'HK' && mkt !== 'CRYPTO') {
    return templates;
  }
  const hasToken = (t: StrategyTemplate, token: string) =>
    (t.markets || []).some((m) => m.toUpperCase() === token);
  if (mkt === 'CN') {
    return templates.filter((t) => !t.markets || t.markets.length === 0 || hasToken(t, 'A_SHARE'));
  }
  const want = mkt === 'US' ? 'US_STOCK' : mkt === 'HK' ? 'HONG_KONG' : 'CRYPTO';
  return templates.filter((t) => hasToken(t, want));
}

/**
 * 轻量离线 fallback（仅保留最常用的 3 个入门策略）。
 * 完整模板列表由后端动态提供，优先通过 strategyTemplateService.getTemplates() 获取。
 */
export const QLIB_STRATEGY_TEMPLATES: StrategyTemplate[] = [
  {
    id: 'standard_topk',
    sort: 1,
    name: '默认 Top-K 选股策略',
    description: '最经典的量化选股逻辑。每日截面排名，精选最具潜力的 Top-K 标的，等权持仓。',
    category: 'basic',
    difficulty: 'beginner',
    code: `"""
默认 Top-K 选股策略 (Standard Top-K Strategy)
[Native] 核心逻辑：Top-K 选股 + 零换手强制约束
"""
STRATEGY_CONFIG = {
    "class": "RedisTopkStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 50,
        "n_drop": 10,
    }
}
`,
    params: [
      { name: 'topk', description: '持仓股票总数', default: 50, min: 5, max: 100 }
    ]
  },
  {
    id: 'StopLoss',
    name: '止损止盈策略',
    description: '在标准 TopK 选股基础上叠加硬性止损/止盈规则，一旦触发立即强制平仓。',
    category: 'risk_control',
    difficulty: 'beginner',
    code: `"""
止损止盈策略 (Stop-Loss / Take-Profit Strategy)
[Native] 核心逻辑：浮亏超过 stop_loss 或浮盈超过 take_profit 时强制平仓。
"""
STRATEGY_CONFIG = {
    "class": "RedisStopLossStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 30,
        "n_drop": 6,
        "stop_loss": -0.08,
        "take_profit": 0.15,
    }
}
`,
    params: [
      { name: 'topk', description: '选股数量', default: 30, min: 5, max: 100 },
      { name: 'stop_loss', description: '止损阈值 (如 -0.10 = -10%)', default: -0.10, min: -0.3, max: -0.01 },
      { name: 'take_profit', description: '止盈阈值 (如 0.20 = +20%)', default: 0.20, min: 0.05, max: 0.5 }
    ]
  },
  {
    id: 'risk_guard_topk',
    name: '大盘风控 Top-K 选股策略',
    description: '以 features_daily 的市值、估值、波动与趋势做硬过滤，并在大盘下行时自动降仓。',
    category: 'risk_control',
    difficulty: 'intermediate',
    code: `"""
大盘风控 Top-K 选股策略 (Risk-Guarded Top-K)
[Native] 核心逻辑：Top-K 选股 + 基本面硬过滤 + 大盘周期降仓。
"""
STRATEGY_CONFIG = {
    "class": "RedisRiskGuardTopkStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 50,
        "n_drop": 10,
        "rebalance_days": 3,
        "market_state_symbol": "SH000300",
        "market_state_window": 20,
        "industry_cap_ratio": 0.30,
        "f_total_mv_min": 2000000000,
        "f_float_mv_min": 500000000,
        "f_beta_20_max": 1.5,
        "f_vol_std_20_max": 0.06,
        "f_ma_gap_20_min": -0.12,
        "f_pe_ttm_min": 0,
        "f_pe_ttm_max": 80,
    }
}
`,
    params: [
      { name: 'topk', description: '持仓股票总数', default: 50, min: 5, max: 200 },
      { name: 'n_drop', description: '每期替换数量', default: 10, min: 0, max: 200 },
      { name: 'rebalance_days', description: '调仓周期 (天)', default: 3, min: 1, max: 60 },
      { name: 'market_state_symbol', description: '市场状态参考指数', default: 'SH000300' },
      { name: 'market_state_window', description: '大盘状态判定窗口 (交易日)', default: 20, min: 5, max: 120 },
      { name: 'industry_cap_ratio', description: '单行业持仓上限占比', default: 0.3, min: 0.1, max: 0.6 },
      { name: 'f_total_mv_min', description: '总市值下限 (元)', default: 2000000000, min: 100000000, max: 100000000000 },
      { name: 'f_float_mv_min', description: '流通市值下限 (元)', default: 500000000, min: 100000000, max: 100000000000 },
      { name: 'f_beta_20_max', description: '20日 Beta 上限', default: 1.5, min: 0.5, max: 3 },
      { name: 'f_vol_std_20_max', description: '20日收益波动率上限', default: 0.06, min: 0.01, max: 0.2 },
      { name: 'f_ma_gap_20_min', description: '相对20日均线偏离下限', default: -0.12, min: -0.5, max: 0.2 },
      { name: 'f_pe_ttm_max', description: 'PE（TTM）上限', default: 80, min: 1, max: 300 },
    ]
  },
  {
    id: 'alpha_cross_section',
    name: '截面 Alpha 预测策略',
    description: '根据预测分自动分配资金权重，分高者重仓。',
    category: 'advanced',
    difficulty: 'intermediate',
    code: `"""
截面 Alpha 预测策略 (Cross-sectional Alpha)
[Native] 核心逻辑：按模型预测分比例进行权重分配。
"""
STRATEGY_CONFIG = {
    "class": "RedisWeightStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 50,
        "min_score": 0.0,
        "max_weight": 0.05,
    }
}
`,
    params: [
      { name: 'topk', description: '参与权重的标的数量', default: 50, min: 10, max: 200 },
      { name: 'max_weight', description: '单票持仓上限 (0~1)', default: 0.05, min: 0.01, max: 0.2 }
    ]
  }
];

/**
 * 按分类获取 fallback 模板
 */
export function getTemplatesByCategory(category: StrategyTemplate['category']): StrategyTemplate[] {
  return QLIB_STRATEGY_TEMPLATES.filter(t => t.category === category);
}

/**
 * 按难度获取 fallback 模板
 */
export function getTemplatesByDifficulty(difficulty: StrategyTemplate['difficulty']): StrategyTemplate[] {
  return QLIB_STRATEGY_TEMPLATES.filter(t => t.difficulty === difficulty);
}

/**
 * 按 ID 查询 fallback 模板
 */
export function getTemplateById(id: string): StrategyTemplate | undefined {
  return QLIB_STRATEGY_TEMPLATES.find(t => t.id === id);
}

/** 无 dir 的内置模板归入此分组，并固定排在模板选择器最前。 */
export const GENERAL_STRATEGY_DIR = '通用策略';

const ASHARE_ID = /^as\d{2}(_|$)/i;
const HK_ID = /^hk_/i;
const MINIBT_ID = /^minibt_/i;

export function resolveTemplateDir(template: StrategyTemplate): string {
  const explicit = template.dir?.trim();
  if (explicit) return explicit;
  if (ASHARE_ID.test(template.id)) return 'A股策略/未分类';
  if (HK_ID.test(template.id)) return '港股策略';
  if (MINIBT_ID.test(template.id)) return 'minibt策略';
  return GENERAL_STRATEGY_DIR;
}

function compareTemplates(a: StrategyTemplate, b: StrategyTemplate): number {
  const rank = (template: StrategyTemplate) => {
    const sort = Number(template.sort);
    return Number.isFinite(sort) && sort > 0 ? sort : 100;
  };
  const diff = rank(a) - rank(b);
  if (diff !== 0) return diff;
  return a.id.localeCompare(b.id);
}

export function groupStrategyTemplatesByDir(
  templates: StrategyTemplate[],
): Array<[string, StrategyTemplate[]]> {
  const map = new Map<string, StrategyTemplate[]>();
  for (const template of templates) {
    const key = resolveTemplateDir(template);
    const bucket = map.get(key);
    if (bucket) {
      bucket.push(template);
    } else {
      map.set(key, [template]);
    }
  }
  const groups = Array.from(map.entries()).map(([label, items]) => [
    label,
    [...items].sort(compareTemplates),
  ] as [string, StrategyTemplate[]]);
  groups.sort(([left], [right]) => {
    if (left === GENERAL_STRATEGY_DIR) return -1;
    if (right === GENERAL_STRATEGY_DIR) return 1;
    return 0;
  });
  return groups;
}

export type StrategyTemplateSection = {
  label: string;
  items: StrategyTemplate[];
  children: Array<{ label: string; items: StrategyTemplate[] }>;
};

/** 通用策略置顶；A股/港股等按第一级目录收拢，避免 10 个子目录再加通用变成 11 组。 */
export function groupStrategyTemplateSections(
  templates: StrategyTemplate[],
): StrategyTemplateSection[] {
  const topMap = new Map<string, StrategyTemplateSection>();
  for (const [dir, items] of groupStrategyTemplatesByDir(templates)) {
    const parts = dir.split('/').map((part) => part.trim()).filter(Boolean);
    const topLabel = parts[0] || GENERAL_STRATEGY_DIR;
    const subLabel = parts.slice(1).join('/');
    if (!topMap.has(topLabel)) {
      topMap.set(topLabel, { label: topLabel, items: [], children: [] });
    }
    const section = topMap.get(topLabel)!;
    if (subLabel) {
      const existing = section.children.find((child) => child.label === subLabel);
      if (existing) {
        existing.items.push(...items);
      } else {
        section.children.push({ label: subLabel, items: [...items] });
      }
    } else {
      section.items.push(...items);
    }
  }
  const sections = Array.from(topMap.values());
  sections.sort((left, right) => {
    if (left.label === GENERAL_STRATEGY_DIR) return -1;
    if (right.label === GENERAL_STRATEGY_DIR) return 1;
    return 0;
  });
  return sections;
}
