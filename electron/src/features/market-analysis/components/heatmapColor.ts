/**
 * 市场分析 · 热力矩形图谱着色口径（板块层与个股下钻层共用的纯函数）
 *
 * A 股口径：**涨红跌绿**（与全站一致，见 moneyFlowService.flowTextClass）。
 * - 「涨跌幅」按档位着色：±10（涨/跌停）、±5、±2、平盘，机构图谱惯例；
 * - 「主力净额」按符号 + 幅度分档着色：净流入红、净流出绿。
 *
 * 单位约定：与后端 /heatmap、/heatmap/drill 契约一致 —— `pct_change` 为 %，
 * `flow_net` 与 `value`（面积规模）均为**亿元**。本文件只贡献颜色与文本，
 * 不做金额换算，避免与后端口径重复换算（DRY）。
 */

export type HeatmapColorMode = 'pct' | 'flow' | 'accel' | 'direction' | 'sentiment';

/** 涨跌幅档位色（A 股涨红跌绿；阈值与「涨/跌停=±10%」口径对齐）。 */
export function pctToColor(pct: number | null | undefined): string {
  const v = typeof pct === 'number' && Number.isFinite(pct) ? pct : 0;
  if (v >= 10) return '#dc2626';
  if (v >= 5) return '#ef4444';
  if (v > 2) return '#f87171';
  if (v >= 0) return '#94a3b8';
  if (v > -5) return '#34d399';
  if (v > -10) return '#10b981';
  return '#059669';
}

/** 主力净额档位色（单位：亿元）。幅度越大颜色越深。 */
export function flowToColor(flowYi: number | null | undefined): string {
  const v = typeof flowYi === 'number' && Number.isFinite(flowYi) ? flowYi : 0;
  if (v > 1) return '#dc2626'; // 净流入 > 1 亿
  if (v > 0) return '#f87171';
  if (v === 0) return '#94a3b8';
  if (v > -1) return '#34d399';
  return '#059669'; // 净流出 <= -1 亿
}

/** 加速度档位色（单位：亿/日）：越快越深，正=加速流入，负=加速流出。 */
export function accelToColor(accelYi: number | null | undefined): string {
  const v = typeof accelYi === 'number' && Number.isFinite(accelYi) ? accelYi : 0;
  if (v >= 5) return '#dc2626';
  if (v >= 1) return '#ef4444';
  if (v > 0) return '#f87171';
  if (v === 0) return '#94a3b8';
  if (v > -1) return '#34d399';
  if (v > -5) return '#10b981';
  return '#059669';
}

export interface HeatmapDirectionCellLike {
  prob: number;
  rel_prob: number;
  market_prob?: number;
  n?: number | null;
  date_win?: number | null;
  /** 全板块截面分位（0~1，1=相对概率最高）；由热力图容器计算 */
  rank?: number | null;
}


/** 情绪热度输入：资金广度 / 广度变化 / 周期涨幅 / 资金加速（后端 /heatmap 同口径）。 */
export interface SentimentHeatItem {
  name: string;
  breadth_flow?: number | null;
  breadth_flow_chg?: number | null;
  pct_above_ma20?: number | null;
  pct_above_ma20_chg?: number | null;
  pct_period?: number | null;
  flow_ratio_accel?: number | null;
}

const SENTIMENT_FIELDS: Array<keyof Omit<SentimentHeatItem, 'name'>> = [
  'pct_above_ma20',
  'pct_above_ma20_chg',
  'breadth_flow',
  'breadth_flow_chg',
  'pct_period',
  'flow_ratio_accel',
];

/** 情绪热度 = 价格广度/资金广度/广度变化/周期涨幅/资金加速的平均分位（0~1）；
 *  缺失分量不参与，全缺失为 null。 */
export function computeSentimentScores<T extends SentimentHeatItem>(
  items: T[],
): Array<T & { sentiment_score: number | null }> {
  const ranks = new Map<string, Map<string, number>>();
  for (const field of SENTIMENT_FIELDS) {
    const entries = items
      .map((it) => ({ name: it.name, v: it[field] }))
      .filter(
        (x): x is { name: string; v: number } =>
          typeof x.v === 'number' && Number.isFinite(x.v),
      )
      .sort((a, b) => a.v - b.v);
    const m = new Map<string, number>();
    const n = entries.length;
    let i = 0;
    while (i < n) {
      // 同值取平均秩，避免并列值按输入顺序被任意区分
      let j = i;
      while (j + 1 < n && entries[j + 1].v === entries[i].v) j += 1;
      const rank = n <= 1 ? 0.5 : (i + j) / 2 / (n - 1);
      for (let k = i; k <= j; k += 1) m.set(entries[k].name, rank);
      i = j + 1;
    }
    ranks.set(field, m);
  }
  return items.map((it) => {
    const scores = SENTIMENT_FIELDS.map((f) => ranks.get(f)?.get(it.name)).filter(
      (v): v is number => typeof v === 'number' && Number.isFinite(v),
    );
    return {
      ...it,
      sentiment_score: scores.length
        ? scores.reduce((a, b) => a + b, 0) / scores.length
        : null,
    };
  });
}

