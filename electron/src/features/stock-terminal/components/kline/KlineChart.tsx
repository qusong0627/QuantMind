/** 个股终端 K 线图：主图（蜡烛+MA/BOLL+指数叠加+推理分数右侧轴+交易/参考线）+ 副图（VOL/MACD/KDJ/RSI） */

import { useEffect, useMemo, useRef, useState } from 'react';
import ReactECharts from 'echarts-for-react';
import { KlineBar } from '../../types';
import { boll, kdj, macd, rsi, sma, volMa, Series } from '../../engine/indicators';

export type SubplotType = 'vol' | 'macd' | 'kdj' | 'rsi';

export interface IndicatorConfig {
  ma: boolean;
  boll: boolean;
  subplots: SubplotType[];
}

export interface IndexOverlay {
  code: string;
  name: string;
  closes: { date: string; close: number }[];
  color: string;
}

export interface SignalPoint {
  date: string;
  fusion: number | null;
  side: string;
}

/** 推理分数历史叠加（多模型）：每模型一条分数线 */
export interface ScoreSeries {
  model: string;
  color: string;
  points: { date: string; fusion: number | null; side: string | null }[];
}

/** 策略提醒点：标记在分数副图上 */
export interface AlertPoint {
  date: string;
  severity: 'danger' | 'warning' | 'positive' | 'info';
  message: string;
  score?: number | null;
}

/** 模拟交易点：buy/sell */
export interface TradeMarker {
  date: string;
  side: 'buy' | 'sell';
  price: number;
  shares: number;
}

/** 参考线：分数轴虚线 */
export interface RefLine {
  id: string;
  value: number;
  label: string;
  color: string;
  visible?: boolean;
}

const COLORS = {
  up: '#e11d48',        // A股：涨红
  down: '#059669',      // 跌绿
  ma5: '#f59e0b',
  ma10: '#3b82f6',
  ma20: '#8b5cf6',
  ma60: '#64748b',
  boll: '#94a3b8',
  volUp: '#fda4af',
  volDown: '#6ee7b7',
  dif: '#3b82f6',
  dea: '#f59e0b',
  histUp: '#e11d48',
  histDown: '#059669',
  k: '#3b82f6',
  d: '#f59e0b',
  j: '#8b5cf6',
  rsi: '#6366f1',
};

const SEVERITY_COLOR: Record<AlertPoint['severity'], string> = {
  danger: '#e11d48',
  warning: '#f59e0b',
  positive: '#10b981',
  info: '#6366f1',
};

const AXIS_LABEL = { fontSize: 10, color: '#64748b' };
const AXIS_LINE = { lineStyle: { color: '#e2e8f0' } };
const SPLIT_LINE = { lineStyle: { color: '#f1f5f9' } };
const SUB_HEIGHT = 84;  // 每个副图高度 px（VOL/MACD 等）
/** 周起点（周一为起点）。周/月周期下把分数对齐到所属周 */
function weekKey(date: string): string {
  const d = new Date(date + 'T00:00:00');
  const day = (d.getDay() + 6) % 7;
  d.setDate(d.getDate() - day);
  return d.toISOString().slice(0, 10);
}
/** 默认黄金线（策略 v2.0 主板黄金买入区间 0.10-0.12 的下沿） */
const DEFAULT_REF_LINE: RefLine = { id: 'default-golden', value: 0.10, label: '黄金线', color: '#10b981' };

interface Props {
  bars: KlineBar[];
  config: IndicatorConfig;
  overlays: IndexOverlay[];
  height?: number;
  period?: 'daily' | 'weekly' | 'monthly'; // 当前K线周期，供分数副图对齐周/月
  signals?: SignalPoint[];
  btEquity?: { date: string; equity: number }[];
  scoreSeries?: ScoreSeries[];
  scorePoints?: { date: string; value: number }[]; // 推理分数副图（主图下方独立副图）
  showScoreSubplot?: boolean;
  alerts?: AlertPoint[];
  trades?: TradeMarker[];
  refLines?: RefLine[];
  /** 初始缩放窗口（%）：默认 0-100 全显；个股终端首屏聚焦最近 200 根 */
  zoomStart?: number;
  zoomEnd?: number;
  onBarClick?: (bar: KlineBar) => void;
}

