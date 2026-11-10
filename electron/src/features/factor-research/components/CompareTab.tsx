/**
 * 因子研究 —— 多因子对比页签：每因子可单独设持仓数，区间内净值/KPI/IC 叠加对比 + 相关热力图。
 *
 * 基准面市场化（2026-10）：超额列与净值叠加的基准取**本市场主基准**（= 后端
 * 超额映射的首键，CN 沪深300 / HK 恒生 / US 标普500），名称用响应里下发的
 * bench name；无基准市场（期货）不渲染叠加开关、超额列显示「—」。
 */
import React, { useEffect, useMemo, useState } from 'react';
import { X } from 'lucide-react';
import { EChartsChart } from '../../../components/common/EChartsChart';
import { postCompare } from '../services/factorResearchService';
import type { FactorDataset, RangeParams } from '../services/factorResearchService';
import type { CompareResponse, ResearchMarket } from '../types/factorResearch';
import { Card, FACTOR_COLORS, fmtNum, fmtPct, IcChart, NavChart } from './common';

interface Props {
  codes: string[];
  onRemove: (code: string) => void;
  nameOf: (code: string) => string;
  range: RangeParams;
  dataset: FactorDataset;
  /** 本页当前市场（对比取数按市场） */
  market: ResearchMarket;
}

