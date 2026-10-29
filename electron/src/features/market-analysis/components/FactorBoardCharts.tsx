/** 因子概率板 · 图表层（v3：先看结构，再翻明细表）。
 *
 * 三张图：
 *  - 板块共振散点：x=板块合成分，y=个股广度，气泡=成分数，颜色=三层判定；
 *    竖线 q67/q33、横线 0.5 就是判定边界，右上=因子与广度同向靠前。
 *  - 因子 IC60 横条：谁在贡献、方向正负一眼可见；
 *  - IC60 × ICIR 散点：横轴强不强、纵轴稳不稳，气泡=权重。
 *
 * 口径：ICIR=mean(IC)/std(IC)，t=ICIR×√n（后端已分开）；缺失不补 0。
 */

import React from 'react';
import ReactECharts from 'echarts-for-react';
import type {
  FactorBoardFactor,
  FactorBoardSectorGroup,
  FactorBoardSectorItem,
} from '../services/factorBoardService';
import { decayMeta, verdictMeta } from '../factorBoardVocabulary';

const MISSING = '—';

const VERDICT_COLORS: Record<string, string> = {
  resonant_long: '#dc2626',
  resonant_short: '#16a34a',
  diverge_model_weak: '#d97706',
  diverge_model_resilient: '#0284c7',
  neutral: '#94a3b8',
};

const DECAY_COLORS: Record<string, string> = {
  ok: '#7c3aed',
  weak: '#d97706',
  flipped: '#dc2626',
  insufficient: '#94a3b8',
};

const AXIS = { fontSize: 9, color: '#64748b' } as const;
const GRID_LINE = { lineStyle: { color: '#f1f5f9' } } as const;

const BoardChart: React.FC<{
  option: Record<string, unknown> | null;
  height?: number;
  emptyText?: string;
  onEvents?: Record<string, (params: unknown) => void>;
}> = ({ option, height = 320, emptyText, onEvents }) => {
  if (!option) {
    return (
      <div className="grid place-items-center text-[10px] text-slate-300 font-bold" style={{ height }}>
        {emptyText ?? MISSING}
      </div>
    );
  }
  return <ReactECharts option={option} notMerge lazyUpdate onEvents={onEvents} style={{ height, width: '100%' }} />;
};

const short = (s: string, n = 8): string => (s.length > n ? `${s.slice(0, n)}…` : s);

