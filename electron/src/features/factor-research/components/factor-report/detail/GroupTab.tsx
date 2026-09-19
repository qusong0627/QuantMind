/**
 * 「分组回测」页签 —— 组合层面的真金白银。
 *
 * ⚠️ 本页签有四处极易写错的口径，全部在 UI 上显式标注，不靠读者猜：
 *  1. `ls_daily` = 0.5×(多头腿 − 空头腿)，**毛收益**（不含成本），每日再平衡、美元中性。
 *  2. `short_cum` 是空头腿**作为多头持有**的累计，**不是做空口径**。做空 P&L 必须
 *     客户端按 ∏(1−r) 复利重算 —— `−(∏(1+r)−1)` 只是近似，两者在长区间会显著分叉。
 *  3. 可交易轨与理想轨的差额表达的是「**理想口径高估了多少**」，不是「策略会亏这么多」。
 *  4. 持有期扫描用的是 `ls_{h}` = **Q10−Q1 极值价差**，与页面上可配的 G3/G9 不是同一个组合。
 */

import React from 'react';
import { EChartsChart } from '../../../../../components/common/EChartsChart';
import type {
  FactorBlocks, FactorDetail, GroupBlock, DegradedBlock, RobustBlock, TradableBlock,
} from '../../../types/factorReport';
import {
  ACCENT, ChartShell, DASH, Degraded, DOWN, NEUTRAL, UP, WARN,
  axisInterval, catAxis, fmtDate, fmtNum, fmtPct, grid, hasData, quantileColor,
  tooltip, valAxis, zeroLine,
} from './chartKit';
import { quantileBarOption, quantileCurveOption, turnoverOption } from './legacyCharts';

interface Props {
  detail: FactorDetail | null;
  blocks: FactorBlocks | null;
}

/** 净值因子序列 → 累计收益（%）。cum_curve 返回的是 ∏(1+r)，不是收益。 */
const toReturnPct = (curve: Array<number | null>): Array<number | null> =>
  curve.map((v) => (v == null ? null : +((v - 1) * 100).toFixed(2)));

/** 空头腿的**做空 P&L** 累计：∏(1 − r) − 1。与「取负多头累计」不是一回事。 */
function shortBookPct(shortDaily: Array<number | null>): Array<number | null> {
  let nav = 1;
  return shortDaily.map((r) => {
    if (r == null || !Number.isFinite(r)) return +((nav - 1) * 100).toFixed(2);
    nav *= 1 - r;
    return +((nav - 1) * 100).toFixed(2);
  });
}

/** 曲线 → 回撤（%，≤0） */
function ddPct(curve: Array<number | null>): Array<number | null> {
  let peak = -Infinity;
  return curve.map((v) => {
    if (v == null || !Number.isFinite(v)) return null;
    if (v > peak) peak = v;
    // 峰值可能 ≤0（一路亏损的组合），此时用绝对值做分母会翻转符号 —— 直接返回 0 表示「处于新高」
    if (!(peak > 0)) return 0;
    return +((v / peak - 1) * 100).toFixed(2);
  });
}

