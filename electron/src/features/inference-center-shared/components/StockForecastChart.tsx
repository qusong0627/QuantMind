import React, { useMemo } from 'react';
import ReactECharts from 'echarts-for-react';
import { KlineItem, ForecastPoint } from '../../../services/inferenceCenterService';
import {
  buildScoreAlignment,
  extendTimeline,
  impliedTargetPrice,
  latestScore,
  type ScoreAnchorMode,
  type ScorePointLike,
} from '../../shared/scoreWindow';

interface StockForecastChartProps {
  kline: KlineItem[];
  forecast: ForecastPoint[];
  symbol: string;
  stockName: string;
  currentPrice: number;
  modelName?: string;
  asOfDate?: string;
  /** 货币符号（¥ / HK$ / $），由各市场适配器提供 */
  currencySymbol?: string;
  /** 与预测同源、同模型、同 as-of 的历史分数（右轴叠加） */
  scorePoints?: ScorePointLike[];
  /** 模型实际周期 T+N */
  horizon?: number;
  /** 预测窗口：执行日 → 目标日的全部交易日 */
  windowDates?: string[];
  anchorMode?: ScoreAnchorMode;
  onAnchorModeChange?: (mode: ScoreAnchorMode) => void;
  /** return / rank / …；只有 return 才允许把分数换算成目标价 */
  targetMode?: string | null;
  entryDate?: string;
  targetDate?: string;
  scoreSource?: string;
  staleDays?: number | null;
  refreshing?: boolean;
}

/**
 * 单票研判：K 线 + T+N 预测扇形 + 模型分数叠加。
 *
 * 口径（与后端 `score_window.py` 一致）：
 * - 分数日 D 由 `scorePoints[].trade_date` 给出；
 * - `prediction` 模式把分数画在 D，`resolution` 模式画在后端算好的 target_date；
 * - 未来窗口 [entryDate, targetDate] 用交易日列表延长 x 轴，最新分数以虚线延伸；
 * - 只有 target_mode=return 才画「分数隐含目标价」；rank/score 口径不换算价格。
 */
