/**
 * 因子研究 —— 单因子页签：定义 + 多档持仓数（top-N）净值/KPI 叠加对比 +
 * 期末净值 vs 持仓数扫描 + 月频 IC + 最新截面个股表（含行业/市值分布）。
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Plus, X } from 'lucide-react';
import { getFactorDetail } from '../services/factorResearchService';
import type { FactorDataset, RangeParams } from '../services/factorResearchService';
import type { FactorDetail, FactorKpi } from '../types/factorResearch';
import { Card, DistBars, fmtNum, fmtPct, IcChart, NScanChart, NavChart, TagChip } from './common';

interface Props {
  code: string | null;
  range: RangeParams;
  dataset: FactorDataset;
}

const N_PRESETS = [5, 10, 30, 50];
const BENCH_COLOR: Record<string, string> = {
  '000300.SH': '#94a3b8',
  '000906.SH': '#cbd5e1',
  '000905.SH': '#a8a29e',
};

function KpiRow({ label, kpi, color, excess300 }: {
  label: string; kpi: FactorKpi; color?: string; excess300?: number | null;
}) {
  return (
    <tr className="border-t border-slate-100">
      <td className="py-1">
        {color && <span className="inline-block w-2 h-2 rounded-full mr-1.5" style={{ backgroundColor: color }} />}
        <span className="font-bold text-slate-700">{label}</span>
      </td>
      <td className={`py-1 text-right font-mono ${(kpi.annual_return || 0) >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
        {fmtPct(kpi.annual_return)}
      </td>
      <td className="py-1 text-right font-mono text-slate-600">{fmtNum(kpi.sharpe)}</td>
      <td className="py-1 text-right font-mono text-slate-500">{fmtPct(kpi.max_drawdown)}</td>
      <td className="py-1 text-right font-mono text-slate-500">{fmtPct(kpi.win_rate)}</td>
      <td className={`py-1 text-right font-mono ${(excess300 || 0) >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
        {excess300 === undefined ? '—' : fmtPct(excess300)}
      </td>
    </tr>
  );
}

export const SingleFactorTab: React.FC<Props> = ({ code, range, dataset }) => {
  const [detail, setDetail] = useState<FactorDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nsList, setNsList] = useState<number[]>([10, 30]);
  const [addN, setAddN] = useState('');
  const [benchOn, setBenchOn] = useState<string[]>(['000300.SH']);

  useEffect(() => {
    setNsList([10, 30]);
    setAddN('');
  }, [code]);

  useEffect(() => {
    if (!code) return;
    let alive = true;
    setLoading(true);
    setError(null);
    getFactorDetail(code, nsList, range, 30, dataset)
      .then((d) => { if (alive) setDetail(d); })
      .catch((e: unknown) => { if (alive) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [code, nsList.join(','), range.start, range.end, dataset]); // eslint-disable-line react-hooks/exhaustive-deps

  const colors = useMemo(() => {
    const base = ['#2563eb', '#e11d48', '#059669', '#d97706', '#7c3aed', '#0891b2'];
    const m = new Map<number, string>();
    nsList.forEach((n, i) => m.set(n, base[i % base.length]));
    return m;
  }, [nsList]);

  const addVariant = (n: number) => {
    if (!Number.isFinite(n) || n < 1 || n > 100) return;
    if (nsList.includes(n)) {
      setAddN('');
      return;
    }
    setNsList([...nsList, n].sort((a, b) => a - b));
    setAddN('');
  };

  if (!code) {
    return <div className="flex-1 flex items-center justify-center text-xs text-slate-400">← 从左侧目录选择因子</div>;
  }

  const benchRows = detail?.benchmarks || [];

  return (
    <div className="flex-1 min-w-0 min-h-0 overflow-y-auto custom-scrollbar flex flex-col gap-2 pr-0.5">
      {error ? (
        <div className="flex-1 flex items-center justify-center text-xs text-rose-500">{error}</div>
      ) : loading && !detail ? (
        <div className="flex-1 rounded-2xl bg-slate-50 animate-pulse" />
      ) : detail ? (
        <>
          {/* 定义 */}
          <Card title="因子定义" className="shrink-0">
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-base font-extrabold text-slate-800">{detail.name_cn}</span>
                <span className="font-mono text-[11px] text-slate-400">{detail.code}</span>
                {detail.display_name && detail.display_name !== detail.name_cn && (
                  <span className="text-[10px] text-slate-400">{detail.display_name}</span>
                )}
                <span className="rounded-full bg-slate-100 border border-slate-200 px-2 py-[1px] text-[10px] font-bold text-slate-500">
                  {detail.l1} · {detail.l2}
                </span>
                <span
                  className={`rounded-full px-2 py-[1px] text-[10px] font-bold ${
                    detail.direction === 1
                      ? 'bg-rose-50 text-rose-600 border border-rose-100'
                      : 'bg-emerald-50 text-emerald-600 border border-emerald-100'
                  }`}
                >
                  方向：{detail.direction === 1 ? '↑ 越大越好' : '↓ 越小越好'}
                </span>
                <TagChip tag={detail.env_tag} />
                <TagChip tag={detail.time_tag} />
                <span className="text-[9px] text-slate-400">标签按当前区间自动判定</span>
              </div>
              <p className="mt-1.5 text-[11px] text-slate-500 leading-relaxed whitespace-pre-line">{detail.description}</p>
              {detail.formula && (
                <pre className="mt-1.5 rounded-lg bg-slate-50 border border-slate-100 px-2.5 py-1.5 text-[10px] text-slate-600 font-mono whitespace-pre-wrap">
                  {detail.formula}
                </pre>
              )}
              {detail.wind_source && <div className="mt-1 text-[9px] text-slate-400">数据源（demo 原口径）：{detail.wind_source}</div>}
            </div>
          </Card>

          {/* 持仓数选择 */}
          <div className="shrink-0 flex items-center gap-2 flex-wrap rounded-xl border border-slate-200/80 bg-white px-3 py-1.5">
            <span className="text-[10px] font-bold text-slate-400">持仓数</span>
            {N_PRESETS.map((n) => {
              const on = nsList.includes(n);
              return (
                <button
                  key={n}
                  onClick={() => (on ? setNsList(nsList.filter((x) => x !== n)) : addVariant(n))}
                  className={`rounded-full border px-2.5 py-0.5 text-[11px] font-bold transition-colors ${
                    on
                      ? 'border-blue-200 bg-blue-50 text-blue-600'
                      : 'border-slate-200 bg-white text-slate-500 hover:bg-slate-50'
                  }`}
                >
                  {n}
                </button>
              );
            })}
            <span className="flex items-center gap-1">
              <input
                type="number"
                min={1}
                max={100}
                value={addN}
                onChange={(e) => setAddN(e.target.value)}
                placeholder="1-100"
                className="w-16 rounded-lg border border-slate-200 px-1.5 py-0.5 text-[11px] font-mono text-right placeholder:text-slate-300"
              />
              <button
                onClick={() => addVariant(Number(addN))}
                disabled={!addN}
                className="flex items-center gap-0.5 rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[10px] font-bold text-indigo-600 hover:bg-indigo-100 disabled:opacity-40"
              >
                <Plus className="w-3 h-3" /> 加入
              </button>
            </span>
            <span className="text-[10px] text-slate-400 ml-1">已选：</span>
            {nsList.map((n) => (
              <span
                key={n}
                className="inline-flex items-center gap-1 rounded-full border px-2 py-[1px] text-[10px] font-bold"
                style={{ borderColor: `${colors.get(n)}55`, color: colors.get(n), backgroundColor: `${colors.get(n)}10` }}
              >
                top{n}
                {nsList.length > 1 && (
                  <button onClick={() => setNsList(nsList.filter((x) => x !== n))} className="hover:opacity-70">
                    <X className="w-2.5 h-2.5" />
                  </button>
                )}
              </span>
            ))}
            <div className="flex-1" />
            <span className="text-[10px] font-bold text-slate-400">基准</span>
            {['000300.SH', '000906.SH', '000905.SH'].map((c) => {
              const on = benchOn.includes(c);
              const names: Record<string, string> = { '000300.SH': '沪深300', '000906.SH': '中证800', '000905.SH': '中证500' };
              return (
                <button
                  key={c}
                  onClick={() => setBenchOn(on ? benchOn.filter((x) => x !== c) : [...benchOn, c])}
                  className={`rounded-full border px-2 py-0.5 text-[10px] font-bold ${
                    on ? 'border-slate-300 bg-slate-100 text-slate-600' : 'border-slate-200 bg-white text-slate-400 hover:bg-slate-50'
                  }`}
                >
                  {names[c]}
                </button>
              );
            })}
          </div>

          {/* 净值 + KPI */}
          <div className="shrink-0 grid grid-cols-1 xl:grid-cols-2 gap-2">
            <Card
              title={`组合净值对比（区间 ${detail.range.start} ~ ${detail.range.end}，起点=1.0）`}
              extra={<span className="text-[10px] text-slate-400">月末等权 · 0.2% 双边成本</span>}
            >
              <NavChart
                series={[
                  ...detail.variants.map((v) => ({
                    name: `top${v.n}`,
                    data: v.nav,
                    color: colors.get(v.n),
                  })),
                  ...benchRows
                    .filter((b) => benchOn.includes(b.code))
                    .map((b) => ({ name: b.name, data: b.nav, color: BENCH_COLOR[b.code], dashed: true })),
                ]}
                height={210}
              />
            </Card>
            <Card title="组合指标" extra={<span className="text-[10px] text-slate-400">区间内重建净值</span>}>
              <div className="h-full flex flex-col min-h-0">
                <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar">
                  <table className="w-full text-[11px]">
                    <thead className="sticky top-0 bg-white">
                      <tr className="text-slate-400 font-bold">
                        <th className="text-left py-1">组合 / 基准</th>
                        <th className="text-right py-1">年化收益</th>
                        <th className="text-right py-1">夏普</th>
                        <th className="text-right py-1">最大回撤</th>
                        <th className="text-right py-1">月度胜率</th>
                        <th className="text-right py-1">超额 vs 沪深300</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.variants.map((v) => (
                        <KpiRow
                          key={v.n}
                          label={`top${v.n}`}
                          kpi={v.kpi}
                          color={colors.get(v.n)}
                          excess300={v.excess['000300.SH']}
                        />
                      ))}
                      {benchRows.map((b) => (
                        <KpiRow key={b.code} label={b.name} kpi={b.kpi || {}} />
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="shrink-0 pt-1 text-[10px] text-slate-400">
                  区间内 RankIC {fmtNum(detail.ic_kpi.ic_mean, 4)} · IC_IR {fmtNum(detail.ic_kpi.ic_ir)}（与持仓数无关）
                </div>
              </div>
            </Card>
          </div>

          {/* IC + N 扫描 */}
          <div className="shrink-0 grid grid-cols-1 xl:grid-cols-2 gap-2">
            <Card title="月频 RankIC（Spearman）" extra={<span className="text-[10px] text-slate-400">柱=当月 · 线=12期均值</span>}>
              <IcChart points={detail.ic} height={190} />
            </Card>
            <Card
              title={`期末净值 vs 持仓数（top-1 ~ top-${detail.nscan.length} 全扫描）`}
              extra={<span className="text-[10px] text-slate-400">虚线=1.0（不赚不亏）</span>}
            >
              <NScanChart rows={detail.nscan} height={190} />
            </Card>
          </div>

          {/* 个股表 + 分布 */}
          <div className="shrink-0 grid grid-cols-1 xl:grid-cols-3 gap-2">
            <Card
              title={
                detail.stocks_date
                  ? `Top 30 股票（截面日 ${detail.stocks_date}${
                      detail.stocks_stale ? '，该因子最近可用截面' : ''
                    }，按得分降序）`
                  : 'Top 30 股票（暂无截面数据）'
              }
              className="xl:col-span-2"
              extra={<span className="text-[10px] text-slate-400">市值/PE/PB 为当日快照 · 成交额=近 252 交易日日均</span>}
            >
              {detail.stocks_stale && (
                <div className="mb-1.5 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1 text-[10px] leading-relaxed text-amber-700">
                  该因子数据未更新到最新截面，以下为它最近一个有数据的截面（{detail.stocks_date}），行业/市值分布同口径。
                </div>
              )}
              <div className="h-full min-h-0 overflow-auto custom-scrollbar" style={{ maxHeight: 320 }}>
                <table className="w-full text-[10px]">
                  <thead className="sticky top-0 bg-white">
                    <tr className="text-slate-400 font-bold">
                      <th className="text-left py-1 w-6">#</th>
                      <th className="text-left py-1 w-14">代码</th>
                      <th className="text-left py-1">名称</th>
                      <th className="text-left py-1 w-20">申万行业</th>
                      <th className="text-right py-1 w-16">市值(亿)</th>
                      <th className="text-right py-1 w-14">PE</th>
                      <th className="text-right py-1 w-10">PB</th>
                      <th className="text-right py-1 w-20">日均成交额(亿)</th>
                      <th className="text-right py-1 w-14">得分</th>
                      <th className="text-right py-1 w-16">原始值</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.stocks.length === 0 && (
                      <tr>
                        <td colSpan={10} className="py-3 text-center text-slate-400">
                          该因子在当前面板区间内没有可用的截面数据
                        </td>
                      </tr>
                    )}
                    {detail.stocks.map((s) => (
                      <tr key={s.symbol} className="border-t border-slate-50 hover:bg-slate-50/60">
                        <td className="py-[3px] text-slate-400 font-mono">{s.rank}</td>
                        <td className="py-[3px] font-mono text-slate-500">{s.symbol}</td>
                        <td className="py-[3px] font-bold text-slate-700">{s.name || '—'}</td>
                        <td className="py-[3px] text-slate-500">{s.industry || '—'}</td>
                        <td className="py-[3px] text-right font-mono text-slate-600">{fmtNum(s.total_mv_yi, 1)}</td>
                        <td className="py-[3px] text-right font-mono text-slate-600">{fmtNum(s.pe_ttm, 1)}</td>
                        <td className="py-[3px] text-right font-mono text-slate-600">{fmtNum(s.pb, 2)}</td>
                        <td className="py-[3px] text-right font-mono text-slate-600">{fmtNum(s.avg_amount_yi, 2)}</td>
                        <td className="py-[3px] text-right font-mono font-bold text-indigo-600">{fmtNum(s.score, 2)}</td>
                        <td className="py-[3px] text-right font-mono text-slate-500">{fmtNum(s.raw, 4)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
            <div className="flex flex-col gap-2 min-h-0">
              <Card title="选出股票的行业分布（申万二级）">
                <DistBars rows={detail.industry_dist.slice(0, 12)} color="#6366f1" height={128} />
              </Card>
              <Card title="市值分布（按总市值分档）">
                <DistBars rows={detail.cap_dist} color="#0891b2" height={128} />
              </Card>
            </div>
          </div>
          <div className="shrink-0 text-[10px] text-slate-300 pb-1">
            月末收盘调仓、Top-N 等权、剔除 ST/退市、双边成本 0.2%（按换手计）·「Top 30 股票」始终为该因子最近可用截面 ·
            快照由 build_factor_research.py 构建
          </div>
        </>
      ) : null}
    </div>
  );
};
