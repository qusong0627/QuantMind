/**
 * 跨市场叠加对比（T-FB-14）——同一因子在各市场的净值/IC 多线对比。
 *
 * 数据面：POST /matrix 拿到该因子各市场的最近 run_id（完成格），再并行拉
 * `/runs/{id}/series`。某市场无序列（降级/失败）时列旁标注原因，不画假线。
 *
 * 口径提示常显：CN 是样本内（挖掘原始市场），其余是样本外重算——不同市场
 * 日历/费率/币种不同，曲线只作形状与稳定性对比，不是同一时间轴上的净值赛跑。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Loader2, AlertCircle, GitCompare } from 'lucide-react';
import { EChartsChart } from '../../../../components/common/EChartsChart';
import { fetchMatrix, getRunSeries } from '../../services-v2/factorBacktestApi';
import type { RunSeries } from '../../types-v2/backtestCenter';
import { cn } from '../../utils-v2';

type OverlayMetric = 'nav_long' | 'ic_cum' | 'nav_ls';

const OVERLAY_METRICS: { id: OverlayMetric; label: string; hint: string }[] = [
  { id: 'nav_long', label: '头部组合净值', hint: '多头腿（TopK）累计净值，起点 1.0' },
  { id: 'ic_cum', label: 'IC 累计', hint: '日 IC 求和累计（IC 不是收益率，不做复利）' },
  { id: 'nav_ls', label: '多空净值', hint: '多空腿累计净值' },
];

/** 多线配色（玻璃拟态浅底上的高对比序列） */
const LINE_COLORS = ['#2563eb', '#dc2626', '#059669', '#d97706', '#7c3aed', '#0e7490'];

interface MarketSeries {
  market: string;
  label: string;
  inSample: boolean;
  status: string;
  error: string | null;
  series: RunSeries | null;
}

export interface ComparisonOverlayProps {
  factorId: string;
  /** 当前从哪个市场钻进来（图例上打「当前」角标） */
  currentMarket: string;
}