/** 板块共振散点：判定边界直接画在图上，点击板块钻取成分股 */
export const SectorResonanceScatter: React.FC<{
  group: FactorBoardSectorGroup | null;
  onPick: (name: string) => void;
  activeName?: string | null;
}> = ({ group, onPick, activeName }) => {
  const items = (group?.items ?? []).filter(
    (it): it is FactorBoardSectorItem & { fac: number; fbreadth: number } =>
      it.fac !== null && it.fbreadth !== null,
  );
  const topLabels = new Set(
    [...items]
      .sort((a, b) => Math.abs(b.fac) - Math.abs(a.fac))
      .slice(0, 14)
      .map((it) => it.name),
  );

  const option = React.useMemo(() => {
    if (!items.length) return null;
    const data = items.map((it) => ({
      value: [it.fac, it.fbreadth, it.n] as [number, number, number],
      name: it.name,
      verdict: it.verdict,
      mbreadth: it.mbreadth,
      n_scored: it.n_scored,
      itemStyle: {
        color: VERDICT_COLORS[it.verdict] ?? VERDICT_COLORS.neutral,
        opacity: 0.78,
        borderColor: it.name === activeName ? '#7c3aed' : undefined,
        borderWidth: it.name === activeName ? 2 : 0,
      },
      label: {
        show: topLabels.has(it.name),
        position: 'top',
        fontSize: 8,
        color: '#475569',
        formatter: short(it.name, 6),
      },
    }));
    return {
      tooltip: {
        textStyle: { fontSize: 10 },
        formatter: (p: { data?: (typeof data)[number] }) => {
          const d = p.data;
          if (!d) return '';
          const meta = verdictMeta(d.verdict);
          return [
            `<b>${d.name}</b>`,
            `判定 ${meta.label}`,
            `板块合成分 ${d.value[0].toFixed(4)}`,
            `个股广度 ${(d.value[1] * 100).toFixed(0)}% · 模型广度 ${d.mbreadth === null ? MISSING : `${(d.mbreadth * 100).toFixed(0)}%`}`,
            `成分 ${d.value[2]} 只 · 有评分 ${d.n_scored ?? MISSING} 只`,
          ].join('<br/>');
        },
      },
      grid: { left: 46, right: 16, top: 18, bottom: 30 },
      xAxis: {
        type: 'value',
        name: '板块合成分',
        nameTextStyle: { fontSize: 9, color: '#94a3b8' },
        axisLabel: AXIS,
        splitLine: GRID_LINE,
      },
      yAxis: {
        type: 'value',
        name: '个股广度',
        nameTextStyle: { fontSize: 9, color: '#94a3b8' },
        min: 0,
        max: 1,
        axisLabel: { ...AXIS, formatter: (v: number) => `${(v * 100).toFixed(0)}%` },
        splitLine: GRID_LINE,
      },
      series: [
        {
          type: 'scatter',
          data,
          symbolSize: (v: [number, number, number]) => Math.min(18, 6 + Math.sqrt(Math.max(1, v[2])) * 0.8),
          labelLayout: { hideOverlap: true },
          emphasis: { scale: 1.35, label: { show: true } },
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { color: '#cbd5e1', type: 'dashed' },
            label: { fontSize: 8, color: '#94a3b8' },
            data: [
              ...(group?.q67 === null || group?.q67 === undefined
                ? []
                : [{ xAxis: group.q67, label: { formatter: 'q67' } }]),
              ...(group?.q33 === null || group?.q33 === undefined
                ? []
                : [{ xAxis: group.q33, label: { formatter: 'q33' } }]),
              { yAxis: 0.5, label: { formatter: '广度 50%' } },
            ],
          },
        },
      ],
    };
  }, [items, group?.q67, group?.q33, activeName, topLabels]);

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2 flex-wrap px-1 text-[10px] font-bold text-slate-500">
        板块共振散点
        <span className="font-normal text-slate-400">x=合成分 · y=个股广度 · 气泡=成分数 · 虚线=判定边界</span>
        <span className="ml-auto flex items-center gap-1.5 font-normal text-[9px] text-slate-400">
          {Object.entries(VERDICT_COLORS).map(([k, c]) => (
            <span key={k} className="inline-flex items-center gap-0.5">
              <span className="w-1.5 h-1.5 rounded-full" style={{ background: c }} />
              {verdictMeta(k).label}
            </span>
          ))}
        </span>
      </div>
      <BoardChart
        option={option}
        height={340}
        emptyText="该分类无可用板块（均分/广度不足）"
        onEvents={{
          click: (p: unknown) => {
            const name = (p as { data?: { name?: string } })?.data?.name;
            if (name) onPick(name);
          },
        }}
      />
    </div>
  );
};

/** IC60 横条：入选因子的方向与强度 */
export const FactorICBar: React.FC<{ factors: FactorBoardFactor[] }> = ({ factors }) => {
  const top = factors
    .filter((f) => f.ic60 !== null)
    .sort((a, b) => Math.abs(b.ic60 as number) - Math.abs(a.ic60 as number))
    .slice(0, 18);

  const option = React.useMemo(() => {
    if (!top.length) return null;
    const ordered = [...top].reverse();
    return {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        textStyle: { fontSize: 10 },
        formatter: (ps: Array<{ dataIndex: number }>) => {
          const f = ordered[ps[0]?.dataIndex ?? 0];
          if (!f) return '';
          return [
            `<b>${f.display}</b> <span style="color:#94a3b8">${f.name}</span>`,
            `IC60 ${f.ic60?.toFixed(4) ?? MISSING} · ICIR ${f.icir60?.toFixed(3) ?? MISSING} · t ${f.t60?.toFixed(1) ?? MISSING}`,
            `胜率 ${f.win60 === null ? MISSING : `${(f.win60 * 100).toFixed(0)}%`} · 权重 ${f.weight === null ? MISSING : `${(f.weight * 100).toFixed(1)}%`}`,
            `衰减 ${decayMeta(f.decay ?? null).label}`,
          ].join('<br/>');
        },
      },
      grid: { left: 116, right: 40, top: 8, bottom: 20 },
      xAxis: { type: 'value', axisLabel: AXIS, splitLine: GRID_LINE },
      yAxis: {
        type: 'category',
        data: ordered.map((f) => short(f.display, 10)),
        axisLabel: { fontSize: 9, color: '#64748b' },
        axisTick: { show: false },
        axisLine: { lineStyle: { color: '#cbd5e1' } },
      },
      series: [
        {
          type: 'bar',
          barMaxWidth: 12,
          data: ordered.map((f) => ({
            value: f.ic60,
            name: f.display,
            itemStyle: {
              color: (f.ic60 ?? 0) >= 0 ? '#dc2626' : '#16a34a',
              borderRadius: (f.ic60 ?? 0) >= 0 ? [0, 3, 3, 0] : [3, 0, 0, 3],
            },
          })),
          label: {
            show: true,
            position: 'right',
            fontSize: 9,
            color: '#64748b',
            formatter: (p: { value: number | null }) => (p.value === null ? '' : p.value.toFixed(3)),
          },
        },
      ],
    };
  }, [top]);

  return (
    <div className="min-w-0">
      <div className="text-[10px] font-bold text-slate-500 px-1 pb-1">
        因子 IC60 Top {top.length} <span className="font-normal text-slate-400">红=正向 · 绿=反向</span>
      </div>
      <BoardChart option={option} height={Math.max(200, top.length * 18 + 34)} emptyText="暂无可评估因子" />
    </div>
  );
};

