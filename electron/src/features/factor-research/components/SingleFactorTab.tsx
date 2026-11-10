/**
 * 因子研究 —— 单因子页签：定义 + 多档持仓数（top-N）净值/KPI 叠加对比 +
 * 期末净值 vs 持仓数扫描 + 月频 IC + 最新截面个股表（含行业/市值分布）。
 *
 * 基准面市场化（2026-10）：基准候选清单与名称一律取详情响应的 `benchmarks`
 * （后端按市场下发，CN 三码 / HK 恒生系 / US 标普系 / 期货为空表）；
 * 超额列默认对**本市场主基准**（清单首位），用户可多选叠加。
 */
import React, { useEffect, useMemo, useState } from 'react';
import { ArrowUpRight, LineChart, Plus, X } from 'lucide-react';
import { getFactorDetail } from '../services/factorResearchService';
import type { FactorDataset, RangeParams } from '../services/factorResearchService';
import { getFactorDatasets } from '../services/factorReportService';
import type { FactorDetail, FactorKpi, ResearchMarket } from '../types/factorResearch';
import { Card, DistBars, fmtNum, fmtPct, IcChart, NScanChart, NavChart, TagChip } from './common';

interface Props {
  code: string | null;
  range: RangeParams;
  dataset: FactorDataset;
  /** 本页当前市场（详情/报告清单按市场） */
  market: ResearchMarket;
  /**
   * 「因子报告」跳转：父级切到因子报告页签并按 (报告数据集, 因子名) 深链
   * —— 与筛选页的「报告」按钮同一入口（openReport）。不传则不渲染按钮。
   */
  onOpenReport?: (reportDataset: string, code: string) => void;
}

const N_PRESETS = [5, 10, 30, 50];
/** CN 三码基准的线色（历史固定映射，保持 A 股观感不变） */
const BENCH_COLOR: Record<string, string> = {
  '000300.SH': '#94a3b8',
  '000906.SH': '#cbd5e1',
  '000905.SH': '#a8a29e',
};
/** 非 CN 基准（HSI/HSCEI/SPX…代码随市场）按候选顺序取同一灰阶家族：
 *  全落同一个灰等于两条虚线分不开，而基准是叠着看的。 */
const BENCH_FALLBACK_SEQ = ['#94a3b8', '#cbd5e1', '#a8a29e', '#64748b'];
const BENCH_COLOR_FALLBACK = '#94a3b8';

