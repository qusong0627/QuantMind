import React, { useMemo } from 'react';
import ReactECharts from 'echarts-for-react';
import { KlineItem, ForecastPoint } from '../../../services/inferenceCenterService';

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
}

export const StockForecastChart: React.FC<StockForecastChartProps> = ({
  kline,
  forecast,
  symbol,
  stockName,
  currentPrice,
  modelName,
  asOfDate,
  currencySymbol = '¥',
}) => {
  const option = useMemo(() => {
    // 1. 历史 K 线数据
    const historyDates = kline.map(k => k.date);
    const klineData = kline.map(k => [k.open, k.close, k.low, k.high]); // ECharts Candlestick: [open, close, lowest, highest]

    // 2. 预测部分数据对齐
    const forecastDates = forecast.map(f => f.date);
    const allDates = [...historyDates, ...forecastDates];
    const hasForecast = forecast.length > 0;

    const lastKlineIndex = historyDates.length - 1;

    // 基准日锚点：K 线窗口含基准日后实际走势（供对照验证），预测扇形必须从
    // as_of_date 的收盘（= currentPrice，后端已按基准日截断取值）长出，
    // 竖线也钉在基准日而非最后一根 K 线
    let baseIndex = lastKlineIndex;
    if (asOfDate) {
      const exact = historyDates.lastIndexOf(asOfDate);
      if (exact >= 0) {
        baseIndex = exact;
      } else {
        const le = historyDates.map((d, i) => ({ d, i })).filter((x) => x.d <= asOfDate).pop();
        if (le) baseIndex = le.i;
      }
    }
    const anchorPrice = currentPrice > 0
      ? currentPrice
      : (baseIndex >= 0 ? kline[baseIndex].close : 0);

    // 历史部分在预测曲线上填充 null，在基准日 K 线处连接
    const p50SeriesData: (number | null)[] = new Array(historyDates.length).fill(null);
    const p90SeriesData: (number | null)[] = new Array(historyDates.length).fill(null);
    const p10SeriesData: (number | null)[] = new Array(historyDates.length).fill(null);

    if (baseIndex >= 0) {
      p50SeriesData[baseIndex] = anchorPrice;
      p90SeriesData[baseIndex] = anchorPrice;
      p10SeriesData[baseIndex] = anchorPrice;
    }

    forecast.forEach(f => {
      p50SeriesData.push(f.predicted_price);
      p90SeriesData.push(f.upper_price);
      p10SeriesData.push(f.lower_price);
    });

    return {
      backgroundColor: 'transparent',
      animation: true,
      animationDuration: 800,
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
              const [, close, low, high] = item.data;
              html += `
                <div style="display: flex; justify-content: space-between; gap: 12px; font-size: 11px; margin: 2px 0;">
                  <span style="color: #334155;">K线收盘:</span>
                  <span style="font-weight: 600; font-family: monospace;">${currencySymbol}${close?.toFixed(2)}</span>
                </div>
              `;
            } else if (item.value !== null && item.value !== undefined) {
              const color = item.color;
              html += `
                <div style="display: flex; justify-content: space-between; gap: 12px; font-size: 11px; margin: 2px 0;">
                  <span style="color: ${color}; font-weight: 500;">${item.seriesName}:</span>
                  <span style="font-weight: 600; font-family: monospace; color: #0f172a;">${currencySymbol}${Number(item.value).toFixed(2)}</span>
                </div>
              `;
            }
          });
          return html;
        },
      },
      legend: {
        data: hasForecast
          ? ['日K线', 'P50 基准中枢 (50%)', 'P90 乐观上界 (90%)', 'P10 悲观下界 (10%)']
          : ['日K线'],
        bottom: 8,
        itemGap: 18,
        textStyle: { color: '#334155', fontSize: 11, fontWeight: 500 },
      },
      grid: {
        left: '4%',
        right: '4%',
        top: '12%',
        bottom: '14%',
        containLabel: true,
      },
      xAxis: {
        type: 'category',
        data: allDates,
        scale: true,
        boundaryGap: true,
        axisLine: { lineStyle: { color: '#e2e8f0' } },
        axisTick: { show: false },
        axisLabel: {
          color: '#334155',
          fontSize: 10,
          formatter: (val: string) => val ? val.slice(5) : '',
        },
        splitLine: { show: false },
      },
      yAxis: {
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
          markLine: baseIndex >= 0 ? {
            symbol: ['none', 'none'],
            data: [
              {
                xAxis: historyDates[baseIndex],
                lineStyle: { color: '#3b82f6', type: 'dashed', width: 1.5 },
                label: {
                  show: true,
                  formatter: baseIndex >= 0 ? `T 基准日 ${historyDates[baseIndex].slice(5)}` : 'T 基准日',
                  position: 'top',
                  color: '#2563eb',
                  fontSize: 10,
                  fontWeight: 700,
                },
              },
            ],
          } : undefined,
        },
        ...(hasForecast ? [{
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
        }] : []),
      ],
    };
  }, [kline, forecast, symbol, currentPrice, asOfDate, currencySymbol]);

  return (
    <div className="w-full h-full relative flex flex-col">
      {/* 2行结构化标题栏 */}
      <div className="flex flex-col gap-1 px-6 pt-3 pb-1 shrink-0 border-b border-slate-50">
        {/* 第一行：主标题 + 实时计算徽标 */}
        <div className="flex items-center justify-between">
          <span className="text-sm font-black text-slate-800 tracking-tight">
            历史 K 线走势与真实模型信号
          </span>
          <span className="inline-flex items-center gap-1 text-[11px] text-emerald-600 font-bold bg-emerald-50 px-2 py-0.5 rounded-md border border-emerald-100 shrink-0 whitespace-nowrap">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
            真实模型结果
          </span>
        </div>

        {/* 第二行：基准日 + 模型信息 */}
        <div className="flex items-center gap-3 text-xs text-slate-600">
          {asOfDate && (
            <span className="flex items-center gap-1 font-mono">
              基准日期: <strong className="text-slate-600 font-semibold">{asOfDate}</strong>
            </span>
          )}
          {modelName && (
            <span className="flex items-center gap-1 font-mono truncate">
              模型: <strong className="text-slate-700 font-semibold bg-slate-50 px-1.5 py-0.2 rounded border border-slate-100 truncate">{modelName}</strong>
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
