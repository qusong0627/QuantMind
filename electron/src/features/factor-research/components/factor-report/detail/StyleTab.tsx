/**
 * 风格相关性页签：Barra 十大风格（**自算 CNE5 式口径**）。
 *
 * 回答两个问题：
 *   1. 这个因子到底在赌什么风格 —— 相关性表 / 排序条形 / 雷达 / 逐日时序；
 *   2. 它的超额是「真本事」还是「风格 beta」—— 把多空日收益与超额日收益
 *      分别对十大风格**纯因子收益**做时序回归，看 α 的 t 值。
 *
 * ⚠️ 风格产物（`build_style_factors.py`）未构建时整块降级，**不显示 0 也不留白**：
 * 「无数据」与「相关性恰好为 0」在视觉上无法区分，这是本项目已有教训。
 * 风格未构建是**正常降级路径**（该步是可选产物），故文案要给恢复动作而不是报错。
 */

import React from 'react';
import { EChartsChart } from '../../../../../components/common/EChartsChart';
import type {
  FactorBlocks, StyleAttribution, StyleBlock, StyleExposureRow,
} from '../../../types/factorReport';
import {
  ACCENT, DASH, Degraded, GRID, NEUTRAL, bySign, catAxis, ChartShell,
  fmtDate, fmtInt, fmtNum, fmtPct, grid, hasData, tooltip, valAxis, zeroLine,
} from './chartKit';

/** 有效天数低于此值：均值受抽样误差主导，排名与数值都不可信 → 行内标黄 */
const MIN_TRUST_DAYS = 60;
/** 图表统一高度（px）—— ECharts 按容器尺寸渲染，没有确定高度就是一块空白 */
const H = 240;

/** 风格产物缺失时的兜底文案（后端通常给了更具体的原因，优先用后端的） */
const NO_PRODUCT =
  '风格产物未构建：需先跑 backend/scripts/build_style_factors.py 生成十大风格暴露与纯因子收益，再重跑报告构建（属于可选产物缺失，不是报错）';

/** 自算口径声明 —— 常驻，避免把本地算的风格当成商业 Barra 读数 */
const CNE5_NOTE =
  '本页十大风格为平台**自算的 CNE5 式口径**（规模 / 贝塔 / 动量 / 残差波动 / 非线性规模 / 账面市值比 / 流动性 / 盈利收益 / 成长 / 杠杆），由本地行情与因子库自行构建，**不与商业 Barra（CNE5 / CNE6）数据完全可比**，仅用于内部横向比较与归因解读。';

const LS_TARGET =
  '回归对象：**多空组合日收益**（多头组 − 空头组）对十大风格**纯因子收益**的时序回归 —— 直接回答「这份超额是真本事还是风格 beta」；α 是剥离风格后剩下的日度超额。';
const EXCESS_TARGET =
  '回归对象：**相对基准的日度超额收益**（多头腿 − 基准指数）对十大风格**纯因子收益**的时序回归，口径与左图一致，仅被解释变量不同。';
const EXCESS_ATTR_REASON =
  '该快照未提供超额归因：基准收益不可用（基准取数失败或未选基准），或与风格收益的日期交集不足以回归。';

/** 相关 / β 配色：涨红跌绿；null 走中性灰（不可用 ≠ 0，不能画成红色） */
const valColor = (v: number | null): string => (v == null ? NEUTRAL : bySign(v));
/** 时序折线的色相轮转：十条约挤在一张图，靠色相分离而非图例文字 */
const styleHue = (i: number): string => `hsl(${(i * 36) % 360}, 60%, 50%)`;
const isNum = (v: number | null | undefined): v is number => v != null && Number.isFinite(v);

/** α 判词：把「超额是真本事还是风格 beta」这句话直接说出口 */
function alphaVerdict(t: number | null): { text: string; significant: boolean } {
  if (!isNum(t)) return { text: 't(α) 不可用（样本不足或回归奇异），无法判定 α 是否显著。', significant: false };
  const sig = Math.abs(t) > 2;
  const s = fmtNum(t, 2);
  return {
    significant: sig,
    text: sig
      ? `t(α) = ${s}，|t| > 2：α 显著非零 —— 剥离十大风格 beta 后仍有超额，这部分不是靠风格暴露赚来的。`
      : `t(α) = ${s}，|t| ≤ 2：α 与 0 无显著差异 —— 收益更可能由风格暴露（β）解释，不能算独立本事。`,
  };
}