/** IC60 × ICIR：横轴强度、纵轴稳定度，气泡=权重 */
export const FactorICIRScatter: React.FC<{ factors: FactorBoardFactor[] }> = ({ factors }) => {
  const points = factors.filter((f) => f.ic60 !== null && f.icir60 !== null);
  const topLabels = new Set(
    [...points]
      .sort((a, b) => Math.abs(b.ic60 as number) - Math.abs(a.ic60 as number))
      .slice(0, 10)
      .map((f) => `${f.lib}/${f.name}`),
  );

  const option = React.useMemo(() => {
    if (!points.length) return null;
    const data = points.map((f) => ({
      value: [f.ic60 as number, f.icir60 as number, (f.weight ?? 0.05) * 100],
      name: f.display,
      key: `${f.lib}/${f.name}`,
      decay: f.decay ?? null,
      t: f.t60,
      win: f.win60,
      itemStyle: { color: DECAY_COLORS[f.decay ?? ''] ?? DECAY_COLORS.insufficient, opacity: 0.82 },
      label: {
        show: topLabels.has(`${f.lib}/${f.name}`),
        position: 'top',
        fontSize: 8,
        color: '#475569',
        formatter: short(f.display, 7),
      },
    }));
    return {
      tooltip: {
        textStyle: { fontSize: 10 },
        formatter: (p: { data?: (typeof data)[number] }) => {
          const d = p.data;
          if (!d) return '';
          return [
            `<b>${d.name}</b>`,
            `IC60 ${d.value[0].toFixed(4)} · ICIR ${d.value[1].toFixed(3)}`,
            `t ${d.t === null || d.t === undefined ? MISSING : d.t.toFixed(1)} · 胜率 ${d.win === null ? MISSING : `${(d.win * 100).toFixed(0)}%`}`,
            `权重 ${d.value[2].toFixed(1)}% · 衰减 ${decayMeta(d.decay).label}`,
          ].join('<br/>');
        },
      },
      grid: { left: 46, right: 18, top: 18, bottom: 30 },
      xAxis: {
        type: 'value',
        name: 'IC60',
        nameTextStyle: { fontSize: 9, color: '#94a3b8' },
        axisLabel: AXIS,
        splitLine: GRID_LINE,
      },
      yAxis: {
        type: 'value',
        name: 'ICIR',
        nameTextStyle: { fontSize: 9, color: '#94a3b8' },
        axisLabel: AXIS,
        splitLine: GRID_LINE,
      },
      series: [
        {
          type: 'scatter',
          data,
          symbolSize: (v: [number, number, number]) => Math.min(18, 7 + Math.max(0, v[2]) * 0.35),
          labelLayout: { hideOverlap: true },
          emphasis: { scale: 1.3 },
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { color: '#cbd5e1', type: 'dashed' },
            label: { fontSize: 8, color: '#94a3b8' },
            data: [{ xAxis: 0 }, { yAxis: 0 }],
          },
        },
      ],
    };
  }, [points, topLabels]);

  return (
    <div className="min-w-0">
      <div className="flex items-center gap-2 px-1 pb-1 text-[10px] font-bold text-slate-500">
        IC60 × ICIR
        <span className="font-normal text-slate-400">气泡=权重 · 右上=又强又稳</span>
        <span className="ml-auto flex items-center gap-1.5 font-normal text-[9px] text-slate-400">
          {Object.entries(DECAY_COLORS).map(([k, c]) => (
            <span key={k} className="inline-flex items-center gap-0.5">
              <span className="w-1.5 h-1.5 rounded-full" style={{ background: c }} />
              {decayMeta(k).label}
            </span>
          ))}
        </span>
      </div>
      <BoardChart option={option} height={320} emptyText="暂无可评估因子" />
    </div>
  );
};
