/**
 * 「相对基准超额」页签：多头组相对主基准的累计超额、超额回撤、分年度与超额分布。
 *
 * ⚠️ 两条铁律：
 * 1. `ex.bench_symbol` 是**实际采用**的基准（点名的取不到时后端顺位回退），与用户点名的 `bench`
 *    不一致必须显式挑明 —— 静默换基准冒充，等于把「沪深300 超额」当「中证1000 超额」给用户看。
 * 2. 取不到就是取不到：一律 `<Degraded reason>`，绝不画 0 也不留白（空图与「值恰好是 0」
 *    在视觉上无法区分，是本项目已有教训）。
 */

import React from 'react';
import { EChartsChart } from '../../../../../components/common/EChartsChart';
import type {
  AnnualExcess, BenchmarkExcess, DrawdownEpisode, ExcessBlock, FactorBlocks, GroupBlock, HistBlock,
} from '../../../types/factorReport';
import {
  ACCENT, ChartShell, DASH, DOWN, Degraded, InfoDot, UP, bySign, catAxis,
  fmtDate, fmtInt, fmtNum, fmtPct, grid, hasData, tooltip, valAxis, zeroLine,
} from './chartKit';

/** 主基准候选：与后端 `blocks.BENCHMARKS` 同序；名称仅在后端没下发时兜底 */
const BENCH_PRESETS = [
  { symbol: '000300.SH', label: '沪深300' },
  { symbol: '000905.SH', label: '中证500' },
  { symbol: '000852.SH', label: '中证1000' },
];

const benchLabel = (symbol: string): string =>
  BENCH_PRESETS.find((b) => b.symbol === symbol)?.label || symbol || DASH;

/** 净值 → 累计收益 %（净值基准 1 即 0%）。缺口原样保留 null，不插值、不断线 */
const navPct = (nav: Array<number | null>): Array<number | null> =>
  nav.map((v) => (v == null || !Number.isFinite(v) ? null : +((v - 1) * 100).toFixed(2)));

/** 超额回撤（%）：超额净值相对**其自身**历史高点的落差，与后端 `max_drawdown` 同口径。
 *  缺口不刷新峰值 —— 停牌/缺数日不产生收益，也就不该抬高基准高点。 */
function drawdownPct(nav: Array<number | null>): Array<number | null> {
  let peak = -Infinity;
  return nav.map((v) => {
    if (v == null || !Number.isFinite(v)) return null;
    if (v > peak) peak = v;
    if (!Number.isFinite(peak) || peak <= 0) return null; // 净值非正时百分比落差无意义
    return +((v / peak - 1) * 100).toFixed(2);
  });
}

/**
 * 直方图 → 柱心 + 计数。`bin_edges` 比 `counts` 多一个边界，柱心取相邻边中点（× 100 转 %）。
 * 长度不自洽即返回空 —— 宁可走降级分支，也不错位画一张看着正常的图。
 */
function histBars(h: HistBlock | null | undefined): { centers: number[]; counts: number[] } {
  if (!h || !h.counts?.length || h.bin_edges?.length !== h.counts.length + 1) {
    return { centers: [], counts: [] };
  }
  const centers = h.counts.map((_, i) => +(((h.bin_edges[i] + h.bin_edges[i + 1]) / 2) * 100).toFixed(2));
  return { centers, counts: h.counts };
}

interface Props {
  blocks: FactorBlocks | null;
  /** 用户点名的基准（未必等于 `blocks.excess_block.bench_symbol`） */
  bench: string;
  onBench: (symbol: string) => void;
  /** 后端口径文案（与计算代码同源，前端不另抄一份） */
  definitions: Record<string, string>;
}

