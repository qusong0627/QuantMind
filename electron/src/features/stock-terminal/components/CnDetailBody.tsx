/** A 股个股终端右侧详情体：9 个 Tab（概况/财务/估值/筹码/融资/形态/股东/资讯/L2）——
 * T-FE-02 收尾：简单模式收起机构级页签（筹码/融资/形态/股东/L2），专业模式全量。 */

import { useEffect, useState } from 'react';
import type { StockProfile } from '../types';
import { OverviewTab } from './OverviewTab';
import { FinancialsTab, ValuationTab, ChipFlowTab, MarginTab, SentimentTab, HoldersTab } from './tabs/P2Tabs';
import { NewsTab } from './tabs/NewsTab';
import { L2FeatureCard } from './L2FeatureCard';
import { useUiMode } from '../../shared/useUiMode';
import { fallbackDetailTab, visibleDetailTabs, type DetailTabDef } from '../../stock-terminal-shared/utils';

type DetailTab = 'overview' | 'financials' | 'valuation' | 'chipflow' | 'margin' | 'sentiment' | 'holders' | 'news' | 'l2';

const DETAIL_TABS: DetailTabDef<DetailTab>[] = [
  { id: 'overview', label: '概况' },
  { id: 'financials', label: '财务' },
  { id: 'valuation', label: '估值' },
  { id: 'chipflow', label: '筹码', proOnly: true },
  { id: 'margin', label: '融资', proOnly: true },
  { id: 'sentiment', label: '形态', proOnly: true },
  { id: 'holders', label: '股东', proOnly: true },
  { id: 'news', label: '资讯' },
  { id: 'l2', label: 'L2', proOnly: true },
];

interface Props {
  symbol: string;
  profile: StockProfile | null;
  signalDate?: string;
}

export function CnDetailBody({ symbol, profile, signalDate }: Props) {
  const { isSimple } = useUiMode();
  const [detailTab, setDetailTab] = useState<DetailTab>('overview');
  const tabs = visibleDetailTabs(DETAIL_TABS, isSimple);

  // 切到简单模式时，被收起的页签回落到第一个可见页签（不留空白）
  useEffect(() => {
    const next = fallbackDetailTab(detailTab, DETAIL_TABS, isSimple);
    if (next !== detailTab) setDetailTab(next);
  }, [isSimple, detailTab]);

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="px-3 py-2 border-b border-slate-100 bg-white shrink-0">
        <div className={`grid ${isSimple ? 'grid-cols-4' : 'grid-cols-5'} gap-1.5`}>
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => setDetailTab(t.id)}
              className={`px-2 py-1.5 rounded-full text-[11px] font-bold border transition-colors ${detailTab === t.id ? 'bg-blue-600 text-white border-blue-600 shadow-sm' : 'bg-slate-50 text-slate-600 border-slate-200 hover:bg-white hover:border-slate-300'}`}
            >
              {t.label}
            </button>
          ))}
        </div>
        {isSimple && (
          <div className="text-[10px] text-slate-400 mt-1">
            简单模式已收起机构级页签（筹码/融资/形态/股东/L2）——顶栏可切换专业模式
          </div>
        )}
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-3 pb-16 bg-gray-50/30 custom-scrollbar">
        <div className={detailTab === 'overview' ? '[&>div]:!grid-cols-1 [&>div]:!gap-3' : ''}>
          {detailTab === 'overview' && <OverviewTab profile={profile} />}
          {detailTab === 'financials' && <FinancialsTab symbol={symbol} asof={signalDate} />}
          {detailTab === 'valuation' && <ValuationTab symbol={symbol} asof={signalDate} />}
          {detailTab === 'chipflow' && <ChipFlowTab symbol={symbol} asof={signalDate} />}
          {detailTab === 'margin' && <MarginTab symbol={symbol} asof={signalDate} />}
          {detailTab === 'sentiment' && <SentimentTab symbol={symbol} asof={signalDate} />}
          {detailTab === 'holders' && <HoldersTab symbol={symbol} asof={signalDate} />}
          {detailTab === 'news' && <NewsTab symbol={symbol} />}
          {detailTab === 'l2' && <L2FeatureCard l2={profile?.l2_features ?? null} signalDate={profile?.signal_date ?? null} />}
        </div>
      </div>
    </div>
  );
}
