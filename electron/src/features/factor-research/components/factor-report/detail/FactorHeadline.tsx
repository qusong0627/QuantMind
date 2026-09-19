/**
 * 详情区常驻头：因子标识 + 7 指标环 + 分组/成本/基准控件。
 *
 * 模型对齐 WorldQuant BRAIN 参考形态：`Returns / IR / Turnover / IC / ICIR /
 * Fitness / Margin` 七个环，环心是原始值、弧长是**全库百分位**。
 *
 * ⚠️ 百分位基准来自 `/summary` 里每个因子的 `headline`（**默认 G3/G9 口径**）。
 * 用户改分组后裸值会变，但全库分布仍是默认口径的 —— 这个不一致必须显式告知，
 * 否则就成了「拿 A 口径的值去比 B 口径的分布」的静默错数。
 */

import React from 'react';
import type { FactorBlocks, FactorSummary, HeadlineBlock, DegradedBlock } from '../../../types/factorReport';
import { MetricRing, percentileOf } from './MetricRing';
import { DASH, NEUTRAL, fmtNum, fmtPct } from './chartKit';

const GROUPS = Array.from({ length: 10 }, (_, i) => i + 1);
const COST_PRESETS = [0, 10, 20, 30, 50];
export const BENCH_PRESETS = [
  { symbol: '000300.SH', label: '沪深300' },
  { symbol: '000905.SH', label: '中证500' },
  { symbol: '000852.SH', label: '中证1000' },
];

interface Props {
  factor: string;
  summary: FactorSummary | null;
  /** 全库因子（百分位分布来源） */
  library: FactorSummary[];
  blocks: FactorBlocks | null;
  nDates: number;
  start: string;
  end: string;
  longGroup: number;
  shortGroup: number;
  costBps: number;
  bench: string;
  onParams: (p: { longGroup?: number; shortGroup?: number; costBps?: number; bench?: string }) => void;
  /** 后端口径文案（与计算代码同源） */
  definitions: Record<string, string>;
  /** 旧快照：新字段缺失，环上多项会是空 */
  staleSchema: boolean;
}