export interface HeatmapCellMetric {
  pct_change?: number | null;
  flow_net?: number | null;
  flow_accel?: number | null;
  direction?: Record<string, HeatmapDirectionCellLike> | null;
  /** 情绪热度（0~1，截面分位合成；不是概率） */
  sentiment_score?: number | null;
}

/** 方向概率：按全板块截面排名着色（不是绝对概率档位）。
 *  排名高=历史同状态下相对更强；数字仍显示历史概率，50%=无相对优势。 */
export function directionToColor(rank: number | null | undefined): string {
  if (typeof rank !== 'number' || !Number.isFinite(rank)) return '#e2e8f0';
  if (rank >= 0.8) return '#dc2626';
  if (rank >= 0.6) return '#f87171';
  if (rank > 0.4) return '#cbd5e1';
  if (rank > 0.2) return '#34d399';
  return '#059669';
}

/** 方向标签：箭头=截面排名（前 50% ↑ / 后 50% ↓），数字=历史跑赢概率。 */
export function heatmapDirectionLabel(cell: HeatmapDirectionCellLike | null | undefined): string {
  if (!cell || typeof cell.rel_prob !== 'number') return '—';
  const arrow =
    typeof cell.rank === 'number' ? (cell.rank >= 0.5 ? '↑' : '↓') : cell.rel_prob >= 0.5 ? '↑' : '↓';
  return `${arrow}${(cell.rel_prob * 100).toFixed(0)}%`;
}

/** 情绪热度：0~1 的截面分位（高=广度/价格/资金共振偏热，低=偏冷）。 */
export function sentimentToColor(score: number | null | undefined): string {
  if (typeof score !== 'number' || !Number.isFinite(score)) return '#e2e8f0';
  if (score >= 0.8) return '#dc2626';
  if (score >= 0.6) return '#f87171';
  if (score > 0.4) return '#cbd5e1';
  if (score > 0.2) return '#34d399';
  return '#059669';
}

/** 统一取色入口：按当前着色维度返回色值；direction 模式需传方向周期。 */
export function heatmapCellColor(
  item: HeatmapCellMetric,
  mode: HeatmapColorMode,
  directionH?: number,
): string {
  if (mode === 'direction') {
    const cell = item.direction?.[String(directionH ?? 10)];
    return directionToColor(cell?.rank);
  }
  if (mode === 'sentiment') return sentimentToColor(item.sentiment_score);
  if (mode === 'accel') return accelToColor(item.flow_accel);
  return mode === 'flow' ? flowToColor(item.flow_net) : pctToColor(item.pct_change);
}

/**
 * 矩形标签上的数值文本（带符号）。
 * 缺失一律显示「—」（与全站「缺失不显示成 0」口径一致）。
 */
export function heatmapLabelValue(
  item: HeatmapCellMetric,
  mode: HeatmapColorMode,
  directionH?: number,
): string {
  if (mode === 'direction') {
    return heatmapDirectionLabel(item.direction?.[String(directionH ?? 10)]);
  }
  if (mode === 'sentiment') {
    const v = item.sentiment_score;
    return typeof v === 'number' && Number.isFinite(v) ? `热度${(v * 100).toFixed(0)}` : '—';
  }
  if (mode === 'accel') {
    const v = item.flow_accel;
    if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
    return `${v >= 0 ? '+' : ''}${v.toFixed(2)}亿/日`;
  }
  if (mode === 'flow') {
    const v = item.flow_net;
    if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
    return `${v >= 0 ? '+' : ''}${v.toFixed(2)}亿`;
  }
  const p = item.pct_change;
  if (typeof p !== 'number' || !Number.isFinite(p)) return '—';
  return `${p >= 0 ? '+' : ''}${p.toFixed(2)}%`;
}

/** 着色维度切换按钮文案（板块层与下钻层共用）。 */
export const HEATMAP_COLOR_LABEL: Record<HeatmapColorMode, string> = {
  pct: '涨跌幅',
  flow: '主力净额',
  accel: '加速度',
  direction: '方向概率',
  sentiment: '情绪热度',
};
