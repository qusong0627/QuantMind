/** 美股市场分析主页面 —— 大盘脉搏 / 市场宽度 / 板块轮动 / 财报季 / 分析师 / 筹码 / 估值
 *
 * 布局取向：**高信息密度**。首屏要能同时看到「大盘状态 + 哪里热 + 钱流去哪」，
 * 因此卡片 padding 压到 2.5、网格 gap 压到 1.5、榜单行高压到 3px，
 * 一屏塞进更多标的 —— 这是看盘工具的用法，不是仪表盘的用法。
 *
 * 口径差异（页面各面板均有标注）：标的池为标普500 + 纳指补充约 517 只，非全市场；
 * 日线为未复权原始价；指数无 VIX 无 ETF。
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  Sparkles, Clock, RefreshCw, Zap, Activity, Layers, FileText, Users, Coins, BarChart3, Wallet,
} from 'lucide-react';
import { message } from 'antd';
import { SectorHeatmapChart, type SectorHeatmapItem } from '../../market-analysis-shared/SectorHeatmapChart';
import { UsIndexCards } from '../components/UsIndexCards';
import { UsBreadthCard } from '../components/UsBreadthCard';
import { UsHotStocksPanel, UsMarketPulseStrip } from '../components/UsHotStocksPanel';
import { UsSectorFundFlowPanel } from '../components/UsSectorFundFlowPanel';
import { UsMarketDistributionPanel } from '../components/UsMarketDistributionPanel';
import { UsBreadthPanel } from '../components/UsBreadthPanel';
import { UsBreadthHighlightsPanel } from '../components/UsBreadthHighlightsPanel';
import { UsSectorRotationPanel } from '../components/UsSectorRotationPanel';
import { UsSectorValuationPanel } from '../components/UsSectorValuationPanel';
import { UsEarningsPanel } from '../components/UsEarningsPanel';
import { UsAnalystPanel } from '../components/UsAnalystPanel';
import { UsHoldingsPanel } from '../components/UsHoldingsPanel';
import { UsValuationPanel } from '../components/UsValuationPanel';
import {
  getBreadth, getHeatmap, getIndicesOverview, getMarketStats, getStatus, refreshMarket,
} from '../services/api';
import type { UsBreadthData, UsIndexItem, UsMarketStats, UsSectorHeatItem } from '../types';

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
  const [stats, setStats] = useState<UsMarketStats | null>(null);
  const [dataDate, setDataDate] = useState('');
  const [updateTime, setUpdateTime] = useState('');
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const loadCore = useCallback(async () => {
    setLoading(true);
    try {
      const [idx, bd, hm, st] = await Promise.all([
        getIndicesOverview(), getBreadth(), getHeatmap(40), getMarketStats(),
      ]);
      setIndices(idx);
      setBreadth(bd);
      setHeatmap(hm);
      setStats(st);
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
    <div className="w-full h-full overflow-y-auto bg-slate-50/60 px-3 pt-2 pb-20 flex flex-col gap-1.5 font-sans">
      {/* Banner：压到一行，只保留市场标识 + 日期 + 刷新 */}
      <div className="relative rounded-xl bg-gradient-to-r from-blue-100/90 via-sky-50/80 to-blue-50/90 px-3 py-1.5 shadow-xs border border-blue-200/60 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <span className="px-2 py-0.5 rounded-full bg-white/80 text-slate-500 border border-blue-200/70 text-[10px] font-extrabold whitespace-nowrap">
            美股
          </span>
          <span className="px-2 py-0.5 rounded-full bg-blue-600/10 text-blue-700 border border-blue-200 text-[11px] font-extrabold font-mono flex items-center gap-1 whitespace-nowrap">
            <Sparkles className="w-3 h-3 text-blue-600" />
            QuantUS
          </span>
          <h1 className="text-[13px] font-extrabold tracking-tight text-slate-800 whitespace-nowrap">
            美股市场多维分析
          </h1>
          <span className="hidden lg:inline text-[9px] text-slate-400 font-mono whitespace-nowrap" title="本模块只覆盖标普500 + 纳指补充的标的池，不是全市场">
            标的池 517 只 · 非全市场
          </span>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          {dataDate && (
            <span className="hidden md:flex items-center gap-1 px-2 py-0.5 rounded-full bg-white/80 text-slate-600 border border-blue-200/70 text-[10px] font-extrabold font-mono whitespace-nowrap">
              <Clock className="w-3 h-3 text-blue-500" />
              <span>{dataDate}</span>
            </span>
          )}
          <span className="hidden xl:inline text-[9px] text-slate-400 font-mono whitespace-nowrap">
            {updateTime}
          </span>
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            className={`flex items-center gap-1 px-3 py-1 rounded-full text-[11px] font-extrabold shadow-sm transition-all whitespace-nowrap cursor-pointer ${
              refreshing
                ? 'bg-blue-400 text-white cursor-wait opacity-80'
                : 'bg-gradient-to-r from-blue-600 to-sky-600 hover:from-blue-500 hover:to-sky-500 active:scale-95 text-white'
            }`}
            title="从本地 QuantUS 重新读取最新数据并刷新全部分析"
          >
            {refreshing ? <RefreshCw className="w-3 h-3 animate-spin" /> : <Zap className="w-3 h-3 text-amber-300 fill-amber-300" />}
            <span>{refreshing ? '刷新中' : '刷新'}</span>
          </button>
        </div>
      </div>

      {/* 五大指数（紧凑，含量比） */}
      <UsIndexCards indices={indices} loading={loading} />

      {/* 市场活力条 */}
      <UsMarketPulseStrip stats={stats} />

      {/* Tab 导航 */}
      <div className="flex items-center gap-1 flex-wrap">
        {NAV_TABS.map((tab) => {
          const Icon = tab.icon;
          const isActive = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-1.5 px-3.5 py-1.5 rounded-full text-[11px] font-extrabold transition-all ${
                isActive
                  ? 'bg-blue-600 text-white shadow-md shadow-blue-600/30'
                  : 'bg-white/90 text-slate-600 hover:text-slate-900 hover:bg-slate-100 border border-slate-200/80'
              }`}
            >
              <Icon className="w-3 h-3" />
              <span>{tab.label}</span>
            </button>
          );
        })}
      </div>

      {/* Tab1 大盘脉搏：首屏 = 哪里热 + 钱去哪 + 板块全景
          布局约定：同排**等高**（网格默认 stretch，不用 items-start），
          每列最后一张卡用 flex-1/h-full 吃掉行高差，卡内内容弹性填充 ——
          避免各卡片按自身内容高导致底边参差。 */}
      {activeTab === 'panorama' && (
        <>
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-1.5 shrink-0 xl:h-[560px]">
            <div className="xl:col-span-2 h-full min-h-0">
              <UsHotStocksPanel limit={18} className="h-full" />
            </div>
            <div className="xl:col-span-1 h-full min-h-0 flex flex-col gap-1.5">
              <UsBreadthCard breadth={breadth} loading={loading} />
              <UsMarketDistributionPanel className="flex-1 min-h-0" />
            </div>
          </div>
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-1.5 shrink-0 xl:h-[460px]">
            <div className="xl:col-span-2 h-full min-h-0 bg-white/90 backdrop-blur-md rounded-2xl p-2.5 border border-slate-200/80 shadow-sm flex flex-col gap-1.5">
              <div className="flex items-center justify-between">
                <h3 className="text-[11px] font-extrabold text-slate-800 flex items-center gap-1.5">
                  <Layers className="w-3.5 h-3.5 text-blue-600" />
                  <span>GICS 板块热力图（中位涨幅 / 成交额 / 领涨龙头）</span>
                </h3>
                <span className="text-[9px] font-mono text-slate-400">{heatmap.length} 个板块</span>
              </div>
              {heatmap.length > 0 ? (
                <div className="flex-1 min-h-0">
                  <SectorHeatmapChart data={heatmapItems} height="100%" valueLabel="成交额权重" />
                </div>
              ) : (
                <div className="py-6 text-center text-[11px] text-slate-400">热力图加载中…</div>
              )}
            </div>
            <div className="xl:col-span-1 h-full">
              <UsSectorFundFlowPanel limit={14} className="h-full" />
            </div>
          </div>
        </>
      )}

      {activeTab === 'breadth' && (
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-1.5 shrink-0 xl:h-[620px]">
          <div className="xl:col-span-2 h-full min-h-0">
            <UsBreadthPanel className="h-full" />
          </div>
          <div className="xl:col-span-1 h-full min-h-0 flex flex-col gap-1.5">
            <UsMarketDistributionPanel />
            <UsBreadthHighlightsPanel className="flex-1 min-h-0" />
          </div>
        </div>
      )}
      {activeTab === 'rotation' && (
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-1.5 shrink-0 xl:h-[620px]">
          <div className="xl:col-span-2 h-full min-h-0">
            <UsSectorRotationPanel className="h-full" />
          </div>
          <div className="xl:col-span-1 h-full min-h-0 flex flex-col gap-1.5">
            <UsMarketDistributionPanel />
            <UsSectorValuationPanel className="flex-1 min-h-0" />
          </div>
        </div>
      )}
      {activeTab === 'earnings' && <UsEarningsPanel />}
      {activeTab === 'analysts' && <UsAnalystPanel />}
      {activeTab === 'holdings' && <UsHoldingsPanel />}
      {activeTab === 'valuation' && <UsValuationPanel />}
    </div>
  );
};
