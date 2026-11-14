import React, { useState, useEffect } from 'react';
import { Sparkles, Activity, Network, Clock, RefreshCw, Zap, Radar, Gauge } from 'lucide-react';
import { DatePicker, message } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import dayjs from 'dayjs';
import type { IndexItem } from '../components/BroadMarketHeader';
import { MarketSentimentPanel } from '../components/MarketSentimentPanel';
import { MarketRegimePanel } from '../components/MarketRegimePanel';
import { MarketPanoramaPanel } from '../components/panorama/MarketPanoramaPanel';
import { StockTerminalSearchBox } from '../../market-analysis-shared/components/StockTerminalSearchBox';
import { StockTerminalWindow } from '../../market-analysis-shared/components/StockTerminalWindow';
import type { SectorHeatmapItem } from '../../market-analysis-shared/SectorHeatmapChart';
import { FactorBoardPanel } from '../components/FactorBoardPanel';
import { FlowPanoramaPanel } from '../components/FlowPanoramaPanel';
import { SmartMoneyPanel } from '../components/smart-money/SmartMoneyPanel';
import { SERVICE_ENDPOINTS } from '../../../config/services';
import { PAGE_LAYOUT } from '../../../config/pageLayout';
import { fmtBeijingClock } from '../../../utils/timeBeijing';

const MARKET_ANALYSIS_API = `${SERVICE_ENDPOINTS.USER_SERVICE}/market-analysis`;

/** 周日作为一周第一天的中文日历：antd 只把 locale.lang 传给 rc-picker，
 *  所以周起始日与星期文案必须写在 lang 里。 */
const SUNDAY_PICKER_LOCALE = {
  ...zhCN.DatePicker,
  lang: {
    ...zhCN.DatePicker.lang,
    locale: 'en',
    monthFormat: 'M月',
    shortWeekDays: ['日', '一', '二', '三', '四', '五', '六'],
  },
};
/** 阶段/情绪历史窗口约 2 年；更早日期后端取不到同口径的日频面板。 */
const MIN_SELECTABLE_DATE = dayjs().subtract(2, 'year').startOf('day');

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
  const [breadth, setBreadth] = useState<MarketBreadthData | null>(null);
  const [heatmapData, setHeatmapData] = useState<SectorHeatmapItem[]>([]);
  const [dataDate, setDataDate] = useState<string>('');
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

  // 选中日期没有行情快照：阶段/情绪/资金流可回看，但指数带/广度/方块图无同口径数据
  const snapshotMissing =
    !!snapDate && snapDates.length > 0 && !snapDates.includes(snapDate);
  const snapshotFrom = snapDates.length ? snapDates[snapDates.length - 1] : undefined;

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
  }, [snapDate, snapDates.length]);

  const fetchMarketData = async (forceRealtime?: boolean) => {
    setLoading(true);
    const token = localStorage.getItem('access_token') || '';
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    const useRealtime = forceRealtime !== undefined ? forceRealtime : realtime;
    const qs = buildQS(snapDate, useRealtime);
    const qsWithAmp = qs ? qs.replace('?', '&') : '';

    // 该日期不在快照覆盖内：不要把「最新快照」冒充成历史数据
    if (snapshotMissing && !useRealtime) {
      setIndices([]);
      setBreadth(null);
      setHeatmapData([]);
      setDataDate(snapDate || '');
      setUpdateTime(fmtBeijingClock(Date.now(), { withSeconds: false }));
      setLoading(false);
      return;
    }

    try {
      const [resIdx, resBreadth, resHeatmap] = await Promise.all([
        fetch(`${MARKET_ANALYSIS_API}/indices/overview${qs}`, { headers }),
        fetch(`${MARKET_ANALYSIS_API}/breadth${qs}`, { headers }),
        fetch(`${MARKET_ANALYSIS_API}/heatmap?category=shenwan${qsWithAmp}`, { headers }),
      ]);

      if (resIdx.ok) {
        const idxData = await resIdx.json();
        if (Array.isArray(idxData) && idxData.length > 0) {
          setIndices(idxData);
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
    { id: 'regime', label: '市场阶段', icon: Gauge },
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
            {dataDate && !snapshotMissing && (
              <span
                title="行情数据对应的最新交易日"
                className="hidden lg:flex items-center gap-1.5 px-3 py-1 rounded-full bg-slate-50 text-slate-600 border border-slate-200/70 text-[11px] font-bold font-mono whitespace-nowrap"
              >
                <Clock className="w-3 h-3 text-purple-500" />
                <span>数据日期:</span>
                <span className="text-purple-700 font-extrabold">{dataDate}</span>
              </span>
            )}

            {/* 🎯 历史回看日期选择器：阶段/情绪/资金流近 2 年；行情快照自 2026-08-31 起 */}
            <div className="flex items-center gap-1.5">
              <DatePicker
                allowClear
                value={snapDate ? dayjs(snapDate) : null}
                onChange={(d) => setSnapDate(d ? d.format('YYYY-MM-DD') : undefined)}
                disabledDate={(d) =>
                  d.isAfter(dayjs(), 'day') ||
                  d.isBefore(MIN_SELECTABLE_DATE, 'day') ||
                  d.day() === 0 ||
                  d.day() === 6
                }
                placeholder="回看日期(默认最新)"
                className="!w-36 sm:!w-40"
                size="small"
                locale={SUNDAY_PICKER_LOCALE}
                title={`阶段/情绪/资金流支持近 2 年回看；指数带、广度与板块方块图仅支持有行情快照的日期${snapshotFrom ? `（${snapshotFrom} 起）` : ''}`}
                suffixIcon={<Clock className="w-3 h-3 text-purple-400" />}
              />
              {snapDate && (
                <span className="text-[11px] text-slate-400 cursor-pointer hover:text-purple-600 whitespace-nowrap" onClick={() => setSnapDate(undefined)}>
                  最新
                </span>
              )}
              {snapshotMissing && (
                <span
                  className="hidden xl:inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10px] font-bold text-amber-700 whitespace-nowrap"
                  title="该日期无行情快照：阶段/情绪/资金流为历史回看，指数带/广度/方块图不展示"
                >
                  无快照
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

        {/* 📈 主内容区：全景看板优先一屏铺满，窄屏可滚动 */}
        <div className={`flex-1 min-h-0 ${activeTab === 'panorama' ? 'overflow-y-auto p-4 pb-40 [@media(max-height:950px)]:pb-52' : 'overflow-y-auto p-6 pb-20'} flex flex-col gap-3.5 bg-[#f8fafc] custom-scrollbar`}>
          {activeTab === 'panorama' && (
            <MarketPanoramaPanel
              date={snapDate}
              indices={indices}
              breadth={breadth}
              heatmap={heatmapData}
              loading={loading}
              snapshotMissing={snapshotMissing}
              snapshotFrom={snapshotFrom}
              onNavigate={setActiveTab}
              onOpenStock={(symbol) => setWindowSymbol(symbol)}
            />
          )}

          {activeTab === 'regime' && (
            <MarketRegimePanel onOpenStock={(s) => setWindowSymbol(s)} />
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
