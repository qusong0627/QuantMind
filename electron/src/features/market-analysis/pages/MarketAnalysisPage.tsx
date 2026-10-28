import React, { useState, useEffect } from 'react';
import { Sparkles, Activity, Network, TrendingUp, Clock, RefreshCw, Zap, Radar, Gauge } from 'lucide-react';
import { DatePicker, message } from 'antd';
import dayjs from 'dayjs';
import { BroadMarketHeader, IndexItem } from '../components/BroadMarketHeader';
import { HeatmapDrillCard } from '../components/HeatmapDrillCard';
import { StockMoneyFlowTable, StockMoneyFlowItem } from '../components/StockMoneyFlowTable';
import { MarketBreadthCard } from '../components/MarketBreadthCard';
import { MarketSentimentPanel } from '../components/MarketSentimentPanel';
import { StockTerminalSearchBox } from '../../market-analysis-shared/components/StockTerminalSearchBox';
import { StockTerminalWindow } from '../../market-analysis-shared/components/StockTerminalWindow';
import { FactorBoardPanel } from '../components/FactorBoardPanel';
import { FlowPanoramaPanel } from '../components/FlowPanoramaPanel';
import { SmartMoneyPanel } from '../components/smart-money/SmartMoneyPanel';
import { SERVICE_ENDPOINTS } from '../../../config/services';
import { PAGE_LAYOUT } from '../../../config/pageLayout';
import { fmtBeijingClock } from '../../../utils/timeBeijing';

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
  const [indices, setIndices] = useState<IndexItem[]>([]);
  const [stockFlows, setStockFlows] = useState<StockMoneyFlowItem[]>([]);
  const [breadth, setBreadth] = useState<MarketBreadthData | null>(null);
  const [dataDate, setDataDate] = useState<string>('');
  const [heatmapData, setHeatmapData] = useState<any[]>([]);
  const [updateTime, setUpdateTime] = useState<string>('');
  // 🎯 个股资金流聚焦（顶栏搜索 / 排行榜下钻 → 资金流全景内打开个股详情卡）
  const [focusSymbol, setFocusSymbol] = useState<string | null>(null);
  // 🎯 个股终端浮窗（聪明钱榜单行点击 → 弹窗；页面共用一个实例）
  const [windowSymbol, setWindowSymbol] = useState<string | null>(null);

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
      const [resIdx, resStock, resBreadth, resHeatmap] = await Promise.all([
        fetch(`${MARKET_ANALYSIS_API}/indices/overview${qs}`, { headers }),
        fetch(`${MARKET_ANALYSIS_API}/money-flow/stocks?limit=50${qsWithAmp}`, { headers }),
        fetch(`${MARKET_ANALYSIS_API}/breadth${qs}`, { headers }),
        fetch(`${MARKET_ANALYSIS_API}/heatmap?category=shenwan${qsWithAmp}`, { headers }),
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

      setUpdateTime(fmtBeijingClock(Date.now(), { withSeconds: false }));
    } catch (e) {
      console.warn('后端市场接口连接出现异常，保留现有渲染状态', e);
    } finally {
      setLoading(false);
    }
  };

  // 点击个股 → 资金流全景 tab 打开个股详情卡（聚焦个股 + 板块归因可回跳板块视角）
  const openStockDetail = (item: StockMoneyFlowItem) => {
    setFocusSymbol(item.symbol);
    setActiveTab('flow-panorama');
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
          case 'stock_flow':
            if (Array.isArray(data?.stock_flow) && data.stock_flow.length > 0) {
              setStockFlows(data.stock_flow);
            }
            break;
          default:
            break;
        }
        setUpdateTime(fmtBeijingClock(Date.now(), { withSeconds: false }));
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
    { id: 'sentiment', label: '大盘情绪', icon: Gauge },
    { id: 'flow-panorama', label: '资金流全景', icon: Network },
    { id: 'factor-board', label: '因子概率板', icon: Sparkles },
    { id: 'smart-money', label: '聪明钱', icon: Radar },
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

          {/* 全局搜索（行业或股票）：顶栏空白处居中；选中个股 → 资金流全景内打开个股详情卡 */}
          <div className="flex-1 min-w-0 flex justify-center px-4">
            <StockTerminalSearchBox
              market="CN"
              widthClass="w-full max-w-[520px]"
              hint="温馨提示：请先下载完整行情数据包并保持每日更新，否则可能搜不到标的、K线或推理分为空。"
              onPick={(symbol) => {
                // 聚焦个股 → 资金流全景内打开详情卡（含板块归因，可回跳板块视角）
                setFocusSymbol(symbol);
                setActiveTab('flow-panorama');
              }}
            />
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

        {/* 📊 二级控制条：功能 Tabs 导航（指数快照卡已移入「大盘全景看板」页签） */}
        <div className="flex-shrink-0 bg-slate-50/70 border-b border-gray-100 px-6 py-2 flex flex-col gap-1.5">
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
        <div className={`flex-1 min-h-0 ${activeTab === 'panorama' ? 'overflow-hidden p-4' : 'overflow-y-auto p-6 pb-20'} flex flex-col gap-3.5 bg-[#f8fafc] custom-scrollbar`}>
          {activeTab === 'panorama' && (
            <div className="flex flex-col gap-3 flex-1 min-h-0 overflow-hidden">
              {/* 核心指数快照（含多周期模型预测）：只在全景看板页签内，不占其它页签空间 */}
              <div className="shrink-0">
                <BroadMarketHeader indices={indices} loading={loading} compact />
              </div>
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
                <div className="flex flex-col gap-2 bg-white/95 backdrop-blur-md rounded-2xl p-3.5 border border-purple-100/80 shadow-md shadow-purple-500/5 min-h-0 flex-1">
                  <div className="flex items-center justify-between shrink-0">
                    <h3 className="text-xs font-extrabold text-slate-800 flex items-center gap-1.5">
                      <TrendingUp className="w-3.5 h-3.5 text-purple-600" />
                      <span>主力资金净流入 Top {Math.min(50, stockFlows.length) || 50} 股票</span>
                      <span className="text-[10px] font-mono font-normal text-slate-400">列表内滚动查看</span>
                    </h3>
                    <span
                      onClick={() => setActiveTab('flow-panorama')}
                      className="text-[11px] text-purple-600 font-extrabold hover:underline cursor-pointer flex items-center"
                    >
                      资金流全景 ➔
                    </span>
                  </div>
                  {/* 滚动容器 + 表头吸顶（antd 表头默认 relative，!sticky 覆盖） */}
                  <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar rounded-xl [&_.ant-table-thead>tr>th]:!sticky [&_.ant-table-thead>tr>th]:top-0 [&_.ant-table-thead>tr>th]:z-10">
                    <StockMoneyFlowTable items={stockFlows.slice(0, 50)} isMini={true} onStockClick={openStockDetail} />
                  </div>
                </div>
              </div>

              <div className="lg:col-span-7 min-h-0">
                <HeatmapDrillCard
                  topItems={heatmapData}
                  date={snapDate}
                  onStockClick={(symbol, name) => openStockDetail({ symbol, name } as StockMoneyFlowItem)}
                />
              </div>
              </div>
            </div>
          )}

          {activeTab === 'sentiment' && <MarketSentimentPanel />}

          {activeTab === 'flow-panorama' && (
            <FlowPanoramaPanel
              date={snapDate}
              focusSymbol={focusSymbol}
              onClearFocus={() => setFocusSymbol(null)}
            />
          )}

          {activeTab === 'factor-board' && <FactorBoardPanel />}

          {activeTab === 'smart-money' && (
            <SmartMoneyPanel date={snapDate} onOpenTerminal={(s) => setWindowSymbol(s)} />
          )}
        </div>

        {/* 个股终端浮窗（页面共用一例；聪明钱榜单行点击打开） */}
        <StockTerminalWindow
          open={!!windowSymbol}
          symbol={windowSymbol}
          market="CN"
          onClose={() => setWindowSymbol(null)}
        />
      </div>
    </div>
  );
};