export const GroupTab: React.FC<Props> = ({ detail, blocks }) => {
  // 从联合类型上判 available 再断言（在具体类型上比 `!== false` 会被 TS 判为无交集）
  const gbRaw = blocks?.group_block;
  const gb = gbRaw && gbRaw.available !== false ? (gbRaw as GroupBlock) : null;
  const gbDeg = gbRaw && gbRaw.available === false ? (gbRaw as DegradedBlock) : null;
  const rbRaw = blocks?.robust_block;
  const rb = rbRaw && rbRaw.available !== false ? (rbRaw as RobustBlock) : null;
  const trad = gb?.tradable ?? null;

  if (!detail || detail.empty) return <Degraded reason={detail?.reason || '请选择一个因子'} />;

  // 两根轴：日频序列（日收益/分布/月度）走窗口，累计曲线走全窗口。
  // 混用会让曲线与其横轴错位 —— 图还是画得出来，只是横轴标签整体偏了。
  const dates = (gb?.dates ?? detail.dates).map(fmtDate);
  const cumDates = (gb?.cum_dates_full ?? gb?.dates ?? detail.dates).map(fmtDate);
  const lg = gb?.long_group ?? 3;
  const sg = gb?.short_group ?? 9;

  // ── 多空累计净值与回撤 ──
  const lsCum = toReturnPct(gb?.ls_cum ?? []);
  const lsDd = ddPct(gb?.ls_cum ?? []);
  const navOption: any = {
    grid: grid({ top: 24 }),
    tooltip: tooltip({ valueFormatter: (v: number) => (v == null ? DASH : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`) }),
    xAxis: catAxis(cumDates, axisInterval(cumDates.length, 8)),
    yAxis: valAxis((v: number) => `${v.toFixed(0)}%`),
    series: [{
      name: `多空 G${lg}/G${sg}`, type: 'line', showSymbol: false, data: lsCum,
      lineStyle: { width: 2, color: ACCENT }, itemStyle: { color: ACCENT },
      areaStyle: { color: 'rgba(99,102,241,0.08)' },
      markLine: zeroLine,
    }],
  };
  const ddOption: any = {
    grid: grid({ top: 24 }),
    tooltip: tooltip({ valueFormatter: (v: number) => (v == null ? DASH : `${v.toFixed(2)}%`) }),
    xAxis: catAxis(cumDates, axisInterval(cumDates.length, 8)),
    yAxis: valAxis((v: number) => `${v.toFixed(0)}%`),
    series: [{
      name: '回撤', type: 'line', showSymbol: false, data: lsDd,
      lineStyle: { width: 1.6, color: DOWN }, itemStyle: { color: DOWN },
      areaStyle: { color: 'rgba(5,150,105,0.16)' },
      // 回撤最深的三个区间：让「最坏的时候有多坏」一眼可见
      markArea: {
        silent: true,
        itemStyle: { color: 'rgba(225,29,72,0.07)' },
        data: (gb?.ls_dd_episodes ?? []).slice(0, 3).map((e) => [
          { xAxis: fmtDate(e.start) }, { xAxis: fmtDate(e.end) },
        ]),
      },
    }],
  };

  // ── 双轨对照 ──
  const tradCum = toReturnPct(trad?.ls_cum ?? []);
  const dualOption: any = {
    grid: grid({ top: 28 }),
    tooltip: tooltip({ valueFormatter: (v: number) => (v == null ? DASH : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`) }),
    legend: { show: true, right: 0, top: 0, itemWidth: 10, itemHeight: 6, textStyle: { fontSize: 10, color: NEUTRAL } },
    xAxis: catAxis(cumDates, axisInterval(cumDates.length, 8)),
    yAxis: valAxis((v: number) => `${v.toFixed(0)}%`),
    series: [
      { name: '理想口径（任意价位可成交）', type: 'line', showSymbol: false, data: lsCum, lineStyle: { width: 2, color: ACCENT }, itemStyle: { color: ACCENT } },
      ...(hasData(tradCum)
        ? [{ name: '可交易口径（剔涨跌停/停牌）', type: 'line', showSymbol: false, data: tradCum, lineStyle: { width: 2, color: WARN, type: 'dashed' }, itemStyle: { color: WARN } }]
        : []),
    ],
  };

  // ── 两腿累计 ──
  // 做空 P&L 优先用后端算的全窗口序列；旧快照没有该字段时退回本地按 ∏(1−r) 复利
  // （口径与后端一致，只是窗口不同 —— 不与全窗口的 long_cum 同轴，故只在缺字段时兜底）
  const shortPct = gb?.short_book_cum?.length
    ? toReturnPct(gb.short_book_cum)
    : shortBookPct(gb?.short_daily ?? []);
  const longPct = toReturnPct(gb?.long_cum ?? []);
  const legOption: any = {
    grid: grid({ top: 28 }),
    tooltip: tooltip({ valueFormatter: (v: number) => (v == null ? DASH : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`) }),
    legend: { show: true, right: 0, top: 0, itemWidth: 10, itemHeight: 6, textStyle: { fontSize: 10, color: NEUTRAL } },
    xAxis: catAxis(cumDates, axisInterval(cumDates.length, 8)),
    yAxis: valAxis((v: number) => `${v.toFixed(0)}%`),
    series: [
      { name: `多头 G${lg} 累计`, type: 'line', showSymbol: false, data: longPct, lineStyle: { width: 2, color: UP }, itemStyle: { color: UP } },
      { name: `空头 G${sg} 做空累计`, type: 'line', showSymbol: false, data: shortPct, lineStyle: { width: 2, color: DOWN }, itemStyle: { color: DOWN } },
    ],
  };

  // ── 各组日均收益 ──
  const gDaily = gb?.group_daily_mean ?? [];
  const groupDailyOption: any = {
    grid: grid({ left: 46, bottom: 30 }),
    tooltip: tooltip({ valueFormatter: (v: number) => (v == null ? DASH : `${(v * 100).toFixed(3)}%`) }),
    xAxis: catAxis(gDaily.map((_, i) => `G${i + 1}`), 0),
    yAxis: valAxis((v: number) => `${(v * 100).toFixed(2)}%`),
    series: [{
      type: 'bar', barMaxWidth: 26,
      data: gDaily.map((v, i) => ({
        value: v == null ? null : +(v * 100).toFixed(4),
        itemStyle: { color: quantileColor(i, gDaily.length), borderRadius: 3 },
      })),
      label: { show: true, position: 'top', fontSize: 9, color: '#64748b', formatter: (p: any) => (p.value == null ? DASH : Number(p.value).toFixed(3)) },
      markLine: zeroLine,
    }],
  };

  // ── 组日均换手 ──
  const gt = gb?.group_turnover ?? [];
  const groupTurnOption: any = {
    grid: grid({ left: 46, bottom: 30 }),
    tooltip: tooltip({ valueFormatter: (v: number) => (v == null ? DASH : `${(v * 100).toFixed(1)}%`) }),
    xAxis: catAxis(gt.map((_, i) => `G${i + 1}`), 0),
    yAxis: valAxis((v: number) => `${(v * 100).toFixed(0)}%`),
    series: [{
      type: 'bar', barMaxWidth: 26,
      data: gt.map((v, i) => ({
        value: v == null ? null : v,
        itemStyle: { color: quantileColor(i, gt.length), borderRadius: 3, opacity: (i + 1 === lg || i + 1 === sg) ? 1 : 0.55 },
      })),
      label: { show: true, position: 'top', fontSize: 9, color: '#64748b', formatter: (p: any) => (p.value == null ? DASH : `${(Number(p.value) * 100).toFixed(0)}%`) },
    }],
  };

  // ── 多空日收益 ──
  const lsDaily = gb?.ls_daily ?? [];
  const lsDailyPct = lsDaily.map((v) => (v == null ? null : +(v * 100).toFixed(3)));
  const lsTsOption: any = {
    grid: grid({ top: 20 }),
    tooltip: tooltip({ valueFormatter: (v: number) => (v == null ? DASH : `${v > 0 ? '+' : ''}${v.toFixed(3)}%`) }),
    xAxis: catAxis(dates, axisInterval(dates.length, 8)),
    yAxis: valAxis((v: number) => `${v.toFixed(1)}%`),
    series: [{
      name: '多空日收益', type: 'bar', barMaxWidth: 3,
      data: lsDailyPct.map((v) => ({ value: v, itemStyle: { color: v != null && v < 0 ? 'rgba(5,150,105,0.55)' : 'rgba(225,29,72,0.5)' } })),
      markLine: zeroLine,
    }],
  };

  // ── 日收益分布 ──
  const dist = gb?.ls_dist ?? null;
  const distBars: Array<[number, number]> = dist?.bin_edges?.length
    ? dist.counts.map((c, i) => [+(((dist.bin_edges[i] + dist.bin_edges[i + 1]) / 2) * 100).toFixed(3), c])
    : [];
  const distOption: any = {
    grid: grid({ left: 42, bottom: 30 }),
    tooltip: tooltip({ valueFormatter: (v: number) => `${v.toFixed(2)}%` }),
    xAxis: catAxis(distBars.map(([c]) => `${c}%`), Math.max(1, Math.floor(distBars.length / 7))),
    yAxis: valAxis(),
    series: [{
      type: 'bar', barMaxWidth: 12,
      data: distBars.map(([c, n]) => ({ value: n, itemStyle: { color: c < 0 ? 'rgba(5,150,105,0.55)' : 'rgba(225,29,72,0.5)' } })),
      markLine: {
        silent: true, symbol: 'none',
        label: { fontSize: 9, color: WARN, formatter: (p: any) => p.name || '' },
        lineStyle: { color: WARN, type: 'dashed' },
        data: dist?.var_95 != null
          ? [{ xAxis: `${+(dist.var_95 * 100).toFixed(3)}%`, name: `VaR95 ${(dist.var_95 * 100).toFixed(2)}%` }]
          : [],
      },
    }],
  };

  // ── 月度热力图（与概览同一构造，但此处是主图） ──
  const monthOption: any = gb?.ls_monthly?.matrix?.length ? monthlyHeat(gb.ls_monthly) : null;

  // ── 持有期扫描 ──
  const sweep = gb?.holding_sweep ?? [];
  const sweepOption: any = {
    grid: grid({ top: 28, bottom: 30 }),
    tooltip: tooltip({ valueFormatter: (v: number) => (v == null ? DASH : `${v > 0 ? '+' : ''}${(v * 100).toFixed(2)}%`) }),
    legend: { show: true, right: 0, top: 0, itemWidth: 10, itemHeight: 6, textStyle: { fontSize: 10, color: NEUTRAL } },
    xAxis: catAxis(sweep.map((s) => `持有 ${s.hold_days} 日`), 0),
    yAxis: valAxis((v: number) => `${(v * 100).toFixed(0)}%`),
    series: [
      { name: '毛收益', type: 'bar', barMaxWidth: 26, data: sweep.map((s) => s.gross_return), itemStyle: { color: 'rgba(99,102,241,0.45)', borderRadius: 3 } },
      { name: '净收益', type: 'bar', barMaxWidth: 26, data: sweep.map((s) => s.net_return), itemStyle: { color: UP, borderRadius: 3 } },
    ],
  };

  // ── 拥挤度 ──
  const crowd = rb?.crowding;

  return (
    <div className="flex flex-col gap-3 min-h-0">
      {!gb && (
        <Degraded reason={gbDeg?.reason ?? '分组回测块不可用（需重跑构建）；下方保留升级前的分位图表。'} />
      )}

      {/* ① 主图：多空净值 + 回撤 */}
      <div className="grid grid-cols-2 gap-3">
        <ChartShell
          primary
          title={`多空 G${lg} / G${sg} 累计净值`}
          info={`多头腿 G${lg} 权重 +50%、空头腿 G${sg} 权重 −50%（美元中性、总杠杆 100%），每日再平衡、等权持有。此处为**毛收益**，不含交易成本。G1 = 因子值最小 … G10 = 因子值最大，与 IC 符号无关。`}
          hint={`累计 ${fmtPct(gb?.ls_cum?.length ? (gb.ls_cum[gb.ls_cum.length - 1] ?? 1) - 1 : null, 1)} · 最大回撤 ${fmtPct(gb?.ls_dd, 2)}`}
        >
          <div className="h-[250px] min-w-0"><EChartsChart option={navOption} /></div>
        </ChartShell>

        <ChartShell
          primary
          title="多空回撤"
          info="组合累计净值相对历史高点的回撤（≤0）。阴影为最深的三个回撤区间 —— 关注**恢复用了多久**，久未恢复说明信号可能已失效。"
          hint={gb?.ls_dd_episodes?.[0] ? `最深 ${fmtPct(gb.ls_dd_episodes[0].dd, 2)} · ${gb.ls_dd_episodes[0].days} 天` : undefined}
        >
          <div className="h-[250px] min-w-0"><EChartsChart option={ddOption} /></div>
        </ChartShell>
      </div>

      {/* ② 可交易轨 vs 理想轨 */}
      <ChartShell
        primary
        title="可交易口径 vs 理想口径"
        info="理想口径假设任何价位都能成交；可交易口径在建仓日剔除涨停（多头腿买不进）与跌停/停牌（空头腿卖不出）的股票。"
        hint={trad?.available ? `被挡 ${trad.blocked_days ?? 0} 天` : undefined}
      >
        {trad?.available && hasData(tradCum) ? (
          <div className="flex flex-col gap-2 min-h-0">
            <div className="h-[220px] min-w-0"><EChartsChart option={dualOption} /></div>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-slate-500">
              <span className="font-mono">理想口径累计 <b className="text-indigo-600">{fmtPct(trad.ideal_cum_end, 2)}</b></span>
              <span className="font-mono">可交易口径累计 <b className="text-amber-600">{fmtPct(trad.tradable_cum_end, 2)}</b></span>
              <span className="font-mono">差额 <b className="text-slate-800">{fmtPct(trad.lost_return, 2)}</b></span>
              <span className="font-mono">多头腿累计被挡 {fmtNum(trad.blocked_long_total, 0)} 只次</span>
              <span className="font-mono">空头腿累计被挡 {fmtNum(trad.blocked_short_total, 0)} 只次</span>
            </div>
            <div className="rounded-lg bg-amber-50/70 px-2 py-1 text-[10px] leading-relaxed text-amber-700">
              {trad.note || '「双轨差额」表达的是**理想口径高估了多少**，不是「策略会亏这么多」。'}
            </div>
          </div>
        ) : (
          <div className="h-[200px]"><Degraded reason={trad?.reason ?? '该数据集尚未构建可交易轨，需重跑构建'} /></div>
        )}
      </ChartShell>

      {/* ③ 两条腿各自的样子 */}
      <div className="grid grid-cols-2 gap-3">
        <ChartShell
          title="两条腿的累计收益"
          info={`多头腿 = 持有 G${lg} 的净值；空头腿 = **做空 G${sg} 的 P&L**，按 ∏(1−r)−1 复利计算（不是把多头累计取负 —— 长区间两者会分叉）。`}
          hint={`多头回撤 ${fmtPct(gb?.long_dd, 2)} · 空头回撤 ${fmtPct(gb?.short_dd, 2)}`}
        >
          <div className="h-[220px] min-w-0"><EChartsChart option={legOption} /></div>
        </ChartShell>

        <ChartShell title="各组日均收益" info="单调梯度才说明因子在整个分布上有效；只有两端翘说明信号集中在尾部，中间组不可用。"
          hint={`G1→G10 价差 ${fmtPct(gDaily.length ? (gDaily[gDaily.length - 1] ?? 0) - (gDaily[0] ?? 0) : null, 3)}`}>
          <div className="h-[220px] min-w-0"><EChartsChart option={groupDailyOption} /></div>
        </ChartShell>
      </div>

      {/* ④ 保留的既有图表：分位净值曲线（主图尺寸，加图例）+ 换手率 */}
      <ChartShell
        primary
        title="分位净值曲线"
        info="每组的累计净值（按日折算）。绿 = 因子值低、红 = 因子值高。虚线为 Q10−Q1 极值价差，与上方可配的 G3/G9 不是同一个组合。"
        hint="绿=因子值低 红=因子值高"
      >
        <div className="h-[280px] min-w-0"><EChartsChart option={quantileCurveOption(detail, true)} /></div>
      </ChartShell>

      <div className="grid grid-cols-2 gap-3">
        <ChartShell title="组日均换手" info={`G1–G10 各组自身的日均单边换手（成员变动比例）。高亮的两组是当前多空腿。两腿合计的换手见指标环 Turnover。`}
          hint={`多空腿合计 ${fmtPct(gb?.turnover_ls, 1)}`}>
          <div className="h-[210px] min-w-0"><EChartsChart option={groupTurnOption} /></div>
        </ChartShell>

        <ChartShell title="换手率时序" info="**截面分位成员迁移比例**（当日换组股票数 / 有效股票数），不是组合换手。"
          hint={`均值 ${detail.turnover_mean ? (detail.turnover_mean * 100).toFixed(0) : DASH}%`}>
          <div className="h-[210px] min-w-0"><EChartsChart option={turnoverOption(detail)} /></div>
        </ChartShell>
      </div>

      {/* ⑤ 日收益的形状 */}
      <ChartShell title="多空日收益时序" info="G3/G9 多空组合的逐日毛收益。看的是有没有「长期单边」或「某段时间特别猛」——后者通常是行情而非能力。">
        <div className="h-[200px] min-w-0"><EChartsChart option={lsTsOption} /></div>
      </ChartShell>

      <div className="grid grid-cols-2 gap-3">
        <ChartShell
          title="多空日收益分布"
          info="日收益直方图。VaR95 为 5% 分位（历史法），CVaR 为超过 VaR 那部分的条件均值（尾部平均亏损）。超额峰度为正说明极端日比正态预期更频繁。"
          hint={dist ? `μ=${fmtPct(dist.mu, 3)} σ=${fmtPct(dist.sigma, 2)} n=${dist.n}` : undefined}
        >
          <div className="flex flex-col min-h-0">
            <div className="h-[200px] min-w-0"><EChartsChart option={distOption} /></div>
            {dist && (
              <div className="mt-1 grid grid-cols-3 gap-x-3 gap-y-0.5 text-[10px]">
                <Stat k="偏度" v={fmtNum(dist.skew, 2)} />
                <Stat k="超额峰度" v={fmtNum(dist.kurt, 2)} />
                <Stat k="VaR 95" v={fmtPct(dist.var_95, 2)} />
                <Stat k="CVaR 95" v={fmtPct(dist.cvar_95, 2)} />
                <Stat k="VaR 99" v={fmtPct(dist.var_99, 2)} />
                <Stat k="CVaR 99" v={fmtPct(dist.cvar_99, 2)} />
              </div>
            )}
          </div>
        </ChartShell>

        <ChartShell title="多空月度收益" info="按年月聚合的多空收益。连续同色的月份越多，越像「一段行情」而不是持续能力。"
          hint={gb?.ls_monthly ? `${gb.ls_monthly.years.length} 年 × ${gb.ls_monthly.months.length} 月` : undefined}>
          {monthOption
            ? <div className="h-[240px] min-w-0"><EChartsChart option={monthOption} /></div>
            : <div className="h-[240px]"><Degraded compact reason="月度矩阵不可用" /></div>}
        </ChartShell>
      </div>

      {/* ⑥ 持有期与拥挤度 */}
      <div className="grid grid-cols-2 gap-3">
        <ChartShell
          title="持有期扫描"
          info="按不同前瞻期重算的组合收益。找「净收益最高」的持有期 —— 换得更勤不一定更赚。"
          hint={sweep.length ? `最优 ${bestHold(sweep)?.hold_days} 日` : undefined}
        >
          {sweep.length ? (
            <div className="flex flex-col gap-2 min-h-0">
              <div className="h-[190px] min-w-0"><EChartsChart option={sweepOption} /></div>
              <div className="rounded-lg bg-amber-50/70 px-2 py-1 text-[10px] leading-relaxed text-amber-700">
                ⚠ 本图用的是 <b>Q10−Q1 极值价差</b>（构建期只存了这一种前瞻期序列），与页面上可配的
                G{lg}/G{sg} <b>不是同一个组合</b>。此处只用于横向比较「哪个持有期更优」。
              </div>
            </div>
          ) : (
            <div className="h-[230px]"><Degraded compact reason="需要多前瞻期序列，该快照未构建" /></div>
          )}
        </ChartShell>

        <ChartShell
          title="拥挤度"
          info="换手分位与 IC 自相关的合成分。拥挤度高意味着大量资金在用同一信号 —— 收益会被套利掉，且回撤时更拥挤。"
          hint={crowd?.score == null ? undefined : `分位 ${fmtPct(crowd.turnover_pct, 0)}`}
        >
          {crowd?.score == null ? (
            <Degraded compact reason="该快照未构建拥挤度（需重跑构建）" />
          ) : (
            <div className="flex flex-col gap-2">
              <div className="flex items-baseline gap-3">
                <span className="text-3xl font-black font-mono text-slate-800">{crowd.score.toFixed(2)}</span>
                <span className={`text-xs font-bold ${crowd.score >= 0.7 ? 'text-rose-600' : crowd.score >= 0.4 ? 'text-amber-600' : 'text-emerald-600'}`}>
                  {crowd.score >= 0.7 ? '拥挤' : crowd.score >= 0.4 ? '中等' : '宽松'}
                </span>
              </div>
              <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
                <div className="h-full rounded-full transition-all"
                  style={{ width: `${Math.min(100, crowd.score * 100)}%`, background: crowd.score >= 0.7 ? UP : crowd.score >= 0.4 ? WARN : DOWN }} />
              </div>
              <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[10px]">
                <Stat k="换手分位" v={fmtPct(crowd.turnover_pct, 0)} />
                <Stat k="IC 自相关 lag1" v={fmtNum(crowd.ic_autocorr_lag1, 3)} />
                <Stat k="有效天数" v={fmtNum(crowd.n_days, 0)} />
              </div>
              {crowd.note && <div className="text-[10px] leading-relaxed text-slate-400">{crowd.note}</div>}
            </div>
          )}
        </ChartShell>
      </div>

      {/* ⑦ 读数补充 */}
      <ChartShell title="分组统计表" info="分组回测的精确读数。空头腿的「做空累计」按 ∏(1−r)−1 复利，而非取负多头累计。">
        <div className="overflow-x-auto">
          <table className="w-full text-[11px]">
            <tbody>
              {groupStats(detail, gb, rb, lg, sg, lsCum, longPct, shortPct).map(([k, v, note], i) => (
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
    </div>
  );
};

const Stat = ({ k, v }: { k: string; v: string }) => (
  <div className="flex items-baseline justify-between gap-1">
    <span className="text-slate-400">{k}</span>
    <span className="font-mono font-bold text-slate-700">{v}</span>
  </div>
);

interface MonthlyMatrix {
  years: number[];
  months: number[];
  matrix: Array<Array<number | null>>;
}

/** 与概览页签同一张图：月×年热力图，对称色域围绕 0 */
function monthlyHeat(m: MonthlyMatrix): any {
  const data: Array<[number, number, number | string]> = [];
  let lo = 0;
  let hi = 0;
  m.matrix.forEach((row, yi) => {
    row.forEach((v, mi) => {
      if (v == null || !Number.isFinite(v)) { data.push([mi, yi, '-']); return; }
      lo = Math.min(lo, v); hi = Math.max(hi, v);
      data.push([mi, yi, +v.toFixed(4)]);
    });
  });
  const bound = Math.max(Math.abs(lo), Math.abs(hi), 1e-4);
  return {
    grid: { left: 44, right: 16, top: 12, bottom: 30 },
    tooltip: {
      ...tooltip(), trigger: 'item',
      formatter: (p: any) => (p.value?.[2] === '-' ? `${m.years[p.value[1]]}年${m.months[p.value[0]]}月：无数据`
        : `${m.years[p.value[1]]}年${m.months[p.value[0]]}月：${(p.value[2] * 100).toFixed(2)}%`),
    },
    xAxis: { type: 'category', data: m.months.map((x) => `${x}月`), axisTick: { show: false }, axisLine: { lineStyle: { color: '#e2e8f0' } }, axisLabel: { fontSize: 9, color: NEUTRAL }, splitArea: { show: true } },
    yAxis: { type: 'category', data: m.years.map(String), axisTick: { show: false }, axisLine: { lineStyle: { color: '#e2e8f0' } }, axisLabel: { fontSize: 9, color: NEUTRAL }, splitArea: { show: true } },
    visualMap: {
      min: -bound, max: bound, calculable: false, orient: 'horizontal',
      left: 'center', bottom: 0, itemWidth: 10, itemHeight: 60,
      textStyle: { fontSize: 9, color: NEUTRAL },
      inRange: { color: [DOWN, '#a7f3d0', '#f8fafc', '#fecdd3', UP] },
    },
    series: [{
      type: 'heatmap', data, label: { show: false },
      itemStyle: { borderColor: '#fff', borderWidth: 1 },
      emphasis: { itemStyle: { shadowBlur: 6, shadowColor: 'rgba(0,0,0,0.2)' } },
    }],
  };
}

function bestHold(sweep: NonNullable<GroupBlock['holding_sweep']>) {
  const ok = sweep.filter((s) => s.net_return != null);
  if (!ok.length) return null;
  return ok.reduce((a, b) => ((b.net_return ?? -Infinity) > (a.net_return ?? -Infinity) ? b : a));
}

/** 曲线末值（%）；空序列返回 null。 */
const lastPct = (curve: Array<number | null>): number | null =>
  curve.length ? curve[curve.length - 1] : null;

function groupStats(
  detail: FactorDetail,
  gb: GroupBlock | null,
  rb: RobustBlock | null,
  lg: number,
  sg: number,
  lsReturnPct: Array<number | null>,
  longPct: Array<number | null>,
  shortPct: Array<number | null>,
): Array<[string, string, string]> {
  const lsLast = lastPct(lsReturnPct);
  const loLast = lastPct(longPct);
  const shLast = lastPct(shortPct);
  const dist = gb?.ls_dist ?? null;
  const sweep = gb?.holding_sweep ?? [];
  const best = sweep.length ? bestHold(sweep) : null;
  return [
    ['多空累计收益', fmtPct(lsLast == null ? null : lsLast / 100, 2), `G${lg} 多 50% / G${sg} 空 50%，毛收益`],
    ['多空最大回撤', fmtPct(gb?.ls_dd, 2), '累计净值相对历史高点'],
    ['多头腿累计', fmtPct(loLast == null ? null : loLast / 100, 2), `持有 G${lg}`],
    ['空头腿做空累计', fmtPct(shLast == null ? null : shLast / 100, 2), `做空 G${sg} · ∏(1−r) 复利`],
    ['多空日收益 μ / σ', `${fmtPct(dist?.mu, 3)} / ${fmtPct(dist?.sigma, 2)}`, `n=${dist?.n ?? DASH}`],
    ['日收益偏度 / 峰度', `${fmtNum(dist?.skew, 2)} / ${fmtNum(dist?.kurt, 2)}`, '超额峰度（正态=0）'],
    ['CVaR 95 / 99', `${fmtPct(dist?.cvar_95, 2)} / ${fmtPct(dist?.cvar_99, 2)}`, '尾部条件均值（历史法）'],
    ['两腿合计换手', fmtPct(gb?.turnover_ls, 1), 'G3/G9 两腿日均单边平均'],
    ['最优持有期', best ? `${best.hold_days} 日（净 ${fmtPct(best.net_return, 2)}）` : DASH, '⚠ Q10−Q1 口径，非当前 G 组'],
    ['拥挤度', fmtNum(rb?.crowding?.score, 3), '换手分位与 IC 自相关的合成'],
    ['样本天数', String(detail.n_dates ?? DASH), `回看窗口 ${gb?.dates?.length ?? detail.dates.length} 天`],
  ];
}
