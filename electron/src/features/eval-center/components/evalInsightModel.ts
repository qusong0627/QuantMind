/**
 * 评估中心「总览 / 维度覆盖 / 关键实测量」纯函数（阶段 2 三段式的第 1、2 段）。
 *
 * 三条纪律：
 *
 * 1. **维度顺序不抄 jsonb**：PG `jsonb` 会按自己的规则重排键（字母序），照抄会让同一张
 *    卡的格子在两次读取之间跳位。顺序按各评分卡的权重口径写死，只有未知类型才退回
 *    「权重降序」。
 * 2. **缺证据要有位置、有原因**：维度缺省不是「不显示」，是「斜纹格 + 该维自带的
 *    note」。把缺省从格子里删掉等于宣称「这维没问题」。
 * 3. **实测量给不出数就给理由**：`missing=true` 时 `value` 写「缺省/待回填」，
 *    `hint` 必带原因。留白读起来像「一切正常」。
 */

import type { EvalScoreRow } from '../types/evalCenter';
import {
  averageScore,
  dimensionViews,
  formatNumber,
  formatPercent,
  formatSignedPct,
  gradeCounts,
  toNumber,
  type DimensionView,
  type GradeCount,
} from './evalCenterModel';

/** 各评分卡的维度顺序（与 `backend/scripts/eval/*_card.py` 的 WEIGHTS 同序） */
const DIMENSION_ORDER: Record<string, string[]> = {
  factor: ['predictive', 'stability', 'independence', 'quality_gate', 'coverage'],
  model: ['oos_predictive', 'stratification', 'robustness', 'rolling_health', 'turnover_cost'],
  strategy: ['return', 'risk', 'stability', 'cost', 'consistency', 'capacity'],
  account: ['exposure', 'attribution', 'risk_events', 'capital_efficiency'],
  daily_selection: ['quality', 'realized', 'consistency', 'calibration', 'coverage'],
};

/** 「有实证」的门槛：有效维度数（雷达图同门槛，两处口径必须一致） */
export const EVIDENCE_MIN_DIMENSIONS = 3;

/** 维度明细 → 按评分卡口径排序（未知类型退回权重降序；不改传入对象） */
export function orderedDimensionViews(row: EvalScoreRow | null | undefined): DimensionView[] {
  const views = dimensionViews(row);
  const order = DIMENSION_ORDER[String(row?.object_type || '')];
  if (!order) {
    return [...views].sort((a, b) => b.weight - a.weight || a.key.localeCompare(b.key));
  }
  const rank = new Map(order.map((key, index) => [key, index]));
  const fallback = order.length;
  return [...views].sort((a, b) => {
    const ra = rank.get(a.key) ?? fallback;
    const rb = rank.get(b.key) ?? fallback;
    return ra - rb || b.weight - a.weight || a.key.localeCompare(b.key);
  });
}

export interface CoverageCell {
  key: string;
  label: string;
  /** false = 该维本期缺省（斜纹格） */
  scored: boolean;
  score: number | null;
  redLine: boolean;
  /** 缺省原因 / 红线原文（空串 = 无） */
  note: string;
}

export interface DimensionCoverage {
  cells: CoverageCell[];
  scored: number;
  total: number;
  /** 证据覆盖率 0..1（无维度 → 0，不是 1） */
  rate: number;
}

/** 缺省格没有原因时用的兜底文案（斜纹格留白会被读成「这维没问题」） */
const MISSING_NO_REASON = '该维本期缺省（后端未给原因）';

/** 评分卡 → 维度覆盖格（有色=已算 / 斜纹=缺省，缺省格必带 note） */
export function dimensionCoverage(row: EvalScoreRow | null | undefined): DimensionCoverage {
  const cells = orderedDimensionViews(row).map((view) => {
    const scored = view.score !== null;
    const raw = row?.dimensions?.[view.key]?.detail as DetailLike | undefined;
    const rawNote = String(raw?.note ?? raw?.reason ?? '').trim();
    return {
      key: view.key,
      label: view.label,
      scored,
      score: view.score,
      redLine: view.redLine,
      note: view.note || (scored ? '' : rawNote || MISSING_NO_REASON),
    };
  });
  const scored = cells.filter((cell) => cell.scored).length;
  return {
    cells,
    scored,
    total: cells.length,
    rate: cells.length === 0 ? 0 : scored / cells.length,
  };
}

// ── 关键实测量 ──────────────────────────────────────────────────────

export type InsightTone = 'pos' | 'neg' | 'risk' | 'flat' | 'pending';

export interface Insight {
  label: string;
  value: string;
  tone: InsightTone;
  /** 悬停原文：口径说明，或缺省原因（缺省时不允许空串） */
  hint: string;
  /** true = 无证据（显示为斜纹/虚线，不与「算出来正好是 0」同形） */
  missing: boolean;
}

type DetailLike = Record<string, unknown>;

/** 取某维的 detail（维度不存在 → null；维度在但无 detail → 空对象） */
function detailOf(row: EvalScoreRow | null | undefined, key: string): DetailLike | null {
  const dim = row?.dimensions?.[key];
  if (!dim) return null;
  const detail = dim.detail;
  return detail && typeof detail === 'object' ? (detail as DetailLike) : {};
}

