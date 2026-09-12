/** 美股市场分析主页面 —— 大盘脉搏 / 市场宽度 / 板块轮动 / 财报季 / 分析师 / 筹码 / 估值
 *
 * 独立市场目录（features/market-analysis-us/），跨市场共享组件在
 * features/market-analysis-shared/（热力矩形图 + UI 小组件）。
 *
 * 与港股/A 股模块的口径差异（页面顶部与各面板均有标注）：
 * - 标的池为标普500 + 纳指补充约 517 只，不是全市场
 * - 日线为未复权原始价；指数数据无 VIX、无 ETF
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  Sparkles, Clock, RefreshCw, Zap, Activity, Layers, FileText, Users, Coins, BarChart3, Wallet,
} from 'lucide-react';
import { message } from 'antd';
import { SectorHeatmapChart, type SectorHeatmapItem } from '../../market-analysis-shared/SectorHeatmapChart';
import { UsIndexCards } from '../components/UsIndexCards';
import { UsBreadthCard } from '../components/UsBreadthCard';
import { UsProfitLeaders } from '../components/UsProfitLeaders';
import { UsBreadthPanel } from '../components/UsBreadthPanel';
import { UsBreadthHighlightsPanel } from '../components/UsBreadthHighlightsPanel';
import { UsSectorRotationPanel } from '../components/UsSectorRotationPanel';
import { UsSectorValuationPanel } from '../components/UsSectorValuationPanel';
import { UsEarningsPanel } from '../components/UsEarningsPanel';
import { UsAnalystPanel } from '../components/UsAnalystPanel';
import { UsHoldingsPanel } from '../components/UsHoldingsPanel';
import { UsValuationPanel } from '../components/UsValuationPanel';
import {
  getBreadth, getHeatmap, getIndicesOverview, getStatus, refreshMarket,
} from '../services/api';
import type { UsBreadthData, UsIndexItem, UsSectorHeatItem } from '../types';

const NAV_TABS = [
  { id: 'panorama', label: '大盘脉搏', icon: Activity },
  { id: 'breadth', label: '市场宽度', icon: BarChart3 },
  { id: 'rotation', label: '板块轮动', icon: Layers },
  { id: 'earnings', label: '财报季', icon: FileText },
  { id: 'analysts', label: '分析师动向', icon: Users },
  { id: 'holdings', label: '资金与筹码', icon: Wallet },
  { id: 'valuation', label: '估值主题', icon: Coins },
];

export const MarketAnalysisUsPage: React.FC = () => {
  const [activeTab, setActiveTab] = useState('panorama');
  const [indices, setIndices] = useState<UsIndexItem[]>([]);
  const [breadth, setBreadth] = useState<UsBreadthData | null>(null);
  const [heatmap, setHeatmap] = useState<UsSectorHeatItem[]>([]);
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
      setDataDate(bd.trade_date || '');
      setUpdateTime(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
    } catch (e) {
      message.error(`美股市场分析数据加载失败: ${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadCore().catch(() => undefined);
    getStatus()
      .then((st) => {
        if (!st.available) message.warning('美股数据目录不可用，请检查数据管理页');
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

  const heatmapItems: SectorHeatmapItem[] = heatmap.map((h) => ({
    name: h.name,
    value: h.value,
    pct_change: h.pct_change,
    leader: h.leader,
    leader_pct: h.leader_pct,
  }));

  return (
    <div className="w-full h-full overflow-y-auto bg-slate-50/60 px-5 pt-4 pb-28 flex flex-col gap-2.5 font-sans">
      {/* Banner 顶栏 */}
      <div className="relative rounded-2xl bg-gradient-to-r from-blue-100/90 via-sky-50/80 to-blue-50/90 text-slate-900 px-5 py-2.5 shadow-xs border border-blue-200/60 flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <span className="px-2.5 py-1 rounded-full bg-white/80 text-slate-500 border border-blue-200/70 text-[11px] font-extrabold whitespace-nowrap" title="顶部市场切换器：市场分析页 → 美股">
            市场: 美股
          </span>
          <span className="px-3 py-0.5 rounded-full bg-blue-600/10 text-blue-700 border border-blue-200 text-xs font-extrabold font-mono flex items-center gap-1.5 shadow-2xs whitespace-nowrap">
            <Sparkles className="w-3.5 h-3.5 text-blue-600" />
            <span>QuantUS 数据引擎</span>
          </span>
          <h1 className="text-base font-extrabold tracking-tight bg-gradient-to-r from-blue-950 via-sky-900 to-slate-900 bg-clip-text text-transparent whitespace-nowrap">
            美股市场多维分析与筹码穿透
          </h1>
        </div>

        <div className="flex items-center gap-2.5 flex-shrink-0">
          <span className="hidden lg:inline-block text-[10px] text-slate-400 font-mono whitespace-nowrap" title="本模块只覆盖标普500 + 纳指补充的标的池，不是全市场">
            标的池: 标普500 + 纳指补充
          </span>
          {dataDate && (
            <span title="个股行情最新交易日（指数分区可能滞后，各面板单独标注）" className="hidden md:flex items-center gap-1.5 px-3 py-1 rounded-full bg-white/80 text-slate-600 border border-blue-200/70 text-[11px] font-extrabold font-mono whitespace-nowrap shadow-2xs">
              <Clock className="w-3 h-3 text-blue-500" />
              <span>个股数据日期:</span>
              <span className="text-blue-700">{dataDate}</span>
            </span>
          )}
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            className={`flex items-center gap-1.5 px-4 py-1.5 rounded-full text-xs font-extrabold shadow-md transition-all duration-200 whitespace-nowrap cursor-pointer ${
              refreshing
                ? 'bg-blue-400 text-white cursor-wait opacity-80'
                : 'bg-gradient-to-r from-blue-600 via-sky-600 to-blue-700 hover:from-blue-500 hover:to-sky-500 active:scale-95 text-white shadow-blue-600/30'
            }`}
            title="从本地 QuantUS 重新读取最新数据并刷新全部分析"
          >
            {refreshing ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Zap className="w-3.5 h-3.5 text-amber-300 fill-amber-300" />}
            <span>{refreshing ? '刷新中…' : '刷新分析'}</span>
          </button>
        </div>
      </div>

      {/* 五大指数 */}
      <UsIndexCards indices={indices} loading={loading} />

      {/* Tab 导航 */}
      <div className="flex items-center justify-between border-b border-blue-100/80 pb-1 pt-0.5">
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
                    ? 'bg-blue-600 text-white shadow-lg shadow-blue-600/30 scale-[1.02]'
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

      {/* 大盘脉搏：温度计 + 赚钱效应（左 1/3）+ 板块热力图（右 2/3） */}
      {activeTab === 'panorama' && (
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-2.5 items-start">
          <div className="xl:col-span-1 flex flex-col gap-2.5">
            <UsBreadthCard breadth={breadth} loading={loading} />
            <UsProfitLeaders />
          </div>
          <div className="xl:col-span-2 bg-white/90 backdrop-blur-md rounded-2xl p-4 border border-slate-200/80 shadow-sm flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-extrabold text-slate-800 flex items-center gap-1.5">
                <Layers className="w-3.5 h-3.5 text-blue-600" />
                <span>GICS 板块热力图（中位涨幅 / 成交额 / 领涨龙头）</span>
              </h3>
              <span className="text-[10px] font-mono text-slate-400">
                {heatmap.length} 个板块
              </span>
            </div>
            {heatmap.length > 0 ? (
              <SectorHeatmapChart data={heatmapItems} height={560} valueLabel="成交额权重" />
            ) : (
              <div className="py-8 text-center text-xs text-slate-400">热力图加载中…</div>
            )}
          </div>
        </div>
      )}

      {activeTab === 'breadth' && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-2.5 items-start">
          <UsBreadthPanel />
          <UsBreadthHighlightsPanel />
        </div>
      )}
      {activeTab === 'rotation' && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-2.5 items-start">
          <UsSectorRotationPanel />
          <UsSectorValuationPanel />
        </div>
      )}
      {activeTab === 'earnings' && <UsEarningsPanel />}
      {activeTab === 'analysts' && <UsAnalystPanel />}
      {activeTab === 'holdings' && <UsHoldingsPanel />}
      {activeTab === 'valuation' && <UsValuationPanel />}
    </div>
  );
};
