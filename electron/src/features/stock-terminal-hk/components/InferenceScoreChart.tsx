/** 默认模型推理分折线图：仅展示用户设置的默认模型（数据源锁定其 pred.parquet），红正绿负，点击点联动右侧详情 */
import { useEffect, useMemo, useState } from 'react';
import { TrendingUp, TrendingDown, RefreshCw } from 'lucide-react';
import { Spin } from 'antd';
import ReactECharts from 'echarts-for-react';
import { modelTrainingService } from '../../../services/modelTrainingService';

interface Props {
  symbol: string; // suffix 600519.SH
  /** 推理分数所属模型；不传或 'default' 时跟随用户默认模型 */
  modelId?: string;
  selectedDate?: string | null; // 当前联动日期（高亮）
  onPointClick?: (date: string) => void;
  onScoresLoaded?: (points: Point[]) => void; // 分数点回传，供 K 线副图使用
  /** 模型列表 + 当前选中模型名回传（供页面顶部模型下拉使用） */
  onModelsLoaded?: (models: Array<{ model_id: string; display_name?: string }>, modelName: string) => void;
  refreshKey?: number;
  height?: number;
  /** 分数回溯窗口（自然日）。默认 180；个股终端需传更大值以覆盖「当前日期向前 2 年」的 K 线全区间，
   * 否则 K 线副图左侧交易日没有分数。 */
  days?: number;
}

export interface Point {
  date: string;
  value: number;
  side: string | null;
}

