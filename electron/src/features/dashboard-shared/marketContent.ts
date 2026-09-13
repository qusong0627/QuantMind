/**
 * ★ 内容分层唯一声明处：每个市场 × 每个框显示什么。
 *
 * 维护约定（改这里之前先读 features/dashboard-shared/README.md）：
 * - 市场之间的差异**只写在本文件**，卡片组件里禁止 `if (market === 'HK')` 这类分支
 * - 空态文案要说清「为什么空」，不要写「暂无数据」四个字了事
 * - 数据源标注必须写真实来源（QuantDB / QuantHK / QuantUS / QuantFutures / QuantBC）
 */

import type { AppMarket } from '../../store/slices/uiSlice';
import type { BoxContent, BoxId, MarketContent } from './types';

const BOX_ORDER: BoxId[] = ['market', 'fund', 'trade', 'strategy', 'charts', 'notify'];

/**
 * 非 A 股市场的智能图表面板开关。
 *
 * 原因（后端现状）：模拟盘日快照 `/simulation/snapshots/daily` 与组合绩效
 * `/portfolios/performance` **都没有市场维度**（表里没有 market 列），
 * 直接展示会把 A 股曲线挂在港股/美股格子上 —— 与本轮要修的问题同类。
 * 因此这些子面板先按市场关闭，只保留有市场口径的成交统计；
 * 后端补上市场维度（方案 B2）后，把这里改回 true 即可。
 */
const NON_CN_CHARTS_PANELS: Partial<BoxContent> = {
  panels: { portfolioSeries: false, positionRatio: false },
  panelNote: '该市场暂只统计成交；资金曲线与持仓分布待后端补齐市场维度',
};

interface MarketSeed {
  market: AppMarket;
  label: string;
  accent: string;
  /** 模拟盘默认开通资金（本币）：空态「一键开通」时预填 */
  defaultSeedCash: number;
  /** 数据源与货币 */
  source: string;
  currency: string;
  /** 各框的市场专属文案覆盖（缺省用通用模板） */
  boxes?: Partial<Record<BoxId, Partial<BoxContent>>>;
}

/** 通用空态模板：以「市场名 + 该框主体」拼出可读的说明 */
function defaultBoxes(seed: MarketSeed): Record<BoxId, BoxContent> {
  const { label, source, currency, market } = seed;
  const isSimulationCapable = market !== 'CRYPTO';
  return {
    market: {
      title: '{label}概览',
      source,
      emptyTitle: `${label}行情暂不可用`,
      emptyHint: `本地 ${source} 数据未就绪，可到「数据管理」页触发同步`,
    },
    fund: {
      title: '资金概览 ({label}/{mode})',
      source: `模拟账户（${source} 口径）`,
      currency,
      emptyTitle: `${label}模拟盘未开通`,
      emptyHint: `当前市场还没有模拟账户（不会显示其它市场的账户余额）`,
      canOpenAccount: isSimulationCapable,
    },
    trade: {
      title: `实时交易记录 (${label})`,
      source: '模拟撮合成交',
      currency,
      emptyTitle: `${label}暂无成交`,
      emptyHint: `只统计 ${label} 标的的模拟成交；其它市场的成交不会混入`,
      canOpenAccount: isSimulationCapable,
    },
    strategy: {
      title: `策略监控 (${label})`,
      source: '策略库（按市场过滤）',
      emptyTitle: `${label}暂无策略`,
      emptyHint: `策略库中没有 ${label} 市场的策略；A 股策略不会在这里顶替显示`,
    },
    charts: {
      title: `智能图表 (${label})`,
      source: '成交统计 + 资金曲线',
      currency,
      emptyTitle: `${label}暂无统计`,
      emptyHint: `统计口径为 ${label} 市场；该市场暂无成交或资金快照`,
    },
    notify: {
      title: `信息通知 (${label})`,
      source: '站内通知（按市场分流）',
      emptyTitle: `${label}暂无通知`,
      emptyHint: `只显示与 ${label} 相关的通知，全局通知在下方单列`,
    },
  };
}

