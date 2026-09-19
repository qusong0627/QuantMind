/**
 * 「IC」页签 —— 因子的信息含量全貌。
 *
 * 单看一个 IC 均值会骗人：它可能是少数几天的极端值撑起来的、可能来自行业暴露、
 * 可能只在小盘股上成立、也可能随时间衰减。本页签把**多口径 IC 并排**摆出来，
 * 让这些情形各自可见：
 *   全截面 / Top半 / Bottom半 · 累计 IC · 分布 · 自相关 · 中性化 · 分市值域 ·
 *   滚动稳定性 · 稳健性分段
 *
 * 图一律优先（信息密度高于表格）；表格只作读数补充。
 */

import React from 'react';
import { EChartsChart } from '../../../../../components/common/EChartsChart';
import type { FactorBlocks, FactorDetail, IcBlock, DegradedBlock, RobustBlock, SubPeriod, HeadlineBlock } from '../../../types/factorReport';
import {
  ACCENT, ACCENT_2, ChartShell, DASH, Degraded, DOWN, GRID, NEUTRAL, UP, WARN,
  axisInterval, bySign, catAxis, fmtDate, fmtNum, fmtPct, grid, hasData, tooltip, valAxis, zeroLine,
} from './chartKit';

interface Props {
  detail: FactorDetail | null;
  blocks: FactorBlocks | null;
}

/** 直方图 → 柱心（bin_edges 比 counts 多一个） */
function histBars(b: { bin_edges: number[]; counts: number[] } | null | undefined): Array<[number, number]> {
  if (!b?.bin_edges?.length || !b.counts?.length) return [];
  return b.counts.map((c, i) => [(b.bin_edges[i] + b.bin_edges[i + 1]) / 2, c]);
}