export function InferenceScoreChart({ symbol, modelId, selectedDate, onPointClick, onScoresLoaded, onModelsLoaded, refreshKey = 0, height = 220, days = 180 }: Props) {
  const [points, setPoints] = useState<Point[]>([]);
  const [loading, setLoading] = useState(false);
  const [modelName, setModelName] = useState<string>('');

  useEffect(() => {
    if (!symbol) {
      setPoints([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    const code = symbol.split('.')[0];
    // 传 model_id（如有）：后端按指定模型返回 pred 分数；否则锁定用户默认模型。
    // days 由调用方控制窗口（个股终端传 750 保证覆盖最长 2 年的 K 线）
    modelTrainingService
      .getStockInferenceHistory(code, days, modelId || undefined)
      .then((resp) => {
        if (cancelled) return;
        const pts: Point[] = (resp.items ?? [])
          .filter((it) => it.fusion_score != null)
          .map((it) => ({
            date: String(it.trade_date).slice(0, 10),
            value: Number(it.fusion_score),
            side: it.signal_side ? String(it.signal_side) : null,
          }))
          .sort((a, b) => a.date.localeCompare(b.date));
        setPoints(pts);
        onScoresLoaded?.(pts);
        const models = resp.models ?? [];
        const chosen = modelId
          ? models.find((m) => m.model_id === modelId)
          : models[0];
        const name = chosen ? (chosen.display_name || chosen.model_id || '') : '';
        setModelName(name);
        onModelsLoaded?.(models, name);
      })
      .catch(() => {
        if (!cancelled) {
          setPoints([]);
          onScoresLoaded?.([]);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, modelId, refreshKey, days]);

  const option = useMemo(() => {
    if (!points.length) return null;
    const dates = points.map((p) => p.date);
    const values = points.map((p) => p.value);
    const numericVals = (values as any[]).filter((v) => typeof v === 'number' && Number.isFinite(v)) as number[];
    const min = numericVals.length ? Math.min(...numericVals) : 0;
    const max = numericVals.length ? Math.max(...numericVals) : 1;
    const span = max - min;
    const pad = span > 1e-9 ? span * 0.15 : Math.max(0.002, Math.abs(max) * 0.3);
    const digits = 3;
    return {
      animation: false,
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis' as const,
        backgroundColor: 'rgba(255,255,255,0.96)',
        borderColor: '#e2e8f0',
        textStyle: { color: '#334155', fontSize: 11 },
        formatter: (params: any) => {
          const p = Array.isArray(params) ? params[0] : params;
          const idx = p.dataIndex;
          const d = dates[idx];
          const v = (values as any[])[idx];
          if (d == null || v == null || !Number.isFinite(v)) return `${d ?? ''}<br/>分数 --`;
          return `${d}<br/>分数 <b style="color:${Number(v) >= 0 ? '#e11d48' : '#059669'}">${Number(v).toFixed(digits)}</b>`;
        },
      },
      grid: { left: 48, right: 16, top: 12, bottom: 28 },
      xAxis: {
        type: 'category' as const,
        data: dates,
        axisLine: { lineStyle: { color: '#e2e8f0' } },
        axisTick: { show: false },
        axisLabel: { color: '#94a3b8', fontSize: 10, formatter: (v: string) => v.slice(5) },
      },
      yAxis: {
        type: 'value' as const,
        min: min - pad,
        max: max + pad,
        axisLine: { lineStyle: { color: '#6366f1' } },
        axisLabel: { color: '#64748b', fontSize: 10, formatter: (v: number) => v.toFixed(digits) },
        splitLine: { lineStyle: { color: '#f1f5f9' } },
      },
      dataZoom: [
        { type: 'inside' as const, xAxisIndex: 0, start: 0, end: 100 },
        { type: 'slider' as const, xAxisIndex: 0, bottom: 0, height: 14, borderColor: '#e2e8f0', fillerColor: 'rgba(99,102,241,0.08)' },
      ],
      series: [
        {
          type: 'line' as const,
          data: values,
          symbol: 'circle',
          symbolSize: 4,
          lineStyle: { width: 1.6, color: '#6366f1' },
          itemStyle: {
            color: (p: any) => (points[p.dataIndex]?.value ?? 0) >= 0 ? '#e11d48' : '#059669',
            borderColor: '#fff',
            borderWidth: 1,
          },
          areaStyle: { color: 'rgba(99,102,241,0.06)' },
          markLine: {
            silent: true,
            symbol: 'none',
            data: [{ yAxis: 0, lineStyle: { color: '#94a3b8', type: 'dashed', width: 1 }, label: { formatter: '0', fontSize: 9, color: '#94a3b8' } }],
          },
          markPoint: selectedDate
            ? {
                symbol: 'circle',
                symbolSize: 10,
                data: (() => {
                  const idx = dates.findIndex((d) => d === selectedDate);
                  if (idx < 0) return [];
                  const v = (values as any[])[idx];
                  if (v == null || !Number.isFinite(v)) return [];
                  return [{ coord: [idx, v], itemStyle: { color: '#f59e0b' } }];
                })(),
              }
            : undefined,
        },
      ],
    };
  }, [points, selectedDate]);

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center" style={{ height }}>
        <Spin size="small" />
        <span className="ml-2 text-xs text-slate-400">推理分数加载中…</span>
      </div>
    );
  }

  if (!points.length) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-2 text-xs text-slate-400" style={{ height }}>
        <TrendingDown className="w-5 h-5 opacity-40" />
        暂无默认模型分数
        <span className="text-[11px]">请先在模型管理设置默认模型（需含 pred 历史分数）</span>
      </div>
    );
  }

  const last = points[points.length - 1];
  const up = last.value >= 0;

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between px-1 mb-1 shrink-0">
        <span className="flex items-center gap-1.5 text-[11px] font-bold text-slate-700">
          <span className={`w-6 h-6 rounded-lg flex items-center justify-center ${up ? 'bg-rose-50 text-rose-500' : 'bg-emerald-50 text-emerald-500'}`}>
            {up ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
          </span>
          默认模型推理分
          {modelName && <span className="text-[10px] font-mono text-slate-400 truncate max-w-[140px]">· {modelName}</span>}
        </span>
        <span className="flex items-center gap-1.5 text-[10px] text-slate-400">
          <span className={`font-mono font-bold ${up ? 'text-rose-500' : 'text-emerald-500'}`}>{last.value.toFixed(4)}</span>
          <span className="text-slate-300">|</span>
          <span>{last.date}</span>
          <RefreshCw className="w-3 h-3 text-slate-300" />
        </span>
      </div>
      <div className="flex-1 min-h-0">
        <ReactECharts
          option={option as any}
          notMerge
          lazyUpdate
          style={{ width: '100%', height: height - 28 }}
          opts={{ renderer: 'canvas' }}
          onEvents={
            onPointClick
              ? {
                  click: (params: any) => {
                    const idx = params?.dataIndex;
                    if (typeof idx === 'number' && points[idx]) onPointClick(points[idx].date);
                  },
                }
              : undefined
          }
        />
      </div>
    </div>
  );
}
