/** 两条回测链路 → 统一的图表视图模型。
 *
 *  - 内置模板：`market_lab.run_backtest()` 同步返回 `{stats, trades[], equity[], meta}`
 *  - Pine 库策略：宿主 worker 沙箱回测产物 `report.json`
 *
 * ⚠️ 字段陷阱：`report.trades` 是**笔数**，明细在 `report.trade_rows`；
 *    而模板的 `trades` 就是明细数组。两者字段名不同、语义也不同，别混用。
 *    `trade_rows` 的字段与 `BtTrade` 完全一致，可直接喂 `KLineChart` 的 markers。
 */
import type { BtResult, BtStat, BtTrade, IndicatorSet, PineReport } from '../../api/client';

export type LabSource = 'template' | 'pine';

/** 没有捕获到指标时的空集（老报告 / 未启用捕获） */
export const EMPTY_INDICATORS: IndicatorSet = { bars: 0, overlays: [], panes: [] };

export interface LabResult {
  source: LabSource;
  /** 主标题（模板名 / 策略库标题） */
  title: string;
  /** 副标题（来源与规模说明） */
  subtitle: string;
  symbol: string;
  adj: string;
  bars: number;
  /** 回测本金：两条链路口径不同（模板 100000；Pine 产物取决于候选自己写的 initial_capital） */
  capital: number;
  /** 金额单位前缀（Pine 产物是引擎口径，不标货币） */
  unit: string;
  stats: Record<string, BtStat>;
  /** 开/平配对明细，供 K线 markers 与逐笔表使用 */
  trades: BtTrade[];
  equity: { date: string; value: number }[];
  /** 策略真正算过的指标序列（回测时捕获；与回测的 K 线逐根对齐） */
  indicators: IndicatorSet;
}

/** stats 的 value/pct 在两条链路里可能是 undefined，统一收敛成 null */
const normStats = (
  raw: Record<string, { value?: number | null; pct?: number | null }>,
): Record<string, BtStat> =>
  Object.fromEntries(
    Object.entries(raw ?? {}).map(([k, v]) => [
      k,
      { value: v?.value ?? null, pct: v?.pct ?? null },
    ]),
  );

/** 指标集：后端缺字段时可能给 {}——空对象在 JS 里是 truthy，直接读 .overlays 会炸 */
const normIndicators = (raw: IndicatorSet | null | undefined): IndicatorSet =>
  raw && Array.isArray(raw.overlays) && Array.isArray(raw.panes)
    ? { bars: raw.bars ?? 0, overlays: raw.overlays, panes: raw.panes }
    : EMPTY_INDICATORS;

export const fromTemplate = (r: BtResult): LabResult => ({
  source: 'template',
  title: r.meta.name,
  subtitle: `${r.meta.strategy} · ${r.meta.bars} 根K线`,
  symbol: r.meta.symbol,
  adj: r.meta.adj,
  bars: r.meta.bars,
  capital: r.equity?.[0]?.value ?? 100000,
  unit: '¥',
  stats: normStats(r.stats),
  trades: r.trades ?? [],
  equity: r.equity ?? [],
  indicators: normIndicators(r.indicators),
});

export const fromPineReport = (r: PineReport, title = ''): LabResult => ({
  source: 'pine',
  title: title || r.id,
  subtitle: `AI 转写 · ${r.trades ?? 0} 笔成交`,
  symbol: r.symbol,
  adj: r.adj,
  bars: 0,
  capital: r.equity?.[0]?.value ?? 0,
  unit: '',
  stats: normStats(r.stats),
  trades: r.trade_rows ?? [],
  equity: r.equity ?? [],
  indicators: normIndicators(r.indicators),
});
