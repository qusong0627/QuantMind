/** K线图：lightweight-charts（TradingView 官方开源图表库）封装。
 *
 *  - 蜡烛图 + 成交量副图；A股红涨绿跌口径
 *  - 回测成交点用 markers 标注（买↑ / 卖↓）
 *  - 策略算过的指标：叠加线（EMA/布林带…）画在主图，振荡指标（MACD/RSI…）各自开副图
 *  - 容器尺寸变化自适应（ResizeObserver），卸载时销毁实例
 */
import { useEffect, useRef } from 'react';
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts';
import type { BtTrade, Freq, IndicatorSeries, IndicatorSet, Kline } from './api';
import {
  DOWN_COLOR as DEFAULT_DOWN,
  paneGroups,
  seriesColor,
  toPoints,
  UP_COLOR as DEFAULT_UP,
} from './indicators';

/** 'YYYY-MM-DD'（日线）或 'YYYY-MM-DD HH:MM:SS'（分钟线）→ lightweight-charts 的 Time。
 *  分钟线按**市场墙钟**解析成 epoch 秒（北京 +08:00 / 加密 UTC），显示时再加回偏移。 */
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

/** #rrggbb + 两位 alpha（非 6 位 hex 原样返回，避免拼出非法颜色） */
const withAlpha = (hex: string, alpha: string) =>
  /^#[0-9a-f]{6}$/i.test(hex) ? `${hex}${alpha}` : hex;