/** 符号 → 语气（A 股口径：正=红=多，负=绿=空，0=中性） */
function signTone(value: number): InsightTone {
  if (value > 0) return 'pos';
  if (value < 0) return 'neg';
  return 'flat';
}

function missingInsight(label: string, detail: DetailLike | null, fallback: string): Insight {
  const note = String(detail?.note || detail?.reason || '').trim();
  return { label, value: '缺省', tone: 'pending', hint: note || fallback, missing: true };
}

function modelInsight(row: EvalScoreRow): Insight {
  const detail = detailOf(row, 'oos_predictive');
  const ic = toNumber(detail?.test_rank_ic);
  if (ic === null) {
    return missingInsight('实测 RankIC', detail, '样本外预测力维缺省（模型缺 OOS 指标）');
  }
  const icir = toNumber(detail?.test_rank_icir);
  return {
    label: '实测 RankIC',
    value: formatNumber(ic, 4),
    tone: signTone(ic),
    hint: `样本外测试集实测 RankIC${icir === null ? '' : `，ICIR ${formatNumber(icir, 4)}`}`,
    missing: false,
  };
}

function factorInsight(row: EvalScoreRow): Insight {
  const detail = detailOf(row, 'predictive');
  const icir = toNumber(detail?.icir);
  if (icir === null) {
    return missingInsight('ICIR', detail, '预测力维缺省（IC 序列不足 20 日）');
  }
  const meanIc = toNumber(detail?.mean_ic);
  const bits: string[] = [];
  if (meanIc !== null) bits.push(`均值 IC ${formatNumber(meanIc, 4)}`);
  if (String(detail?.direction || '') === 'inverted') {
    bits.push('方向反向 —— 反转即可用，不按 0 分处理');
  }
  return {
    label: 'ICIR',
    value: formatNumber(icir, 2),
    tone: signTone(icir),
    hint: bits.join('；') || 'ICIR 取自预测力维',
    missing: false,
  };
}

function strategyInsight(row: EvalScoreRow): Insight {
  const ret = detailOf(row, 'return');
  const risk = detailOf(row, 'risk');
  const ann = toNumber(ret?.annual_return);
  const mdd = toNumber(risk?.max_drawdown);
  if (ann === null && mdd === null) {
    return missingInsight('年化 / 最大回撤', ret || risk, '收益维缺省（净值曲线样本不足）');
  }
  const bits: string[] = [];
  const bench = toNumber(ret?.benchmark_annual);
  const excess = toNumber(ret?.excess_annual);
  if (bench !== null) bits.push(`基准 ${formatSignedPct(bench)}`);
  if (excess !== null) bits.push(`超额 ${formatSignedPct(excess)}`);
  return {
    label: '年化 / 最大回撤',
    value: `${ann === null ? '—' : formatSignedPct(ann)} / ${
      mdd === null ? '—' : formatSignedPct(mdd)
    }`,
    tone: ann === null ? 'risk' : signTone(ann),
    hint: bits.length ? bits.join('；') : '年化取自收益维、最大回撤取自风险维',
    missing: false,
  };
}

function accountInsight(row: EvalScoreRow): Insight {
  const detail = detailOf(row, 'capital_efficiency');
  const util = toNumber(detail?.utilization);
  if (util === null) {
    return missingInsight('资金利用率', detail, '资金效率维缺省（总资产缺失或 ≤0）');
  }
  const cash = toNumber(detail?.cash_ratio);
  return {
    label: '资金利用率',
    value: formatPercent(util, 1),
    tone: signTone(util),
    hint: cash === null ? '利用率 = 1 − 现金占比' : `现金占比 ${formatPercent(cash, 1)}（利用率 = 1 − 现金占比）`,
    missing: false,
  };
}

function dailySelectionInsight(row: EvalScoreRow): Insight {
  const detail = detailOf(row, 'realized');
  const horizon = toNumber(detail?.horizon);
  const label = horizon === null ? '事后验证' : `T+${horizon} 超额`;
  if (detail?.pending) {
    return {
      label,
      value: '待回填',
      tone: 'pending',
      hint: String(detail.note || `T+1..T+${horizon ?? 'H'} 前向数据未齐，待回填（不假填）`),
      missing: true,
    };
  }
  const excess = toNumber(detail?.mean_excess);
  if (excess === null) {
    return missingInsight(label, detail, '事后验证维缺省（前向收益未算出）');
  }
  const hit = toNumber(detail?.hit_rate);
  const n = toNumber(detail?.n);
  const bits = [
    n === null ? null : `${n} 个标的`,
    hit === null ? null : `命中率 ${formatPercent(hit, 1)}`,
  ].filter((bit): bit is string => bit !== null);
  return {
    label,
    value: formatSignedPct(excess, 2),
    tone: signTone(excess),
    hint: bits.join(' · ') || '平均超额（标的等权，对比基准）',
    missing: false,
  };
}

