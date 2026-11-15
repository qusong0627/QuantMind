/** 期货市场分析主页面 —— 行情总览 / 板块热力 / 期限结构 / 持仓分析 / 仓单库存 / 内外盘基差
 *
 * 独立市场目录（features/market-analysis-futures/），共享三市场市场分析的热力图
 * 组件（features/market-analysis-shared/SectorHeatmapChart）与 UI 小组件。
 *
 * 口径提示（页面各处如实标注，勿用股票习惯误读）：
 * - 主力连续（RB0 等）含换月跳空；名义额 = 价 × 量仓 × 乘数（非真实资金流水）
 * - 国际品种/上金所无量仓数据 → 「—」，不是 0
 * - 市场切换在「大盘分析」顶部切换器（选期货 → 本页）
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  Activity, Clock, RefreshCw, Zap, Sparkles, Flame, Telescope, TrendingUp, Scale, Package,
  ArrowLeftRight,
} from 'lucide-react';
import { message } from 'antd';
import { FuturesStatCards } from '../components/FuturesStatCards';
import { FuturesOverviewPanel } from '../components/FuturesOverviewPanel';
import { FuturesHeatmapPanel } from '../components/FuturesHeatmapPanel';
import { FuturesOutlookPanel } from '../components/FuturesOutlookPanel';
import { FuturesTermPanel } from '../components/FuturesTermPanel';
import { FuturesPositionsPanel } from '../components/FuturesPositionsPanel';
import { FuturesInventoryPanel } from '../components/FuturesInventoryPanel';
import { FuturesSpreadPanel } from '../components/FuturesSpreadPanel';
import { getOverview, getStatus, refreshMarket } from '../services/api';
import type { FuturesFeedStatus, FuturesOverview } from '../types';
import { MarketSelector } from '../../../components/layout/MarketSelector';

const NAV_TABS = [
  { id: 'panorama', label: '行情总览', icon: Activity },
  { id: 'heatmap', label: '板块热力', icon: Flame },
  { id: 'outlook', label: '前瞻视角', icon: Telescope },
  { id: 'term', label: '期限结构', icon: TrendingUp },
  { id: 'positions', label: '持仓分析', icon: Scale },
  { id: 'inventory', label: '仓单库存', icon: Package },
  { id: 'spread', label: '内外盘基差', icon: ArrowLeftRight },
];

/** 默认展示品种（黄金流动性好、合约月份齐全；不在期限榜时由面板兜底） */
const DEFAULT_STEM = 'AU';

