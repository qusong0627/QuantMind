import React, { useState, useEffect, useMemo } from 'react';
import { Sparkles, Search, Activity, Layers, Network, TrendingUp, BarChart3, Clock, RefreshCw, Zap } from 'lucide-react';
import { Input, DatePicker, Spin, message } from 'antd';
import dayjs from 'dayjs';
import { BroadMarketHeader, IndexItem } from '../components/BroadMarketHeader';
import { ShenwanHeatmapChart } from '../components/ShenwanHeatmapChart';
import { CapitalFlowSankeyChart } from '../components/CapitalFlowSankeyChart';
import { StockMoneyFlowTable, StockMoneyFlowItem } from '../components/StockMoneyFlowTable';
import { MarketBreadthCard } from '../components/MarketBreadthCard';
import { TagLookupPanel } from '../components/TagLookupPanel';
import { CapitalFlowHorizontalBarChart, FlowItem } from '../components/CapitalFlowHorizontalBarChart';
import { Tag as TagIcon } from 'lucide-react';
import { SERVICE_ENDPOINTS } from '../../../config/services';
import { PAGE_LAYOUT } from '../../../config/pageLayout';

const MARKET_ANALYSIS_API = `${SERVICE_ENDPOINTS.USER_SERVICE}/market-analysis`;

interface MarketBreadthData {
  trade_date: string;
  advance_count: number;
  decline_count: number;
  flat_count: number;
  limit_up_count: number;
  limit_down_count: number;
  total_turnover_yi: number;
  profit_effect: number;
  limit_up_broken_ratio: number;
}