// ─────────────────────────── 子块 ───────────────────────────

const Stat: React.FC<{ label: string; value: string; highlight?: boolean; title?: string }> = ({
  label, value, highlight, title,
}) => (
  <div className="flex flex-col">
    <span className="text-[9px] font-bold text-slate-400">{label}</span>
    <span className={`font-mono text-sm ${highlight ? 'font-black text-rose-600' : 'font-bold text-slate-700'}`} title={title}>
      {value}
    </span>
  </div>
);

/** 十大风格暴露表：顺序沿用后端的 |均值相关| 降序，前端不再重排 */
const ExposureTable: React.FC<{ rows: StyleExposureRow[] }> = ({ rows }) => (
  <div className="h-full min-h-0 overflow-auto">
    <table className="w-full text-[11px]">
      <thead className="sticky top-0 bg-white text-[10px] text-slate-400">
        <tr className="border-b border-slate-100">
          <th className="py-1 pr-2 text-left font-bold">#</th>
          <th className="py-1 pr-2 text-left font-bold">风格</th>
          <th className="py-1 pr-2 text-right font-bold">均值相关</th>
          <th className="py-1 pr-2 text-right font-bold">标准差</th>
          <th className="py-1 text-right font-bold">有效天数</th>
        </tr>
      </thead>
      <tbody className="font-mono">
        {rows.map((r) => {
          const thin = r.n_days < MIN_TRUST_DAYS;
          return (
            <tr key={r.style} className={`border-b border-slate-50 ${thin ? 'bg-amber-50/70' : ''}`}>
              <td className="py-1 pr-2 text-slate-400">{r.rank}</td>
              <td className="py-1 pr-2 font-sans">
                <span className="font-bold text-slate-700">{r.label}</span>
                <span className="ml-1 text-[9px] text-slate-400">{r.style}</span>
                {thin && (
                  <span className="ml-1 rounded bg-amber-100 px-1 text-[9px] font-bold text-amber-700"
                    title={`仅 ${r.n_days} 天有效：均值受抽样误差主导，排名与数值都不可信`}>
                    天数不足
                  </span>
                )}
              </td>
              <td className="py-1 pr-2 text-right font-bold" style={{ color: valColor(r.mean_corr) }}>
                {fmtNum(r.mean_corr, 3)}
              </td>
              {/* 标准差恒非负，逐项着色只会整列红 —— 留中性灰才读得出量级 */}
              <td className="py-1 pr-2 text-right text-slate-500">{fmtNum(r.std_corr, 3, false)}</td>
              <td className={`py-1 text-right ${thin ? 'font-bold text-amber-700' : 'text-slate-500'}`}>{fmtInt(r.n_days)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  </div>
);

/** 超额风格相关性汇总：为空时给**具体**原因，不显示空表头，也不假装「相关为 0」 */
const ExcessCorrTable: React.FC<{ rows: StyleBlock['excess_corr']; reason?: string }> = ({ rows, reason }) => {
  const list = rows ?? [];
  if (!list.length) {
    return (
      <div className="flex h-full min-h-0 items-center justify-center rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-4 text-center text-[10px] leading-relaxed text-slate-400">
        {reason || '超额风格相关性无数据（后端未给出原因，需查 style_block）'}
      </div>
    );
  }
  return (
    <div className="h-full min-h-0 overflow-auto">
      <table className="w-full text-[11px]">
        <thead className="sticky top-0 bg-white text-[10px] text-slate-400">
          <tr className="border-b border-slate-100">
            <th className="py-1 pr-2 text-left font-bold">风格</th>
            <th className="py-1 pr-2 text-right font-bold">超额相关性</th>
            <th className="py-1 text-right font-bold">有效天数</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {list.map((r) => (
            <tr key={r.style} className="border-b border-slate-50">
              <td className="py-1 pr-2 font-sans">
                <span className="font-bold text-slate-700">{r.label}</span>
                <span className="ml-1 text-[9px] text-slate-400">{r.style}</span>
              </td>
              <td className="py-1 pr-2 text-right font-bold" style={{ color: valColor(r.corr) }}>{fmtNum(r.corr, 3)}</td>
              <td className="py-1 text-right text-slate-500">{fmtInt(r.n_days)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

/**
 * 归因回归体：α / t / R² / n + 十个风格的 β 与 t。
 * 只出内容不出卡壳 —— 外层由调用方用 ChartShell 包（嵌套两层卡会变成双边框）。
 */
const AttributionBody: React.FC<{
  target: string;
  attr: StyleAttribution | null;
  reason: string;
  labels: Record<string, string>;
}> = ({ target, attr, reason, labels }) => (
  !attr ? <Degraded reason={reason} /> : (
    <div className="flex h-full min-h-0 flex-col gap-1.5 overflow-auto">
      <div className="text-[10px] leading-relaxed text-slate-500">{target}</div>

      <div className="flex flex-wrap items-end gap-x-4 gap-y-1 rounded-xl bg-slate-50/80 px-3 py-2">
        <div className="flex flex-col">
          <span className="text-[9px] font-bold text-slate-400">α（剥离风格后的日度超额）</span>
          <span className="font-mono text-xl font-black leading-tight" style={{ color: valColor(attr.alpha) }}
            title={`日度 α 原始值 ${fmtNum(attr.alpha, 8)}`}>
            {fmtPct(attr.alpha, 3)}
          </span>
        </div>
        <Stat label="t(α) · |t|>2 为显著" value={fmtNum(attr.t_alpha, 2)}
          highlight={isNum(attr.t_alpha) && Math.abs(attr.t_alpha) > 2} />
        {/* R² 用无符号格式：它天然落在 [0,1]，带 + 号会读成「收益」 */}
        <Stat label="R²（被风格解释的比例）" value={fmtNum(attr.r_squared, 3, false)}
          title="日度收益的方差中，能被十大风格纯因子收益线性解释的占比。R² 越高，越说明收益来自风格暴露而非独立 alpha。" />
        <Stat label="样本天数 n" value={fmtInt(attr.n)} />
      </div>

      <div className={`text-[10px] font-bold leading-relaxed ${alphaVerdict(attr.t_alpha).significant ? 'text-rose-600' : 'text-slate-500'}`}>
        {alphaVerdict(attr.t_alpha).text}
      </div>
      <div className="text-[10px] leading-relaxed text-slate-400">{attr.note}</div>

      <table className="w-full text-[11px]">
        <thead className="text-[10px] text-slate-400">
          <tr className="border-b border-slate-100">
            <th className="py-1 pr-2 text-left font-bold">风格</th>
            <th className="py-1 pr-2 text-right font-bold">β</th>
            <th className="py-1 text-right font-bold">t(β)</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {(attr.betas ?? []).map((b) => (
            <tr key={b.style} className="border-b border-slate-50">
              <td className="py-1 pr-2 font-sans text-slate-600">{labels[b.style] || b.style}</td>
              <td className="py-1 pr-2 text-right font-bold" style={{ color: valColor(b.beta) }}>{fmtNum(b.beta, 4)}</td>
              <td className={`py-1 text-right ${isNum(b.t) && Math.abs(b.t) > 2 ? 'font-bold text-rose-600' : 'text-slate-500'}`}>
                {fmtNum(b.t, 2)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
);

/** 口径声明条：自算 CNE5 与商业 Barra 不可比，这条必须常驻 */
const StyleNotice: React.FC = () => (
  <div className="rounded-2xl border border-indigo-100 bg-indigo-50/50 px-3 py-2 text-[10px] leading-relaxed text-slate-600">
    <span className="font-bold text-indigo-700">口径声明：</span>{CNE5_NOTE}
  </div>
);

// ─────────────────────────── 主组件 ───────────────────────────

interface Props {
  blocks: FactorBlocks | null;
  definitions: Record<string, string>;
}

export const StyleTab: React.FC<Props> = ({ blocks, definitions }) => {
  const def = (k: string, fallback: string) => definitions?.[k] || fallback;

  if (!blocks) {
    return (
      <div className="flex flex-1 min-h-0 items-center justify-center rounded-2xl border border-dashed border-slate-200 bg-white/60">
        <span className="text-xs text-slate-400">机构级派生块加载中…（风格块随 /detail 一起返回）</span>
      </div>
    );
  }

  const st = blocks.style_block;
  if (!st || st.available === false) {
    return (
      <div className="flex flex-col gap-3 min-h-0">
        <StyleNotice />
        <div className="min-h-[200px] rounded-2xl border border-slate-200/80 bg-white p-3 shadow-sm">
          <Degraded reason={st?.reason || NO_PRODUCT} />
        </div>
      </div>
    );
  }

  const exposures = st.exposures ?? [];
  const dates = st.dates ?? [];
  const labels: Record<string, string> = {};
  exposures.forEach((r) => { labels[r.style] = r.label; });
  const thinCount = exposures.filter((r) => r.n_days < MIN_TRUST_DAYS).length;

  // 条形图：升序排列 —— ECharts 类目轴自下而上渲染，最大的一条自然落在顶部
  const barRows = [...exposures].sort((a, b) => (a.mean_corr ?? -9) - (b.mean_corr ?? -9));
  const barOption: any = {
    grid: grid({ left: 76, right: 48, top: 6, bottom: 20 }),
    tooltip: tooltip({ formatter: (ps: any[]) => `${ps[0]?.name}<br/>均值相关 ${fmtNum(ps[0]?.value, 3)}` }),
    xAxis: valAxis((v: number) => v.toFixed(1), { min: -1, max: 1 }),
    yAxis: catAxis(barRows.map((r) => r.label), 0),
    series: [{
      type: 'bar',
      barMaxWidth: 14,
      data: barRows.map((r) => ({
        value: r.mean_corr,
        itemStyle: {
          color: valColor(r.mean_corr),
          borderRadius: r.mean_corr != null && r.mean_corr < 0 ? [3, 0, 0, 3] : [0, 3, 3, 0],
        },
        // 负数条的数值标在左侧，否则会压住零轴
        label: { position: r.mean_corr != null && r.mean_corr < 0 ? 'left' : 'right' },
      })),
      label: { show: true, fontSize: 9, color: '#64748b', formatter: (p: any) => (isNum(p.value) ? fmtNum(p.value, 2) : DASH) },
      markLine: { ...zeroLine, symbol: 'none', data: [{ xAxis: 0 }] },
    }],
  };

  // 雷达：只画有值的风格，缺值**不补 0** —— 补 0 会把「没数据」画成「不相关」
  const radarRows = exposures.filter((r): r is StyleExposureRow & { mean_corr: number } => isNum(r.mean_corr));
  const radarOption: any = {
    tooltip: tooltip({ trigger: 'item' }),
    radar: {
      indicator: radarRows.map((r) => ({ name: r.label, min: -1, max: 1 })),
      radius: '62%',
      center: ['50%', '56%'],
      axisName: { fontSize: 9, color: '#64748b' },
      axisLine: { lineStyle: { color: GRID } },
      splitLine: { lineStyle: { color: GRID } },
      splitArea: { show: false },
    },
    series: [{
      type: 'radar',
      symbolSize: 3,
      data: [{
        name: '均值相关',
        value: radarRows.map((r) => r.mean_corr),
        lineStyle: { color: ACCENT, width: 1.6 },
        itemStyle: { color: ACCENT },
        areaStyle: { color: 'rgba(99,102,241,0.18)' },
      }],
    }],
  };

  // 时序：仅画真正有值的系列（全 null 的系列画出来只是一条贴 0 的假线）
  const tsSeries: any[] = exposures
    .map((r, i) => ({ r, i }))
    .filter(({ r }) => hasData(st.exposure_ts?.[r.style]))
    .map(({ r, i }, k) => ({
      name: r.label,
      type: 'line',
      showSymbol: false,
      data: (st.exposure_ts?.[r.style] ?? []).map((v) => (isNum(v) ? +v.toFixed(3) : null)),
      lineStyle: { width: 1.2, color: styleHue(i) },
      itemStyle: { color: styleHue(i) },
      // 零线只画一次，免得十条线叠出十条参考线
      ...(k === 0 ? { markLine: { ...zeroLine, symbol: 'none', data: [{ yAxis: 0 }] } } : {}),
    }));
  const tsOption: any = {
    grid: grid({ left: 40, right: 14, top: 8, bottom: 46 }),
    tooltip: tooltip(),
    legend: {
      type: 'scroll', bottom: 0, itemWidth: 10, itemHeight: 6,
      textStyle: { fontSize: 9, color: NEUTRAL }, data: tsSeries.map((s) => s.name),
    },
    xAxis: catAxis(dates.map(fmtDate)),
    yAxis: valAxis((v: number) => v.toFixed(1), { min: -1, max: 1 }),
    series: tsSeries,
  };

  const attrHint = st.n_attribution_days != null ? `n=${st.n_attribution_days} 天` : undefined;

  return (
    <div className="flex flex-col gap-3 min-h-0">
      <StyleNotice />

      <div className="grid grid-cols-2 gap-3">
        <ChartShell title="Barra 十大风格相关性" hint={`${exposures.length} 项${thinCount ? ` · ${thinCount} 项天数不足` : ''}`}
          info={def('style_corr', '因子值与自算 Barra CNE5 式十大风格暴露的横截面秩相关（逐日均值）。')}>
          {exposures.length
            ? <div className="h-[240px] min-h-0 min-w-0"><ExposureTable rows={exposures} /></div>
            : <Degraded reason="该快照没有风格暴露行（exposures 为空），不显示空表。" />}
        </ChartShell>

        <ChartShell title="相关性排序" hint="均值相关 · −1 … 1"
          info="按均值相关排序的横向条形：正相关（红）说明因子在赌该风格，负相关（绿）说明与它反向；越靠两端风格暴露越强。">
          {barRows.length
            ? <EChartsChart option={barOption} style={{ height: H }} />
            : <Degraded reason="无可画的风格相关性（exposures 为空）。" />}
        </ChartShell>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <ChartShell title="风格暴露雷达" hint="均值相关 · 半径 −1 … 1"
          info="把十项均值相关摊成一张雷达：形状给出「这个因子整体偏向哪类风格」的一眼判断。仅绘制有值的风格，缺值不补 0。">
          {radarRows.length >= 3
            ? <EChartsChart option={radarOption} style={{ height: H }} />
            : <Degraded reason={`可用于雷达的风格不足 3 项（当前 ${radarRows.length} 项有值），画出来不成形，故不画。`} />}
        </ChartShell>

        <ChartShell title="风格相关时序" hint={`${tsSeries.length} 条 · 与日期等长`}
          info="逐日横截面秩相关的走势：一条稳定的水平线说明风格暴露是常态，尖峰说明只在个别行情里押注该风格。图例可滚动。">
          {tsSeries.length && dates.length
            ? <EChartsChart option={tsOption} style={{ height: H }} />
            : <Degraded reason="风格暴露时序为空（exposure_ts 全为 null 或缺 dates），不画空折线。" />}
        </ChartShell>
      </div>

      <ChartShell title="超额风格相关性汇总"
        info="多头腿相对基准的超额收益与各风格纯因子收益的**单变量**时序相关。与下方「超额收益的风格归因」配对读：单变量高而回归 β 不显著，说明这个相关性是被别的风格带出来的（size 与 nlsize 天然共线）。无数据时给出具体原因，不代表相关为 0。">
        <div className="h-[150px] min-h-0 min-w-0">
          <ExcessCorrTable rows={st.excess_corr} reason={st.excess_corr_reason} />
        </div>
      </ChartShell>

      <div className="grid grid-cols-2 gap-3">
        <ChartShell title="风格归因回归" hint={attrHint}
          info={def('style_attribution', '把日收益对十大风格纯因子收益做时序回归 y = α + Σβ·f + ε：α 是风格之外的部分，t(α) 看它是否显著非零。')}>
          <div className="h-[320px] min-h-0 min-w-0">
            <AttributionBody target={LS_TARGET} attr={st.attribution ?? null}
              reason={st.attribution_reason || '风格归因未构建：风格纯因子收益产物缺失，或与报告日期的交集不足以回归。'}
              labels={labels} />
          </div>
        </ChartShell>

        <ChartShell title="超额收益的风格归因" hint={attrHint}
          info="同一套回归换成超额收益作被解释变量：α 表示基准之外的、与风格无关的那部分超额。">
          <div className="h-[320px] min-h-0 min-w-0">
            <AttributionBody target={EXCESS_TARGET} attr={st.excess_attribution ?? null}
              reason={EXCESS_ATTR_REASON} labels={labels} />
          </div>
        </ChartShell>
      </div>
    </div>
  );
};
