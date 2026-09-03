/** 港股市场分析主页面 —— 恒指脉搏 / 南向资金 / CCASS 席位 / 估值主题 / 轮动比价
 *
 * 独立市场目录（features/market-analysis-hk/），共享 A 股市场分析的热力图组件
 * （跨市场数据形状一致，见 features/market-analysis/components/ShenwanHeatmapChart）。
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  Sparkles, Clock, RefreshCw, Zap, Activity, Waves, Building2, Coins, Layers, Landmark, Briefcase,
} from 'lucide-react';
import { message } from 'antd';
import { ShenwanHeatmapChart, ShenwanSectorItem } from '../../market-analysis/components/ShenwanHeatmapChart';
import { HkIndexCards } from '../components/HkIndexCards';
import { HkBreadthCard } from '../components/HkBreadthCard';
import { ProfitLeadersCard } from '../components/HkProfitLeaders';
import { HkSouthPanel } from '../components/HkSouthPanel';
import { HkCcassPanel } from '../components/HkCcassPanel';
import { HkValuationPanel } from '../components/HkValuationPanel';
import { HkAhPremiumPanel } from '../components/HkAhPremiumPanel';
import { HkDividendCalendarPanel } from '../components/HkDividendCalendarPanel';
import { HkRotationPanel } from '../components/HkRotationPanel';
import { HkSectorValuationPanel } from '../components/HkSectorValuationPanel';
import { HkAhPanel } from '../components/HkAhPanel';
import { HkInstitutionalPanel } from '../components/HkInstitutionalPanel';
import {
  getBreadth, getHeatmap, getIndicesOverview, getStatus, refreshMarket,
} from '../services/api';
import type { HkBreadthData, HkIndexItem, HkSectorHeatItem } from '../types';

const NAV_TABS = [
  { id: 'panorama', label: '恒指脉搏', icon: Activity },
  { id: 'ccass', label: 'CCASS 席位', icon: Building2 },
  { id: 'south', label: '南向资金', icon: Waves },
  { id: 'valuation', label: '估值主题', icon: Coins },
  { id: 'rotation', label: '轮动 & AH', icon: Layers },
  { id: 'institutional', label: '机构持仓', icon: Briefcase },
];

export const MarketAnalysisHkPage: React.FC = () => {
  const [activeTab, setActiveTab] = useState('panorama'); // 默认进入恒指脉搏
  const [indices, setIndices] = useState<HkIndexItem[]>([]);
  const [breadth, setBreadth] = useState<HkBreadthData | null>(null);
  const [heatmap, setHeatmap] = useState<HkSectorHeatItem[]>([]);
  const [dataDate, setDataDate] = useState('');
  const [updateTime, setUpdateTime] = useState('');
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const loadCore = useCallback(async () => {
    setLoading(true);
    try {
      const [idx, bd, hm] = await Promise.all([
        getIndicesOverview(), getBreadth(), getHeatmap(40),
      ]);
      setIndices(idx);
      setBreadth(bd);
      setHeatmap(hm);
      setDataDate(bd.trade_date || idx[0]?.trade_date || '');
      setUpdateTime(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
    } catch (e) {
      message.error(`港股市场分析数据加载失败: ${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadCore().catch(() => undefined);
    getStatus()
      .then((st) => {
        if (!st.available) message.warning('港股数据目录不可用，请检查数据管理页');
      })
      .catch(() => undefined);
  }, [loadCore]);

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      const res = await refreshMarket();
      message.success(`${res.message}（${res.trade_date}）`);
      await loadCore();
    } catch (e) {
      message.error(`刷新失败: ${(e as Error).message}`);
    } finally {
      setRefreshing(false);
    }
  };

  const heatmapItems: ShenwanSectorItem[] = heatmap.map((h) => ({
    name: h.name,
    value: h.value,
    pct_change: h.pct_change,
    leader: h.leader,
    leader_pct: h.leader_pct,
  }));

  return (
    <div className="w-full h-full overflow-y-auto bg-slate-50/60 px-5 pt-4 pb-28 flex flex-col gap-2.5 font-sans">
      {/* Banner 顶栏 */}
      <div className="relative rounded-2xl bg-gradient-to-r from-indigo-100/90 via-sky-50/80 to-indigo-50/90 text-slate-900 px-5 py-2.5 shadow-xs border border-indigo-200/60 flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          {/* 市场切换在「大盘分析」顶部（A股/港股切换器），此处仅作标识 */}
          <span className="px-2.5 py-1 rounded-full bg-white/80 text-slate-500 border border-indigo-200/70 text-[11px] font-extrabold whitespace-nowrap" title="顶部市场切换器：大盘分析页 → A股/港股">
            市场: 港股
          </span>
          <span className="px-3 py-0.5 rounded-full bg-indigo-600/10 text-indigo-700 border border-indigo-200 text-xs font-extrabold font-mono flex items-center gap-1.5 shadow-2xs whitespace-nowrap">
            <Sparkles className="w-3.5 h-3.5 text-indigo-600" />
            <span>QuantHK 数据引擎</span>
          </span>
          <h1 className="text-base font-extrabold tracking-tight bg-gradient-to-r from-indigo-950 via-sky-900 to-slate-900 bg-clip-text text-transparent whitespace-nowrap">
            港股市场多维分析与资金穿透
          </h1>
        </div>

        <div className="flex items-center gap-2.5 flex-shrink-0">
          {dataDate && (
            <span title="行情数据对应的最新交易日" className="hidden md:flex items-center gap-1.5 px-3 py-1 rounded-full bg-white/80 text-slate-600 border border-indigo-200/70 text-[11px] font-extrabold font-mono whitespace-nowrap shadow-2xs">
              <Clock className="w-3 h-3 text-indigo-500" />
              <span>数据日期:</span>
              <span className="text-indigo-700">{dataDate}</span>
            </span>
          )}
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            className={`flex items-center gap-1.5 px-4 py-1.5 rounded-full text-xs font-extrabold shadow-md transition-all duration-200 whitespace-nowrap cursor-pointer ${
              refreshing
                ? 'bg-indigo-400 text-white cursor-wait opacity-80'
                : 'bg-gradient-to-r from-indigo-600 via-sky-600 to-indigo-700 hover:from-indigo-500 hover:to-sky-500 active:scale-95 text-white shadow-indigo-600/30'
            }`}
            title="从本地 QuantHK 重新读取最新数据并刷新全部分析"
          >
            {refreshing ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Zap className="w-3.5 h-3.5 text-amber-300 fill-amber-300" />}
            <span>{refreshing ? '刷新中…' : '刷新分析'}</span>
          </button>
        </div>
      </div>

      {/* 恒生四大指数 */}
      <HkIndexCards indices={indices} loading={loading} />

      {/* Tab 导航 */}
      <div className="flex items-center justify-between border-b border-indigo-100/80 pb-1 pt-0.5">
        <div className="flex items-center gap-2 overflow-x-auto p-1">
          {NAV_TABS.map((tab) => {
            const Icon = tab.icon;
            const isActive = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2 px-5 py-2 rounded-full text-xs font-extrabold transition-all duration-200 whitespace-nowrap ${
                  isActive
                    ? 'bg-indigo-600 text-white shadow-lg shadow-indigo-600/30 scale-[1.02]'
                    : 'bg-white/90 text-slate-600 hover:text-slate-900 hover:bg-slate-100 border border-slate-200/80 shadow-2xs hover:shadow-xs'
                }`}
              >
                <Icon className="w-3.5 h-3.5" />
                <span>{tab.label}</span>
              </button>
            );
          })}
        </div>
        <span className="text-[11px] text-slate-400 font-mono hidden sm:inline-block">
          数据更新于: {updateTime || '刚刚'}
        </span>
      </div>

      {/* 恒指脉搏：温度计 + 赚钱效应 Top10（左 1/3）+ 行业热力图（右 2/3） */}
      {activeTab === 'panorama' && (
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-2.5 items-start">
          <div className="xl:col-span-1 flex flex-col gap-2.5">
            <HkBreadthCard breadth={breadth} loading={loading} />
            <ProfitLeadersCard />
          </div>
          <div className="xl:col-span-2 bg-white/90 backdrop-blur-md rounded-2xl p-4 border border-slate-200/80 shadow-sm flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-extrabold text-slate-800 flex items-center gap-1.5">
                <Landmark className="w-3.5 h-3.5 text-purple-600" />
                <span>恒生行业热力图（平均涨幅 / 成交额 / 领涨龙头）</span>
              </h3>
              <span className="text-[10px] font-mono text-slate-400">
                {heatmap.length} 个行业
              </span>
            </div>
            {heatmap.length > 0 ? (
              <ShenwanHeatmapChart data={heatmapItems} height={560} />
            ) : (
              <div className="py-8 text-center text-xs text-slate-400">热力图加载中…</div>
            )}
          </div>
        </div>
      )}

      {activeTab === 'south' && <HkSouthPanel />}
      {activeTab === 'ccass' && <HkCcassPanel />}
      {activeTab === 'valuation' && (
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-2.5 items-start">
          <div className="xl:col-span-1">
            <HkValuationPanel />
          </div>
          <div className="xl:col-span-1">
            <HkAhPremiumPanel />
          </div>
          <div className="xl:col-span-1">
            <HkDividendCalendarPanel />
          </div>
        </div>
      )}
      {activeTab === 'rotation' && (
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-2.5 items-stretch">
          <div className="xl:col-span-1 h-full">
            <HkRotationPanel />
          </div>
          <div className="xl:col-span-1 h-full">
            <HkSectorValuationPanel />
          </div>
          <div className="xl:col-span-1 h-full">
            <HkAhPanel />
          </div>
        </div>
      )}
      {activeTab === 'institutional' && <HkInstitutionalPanel />}
    </div>
  );
};