export default function KLineChart({
  bars,
  trades = [],
  height = 420,
  showMarkers = true,
  indicators,
  showIndicators = true,
  upColor = DEFAULT_UP,
  downColor = DEFAULT_DOWN,
  freq = '1d',
  tzOffsetHours = 8,
}: {
  bars: Kline[];
  trades?: BtTrade[];
  height?: number;
  showMarkers?: boolean;
  /** 策略回测时真正算过的指标（可能来自老报告，缺省即无） */
  indicators?: IndicatorSet;
  showIndicators?: boolean;
  /** 涨跌配色：美股/加密绿涨红跌，A股/港股/期货红涨绿跌（由页面按市场传入） */
  upColor?: string;
  downColor?: string;
  /** 周期：日线用日期轴；分钟线显示时/分 */
  freq?: Freq;
  /** 分钟线时间轴的墙钟偏移（A股 +8 / 加密 0） */
  tzOffsetHours?: number;
}) {
  const intraday = freq !== '1d';
  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const marksRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  // 建图（只一次）
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const chart = createChart(el, {
      height,
      layout: {
        background: { type: ColorType.Solid, color: '#ffffff' },
        textColor: '#1a1a1a',
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#f0f0f0' },
        horzLines: { color: '#f0f0f0' },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#000000' },
      timeScale: {
        borderColor: '#000000',
        rightOffset: 4,
        barSpacing: 6,
        timeVisible: intraday,
        secondsVisible: false,
        // 默认最小 0.5px/根：2595 根全历史在 ~1080px 宽里塞不下（只能看到后 2100 根），
        // 放宽到 0.2 让「全历史」真的全览（缩略图观感，放大后正常）
        minBarSpacing: 0.2,
        ...(intraday ? {
          // 刻度按市场墙钟显示（默认按 UTC，会差 8 小时）；
          // 跨日时第一次出现的刻度用日期，其余用时分（否则每个交易日都显示 09:35）
          tickMarkFormatter: (() => {
            let lastDay = '';
            return (t: Time) => {
              const sec = typeof t === 'number' ? t : 0;
              const d = new Date((sec + tzOffsetHours * 3600) * 1000);
              const p = (n: number) => String(n).padStart(2, '0');
              const day = `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
              if (day !== lastDay) {
                lastDay = day;
                return day;
              }
              return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
            };
          })(),
        } : {}),
      },
      localization: {
        locale: 'zh-CN',
        priceFormatter: (p: number) => p.toFixed(2),
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

    candleRef.current = chart.addSeries(CandlestickSeries, {
      upColor,
      downColor,
      borderUpColor: upColor,
      borderDownColor: downColor,
      wickUpColor: upColor,
      wickDownColor: downColor,
      priceLineVisible: false,
    });
    volRef.current = chart.addSeries(HistogramSeries, {
      priceScaleId: 'vol',
      priceFormat: { type: 'volume' },
      priceLineVisible: false,
      lastValueVisible: false,
    });
    chart.priceScale('vol').applyOptions({
      scaleMargins: { top: 0.78, bottom: 0 },
      borderColor: '#e0e0e0',
    });
    // v5：markers 走插件（v4 的 series.setMarkers 已移除）
    marksRef.current = createSeriesMarkers(candleRef.current, []);

    const ro = new ResizeObserver(() => {
      chart.applyOptions({ width: el.clientWidth });
    });
    ro.observe(el);
    chart.applyOptions({ width: el.clientWidth });

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volRef.current = null;
      marksRef.current = null;
    };
  }, [height, upColor, downColor, intraday, tzOffsetHours]);

  // 灌数据
  useEffect(() => {
    const chart = chartRef.current;
    const candle = candleRef.current;
    const vol = volRef.current;
    if (!chart || !candle || !vol) return;

    candle.setData(
      bars.map((b) => ({
        time: toTime(b.date, tzOffsetHours),
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      })),
    );
    vol.setData(
      bars.map((b) => ({
        time: toTime(b.date, tzOffsetHours),
        value: b.volume,
        color: b.close >= b.open ? withAlpha(upColor, '59') : withAlpha(downColor, '59'),
      })),
    );

    if (showMarkers && trades.length) {
      const marks: SeriesMarker<Time>[] = [];
      for (const t of trades) {
        if (t.entry_time) {
          marks.push({
            time: toTime(t.entry_time, tzOffsetHours),
            position: 'belowBar',
            color: upColor,
            shape: 'arrowUp',
            // 合规：展示面不出现「买/卖」字眼，只用箭头方向表达入场/离场
            text: '',
          });
        }
        if (t.exit_time) {
          marks.push({
            time: toTime(t.exit_time, tzOffsetHours),
            position: 'aboveBar',
            color: downColor,
            shape: 'arrowDown',
            text: '',
          });
        }
      }
      marks.sort((a, b) => String(a.time).localeCompare(String(b.time)));
      marksRef.current?.setMarkers(marks);
    } else {
      marksRef.current?.setMarkers([]);
    }
    chart.timeScale().fitContent();
  }, [bars, trades, showMarkers, height, upColor, downColor, freq, tzOffsetHours]);   // height 变化会重建图表（见建图 effect），数据得重灌

  // 指标：叠加线挂主图（pane 0），振荡指标按调用点分组，每组一个副图（pane 1..N）
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const created: ISeriesApi<'Line' | 'Histogram'>[] = [];

    const add = (s: IndicatorSeries, pane: number, color: string) => {
      const pts = toPoints(bars, s.values);
      if (!pts.length) return;      // 不同源/无数据：不画，也不留下空副图
      if (s.kind === 'hist') {
        const hist = chart.addSeries(
          HistogramSeries,
          { priceLineVisible: false, lastValueVisible: pane > 0 },
          pane,
        );
        hist.setData(pts.map((p) => (p.value == null
          ? { time: p.time }
          : { time: p.time, value: p.value, color: p.value >= 0 ? upColor : downColor })));
        created.push(hist);
        return;
      }
      const line = chart.addSeries(
        LineSeries,
        {
          color,
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: pane > 0,
          crosshairMarkerVisible: false,
        },
        pane,
      );
      line.setData(pts);
      created.push(line);
    };

    if (showIndicators && indicators) {
      indicators.overlays.forEach((s, i) => add(s, 0, seriesColor(true, i)));
      paneGroups(indicators).forEach((g, gi) => {
        indicators.panes
          .filter((s) => s.group === g)
          .forEach((s, si) => add(s, gi + 1, seriesColor(false, si)));
      });
      // 主图占大头：主图 4 份，每个副图 1 份
      chart.panes().forEach((p, i) => p.setStretchFactor(i === 0 ? 4 : 1));
    }

    return () => {
      if (!chartRef.current) return;   // 建图 effect 已销毁图表（高度变化/卸载）
      for (const s of created) chart.removeSeries(s);
      // 序列移除后空副图可能残留，显式回收，只留主图
      while (chart.panes().length > 1) chart.removePane(chart.panes().length - 1);
    };
  }, [bars, indicators, showIndicators, upColor, downColor]);

  return <div ref={boxRef} className="kline-box" style={{ height }} />;
}
