/**
 * 首页六宫格共享层契约。
 *
 * 这里只放「跨市场共用的形状」，市场之间的差异全部写在 marketContent.ts。
 * 卡片组件读规格渲染，禁止在卡片里写 `if (market === 'HK')`。
 */

import type { AppMarket } from '../../store/slices/uiSlice';

export type { AppMarket };

/** 六宫格里的六个框（与 App.tsx 的 DashboardModule.component 一致） */
export type BoxId = 'market' | 'fund' | 'trade' | 'strategy' | 'charts' | 'notify';

export const BOX_IDS: BoxId[] = ['market', 'fund', 'trade', 'strategy', 'charts', 'notify'];

/** 单个框在某个市场下的内容规格 */
export interface BoxContent {
  /**
   * 卡标题模板，支持 `{label}`（市场名）与 `{mode}`（模拟/实盘）占位符。
   * 例：`资金概览 ({label}/{mode})` → 「资金概览 (港股/模拟)」。
   * 标题也走规格，是为了加市场时不用改卡片。
   */
  title: string;
  /** 数据源标注，例如 QuantDB / QuantHK / QuantUS / QuantFutures */
  source: string;
  /** 货币符号（金额类卡片用，非金额卡片可省） */
  currency?: string;
  /** 该市场无数据时的标题 */
  emptyTitle: string;
  /** 该市场无数据时的说明（写清为什么空，不要含糊） */
  emptyHint: string;
  /** 空态是否提供「开通模拟盘」动作（仅资金/交易类框适用） */
  canOpenAccount?: boolean;
  /**
   * 子面板可用性开关（缺省全开）。用于「后端还没有市场维度」的过渡期：
   * 声明某子面板在该市场没有市场口径数据，卡片按声明渲染占位，
   * **不要**拿别的市场的数据顶上。
   * 例：模拟盘日快照 / 组合绩效暂未按市场分表 → 非 A 股市场关闭 portfolioSeries。
   */
  panels?: { portfolioSeries?: boolean; positionRatio?: boolean };
  /** 子面板被关闭时的说明（写清原因，让用户知道不是坏了） */
  panelNote?: string;
}

/** 一个市场的完整内容规格 */
export interface MarketContent {
  market: AppMarket;
  /** 市场中文名：A股 / 港股 / 美股 / 期货 / 区块链 */
  label: string;
  /** 卡片徽标的主题色（Tailwind class 组合，避免共享组件里写死颜色） */
  accent: string;
  /** 该市场模拟盘的默认开通资金（元/本币），空态开通时预填 */
  defaultSeedCash?: number;
  boxes: Record<BoxId, BoxContent>;
}

/** 取数层统一返回的数据态，供 BoxPlaceholder 决定渲染哪一态 */
export interface BoxDataState {
  loading?: boolean;
  /** 是否有可用数据 */
  hasData: boolean;
  /** 该市场账户未开通（后端 account_not_initialized） */
  notInitialized?: boolean;
  /** 取数失败原因（有值时优先展示失败态） */
  error?: string | null;
}
