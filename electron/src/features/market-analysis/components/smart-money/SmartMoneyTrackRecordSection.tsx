/** 聪明钱 · 战绩追踪区（细案 §6.2-4，「历史那些聪明钱比较聪明，包括最近」主角面）。
 *
 * 内容：累计超额曲线（h5 主图，重叠窗叠加的展示口径）→ 四档窗口统计条 →
 * 评分桶单调性柱 → 复合分滚动 IC（60 日）→ 近 10 日案例表（含进行中，可点入个股）。
 * 纪律：历史回溯统计（非预测）；缺失「—」不补 0；案例表 symbol 前缀式直通个股浮窗。
 */

import React, { useMemo, useState } from 'react';
import { History, ListOrdered } from 'lucide-react';
import { Tooltip } from 'antd';
import { EmptyHint, FaceCard, PeriodChips } from '../../../market-analysis-shared/ui';
import type {
  SmartTrackRecent,
  SmartTrackRecordResponse,
  SmartTrackWindow,
} from '../../services/smartMoneyService';
import {
  BUCKET_TONES,
  MISSING,
  directionHint,
  fmtBp,
  fmtNum,
  fmtPct,
  fmtSigned,
  scoreCls,
  signCls,
} from '../../smartMoneyVocabulary';
import {
  AXIS_LABEL,
  BASE_GRID,
  CHART_COLORS,
  SmartChart,
  tipNum,
} from './charts';

const HORIZON_OPTIONS = [
  { id: 'h5', label: 'h5' },
  { id: 'h2', label: 'h2' },
  { id: 'h3', label: 'h3' },
  { id: 'h10', label: 'h10' },
];

const WINDOW_LABELS: Record<string, string> = {
  '20d': '近 20 日',
  '60d': '近 60 日',
  '120d': '近 120 日',
  all: '全史',
};

const STATUS_TONES: Record<string, string> = {
  持有中: 'bg-sky-50 text-sky-700 border-sky-100',
  已实现h10: 'bg-slate-50 text-slate-500 border-slate-200',
};

interface SmartMoneyTrackRecordSectionProps {
  data: SmartTrackRecordResponse | null;
  loading?: boolean;
  onPick?: (symbol: string) => void;
  /** true = 无卡壳（统一大框内的标签面） */
  bare?: boolean;
}

const WindowStatRow: React.FC<{ w: SmartTrackWindow }> = ({ w }) => (
  <div className="grid grid-cols-[64px_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)] gap-1.5 px-1 py-1 items-baseline border-b border-slate-50 last:border-0">
    <span className="text-[10px] font-extrabold text-slate-600">
      {WINDOW_LABELS[w.window] ?? w.window}
    </span>
    <span className={`text-right text-[10px] font-mono font-bold ${signCls(w.h5?.mean_excess_bp)}`}>
      {fmtBp(w.h5?.mean_excess_bp)}
    </span>
    <span className="text-right text-[10px] font-mono text-slate-500">
      {fmtPct(w.h5?.hit_rate, 0)}
    </span>
    <span className={`text-right text-[10px] font-mono ${signCls(w.h10?.mean_excess_bp)}`}>
      {fmtBp(w.h10?.mean_excess_bp)}
    </span>
    <span className="text-right text-[10px] font-mono text-slate-400">
      {w.h10?.n_days ?? MISSING}
    </span>
    <span className="text-right text-[10px] font-mono text-slate-400">
      {w.h5?.n_days ?? MISSING}
    </span>
  </div>
);