/** 市场种子表：一处声明，规格由它派生 */
const SEEDS: Record<AppMarket, MarketSeed> = {
  CN: {
    market: 'CN',
    label: 'A股',
    accent: 'bg-red-50 text-red-700 border-red-200',
    defaultSeedCash: 1_000_000,
    source: 'QuantDB',
    currency: '¥',
    boxes: {
      strategy: { emptyHint: '策略库中没有 A 股策略（历史无 market 字段的策略按 A 股计）' },
    },
  },
  HK: {
    market: 'HK',
    label: '港股',
    accent: 'bg-orange-50 text-orange-700 border-orange-200',
    defaultSeedCash: 1_000_000,
    source: 'QuantHK',
    // 港股模拟盘以港币计价；开通时按本币预填
    currency: 'HK$',
    boxes: {
      trade: { emptyHint: '只统计港股标的（xxxxx.HK）的模拟成交；A 股成交不会混入' },
      market: { emptyHint: '本地 QuantHK 数据未就绪，可到「数据管理 → 港股」触发同步' },
      charts: NON_CN_CHARTS_PANELS,
    },
  },
  US: {
    market: 'US',
    label: '美股',
    accent: 'bg-blue-50 text-blue-700 border-blue-200',
    defaultSeedCash: 1_000_000,
    source: 'QuantUS',
    currency: '$',
    boxes: {
      trade: { emptyHint: '只统计美股 ticker 的模拟成交；A 股/港股成交不会混入' },
      strategy: { emptyHint: '策略库中暂无美股策略（美股标的池约 517 只，非全市场）' },
      charts: NON_CN_CHARTS_PANELS,
    },
  },
  FUTURES: {
    market: 'FUTURES',
    label: '期货',
    accent: 'bg-amber-50 text-amber-700 border-amber-200',
    defaultSeedCash: 1_000_000,
    source: 'QuantFutures',
    // 国内品种以人民币计价（国际品种为美元），模拟盘按人民币开通
    currency: '¥',
    boxes: {
      trade: { emptyHint: '只统计期货合约（.CN / .FUT）的模拟成交' },
      charts: NON_CN_CHARTS_PANELS,
    },
  },
  CRYPTO: {
    market: 'CRYPTO',
    label: '区块链',
    accent: 'bg-purple-50 text-purple-700 border-purple-200',
    defaultSeedCash: 100_000,
    source: 'QuantBC',
    currency: 'USDT',
    boxes: {
      fund: { emptyHint: '区块链市场当前未开放模拟盘（生产默认隐藏该市场）' },
      trade: { canOpenAccount: false, emptyHint: '区块链市场当前未开放模拟盘' },
      charts: NON_CN_CHARTS_PANELS,
    },
  },
};

function buildMarketContent(seed: MarketSeed): MarketContent {
  const boxes = defaultBoxes(seed);
  const overrides = seed.boxes || {};
  for (const id of BOX_ORDER) {
    const patch = overrides[id];
    if (patch) boxes[id] = { ...boxes[id], ...patch };
  }
  return {
    market: seed.market,
    label: seed.label,
    accent: seed.accent,
    defaultSeedCash: seed.defaultSeedCash,
    boxes,
  };
}

export const MARKET_CONTENT: Record<AppMarket, MarketContent> = Object.fromEntries(
  (Object.keys(SEEDS) as AppMarket[]).map((m) => [m, buildMarketContent(SEEDS[m])]),
) as Record<AppMarket, MarketContent>;

/**
 * 标题模板渲染：把 `{label}` / `{mode}` 占位符替换成市场名与交易模式。
 *
 * 未提供的占位符按空串处理；模板里没有的键忽略（避免各卡片自己拼标题导致口径漂移）。
 */
export function formatBoxTitle(
  content: Pick<BoxContent, 'title'>,
  vars: { label?: string; mode?: string } = {},
): string {
  return String(content?.title || '')
    .replace(/\{label\}/g, vars.label || '')
    .replace(/\{mode\}/g, vars.mode || '')
    .trim();
}

/** 兜底：未知市场按 A 股规格渲染（宁可显示 A 股规格也不要白屏） */
export function getMarketContent(market: AppMarket | string | undefined): MarketContent {
  const key = String(market || 'CN').toUpperCase() as AppMarket;
  return MARKET_CONTENT[key] || MARKET_CONTENT.CN;
}