export const MarketAnalysisFuturesPage: React.FC = () => {
  const [activeTab, setActiveTab] = useState('panorama');
  const [overview, setOverview] = useState<FuturesOverview | null>(null);
  const [status, setStatus] = useState<FuturesFeedStatus | null>(null);
  const [stem, setStem] = useState(DEFAULT_STEM);
  const [outlookSector, setOutlookSector] = useState<string | null>(null);
  const [dataDate, setDataDate] = useState('');
  const [updateTime, setUpdateTime] = useState('');
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  const loadCore = useCallback(async () => {
    setLoading(true);
    try {
      const [ov, st] = await Promise.all([getOverview(), getStatus()]);
      setOverview(ov);
      setStatus(st);
      setDataDate(ov.trade_date || st.kline_latest_full || st.kline_latest || '');
      setUpdateTime(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
      if (!st.available) message.warning('QuantFutures 数据目录不可用，请检查数据管理页');
    } catch (e) {
      message.error(`期货市场分析数据加载失败: ${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadCore().catch(() => undefined);
  }, [loadCore]);

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      const res = await refreshMarket();
      message.success(`${res.message}（${res.trade_date}）`);
      await loadCore();
      setReloadKey((k) => k + 1); // 各 Tab 面板跟随重取
    } catch (e) {
      message.error(`刷新失败: ${(e as Error).message}`);
    } finally {
      setRefreshing(false);
    }
  };

  // 热力图点击品种 → 切到期限结构 Tab 并载入该品种
  const handlePickStem = (s: string) => {
    setStem(s);
    setActiveTab('term');
  };

  // 板块统计点击 → 切到前瞻视角并带板块过滤（从"看异动"到"看因子"闭环）
  const handlePickSector = (sector: string) => {
    setOutlookSector(sector);
    setActiveTab('outlook');
  };

  return (
    <div className="w-full h-full overflow-y-auto bg-slate-50/60 px-5 pt-4 pb-28 flex flex-col gap-2.5 font-sans">
      {/* Banner 顶栏 */}
      <div className="relative rounded-2xl bg-gradient-to-r from-amber-100/90 via-orange-50/80 to-amber-50/90 text-slate-900 px-5 py-2.5 shadow-xs border border-amber-200/60 flex items-center justify-between gap-4">
        <div className="flex items-center gap-3 min-w-0">
          {/* 市场切换在「大盘分析」顶部（A股/港股/美股/期货切换器），此处仅作标识 */}
          <span
            className="px-2.5 py-1 rounded-full bg-white/80 text-slate-500 border border-amber-200/70 text-[11px] font-extrabold whitespace-nowrap"
            title="顶部市场切换器：大盘分析页 → 选择「期货」"
          >
            市场: 期货
          </span>
          <span className="px-3 py-0.5 rounded-full bg-amber-600/10 text-amber-700 border border-amber-200 text-xs font-extrabold font-mono flex items-center gap-1.5 shadow-2xs whitespace-nowrap">
            <Sparkles className="w-3.5 h-3.5 text-amber-600" />
            <span>QuantFutures 数据引擎</span>
          </span>
          <h1 className="text-base font-extrabold tracking-tight bg-gradient-to-r from-amber-950 via-orange-900 to-slate-900 bg-clip-text text-transparent whitespace-nowrap truncate">
            期货市场全景 · 前瞻因子与期限结构透视
          </h1>
          <span className="hidden lg:inline text-[10px] text-slate-400 font-mono whitespace-nowrap">
            国内主力连续 + 国际品种 + 上金所现货
          </span>
        </div>

        <div className="flex items-center gap-2.5 flex-shrink-0">
          <MarketSelector />
          {dataDate && (
            <span
              title="行情数据对应的最新交易日（完整交易日以国内品种齐全的分区为准）"
              className="hidden md:flex items-center gap-1.5 px-3 py-1 rounded-full bg-white/80 text-slate-600 border border-amber-200/70 text-[11px] font-extrabold font-mono whitespace-nowrap shadow-2xs"
            >
              <Clock className="w-3 h-3 text-amber-500" />
              <span>数据日期:</span>
              <span className="text-amber-700">{dataDate}</span>
            </span>
          )}
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            className={`flex items-center gap-1.5 px-4 py-1.5 rounded-full text-xs font-extrabold shadow-md transition-all duration-200 whitespace-nowrap cursor-pointer ${
              refreshing
                ? 'bg-amber-400 text-white cursor-wait opacity-80'
                : 'bg-gradient-to-r from-amber-600 via-orange-600 to-amber-700 hover:from-amber-500 hover:to-orange-500 active:scale-95 text-white shadow-amber-600/30'
            }`}
            title="从本地 QuantFutures 重新读取最新数据并清缓存重算全部分析"
          >
            {refreshing ? (
              <RefreshCw className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Zap className="w-3.5 h-3.5 text-amber-100 fill-amber-100" />
            )}
            <span>{refreshing ? '刷新中…' : '刷新分析'}</span>
          </button>
        </div>
      </div>

      {/* 三层市场统计卡 */}
      <FuturesStatCards overview={overview} />

      {/* Tab 导航 */}
      <div className="flex items-center justify-between border-b border-amber-100/80 pb-1 pt-0.5">
        <div className="flex items-center gap-2 overflow-x-auto p-1">
          {NAV_TABS.map((tab) => {
            const Icon = tab.icon;
            const isActive = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2 px-5 py-2 rounded-full text-xs font-extrabold transition-all duration-200 whitespace-nowrap cursor-pointer ${
                  isActive
                    ? 'bg-amber-600 text-white shadow-lg shadow-amber-600/30 scale-[1.02]'
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
          数据更新于: {updateTime || (loading ? '加载中…' : '刚刚')}
        </span>
      </div>

      {activeTab === 'panorama' && (
        <FuturesOverviewPanel overview={overview} status={status} reloadKey={reloadKey} />
      )}
      {activeTab === 'heatmap' && (
        <FuturesHeatmapPanel
          reloadKey={reloadKey}
          onPickStem={handlePickStem}
          onPickSector={handlePickSector}
        />
      )}
      {activeTab === 'outlook' && (
        <FuturesOutlookPanel
          sector={outlookSector}
          onSectorChange={setOutlookSector}
          reloadKey={reloadKey}
        />
      )}
      {activeTab === 'term' && (
        <FuturesTermPanel stem={stem} onStemChange={setStem} reloadKey={reloadKey} />
      )}
      {activeTab === 'positions' && <FuturesPositionsPanel reloadKey={reloadKey} />}
      {activeTab === 'inventory' && <FuturesInventoryPanel reloadKey={reloadKey} />}
      {activeTab === 'spread' && <FuturesSpreadPanel reloadKey={reloadKey} />}
    </div>
  );
};