export const FactorHeadline: React.FC<Props> = ({
  factor, summary, library, blocks, nDates, start, end,
  longGroup, shortGroup, costBps, bench, onParams, definitions, staleSchema,
}) => {
  // 从联合类型上判 available 后再断言：直接在具体类型上比 `!== false` 会被 TS 判为无交集
  const headRaw = blocks?.headline;
  const head = headRaw && headRaw.available !== false ? (headRaw as HeadlineBlock) : null;
  const degraded = headRaw && headRaw.available === false ? (headRaw as DegradedBlock) : null;

  // 全库分布（默认口径）。旧快照没有 headline → 池为空 → 百分位为 null（环留灰）
  const pool = (key: 'returns' | 'ir' | 'turnover' | 'fitness' | 'margin') =>
    library.map((f) => f.headline?.[key] ?? null);

  const metricDef = (k: string, fallback: string) => definitions?.[k] || fallback;
  const isDefaultGroup = longGroup === 3 && shortGroup === 9;

  const rings: Array<{
    label: string; value: number | null; pick: 'abs' | 'signed'; lower?: boolean;
    format: (v: number) => string; pool: Array<number | null>; info: string;
  }> = [
    {
      label: 'Returns', value: head?.returns ?? null, pick: 'signed',
      format: (v) => fmtPct(v, 1), pool: pool('returns'),
      info: metricDef('returns', '多空组合年化收益 = 日均收益 × 252（简单年化，非 CAGR）。G3 多、G9 空，美元中性、每日再平衡。'),
    },
    {
      label: 'IR', value: head?.ir ?? null, pick: 'signed',
      format: (v) => fmtNum(v, 2), pool: pool('ir'),
      info: metricDef('ir', '信息比率 = 日均收益 / 日收益标准差 × √252，无风险利率取 0。'),
    },
    {
      label: 'Turnover', value: head?.turnover ?? null, pick: 'signed', lower: true,
      format: (v) => `${(v * 100).toFixed(0)}%`, pool: pool('turnover'),
      info: metricDef('turnover', '组合日均单边换手：多头腿与空头腿各自成员变动比例取平均。越低越省成本，故低分位才是「有利」。'),
    },
    {
      label: 'IC', value: head?.ic ?? null, pick: 'abs',
      format: (v) => fmtNum(v, 4), pool: library.map((f) => f.ic_mean ?? null),
      info: metricDef('ic', '日频横截面 Spearman 秩相关（因子值 vs T+k 前瞻收益）的均值。弧度按 |IC| 排名：强负 IC 同样是有效信号，反做即可。'),
    },
    {
      label: 'ICIR', value: head?.icir ?? null, pick: 'abs',
      format: (v) => fmtNum(v, 3), pool: library.map((f) => f.icir ?? null),
      info: metricDef('icir', 'IC 均值 / IC 标准差，**不年化**（与平台其余模块口径一致）。按 |ICIR| 排名。'),
    },
    {
      label: 'Fitness', value: head?.fitness ?? null, pick: 'signed',
      format: (v) => fmtNum(v, 2), pool: pool('fitness'),
      info: metricDef('fitness', 'IR × √(|Returns| / max(Turnover, 0.125))。0.125 的换手地板是 WorldQuant BRAIN 既有约定，非本平台发明。'),
    },
    {
      label: 'Margin', value: head?.margin ?? null, pick: 'signed',
      format: (v) => fmtNum(v, 3), pool: pool('margin'),
      info: metricDef('margin', 'Returns / Turnover（量纲不齐但为 BRAIN 既有约定，不改）。可理解为「每单位换手换来多少收益」。'),
    },
  ];

  return (
    <div className="shrink-0 rounded-2xl border border-slate-200/80 bg-white p-3 shadow-sm">
      <div className="flex items-center gap-3 flex-wrap">
        {/* 因子标识 */}
        <div className="min-w-[180px] max-w-[240px]">
          <div className="flex items-center gap-2">
            <span className="text-sm font-black text-slate-800 truncate" title={factor}>
              {summary?.display_name || factor}
            </span>
            {summary && (
              <span className="shrink-0 rounded-full border border-indigo-100 bg-indigo-50 px-2 py-[1px] text-[10px] font-bold text-indigo-600">
                {summary.library}
              </span>
            )}
          </div>
          <div className="text-[10px] text-slate-400 font-mono truncate">
            {summary?.display_name ? `${factor} · ${summary.category_name || ''}` : summary?.category_name || ''}
          </div>
          <div className="text-[10px] text-slate-400 font-mono mt-0.5">
            n={nDates} · {start} → {end}
          </div>
        </div>

        {/* 7 指标环 */}
        <div className="flex items-start gap-1 flex-wrap">
          {rings.map((r) => (
            <MetricRing
              key={r.label}
              label={r.label}
              value={r.value}
              percentile={percentileOf(r.pool, r.value, r.pick)}
              lowerIsBetter={r.lower}
              format={r.format}
              info={r.info}
            />
          ))}
        </div>

        {/* 控件：分组 / 成本 / 基准 */}
        <div className="ml-auto flex flex-col gap-1.5 shrink-0">
          <div className="flex items-center gap-1.5 justify-end">
            <span className="text-[10px] font-bold text-slate-400">多空组合</span>
            <GroupSelect value={longGroup} onChange={(v) => onParams({ longGroup: v })} exclude={shortGroup} tone="up" />
            <span className="text-[10px] text-slate-300">多</span>
            <GroupSelect value={shortGroup} onChange={(v) => onParams({ shortGroup: v })} exclude={longGroup} tone="down" />
            <span className="text-[10px] text-slate-300">空</span>
          </div>
          <div className="flex items-center gap-1.5 justify-end">
            <span className="text-[10px] font-bold text-slate-400">双边成本</span>
            <div className="flex items-center rounded-full border border-slate-200 bg-slate-50 p-0.5">
              {COST_PRESETS.map((b) => (
                <button
                  key={b}
                  onClick={() => onParams({ costBps: b })}
                  className={`rounded-full px-2 py-[2px] text-[10px] font-bold transition-colors ${
                    costBps === b ? 'bg-white text-indigo-700 shadow-sm' : 'text-slate-500 hover:text-slate-700'
                  }`}
                >
                  {b}
                </button>
              ))}
              <span className="px-1 text-[10px] font-bold text-slate-400">bp</span>
            </div>
          </div>
        </div>
      </div>

      {/* 口径提示条：只在真正有歧义时出现，避免常驻噪声 */}
      {(!isDefaultGroup || costBps !== 20 || degraded || staleSchema) && (
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-slate-100 pt-1.5 text-[10px] text-slate-500">
          {!isDefaultGroup && (
            <span className="text-amber-600">
              ⚠ 已切到 G{longGroup}多 / G{shortGroup}空：环上的值随之改变，但**百分位基准仍是全库默认
              G3/G9 口径**，两者不同口径，仅供横向参考。
            </span>
          )}
          {costBps !== 20 && <span>净口径按 {costBps}bp 双边成本计算（毛口径不受影响）。</span>}
          {degraded && <span className="text-amber-600">⚠ 指标环不可用：{degraded.reason}</span>}
          {staleSchema && <span className="text-amber-600">⚠ 该快照为旧结构，半截面/中性化/风格等新增指标缺失，需重跑构建。</span>}
          {head && head.n_dates > 0 && (
            <span className="font-mono">
              毛 Returns {fmtPct(head.returns, 1)} → 净 {fmtPct(head.net_returns, 1)}（成本吃掉{' '}
              {head.returns != null && head.net_returns != null
                ? fmtPct(head.returns - head.net_returns, 1)
                : DASH}）
            </span>
          )}
        </div>
      )}
      {!blocks && (
        <div className="mt-2 border-t border-slate-100 pt-1.5 text-[10px] text-slate-400">
          机构级派生块加载中…（旧字段照常可用）
        </div>
      )}
      <div className="sr-only" style={{ color: NEUTRAL }}>bench={bench}</div>
    </div>
  );
};

function GroupSelect({ value, onChange, exclude, tone }: {
  value: number;
  onChange: (v: number) => void;
  exclude: number;
  tone: 'up' | 'down';
}) {
  const color = tone === 'up' ? 'text-rose-600' : 'text-emerald-600';
  return (
    <select
      value={value}
      onChange={(e) => onChange(Number(e.target.value))}
      className={`rounded-lg border border-slate-200 bg-white px-1.5 py-[2px] text-[11px] font-black font-mono ${color} focus:border-indigo-300 focus:outline-none`}
      title={`G1 = 因子值最小 … G10 = 因子值最大；不能与另一腿同组`}
    >
      {GROUPS.filter((g) => g !== exclude).map((g) => (
        <option key={g} value={g}>G{g}</option>
      ))}
    </select>
  );
}