function KpiRow({ label, kpi, color, excess, excessNone }: {
  label: string; kpi: FactorKpi; color?: string; excess?: number | null; excessNone?: boolean;
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
      <td
        className={`py-1 text-right font-mono ${
          kpi.fitness == null ? 'text-slate-300' : kpi.fitness >= 0 ? 'text-rose-600' : 'text-emerald-600'
        }`}
        title={
          kpi.fitness == null
            ? '样本不足 6 个月或序列退化，Fitness 不可计算'
            : `${kpi.fitness_grade ?? ''}｜可对账输入：毛 IR ${fmtNum(kpi.brain_ir)} · 简单年化毛收益 ${fmtPct(kpi.brain_returns)} · 日均单边换手 ${fmtPct(kpi.brain_turnover, 2)}`
        }
      >
        {fmtNum(kpi.fitness)}
      </td>
      <td className="py-1 text-right font-mono text-slate-500">{fmtPct(kpi.max_drawdown)}</td>
      <td className="py-1 text-right font-mono text-slate-500">{fmtPct(kpi.win_rate)}</td>
      <td className={`py-1 text-right font-mono ${(excess || 0) >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
        {excessNone || excess === undefined ? '—' : fmtPct(excess)}
      </td>
    </tr>
  );
}

export const SingleFactorTab: React.FC<Props> = ({ code, range, dataset, market, onOpenReport }) => {
  const [detail, setDetail] = useState<FactorDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nsList, setNsList] = useState<number[]>([10, 30]);
  const [addN, setAddN] = useState('');
  /** null = 用户未手动选过 → 用本市场主基准（清单首位）；[] = 用户主动全部取消 */
  const [benchOn, setBenchOn] = useState<string[] | null>(null);
  /**
   * 因子报告那边**有快照**的数据集（dataset → label）。只有它才能证明
   * 「因子报告」入口点了会落到这个因子上：报告快照是另一个脚本按自己的清单
   * 生成的，数据集没快照或没这个因子时，入口要么指向重建横幅要么指向一屏
   * 「该因子不在快照里」。取不到（失败/未加载）= 没有入口 —— 与筛选页同口径
   * （少一个按钮，好过渲染一个点了不落到本因子的跳转）。
   */
  const [reportDatasets, setReportDatasets] = useState<Map<string, string> | null>(null);

  useEffect(() => {
    setNsList([10, 30]);
    setAddN('');
    setBenchOn(null); // 换因子/换市场：基准选择复位到新清单的主基准
  }, [code, market]);

  // 报告数据集清单按市场下发（各市场可用的数据集不同），换市场必须作废重取 ——
  // 不清的话下面「已拿到就不再问」的闸门会把上一市场的地图锁死一整个会话。
  useEffect(() => {
    setReportDatasets(null);
  }, [market]);

  useEffect(() => {
    if (!code) return;
    let alive = true;
    setLoading(true);
    setError(null);
    getFactorDetail(code, nsList, range, 30, dataset, market)
      .then((d) => { if (alive) setDetail(d); })
      .catch((e: unknown) => { if (alive) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [code, nsList.join(','), range.start, range.end, dataset, market]); // eslint-disable-line react-hooks/exhaustive-deps

  // 报告数据集可用性：只有该因子确有来源库时才去问（经典库连请求都不发）。
  // 清单一次成功即服务所有库（dataset → label 是全局面，与当前库无关）——
  // 拿到后不再重复问。失败**不落任何状态**：换因子（code 变）就是一次新的
  // 定位机会，会再试一次 —— 引擎重启/超时恢复后入口能自愈，而不是整个会话
  // 都不出现（catalog 这个端点逐数据集扫盘，不适合每次换因子都问一遍）。
  useEffect(() => {
    const lib = detail?.source_library;
    if (!lib || reportDatasets) return;
    let alive = true;
    getFactorDatasets(market)
      .then((res) => {
        if (!alive) return;
        const m = new Map<string, string>();
        (res.items || []).forEach((d) => { if (d.available) m.set(d.dataset, d.label); });
        setReportDatasets(m);
      })
      .catch(() => undefined); // 取不到 = 不渲染入口（不是错误面，因子页本身完好）
    return () => { alive = false; };
  }, [detail?.source_library, code, reportDatasets, market]);

  const colors = useMemo(() => {
    const base = ['#2563eb', '#e11d48', '#059669', '#d97706', '#7c3aed', '#0891b2'];
    const m = new Map<number, string>();
    nsList.forEach((n, i) => m.set(n, base[i % base.length]));
    return m;
  }, [nsList]);

  /** 「因子报告」跳转目标：来源库在可用报告数据集里才有值（label 顺带作提示文案）。 */
  const reportTarget = useMemo(() => {
    const lib = detail?.source_library || '';
    if (!lib) return null;
    const label = reportDatasets?.get(lib);
    return label ? { dataset: lib, label } : null;
  }, [detail?.source_library, reportDatasets]);

  const addVariant = (n: number) => {
    if (!Number.isFinite(n) || n < 1 || n > 100) return;
    if (nsList.includes(n)) {
      setAddN('');
      return;
    }
    setNsList([...nsList, n].sort((a, b) => a - b));
    setAddN('');
  };

  /**
   * 已选基准代码。用户未手动设置（null）→ 默认本市场主基准（详情清单首位，
   * 后端按 bench_order 下发）；无基准市场（期货）→ 空数组，不给菜单也不叠线。
   */
  const activeBenches = useMemo(() => {
    if (benchOn !== null) return benchOn;
    const first = detail?.benchmarks?.[0]?.code;
    return first ? [first] : [];
  }, [benchOn, detail]);
  /** 超额列口径基准 = 已选首位（取消全部时无口径，列显示「—」） */
  const excessBench = useMemo(
    () => detail?.benchmarks.find((b) => b.code === activeBenches[0]) ?? null,
    [detail, activeBenches],
  );

  /** 基准线色（CN 走固定映射，其余市场按候选顺序取灰阶序列） */
  const benchColors = useMemo(() => {
    const m = new Map<string, string>();
    (detail?.benchmarks ?? []).forEach((b, i) => {
      m.set(b.code, BENCH_COLOR[b.code] ?? BENCH_FALLBACK_SEQ[i % BENCH_FALLBACK_SEQ.length]);
    });
    return m;
  }, [detail]);

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
          {detail.variants.length === 0 && (
            <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
              {detail.unavailable_reason || '该因子在所选窗口内无有效截面'}
              <span className="ml-1 text-amber-500">（换一个窗口或看该因子的首个有效月）</span>
            </div>
          )}
          {/* 定义 */}
          <Card
            title="因子定义"
            className="shrink-0"
            extra={
              // !loading：换因子时旧卡面会停留到新数据落地，此刻按钮仍属**上一个**
              // 因子 —— 藏起来，别让一次点击跳到刚离开的那个因子的报告。
              reportTarget && onOpenReport && !loading ? (
                <button
                  type="button"
                  data-testid="open-factor-report"
                  onClick={() => onOpenReport(reportTarget.dataset, detail.code)}
                  title={`在因子报告「${reportTarget.label}」里打开 ${detail.code}`}
                  className="flex items-center gap-1 rounded-full border border-indigo-200 bg-indigo-50 px-2.5 py-0.5 text-[10px] font-bold text-indigo-600 hover:bg-indigo-100"
                >
                  <LineChart className="w-3 h-3" />
                  因子报告
                  <ArrowUpRight className="w-3 h-3" />
                </button>
              ) : undefined
            }
          >
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
            {/* 基准候选 = 本市场清单（后端下发）；期货无基准表 → 整段不渲染 */}
            {benchRows.length > 0 && (
              <>
                <span className="text-[10px] font-bold text-slate-400">基准</span>
                {benchRows.map((b) => {
                  const on = activeBenches.includes(b.code);
                  return (
                    <button
                      key={b.code}
                      onClick={() =>
                        setBenchOn(on ? activeBenches.filter((x) => x !== b.code) : [...activeBenches, b.code])
                      }
                      className={`rounded-full border px-2 py-0.5 text-[10px] font-bold ${
                        on ? 'border-slate-300 bg-slate-100 text-slate-600' : 'border-slate-200 bg-white text-slate-400 hover:bg-slate-50'
                      }`}
                    >
                      {b.name}
                    </button>
                  );
                })}
              </>
            )}
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
                    .filter((b) => activeBenches.includes(b.code))
                    .map((b) => ({ name: b.name, data: b.nav, color: benchColors.get(b.code) ?? BENCH_COLOR_FALLBACK, dashed: true })),
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
                        <th className="text-right py-1" title="世坤 BRAIN 口径：IR × √(|Returns| / max(Turnover, 0.125))；月频折算，悬停数值看分级">Fitness</th>
                        <th className="text-right py-1">最大回撤</th>
                        <th className="text-right py-1">月度胜率</th>
                        <th className="text-right py-1">{excessBench ? `超额 vs ${excessBench.name}` : '超额 vs 基准'}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.variants.map((v) => (
                        <KpiRow
                          key={v.n}
                          label={`top${v.n}`}
                          kpi={v.kpi}
                          color={colors.get(v.n)}
                          excess={excessBench ? v.excess[excessBench.code] : null}
                          excessNone={!excessBench}
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
                  <span className="ml-2">Fitness 为 BRAIN 粗筛口径（月频折算；悬停数值看分级与三个输入）</span>
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
                      {/* 行业口径随市场：CN=申万（instrument_detail）；非 CN=该市场板块维度 */}
                      <th className="text-left py-1 w-20">{market === 'CN' ? '申万行业' : '行业'}</th>
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
              <Card title={market === 'CN' ? '选出股票的行业分布（申万二级）' : '选出股票的行业分布'}>
                <DistBars rows={detail.industry_dist.slice(0, 12)} color="#6366f1" height={128} />
              </Card>
              <Card title="市值分布（按总市值分档）">
                <DistBars rows={detail.cap_dist} color="#0891b2" height={128} />
              </Card>
            </div>
          </div>
          <div className="shrink-0 text-[10px] text-slate-300 pb-1">
            {/* ST/退市口径是 A 股专属（instrument_detail）；非 CN 只做历史有效数据过滤 */}
            月末收盘调仓、Top-N 等权、{market === 'CN' ? '剔除 ST/退市、' : '仅按历史有效数据过滤（ST/退市口径为 A 股专属）、'}
            双边成本 0.2%（按换手计）·「Top 30 股票」始终为该因子最近可用截面 ·
            快照由 {dataset === 'classic' ? 'build_factor_research.py' : 'build_factor_panel_private.py'} 构建
          </div>
        </>
      ) : null}
    </div>
  );
};