const INSIGHT_BUILDERS: Record<string, (row: EvalScoreRow) => Insight> = {
  model: modelInsight,
  factor: factorInsight,
  strategy: strategyInsight,
  account: accountInsight,
  daily_selection: dailySelectionInsight,
};

/**
 * 该类最关键的一个实测量（榜单行右侧的「不看分数看实测」那一格）。
 *
 * 体检卡（strategy_health）与未知类型没有这一列 → null（调用方不渲染该列，
 * 而不是渲染一个空框）。
 */
export function insightFor(row: EvalScoreRow | null | undefined): Insight | null {
  if (!row) return null;
  const builder = INSIGHT_BUILDERS[String(row.object_type || '')];
  return builder ? builder(row) : null;
}

// ── 换手 × 成本对比条（模型卡 turnover_cost 维） ─────────────────────

export interface CostBar {
  label: string;
  value: number | null;
  tone: InsightTone;
  /** 条长比例 0..1（按三条里的最大绝对值归一） */
  width: number;
}

export interface CostCompare {
  bars: CostBar[];
  note: string;
}

/** 毛利 / 成本拖累 / 净利三条对比（缺省 → 空数组 + 原因，不画三条 0 长的假条） */
export function costCompare(row: EvalScoreRow | null | undefined): CostCompare {
  const detail = detailOf(row, 'turnover_cost');
  const gross = toNumber(detail?.gross_annual);
  const drag = toNumber(detail?.cost_drag_annual);
  const net = toNumber(detail?.net_annual);
  if (gross === null && drag === null && net === null) {
    return { bars: [], note: String(detail?.note || '换手与成本维缺省（成本后收益无从计算）') };
  }
  const scale = Math.max(Math.abs(gross ?? 0), Math.abs(drag ?? 0), Math.abs(net ?? 0));
  const width = (value: number | null) => (scale > 0 && value !== null ? Math.abs(value) / scale : 0);
  const turnover = toNumber(detail?.turnover_mean);
  const roundTrip = toNumber(detail?.round_trip_cost);
  const topK = toNumber(detail?.top_k);
  const bits = [
    turnover === null ? null : `换手均值 ${formatPercent(turnover, 1)}／期`,
    roundTrip === null ? null : `单次往返成本 ${formatPercent(roundTrip, 2)}`,
    topK === null ? null : `Top${topK}`,
  ].filter((bit): bit is string => bit !== null);
  return {
    bars: [
      { label: '毛利', value: gross, tone: gross === null ? 'flat' : signTone(gross), width: width(gross) },
      { label: '成本拖累', value: drag, tone: 'risk', width: width(drag) },
      { label: '净利', value: net, tone: net === null ? 'flat' : signTone(net), width: width(net) },
    ],
    note: bits.length ? bits.join(' · ') : '成本后收益（净利 = 毛利 + 成本拖累）',
  };
}

// ── 总览带 ──────────────────────────────────────────────────────────

export interface OverviewStats {
  total: number;
  avg: number | null;
  counts: GradeCount[];
  /** 至少一条红线的对象数 */
  redLineCount: number;
  /** ≥EVIDENCE_MIN_DIMENSIONS 维有分的对象数 */
  evidenceObjects: number;
  coverageRate: number;
  note: string;
}

export function overviewStats(rows: EvalScoreRow[] | null | undefined): OverviewStats {
  const list = rows || [];
  const total = list.length;
  const evidenceObjects = list.filter(
    (row) => dimensionCoverage(row).scored >= EVIDENCE_MIN_DIMENSIONS
  ).length;
  return {
    total,
    avg: averageScore(list),
    counts: gradeCounts(list),
    redLineCount: list.filter((row) => (row.red_line_failed?.length || 0) > 0).length,
    evidenceObjects,
    coverageRate: total === 0 ? 0 : evidenceObjects / total,
    note: total
      ? `本类 ${total} 个对象中 ${evidenceObjects} 个有 ≥${EVIDENCE_MIN_DIMENSIONS} 维实证`
      : '本类暂无对象（评分任务在 EOD 跑批/回测体检后自动写入）',
  };
}

// ── 页脚（来源 / 口径 / 时间戳，照 EvidenceMatrix 的 footer 写法） ────

export interface EvidenceFootnote {
  source: string;
  caliber: string;
  asOf: string;
  missing: string;
}

export function evidenceFootnote(row: EvalScoreRow | null | undefined): EvidenceFootnote {
  const version = (row?.inputs_version || {}) as Record<string, unknown>;
  const source = version.dataset ?? version.source ?? version.data_source;
  const coverage = dimensionCoverage(row);
  const missingLabels = coverage.cells.filter((cell) => !cell.scored).map((cell) => cell.label);
  const stamps = [row?.snapshot_date ? `快照 ${row.snapshot_date}` : '快照 —'];
  if (row?.created_at) stamps.push(`生成 ${row.created_at}`);
  return {
    source: source ? String(source) : 'eval_scores',
    caliber: `维度 ${coverage.scored}/${coverage.total} 计入加权（缺省维不计入，权重已归一）`,
    asOf: stamps.join(' · '),
    missing: missingLabels.length ? missingLabels.join('、') : '无',
  };
}
