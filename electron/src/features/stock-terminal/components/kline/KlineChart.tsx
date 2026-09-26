/** 个股终端 K 线图：主图（蜡烛 + MA + 点选日竖线）+ 副图（VOL/MACD/KDJ/RSI）+ 推理分数副图 */

import { useEffect, useMemo, useRef, useState } from 'react';
import ReactECharts from 'echarts-for-react';
import { KlineBar } from '../../types';
import { kdj, macd, rsi, sma, volMa, Series } from '../../engine/indicators';

export type SubplotType = 'vol' | 'macd' | 'kdj' | 'rsi';

export interface IndicatorConfig {
  ma: boolean;
  subplots: SubplotType[];
}

const COLORS = {
  up: '#e11d48',        // A股：涨红
  down: '#059669',      // 跌绿
  ma5: '#f59e0b',
  ma10: '#3b82f6',
  ma20: '#8b5cf6',
  ma60: '#64748b',
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

interface Props {
  bars: KlineBar[];
  config: IndicatorConfig;
  height?: number;
  period?: 'daily' | 'weekly' | 'monthly'; // 当前K线周期，供分数副图对齐周/月
  scorePoints?: { date: string; value: number }[]; // 推理分数副图（主图下方独立副图）
  showScoreSubplot?: boolean;
  /** 初始缩放窗口（%）：默认 0-100 全显；个股终端首屏聚焦最近 200 根 */
  zoomStart?: number;
  zoomEnd?: number;
  /** 点选日（YYYY-MM-DD）：命中 K 线窗口时在主图画一条竖虚线标出 */
  selectedDate?: string;
  onBarClick?: (bar: KlineBar) => void;
}

export function KlineChart({
  bars, config, height = 460, period = 'daily',
  scorePoints, showScoreSubplot = false, zoomStart = 0, zoomEnd = 100, selectedDate, onBarClick,
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
    const macdRes = config.subplots.includes('macd') ? macd(closes) : null;
    const kdjRes = config.subplots.includes('kdj') ? kdj(bars) : null;
    const rsiRes = config.subplots.includes('rsi') ? rsi(closes) : null;
    const volMa5 = config.subplots.includes('vol') ? volMa(bars, 5) : null;
    const volMa10 = config.subplots.includes('vol') ? volMa(bars, 10) : null;

    // ── grid 布局：主图（蜡烛+MA）+ 副图依次下排 ──
    // axes 下标与 grid 下标独立，各系列用显式 xAxisIndex/yAxisIndex 绑定所属 grid
    const GRID_L = 64, GRID_R = 16;
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
    const mainH = Math.max(140, chartH - TOP - GAP - subTotal - 52);
    const grids: any[] = [];
    const xAxes: any[] = [];
    const yAxes: any[] = [];
    const series: any[] = [];

    // 主图
    grids.push({ left: GRID_L, right: GRID_R, top: TOP, height: mainH });
    xAxes.push({ type: 'category', gridIndex: 0, data: dates, boundaryGap: true, axisLine: AXIS_LINE, axisTick: { show: false }, axisLabel: { show: false } });
    yAxes.push({ type: 'value', gridIndex: 0, scale: true, axisLabel: { ...AXIS_LABEL, formatter: (v: number) => Number(v).toFixed(3) }, axisLine: AXIS_LINE, splitLine: SPLIT_LINE });

    // 蜡烛
    series.push({
      name: 'K线', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0,
      data: bars.map(b => [b.open, b.close, b.low, b.high]),
      itemStyle: { color: COLORS.up, color0: COLORS.down, borderColor: COLORS.up, borderColor0: COLORS.down },
    });

    // 点选日竖线：只在命中已加载 K 线窗口时画（日期不在窗口内则仅右侧面板联动）
    if (selectedDate && idxByDate.has(selectedDate)) {
      (series[0] as any).markLine = {
        silent: true, symbol: 'none',
        data: [{
          xAxis: selectedDate,
          lineStyle: { color: '#2563eb', type: 'dashed', width: 1.2 },
          label: { formatter: '点选日', fontSize: 9, color: '#2563eb', position: 'insideEndTop' },
        }],
      };
    }

    const line = (name: string, data: Series, color: string, width = 1.2) =>
      series.push({
        name, type: 'line', xAxisIndex: 0, yAxisIndex: 0, data,
        symbol: 'none', lineStyle: { width, color }, itemStyle: { color }, emphasis: { disabled: true }, z: 3,
      });

    if (ma5) line('MA5', ma5, COLORS.ma5);
    if (ma10) line('MA10', ma10, COLORS.ma10);
    if (ma20) line('MA20', ma20, COLORS.ma20);
    if (ma60) line('MA60', ma60, COLORS.ma60);

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

    const legendData: string[] = [];
    if (ma5) legendData.push('MA5', 'MA10', 'MA20', 'MA60');

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
          // 日涨跌幅：当前柱收盘对上一柱收盘（日线即当日涨跌幅，周/月为周期涨跌幅），
          // A 股口径红涨绿跌；窗口首根没有上一柱，不显示。
          const barIdx = (list.find((p: any) => String(p.seriesName) === 'K线') ?? list[0])?.dataIndex;
          const curBar = typeof barIdx === 'number' ? bars[barIdx] : undefined;
          const prevBar = typeof barIdx === 'number' && barIdx > 0 ? bars[barIdx - 1] : undefined;
          if (curBar && prevBar && prevBar.close) {
            const pct = ((curBar.close - prevBar.close) / prevBar.close) * 100;
            const pctColor = pct >= 0 ? COLORS.up : COLORS.down;
            html += `<div style="margin-bottom:2px;">涨跌幅: <b style="color:${pctColor}">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</b></div>`;
          }
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
            // MA 等均线保留三位小数
            if (name.startsWith('MA')) {
              const v = Array.isArray(data) ? (data as any)[1] ?? data : (p as any).value ?? data;
              const num = typeof v === 'number' ? v : Number(Array.isArray(v) ? v[1] : v);
              if (Number.isFinite(num)) {
                html += `<div>${name}: ${Number(num).toFixed(3)}</div>`;
                continue;
              }
            }
            // 推理分数：保留四位小数（与分数副图轴刻度精度一致）
            if (name === '推理分数') {
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
        { type: 'slider', xAxisIndex: xAxes.map((_, i) => i), start: zoomStart, end: zoomEnd, bottom: 22, height: 20, borderColor: '#e2e8f0', fillerColor: 'rgba(59,130,246,0.08)' },
      ],
      series,
    };
  }, [bars, config, chartH, scorePoints, showScoreSubplot, period, zoomStart, zoomEnd, selectedDate]);

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