export const MarketAnalysisPage: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [activeTab, setActiveTab] = useState('panorama'); // 默认进入大盘全景看板
  const [searchQuery, setSearchQuery] = useState('');
  const [indices, setIndices] = useState<IndexItem[]>([]);
  const [stockFlows, setStockFlows] = useState<StockMoneyFlowItem[]>([]);
  const [breadth, setBreadth] = useState<MarketBreadthData | null>(null);
  const [dataDate, setDataDate] = useState<string>('');
  const [heatmapData, setHeatmapData] = useState<any[]>([]);
  const [sankeyData, setSankeyData] = useState<{ nodes: any[]; links: any[] } | null>(null);
  const [updateTime, setUpdateTime] = useState<string>('');
  const [treemapData, setTreemapData] = useState<any[]>([]);

  // 🎯 多周期资金流向专属状态
  const [period, setPeriod] = useState<'1d' | '3d' | '5d' | '10d' | '20d'>('5d');
  const [flowDimension, setFlowDimension] = useState<'sector' | 'stock'>('sector');
  const [categoryMode, setCategoryMode] = useState<'shenwan' | 'concept'>('shenwan');
  const [chartViewMode, setChartViewMode] = useState<'bar' | 'treemap'>('bar');
  const [selectedFlowItem, setSelectedFlowItem] = useState<FlowItem | null>(null);
  // 🎯 全局搜索 + 排序口径（Phase 3 迁移自官网 Dashboard）
  const [allStocks, setAllStocks] = useState<StockMoneyFlowItem[]>([]);
  const [focusStock, setFocusStock] = useState<StockMoneyFlowItem | null>(null);
  const [isSearchOpen, setIsSearchOpen] = useState(false);
  const [sortMode, setSortMode] = useState<'absolute' | 'relative'>('absolute');

  // 🎯 快照历史日期（Phase 3 后端 /snapshot/dates）
  const [snapDate, setSnapDate] = useState<string | undefined>();
  const [snapDates, setSnapDates] = useState<string[]>([]);
  // 🎯 手动分析完成后切换为实时模式（GET 接口跳过快照，展示最新交易日数据）
  const [realtime, setRealtime] = useState(false);

  useEffect(() => {
    fetchMarketData();
  }, []);

  // 统一 QS 构造：避免无 date 时出现 &realtime=1 导致 404（/overview&realtime=1）
  const buildQS = (date?: string, rt?: boolean) => {
    const p = new URLSearchParams();
    if (date) p.set('date', date);
    if (rt) p.set('realtime', '1');
    const s = p.toString();
    return s ? `?${s}` : '';
  };

  // 矩形树图数据：随分类模式切换拉取对应热力图
  useEffect(() => {
    if (activeTab !== 'flow-bar' || chartViewMode !== 'treemap') return;
    const token = localStorage.getItem('access_token') || '';
    const qs = buildQS(snapDate, realtime);
    const suffix = qs ? qs.replace('?', '&') : '';
    fetch(`${MARKET_ANALYSIS_API}/heatmap?category=${categoryMode}${suffix}`, { headers: { Authorization: `Bearer ${token}` } })
      .then((res) => (res.ok ? res.json() : null))
      .then((d) => setTreemapData(d?.items && d.items.length > 0 ? d.items : []))
      .catch(() => setTreemapData([]));
  }, [activeTab, chartViewMode, categoryMode, snapDate, realtime]);

  // 加载可用快照日期 + 快照模式；切换日期重取
  useEffect(() => {
    let cancelled = false;
    const token = localStorage.getItem('access_token') || '';
    fetch(`${MARKET_ANALYSIS_API}/snapshot/dates`, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      .then((res) => (res.ok ? res.json() : null))
      .then((d) => {
        if (!cancelled && d && Array.isArray(d.dates)) {
          setSnapDates(d.dates);
          if (!snapDate && d.latest) setSnapDate(undefined); // 默认最新（latest）
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    fetchMarketData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapDate]);

  const fetchMarketData = async (forceRealtime?: boolean) => {
    setLoading(true);
    const token = localStorage.getItem('access_token') || '';
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    const useRealtime = forceRealtime !== undefined ? forceRealtime : realtime;
    const qs = buildQS(snapDate, useRealtime);
    const qsWithAmp = qs ? qs.replace('?', '&') : '';

    try {
      const [resIdx, resStock, resBreadth, resHeatmap, resSankey] = await Promise.all([
        fetch(`${MARKET_ANALYSIS_API}/indices/overview${qs}`, { headers }),
        fetch(`${MARKET_ANALYSIS_API}/money-flow/stocks?limit=20${qsWithAmp}`, { headers }),
        fetch(`${MARKET_ANALYSIS_API}/breadth${qs}`, { headers }),
        fetch(`${MARKET_ANALYSIS_API}/heatmap?category=shenwan${qsWithAmp}`, { headers }),
        fetch(`${MARKET_ANALYSIS_API}/money-flow/sankey${qs}`, { headers }),
      ]);

      if (resIdx.ok) {
        const idxData = await resIdx.json();
        if (Array.isArray(idxData) && idxData.length > 0) {
          setIndices(idxData);
        }
      }
      if (resStock.ok) {
        const stockData = await resStock.json();
        if (Array.isArray(stockData) && stockData.length > 0) {
          setStockFlows(stockData);
        }
      }
      if (resBreadth.ok) {
        const bData = await resBreadth.json();
        setDataDate(bData?.trade_date || '');
        if (bData && bData.total_turnover_yi > 0) {
          setBreadth(bData);
        }
      }
      if (resHeatmap.ok) {
        const hData = await resHeatmap.json();
        if (hData && Array.isArray(hData.items) && hData.items.length > 0) {
          setHeatmapData(hData.items);
        }
      }
      if (resSankey.ok) {
        const sData = await resSankey.json();
        if (sData && sData.nodes && sData.nodes.length > 0) {
          setSankeyData(sData);
        }
      }

      const now = new Date();
      setUpdateTime(now.toTimeString().split(' ')[0]);
    } catch (e) {
      console.warn('后端市场接口连接出现异常，保留现有渲染状态', e);
    } finally {
      setLoading(false);
    }
  };

  // 🎯 全市场个股资金流（供全局搜索本地过滤，不触发服务端计算）
  useEffect(() => {
    let cancelled = false;
    const token = localStorage.getItem('access_token') || '';
    fetch(`${MARKET_ANALYSIS_API}/money-flow/stocks/full${realtime ? '?realtime=1' : ''}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
      .then((res) => (res.ok ? res.json() : null))
      .then((d) => { if (!cancelled && Array.isArray(d)) setAllStocks(d); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [realtime]);

  // 本地过滤搜索结果（名称/代码子串）
  const searchResults = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return [];
    return allStocks
      .filter((s) => s.name?.toLowerCase().includes(q) || s.symbol?.toLowerCase().includes(q))
      .slice(0, 8);
  }, [searchQuery, allStocks]);

  // 个股资金流「筹码四分结构」柱状图
  const renderFourBarChart = (s: StockMoneyFlowItem) => {
    const items = [
      { label: '超大单', key: 'super_large', color: '#e11d48', cls: 'text-rose-700' },
      { label: '大单', key: 'large', color: '#ea580c', cls: 'text-orange-700' },
      { label: '中单', key: 'medium', color: '#d97706', cls: 'text-amber-700' },
      { label: '小单', key: 'small', color: '#059669', cls: 'text-emerald-700' },
    ] as const;
    const maxAbs = Math.max(1, ...items.map((i) => Math.abs((s as any)[i.key] ?? 0)));
    return (
      <div className="flex flex-col gap-2.5">
        {items.map((i) => {
          const v = ((s as any)[i.key] as number) ?? 0;
          const yi = v / 1e8;
          const pct = Math.max(4, (Math.abs(v) / maxAbs) * 100);
          return (
            <div key={i.key} className="flex items-center gap-3">
              <span className={`w-14 text-xs font-bold ${i.cls} flex-shrink-0`}>{i.label}</span>
              <div className="flex-1 h-6 bg-slate-100 rounded-full overflow-hidden">
                <div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, backgroundColor: i.color }} />
              </div>
              <span className={`w-24 text-right font-mono text-xs font-extrabold flex-shrink-0 ${yi >= 0 ? 'text-rose-700' : 'text-emerald-700'}`}>
                {yi >= 0 ? '+' : ''}{yi.toFixed(2)} 亿
              </span>
            </div>
          );
        })}
      </div>
    );
  };

  // 点击个股 → 打开资金流详情(四分结构)并切到个股资金流 tab / 滚动到详情
  const openStockDetail = (item: StockMoneyFlowItem) => {
    setFocusStock(item);
    setActiveTab('stock-flow');
    setTimeout(() => {
      document.getElementById('focus-stock-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 60);
  };

  /** 手动触发读取 QuantDB 数据，SSE 逐步流式推送，边分析边渲染 */
  const handleTriggerAnalysis = async () => {
    setAnalyzing(true);
    const token = localStorage.getItem('access_token') || '';
    const headers = token ? { Authorization: `Bearer ${token}` } : {};

    try {
      const res = await fetch(`${MARKET_ANALYSIS_API}/analyze/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...headers,
        },
      });

      if (!res.ok || !res.body) {
        message.info('已触发市场分析');
        await fetchMarketData(true);
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      const handleEvent = (eventType: string, evt: any) => {
        const data = evt?.data;
        switch (eventType) {
          case 'indices':
            if (Array.isArray(data?.indices) && data.indices.length > 0) {
              setIndices(data.indices);
            }
            break;
          case 'breadth':
            if (data?.breadth && data.breadth.total_turnover_yi > 0) {
              setBreadth(data.breadth);
              setDataDate(data.breadth.trade_date || '');
            }
            break;
          case 'heatmap':
            if (Array.isArray(data?.heatmap) && data.heatmap.length > 0) {
              setHeatmapData(data.heatmap);
            }
            break;
          case 'sankey':
            if (data?.sankey && data.sankey.nodes && data.sankey.nodes.length > 0) {
              setSankeyData(data.sankey);
            }
            break;
          case 'stock_flow':
            if (Array.isArray(data?.stock_flow) && data.stock_flow.length > 0) {
              setStockFlows(data.stock_flow);
            }
            break;
          default:
            break;
        }
        setUpdateTime(new Date().toTimeString().split(' ')[0]);
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE 消息以空行分隔；解析完整块后保留尾部未闭合部分
        const blocks = buffer.split('\n\n');
        buffer = blocks.pop() || '';
        for (const block of blocks) {
          if (!block.trim()) continue;
          let eventType = '';
          let dataStr = '';
          for (const line of block.split('\n')) {
            const l = line.trim();
            if (l.startsWith('event:')) {
              eventType = l.slice(6).trim();
            } else if (l.startsWith('data:')) {
              dataStr += l.slice(5).trim();
            }
          }
          if (!dataStr) continue;
          let evt: any;
          try {
            evt = JSON.parse(dataStr);
          } catch {
            continue;
          }
          handleEvent(eventType, evt);
        }
      }
      message.success('市场分析完成，已同步 QuantDB 最新数据');
      // 手动分析后强制实时重拉所有面板（跳过快照），确保各 tab 展示最新交易日数据
      setSnapDate(undefined);
      setRealtime(true);
      await fetchMarketData(true);
    } catch (e) {
      console.error('市场分析触发失败:', e);
      message.error('触发市场分析失败，请检查后端服务状态');
    } finally {
      setAnalyzing(false);
    }
  };

  const navTabs = [
    { id: 'panorama', label: '大盘全景看板', icon: Activity },
    { id: 'flow-bar', label: '多周期资金流向', icon: BarChart3 },
    { id: 'money-flow', label: '板块资金链', icon: Network },
    { id: 'stock-flow', label: '个股资金流向', icon: TrendingUp },
    { id: 'tag-lookup', label: '标签双向查询', icon: TagIcon },
  ];

  const periodOptions: Array<{ id: '1d' | '5d' | '20d'; label: string }> = [
    { id: '1d', label: '1日' },
    { id: '5d', label: '5日' },
    { id: '20d', label: '20日' },
  ];

  return (
    <div className={PAGE_LAYOUT.outerClass}>
      <div className={PAGE_LAYOUT.frameClass}>
        {/* 🌟 统一顶栏 60px */}
        <header
          className={PAGE_LAYOUT.headerClass}
          style={{ height: `${PAGE_LAYOUT.headerHeight}px` }}
        >
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-10 h-10 bg-gradient-to-br from-purple-600 via-indigo-600 to-purple-700 rounded-2xl flex items-center justify-center shadow-md shrink-0">
              <Activity className="w-5 h-5 text-white" />
            </div>
            <div className="flex items-center gap-2.5 min-w-0">
              <h1 className="text-lg font-bold text-slate-800 tracking-tight whitespace-nowrap">市场分析</h1>
              <span className="hidden sm:inline-flex px-2.5 py-0.5 rounded-full bg-purple-50 text-purple-700 border border-purple-200/80 text-[11px] font-bold font-mono items-center gap-1">
                <Sparkles className="w-3 h-3 text-purple-600" />
                <span>QuantDB 2.0 数据引擎</span>
              </span>
            </div>
          </div>

          <div className="flex items-center gap-2.5 flex-shrink-0">
            {dataDate && (
              <span
                title="行情数据对应的最新交易日"
                className="hidden lg:flex items-center gap-1.5 px-3 py-1 rounded-full bg-slate-50 text-slate-600 border border-slate-200/70 text-[11px] font-bold font-mono whitespace-nowrap"
              >
                <Clock className="w-3 h-3 text-purple-500" />
                <span>数据日期:</span>
                <span className="text-purple-700 font-extrabold">{dataDate}</span>
              </span>
            )}
            <div className="relative w-48 sm:w-60">
              <Input
                prefix={<Search className="w-3.5 h-3.5 text-purple-400 mr-1.5" />}
                placeholder="全局搜索行业或股票..."
                value={searchQuery}
                onChange={(e) => { setSearchQuery(e.target.value); setIsSearchOpen(true); }}
                onFocus={() => setIsSearchOpen(true)}
                onBlur={() => setTimeout(() => setIsSearchOpen(false), 150)}
                className="rounded-xl border border-purple-200/80 bg-slate-50/60 text-xs text-slate-800 placeholder-slate-400 py-1 px-3 shadow-2xs hover:border-purple-300 focus:bg-white focus:ring-2 focus:ring-purple-100 transition-all"
              />
              {isSearchOpen && searchQuery.trim() && searchResults.length > 0 && (
                <div className="absolute left-0 right-0 top-full mt-1.5 bg-white rounded-xl border border-purple-100 shadow-xl z-50 overflow-hidden max-h-80 overflow-y-auto">
                  {searchResults.map((item) => (
                    <button
                      key={item.symbol}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => { setFocusStock(item); setActiveTab('stock-flow'); setSearchQuery(''); setIsSearchOpen(false); }}
                      className="w-full flex items-center justify-between gap-2 px-3 py-2 hover:bg-purple-50 border-b border-slate-100 last:border-0 text-left"
                    >
                      <span className="text-xs font-extrabold text-slate-800 truncate">{item.name}</span>
                      <span className="text-[11px] font-mono text-slate-400">{item.symbol}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* 🎯 快照历史日期选择器 */}
            <div className="flex items-center gap-1.5">
              <DatePicker
                allowClear
                value={snapDate ? dayjs(snapDate) : null}
                onChange={(d) => setSnapDate(d ? d.format('YYYY-MM-DD') : undefined)}
                disabledDate={(d) => (snapDates.length > 0 ? !snapDates.includes(d.format('YYYY-MM-DD')) : false)}
                placeholder="快照日期(默认最新)"
                className="!w-36 sm:!w-40"
                size="small"
                suffixIcon={<Clock className="w-3 h-3 text-purple-400" />}
              />
              {snapDate && (
                <span className="text-[11px] text-slate-400 cursor-pointer hover:text-purple-600 whitespace-nowrap" onClick={() => setSnapDate(undefined)}>
                  最新
                </span>
              )}
            </div>

            {/* 🎯 手动市场分析触发按钮 */}
            <button
              onClick={handleTriggerAnalysis}
              disabled={analyzing}
              className={`flex items-center gap-1.5 px-3.5 py-1.5 rounded-full text-xs font-extrabold shadow-md transition-all duration-200 whitespace-nowrap cursor-pointer ${
                analyzing
                  ? 'bg-purple-400 text-white cursor-wait opacity-80'
                  : 'bg-gradient-to-r from-purple-600 via-indigo-600 to-purple-700 hover:from-purple-500 hover:to-indigo-500 active:scale-95 text-white shadow-purple-600/30'
              }`}
              title="从本地 QuantDB 读取最新数据并重新执行全市场多维分析"
            >
              {analyzing ? (
                <RefreshCw className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Zap className="w-3.5 h-3.5 text-amber-300 fill-amber-300" />
              )}
              <span>{analyzing ? '正在分析…' : '市场分析'}</span>
            </button>
          </div>
        </header>

        {/* 📊 二级控制条：五大核心指数快照 + 功能 Tabs 导航 */}
        <div className="flex-shrink-0 bg-slate-50/70 border-b border-gray-100 px-6 py-2.5 flex flex-col gap-2.5">
          <BroadMarketHeader indices={indices} loading={loading} />
          <div className="flex items-center justify-between pt-0.5">
            <div className="flex items-center gap-1.5 overflow-x-auto">
              {navTabs.map((tab) => {
                const Icon = tab.icon;
                const isActive = activeTab === tab.id;
                return (
                  <button
                    key={tab.id}
                    onClick={() => setActiveTab(tab.id)}
                    className={`flex items-center gap-1.5 px-4 py-1.5 rounded-full text-xs font-bold transition-all duration-200 whitespace-nowrap ${
                      isActive
                        ? 'bg-purple-600 text-white shadow-md shadow-purple-600/25 scale-[1.01]'
                        : 'bg-white text-slate-600 hover:text-slate-900 hover:bg-slate-100/80 border border-slate-200/80 shadow-2xs'
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
        </div>

        {/* 📈 主内容区：全景看板禁用外部滚动条，保证 100% 紧凑铺满 */}
        <div className={`flex-1 min-h-0 ${activeTab === 'panorama' || activeTab === 'tag-lookup' ? 'overflow-hidden p-4' : 'overflow-y-auto p-6 pb-20'} flex flex-col gap-3.5 bg-[#f8fafc] custom-scrollbar`}>
          {/* 📊 资金流向全景主功能页 (含 1日/3日/5日/10日/20日 横向柱状图) */}
      {activeTab === 'flow-bar' && (
        <div className="flex flex-col gap-4">
          {/* 🛠️ 控制中心工具栏 Toolbar */}
          <div className="bg-white/95 backdrop-blur-md rounded-2xl p-3.5 border border-purple-100/80 shadow-xs flex flex-wrap items-center justify-between gap-4">
            {/* 1. 周期选择器 (1日, 3日, 5日, 10日, 20日) */}
            <div className="flex items-center gap-2">
              <span className="text-xs font-extrabold text-slate-700 flex items-center gap-1">
                <Clock className="w-3.5 h-3.5 text-purple-600" />
                <span>统计周期:</span>
              </span>
              <div className="flex items-center bg-slate-100/90 border border-slate-200/60 p-1 rounded-full gap-1 shadow-2xs">
                {periodOptions.map((p) => (
                  <button
                    key={p.id}
                    onClick={() => setPeriod(p.id)}
                    className={`px-3.5 py-1 rounded-full text-xs font-extrabold transition-all duration-200 ${
                      period === p.id
                        ? 'bg-purple-600 text-white shadow-md shadow-purple-600/20 scale-[1.02]'
                        : 'text-slate-600 hover:text-slate-900 hover:bg-slate-200/60'
                    }`}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>

            {/* 2. 维度与分类选择 */}
            <div className="flex items-center gap-3">
              <div className="flex items-center bg-purple-50/90 border border-purple-200/70 rounded-full p-1 gap-1 shadow-2xs">
                <button
                  onClick={() => setFlowDimension('sector')}
                  className={`px-4 py-1 rounded-full text-xs font-extrabold transition-all duration-200 ${
                    flowDimension === 'sector'
                      ? 'bg-purple-600 text-white shadow-md shadow-purple-600/20 scale-[1.02]'
                      : 'text-purple-700 hover:text-purple-900 hover:bg-purple-100/60'
                  }`}
                >
                  板块/行业流向
                </button>
                <button
                  onClick={() => setFlowDimension('stock')}
                  className={`px-4 py-1 rounded-full text-xs font-extrabold transition-all duration-200 ${
                    flowDimension === 'stock'
                      ? 'bg-purple-600 text-white shadow-md shadow-purple-600/20 scale-[1.02]'
                      : 'text-purple-700 hover:text-purple-900 hover:bg-purple-100/60'
                  }`}
                >
                  个股流向
                </button>
              </div>

              {flowDimension === 'sector' && (
                <div className="flex items-center bg-slate-100/90 border border-slate-200/60 rounded-full p-1 gap-1 shadow-2xs">
                  <button
                    onClick={() => setCategoryMode('shenwan')}
                    className={`px-4 py-1 rounded-full text-xs font-extrabold transition-all duration-200 ${
                      categoryMode === 'shenwan'
                        ? 'bg-white text-slate-900 shadow-sm border border-slate-200/80 font-black'
                        : 'text-slate-500 hover:text-slate-800'
                    }`}
                  >
                    通达信二级(80)
                  </button>
                  <button
                    onClick={() => setCategoryMode('concept')}
                    className={`px-4 py-1 rounded-full text-xs font-extrabold transition-all duration-200 ${
                      categoryMode === 'concept'
                        ? 'bg-white text-slate-900 shadow-sm border border-slate-200/80 font-black'
                        : 'text-slate-500 hover:text-slate-800'
                    }`}
                  >
                    热门概念(50)
                  </button>
                </div>
              )}

              {/* 3. 视图模式 (柱状图 vs TreeMap 树图) */}
              <div className="flex items-center bg-slate-100/90 border border-slate-200/60 rounded-full p-1 gap-1 shadow-2xs">
                <button
                  onClick={() => setChartViewMode('bar')}
                  className={`flex items-center gap-1.5 px-4 py-1 rounded-full text-xs font-extrabold transition-all duration-200 ${
                    chartViewMode === 'bar'
                      ? 'bg-purple-600 text-white shadow-md shadow-purple-600/20 scale-[1.02]'
                      : 'text-slate-600 hover:text-slate-900'
                  }`}
                >
                  <BarChart3 className="w-3.5 h-3.5" />
                  <span>横向柱图</span>
                </button>
                <button
                  onClick={() => setChartViewMode('treemap')}
                  className={`flex items-center gap-1.5 px-4 py-1 rounded-full text-xs font-extrabold transition-all duration-200 ${
                    chartViewMode === 'treemap'
                      ? 'bg-purple-600 text-white shadow-md shadow-purple-600/20 scale-[1.02]'
                      : 'text-slate-600 hover:text-slate-900'
                  }`}
                >
                  <Layers className="w-3.5 h-3.5" />
                  <span>矩形树图</span>
                </button>
              </div>

              {/* 4. 排序口径 (绝对净值 vs 相对涨跌) */}
              <div className="flex items-center bg-amber-50/90 border border-amber-200/70 rounded-full p-1 gap-1 shadow-2xs">
                <button
                  onClick={() => setSortMode('absolute')}
                  className={`px-3 py-1 rounded-full text-xs font-extrabold transition-all ${sortMode === 'absolute' ? 'bg-amber-600 text-white shadow' : 'text-amber-700'}`}
                >
                  绝对(亿)
                </button>
                <button
                  onClick={() => setSortMode('relative')}
                  className={`px-3 py-1 rounded-full text-xs font-extrabold transition-all ${sortMode === 'relative' ? 'bg-amber-600 text-white shadow' : 'text-amber-700'}`}
                >
                  相对(%)
                </button>
              </div>
            </div>
          </div>

          {/* 📈 主图表展现区 */}
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 items-start">
            <div
              className={`${
                selectedFlowItem ? 'lg:col-span-8' : 'lg:col-span-12'
              } bg-white/95 backdrop-blur-md rounded-3xl p-5 border border-purple-100/80 shadow-md shadow-purple-500/5 flex flex-col gap-3 transition-all duration-300`}
            >
              <div className="flex items-center justify-between border-b border-slate-100 pb-3">
                <div className="flex items-center gap-2">
                  <span className="w-2.5 h-2.5 rounded-full bg-purple-600 animate-pulse" />
                  <h3 className="text-sm font-extrabold text-slate-900">
                    {period.toUpperCase()} 资金净流入/净流出{flowDimension === 'sector' ? '板块' : '个股'}排行榜
                  </h3>
                </div>
                <span className="text-xs text-slate-400 font-mono">
                  右侧红色: 资金净流入  |  左侧绿色: 资金净流出
                </span>
              </div>

              {chartViewMode === 'bar' ? (
                <CapitalFlowHorizontalBarChart
                  period={period}
                  dimension={flowDimension}
                  categoryMode={categoryMode}
                  height={940}
                  onItemClick={(item) => setSelectedFlowItem(item)}
                  sortMode={sortMode}
                  realtime={realtime}
                />
              ) : (
                <ShenwanHeatmapChart data={treemapData} height={780} />
              )}
            </div>

            {/* 🎯 点击下钻卡片 Panel */}
            {selectedFlowItem && (
              <div className="lg:col-span-4 bg-white/95 backdrop-blur-md rounded-3xl p-5 border border-purple-100/80 shadow-md shadow-purple-500/5 flex flex-col gap-4 animate-in fade-in slide-in-from-right-4 duration-300">
                <div className="flex items-center justify-between border-b border-purple-100 pb-3">
                  <div>
                    <h4 className="text-sm font-extrabold text-slate-900 flex items-center gap-2">
                      <span>{selectedFlowItem.name}</span>
                      {selectedFlowItem.symbol && (
                        <span className="text-xs font-mono text-purple-600 bg-purple-50 px-2 py-0.5 rounded-md border border-purple-200">
                          {selectedFlowItem.symbol}
                        </span>
                      )}
                    </h4>
                    <p className="text-[11px] text-slate-400 mt-0.5">下钻资金流与主力动向拆解</p>
                  </div>
                  <button
                    onClick={() => setSelectedFlowItem(null)}
                    className="text-slate-400 hover:text-slate-700 text-xs font-bold px-2 py-1 bg-slate-100 rounded-lg"
                  >
                    关闭 ✕
                  </button>
                </div>

                <div className="flex flex-col gap-3">
                  <div className="grid grid-cols-2 gap-2">
                    <div className="bg-purple-50/60 rounded-2xl p-3 border border-purple-100">
                      <span className="text-[11px] text-slate-500">区间累计净流入</span>
                      <div className={`text-base font-extrabold font-mono mt-1 ${selectedFlowItem.net_inflow >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
                        {selectedFlowItem.net_inflow >= 0 ? '+' : ''}
                        {(selectedFlowItem.net_inflow / 100000000).toFixed(2)} 亿元
                      </div>
                    </div>

                    <div className="bg-purple-50/60 rounded-2xl p-3 border border-purple-100">
                      <span className="text-[11px] text-slate-500">主力净占比</span>
                      <div className="text-base font-extrabold font-mono text-purple-700 mt-1">
                        {selectedFlowItem.main_ratio}%
                      </div>
                    </div>
                  </div>

                  <div className="flex flex-col gap-2 pt-2 border-t border-slate-100">
                    <span className="text-xs font-bold text-slate-700">筹码四分结构 (元):</span>
                    <div className="space-y-1.5 text-xs font-mono">
                      <div className="flex justify-between items-center bg-rose-50/80 px-3 py-1.5 rounded-xl border border-rose-100">
                        <span className="text-rose-700 font-bold">🔴 超大单 (主力)</span>
                        <span className="font-extrabold text-rose-800">
                          {(selectedFlowItem.super_large / 100000000).toFixed(2)} 亿
                        </span>
                      </div>
                      <div className="flex justify-between items-center bg-orange-50/80 px-3 py-1.5 rounded-xl border border-orange-100">
                        <span className="text-orange-700 font-bold">🟠 大单 (游资)</span>
                        <span className="font-extrabold text-orange-800">
                          {(selectedFlowItem.large / 100000000).toFixed(2)} 亿
                        </span>
                      </div>
                      <div className="flex justify-between items-center bg-amber-50/80 px-3 py-1.5 rounded-xl border border-amber-100">
                        <span className="text-amber-700 font-bold">🟡 中单</span>
                        <span className="font-extrabold text-amber-800">
                          {(selectedFlowItem.medium / 100000000).toFixed(2)} 亿
                        </span>
                      </div>
                      <div className="flex justify-between items-center bg-emerald-50/80 px-3 py-1.5 rounded-xl border border-emerald-100">
                        <span className="text-emerald-700 font-bold">🟢 小单 (散户)</span>
                        <span className="font-extrabold text-emerald-800">
                          {(selectedFlowItem.small / 100000000).toFixed(2)} 亿
                        </span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── 原有其他页面内容维持完备 ── */}
      {activeTab === 'panorama' && (
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-3.5 items-stretch flex-1 min-h-0 overflow-hidden">
          <div className="lg:col-span-5 flex flex-col justify-between gap-3 min-h-0">
            <MarketBreadthCard
              advanceCount={breadth?.advance_count}
              declineCount={breadth?.decline_count}
              flatCount={breadth?.flat_count}
              limitUpCount={breadth?.limit_up_count}
              limitDownCount={breadth?.limit_down_count}
              totalTurnoverYi={breadth?.total_turnover_yi}
              profitEffect={breadth?.profit_effect}
              limitUpBrokenRatio={breadth?.limit_up_broken_ratio}
            />
            <div className="flex flex-col gap-2 bg-white/95 backdrop-blur-md rounded-2xl p-3.5 border border-purple-100/80 shadow-md shadow-purple-500/5 min-h-0 flex-1 justify-between">
              <div className="flex items-center justify-between">
                <h3 className="text-xs font-extrabold text-slate-800 flex items-center gap-1.5">
                  <TrendingUp className="w-3.5 h-3.5 text-purple-600" />
                  <span>主力资金净流入 Top 5 股票</span>
                </h3>
                <span
                  onClick={() => setActiveTab('stock-flow')}
                  className="text-[11px] text-purple-600 font-extrabold hover:underline cursor-pointer flex items-center"
                >
                  完整排行榜 ➔
                </span>
              </div>
              <StockMoneyFlowTable items={stockFlows.slice(0, 5)} isMini={true} onStockClick={openStockDetail} />
            </div>
          </div>

          <div className="lg:col-span-7 bg-white/95 backdrop-blur-md rounded-2xl p-3.5 border border-purple-100/80 shadow-md shadow-purple-500/5 flex flex-col min-h-0 justify-between">
            <div className="flex items-center justify-between border-b border-purple-100/60 pb-2 mb-1 shrink-0">
              <h3 className="text-xs font-extrabold text-slate-900 flex items-center gap-1.5">
                <Layers className="w-3.5 h-3.5 text-purple-600" />
                <span>通达信二级分类热力矩形图谱</span>
              </h3>
              <span className="text-[10px] text-slate-400 font-mono">市值权重 vs 涨跌幅</span>
            </div>
            <div className="flex-1 min-h-0">
              <ShenwanHeatmapChart data={heatmapData} height="100%" />
            </div>
          </div>
        </div>
      )}

      {activeTab === 'money-flow' && (
        <div className="bg-white/90 backdrop-blur-md rounded-2xl p-5 border border-slate-200/80 shadow-sm flex flex-col gap-4">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-extrabold text-slate-800 flex items-center gap-1.5">
              <Network className="w-3.5 h-3.5 text-purple-600" />
              <span>主力与散户资金流动全景桑基图 (Sankey Diagram)</span>
            </h3>
            <span className="text-xs text-slate-400">资金实时划转链条</span>
          </div>
          <CapitalFlowSankeyChart
            nodes={sankeyData?.nodes}
            links={sankeyData?.links}
            height={480}
          />
        </div>
      )}

      {activeTab === 'stock-flow' && (
        <div className="flex-1 min-h-0 flex flex-col gap-3 overflow-hidden">
          {focusStock && (
            <div id="focus-stock-detail" className="flex-shrink-0 bg-white/95 backdrop-blur-md rounded-3xl p-5 border border-purple-100/80 shadow-md shadow-purple-500/5 flex flex-col gap-4 scroll-mt-4">
              <div className="flex items-center justify-between">
                <h3 className="text-xs font-extrabold text-slate-800 flex items-center gap-1.5">
                  <TrendingUp className="w-3.5 h-3.5 text-purple-600" />
                  <span>个股资金流详情</span>
                  <span className="font-mono text-purple-600 bg-purple-50 px-2 py-0.5 rounded-md border border-purple-200">
                    {focusStock.name} ({focusStock.symbol})
                  </span>
                </h3>
                <span className="text-xs text-slate-400 font-mono">{breadth?.trade_date || dataDate}</span>
              </div>
              <div className="grid grid-cols-3 gap-4">
                <div className="bg-purple-50/60 rounded-2xl p-4 border border-purple-100 flex flex-col items-center justify-center text-center">
                  <span className="text-[11px] text-slate-500">当日净流入</span>
                  <div className={`text-base font-extrabold font-mono mt-1 ${(focusStock.net_inflow ?? 0) >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
                    {(focusStock.net_inflow ?? 0) >= 0 ? '+' : ''}{((focusStock.net_inflow ?? 0) / 1e8).toFixed(2)} 亿元
                  </div>
                </div>
                <div className="bg-purple-50/60 rounded-2xl p-4 border border-purple-100 flex flex-col items-center justify-center text-center">
                  <span className="text-[11px] text-slate-500">主力净占比</span>
                  <div className="text-base font-extrabold font-mono text-purple-700 mt-1">{focusStock.main_ratio ?? 0}%</div>
                </div>
                <div className="bg-purple-50/60 rounded-2xl p-4 border border-purple-100 flex flex-col items-center justify-center text-center">
                  <span className="text-[11px] text-slate-500">涨跌幅 / 最新价</span>
                  <div className={`text-base font-extrabold font-mono mt-1 ${(focusStock.pct_change ?? 0) >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
                    {(focusStock.pct_change ?? 0) >= 0 ? '+' : ''}{focusStock.pct_change?.toFixed(2)}% / {focusStock.close_price ?? '—'}
                  </div>
                </div>
              </div>
              <div>{renderFourBarChart(focusStock)}</div>
            </div>
          )}
          <div className="flex items-center justify-between flex-shrink-0">
            <h3 className="text-xs font-extrabold text-slate-800 flex items-center gap-1.5">
              <TrendingUp className="w-3.5 h-3.5 text-purple-600" />
              <span>个股主力资金净流入排行榜</span>
            </h3>
            <span className="text-xs text-slate-400">包含超大单、大单、中单、小单拆解</span>
          </div>
          <div className="flex-1 min-h-0 overflow-y-auto pb-2">
            <StockMoneyFlowTable items={stockFlows} loading={loading} latestDate={breadth?.trade_date} onStockClick={openStockDetail} />
          </div>
        </div>
      )}

          {activeTab === 'tag-lookup' && (
            <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
              <TagLookupPanel />
            </div>
          )}
        </div>
      </div>
    </div>
  );
};