export const IcTab: React.FC<Props> = ({ detail, blocks }) => {
  // 从联合类型上判 available 再断言（在具体类型上比 `!== false` 会被 TS 判为无交集）
  const icRaw = blocks?.ic_block;
  const ic = icRaw && icRaw.available !== false ? (icRaw as IcBlock) : null;
  const degraded = icRaw && icRaw.available === false ? (icRaw as DegradedBlock) : null;
  const rbRaw = blocks?.robust_block;
  const rb = rbRaw && rbRaw.available !== false ? (rbRaw as RobustBlock) : null;
  // ICIR 只在 headline 块里（ic_block 不带），别在 ic_block 上找
  const icir = (blocks?.headline as HeadlineBlock | undefined)?.icir ?? null;

  if (!detail || detail.empty) {
    return <Degraded reason={detail?.reason || '请选择一个因子'} />;
  }

  // 既有图：IC 时序（日 IC 柱 + 20 日均线）。数据取旧字段，保证与升级前**逐像素同源**。
  const legacyDates = detail.dates.map(fmtDate);
  const icSeriesOption: any = {
    grid: grid({ top: 24 }),
    tooltip: tooltip(),
    legend: { show: true, right: 0, top: 0, itemWidth: 10, itemHeight: 6, textStyle: { fontSize: 10, color: NEUTRAL } },
    xAxis: catAxis(legacyDates),
    yAxis: valAxis((v: number) => v.toFixed(3)),
    series: [
      {
        name: '日 IC', type: 'bar', barMaxWidth: 4,
        data: detail.ic_series.map((v) => (v == null ? null : +v.toFixed(4))),
        itemStyle: { color: 'rgba(99, 102, 241, 0.35)' },
      },
      {
        name: '20 日均值', type: 'line', showSymbol: false,
        data: detail.ic_rolling.map((v) => (v == null ? null : +v.toFixed(4))),
        lineStyle: { width: 2, color: UP }, itemStyle: { color: UP },
      },
    ],
  };

  const decayKeys = ic ? Object.keys(ic.decay).sort((a, b) => Number(a) - Number(b)) : [];
  const decayOption: any = {
    grid: grid({ left: 44 }),
    tooltip: tooltip({ valueFormatter: (v: number) => `${v?.toFixed(5)}` }),
    xAxis: catAxis(decayKeys.map((h) => `T+${h}`), 0),
    yAxis: valAxis((v: number) => v.toFixed(4)),
    series: [{
      type: 'line', showSymbol: true, symbolSize: 7,
      data: decayKeys.map((h) => ic?.decay?.[h] ?? null),
      lineStyle: { width: 2, color: ACCENT }, itemStyle: { color: ACCENT },
      areaStyle: { color: 'rgba(99,102,241,0.10)' },
      markLine: zeroLine,
    }],
  };

  // 全截面 / Top半 / Bottom半 —— 三条口径并列。Top半与Bottom半**符号相反**才是
  // 真单调：同号说明信号集中在某一端，不是全截面有效的因子。
  const halfs = [
    { name: '全截面', v: ic?.ic_mean ?? null, c: ACCENT },
    { name: 'Top 半', v: ic?.ic_top_mean ?? null, c: UP },
    { name: 'Bottom 半', v: ic?.ic_bot_mean ?? null, c: DOWN },
  ];
  const halfOption: any = {
    grid: grid({ left: 52, bottom: 30 }),
    tooltip: tooltip({ valueFormatter: (v: number) => (v == null ? DASH : v.toFixed(5)) }),
    xAxis: catAxis(halfs.map((h) => h.name), 0),
    yAxis: valAxis((v: number) => v.toFixed(4)),
    series: [{
      type: 'bar', barMaxWidth: 46,
      data: halfs.map((h) => ({ value: h.v, itemStyle: { color: h.c, borderRadius: 4 } })),
      label: { show: true, position: 'top', fontSize: 10, color: '#475569', formatter: (p: any) => (p.value == null ? DASH : Number(p.value).toFixed(5)) },
      markLine: zeroLine,
    }],
  };

  const cumOption: any = {
    grid: grid({ top: 26 }),
    tooltip: tooltip(),
    legend: { show: true, right: 0, top: 0, itemWidth: 10, itemHeight: 6, textStyle: { fontSize: 10, color: NEUTRAL } },
    xAxis: catAxis((ic?.cum_dates_full ?? detail.dates).map(fmtDate), axisInterval((ic?.cum_dates_full ?? detail.dates).length, 8)),
    yAxis: valAxis((v: number) => v.toFixed(2)),
    series: [
      { name: '累计 IC', type: 'line', showSymbol: false, data: ic?.ic_cum_full ?? [], lineStyle: { width: 2.2, color: ACCENT }, itemStyle: { color: ACCENT } },
      ...(hasData(ic?.cum_ic_top_full) ? [{ name: '累计 Top 半 IC', type: 'line', showSymbol: false, data: ic!.cum_ic_top_full, lineStyle: { width: 1.4, color: UP, type: 'dashed' }, itemStyle: { color: UP } }] : []),
      ...(hasData(ic?.cum_ic_bot_full) ? [{ name: '累计 Bottom 半 IC', type: 'line', showSymbol: false, data: ic!.cum_ic_bot_full, lineStyle: { width: 1.4, color: DOWN, type: 'dashed' }, itemStyle: { color: DOWN } }] : []),
    ],
  };

  const icHist = histBars(ic?.ic_hist);
  const histOption: any = {
    grid: grid({ left: 44 }),
    tooltip: tooltip({ valueFormatter: (v: number) => v.toFixed(4) }),
    xAxis: catAxis(icHist.map(([c]) => c.toFixed(3)), Math.max(1, Math.floor(icHist.length / 8))),
    yAxis: valAxis(),
    series: [{
      type: 'bar', barMaxWidth: 12,
      data: icHist.map(([, c]) => c),
      itemStyle: { color: 'rgba(99,102,241,0.45)' },
      markLine: {
        silent: true, symbol: 'none',
        label: { fontSize: 9, color: WARN },
        lineStyle: { color: WARN, type: 'dashed' },
        data: [{ xAxis: '-0.020' }, { xAxis: '0.020' }],
      },
    }],
  };

  const auto = ic?.ic_autocorr ?? [];
  const autocorrOption: any = {
    grid: grid({ left: 46 }),
    tooltip: tooltip({ valueFormatter: (v: number) => (v == null ? DASH : v.toFixed(3)) }),
    xAxis: catAxis(auto.map((_, i) => `lag${i + 1}`), 1),
    yAxis: valAxis((v: number) => v.toFixed(1), { min: -1, max: 1 }),
    series: [{
      type: 'bar', barMaxWidth: 16,
      data: auto.map((v) => ({ value: v, itemStyle: { color: v != null && v > 0 ? UP : DOWN } })),
      markLine: zeroLine,
    }],
  };

  // 中性化对比：后端给的是「行业去均值 + 对市值正交」后的残差秩 IC，
  // 即行业与市值是**一次回归一起剔的**，不是一个一个来的 —— 文案别写成「再加市值」。
  const neuDays = ic?.ic_neutral_days ?? 0;
  const neuOption: any = {
    grid: grid({ left: 52, bottom: 30 }),
    tooltip: tooltip({ valueFormatter: (v: number) => (v == null ? DASH : v.toFixed(5)) }),
    xAxis: catAxis(['原始 IC', '行业+市值中性'], 0),
    yAxis: valAxis((v: number) => v.toFixed(4)),
    series: [{
      type: 'bar', barMaxWidth: 46,
      data: [
        { value: ic?.ic_mean ?? null, itemStyle: { color: ACCENT, borderRadius: 4 } },
        { value: ic?.ic_neutral_mean ?? null, itemStyle: { color: ACCENT_2, borderRadius: 4 } },
      ],
      label: { show: true, position: 'top', fontSize: 10, color: '#475569', formatter: (p: any) => (p.value == null ? DASH : Number(p.value).toFixed(5)) },
      markLine: zeroLine,
    }],
  };

  const dom = ic?.ic_domain_series;
  const domOption: any = {
    grid: grid({ top: 26 }),
    tooltip: tooltip(),
    legend: { show: true, right: 0, top: 0, itemWidth: 10, itemHeight: 6, textStyle: { fontSize: 10, color: NEUTRAL } },
    xAxis: catAxis(detail.dates.map(fmtDate)),
    yAxis: valAxis((v: number) => v.toFixed(3)),
    series: (['large', 'mid', 'small'] as const)
      .filter((k) => hasData(dom?.[k]))
      .map((k, i) => ({
        name: { large: '大盘', mid: '中盘', small: '小盘' }[k],
        type: 'line', showSymbol: false,
        data: dom![k],
        lineStyle: { width: 1.6, color: [ACCENT, WARN, ACCENT_2][i] },
        itemStyle: { color: [ACCENT, WARN, ACCENT_2][i] },
      })),
    ...(hasData(dom?.large) || hasData(dom?.mid) || hasData(dom?.small) ? {} : {}),
  };

  const rollOption: any = {
    grid: grid({ top: 26 }),
    tooltip: tooltip(),
    legend: { show: true, right: 0, top: 0, itemWidth: 10, itemHeight: 6, textStyle: { fontSize: 10, color: NEUTRAL } },
    xAxis: catAxis((ic?.cum_dates_full ?? detail.dates).map(fmtDate), axisInterval((ic?.cum_dates_full ?? []).length || detail.dates.length, 8)),
    yAxis: [
      valAxis((v: number) => v.toFixed(3)),
      { ...valAxis((v: number) => v.toFixed(1)), splitLine: { show: false } },
    ],
    series: [
      { name: '252 日滚动 IC', type: 'line', showSymbol: false, data: ic?.ic_rolling_long ?? [], lineStyle: { width: 1.6, color: ACCENT }, itemStyle: { color: ACCENT } },
      { name: '252 日滚动 IR', type: 'line', yAxisIndex: 1, showSymbol: false, data: ic?.ir_rolling_full ?? [], lineStyle: { width: 1.4, color: ACCENT_2, type: 'dashed' }, itemStyle: { color: ACCENT_2 } },
    ],
  };

  const segs: SubPeriod[] = rb?.sub_period ?? [];
  const segOption: any = {
    grid: grid({ left: 46 }),
    tooltip: tooltip({ valueFormatter: (v: number) => (v == null ? DASH : v.toFixed(4)) }),
    xAxis: catAxis(segs.map((s) => `${(s.start ?? '').slice(2, 7)}`), 0),
    yAxis: valAxis((v: number) => v.toFixed(3)),
    series: [{
      type: 'bar', barMaxWidth: 40,
      data: segs.map((s) => ({ value: s.ic_mean, itemStyle: { color: bySign(s.ic_mean), borderRadius: 4 } })),
      label: { show: true, position: 'top', fontSize: 9, color: '#64748b', formatter: (p: any) => (p.value == null ? DASH : Number(p.value).toFixed(4)) },
      markLine: zeroLine,
    }],
  };

  const rows: Array<[string, string, string]> = [
    ['IC 均值', fmtNum(ic?.ic_mean, 5), '全截面日频秩相关的均值'],
    ['IC 标准差', fmtNum(ic?.ic_std, 5), '日 IC 的离散度'],
    ['ICIR', fmtNum(icir, 4), '不年化（与平台其余模块口径一致）'],
    ['IC 胜率', ic?.win_rate == null ? DASH : `${(ic.win_rate * 100).toFixed(1)}%`, 'IC > 0 的交易日占比'],
    ['普通 t 值', fmtNum(blocks?.significance && (blocks.significance as any).t_value, 2), 'ICIR × √n，IC 有自相关时高估'],
    ['Newey-West t', fmtNum((blocks?.significance as any)?.nw_t_value, 2), '按 IC 自相关修正标准误'],
    ['p 值', fmtNum((blocks?.significance as any)?.p_value, 4), '双尾'],
    ['BHY q 值', fmtNum((blocks?.significance as any)?.q_value_bhy, 4), '全库多重检验校正后'],
    ['Bootstrap IC 95% CI', ciText((blocks?.significance as any)?.bootstrap_ic_mean), '不依赖正态假设'],
    ['IC 半衰期', ic?.half_life_days == null ? DASH : `${ic.half_life_days} 个交易日`, 'IC 衰减到一半所需天数'],
    ['Top 半 / Bottom 半', `${fmtNum(ic?.ic_top_mean, 5)} / ${fmtNum(ic?.ic_bot_mean, 5)}`, '按因子值中位切分'],
    ['中性化 IC', `${fmtNum(ic?.ic_neutral_mean, 5)}（${neuDays} 天）`, '行业去均值 + 对市值正交'],
    ['样本外衰减', fmtNum(rb?.oos?.decay, 5), '后一半 − 前一半'],
    ['拥挤度', fmtNum(rb?.crowding?.score, 3), '换手分位与 IC 自相关的合成'],
    ['与全库最大 |ρ|', fmtNum(ic?.independence?.max_corr, 3), '独立性代理：越大越像复制品'],
  ];

  const missingCore = !ic;

  return (
    <div className="flex flex-col gap-3 min-h-0">
      {missingCore && (
        <Degraded compact reason={degraded?.reason
          ?? 'IC 派生块不可用（可能为旧快照）；下方仍保留升级前就有的 IC 时序图。'} />
      )}

      <div className="grid grid-cols-2 gap-3">
        <ChartShell primary title="日 IC 时序" hint={`IC 均值 ${fmtNum(ic?.ic_mean ?? detail.ic_mean, 5)} · 20 日均线`}>
          <div className="h-[220px] min-w-0"><EChartsChart option={icSeriesOption} /></div>
        </ChartShell>

        <ChartShell title="IC 衰减" info="各前瞻期（T+1/2/5/10/20）的 IC 均值。衰减慢说明信号持续时间长，可用更低的调仓频率。"
          hint={ic?.half_life_days != null ? `半衰期 ${ic.half_life_days} 日` : undefined}>
          {decayKeys.length
            ? <div className="h-[220px] min-w-0"><EChartsChart option={decayOption} /></div>
            : <div className="h-[220px]"><Degraded compact reason="该快照未构建多前瞻期 IC" /></div>}
        </ChartShell>

        <ChartShell title="全截面 / Top半 / Bottom半 IC" info="Top 半与 Bottom 半**异号**才是真正的全截面单调信号；同号说明信号只集中在某一端。">
          <div className="h-[200px] min-w-0"><EChartsChart option={halfOption} /></div>
        </ChartShell>

        <ChartShell title="累计 IC" info="全窗口累积（不受回看期截断）。斜率稳定才说明 IC 是持续产生的，而不是靠某几天。"
          hint={ic ? `全窗口 ${ic.cum_dates_full?.length ?? 0} 天` : undefined}>
          <div className="h-[200px] min-w-0"><EChartsChart option={cumOption} /></div>
        </ChartShell>

        <ChartShell title="IC 分布" info="虚线为 ±0.02 参考线（|IC| 超过 0.02 通常才算有实际意义）。分布对称且均值偏离 0 越远越好。"
          hint={ic?.ic_hist ? `n=${ic.ic_hist.n}` : undefined}>
          {icHist.length
            ? <div className="h-[200px] min-w-0"><EChartsChart option={histOption} /></div>
            : <div className="h-[200px]"><Degraded compact reason="无 IC 分布" /></div>}
        </ChartShell>

        <ChartShell title="IC 自相关 lag1–20" info="自相关高说明 IC 有持续性（也可能是拥挤——大家都在用同一信号）。它同时是 Newey-West t 的修正依据。">
          {hasData(auto)
            ? <div className="h-[200px] min-w-0"><EChartsChart option={autocorrOption} /></div>
            : <div className="h-[200px]"><Degraded compact reason="样本不足，无法计算自相关" /></div>}
        </ChartShell>

        <ChartShell title="中性化 IC 对比" info="行业去均值**同时**对市值正交后的残差秩 IC。若它远小于原始 IC，说明原始信号主要来自行业或市值暴露。"
          hint={neuDays ? `有效 ${neuDays} 天` : undefined}>
          <div className="h-[200px] min-w-0"><EChartsChart option={neuOption} /></div>
        </ChartShell>

        <ChartShell title="分市值域 IC" info="按当日总市值三分位切成大/中/小三域分别算 IC。小盘因子常在大票上失效——这直接回答「大资金能不能用」。">
          {hasData(dom?.large) || hasData(dom?.mid) || hasData(dom?.small)
            ? <div className="h-[200px] min-w-0"><EChartsChart option={domOption} /></div>
            : <div className="h-[200px]"><Degraded compact reason="该快照未构建分市值域 IC" /></div>}
        </ChartShell>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <ChartShell primary title="滚动稳定性（252 日）" info="滚动 IC 与滚动 IR。曲线穿 0 频繁说明该因子并不稳定，任何单期结论都不可信。"
          hint={`全窗口 ${ic?.cum_dates_full?.length ?? 0} 天`}>
          <div className="h-[230px] min-w-0"><EChartsChart option={rollOption} /></div>
        </ChartShell>

        <ChartShell title="稳健性分段" info="把样本等分成 4 段分别算 IC。段间跳动大 = 有效期不稳定；样本外衰减为负说明近期有效性下滑。"
          hint={rb?.ic_stability != null ? `稳定性 ${fmtNum(rb.ic_stability, 3)}` : undefined}>
          {segs.length
            ? <div className="h-[230px] min-w-0"><EChartsChart option={segOption} /></div>
            : <div className="h-[230px]"><Degraded compact reason="分段统计不可用" /></div>}
        </ChartShell>
      </div>

      {/* 读数补充：图表看形态，表格看数值 */}
      <ChartShell title="IC 统计表" info="普通 t 与 Newey-West t **并列**：IC 自相关显著时，普通 t 会严重高估显著性，两者差距越大越要信 NW。">
        <div className="overflow-x-auto">
          <table className="w-full text-[11px]">
            <tbody>
              {rows.map(([k, v, note], i) => (
                <tr key={k} className={i % 2 ? 'bg-slate-50/60' : ''}>
                  <td className="px-2 py-1 font-bold text-slate-600 whitespace-nowrap">{k}</td>
                  <td className="px-2 py-1 font-mono font-black text-slate-800 whitespace-nowrap">{v}</td>
                  <td className="px-2 py-1 text-slate-400">{note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </ChartShell>

      {rb?.regime?.length ? (
        <ChartShell title="市场状态分段" info="按指数走势划分的牛/熊/震荡区段内的多空表现，看因子是否只在某一种行情里有效。">
          <div className="grid grid-cols-3 gap-2">
            {rb.regime.map((r) => (
              <div key={r.regime} className="rounded-xl border border-slate-200/80 bg-slate-50/50 p-2">
                <div className="text-[10px] font-bold text-slate-500">{r.regime} · {r.n_days} 天</div>
                <div className={`text-sm font-black font-mono ${(r.ls_annual ?? 0) >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
                  {fmtPct(r.ls_annual, 1)}
                </div>
                <div className="text-[10px] text-slate-400 font-mono">日均 {fmtPct(r.ls_mean_daily, 3)}</div>
              </div>
            ))}
          </div>
        </ChartShell>
      ) : null}

      <div className="sr-only" style={{ color: GRID, background: NEUTRAL }}>ic-tab</div>
    </div>
  );
};

function ciText(ci: { lo: number | null; hi: number | null; level: number } | null | undefined): string {
  if (!ci || ci.lo == null || ci.hi == null) return DASH;
  return `[${ci.lo.toFixed(5)}, ${ci.hi.toFixed(5)}]`;
}
