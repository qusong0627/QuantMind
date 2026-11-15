/** 净值曲线（行情回测内嵌版）：lightweight-charts 单线，轻量、无跨模块依赖。
 *
 * 只服务回测结果里的 `equity: {date, value}[]`：
 *  - 黑色主线 + 浅灰网格，与行情回测的终端风一致；
 *  - 容器宽度由 ResizeObserver 跟随，卸载销毁实例；
 *  - 数据点 < 2 时如实显示「无净值数据」，不画假线。
 */
import { useEffect, useRef } from 'react';
import {
  ColorType,
  createChart,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts';

/** 日线 'YYYY-MM-DD' → 日期轴；分钟线完整时间按市场墙钟 → UTC 秒（显示时加偏移） */
const toTime = (d: string, tzOffsetHours = 8): Time => {
  const s = String(d || '');
  if (s.length > 10) {
    const sign = tzOffsetHours >= 0 ? '+' : '-';
    const off = `${sign}${String(Math.abs(tzOffsetHours)).padStart(2, '0')}:00`;
    const ms = Date.parse(s.replace(' ', 'T') + off);
    return Math.floor((Number.isFinite(ms) ? ms : 0) / 1000) as UTCTimestamp;
  }
  return s as unknown as Time;
};

export default function EquityLineChart({
  points,
  height = 200,
  color = '#1a1a1a',
  tzOffsetHours = 8,
}: {
  points: { date: string; value: number }[];
  height?: number;
  color?: string;
  tzOffsetHours?: number;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const lineRef = useRef<ISeriesApi<'Line'> | null>(null);
  const hasData = points.length >= 2;
  const intraday = points.some((p) => String(p.date).length > 10);

  // 建图（只一次，height 变化重建）。
  // 依赖里必须带 hasData：首次渲染若不足 2 点会走空态分支（容器不挂载），
  // 数据到位后容器才出现 —— 不看 hasData 的话 effect 不会重跑，图永远是空的。
  useEffect(() => {
    if (!hasData) return;
    const el = boxRef.current;
    if (!el) return;
    const chart = createChart(el, {
      height,
      layout: {
        background: { type: ColorType.Solid, color: '#ffffff' },
        textColor: '#1a1a1a',
        fontFamily: "'Courier New', monospace",
        fontSize: 10,
      },
      grid: {
        vertLines: { color: '#f0f0f0' },
        horzLines: { color: '#f0f0f0' },
      },
      rightPriceScale: { borderColor: '#e0e0e0' },
      timeScale: {
        borderColor: '#e0e0e0', rightOffset: 2, minBarSpacing: 0.5,
        timeVisible: intraday, secondsVisible: false,
      },
      localization: {
        locale: 'zh-CN',
        timeFormatter: (t: Time) => {
          if (!intraday) return '';
          const sec = typeof t === 'number' ? t : 0;
          const d = new Date((sec + tzOffsetHours * 3600) * 1000);
          const p = (n: number) => String(n).padStart(2, '0');
          return `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} `
            + `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
        },
      },
    });
    chartRef.current = chart;

    lineRef.current = chart.addSeries(LineSeries, {
      color,
      lineWidth: 2,
      priceLineVisible: false,
    });

    const ro = new ResizeObserver(() => {
      chart.applyOptions({ width: el.clientWidth });
    });
    ro.observe(el);
    chart.applyOptions({ width: el.clientWidth });

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      lineRef.current = null;
    };
  }, [height, color, hasData, intraday, tzOffsetHours]);

  // 灌数据
  useEffect(() => {
    const chart = chartRef.current;
    const line = lineRef.current;
    if (!chart || !line) return;
    line.setData(
      points
        .filter((p) => Number.isFinite(p.value))
        .map((p) => ({ time: toTime(p.date, tzOffsetHours), value: p.value })),
    );
    chart.timeScale().fitContent();
  }, [points, tzOffsetHours]);

  if (!hasData) {
    return <div className="lab-empty">无净值数据</div>;
  }

  return <div ref={boxRef} className="lab-equity-box" style={{ height }} />;
}