const RecentRow: React.FC<{ r: SmartTrackRecent; onPick?: (s: string) => void }> = ({
  r,
  onPick,
}) => (
  <div
    onClick={() => onPick?.(r.symbol)}
    title={`打开 ${r.symbol} 个股终端`}
    className="grid grid-cols-[64px_minmax(110px,1.3fr)_48px_64px_64px_64px_56px] gap-1.5 px-1 py-1 items-baseline border-b border-slate-50 last:border-0 cursor-pointer hover:bg-purple-50/60 transition-colors"
  >
    <span className="text-[9px] font-mono text-slate-400">{r.date.slice(5)}</span>
    <span className="min-w-0 flex items-baseline gap-1">
      <span className="text-[10px] font-bold text-slate-700 truncate">{r.name || r.symbol}</span>
      <span className="text-[9px] font-mono text-slate-400">{r.symbol}</span>
    </span>
    <span className={`text-right text-[10px] font-mono font-bold ${scoreCls(r.score)}`}>
      {fmtNum(r.score, 1)}
    </span>
    <span className={`text-right text-[10px] font-mono ${signCls(r.ret_so_far)}`}>
      {fmtPct(r.ret_so_far, 1)}
    </span>
    <span className={`text-right text-[10px] font-mono ${signCls(r.ret_5)}`}>
      {fmtPct(r.ret_5, 1)}
    </span>
    <span className={`text-right text-[10px] font-mono ${signCls(r.ret_10)}`}>
      {fmtPct(r.ret_10, 1)}
    </span>
    <span
      className={`text-center text-[9px] font-bold border rounded px-1 ${
        STATUS_TONES[r.status] ?? 'bg-slate-50 text-slate-500 border-slate-200'
      }`}
    >
      {r.status}
      {r.status === '持有中' && r.hold_days > 0 ? `·${r.hold_days}d` : ''}
    </span>
  </div>
);