export const ComparisonOverlay: React.FC<ComparisonOverlayProps> = ({
  factorId,
  currentMarket,
}) => {
  const [entries, setEntries] = useState<MarketSeries[]>([]);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [metric, setMetric] = useState<OverlayMetric>('nav_long');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    setLoading(true);
    setError(null);
    try {
      const matrix = await fetchMatrix({ factorIds: [factorId] });
      if (seq !== seqRef.current) return;
      if (!matrix.success || !matrix.data) {
        setError(matrix.error ?? '加载跨市场矩阵失败');
        return;
      }
      const row = matrix.data.factors[0];
      if (!row) {
        setEntries([]);
        return;
      }
      const targets = matrix.data.markets.map((col) => {
        const cell = row.cells[col.market];
        return { col, cell };
      });
      // 并行拉有序列的市场的曲线（通常 ≤5 个请求）
      const loaded = await Promise.all(
        targets.map(async ({ col, cell }): Promise<MarketSeries> => {
          const base = {
            market: col.market,
            label: col.label,
            inSample: col.inSample,
            status: cell?.status ?? 'not_run',
            error: cell?.error ?? null,
          };
          if (!cell || cell.status !== 'completed' || !cell.runId) {
            return { ...base, series: null };
          }
          const resp = await getRunSeries(cell.runId);
          return {
            ...base,
            series: resp.success && resp.data ? resp.data.series : null,
          };
        }),
      );
      if (seq !== seqRef.current) return;
      setEntries(loaded);
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [factorId]);

  useEffect(() => {
    void load();
    setExcluded(new Set());
  }, [load]);

  const metricSpec = OVERLAY_METRICS.find((m) => m.id === metric)!;
  const visible = entries.filter((e) => e.series && !excluded.has(e.market));

  const chartOption = useMemo(() => {
    if (visible.length === 0) return null;
    const series = visible.map((e, i) => {
      const s = e.series!;
      const values =
        metric === 'nav_long'
          ? s.navLong
          : metric === 'nav_ls'
            ? s.navLs
            : s.icCum;
      return {
        name: `${e.label}${e.market === currentMarket ? '（当前）' : ''}`,
        type: 'line' as const,
        showSymbol: false,
        data: s.dates.map((d, idx) => [d, values[idx] ?? null]),
        lineStyle: { width: 2, color: LINE_COLORS[i % LINE_COLORS.length] },
        itemStyle: { color: LINE_COLORS[i % LINE_COLORS.length] },
        connectNulls: false,
      };
    });
    return {
      animation: false,
      grid: { top: 34, left: 56, right: 16, bottom: 48 },
      legend: {
        top: 0,
        textStyle: { fontSize: 11, color: '#64748b' },
        itemWidth: 12,
        itemHeight: 8,
      },
      tooltip: {
        trigger: 'axis',
        textStyle: { fontSize: 11 },
        valueFormatter: (v: number | null) => (v == null ? '—' : String(v)),
      },
      xAxis: {
        type: 'time',
        axisLine: { lineStyle: { color: '#e2e8f0' } },
        axisLabel: { fontSize: 10, color: '#94a3b8' },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { fontSize: 10, color: '#94a3b8' },
        splitLine: { lineStyle: { color: '#f1f5f9' } },
      },
      dataZoom: [
        { type: 'inside' },
        { type: 'slider', height: 16, bottom: 8, borderColor: 'transparent' },
      ],
      series,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, metric, currentMarket]);

  if (loading) {
    return (
      <p className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> 加载各市场曲线…
      </p>
    );
  }

  if (error) {
    return (
      <p className="flex items-center gap-2 py-4 text-sm text-destructive">
        <AlertCircle className="h-4 w-4" /> {error}
      </p>
    );
  }

  return (
    <div className="space-y-3" data-testid="comparison-overlay">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <GitCompare className="h-3.5 w-3.5 text-primary" />
        <span className="text-muted-foreground">{metricSpec.hint}</span>
        <span className="ml-auto inline-flex overflow-hidden rounded-md border border-border/60">
          {OVERLAY_METRICS.map((m) => (
            <button
              key={m.id}
              type="button"
              onClick={() => setMetric(m.id)}
              className={cn(
                'px-2 py-1 font-medium transition-colors',
                metric === m.id
                  ? 'bg-primary/15 text-primary'
                  : 'text-muted-foreground hover:bg-muted/50',
              )}
            >
              {m.label}
            </button>
          ))}
        </span>
      </div>

      {/* 市场开关（只有有序列的市场可勾） */}
      <div className="flex flex-wrap gap-1.5 text-[11px]">
        {entries.map((e) => (
          <label
            key={e.market}
            title={e.series ? '' : `无曲线（${e.status === 'not_run' ? '未回测' : e.error ?? e.status}）`}
            className={cn(
              'inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5',
              e.series
                ? 'cursor-pointer border-border/60 hover:border-primary/50'
                : 'cursor-not-allowed border-dashed border-slate-200 text-slate-400',
            )}
          >
            <input
              type="checkbox"
              disabled={!e.series}
              checked={!!e.series && !excluded.has(e.market)}
              onChange={() =>
                setExcluded((prev) => {
                  const next = new Set(prev);
                  if (next.has(e.market)) next.delete(e.market);
                  else next.add(e.market);
                  return next;
                })
              }
              className="h-3 w-3 accent-primary"
            />
            {e.label}
            {e.inSample && <span className="opacity-70">样本内</span>}
          </label>
        ))}
      </div>

      {visible.length === 0 ? (
        <p className="py-4 text-sm text-muted-foreground">
          该因子还没有任何可下钻的完成曲线——先派发批量回测。
        </p>
      ) : (
        <div className="h-[380px] w-full" data-testid="comparison-chart">
          {chartOption && <EChartsChart option={chartOption} style={{ height: '100%' }} />}
        </div>
      )}

      <p className="text-[11px] leading-relaxed text-muted-foreground">
        口径：CN 列为样本内（挖掘原始市场），其余为样本外重算；各市场交易日历、费率与币种不同，
        曲线用于形状与稳定性对比，不是同一时间轴上的净值赛跑。缺失日不连线。
      </p>
    </div>
  );
};

export default ComparisonOverlay;