export function KlineChart({
  bars, config, overlays, height = 460, period = 'daily',
  signals = [], btEquity = [], scoreSeries = [], scorePoints, showScoreSubplot = false, alerts = [], trades = [], refLines = [],
  zoomStart = 0, zoomEnd = 100, onBarClick,
}: Props) {
  // 自适应容器高度：图表铺满父容器（个股终端 K 线卡内部空间），不再写死 320 留下大片空白；
  // 未测量到时回退 height 属性（其它定高调用方）
  const wrapRef = useRef<HTMLDivElement>(null);
  const [boxH, setBoxH] = useState(0);
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setBoxH(el.clientHeight));
    ro.observe(el);
    setBoxH(el.clientHeight);
    return () => ro.disconnect();
  }, []);
  const chartH = boxH > 120 ? boxH : height;

  const option = useMemo(() => {
    const dates = bars.map(b => b.date);
    const closes = bars.map(b => b.close);
    const volumes = bars.map(b => b.volume ?? 0);
    const idxByDate = new Map(dates.map((d, i) => [d, i]));
    const ma5 = config.ma ? sma(closes, 5) : null;
    const ma10 = config.ma ? sma(closes, 10) : null;
    const ma20 = config.ma ? sma(closes, 20) : null;
    const ma60 = config.ma ? sma(closes, 60) : null;
    const bb = config.boll ? boll(closes) : null;
    const macdRes = config.subplots.includes('macd') ? macd(closes) : null;
    const kdjRes = config.subplots.includes('kdj') ? kdj(bars) : null;
    const rsiRes = config.subplots.includes('rsi') ? rsi(closes) : null;
    const volMa5 = config.subplots.includes('vol') ? volMa(bars, 5) : null;
    const volMa10 = config.subplots.includes('vol') ? volMa(bars, 10) : null;

    // 指数叠加：以各自首日为基准归一化为百分比
    const overlaySeries = overlays.map(ov => {
      const byDate = new Map(ov.closes.map(c => [c.date, c.close]));
      const base = ov.closes.length ? ov.closes[0].close : 1;
      const aligned = bars.map(b => {
        const c = byDate.get(b.date);
        return c != null && base > 0 ? Number((((c - base) / base) * 100).toFixed(2)) : null;
      });
      return { name: ov.name, data: aligned, color: ov.color };
    });

    // ── grid 布局：主图（蜡烛+MA+指数+模型分数右轴）+ 副图依次下排 ──
    // axes 下标与 grid 下标独立：用 gridAxes 记录每个 grid 的 x/y 轴在 xAxis/yAxis 数组中的下标
    const GRID_L = 64, GRID_R = scoreSeries.length ? 46 : 16;  // 右侧留出分数轴刻度
    const GAP = 28;                    // 主图与第一个副图间距
    const SUB_GAP = 24;                // 副图之间间距
    const TOP = 24;                    // 顶部留出图例行
    const hasScoreSubplot = !!(showScoreSubplot && scorePoints?.length);
    // 分数副图高度按整图高度动态取 ≈38%（K线主图区约占 60%）：默认就给底部分数图大框
    const scoreSubH = hasScoreSubplot ? Math.max(120, Math.round((chartH - TOP - GAP - 26) * 0.38)) : 0;
    const subCount = config.subplots.length + (hasScoreSubplot ? 1 : 0);
    const subTotal = subCount > 0
      ? (config.subplots.length * SUB_HEIGHT
          + (hasScoreSubplot ? scoreSubH : 0)
          + (subCount - 1) * SUB_GAP)
      : 0;
    const mainH = Math.max(140, chartH - TOP - GAP - subTotal - 26);
    const grids: any[] = [];
    const xAxes: any[] = [];
    const yAxes: any[] = [];
    const series: any[] = [];
    const gridAxes: { x: number; y: number }[] = []; // gridIdx -> axes idx

    // 主图
    grids.push({ left: GRID_L, right: GRID_R, top: TOP, height: mainH });
    xAxes.push({ type: 'category', gridIndex: 0, data: dates, boundaryGap: true, axisLine: AXIS_LINE, axisTick: { show: false }, axisLabel: { show: false } });
    yAxes.push({ type: 'value', gridIndex: 0, scale: true, axisLabel: { ...AXIS_LABEL, formatter: (v: number) => Number(v).toFixed(3) }, axisLine: AXIS_LINE, splitLine: SPLIT_LINE });
    gridAxes[0] = { x: 0, y: 0 };
    // 指数归一化百分比轴（主图左侧内沿，仅当叠加指数时不遮挡分数轴）
    if (overlaySeries.length) {
      yAxes.push({
        type: 'value', gridIndex: 0, scale: true,
        axisLabel: { show: false }, axisLine: { show: false }, splitLine: { show: false },
        min: (v: any) => -Math.max(30, Math.ceil(Math.abs(v.min) / 10) * 10),
        max: (v: any) => Math.max(30, Math.ceil(Math.abs(v.max) / 10) * 10),
      });
    }
    // 模型分数轴（主图右外侧）：推理分数与参考线共用此轴
    let scoreYI = -1;
    if (scoreSeries.length) {
      scoreYI = yAxes.length;
      const allScores = scoreSeries.flatMap(sr => sr.points.map(p => p.fusion).filter((f): f is number => f != null));
      const lo = allScores.length ? Math.min(...allScores) : -1;
      const hi = allScores.length ? Math.max(...allScores) : 1;
      // 分数轴按当前模型分数跨度自适应：原 min pad 0.05 对新模型（0.001 量级）过大，
      // 把轴撑到 ±0.06 刻度显得很大——改为纯比例 padding；
      // 单点/全等分数（span=0）时按分数绝对值比例兜底（0.002 下限），不再用 0.05
      const span = hi - lo;
      const pad = span > 1e-9 ? span * 0.15 : Math.max(0.002, Math.abs(hi) * 0.3);
      // 刻度小数位随量级收紧：跨度过小时 toFixed(2) 会全部显示 0.00
      const digits = span < 0.01 ? 4 : span < 0.1 ? 3 : 2;
      yAxes.push({
        type: 'value', gridIndex: 0, position: 'right', scale: false,
        min: lo - pad, max: hi + pad,
        axisLabel: { ...AXIS_LABEL, formatter: (v: number) => v.toFixed(digits) },
        axisLine: { lineStyle: { color: '#6366f1' } },
        splitLine: { show: false },
        name: '分数', nameTextStyle: { fontSize: 9, color: '#6366f1' },
      });
    }

    // 蜡烛
    series.push({
      name: 'K线', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0,
      data: bars.map(b => [b.open, b.close, b.low, b.high]),
      itemStyle: { color: COLORS.up, color0: COLORS.down, borderColor: COLORS.up, borderColor0: COLORS.down },
    });

    const line = (name: string, data: Series, color: string, yAxisIdx = 0, width = 1.2) =>
      series.push({
        name, type: 'line', xAxisIndex: 0, yAxisIndex: yAxisIdx, data,
        symbol: 'none', lineStyle: { width, color }, itemStyle: { color }, emphasis: { disabled: true }, z: 3,
      });

    if (ma5) line('MA5', ma5, COLORS.ma5);
    if (ma10) line('MA10', ma10, COLORS.ma10);
    if (ma20) line('MA20', ma20, COLORS.ma20);
    if (ma60) line('MA60', ma60, COLORS.ma60);
    if (bb) {
      line('BOLL中轨', bb.mid, COLORS.boll);
      line('BOLL上轨', bb.upper, COLORS.boll);
      line('BOLL下轨', bb.lower, COLORS.boll);
    }
    overlaySeries.forEach((ov, i) => line(ov.name, ov.data, ov.color, 1, 1.2));

    // 策略净值叠加
    if (btEquity.length) {
      const eqByDate = new Map(btEquity.map(p => [p.date, p.equity]));
      const firstEq = btEquity.length ? btEquity[0].equity : 1;
      const baseClose = bars.length ? bars[0].close : 1;
      series.push({
        name: '策略净值', type: 'line', xAxisIndex: 0, yAxisIndex: 0,
        data: bars.map(b => {
          const eq = eqByDate.get(b.date);
          if (eq == null || firstEq <= 0) return null;
          return Number((baseClose * (eq / firstEq)).toFixed(2));
        }),
        symbol: 'none', lineStyle: { width: 1.6, color: '#f97316', type: 'dashed' }, itemStyle: { color: '#f97316' }, z: 4, emphasis: { disabled: true },
      });
    }

    // 推理信号标记
    if (signals.length) {
      const buyData: any[] = [], sellData: any[] = [];
      for (const sig of signals) {
        const i = idxByDate.get(sig.date);
        if (i == null) continue;
        const bar = bars[i];
        const v = sig.side === 'BUY' ? bar.low * 0.99 : bar.high * 1.01;
        if (sig.side === 'BUY') buyData.push({ value: [i, Number(v.toFixed(2))], sig });
        else if (sig.side === 'SELL') sellData.push({ value: [i, Number(v.toFixed(2))], sig });
      }
      const mk = (data: any[], symbol: string, color: string, offset: number) => ({
        name: '信号', type: 'scatter', xAxisIndex: 0, yAxisIndex: 0,
        data, symbol, symbolSize: 11, symbolOffset: [0, offset],
        itemStyle: { color, borderColor: '#fff', borderWidth: 1 },
        label: { show: true, formatter: (p: any) => p.data.sig.side, fontSize: 8, color, fontWeight: 'bold', position: 'top' },
        z: 10,
      });
      if (buyData.length) series.push(mk(buyData, 'triangle', COLORS.up, -8));
      if (sellData.length) series.push(mk(sellData, 'triangle', COLORS.down, 8));
    }

    // 模拟交易标记
    if (trades.length) {
      const buyT: any[] = [], sellT: any[] = [];
      for (const t of trades) {
        const i = idxByDate.get(t.date);
        if (i == null) continue;
        const bar = bars[i];
        const v = t.side === 'buy' ? bar.low * 0.985 : bar.high * 1.015;
        if (t.side === 'buy') buyT.push({ value: [i, Number(v.toFixed(2))], t });
        else sellT.push({ value: [i, Number(v.toFixed(2))], t });
      }
      const tmk = (data: any[], symbol: string, color: string, offset: number) => ({
        name: '交易', type: 'scatter', xAxisIndex: 0, yAxisIndex: 0,
        data, symbol, symbolSize: 13, symbolOffset: [0, offset],
        itemStyle: { color, borderColor: '#fff', borderWidth: 1.5 },
        label: { show: true, formatter: (p: any) => p.data.t.shares, fontSize: 8, color, fontWeight: 'bold', position: 'bottom' },
        z: 11,
      });
      if (buyT.length) series.push(tmk(buyT, 'triangle', COLORS.up, -12));
      if (sellT.length) series.push(tmk(sellT, 'triangle', COLORS.down, 12));
    }

    // ── 副图：依次下排（grids 1..N）──
    let subTop = TOP + mainH + GAP;
    config.subplots.forEach((sp, idx) => {
      const gi = idx + 1;
      const xi = xAxes.length;
      const yi = yAxes.length;
      grids.push({ left: GRID_L, right: GRID_R, top: subTop, height: SUB_HEIGHT });
      const showLabel = idx === config.subplots.length - 1;
      xAxes.push({
        type: 'category', gridIndex: gi, data: dates, boundaryGap: true,
        axisLine: AXIS_LINE, axisTick: { show: false },
        axisLabel: showLabel ? { ...AXIS_LABEL, color: '#94a3b8' } : { show: false },
      });
      yAxes.push({ type: 'value', gridIndex: gi, scale: true, axisLabel: AXIS_LABEL, axisLine: AXIS_LINE, splitLine: SPLIT_LINE });
      gridAxes[gi] = { x: xi, y: yi };

      if (sp === 'vol') {
        series.push({
          name: '成交量', type: 'bar', xAxisIndex: xi, yAxisIndex: yi,
          data: volumes.map((v, i) => ({ value: v, itemStyle: { color: bars[i].close >= bars[i].open ? COLORS.volUp : COLORS.volDown } })),
        });
        series.push({ name: 'VMA5', type: 'line', xAxisIndex: xi, yAxisIndex: yi, data: volMa5, symbol: 'none', lineStyle: { width: 1, color: COLORS.ma5 }, z: 3 });
        series.push({ name: 'VMA10', type: 'line', xAxisIndex: xi, yAxisIndex: yi, data: volMa10, symbol: 'none', lineStyle: { width: 1, color: COLORS.ma10 }, z: 3 });
      } else if (sp === 'macd' && macdRes) {
        series.push({
          name: 'MACD柱', type: 'bar', xAxisIndex: xi, yAxisIndex: yi,
          data: macdRes.hist.map(v => ({ value: v, itemStyle: { color: (v ?? 0) >= 0 ? COLORS.histUp : COLORS.histDown } })),
        });
        series.push({ name: 'DIF', type: 'line', xAxisIndex: xi, yAxisIndex: yi, data: macdRes.dif, symbol: 'none', lineStyle: { width: 1, color: COLORS.dif }, z: 3 });
        series.push({ name: 'DEA', type: 'line', xAxisIndex: xi, yAxisIndex: yi, data: macdRes.dea, symbol: 'none', lineStyle: { width: 1, color: COLORS.dea }, z: 3 });
      } else if (sp === 'kdj' && kdjRes) {
        series.push({ name: 'K', type: 'line', xAxisIndex: xi, yAxisIndex: yi, data: kdjRes.k, symbol: 'none', lineStyle: { width: 1, color: COLORS.k }, z: 3 });
        series.push({ name: 'D', type: 'line', xAxisIndex: xi, yAxisIndex: yi, data: kdjRes.d, symbol: 'none', lineStyle: { width: 1, color: COLORS.d }, z: 3 });
        series.push({ name: 'J', type: 'line', xAxisIndex: xi, yAxisIndex: yi, data: kdjRes.j, symbol: 'none', lineStyle: { width: 1, color: COLORS.j }, z: 3 });
      } else if (sp === 'rsi' && rsiRes) {
        series.push({ name: 'RSI14', type: 'line', xAxisIndex: xi, yAxisIndex: yi, data: rsiRes, symbol: 'none', lineStyle: { width: 1.2, color: COLORS.rsi }, z: 3 });
      }
      subTop += SUB_HEIGHT + SUB_GAP;
    });

    // ── 推理分数副图（主图下方独立副图，与主图同 x 轴对齐）──
    if (hasScoreSubplot && scorePoints?.length) {
      const gi = grids.length;
      const xi = xAxes.length;
      const yi = yAxes.length;
      grids.push({ left: GRID_L, right: GRID_R, top: subTop, height: scoreSubH });
      xAxes.push({
        type: 'category', gridIndex: gi, data: dates, boundaryGap: true,
        axisLine: AXIS_LINE, axisTick: { show: false },
        axisLabel: { ...AXIS_LABEL, color: '#94a3b8' },
      });
      const vals = scorePoints.map((p) => p.value);
      const lo = Math.min(...vals);
      const hi = Math.max(...vals);
      const span = hi - lo;
      const pad = span > 1e-9 ? span * 0.15 : Math.max(0.002, Math.abs(hi) * 0.3);
      // 刻度小数位随量级收紧：跨度过小时 toFixed(2) 会全部显示 0.00
      const digits = span < 0.01 ? 4 : span < 0.1 ? 3 : 2;
      yAxes.push({ type: 'value', gridIndex: gi, scale: true, axisLabel: { ...AXIS_LABEL, formatter: (v: number) => Number(v).toFixed(digits) }, axisLine: { lineStyle: { color: '#6366f1' } }, splitLine: SPLIT_LINE });
      gridAxes[gi] = { x: xi, y: yi };
      const scoreMap = new Map(scorePoints.map((p) => [p.date, p.value]));
      series.push({
        name: '推理分数', type: 'line', xAxisIndex: xi, yAxisIndex: yi,
        data: bars.map((b) => {
          // 日线精确到日；周/月取该周期内最后一条分数（weekKey 模块级，周一为周起点）
          if (period === 'daily') {
            const v = scoreMap.get(b.date);
            return v != null ? Number(v) : null;
          }
          let last: number | null = null;
          for (const p of scorePoints) {
            if (period === 'weekly') {
              if (weekKey(p.date) === weekKey(b.date)) last = p.value;
            } else if (p.date.slice(0, 7) === b.date.slice(0, 7)) {
              last = p.value;
            }
          }
          return last != null ? Number(last) : null;
        }),
        symbol: 'none', lineStyle: { width: 1.8, color: '#6366f1' }, itemStyle: { color: '#6366f1' }, areaStyle: { color: 'rgba(99,102,241,0.12)' }, z: 5, connectNulls: false,
      });
      // 0 轴参考线
      series[series.length - 1].markLine = {
        silent: true, symbol: 'none',
        data: [{ yAxis: 0, lineStyle: { color: '#94a3b8', type: 'dashed', width: 1 }, label: { formatter: '0', fontSize: 9, color: '#94a3b8' } }],
      } as any;
      subTop += scoreSubH + SUB_GAP;
    }

    // ── 推理分数：叠加到主图，共用主图 x 轴 + 右侧分数轴（scoreYI），与 K 线日期天然对齐 ──
    if (scoreSeries.length) {
      scoreSeries.forEach(sr => {
        const scoreMap = new Map(sr.points.map(p => [p.date, p.fusion]));
        series.push({
          name: `分数·${sr.model.slice(0, 10)}`, type: 'line', xAxisIndex: 0, yAxisIndex: scoreYI,
          data: bars.map(b => {
            // 日线精确到日；周/月取该周期内最后一条可用分数（周期落在 K 线日期上）
            if (period === 'daily') {
              const f = scoreMap.get(b.date);
              return f != null ? Number(f) : null;
            }
            let last: number | null = null;
            for (const p of sr.points) {
              if (p.fusion == null) continue;
              if (period === 'weekly') {
                if (weekKey(p.date) === weekKey(b.date)) last = p.fusion;
              } else if (p.date.slice(0, 7) === b.date.slice(0, 7)) {
                last = p.fusion;
              }
            }
            return last != null ? Number(last) : null;
          }),
          symbol: 'circle', symbolSize: 5, connectNulls: false,
          lineStyle: { width: 1.6, color: sr.color }, itemStyle: { color: sr.color, borderColor: '#fff', borderWidth: 1 },
          z: 6, emphasis: { scale: 1.3 },
        });
      });

      // 策略提醒标记（菱形，按 severity 着色，画在分数轴上）
      if (alerts.length) {
        const byDate = new Map(alerts.map(a => [a.date, a]));
        const alertData = bars
          .map((b, i) => {
            const a = byDate.get(b.date);
            if (!a) return null;
            const sr = scoreSeries[0];
            const f = a.score ?? sr?.points.find(p => p.date === b.date)?.fusion ?? null;
            return f == null ? null : { value: [i, Number(f)], a };
          })
          .filter(Boolean);
        if (alertData.length) {
          series.push({
            name: '策略提醒', type: 'scatter', xAxisIndex: 0, yAxisIndex: scoreYI,
            data: alertData, symbol: 'diamond', symbolSize: 12,
            itemStyle: {
              color: (p: any) => SEVERITY_COLOR[p.data.a.severity] ?? '#6366f1',
              borderColor: '#fff', borderWidth: 1,
            },
            label: { show: false },
            tooltip: {
              formatter: (p: any) => {
                const a = p.data.a;
                const sc = a.score != null ? ` · 分数 ${Number(a.score).toFixed(4)}` : '';
                return `<div><b>${dates[p.data.value[0]]}</b><br/><span style="color:${SEVERITY_COLOR[a.severity]};font-weight:bold">${a.message}</span>${sc}</div>`;
              },
            },
            z: 12,
          });
        }
      }

      // 参考线 + 默认黄金线 0.10：画在右侧分数轴上（主图 markLine）
      const visRef = refLines.filter(l => l.visible !== false);
      const hasGolden = visRef.some(l => Math.abs(l.value - DEFAULT_REF_LINE.value) < 1e-6);
      const allLines = hasGolden ? visRef : [DEFAULT_REF_LINE, ...visRef];
      const firstScore = series.find((s: any) => String(s.name).startsWith('分数·'));
      if (firstScore) {
        firstScore.markLine = {
          silent: true, symbol: 'none',
          data: allLines.map(l => ({
            yAxis: l.value,
            lineStyle: { color: l.color, type: 'dashed', width: 1.5 },
            label: { formatter: `${l.label} ${l.value >= 0 ? '+' : ''}${l.value.toFixed(2)}`, fontSize: 9, position: 'insideEndTop', color: l.color },
          })),
        };
      }
    }

    const legendData: string[] = [];
    if (ma5) legendData.push('MA5', 'MA10', 'MA20', 'MA60');
    scoreSeries.forEach(sr => legendData.push(`分数·${sr.model.slice(0, 10)}`));

    return {
      animation: false,
      backgroundColor: 'transparent',
      legend: legendData.length ? {
        show: true, top: 2, left: 68, itemWidth: 12, itemHeight: 8, itemGap: 8,
        textStyle: { fontSize: 9, color: '#64748b' },
        data: legendData,
      } : undefined,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', label: { backgroundColor: '#475569', fontSize: 10 } },
        backgroundColor: 'rgba(255,255,255,0.96)',
        borderColor: '#e2e8f0',
        textStyle: { color: '#334155', fontSize: 11 },
        formatter: (params: any) => {
          const list = Array.isArray(params) ? params : [params];
          if (!list.length) return '';
          const axisValue = list[0]?.axisValue ?? '';
          let html = `<div style="font-weight:600;margin-bottom:4px;">${axisValue}</div>`;
          for (const p of list) {
            const name = String(p.seriesName ?? '');
            const data = p.data;
            // 蜡烛：[open, close, low, high]；ECharts 6 category 轴下 tooltip 值为
            // [xIndex, open, close, low, high]（5 元素，首位是柱下标），必须剥离后再解构，
            // 否则下标会被当成开盘价显示（如 473.000）
            if (name === 'K线' && Array.isArray(data)) {
              const arr = (Array.isArray(data) ? data : (data as any)?.value) as number[];
              const src = Array.isArray(arr) && arr.length >= 4 ? arr : (Array.isArray((p as any).value) ? (p as any).value : []);
              const vals = Array.isArray(src) && src.length >= 5 ? src.slice(src.length - 4) : src;
              if (Array.isArray(vals) && vals.length >= 4) {
                const [open, close, low, high] = vals as number[];
                html += `<div>开盘: ${Number(open).toFixed(3)}&nbsp;&nbsp;收盘: ${Number(close).toFixed(3)}<br/>最低: ${Number(low).toFixed(3)}&nbsp;&nbsp;最高: ${Number(high).toFixed(3)}</div>`;
                continue;
              }
            }
            // MA / BOLL 等均线保留三位小数
            if (name.startsWith('MA') || name.startsWith('BOLL')) {
              const v = Array.isArray(data) ? (data as any)[1] ?? data : (p as any).value ?? data;
              const num = typeof v === 'number' ? v : Number(Array.isArray(v) ? v[1] : v);
              if (Number.isFinite(num)) {
                html += `<div>${name}: ${Number(num).toFixed(3)}</div>`;
                continue;
              }
            }
            // 推理分数：保留四位小数（与右侧分数轴刻度精度一致）
            if (name.startsWith('分数·') || name === '推理分数') {
              const v = Array.isArray(data) ? (data as any)[1] ?? data : (p as any).value ?? data;
              const num = typeof v === 'number' ? v : Number(Array.isArray(v) ? v[1] : v);
              if (Number.isFinite(num)) {
                html += `<div>${name}: <b>${Number(num).toFixed(4)}</b></div>`;
                continue;
              }
            }
            // 其他系列按默认展示，数值类保留三位
            const raw = (p as any).value ?? data;
            const numVal = Array.isArray(raw) ? raw[1] : raw;
            if (typeof numVal === 'number' && Number.isFinite(numVal)) {
              // 成交量等大数值不强制三位，保持原样但 MA 已单独处理
              if (name === '成交量' || name === 'VMA5' || name === 'VMA10') {
                html += `<div>${name}: ${numVal}</div>`;
              } else {
                html += `<div>${name}: ${Number(numVal).toFixed(3)}</div>`;
              }
            } else if (raw != null) {
              html += `<div>${name}: ${raw}</div>`;
            }
          }
          return html;
        },
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: grids,
      xAxis: xAxes,
      yAxis: yAxes,
      dataZoom: [
        { type: 'inside', xAxisIndex: xAxes.map((_, i) => i), start: zoomStart, end: zoomEnd },
        { type: 'slider', xAxisIndex: xAxes.map((_, i) => i), start: zoomStart, end: zoomEnd, bottom: 2, height: 16, borderColor: '#e2e8f0', fillerColor: 'rgba(59,130,246,0.08)' },
      ],
      series,
    };
  }, [bars, config, overlays, chartH, signals, btEquity, scoreSeries, scorePoints, showScoreSubplot, period, alerts, trades, refLines, zoomStart, zoomEnd]);

  const onEvents = onBarClick ? {
    click: (params: any) => {
      const raw = params?.data;
      const idx = typeof raw === 'object' && raw?.value ? raw.value[0] : params?.dataIndex;
      const i = Number.isInteger(idx) && idx >= 0 && idx < bars.length ? idx : -1;
      if (i < 0) return;
      onBarClick(bars[i]);
    },
  } : undefined;

  return (
    <div ref={wrapRef} className="w-full h-full min-h-0">
      <ReactECharts
        option={option}
        notMerge
        lazyUpdate
        style={{ width: '100%', height: chartH }}
        opts={{ renderer: 'canvas' }}
        onEvents={onEvents}
      />
    </div>
  );
}

export const OVERLAY_COLORS = ['#0ea5e9', '#f97316', '#a855f7', '#14b8a6'];