export const SmartMoneyTrackRecordSection: React.FC<SmartMoneyTrackRecordSectionProps> = ({
  data,
  loading,
  onPick,
  bare = false,
}) => {
  const [h, setH] = useState('h5');

  const cumOption = useMemo(() => {
    const series = data?.cum_excess?.[h] ?? [];
    if (!series.length) return null;
    const dates = series.map((p) => String(p.date).slice(5));
    const vals = series.map((p) => (p.cum === null ? null : Number(p.cum) * 100));
    return {
      tooltip: {
        trigger: 'axis',
        textStyle: { fontSize: 10 },
        formatter: (ps: Array<{ dataIndex: number }>) => {
          const i = ps[0]?.dataIndex ?? 0;
          const p = series[i];
          if (!p) return '';
          return `${p.date}<br/>累计超额 ${tipNum(vals[i], (n) => `${fmtSigned(n, 2)}%`)}`;
        },
      },
      grid: { ...BASE_GRID, top: 10 },
      xAxis: {
        type: 'category',
        data: dates,
        boundaryGap: false,
        axisLabel: { ...AXIS_LABEL, interval: Math.floor(dates.length / 6) },
      },
      yAxis: {
        type: 'value',
        axisLabel: { ...AXIS_LABEL, formatter: (v: number) => `${v.toFixed(0)}%` },
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      },
      series: [
        {
          type: 'line',
          data: vals,
          smooth: true,
          symbol: 'none',
          connectNulls: false,
          lineStyle: { width: 1.8, color: CHART_COLORS.score },
          itemStyle: { color: CHART_COLORS.score },
          areaStyle: { color: CHART_COLORS.scoreFill },
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { color: CHART_COLORS.axis, type: 'dashed' },
            data: [{ yAxis: 0 }],
          },
        },
      ],
    };
  }, [data, h]);

  const bucketOption = useMemo(() => {
    const b = data?.buckets;
    if (!b || !b.mean_excess_bp?.some((v) => v !== null)) return null;
    const vals = b.mean_excess_bp.map((v) => (v === null ? null : Number(v)));
    return {
      tooltip: {
        trigger: 'axis',
        textStyle: { fontSize: 10 },
        formatter: (ps: Array<{ dataIndex: number }>) => {
          const i = ps[0]?.dataIndex ?? 0;
          if (i < 0 || i >= b.labels.length) return '';
          return `${b.labels[i]} 分箱<br/>平均超额(h5) ${tipNum(vals[i], (n) => `${fmtSigned(n, 1)}bp`)}<br/>样本日 ${b.n_days[i] ?? MISSING}`;
        },
      },
      grid: { ...BASE_GRID, top: 10 },
      xAxis: {
        type: 'category',
        data: b.labels,
        axisLabel: AXIS_LABEL,
      },
      yAxis: {
        type: 'value',
        axisLabel: { ...AXIS_LABEL, formatter: (v: number) => `${v.toFixed(0)}` },
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      },
      series: [
        {
          type: 'bar',
          data: vals,
          barMaxWidth: 34,
          itemStyle: {
            color: (p: { value: number | null }) =>
              p.value === null
                ? CHART_COLORS.axis
                : p.value > 0
                  ? CHART_COLORS.pos
                  : p.value < 0
                    ? CHART_COLORS.neg
                    : CHART_COLORS.neutral,
          },
          label: {
            show: true,
            position: 'top',
            fontSize: 9,
            color: CHART_COLORS.neutral,
            formatter: (p: { value: number | null }) =>
              p.value === null ? '' : `${p.value > 0 ? '+' : ''}${p.value.toFixed(0)}`,
          },
        },
      ],
    };
  }, [data]);

  const rollingOption = useMemo(() => {
    const r = data?.rolling_ic;
    if (!r || !r.series?.length) return null;
    const dates = r.series.map((p) => String(p.date).slice(5));
    const vals = r.series.map((p) => (p.ic === null ? null : Number(p.ic)));
    return {
      tooltip: {
        trigger: 'axis',
        textStyle: { fontSize: 10 },
        formatter: (ps: Array<{ dataIndex: number }>) => {
          const i = ps[0]?.dataIndex ?? 0;
          const p = r.series[i];
          if (!p) return '';
          return `${p.date}<br/>滚动 IC(h${r.h}·${r.window}日) ${tipNum(vals[i], (n) => fmtNum(n, 4))}`;
        },
      },
      grid: { ...BASE_GRID, top: 10 },
      xAxis: {
        type: 'category',
        data: dates,
        boundaryGap: false,
        axisLabel: { ...AXIS_LABEL, interval: Math.floor(dates.length / 5) },
      },
      yAxis: {
        type: 'value',
        axisLabel: { ...AXIS_LABEL, formatter: (v: number) => v.toFixed(2) },
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      },
      series: [
        {
          type: 'line',
          data: vals,
          smooth: true,
          symbol: 'none',
          connectNulls: false,
          lineStyle: { width: 1.6, color: CHART_COLORS.score },
          itemStyle: { color: CHART_COLORS.score },
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { color: CHART_COLORS.axis, type: 'dashed' },
            data: [{ yAxis: 0 }],
          },
        },
      ],
    };
  }, [data]);

  const all = data?.windows?.find((w) => w.window === 'all')?.h5 ?? null;
  const mono = data?.buckets?.monotonicity ?? null;

  return (
    <FaceCard
      bare={bare}
      icon={<History className="w-3.5 h-3.5 text-purple-600" />}
      title={
        <span className="flex items-center gap-1.5">
          战绩追踪 · 历史回溯统计（非预测）
          <span className="text-[10px] font-normal text-slate-400">
            每日池内 Top-{data?.top_n ?? MISSING} 等权组合 · 基准={data?.benchmark ?? '全市场等权'}
          </span>
        </span>
      }
      extra={
        data?.as_of ? (
          <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">
            截至 {data.as_of}
          </span>
        ) : null
      }
    >
      {loading && !data ? (
        <EmptyHint loading />
      ) : !data ? (
        <EmptyHint text="战绩数据尚未生成（随每日构建产出）" />
      ) : (
        <div className="flex flex-col gap-3 min-w-0">
          {/* 全史头号数字 */}
          <div className="rounded-xl bg-purple-50/60 border border-purple-100 px-3 py-2 flex items-center gap-x-5 gap-y-1 flex-wrap">
            <span className="text-[10px] font-extrabold text-purple-800">全史（h5）</span>
            <span className="text-[10px] text-slate-500">
              平均超额{' '}
              <span className={`font-mono font-bold ${signCls(all?.mean_excess_bp)}`}>
                {fmtBp(all?.mean_excess_bp)}bp
              </span>
            </span>
            <span className="text-[10px] text-slate-500">
              胜率 <span className="font-mono font-bold text-slate-800">{fmtPct(all?.hit_rate, 1)}</span>
            </span>
            <span className="text-[10px] text-slate-500">
              计分日 <span className="font-mono font-bold text-slate-800">{all?.n_days ?? MISSING}</span>
            </span>
            <span className="text-[10px] text-slate-500">
              分箱单调性{' '}
              <Tooltip title="评分桶中值 vs 桶平均 h5 超额的秩一致度（∈[−1,1]）；离 1 越远越不单调，如实披露">
                <span className={`font-mono font-bold ${mono !== null && Number(mono) > 0.5 ? 'text-slate-800' : 'text-amber-700'}`}>
                  {fmtNum(mono, 2)}
                </span>
              </Tooltip>
              {mono !== null && Number(mono) <= 0.5 && (
                <span className="text-amber-600 ml-1">（非严格单调，如实披露）</span>
              )}
            </span>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 min-w-0">
            {/* 累计超额曲线 */}
            <div className="min-w-0">
              <div className="flex items-center justify-between gap-1 px-1">
                <span className="text-[10px] font-extrabold text-slate-600">
                  累计超额曲线（{h}）
                </span>
                <PeriodChips options={HORIZON_OPTIONS} value={h} onChange={setH} />
              </div>
              <SmartChart option={cumOption} height={150} />
              <div className="text-[9px] text-slate-400 px-1">
                逐计分日超额（组合−全市场等权）累加；重叠窗叠加的展示口径，非无重叠资金曲线。
              </div>
            </div>

            {/* 评分桶 + 滚动 IC */}
            <div className="min-w-0 flex flex-col gap-2">
              <div>
                <div className="text-[10px] font-extrabold text-slate-600 px-1">
                  评分桶 → 平均 h5 超额（bp）
                </div>
                <SmartChart option={bucketOption} height={110} />
              </div>
              <div>
                <div className="text-[10px] font-extrabold text-slate-600 px-1">
                  复合分滚动 IC（h{data.rolling_ic?.h ?? MISSING} · {data.rolling_ic?.window ?? MISSING} 日窗）
                </div>
                <SmartChart
                  option={rollingOption}
                  height={100}
                  emptyText="滚动 IC 尚未生成"
                />
              </div>
            </div>
          </div>

          {/* 四档窗口统计 */}
          <div className="min-w-0">
            <div className="grid grid-cols-[64px_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)] gap-1.5 px-1 pb-1 text-[9px] font-extrabold text-slate-400 border-b border-slate-100">
              <span>窗口</span>
              <span className="text-right">超额·h5(bp)</span>
              <span className="text-right">胜率·h5</span>
              <span className="text-right">超额·h10(bp)</span>
              <span className="text-right">n·h10</span>
              <span className="text-right">n·h5</span>
            </div>
            {(data.windows ?? []).map((w) => (
              <WindowStatRow key={w.window} w={w} />
            ))}
            <div className="text-[9px] text-slate-400 px-1 mt-1">
              胜率=日均超额&gt;0 占比；近窗样本小，请连同 n 一并阅读。
            </div>
          </div>

          {/* 最近案例：默认收起，图表优先；需要逐行对账时展开 */}
          <details className="min-w-0 rounded-xl border border-slate-100 bg-white/60 px-2 py-1.5">
            <summary className="cursor-pointer select-none flex items-center gap-1.5 pb-1">
              <ListOrdered className="w-3 h-3 text-purple-600" />
              <span className="text-[10px] font-extrabold text-slate-600">
                最近案例（近 10 计分日 × Top-5，含进行中）
              </span>
              <span className="text-[9px] text-slate-400">
                T+1 收盘入场口径 · 点行打开个股档案 · 展开看逐行
              </span>
            </summary>
            {/* 限高滚动：近 10 计分日 × Top-5 全量入表会拉长整面，表头吸顶滚动阅读 */}
            <div className="max-h-[300px] overflow-y-auto custom-scrollbar rounded-lg border border-slate-100/80">
              <div className="grid grid-cols-[64px_minmax(110px,1.3fr)_48px_64px_64px_64px_56px] gap-1.5 px-1 py-1 text-[9px] font-extrabold text-slate-400 border-b border-slate-100 sticky top-0 bg-white/95 backdrop-blur z-[5]">
                <span>日</span>
                <span>标的</span>
                <span className="text-right">分</span>
                <span className="text-right">至今</span>
                <span className="text-right">h5</span>
                <span className="text-right">h10</span>
                <span className="text-center">状态</span>
              </div>
              {(data.recent ?? []).length ? (
                (data.recent ?? []).map((r) => (
                  <RecentRow key={`${r.date}-${r.symbol}`} r={r} onPick={onPick} />
                ))
              ) : (
                <EmptyHint text="近期无案例入池" />
              )}
            </div>
            <div className="text-[9px] text-slate-400 px-1 mt-1">
              「至今」=T+1 收盘入场至最新收盘的浮动收益（持有中最新实况）；「h5/h10」=已实现前向收益，未到期为「—」。
            </div>
          </details>
        </div>
      )}
    </FaceCard>
  );
};
