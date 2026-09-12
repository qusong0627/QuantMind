/** A 股个股终端右侧详情体：9 个 Tab（概况/财务/估值/筹码/融资/形态/股东/资讯/L2） */

import { useState } from 'react';
import type { StockProfile } from '../types';
import { OverviewTab } from './OverviewTab';
import { FinancialsTab, ValuationTab, ChipFlowTab, MarginTab, SentimentTab, HoldersTab } from './tabs/P2Tabs';
import { NewsTab } from './tabs/NewsTab';
import { L2FeatureCard } from './L2FeatureCard';

type DetailTab = 'overview' | 'financials' | 'valuation' | 'chipflow' | 'margin' | 'sentiment' | 'holders' | 'news' | 'l2';

const DETAIL_TABS: { id: DetailTab; label: string }[] = [
  { id: 'overview', label: '概况' },
  { id: 'financials', label: '财务' },
  { id: 'valuation', label: '估值' },
  { id: 'chipflow', label: '筹码' },
  { id: 'margin', label: '融资' },
  { id: 'sentiment', label: '形态' },
  { id: 'holders', label: '股东' },
  { id: 'news', label: '资讯' },
  { id: 'l2', label: 'L2' },
];

interface Props {
  symbol: string;
  profile: StockProfile | null;
  signalDate?: string;
}

export function CnDetailBody({ symbol, profile, signalDate }: Props) {
  const [detailTab, setDetailTab] = useState<DetailTab>('overview');

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="px-3 py-2 border-b border-slate-100 bg-white shrink-0">
        <div className="grid grid-cols-5 gap-1.5">
          {DETAIL_TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setDetailTab(t.id)}
              className={`px-2 py-1.5 rounded-full text-[11px] font-bold border transition-colors ${detailTab === t.id ? 'bg-blue-600 text-white border-blue-600 shadow-sm' : 'bg-slate-50 text-slate-600 border-slate-200 hover:bg-white hover:border-slate-300'}`}
            >
              {t.label}
            </button>
          ))}
        </div>
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
