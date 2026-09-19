/**
 * 「概览」页签 —— 一屏回答「这个因子能不能用」。
 *
 * 编排逻辑：先给**结论性**的两块（机构评分卡、分位收益梯度），再给两条基线
 * （净值 / 换手），然后是**风险形状**（年月热力、累计 IC），最后是
 * **可实施性**（成本能吃多少、容量有多大）与读数表。
 *
 * 五维评分卡**直接读 `/api/v1/eval/scores` 的既有评分**，本页不重算 —— 重算
 * 就等于在同一平台里造出第二套评分口径（本项目既有铁律）。
 */

import React, { useEffect, useState } from 'react';
import { EChartsChart } from '../../../../../components/common/EChartsChart';
import { listScores } from '../../../../eval-center/services/evalCenterService';
import type { EvalScoreRow } from '../../../../eval-center/types/evalCenter';
import type { FactorBlocks, FactorDetail, CostBlock, GroupBlock, IcBlock, DegradedBlock, HeadlineBlock } from '../../../types/factorReport';
import {
  ACCENT, ACCENT_2, ChartShell, DASH, Degraded, DOWN, NEUTRAL, UP, WARN,
  axisInterval, catAxis, fmtDate, fmtNum, fmtPct, grid, hasData, tooltip, valAxis, zeroLine,
} from './chartKit';
import { quantileBarOption, quantileCurveOption, quantileSpread, turnoverOption } from './legacyCharts';

interface Props {
  factor: string;
  dataset: string;
  detail: FactorDetail | null;
  blocks: FactorBlocks | null;
  costBps: number;
  onCost: (bps: number) => void;
}

