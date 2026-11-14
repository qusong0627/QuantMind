import React, { useEffect, useMemo, useState } from 'react';
import type { IndexItem } from '../BroadMarketHeader';
import { HeatmapDrillCard } from '../HeatmapDrillCard';
import type { SectorHeatmapItem } from '../../../market-analysis-shared/SectorHeatmapChart';
import { fetchMarketRegime, type RegimeResponse } from '../../services/regimeService';
import { fetchMarketSentiment, type MarketSentiment } from '../../services/sentimentService';
import { getFlowPanorama, type FlowPanoramaResponse } from '../../services/moneyFlowService';
import { IndexTape } from './IndexTape';
import { PanoramaHeader } from './PanoramaHeader';
import { StageSummaryCard } from './StageSummaryCard';
import { SentimentSummaryCard, type PanoramaBreadth } from './SentimentSummaryCard';
import { FlowSummaryCard } from './FlowSummaryCard';

export function MarketPanoramaPanel({
  date,
  indices,
  breadth,
  heatmap,
  loading: pageLoading,
  snapshotMissing,
  snapshotFrom,
  onNavigate,
  onOpenStock,
}: {
  date?: string;
  indices: IndexItem[];
  breadth: PanoramaBreadth | null;
  heatmap: SectorHeatmapItem[];
  loading?: boolean;
  snapshotMissing?: boolean;
  snapshotFrom?: string;
  onNavigate: (tab: string) => void;
  onOpenStock?: (symbol: string) => void;
}): React.ReactElement {
  const [regime, setRegime] = useState<RegimeResponse | null>(null);
  const [sentiment, setSentiment] = useState<MarketSentiment | null>(null);
  const [flow, setFlow] = useState<FlowPanoramaResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(0);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setFailed(0);
    Promise.allSettled([
      fetchMarketRegime(date),
      fetchMarketSentiment(date),
      getFlowPanorama({
        date,
        period: '1d',
        sectorSet: 'industry2',
        topSectors: 30,
        stocksPerSector: 3,
        direction: 'all',
      }),
    ]).then(([r, s, f]) => {
      if (cancelled) return;
      const failures = [r, s, f].filter((x) => x.status === 'rejected').length;
      setFailed(failures);
      if (r.status === 'fulfilled') setRegime(r.value);
      if (s.status === 'fulfilled') setSentiment(s.value);
      if (f.status === 'fulfilled') setFlow(f.value);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [date, tick]);

  const oneLiner = useMemo(() => {
    if (!regime) return '正在汇总市场阶段、大盘情绪与资金流…';
    const byKey = new Map(regime.layers.map((l) => [l.key, l.stage]));
    return `长期 ${byKey.get('long') ?? '—'} · 中期 ${byKey.get('medium') ?? '—'} · 短期 ${byKey.get('short') ?? '—'}`;
  }, [regime]);

  return (
    <div className="flex min-h-full flex-col gap-3">
      <PanoramaHeader
        date={date}
        regime={regime}
        sentiment={sentiment}
        breadth={breadth}
        oneLiner={oneLiner}
        snapshotMissing={snapshotMissing}
        snapshotFrom={snapshotFrom}
        failed={failed}
        loading={loading}
        onRefresh={() => setTick((t) => t + 1)}
      />

      <div className="shrink-0">
        <IndexTape
          indices={indices}
          loading={pageLoading}
          note={
            snapshotMissing
              ? `该日期无行情快照：指数带仅支持 ${snapshotFrom ?? '快照日'} 起；阶段/情绪/资金流仍按该日期回看。`
              : undefined
          }
        />
      </div>

      <div className="grid min-h-[520px] flex-1 grid-cols-1 gap-3 lg:grid-cols-12">
        <div className="min-h-0 lg:col-span-6 xl:col-span-5">
          <StageSummaryCard
            regime={regime}
            loading={loading}
            onOpen={() => onNavigate('regime')}
          />
        </div>
        <div className="min-h-0 lg:col-span-6 xl:col-span-4">
          <SentimentSummaryCard
            sentiment={sentiment}
            breadth={breadth}
            loading={loading}
            onOpen={() => onNavigate('sentiment')}
          />
        </div>
        <div className="min-h-0 lg:col-span-12 xl:col-span-3">
          <FlowSummaryCard
            sentiment={sentiment}
            flow={flow}
            loading={loading}
            onOpen={() => onNavigate('flow-panorama')}
          />
        </div>
      </div>

      <div className="shrink-0">
        {snapshotMissing ? (
          <div className="rounded-2xl border border-dashed border-amber-200 bg-amber-50/50 px-4 py-5 text-[11px] leading-relaxed text-amber-700">
            该日期无板块热力快照，方块图仅支持有行情快照的日期（{snapshotFrom ?? '快照日'} 起）。
            阶段、情绪、资金流三张卡仍按所选日期回看；切回快照日即可查看板块方块图与个股下钻。
          </div>
        ) : (
          <div className="h-[400px] [@media(min-height:1000px)]:h-[460px]">
            <HeatmapDrillCard
              topItems={heatmap}
              date={date}
              onStockClick={(symbol) => onOpenStock?.(symbol)}
            />
          </div>
        )}
      </div>
    </div>
  );
}