export const StockForecastChart: React.FC<StockForecastChartProps> = ({
  kline,
  forecast,
  symbol,
  stockName,
  currentPrice,
  modelName,
  asOfDate,
  currencySymbol = '¥',
  scorePoints = [],
  horizon,
  windowDates = [],
  anchorMode = 'prediction',
  onAnchorModeChange,
  targetMode,
  entryDate,
  targetDate,
  scoreSource,
  staleDays,
  refreshing = false,
}) => {
  const option = useMemo(() => {
    // ── 时间轴：历史 K 线 ∪ 预测窗口 ∪ 扇形终点 ──────────────────
    // 全部是真实交易日（后端给），不再拼自然日。
    const historyDates = kline.map((k) => k.date);
    const forecastDates = forecast.map((f) => f.date);
    const timeline = extendTimeline(historyDates, [...windowDates, ...forecastDates]);
    const barByDate = new Map(kline.map((k) => [k.date, k]));
    const klineData = timeline.map((d) => {
      const bar = barByDate.get(d);
      // 未来窗口没有 K 线：用 '-' 占位，不画假蜡烛。
      return bar ? [bar.open, bar.close, bar.low, bar.high] : '-';
    });

    // ── 基准日（as-of）在时间轴上的位置 ─────────────────────────
    let baseIndex = historyDates.length - 1;
    if (asOfDate) {
      const exact = timeline.indexOf(asOfDate);
      if (exact >= 0) {
        baseIndex = exact;
      } else {
        for (let i = timeline.length - 1; i >= 0; i -= 1) {
          if (timeline[i] <= asOfDate) {
            baseIndex = i;
            break;
          }
        }
      }
    }
    const baseBar = baseIndex >= 0 ? kline.find((k) => k.date === timeline[baseIndex]) : undefined;
    const anchorPrice = currentPrice > 0 ? currentPrice : (baseBar?.close ?? 0);

    // ── 预测扇形：锚点 → 目标日（单点，不做逐日插值）────────────
    const p50SeriesData: (number | null)[] = new Array(timeline.length).fill(null);
    const p90SeriesData: (number | null)[] = new Array(timeline.length).fill(null);
    const p10SeriesData: (number | null)[] = new Array(timeline.length).fill(null);
    if (baseIndex >= 0 && anchorPrice > 0) {
      p50SeriesData[baseIndex] = anchorPrice;
      p90SeriesData[baseIndex] = anchorPrice;
      p10SeriesData[baseIndex] = anchorPrice;
    }
    forecast.forEach((f) => {
      const idx = timeline.indexOf(f.date);
      if (idx < 0) return;
      p50SeriesData[idx] = f.predicted_price;
      p90SeriesData[idx] = f.upper_price;
      p10SeriesData[idx] = f.lower_price;
    });
    const hasForecast = p50SeriesData.some((v) => v != null);

    // ── 分数序列：按落点模式映射，只画真实分数 ──────────────────
    const aligned = buildScoreAlignment(scorePoints, anchorMode);
    const alignedByDate = new Map(aligned.map((p) => [p.date, p]));
    const scoreData = timeline.map((d) => alignedByDate.get(d)?.value ?? null);
    const hasScore = scoreData.some((v) => v != null);

    // 最新分数与其预测窗口：虚线水平延伸（不是逐日价格路径）。
    const latest = latestScore(scorePoints);
    const windowStart = entryDate || windowDates[0] || '';
    const windowEnd = targetDate || windowDates[windowDates.length - 1] || '';
    const projectionData = timeline.map((d) => {
      if (!latest || !windowEnd) return null;
      const start = windowStart || latest.date;
      if (d < start || d > windowEnd) return null;
      return latest.value;
    });
    const hasProjection = projectionData.some((v) => v != null);

    // 分数隐含目标价：仅 return 口径。用分数日的收盘作锚，避免用
    // as-of 之后的现价去放大过期分数。
    const scoreAnchorClose =
      (latest && barByDate.get(latest.date)?.close) || anchorPrice;
    const targetPrice = impliedTargetPrice(latest?.value, scoreAnchorClose, targetMode);

    // ── 窗口阴影 ─────────────────────────────────────────────────
    const hasWindow =
      Boolean(windowStart) && Boolean(windowEnd) &&
      timeline.includes(windowStart) && timeline.includes(windowEnd);

    // ── 分数轴量程（留 15% 边）──────────────────────────────────
    const scoreVals = aligned.map((p) => p.value);
    const scoreMin = scoreVals.length ? Math.min(...scoreVals) : 0;
    const scoreMax = scoreVals.length ? Math.max(...scoreVals) : 1;
    const scoreSpan = scoreMax - scoreMin;
    const scorePad = scoreSpan > 1e-9 ? scoreSpan * 0.15 : Math.max(0.002, Math.abs(scoreMax) * 0.3);

    const legendData = [
      '日K线',
      ...(hasForecast ? ['P50 基准中枢 (50%)', 'P90 乐观上界 (90%)', 'P10 悲观下界 (10%)'] : []),
      ...(hasScore ? ['模型分数'] : []),
      ...(hasProjection ? ['分数·预测窗口'] : []),
    ];

    return {
      backgroundColor: 'transparent',
      animation: true,
      animationDuration: 600,
      textStyle: {
        fontFamily: "'Microsoft YaHei', '微软雅黑', 'PingFang SC', sans-serif",
      },
      tooltip: {
        trigger: 'axis',
        axisPointer: {
          type: 'cross',
          lineStyle: { color: '#94a3b8', width: 1, type: 'dashed' },
        },
        backgroundColor: 'rgba(255, 255, 255, 0.95)',
        borderColor: '#e2e8f0',
        borderWidth: 1,
        textStyle: { color: '#1e293b', fontSize: 12 },
        padding: [10, 14],
        extraCssText: 'box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.1); border-radius: 12px;',
        formatter: (params: any[]) => {
          if (!params || params.length === 0) return '';
          const date = params[0].axisValue;
          let html = `<div style="font-weight: 700; margin-bottom: 6px; color: #0f172a;">${date}</div>`;

          params.forEach((item: any) => {
            if (item.seriesType === 'candlestick') {
              const bar = barByDate.get(date);
              if (!bar) return;
              html += `
                <div style="display: flex; justify-content: space-between; gap: 12px; font-size: 11px; margin: 2px 0;">
                  <span style="color: #334155;">K线收盘:</span>
                  <span style="font-weight: 600; font-family: monospace;">${currencySymbol}${Number(bar.close).toFixed(2)}</span>
                </div>
              `;
              return;
            }
            if (item.value == null) return;
            if (String(item.seriesName).startsWith('模型分数') || String(item.seriesName).startsWith('分数·')) {
              const alignedPoint = alignedByDate.get(date);
              const scoreDate = alignedPoint?.scoreDate ? ` · 分数日 ${alignedPoint.scoreDate}` : '';
              html += `
                <div style="display: flex; justify-content: space-between; gap: 12px; font-size: 11px; margin: 2px 0;">
                  <span style="color: ${item.color}; font-weight: 500;">${item.seriesName}${scoreDate}:</span>
                  <span style="font-weight: 600; font-family: monospace; color: #0f172a;">${Number(item.value).toFixed(4)}</span>
                </div>
              `;
              return;
            }
            html += `
              <div style="display: flex; justify-content: space-between; gap: 12px; font-size: 11px; margin: 2px 0;">
                <span style="color: ${item.color}; font-weight: 500;">${item.seriesName}:</span>
                <span style="font-weight: 600; font-family: monospace; color: #0f172a;">${currencySymbol}${Number(item.value).toFixed(2)}</span>
              </div>
            `;
          });
          if (hasWindow) {
            html += `<div style="margin-top: 6px; padding-top: 4px; border-top: 1px dashed #e2e8f0; font-size: 10px; color: #64748b;">
              T+${horizon ?? '—'} 窗口：${windowStart} 执行 → ${windowEnd} 目标
            </div>`;
          }
          if (latest) {
            html += `<div style="font-size: 10px; color: #64748b;">最后分数日：${latest.date}${staleDays && staleDays > 0 ? `（已过期 ${staleDays} 天）` : ''}</div>`;
          }
          if (scoreSource) {
            html += `<div style="font-size: 10px; color: #94a3b8;">来源：${scoreSource}</div>`;
          }
          return html;
        },
      },
      legend: {
        data: legendData,
        formatter: (name: string) => name.replace(/\s*\(\d+%\)$/, ''),
        bottom: 4,
        itemGap: 12,
        itemWidth: 14,
        itemHeight: 8,
        textStyle: { color: '#334155', fontSize: 10, fontWeight: 500 },
      },
      grid: {
        left: '4%',
        right: hasScore ? '8%' : '4%',
        top: '12%',
        bottom: '14%',
        containLabel: true,
      },
      xAxis: {
        type: 'category',
        data: timeline,
        scale: true,
        boundaryGap: true,
        axisLine: { lineStyle: { color: '#e2e8f0' } },
        axisTick: { show: false },
        axisLabel: {
          color: '#334155',
          fontSize: 10,
          formatter: (val: string) => (val ? val.slice(5) : ''),
        },
        splitLine: { show: false },
      },
      yAxis: [
        {
          type: 'value',
          scale: true,
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: {
            color: '#334155',
            fontSize: 10,
            formatter: (v: number) => `${currencySymbol}${v.toFixed(1)}`,
          },
          splitLine: {
            lineStyle: { color: 'rgba(226, 232, 240, 0.6)', type: 'dashed' },
          },
        },
        ...(hasScore
          ? [{
              type: 'value',
              position: 'right',
              scale: true,
              min: scoreMin - scorePad,
              max: scoreMax + scorePad,
              name: '分数',
              nameTextStyle: { fontSize: 9, color: '#6366f1' },
              axisLine: { show: true, lineStyle: { color: '#c7d2fe' } },
              axisTick: { show: false },
              axisLabel: {
                color: '#6366f1',
                fontSize: 9,
                formatter: (v: number) => Number(v).toFixed(3),
              },
              splitLine: { show: false },
            }]
          : []),
      ],
      series: [
        {
          name: '日K线',
          type: 'candlestick',
          data: klineData,
          itemStyle: {
            color: '#ef4444',
            color0: '#10b981',
            borderColor: '#ef4444',
            borderColor0: '#10b981',
          },
          markArea: hasWindow
            ? {
                silent: true,
                itemStyle: { color: 'rgba(99, 102, 241, 0.06)' },
                label: {
                  show: true,
                  position: 'insideTop',
                  formatter: `T+${horizon ?? ''} 预测窗口`,
                  color: '#6366f1',
                  fontSize: 9,
                  fontWeight: 700,
                },
                data: [[{ xAxis: windowStart }, { xAxis: windowEnd }]],
              }
            : undefined,
          markLine: baseIndex >= 0
            ? {
                symbol: ['none', 'none'],
                data: [
                  {
                    xAxis: timeline[baseIndex],
                    lineStyle: { color: '#3b82f6', type: 'dashed', width: 1.5 },
                    label: {
                      show: true,
                      formatter: `T 数据日 ${timeline[baseIndex].slice(5)}`,
                      position: 'top',
                      color: '#2563eb',
                      fontSize: 10,
                      fontWeight: 700,
                    },
                  },
                  ...(targetPrice != null && windowEnd
                    ? [{
                        yAxis: targetPrice,
                        lineStyle: { color: '#f59e0b', type: 'dashed', width: 1.2 },
                        label: {
                          show: true,
                          formatter: `分数隐含目标价 ${currencySymbol}${targetPrice.toFixed(2)}`,
                          position: 'insideEndTop',
                          color: '#b45309',
                          fontSize: 9,
                          fontWeight: 700,
                        },
                      }]
                    : []),
                ],
              }
            : undefined,
        },
        ...(hasForecast
          ? [
              {
                name: 'P90 乐观上界 (90%)',
                type: 'line',
                data: p90SeriesData,
                smooth: 0.3,
                lineStyle: { color: '#ef4444', width: 2, type: 'dashed' },
                itemStyle: { color: '#ef4444' },
                symbol: 'circle',
                symbolSize: 4,
                z: 3,
              },
              {
                name: 'P50 基准中枢 (50%)',
                type: 'line',
                data: p50SeriesData,
                smooth: 0.3,
                lineStyle: { color: '#2563eb', width: 3 },
                itemStyle: { color: '#2563eb' },
                symbol: 'circle',
                symbolSize: 6,
                areaStyle: {
                  color: {
                    type: 'linear',
                    x: 0,
                    y: 0,
                    x2: 0,
                    y2: 1,
                    colorStops: [
                      { offset: 0, color: 'rgba(37, 99, 235, 0.16)' },
                      { offset: 1, color: 'rgba(37, 99, 235, 0.02)' },
                    ],
                  },
                },
                z: 4,
              },
              {
                name: 'P10 悲观下界 (10%)',
                type: 'line',
                data: p10SeriesData,
                smooth: 0.3,
                lineStyle: { color: '#10b981', width: 2, type: 'dashed' },
                itemStyle: { color: '#10b981' },
                symbol: 'circle',
                symbolSize: 4,
                z: 3,
              },
            ]
          : []),
        ...(hasScore
          ? [{
              name: '模型分数',
              type: 'line',
              yAxisIndex: 1,
              data: scoreData,
              symbol: 'circle',
              symbolSize: 4,
              connectNulls: false,
              lineStyle: { color: '#6366f1', width: 1.6 },
              itemStyle: { color: '#6366f1', borderColor: '#fff', borderWidth: 1 },
              z: 6,
            }]
          : []),
        ...(hasProjection
          ? [{
              name: '分数·预测窗口',
              type: 'line',
              yAxisIndex: 1,
              data: projectionData,
              symbol: 'none',
              connectNulls: true,
              lineStyle: { color: '#6366f1', width: 1.4, type: 'dashed' },
              itemStyle: { color: '#6366f1' },
              z: 5,
            }]
          : []),
      ],
    };
  }, [
    kline, forecast, asOfDate, currentPrice, currencySymbol,
    scorePoints, horizon, windowDates, anchorMode, targetMode,
    entryDate, targetDate, scoreSource, staleDays,
  ]);

  const hasScore = scorePoints.some((p) => p.fusion_score != null);

  return (
    <div className="w-full h-full relative flex flex-col">
      {/* 2行结构化标题栏 */}
      <div className="flex flex-col gap-1 px-6 pt-3 pb-1 shrink-0 border-b border-slate-50">
        {/* 第一行：主标题 + 真实模型徽标 + 落点切换 */}
        <div className="flex items-center justify-between gap-2">
          <span className="text-sm font-black text-slate-800 tracking-tight">
            历史 K 线走势与真实模型信号
          </span>
          <div className="flex items-center gap-1.5 shrink-0">
            {onAnchorModeChange && (
              <div className="flex items-center rounded-full bg-slate-100 border border-slate-200 p-0.5">
                {([
                  { key: 'prediction', label: '预测日' },
                  { key: 'resolution', label: '兑现日' },
                ] as const).map((m) => (
                  <button
                    key={m.key}
                    type="button"
                    onClick={() => onAnchorModeChange(m.key)}
                    title={m.key === 'prediction' ? '分数画在产生它的数据日 D' : '分数画在 T+N 目标日 D+1+N'}
                    className={`px-2 py-0.5 rounded-full text-[10px] font-bold transition-colors ${
                      anchorMode === m.key ? 'bg-white text-indigo-600 shadow-sm' : 'text-slate-500 hover:text-slate-700'
                    }`}
                  >
                    {m.label}
                  </button>
                ))}
              </div>
            )}
            <span className="inline-flex items-center gap-1 text-[11px] text-emerald-600 font-bold bg-emerald-50 px-2 py-0.5 rounded-md border border-emerald-100 whitespace-nowrap">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
              真实模型结果
            </span>
            {refreshing && (
              <span className="text-[10px] font-bold text-indigo-500 animate-pulse whitespace-nowrap">
                更新中…
              </span>
            )}
          </div>
        </div>

        {/* 第二行：基准日 + 模型 + 周期 + 来源 */}
        <div className="flex items-center gap-3 text-xs text-slate-600 min-w-0 flex-wrap">
          {asOfDate && (
            <span className="flex items-center gap-1 font-mono whitespace-nowrap shrink-0">
              数据日: <strong className="text-slate-600 font-semibold">{asOfDate}</strong>
            </span>
          )}
          {horizon != null && (
            <span className="flex items-center gap-1 font-mono whitespace-nowrap shrink-0">
              周期: <strong className="text-indigo-600 font-semibold">T+{horizon}</strong>
            </span>
          )}
          {modelName && (
            <span className="flex items-center gap-1 font-mono min-w-0">
              <span className="whitespace-nowrap shrink-0">模型:</span>
              <strong className="text-slate-700 font-semibold bg-slate-50 px-1.5 py-0.2 rounded border border-slate-100 truncate min-w-0">{modelName}</strong>
            </span>
          )}
          {scoreSource && (
            <span className="flex items-center gap-1 font-mono whitespace-nowrap shrink-0">
              分数来源: <strong className="text-slate-600 font-semibold">{scoreSource}</strong>
            </span>
          )}
          {staleDays != null && staleDays > 0 && (
            <span className="font-bold text-rose-600 whitespace-nowrap shrink-0">
              分数已过期 {staleDays} 天
            </span>
          )}
          {!hasScore && (
            <span className="text-[10px] text-slate-400 whitespace-nowrap">
              该模型无历史分数（无 pred 产物或窗口内无分数）
            </span>
          )}
        </div>
      </div>
      <div className="flex-1 min-h-0 w-full">
        <ReactECharts
          option={option}
          style={{ width: '100%', height: '100%' }}
          opts={{ renderer: 'canvas' }}
        />
      </div>
    </div>
  );
};