export const CompareTab: React.FC<Props> = ({ codes, onRemove, nameOf, range, dataset, market }) => {
  const [data, setData] = useState<CompareResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ns, setNs] = useState<Record<string, number>>({});
  const [benchOn, setBenchOn] = useState(true);

  const items = useMemo(
    () => codes.map((c) => ({ code: c, n: ns[c] ?? 30 })),
    [codes, ns],
  );

  useEffect(() => {
    if (!items.length) {
      setData(null);
      return;
    }
    let alive = true;
    setLoading(true);
    setError(null);
    postCompare(items, range, dataset, market)
      .then((d) => { if (alive) setData(d); })
      .catch((e: unknown) => { if (alive) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [JSON.stringify(items), range.start, range.end, dataset, market]); // eslint-disable-line react-hooks/exhaustive-deps

  // 主基准：超额映射（后端按 bench_order 生成）的首键 ↔ 响应 benchmarks 里的名称。
  // 期货无基准表 → 首键缺席 → null：不叠线、超额列「—」。
  const benchPrimary = useMemo(() => {
    const first = data?.factors[0]?.excess;
    const code = first ? Object.keys(first)[0] : undefined;
    if (!code) return null;
    return data?.benchmarks.find((b) => b.code === code) ?? { code, name: code };
  }, [data]);

  const corrMatrix = useMemo(() => {
    if (!data?.corr || !data.factors.length) return null;
    const names = data.factors.map((f) => f.code);
    const idx = new Map(names.map((c, i) => [c, i]));
    const m: number[][] = names.map(() => names.map(() => NaN));
    for (const p of data.corr) {
      const i = idx.get(p.factor_a);
      const j = idx.get(p.factor_b);
      if (i === undefined || j === undefined) continue;
      m[i][j] = p.corr ?? NaN;
      m[j][i] = p.corr ?? NaN;
    }
    return { names, m };
  }, [data]);

  const heatOption = useMemo(() => {
    if (!corrMatrix || corrMatrix.names.length < 2) return null;
    const { names, m } = corrMatrix;
    return {
      grid: { left: 76, right: 16, top: 10, bottom: 56 },
      tooltip: { formatter: (p: { data: [number, number, number] }) => `${names[p.data[1]]} × ${names[p.data[0]]}<br/>相关 ${p.data[2]}` },
      xAxis: { type: 'category', data: names, axisLabel: { fontSize: 9, color: '#94a3b8', rotate: 40 }, axisTick: { show: false } },
      yAxis: { type: 'category', data: names, axisLabel: { fontSize: 9, color: '#94a3b8' }, axisTick: { show: false } },
      visualMap: { show: false, min: -1, max: 1, inRange: { color: ['#047857', '#e2e8f0', '#be123c'] } },
      series: [{
        type: 'heatmap',
        data: m.flatMap((row, i) => row.map((v, j) => [i, j, v])),
        label: { show: names.length <= 8, fontSize: 9, color: '#0f172a', formatter: (p: { data: [number, number, number] }) => (p.data[2] === 1 ? '' : String(p.data[2])) },
        itemStyle: { borderColor: '#fff', borderWidth: 1 },
      }],
    };
  }, [corrMatrix]);

  if (!codes.length) {
    return (
      <div className="flex-1 flex items-center justify-center text-xs text-slate-400">
        从左侧目录或排行榜勾选 ≥2 个因子加入对比
      </div>
    );
  }

  return (
    <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar flex flex-col gap-2 pr-0.5">
      {/* 已选因子 + 持仓数 */}
      <div className="shrink-0 flex items-center gap-2 flex-wrap rounded-xl border border-slate-200/80 bg-white px-3 py-1.5">
        {codes.map((c, i) => (
          <span
            key={c}
            className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-bold"
            style={{
              borderColor: `${FACTOR_COLORS[i % FACTOR_COLORS.length]}55`,
              color: FACTOR_COLORS[i % FACTOR_COLORS.length],
              backgroundColor: `${FACTOR_COLORS[i % FACTOR_COLORS.length]}10`,
            }}
          >
            {nameOf(c)}
            <span className="flex items-center gap-0.5 font-mono text-[10px]">
              N
              <input
                type="number"
                min={1}
                max={100}
                value={ns[c] ?? 30}
                onChange={(e) => setNs({ ...ns, [c]: Math.max(1, Math.min(100, Number(e.target.value) || 30)) })}
                className="w-11 rounded border border-slate-200 bg-white px-1 py-0 text-[10px] text-right"
              />
            </span>
            <button onClick={() => onRemove(c)} className="hover:opacity-70"><X className="w-3 h-3" /></button>
          </span>
        ))}
        <div className="flex-1" />
        {benchPrimary && (
          <button
            onClick={() => setBenchOn(!benchOn)}
            className={`rounded-full border px-2.5 py-0.5 text-[10px] font-bold ${
              benchOn ? 'border-slate-300 bg-slate-100 text-slate-600' : 'border-slate-200 text-slate-400'
            }`}
          >
            叠加{benchPrimary.name}
          </button>
        )}
        <span className="text-[10px] text-slate-400">最多 12 个（超出自动截断）</span>
      </div>

      {error ? (
        <div className="py-10 text-center text-xs text-rose-500">{error}</div>
      ) : loading && !data ? (
        <div className="flex-1 rounded-2xl bg-slate-50 animate-pulse" />
      ) : data ? (
        <>
          {/* KPI 对照表 */}
          <Card
            className="shrink-0"
            title="KPI 对照"
            extra={<span className="text-[10px] text-slate-400">区间 {data.range.start} ~ {data.range.end} · 各自持仓数见上</span>}
          >
            <table className="w-full text-[11px]">
              <thead>
                <tr className="text-slate-400 font-bold">
                  <th className="text-left py-1">因子</th>
                  <th className="text-right py-1 w-12">N</th>
                  <th className="text-right py-1">年化</th>
                  <th className="text-right py-1">夏普</th>
                  <th className="text-right py-1" title="世坤 BRAIN 口径：IR × √(|Returns| / max(Turnover, 0.125))；月频折算">Fitness</th>
                  <th className="text-right py-1">最大回撤</th>
                  <th className="text-right py-1">月胜率</th>
                  <th className="text-right py-1">{benchPrimary ? `超额vs${benchPrimary.name}` : '超额'}</th>
                  <th className="text-right py-1">RankIC</th>
                  <th className="text-right py-1">IC_IR</th>
                </tr>
              </thead>
              <tbody>
                {data.factors.map((f, i) => (
                  <tr key={f.code} className="border-t border-slate-100">
                    <td className="py-1">
                      <span className="inline-block w-2 h-2 rounded-full mr-1.5" style={{ backgroundColor: FACTOR_COLORS[i % FACTOR_COLORS.length] }} />
                      <span className="font-bold text-slate-700">{f.name_cn}</span>
                      <span className="ml-1.5 font-mono text-[10px] text-slate-400">{f.code}</span>
                    </td>
                    <td className="py-1 text-right font-mono text-slate-500">{f.n}</td>
                    <td className={`py-1 text-right font-mono ${(f.kpi.annual_return || 0) >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>{fmtPct(f.kpi.annual_return)}</td>
                    <td className="py-1 text-right font-mono text-slate-600">{fmtNum(f.kpi.sharpe)}</td>
                    <td
                      className={`py-1 text-right font-mono ${
                        f.kpi.fitness == null ? 'text-slate-300' : f.kpi.fitness >= 0 ? 'text-rose-600' : 'text-emerald-600'
                      }`}
                      title={
                        f.kpi.fitness == null
                          ? '样本不足 6 个月或序列退化，Fitness 不可计算'
                          : `${f.kpi.fitness_grade ?? ''}｜可对账输入：毛 IR ${fmtNum(f.kpi.brain_ir)} · 简单年化毛收益 ${fmtPct(f.kpi.brain_returns)} · 日均单边换手 ${fmtPct(f.kpi.brain_turnover, 2)}`
                      }
                    >
                      {fmtNum(f.kpi.fitness)}
                    </td>
                    <td className="py-1 text-right font-mono text-slate-500">{fmtPct(f.kpi.max_drawdown)}</td>
                    <td className="py-1 text-right font-mono text-slate-500">{fmtPct(f.kpi.win_rate)}</td>
                    <td className={`py-1 text-right font-mono ${(benchPrimary ? f.excess[benchPrimary.code] || 0 : 0) >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
                      {fmtPct(benchPrimary ? f.excess[benchPrimary.code] : null)}
                    </td>
                    <td className={`py-1 text-right font-mono ${(f.kpi.ic_mean || 0) >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>{fmtNum(f.kpi.ic_mean, 3)}</td>
                    <td className="py-1 text-right font-mono text-slate-500">{fmtNum(f.kpi.ic_ir)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          {/* 净值叠加 + 相关热力图 */}
          <div className="shrink-0 grid grid-cols-1 xl:grid-cols-2 gap-2">
            <Card title="净值对比（区间内起点=1.0）" extra={<span className="text-[10px] text-slate-400">各因子按自身持仓数</span>}>
              <NavChart
                series={[
                  ...data.factors.map((f, i) => ({
                    name: `${f.name_cn}(top${f.n})`,
                    data: f.nav,
                    color: FACTOR_COLORS[i % FACTOR_COLORS.length],
                  })),
                  ...(benchOn && benchPrimary
                    ? data.benchmarks
                        .filter((b) => b.code === benchPrimary.code)
                        .map((b) => ({ name: b.name, data: b.nav, color: '#94a3b8', dashed: true }))
                    : []),
                ]}
                height={230}
              />
            </Card>
            <Card title="因子相关性（月频秩相关均值 · 全样本）" extra={<span className="text-[10px] text-slate-400">红=正相关 绿=负相关</span>}>
              {heatOption ? (
                <div style={{ height: 230 }}><EChartsChart option={heatOption} /></div>
              ) : (
                <div className="h-[230px] flex items-center justify-center text-xs text-slate-400">需要 ≥2 个因子</div>
              )}
            </Card>
          </div>

          {/* IC 并排 */}
          <div className="shrink-0 grid grid-cols-1 xl:grid-cols-2 gap-2">
            {data.factors.map((f) => (
              <Card key={f.code} title={`${f.name_cn} · 月频 IC`} extra={<span className="text-[10px] text-slate-400">{f.code}</span>}>
                <IcChart points={f.ic} height={140} />
              </Card>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
};
