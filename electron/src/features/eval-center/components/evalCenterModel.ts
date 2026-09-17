/** 评估中心纯函数（评分卡展示模型；可单测，无副作用） */

import type { EvalScoreRow } from '../types/evalCenter';

export interface GradeMeta {
  label: string;
  className: string;
  isLowConfidence: boolean;
}

/** 评级 → 徽章样式（A≥85 / B 70-84 / C 60-69 / D<60；体检四分类 A/B/L/E 同表可渲染） */
export function gradeMeta(grade: string | null | undefined, lowConfidence = false): GradeMeta {
  const raw = String(grade || '').trim().toUpperCase();
  const isLowConfidence = lowConfidence || raw.endsWith('†');
  const base = raw.replace('†', '');
  const styleMap: Record<string, string> = {
    A: 'bg-red-50 text-red-700 border-red-200',
    B: 'bg-blue-50 text-blue-700 border-blue-200',
    C: 'bg-orange-50 text-orange-700 border-orange-200',
    D: 'bg-slate-100 text-slate-600 border-slate-200',
    L: 'bg-amber-50 text-amber-800 border-amber-200',
    E: 'bg-slate-100 text-slate-500 border-slate-300',
  };
  const labelMap: Record<string, string> = {
    A: 'A 优秀',
    B: 'B 良好',
    C: 'C 合格',
    D: 'D 不合格',
    L: 'L 运气嫌疑',
    E: 'E 证据不足',
  };
  return {
    label: labelMap[base] || (base ? `${base}` : '未评级'),
    className: styleMap[base] || 'bg-slate-100 text-slate-500 border-slate-200',
    isLowConfidence,
  };
}

/** 评级 → 主色（图表/进度条用 hex；语义遵循 A 股红涨：A 红 / B 蓝 / C 橙 / D 灰） */
export function gradeColor(grade: string | null | undefined): string {
  const base = String(grade || '').trim().toUpperCase().replace('†', '');
  const colorMap: Record<string, string> = {
    A: '#dc2626',
    B: '#2563eb',
    C: '#ea580c',
    D: '#64748b',
    L: '#d97706',
    E: '#94a3b8',
  };
  return colorMap[base] || '#6366f1';
}

export interface GradeCount {
  grade: string;
  count: number;
}

/** 评级分布（A/B/C/D/L/E 固定顺序；未评级归 '?'；零值不出现） */
export function gradeCounts(rows: EvalScoreRow[] | null | undefined): GradeCount[] {
  const list = rows || [];
  const order = ['A', 'B', 'C', 'D', 'L', 'E', '?'];
  const tally = new Map<string, number>();
  for (const row of list) {
    const base = String(row.grade || '').trim().toUpperCase().replace('†', '') || '?';
    const key = order.includes(base) ? base : '?';
    tally.set(key, (tally.get(key) || 0) + 1);
  }
  return order
    .filter((g) => (tally.get(g) || 0) > 0)
    .map((g) => ({ grade: g, count: tally.get(g) as number }));
}

/** 平均分（只统计有效分数；一位小数；无有效分数 → null） */
export function averageScore(rows: EvalScoreRow[] | null | undefined): number | null {
  const scores = (rows || [])
    .map((r) => r.score)
    .filter((s): s is number => typeof s === 'number');
  if (scores.length === 0) return null;
  return Math.round((scores.reduce((a, b) => a + b, 0) / scores.length) * 10) / 10;
}

export interface RowLabels {
  primary: string;
  secondary: string | null;
}

/** 列表主/副标题：display_name 优先、原 id 作副标题；无名字时按类型兜底 */
export function rowLabels(row: Pick<EvalScoreRow, 'object_type' | 'object_id' | 'display_name'>): RowLabels {
  const name = String(row.display_name || '').trim();
  if (name) return { primary: name, secondary: row.object_id };
  if (row.object_type === 'strategy') {
    return { primary: `策略回测 ${row.object_id.slice(0, 8)}…`, secondary: row.object_id };
  }
  return { primary: row.object_id, secondary: null };
}

export interface DimensionView {
  key: string;
  label: string;
  score: number | null;
  weight: number;
  redLine: boolean;
  note: string;
}

/** 维度明细 → 视图行（缺省维度如实展示 note，不隐藏） */
export function dimensionViews(row: EvalScoreRow | null | undefined): DimensionView[] {
  const dims = row?.dimensions || {};
  return Object.entries(dims).map(([key, dim]) => {
    const detail = (dim?.detail || {}) as Record<string, unknown>;
    const note = detail.insufficient
      ? String(detail.note || '本期不可评（权重已归一）')
      : detail.red_line
        ? String(detail.red_line)
        : '';
    return {
      key,
      label: String(dim?.label || key),
      score: typeof dim?.score === 'number' ? dim.score : null,
      weight: typeof dim?.weight === 'number' ? dim.weight : 0,
      redLine: Boolean(dim?.red_line_failed),
      note,
    };
  });
}

export interface RadarEntry {
  name: string;
  value: number;
}

/** 雷达数据（≥3 个有效维度才可画；缺省维度不进雷达，如实由明细表列出） */
export function radarEntries(row: EvalScoreRow | null | undefined): RadarEntry[] | null {
  const withScore = dimensionViews(row).filter((d) => d.score !== null);
  if (withScore.length < 3) return null;
  return withScore.map((d) => ({ name: d.label, value: Math.round((d.score as number) * 10) / 10 }));
}

export interface HistorySeries {
  dates: string[];
  scores: (number | null)[];
  grades: (string | null)[];
}

/** 历史序列（升序输入；空/单点也可返回，由视图层决定是否画线） */
export function historySeries(rows: EvalScoreRow[] | null | undefined): HistorySeries {
  const list = rows || [];
  return {
    dates: list.map((r) => r.snapshot_date || ''),
    scores: list.map((r) => (typeof r.score === 'number' ? r.score : null)),
    grades: list.map((r) => r.grade || null),
  };
}

/** 覆盖描述：可评维度数/总维度数 + 红线数（评分卡列表行摘要） */
export function coverageSummary(row: EvalScoreRow | null | undefined): string {
  const views = dimensionViews(row);
  const scored = views.filter((d) => d.score !== null).length;
  const red = views.filter((d) => d.redLine).length;
  const parts = [`维度 ${scored}/${views.length}`];
  if (red > 0) parts.push(`红线 ${red}`);
  return parts.join(' · ');
}