export const ExcessTab: React.FC<Props> = ({ blocks, bench, onBench, definitions }) => {
  // 派生块还没到 → 加载态（旧字段照常可用，不能在这里就报「不可用」）
  if (!blocks) {
    return (
      <div className="flex flex-col gap-3 min-h-0">
        <div className="h-[64px] rounded-2xl border border-slate-200/80 bg-white animate-pulse" />
        <div className="grid grid-cols-2 gap-3">
          {[0, 1].map((i) => (
            <div key={i} className="h-[220px] rounded-2xl border border-slate-200/80 bg-white animate-pulse" />
          ))}
        </div>
        <div className="h-[240px] rounded-2xl border border-slate-200/80 bg-white animate-pulse" />
      </div>
    );
  }

  const ex: ExcessBlock = blocks.excess_block;
  if (!ex || ex.available === false) {
    return <Degraded reason={ex?.reason ?? '后端未下发「相对基准超额」块（旧快照或派生失败），需重跑报告构建。'} />;
  }

  const defOf = (key: string, fallback: string) => definitions?.[key] || fallback;

  // ── 基准身份：点名 vs 实际 ──
  const rows: BenchmarkExcess[] = ex.benchmarks ?? [];
  const actual = ex.bench_symbol ?? '';
  const actualName = ex.bench_name || benchLabel(actual);
  const picked = rows.find((b) => b.symbol === bench);
  const mismatched = !!actual && !!bench && actual !== bench;
  const missing = rows.filter((b) => b.available === false && b.symbol !== bench);

  // ── 主序列 ──
  // 本页签上的序列全是累计型 → 后端给的 `dates` 就是**全窗口**轴（与 IC 页签的
  // `cum_dates_full` 同义）。日频的 `ls_dates` 是另一根轴，别拿它画这些图。
  const dates = ex.dates ?? [];
  const xLabels = dates.map(fmtDate);
  const excess = ex.long_excess_cum ?? [];
  const excessPct = navPct(excess);
  const ddPct = drawdownPct(excess);
  const okExcess = hasData(excess);
  // 末值超额用原始净值（-1 后才是收益率）；已 ×100 的序列不能再喂给 fmtPct
  const lastExcess = excess.length ? excess[excess.length - 1] : null;

  // 多头组净值只认 group_block（与超额同源同期）；取不到就只画超额，不猜、不用超额反推指数
  const gbRaw = blocks.group_block as GroupBlock | undefined;
  const gb: GroupBlock | null =
    gbRaw && (gbRaw as { available?: boolean }).available === true ? (gbRaw as GroupBlock) : null;
  const longCum = gb && hasData(gb.long_cum) ? gb.long_cum : null;
  const longPct = longCum ? navPct(longCum) : null;
  // 两条线共用一份日期轴才画在一起；长度不等说明口径已漂移，宁缺毋错位
  const longAligned = !!longPct && longPct.length === dates.length;

  const annual: AnnualExcess[] = ex.annual ?? [];
  const hist = histBars(ex.excess_hist);
  const tops: DrawdownEpisode[] = ex.top_drawdowns ?? [];
  const es = ex.excess_stats;

  // ── 图表 option ──
  const pctTip = tooltip({ valueFormatter: (v: number) => (v == null ? DASH : `${v > 0 ? '+' : ''}${v}%`) });

  const excessCumOption: any = {
    grid: grid({ left: 52, top: 22, bottom: 26 }),
    tooltip: pctTip,
    xAxis: catAxis(xLabels),
    yAxis: valAxis('{value}%'),
    series: [{
      name: '累计超额', type: 'line', data: excessPct, showSymbol: false,
      lineStyle: { width: 2, color: ACCENT }, itemStyle: { color: ACCENT },
      areaStyle: { color: 'rgba(99, 102, 241, 0.12)' },
      markLine: { ...zeroLine, symbol: 'none', data: [{ yAxis: 0 }] },
    }],
  };

  const excessDdOption: any = {
    grid: grid({ left: 52, top: 22, bottom: 26 }),
    tooltip: tooltip({ valueFormatter: (v: number) => (v == null ? DASH : `${v}%`) }),
    xAxis: catAxis(xLabels),
    yAxis: valAxis('{value}%', { max: 0 }), // 回撤恒 ≤ 0，封顶 0 才读得出「离前高还差多远」
    series: [{
      name: '超额回撤', type: 'line', data: ddPct, showSymbol: false,
      lineStyle: { width: 1.6, color: DOWN }, itemStyle: { color: DOWN },
      areaStyle: { color: 'rgba(5, 150, 105, 0.16)' },
    }],
  };

  const navSeries: any[] = longAligned
    ? [{
      name: `多头组 G${gb?.long_group ?? '?'}`, type: 'line', data: longPct, showSymbol: false,
      lineStyle: { width: 2, color: UP }, itemStyle: { color: UP },
    }]
    : [];
  navSeries.push({
    name: '累计超额', type: 'line', data: excessPct, showSymbol: false,
    lineStyle: { width: longAligned ? 1.6 : 2, color: ACCENT }, itemStyle: { color: ACCENT },
    markLine: { ...zeroLine, symbol: 'none', data: [{ yAxis: 0 }] },
  });

  const navCompareOption: any = {
    grid: grid({ left: 52, top: 26, bottom: 26 }),
    tooltip: pctTip,
    legend: { show: true, top: 0, right: 6, itemWidth: 12, itemHeight: 6, textStyle: { fontSize: 10, color: '#64748b' } },
    xAxis: catAxis(xLabels),
    yAxis: valAxis('{value}%'),
    series: navSeries,
  };

  const annualOption: any = {
    grid: grid({ left: 52, top: 22, bottom: 26 }),
    tooltip: tooltip({
      // 图上只有一根超额柱，其余三项靠 tooltip 补齐（这四个口径本该一起读）
      formatter: (ps: any[]) => {
        const a = annual[ps?.[0]?.dataIndex ?? -1];
        if (!a) return '';
        return [
          `<b>${a.year}</b> 年 · ${a.n_days} 个交易日`,
          `多头组 ${fmtPct(a.long_ret, 2)}`, `基准 ${fmtPct(a.bench_ret, 2)}`,
          `超额 ${fmtPct(a.excess, 2)}`, `多空 ${fmtPct(a.ls_ret, 2)}`,
        ].join('<br/>');
      },
    }),
    xAxis: catAxis(annual.map((a) => String(a.year)), 0),
    yAxis: valAxis('{value}%'),
    series: [{
      name: '年度超额', type: 'bar', barMaxWidth: 40,
      data: annual.map((a) => (a.excess == null ? null
        : { value: +(a.excess * 100).toFixed(2), itemStyle: { color: bySign(a.excess) } })),
      markLine: { ...zeroLine, symbol: 'none', data: [{ yAxis: 0 }] },
    }],
  };

  const histOption: any = {
    grid: grid({ left: 46, top: 22, bottom: 30 }),
    tooltip: tooltip({
      formatter: (ps: any[]) => {
        const v = ps?.[0]?.value as number[] | undefined;
        if (!v) return '';
        return `超额 ${v[0] > 0 ? '+' : ''}${v[0]}%<br/>天数 ${v[1]} / 共 ${ex.excess_hist?.n ?? 0} 天`;
      },
    }),
    xAxis: valAxis('{value}%'), // 值轴：0 参考线才落得准（类目轴上只能落在某个柱心）
    yAxis: valAxis(undefined, { min: 0, splitNumber: 3 }),
    series: [{
      name: '天数', type: 'bar',
      // 值轴上的直方图：每箱一个 [柱心, 计数] 点，条宽由 ECharts 按最小间距自动推。
      // 颜色逐个预算好（正红负绿），不依赖回调参数形状 —— 回调拿到的 data/value 形态随版本会变。
      data: hist.centers.map((c, i) => ({ value: [c, hist.counts[i]], itemStyle: { color: bySign(c) } })),
      markLine: { ...zeroLine, symbol: 'none', data: [{ xAxis: 0 }] },
    }],
  };

  const statRows: Array<[string, string]> = [
    ['年化超额', fmtPct(es?.annual_excess ?? ex.excess_annual ?? null, 2)],
    ['跟踪误差', fmtPct(es?.tracking_error ?? null, 2, false)],
    ['信息比率', fmtNum(es?.information_ratio ?? null, 2)],
    ['Beta', fmtNum(es?.beta ?? null, 2)],
    ['相关性', fmtNum(es?.corr ?? null, 2, false)],
    ['有效天数', fmtInt(es?.n_days ?? null)],
  ];

  return (
    <div className="flex flex-col gap-3 min-h-0">
      {/* ── 1. 基准切换条 ── */}
      <div className="rounded-2xl border border-slate-200/80 bg-white p-3 shadow-sm min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[10px] font-bold text-slate-400">主基准</span>
          <div className="flex items-center rounded-full border border-slate-200 bg-slate-50 p-0.5">
            {BENCH_PRESETS.map((b) => {
              const meta = rows.find((r) => r.symbol === b.symbol);
              const bad = meta?.available === false;
              return (
                <button
                  key={b.symbol}
                  onClick={() => onBench(b.symbol)}
                  title={bad ? `取不到：${meta?.reason ?? '未知原因'}` : `${meta?.name || b.label}（${b.symbol}）`}
                  className={`rounded-full px-2.5 py-[3px] text-[11px] font-bold transition-colors ${
                    b.symbol === bench ? 'bg-white text-indigo-700 shadow-sm' : 'text-slate-500 hover:text-slate-700'
                  } ${bad ? 'line-through decoration-amber-400' : ''}`}
                >
                  {meta?.name || b.label}
                  {bad && <span className="ml-1 text-amber-500">⚠</span>}
                </button>
              );
            })}
          </div>
          <span className="flex items-center gap-1 text-[10px] text-slate-400 font-mono">
            实际采用 {actual || DASH}{actualName ? ` · ${actualName}` : ''}
            <InfoDot text={defOf('benchmarks', '主基准取数走 QuantDB index_daily；点名的指数缺数时按 沪深300 → 中证500 → 中证1000 顺位回退，页面上显示的是**实际采用**的那一个。')} />
          </span>
        </div>

        {/* 点名 ≠ 实际：必须显式挑明，绝不允许静默冒充 */}
        {mismatched && (
          <div className="mt-2 rounded-xl border border-amber-200 bg-amber-50/60 px-2.5 py-1.5 text-[10px] leading-relaxed text-amber-700">
            ⚠ 你点名的基准<b>{benchLabel(bench)}（{bench}）</b>本次没取到，本页全部数字都是
            <b>{actualName}（{actual}）</b>口径的 —— 两者不可互相替代，请勿把这里的超额读成前者。
            {picked?.reason ? ` 取数失败原因：${picked.reason}` : ''}
          </div>
        )}

        {/* 其余基准的缺数原因一并摊开：否则「为什么点不动」无从判断 */}
        {missing.length > 0 && (
          <div className="mt-1.5 flex flex-col gap-0.5 text-[10px] text-slate-400">
            {missing.map((m) => (
              <span key={m.symbol} title={m.reason}>
                ⓘ {m.name || benchLabel(m.symbol)}（{m.symbol}）取不到：{m.reason ?? '未给原因'}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* ── 2. 超额累计 + 超额回撤 ── */}
      <div className="grid grid-cols-2 gap-3">
        <ChartShell
          title="多头组累计超额"
          className="h-[220px]"
          hint={`${actualName} · 末值 ${fmtPct(lastExcess == null ? null : lastExcess - 1, 1)}`}
          info={defOf('excess_cum', '超额 = 多头组日收益 − 基准日收益，逐日复利累乘成净值（起点 1），图上画的是 (净值−1)×100%。这是**差额的复利**，与「多头复利 − 基准复利」不等价（后者见分年度图与年化超额）。')}
        >
          {okExcess
            ? <EChartsChart option={excessCumOption} />
            : <Degraded compact reason="超额序列为空（多头组与基准的共同有效交易日不足）" />}
        </ChartShell>

        <ChartShell
          title="超额回撤"
          className="h-[220px]"
          hint={`最大回撤 ${fmtPct(ex.long_excess_dd, 2)}`}
          info={defOf('max_drawdown', '最大回撤 = min(净值 / 历史最高净值 − 1)，取全区间最差值。区别于「末值回撤」。')}
        >
          {okExcess
            ? <EChartsChart option={excessDdOption} />
            : <Degraded compact reason="超额序列为空，无法计算回撤" />}
        </ChartShell>
      </div>

      {/* ── 3. 多头组 vs 累计超额 ── */}
      <ChartShell
        title="多头组累计 vs 累计超额"
        className="h-[240px]"
        hint={longAligned ? '两者之差即基准贡献' : '多头组净值不可用，仅画超额'}
        info={defOf('excess_vs_long', '两条线的落差就是同期基准的累计贡献。基准自身净值未单独下发：用「多头 − 超额」反推指数在复利口径下不等价，故此处不画、不猜。')}
      >
        {okExcess
          ? <EChartsChart option={navCompareOption} />
          : <Degraded compact reason="超额序列为空，无法对比" />}
      </ChartShell>

      {/* ── 4. 分年度超额 + 5. 超额收益分布 ── */}
      <div className="grid grid-cols-2 gap-3">
        <ChartShell
          title="分年度超额收益"
          className="h-[220px]"
          hint={`${annual.length} 个年度`}
          info={defOf('annual_excess', '年内复利：多头组与基准各自在年内复利成收益，超额 = 多头年收益 − 基准年收益（**不是**年内日超额的复利）。')}
        >
          {annual.length
            ? <EChartsChart option={annualOption} />
            : <Degraded compact reason="分年度超额为空（区间不足一个完整年度，或快照未包含）" />}
        </ChartShell>

        <ChartShell
          title="超额收益分布"
          className="h-[220px]"
          hint={`CVaR95 ${fmtPct(ex.cvar_95, 2)} · CVaR99 ${fmtPct(ex.cvar_99, 2)}`}
          info={defOf('excess_hist', '日超额收益的直方图（等宽分箱，柱心为箱中点）。CVaR = 尾部最差 5% / 1% 交易日的平均超额，左尾比命中率更能说明「坏日子有多坏」。')}
        >
          {hist.counts.length
            ? <EChartsChart option={histOption} />
            : <Degraded compact reason="超额收益分布为空（有效交易日不足，无法分箱）" />}
        </ChartShell>
      </div>

      {/* ── 6. 超额回撤区间（前 5 大） ── */}
      <div className="rounded-2xl border border-slate-200/80 bg-white p-3 shadow-sm min-w-0">
        <div className="flex items-baseline justify-between gap-2 mb-1.5">
          <h4 className="flex items-center gap-1 text-xs font-extrabold text-slate-800">
            超额回撤区间（前 5 大）
            <InfoDot text={defOf('max_drawdown', '回撤区间 = 峰→谷；天数按交易日计；「已收复」指谷底之后净值回到该峰值之上。')} />
          </h4>
          <span className="text-[10px] text-slate-400 font-mono">{tops.length} 段</span>
        </div>
        {tops.length ? (
          <table className="w-full text-[11px]">
            <thead>
              <tr className="text-[10px] text-slate-400">
                <th className="py-1 text-left font-bold">区间（峰 → 谷）</th>
                <th className="py-1 text-right font-bold">回撤</th>
                <th className="py-1 text-right font-bold">天数</th>
                <th className="py-1 text-right font-bold">状态</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-600">
              {tops.map((e, i) => (
                <tr key={`${e.start}-${e.i0}-${i}`} className="border-t border-slate-100">
                  <td className="py-1">{e.start ?? DASH} → {e.end ?? DASH}</td>
                  <td className="py-1 text-right text-emerald-600">{fmtPct(e.dd, 2)}</td>
                  <td className="py-1 text-right">{fmtInt(e.days)}</td>
                  <td className="py-1 text-right text-slate-400">{e.recovered ? '已收复' : '未收复'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="py-2 text-[10px] text-slate-400">
            该基准下没有记录到回撤区间（未产生正深度回撤）。若与图形不符，请重跑报告构建。
          </div>
        )}
      </div>

      {/* ── 7. 超额统计 ── */}
      <div className="rounded-2xl border border-slate-200/80 bg-white p-3 shadow-sm min-w-0">
        <div className="flex items-baseline justify-between gap-2 mb-1.5">
          <h4 className="flex items-center gap-1 text-xs font-extrabold text-slate-800">
            超额统计
            <InfoDot text={defOf('n_days', '有效天数：该指标实际参与计算的交易日数。样本不足时不给年化。')} />
          </h4>
          <span className="text-[10px] text-slate-400 font-mono">{actual}</span>
        </div>
        <div className="grid grid-cols-3 gap-x-4 gap-y-1.5 sm:grid-cols-6">
          {statRows.map(([label, value]) => (
            <div key={label} className="min-w-0">
              <div className="text-[10px] text-slate-400 truncate">{label}</div>
              <div className="text-[13px] font-black font-mono text-slate-700">{value}</div>
            </div>
          ))}
        </div>
        <div className="mt-1.5 border-t border-slate-100 pt-1.5 text-[10px] text-slate-400">
          年化超额 = 日均超额 × 252（简单年化）；跟踪误差 = 超额日波动 × √252；信息比率与 Beta 由多头组
          日收益对基准日收益比值得出 —— 全部以 {actualName} 为基准。
        </div>
      </div>
    </div>
  );
};