export const OverviewTab: React.FC<Props> = ({ factor, detail, blocks, costBps, onCost }) => {
  const [score, setScore] = useState<EvalScoreRow | null>(null);
  const [scoreErr, setScoreErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setScore(null);
    setScoreErr(null);
    if (!factor) return;
    listScores({ objectType: 'factor', objectId: factor, limit: 1 })
      .then((r) => { if (alive) setScore(r?.data?.[0] ?? null); })
      .catch((e: unknown) => {
        if (alive) setScoreErr(e instanceof Error ? e.message : '评分卡读取失败');
      });
    return () => { alive = false; };
  }, [factor]);

  // 从联合类型上判 available 再断言（在具体类型上比 `!== false` 会被 TS 判为无交集）
  const icRaw = blocks?.ic_block;
  const ic = icRaw && icRaw.available !== false ? (icRaw as IcBlock) : null;
  const gbRaw = blocks?.group_block;
  const gb = gbRaw && gbRaw.available !== false ? (gbRaw as GroupBlock) : null;
  const costRaw = blocks?.cost_block;
  const cost = costRaw && costRaw.available !== false ? (costRaw as CostBlock) : null;
  const costDeg = costRaw && costRaw.available === false ? (costRaw as DegradedBlock) : null;
  // ICIR 与净口径指标只在 headline 块里
  const head = (blocks?.headline as HeadlineBlock | undefined) ?? null;

  if (!detail || detail.empty) return <Degraded reason={detail?.reason || '请选择一个因子'} />;

  return (
    <div className="flex flex-col gap-3 min-h-0">
      {/* ① 结论性两块：机构评分 + 收益梯度 */}
      <div className="grid grid-cols-2 gap-3">
        <ChartShell
          primary
          title="机构评分卡"
          info="平台「评估中心」的既有五维评分（预测力 / 稳定性 / 独立性 / 质量闸门 / 覆盖），本页只读取不重算。红线维度触发时总分封顶 59。"
          hint={score ? `${score.snapshot_date ?? ''} · 分位口径` : undefined}
        >
          {scoreErr ? (
            <div className="h-[240px]"><Degraded compact reason={scoreErr} /></div>
          ) : !score ? (
            <div className="h-[240px] flex items-center justify-center text-[11px] text-slate-400">
              未评分（该因子不在评估中心快照内）
            </div>
          ) : (
            <ScoreRadar row={score} />
          )}
        </ChartShell>

        <ChartShell primary title="分位平均前瞻收益" info="G1 = 因子值最小 … G10 = 因子值最大，与 IC 符号无关。单调递增说明因子值越大收益越高；两端翘、中间平则说明信号只集中在尾部。"
          hint={`Q10−Q1 = ${quantileSpread(detail) > 0 ? '+' : ''}${quantileSpread(detail)}%`}>
          <div className="h-[240px] min-w-0"><EChartsChart option={quantileBarOption(detail)} /></div>
        </ChartShell>
      </div>

      {/* ② 两条基线 */}
      <div className="grid grid-cols-2 gap-3">
        <ChartShell title="分位净值曲线" hint="按日折算 · 绿=因子值低 红=因子值高" info="每组的累计净值。只看首尾两组不够——中间组是否单调才是因子质量的判据。">
          <div className="h-[220px] min-w-0"><EChartsChart option={quantileCurveOption(detail)} /></div>
        </ChartShell>

        <ChartShell title="换手率" info="**截面分位成员迁移比例**（当日换组股票数 / 有效股票数），不是组合换手。组合的真实单边换手见分组回测页签与指标环。"
          hint={`均值 ${detail.turnover_mean ? (detail.turnover_mean * 100).toFixed(0) : DASH}%`}>
          <div className="h-[220px] min-w-0"><EChartsChart option={turnoverOption(detail)} /></div>
        </ChartShell>
      </div>

      {/* ③ 风险形状 */}
      <div className="grid grid-cols-2 gap-3">
        <ChartShell title="多空月度收益热力图" info="按年月聚合的多空组合收益。连续同色的月份越多，说明收益越像「一段行情」而不是持续能力。"
          hint={gb?.ls_monthly ? `${gb.ls_monthly.years.length} 年 × ${gb.ls_monthly.months.length} 月` : undefined}>
          {gb?.ls_monthly?.matrix?.length
            ? <div className="h-[220px] min-w-0"><EChartsChart option={monthlyHeatOption(gb.ls_monthly)} /></div>
            : <div className="h-[220px]"><Degraded compact reason="月度矩阵不可用（需重跑构建）" /></div>}
        </ChartShell>

        <ChartShell title="多空累计 IC" info="全窗口累积 IC（不受回看期截断）。斜率稳定才说明信息是持续产生的。"
          hint={ic ? `${ic.cum_dates_full?.length ?? 0} 天` : undefined}>
          {hasData(ic?.ic_cum_full)
            ? <div className="h-[220px] min-w-0"><EChartsChart option={cumIcOption(ic!)} /></div>
            : <div className="h-[220px]"><Degraded compact reason="累计 IC 不可用（需重跑构建）" /></div>}
        </ChartShell>
      </div>

      {/* ④ 可实施性：成本能吃掉多少、容量有多大 */}
      <div className="grid grid-cols-3 gap-3">
        <ChartShell
          primary
          className="col-span-2"
          title="成本敏感性"
          info="按不同双边成本重算净 IR / Fitness。盈亏平衡成本 = 净 IR 归零的 bps —— 它低于你的真实交易成本时，这个因子在纸面上再好看也不可交易。"
          hint={
            cost?.sensitivity
              ? cost.sensitivity.break_even_note
                ? '该方向毛收益为负 · 无正的盈亏平衡点'
                : `盈亏平衡 ${cost.sensitivity.break_even_bps == null ? DASH : `${cost.sensitivity.break_even_bps.toFixed(1)}bp`}`
              : undefined
          }
        >
          {cost?.sensitivity?.rows?.length ? (
            <div className="flex flex-col gap-2 min-h-0">
              <div className="h-[190px] min-w-0"><EChartsChart option={costOption(cost.sensitivity.rows, costBps)} /></div>
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className="text-[10px] font-bold text-slate-400">试算成本</span>
                {[0, 10, 20, 30, 50].map((b) => (
                  <button
                    key={b}
                    onClick={() => onCost(b)}
                    className={`rounded-full px-2 py-[2px] text-[10px] font-bold transition-colors ${
                      costBps === b ? 'bg-indigo-600 text-white' : 'bg-slate-100 text-slate-500 hover:bg-slate-200'
                    }`}
                  >
                    {b}bp
                  </button>
                ))}
                <span className="text-[10px] text-slate-400">
                  （点击后全页重算净口径，指标环与分组回测同步变化）
                </span>
              </div>
              {cost.sensitivity.break_even_note && (
                <div className="rounded-lg bg-amber-50/70 border border-amber-200/70 px-2 py-1.5 text-[10px] leading-relaxed text-amber-700">
                  {cost.sensitivity.break_even_note}
                </div>
              )}
            </div>
          ) : (
            <div className="h-[230px]"><Degraded reason={costDeg?.reason ?? '成本敏感性不可用（需重跑构建）'} /></div>
          )}
        </ChartShell>

        <ChartShell
          title="容量估算"
          info="按持仓股票的中位日成交额 × 参与率假设反推可承载规模。**这是简化模型，不是实测** —— 参与率与冲击系数都是假设值，实盘还受冲击成本、涨跌停、券源约束。"
        >
          <CapacityCard cost={cost} />
        </ChartShell>
      </div>

      {/* ⑤ 读数补充 */}
      <ChartShell title="关键统计" info="图表看形态，此表给精确读数。所有「有效天数」为参与该指标计算的交易日数——样本不足时不年化。">
        <div className="overflow-x-auto">
          <table className="w-full text-[11px]">
            <tbody>
              {keyStats(detail, blocks, ic, gb, score, head).map(([k, v, note], i) => (
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

// ─────────────────────────── 子块 ───────────────────────────

const DIM_ORDER = ['predictive', 'stability', 'independence', 'quality', 'coverage'];

function ScoreRadar({ row }: { row: EvalScoreRow }) {
  const dims = Object.entries(row.dimensions || {});
  // 顺序按已知维度优先，其余按后端返回序 —— 不假设后端一定给全五维
  const ordered = [
    ...DIM_ORDER.map((k) => [k, row.dimensions?.[k]] as const).filter(([, d]) => d),
    ...dims.filter(([k]) => !DIM_ORDER.includes(k)),
  ];
  const usable = ordered.filter(([, d]) => d?.score != null && Number.isFinite(d.score));
  const missing = ordered.filter(([, d]) => d?.score == null).map(([, d]) => d?.label ?? '未知');

  if (!usable.length) {
    return <div className="h-[240px]"><Degraded compact reason={`该因子已评分但五维均不可评${missing.length ? `（${missing.join('、')}）` : ''}`} /></div>;
  }

  const opt: any = {
    tooltip: { ...tooltip(), trigger: 'item' },
    radar: {
      // 半径随容器收敛，避免窄栏里顶到边缘
      radius: '62%',
      center: ['50%', '54%'],
      indicator: usable.map(([, d]) => ({ name: d!.label || '维度', max: 100 })),
      axisName: { fontSize: 10, color: '#64748b' },
      splitLine: { lineStyle: { color: '#eef2f7' } },
      splitArea: { areaStyle: { color: ['rgba(248,250,252,0.6)', 'rgba(255,255,255,0)'] } },
      axisLine: { lineStyle: { color: '#eef2f7' } },
    },
    series: [{
      type: 'radar',
      symbolSize: 5,
      data: [{
        value: usable.map(([, d]) => d!.score),
        name: '维度得分',
        lineStyle: { color: ACCENT, width: 2 },
        itemStyle: { color: ACCENT },
        areaStyle: { color: 'rgba(99,102,241,0.18)' },
      }],
    }],
  };

  return (
    <div className="flex flex-col min-h-0">
      <div className="flex items-baseline gap-2 px-1">
        <span className={`text-2xl font-black font-mono ${
          row.score == null ? 'text-slate-400' : row.score >= 80 ? 'text-rose-600' : row.score >= 60 ? 'text-indigo-600' : 'text-emerald-600'
        }`}>
          {row.score == null ? DASH : row.score.toFixed(1)}
        </span>
        <span className="text-sm font-black text-slate-500">{row.grade ?? DASH}</span>
        {row.red_line_failed?.length > 0 && (
          <span className="rounded-full bg-rose-50 px-2 py-[1px] text-[10px] font-bold text-rose-600">
            红线：{row.red_line_failed.join('、')}
          </span>
        )}
        {row.low_confidence && (
          <span className="rounded-full bg-amber-50 px-2 py-[1px] text-[10px] font-bold text-amber-600">低置信</span>
        )}
      </div>
      <div className="h-[210px] min-w-0"><EChartsChart option={opt} /></div>
      {missing.length > 0 && (
        <div className="px-1 text-[10px] text-amber-600">未参与评分：{missing.join('、')}（缺失维度按剩余权重归一）</div>
      )}
    </div>
  );
}

function CapacityCard({ cost }: { cost: CostBlock | null }) {
  const cap = cost?.capacity;
  if (!cap) {
    return <Degraded compact reason={cost?.reason ?? (cost ? '容量估算不可用' : '成本块不可用（需重跑构建）')} />;
  }
  const aum = cap.est_aum;
  return (
    <div className="flex flex-col gap-2">
      <div>
        <div className="text-[10px] font-bold text-slate-400">估算可承载规模</div>
        <div className="text-xl font-black font-mono text-slate-800">
          {aum == null ? DASH : aum >= 1e8 ? `${(aum / 1e8).toFixed(1)} 亿` : `${(aum / 1e4).toFixed(0)} 万`}
        </div>
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[10px]">
        <Cell k="参与率假设" v={cap.assumed_participation == null ? DASH : `${(cap.assumed_participation * 100).toFixed(0)}%`} />
        <Cell k="持仓只数" v={cap.n_positions == null ? DASH : String(cap.n_positions)} />
        <Cell k="中位成交额" v={cap.median_amount == null ? DASH : `${(cap.median_amount / 1e4).toFixed(0)} 万`} />
        <Cell k="组合换手" v={cap.turnover == null ? DASH : `${(cap.turnover * 100).toFixed(0)}%`} />
      </div>
      {cap.median_amount_scope && (
        <div className="text-[10px] text-slate-400">口径：{cap.median_amount_scope}</div>
      )}
      <div className="rounded-lg bg-amber-50/70 px-2 py-1 text-[10px] leading-relaxed text-amber-700">
        {cap.note || '简化模型，含显式假设'}
      </div>
    </div>
  );
}

const Cell = ({ k, v }: { k: string; v: string }) => (
  <div className="flex items-baseline justify-between gap-1">
    <span className="text-slate-400">{k}</span>
    <span className="font-mono font-bold text-slate-700">{v}</span>
  </div>
);

// ─────────────────────────── option 构造 ───────────────────────────

interface MonthlyMatrix {
  years: number[];
  months: number[];
  matrix: Array<Array<number | null>>;
}

function monthlyHeatOption(m: MonthlyMatrix): any {
  const data: Array<[number, number, number | string]> = [];
  let lo = 0;
  let hi = 0;
  m.matrix.forEach((row, yi) => {
    row.forEach((v, mi) => {
      if (v == null || !Number.isFinite(v)) {
        data.push([mi, yi, '-']); // ECharts 约定：'-' 即无数据
        return;
      }
      lo = Math.min(lo, v);
      hi = Math.max(hi, v);
      data.push([mi, yi, +v.toFixed(4)]);
    });
  });
  // 对称色域：涨红跌绿围绕 0 对称，否则「0 附近」会被染成某一边
  const bound = Math.max(Math.abs(lo), Math.abs(hi), 1e-4);
  return {
    grid: { left: 44, right: 16, top: 12, bottom: 30 },
    tooltip: {
      ...tooltip(),
      trigger: 'item',
      formatter: (p: any) => (p.value?.[2] === '-' ? `${m.years[p.value[1]]}年${m.months[p.value[0]]}月：无数据`
        : `${m.years[p.value[1]]}年${m.months[p.value[0]]}月：${(p.value[2] * 100).toFixed(2)}%`),
    },
    xAxis: {
      type: 'category', data: m.months.map((x) => `${x}月`),
      axisTick: { show: false }, axisLine: { lineStyle: { color: '#e2e8f0' } },
      axisLabel: { fontSize: 9, color: NEUTRAL }, splitArea: { show: true },
    },
    yAxis: {
      type: 'category', data: m.years.map(String),
      axisTick: { show: false }, axisLine: { lineStyle: { color: '#e2e8f0' } },
      axisLabel: { fontSize: 9, color: NEUTRAL }, splitArea: { show: true },
    },
    visualMap: {
      min: -bound, max: bound, calculable: false, orient: 'horizontal',
      left: 'center', bottom: 0, itemWidth: 10, itemHeight: 60,
      textStyle: { fontSize: 9, color: NEUTRAL },
      inRange: { color: [DOWN, '#a7f3d0', '#f8fafc', '#fecdd3', UP] },
    },
    series: [{
      type: 'heatmap', data,
      label: { show: false },
      itemStyle: { borderColor: '#fff', borderWidth: 1 },
      emphasis: { itemStyle: { shadowBlur: 6, shadowColor: 'rgba(0,0,0,0.2)' } },
    }],
  };
}

function cumIcOption(ic: IcBlock): any {
  const dts = (ic.cum_dates_full ?? []).map(fmtDate);
  return {
    grid: grid({ top: 24 }),
    tooltip: tooltip(),
    legend: { show: true, right: 0, top: 0, itemWidth: 10, itemHeight: 6, textStyle: { fontSize: 10, color: NEUTRAL } },
    xAxis: catAxis(dts, axisInterval(dts.length, 6)),
    yAxis: valAxis((v: number) => v.toFixed(2)),
    series: [
      { name: '累计 IC', type: 'line', showSymbol: false, data: ic.ic_cum_full ?? [], lineStyle: { width: 2, color: ACCENT }, itemStyle: { color: ACCENT } },
      ...(hasData(ic.cum_ic_top_full) ? [{ name: '累计 Top 半', type: 'line', showSymbol: false, data: ic.cum_ic_top_full, lineStyle: { width: 1.2, color: UP, type: 'dashed' }, itemStyle: { color: UP } }] : []),
      ...(hasData(ic.cum_ic_bot_full) ? [{ name: '累计 Bottom 半', type: 'line', showSymbol: false, data: ic.cum_ic_bot_full, lineStyle: { width: 1.2, color: DOWN, type: 'dashed' }, itemStyle: { color: DOWN } }] : []),
    ],
  };
}

function costOption(rows: CostBlock['sensitivity']['rows'], activeBps: number): any {
  const bps = rows.map((r) => r.bps);
  const markLine = {
    ...zeroLine,
    data: [{ yAxis: 0 }],
  };
  return {
    grid: grid({ top: 26 }),
    tooltip: tooltip({ valueFormatter: (v: number) => (v == null ? DASH : v.toFixed(3)) }),
    legend: { show: true, right: 0, top: 0, itemWidth: 10, itemHeight: 6, textStyle: { fontSize: 10, color: NEUTRAL } },
    xAxis: catAxis(bps.map((b) => `${b}bp`), 0),
    yAxis: valAxis((v: number) => v.toFixed(2)),
    series: [
      {
        name: '净 IR', type: 'line', showSymbol: true, symbolSize: 6,
        data: rows.map((r) => (r.net_ir == null ? null : +r.net_ir.toFixed(4))),
        lineStyle: { width: 2, color: ACCENT }, itemStyle: { color: ACCENT },
        markLine: { ...markLine, silent: true, symbol: 'none', label: { fontSize: 9, color: NEUTRAL } },
        // 当前生效成本档：加一个高亮点，让曲线与页头控件对得上
        markPoint: {
          symbol: 'circle', symbolSize: 11,
          itemStyle: { color: WARN, borderColor: '#fff', borderWidth: 2 },
          label: { show: false },
          data: bps.includes(activeBps) ? [{ coord: [`${activeBps}bp`, rows.find((r) => r.bps === activeBps)?.net_ir ?? null] }] : [],
        },
      },
      {
        name: '净 Fitness', type: 'line', showSymbol: true, symbolSize: 6,
        data: rows.map((r) => (r.net_fitness == null ? null : +r.net_fitness.toFixed(4))),
        lineStyle: { width: 1.6, color: ACCENT_2, type: 'dashed' }, itemStyle: { color: ACCENT_2 },
      },
    ],
  };
}

// ─────────────────────────── 统计表 ───────────────────────────

function keyStats(
  detail: FactorDetail,
  blocks: FactorBlocks | null,
  ic: IcBlock | null,
  gb: GroupBlock | null,
  score: EvalScoreRow | null,
  head: HeadlineBlock | null,
): Array<[string, string, string]> {
  const sig = blocks?.significance as any;
  const turn = gb?.turnover_ls;
  const maxDD = gb?.ls_dd ?? null;
  return [
    ['样本天数', String(detail.n_dates ?? '—'), `有效 ${ic?.n_valid_mean != null ? Math.round(ic.n_valid_mean) : DASH} 只/日`],
    ['覆盖率', detail.coverage_mean == null ? DASH : `${(detail.coverage_mean * 100).toFixed(1)}%`, '因子有值股票数 / 全市场'],
    ['多空组合换手', turn == null ? DASH : `${(turn * 100).toFixed(1)}%`, 'G3/G9 两腿日均单边（≠ 上方截面迁移率）'],
    ['多空最大回撤', maxDD == null ? DASH : `${(maxDD * 100).toFixed(2)}%`, '组合累计净值最大回撤'],
    ['IC 均值 / ICIR', `${fmtNum(ic?.ic_mean ?? detail.ic_mean, 5)} / ${fmtNum(head?.icir, 4)}`, '秩相关 · ICIR 不年化'],
    ['IC 半衰期', ic?.half_life_days == null ? DASH : `${ic.half_life_days} 个交易日`, 'IC 衰减到一半所需天数'],
    ['中性化 IC', fmtNum(ic?.ic_neutral_mean, 5), `行业+市值中性 · ${ic?.ic_neutral_days ?? 0} 天`],
    ['普通 t / NW t', `${fmtNum(sig?.t_value, 2)} / ${fmtNum(sig?.nw_t_value, 2)}`, '差得越多，普通 t 的高估越严重'],
    ['BHY q 值', fmtNum(sig?.q_value_bhy, 4), `全库 ${sig?.n_factors_tested ?? DASH} 个因子多重检验校正后`],
    ['独立性 max|ρ|', fmtNum(ic?.independence?.max_corr, 3), `与最相关的 ${ic?.independence?.n_peers ?? DASH} 个同库因子`],
    ['评估中心评级', score ? `${score.grade ?? DASH}（${score.score ?? DASH}）` : '未评分', score?.snapshot_date ? `${score.snapshot_date} 快照` : '不在评分快照内'],
    ['毛 → 净 Returns', `${fmtPct(head?.returns, 1)} → ${fmtPct(head?.net_returns, 1)}`, '成本吃掉的部分'],
  ];
}
