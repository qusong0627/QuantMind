/** K 线工作区：可嵌入页面或 Modal，含周期/指标/指数叠加/回放/信号/回测全部功能 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  CandlestickChart, Star, Activity, TrendingUp, ArrowLeftRight, ChevronLeft, ChevronRight, Search,
} from 'lucide-react';
import { Button, Select, Tooltip, message, Modal, InputNumber, Input, Switch, Tag, AutoComplete } from 'antd';
import { useNavigate } from 'react-router-dom';
import { StockListItem, StockProfile, KlineBar } from '../../types';
import { stockTerminalService } from '../../services/stockTerminalService';
import { researchService } from '../../../../services/researchService';
import { KlineChart, IndicatorConfig, IndexOverlay, SignalPoint, ScoreSeries, TradeMarker, RefLine, AlertPoint, OVERLAY_COLORS, SubplotType } from './KlineChart';
import { KlineReplay } from './KlineReplay';
import { ChartBacktestPanel, ChartBacktestData } from '../ChartBacktestPanel';
import { toPrefix } from '../StockSidebar';

/** 纯数字代码推导市场后缀：SH/SZ/BJ */
export function suffixOf(code: string): string {
  const c = code.replace(/\D/g, '').slice(-6);
  if (c.startsWith('6') || c.startsWith('9')) return `${c}.SH`;
  if (c.startsWith('4') || c.startsWith('8')) return `${c}.BJ`;
  return `${c}.SZ`;
}

const INDEX_OPTIONS = [
  { label: '上证指数', value: '000001.SH' },
  { label: '深证成指', value: '399001.SZ' },
  { label: '沪深300', value: '000300.SH' },
  { label: '中证500', value: '000905.SZ' },
];

const SUBPLOT_META: Record<SubplotType, string> = { vol: 'VOL', macd: 'MACD', kdj: 'KDJ', rsi: 'RSI' };

/** 多模型分数线配色（与 KlineChart 分数轴同族） */
const SCORE_PALETTE = ['#6366f1', '#f59e0b', '#10b981', '#e11d48', '#0ea5e9', '#0ea5e9', '#a855f7', '#f97316'];

interface Props {
  stock: StockListItem;
  profile?: StockProfile | null;
  height?: number;
  /** 切换股票（上一股/下一股/搜索选择）时通知父级同步 */
  onSelectStock?: (item: StockListItem) => void;
}

export function KlineWorkspace({ stock, profile, height = 460, onSelectStock }: Props) {
  const navigate = useNavigate();
  const [bars, setBars] = useState<KlineBar[]>([]);
  const [loadingKline, setLoadingKline] = useState(false);
  const [period, setPeriod] = useState<'daily' | 'weekly' | 'monthly' | 'min5' | 'min1'>('daily');
  const [minAvail, setMinAvail] = useState<{ min5: boolean; min1: boolean }>({ min5: false, min1: false });
  const [config, setConfig] = useState<IndicatorConfig>({ ma: true, boll: false, subplots: ['vol', 'macd'] });
  const [overlayCodes, setOverlayCodes] = useState<string[]>([]);
  const [overlayCache, setOverlayCache] = useState<Record<string, { date: string; close: number }[]>>({});
  const [signals, setSignals] = useState<SignalPoint[]>([]);
  const [signalOn, setSignalOn] = useState(true);
  const [btData, setBtData] = useState<ChartBacktestData | null>(null);
  const [replayActive, setReplayActive] = useState(false);
  const [replayCursor, setReplayCursor] = useState(1);
  const [replayPlaying, setReplayPlaying] = useState(false);
  const [replaySpeed, setReplaySpeed] = useState(1);
  const [watchlist, setWatchlist] = useState<Set<string>>(new Set());
  // 推理分数历史（多模型叠加）
  const [scoreModels, setScoreModels] = useState<{ model_id: string; display_name?: string }[]>([]);
  const [scoreSeries, setScoreSeries] = useState<ScoreSeries[]>([]);
  // 全量模型分数（按模型分组的原始数据）：选中模型不在 top3 时也从这里构造分数序列
  const [scoreByModel, setScoreByModel] = useState<Map<string, { date: string; fusion: number | null; side: string | null }[]>>(new Map());
  const [scoreLoading, setScoreLoading] = useState(false);
  const [selectedModel, setSelectedModel] = useState<string>('all');
  // 模拟交易
  const [trades, setTrades] = useState<TradeMarker[]>([]);
  const [tradeModal, setTradeModal] = useState<{ bar: KlineBar } | null>(null);
  const [tradeShares, setTradeShares] = useState(100);
  // 参考线（按模型 localStorage 持久化）
  const [refLines, setRefLines] = useState<RefLine[]>([]);
  const [refLineModal, setRefLineModal] = useState(false);
  const [newRefLine, setNewRefLine] = useState({ value: 0.1, label: '可买', color: '#10b981' });
  // 股票导航：上一股/下一股（分数降序列表）+ 搜索
  const [navList, setNavList] = useState<StockListItem[]>([]);
  const [searchText, setSearchText] = useState('');
  const [searchOptions, setSearchOptions] = useState<{ value: string; label: React.ReactNode }[]>([]);
  // 大盘状态（上证指数 vs MA20）
  const [indexStatus, setIndexStatus] = useState<{ latestClose: number; ma20: number | null; below: boolean } | null>(null);
  // 当前排名（按最近推理日 + 所选模型在全市场中的分数排名）
  const [scoreRank, setScoreRank] = useState<number | null>(null);

  // 导航列表：默认最近交易日的全市场分数降序（同左栏列表）
  useEffect(() => {
    let cancelled = false;
    stockTerminalService.getStockList({ page_size: 5000 }).then(resp => {
      if (!cancelled) setNavList(resp.items);
    }).catch(() => { /* ignore */ });
    return () => { cancelled = true; };
  }, []);

  const navIndex = navList.findIndex(it => it.symbol === stock.symbol);

  // 大盘状态：上证指数收盘 vs MA20（QuantDB index_daily）
  useEffect(() => {
    let cancelled = false;
    stockTerminalService.getIndexKline('000001.SH', 120).then(closes => {
      if (cancelled || closes.length < 21) return;
      const arr = closes.map(c => c.close);
      const last20 = arr.slice(-20);
      const ma20 = last20.reduce((a, b) => a + b, 0) / last20.length;
      const latestClose = arr[arr.length - 1];
      setIndexStatus({ latestClose: Number(latestClose.toFixed(2)), ma20: Number(ma20.toFixed(2)), below: latestClose < ma20 });
    }).catch(() => { /* ignore */ });
    return () => { cancelled = true; };
  }, []);

  // 自选状态
  useEffect(() => {
    researchService.getWatchlist(200).then(resp => {
      setWatchlist(new Set(resp.items.map(i => i.symbol)));
    }).catch(() => { /* ignore */ });
  }, []);
  const isWatched = watchlist.has(toPrefix(stock.symbol));

  // 参考线：localStorage 按模型持久化
  const refLineKey = `qm:ref-lines:${selectedModel !== 'all' ? selectedModel : 'default'}`;
  useEffect(() => {
    try {
      const raw = localStorage.getItem(refLineKey);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) setRefLines(parsed.filter((l: any) => l && typeof l.value === 'number'));
      }
    } catch { /* ignore */ }
  }, [refLineKey]);
  const saveRefLines = (lines: RefLine[]) => {
    setRefLines(lines);
    try { localStorage.setItem(refLineKey, JSON.stringify(lines)); } catch { /* ignore */ }
  };

  const gotoStock = useCallback((item: StockListItem) => {
    onSelectStock?.(item);
  }, [onSelectStock]);

  const prevStock = useCallback(() => {
    if (navIndex > 0) gotoStock(navList[navIndex - 1]);
  }, [navIndex, navList, gotoStock]);
  const nextStock = useCallback(() => {
    if (navIndex >= 0 && navIndex < navList.length - 1) gotoStock(navList[navIndex + 1]);
  }, [navIndex, navList, gotoStock]);

  // 股票搜索：防抖查询 /stock-terminal/list?q=
  useEffect(() => {
    const kw = searchText.trim();
    if (!kw) { setSearchOptions([]); return; }
    let cancelled = false;
    const t = setTimeout(() => {
      stockTerminalService.getStockList({ q: kw, page_size: 15 }).then(resp => {
        if (cancelled) return;
        setSearchOptions(resp.items.map(it => ({
          value: it.symbol,
          label: (
            <div className="flex items-center justify-between text-[11px]">
              <span className="font-bold text-slate-700">{it.name}</span>
              <span className="font-mono text-slate-400">{it.symbol}</span>
              <span className="font-mono text-indigo-600">{it.fusion != null ? Number(it.fusion).toFixed(4) : '--'}</span>
            </div>
          ),
        })));
      }).catch(() => { if (!cancelled) setSearchOptions([]); });
    }, 300);
    return () => { cancelled = true; clearTimeout(t); };
  }, [searchText]);

  const toggleWatch = useCallback(async () => {
    const prefix = toPrefix(stock.symbol);
    try {
      if (isWatched) {
        await researchService.removeFromWatchlist(prefix);
        const n = new Set(watchlist); n.delete(prefix); setWatchlist(n);
        message.success(`已移出自选：${stock.name}`);
      } else {
        await researchService.addToWatchlist(prefix, { stockName: stock.name });
        const n = new Set(watchlist); n.add(prefix); setWatchlist(n);
        message.success(`已加入自选：${stock.name}`);
      }
    } catch { message.error('自选操作失败'); }
  }, [stock, isWatched, watchlist]);

  // 加载 K 线 + 信号
  useEffect(() => {
    if (!stock) return;
    let cancelled = false;
    setLoadingKline(true);
    setReplayActive(false);
    setReplayCursor(1);
    setBtData(null);
    const sym = stock.symbol;
    const load = async () => {
      try {
        if (period === 'min5' || period === 'min1') {
          const { items, available } = await stockTerminalService.getMinuteKline(sym, period, 10);
          if (!cancelled) {
            setBars(items);
            setMinAvail(period === 'min1' ? { min5: minAvail.min5, min1: available } : { min5: available, min1: minAvail.min1 });
          }
          return;
        }
        let items = await stockTerminalService.getDailyKline(sym, 100);
        if ((period === 'weekly' || period === 'monthly') && items.length) items = resampleBars(items, period);
        if (!cancelled) setBars(items);
      } finally {
        if (!cancelled) setLoadingKline(false);
      }
    };
    load();
    stockTerminalService.getSignalOverlay(sym).then(sigMap => {
      if (cancelled) return;
      setSignals(Object.values(sigMap).flat().sort((a, b) => a.date.localeCompare(b.date)));
    });
    // 推理分数历史（多模型）：/models/inference/stock/{symbol}/history
    setScoreLoading(true);
    import('../../../../services/modelTrainingService').then(({ modelTrainingService }) => {
      const code = stock.symbol.split('.')[0];
      return modelTrainingService.getStockInferenceHistory(code, 500).then(resp => {
        if (cancelled) return;
        setScoreModels(resp?.models ?? []);
        const byModel = new Map<string, { date: string; fusion: number | null; side: string | null }[]>();
        for (const it of resp?.items ?? []) {
          const m = it.signal_model_id || 'default';
          if (!byModel.has(m)) byModel.set(m, []);
          byModel.get(m)!.push({ date: it.trade_date.slice(0, 10), fusion: it.fusion_score, side: it.signal_side });
        }
        // 全量保存：选中模型不在 top3 时也能从全量构造分数序列
        setScoreByModel(byModel);
        // 全部模型：只保留最近活跃的 3 个模型（点数多的优先），避免 43 条线杂乱
        const sorted = [...byModel.entries()]
          .map(([m, pts]) => ({ m, pts: pts.sort((a, b) => a.date.localeCompare(b.date)) }))
          .sort((a, b) => b.pts.length - a.pts.length);
        // 默认模型：is_default 标记优先（后端 models 已按 is_default 置顶，双保险再显式找一遍）
        const models = resp?.models ?? [];
        const defModel = (models.find((m: any) => m.is_default) ?? models[0])?.model_id;
        // 默认模型必须有线：不在 top3 时挤入，避免默认选中后分数线为空
        let top = sorted.slice(0, 3);
        if (defModel && !top.some(t => t.m === defModel)) {
          const def = sorted.find(t => t.m === defModel);
          if (def) top = [def, ...top.slice(0, 2)];
        }
        const out: ScoreSeries[] = top.map(({ m, pts }, i) => ({
          model: m, color: SCORE_PALETTE[i % SCORE_PALETTE.length], points: pts,
        }));
        setScoreSeries(out);
        // 默认选中默认模型（不是全部模型）：分数轴按当前模型分数贴合，
        // 而不是「全部模型」混合 3.x 与 0.001 量级被撑大
        if (defModel && selectedModel === 'all') setSelectedModel(defModel);
      });
    }).catch(() => { if (!cancelled) setScoreSeries([]); }).finally(() => { if (!cancelled) setScoreLoading(false); });
    return () => { cancelled = true; };
  }, [stock, period]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    overlayCodes.forEach(async code => {
      if (overlayCache[code]) return;
      const closes = await stockTerminalService.getIndexKline(code, 100);
      setOverlayCache({ ...overlayCache, [code]: closes });
    });
  }, [overlayCodes, overlayCache]);

  const overlays: IndexOverlay[] = useMemo(
    () => overlayCodes
      .filter(c => overlayCache[c]?.length)
      .map((c, i) => ({
        code: c,
        name: INDEX_OPTIONS.find(o => o.value === c)?.label ?? c,
        closes: overlayCache[c],
        color: OVERLAY_COLORS[i % OVERLAY_COLORS.length],
      })),
    [overlayCodes, overlayCache],
  );

  const visibleBars = useMemo(() => {
    if (!replayActive || bars.length === 0) return bars;
    const n = Math.max(30, Math.round(replayCursor * bars.length));
    return bars.slice(0, n);
  }, [bars, replayActive, replayCursor]);

  const toggleSubplot = (sp: SubplotType) => {
    setConfig({
      ...config,
      subplots: config.subplots.includes(sp) ? config.subplots.filter(x => x !== sp) : [...config.subplots, sp],
    });
  };

  // 模型切换：重建分数序列（all=全部模型，否则单模型）
  // 选中模型不在 top3（活跃序列）时从全量 scoreByModel 构造，保证右侧必有分数线
  const activeScoreSeries = useMemo(() => {
    if (selectedModel === 'all') return scoreSeries;
    if (!scoreSeries.some(s => s.model === selectedModel)) {
      const pts = scoreByModel.get(selectedModel);
      if (pts?.length) {
        const idx = scoreModels.findIndex(m => m.model_id === selectedModel);
        return [{ model: selectedModel, color: SCORE_PALETTE[idx % SCORE_PALETTE.length], points: pts }];
      }
    }
    return scoreSeries.filter(s => s.model === selectedModel);
  }, [scoreSeries, selectedModel, scoreByModel, scoreModels]);

  // 选中模型在全量数据中缺失时（history 接口 DISTINCT ON 每天只留最新批次，部分模型被
  // 其它模型批次完全挤掉，但下拉来自全量表）按 model_id 单独请求该模型完整历史
  useEffect(() => {
    if (selectedModel === 'all' || scoreByModel.has(selectedModel)) return;
    let cancelled = false;
    import('../../../../services/modelTrainingService').then(({ modelTrainingService }) => {
      const code = stock.symbol.split('.')[0];
      return modelTrainingService.getStockInferenceHistory(code, 500, selectedModel).then(resp => {
        if (cancelled) return;
        const pts = (resp?.items ?? [])
          .filter(it => it.signal_model_id === selectedModel)
          .map(it => ({ date: it.trade_date.slice(0, 10), fusion: it.fusion_score, side: it.signal_side }))
          .sort((a, b) => a.date.localeCompare(b.date));
        if (!pts.length) return;
        if (scoreByModel.has(selectedModel)) return;
        const next = new Map(scoreByModel);
        next.set(selectedModel, pts);
        setScoreByModel(next);
      });
    }).catch(() => { /* 单模型数据缺失时保持无分数,不影响其它功能 */ });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedModel, stock.symbol, scoreByModel]);

  // 市值分档（与后端 model_training 同阈值：亿）
  const capTier = useMemo(() => {
    const mv = profile?.total_mv ?? stock.total_mv;
    if (mv == null) return '';
    if (mv < 30) return '微盘';
    if (mv < 100) return '小盘';
    if (mv < 300) return '中盘';
    if (mv < 1000) return '大盘';
    return '超大盘';
  }, [profile, stock]);

  // 当前模型分数序列（策略统计/提醒基于第一条活跃模型）
  const primaryScores = activeScoreSeries.length ? activeScoreSeries[0].points : [];

  // 策略提醒规则引擎（选股策略 v2.0，同 StockScoreChart）
  const strategyAlerts = useMemo<AlertPoint[]>(() => {
    const sorted = primaryScores
      .filter(p => p.fusion !== null && p.fusion !== undefined)
      .sort((a, b) => a.date.localeCompare(b.date));
    const board = stock.board || '';
    const isMainBoard = board.includes('主板');
    const negTag = (() => {
      const last = sorted[sorted.length - 1];
      return last && last.fusion !== null && last.fusion <= -0.20 ? '极端负分' : '';
    })();
    const out: AlertPoint[] = [];
    for (let i = 0; i < sorted.length; i++) {
      const sc = Number(sorted[i].fusion);
      const date = sorted[i].date;
      const prev = i > 0 ? Number(sorted[i - 1].fusion) : null;
      const next = i < sorted.length - 1 ? Number(sorted[i + 1].fusion) : null;
      // 第1组：分数区间
      if (sc >= 0.10 && sc < 0.12) {
        out.push({ date, severity: 'positive', message: isMainBoard ? '黄金买入区间（0.10-0.12·主板）' : '黄金区间（0.10-0.12）', score: sc });
      } else if (sc >= 0.12 && sc < 0.15) {
        out.push({ date, severity: 'warning', message: '可选但警惕追高（0.12-0.15）', score: sc });
      } else if (sc >= 0.15 && sc < 0.20) {
        out.push({ date, severity: 'warning', message: '高分谨慎区（0.15-0.20）', score: sc });
      } else if (sc >= 0.20) {
        out.push({ date, severity: 'danger', message: '极端高分，样本极少，勿追', score: sc });
      } else if (sc <= -0.20) {
        out.push({ date, severity: 'danger', message: '极端负分（≤-0.20）', score: sc });
      } else if (sc <= -0.15) {
        out.push({ date, severity: 'danger', message: '负分做空候选（≤-0.15）', score: sc });
      }
      // 第2组：3天趋势
      if (prev !== null && next !== null) {
        if (prev < sc && sc > next) out.push({ date, severity: 'positive', message: '先升后降·最佳买点', score: sc });
        else if (prev < sc && sc < next) out.push({ date, severity: 'warning', message: '连续上升·过热不追', score: sc });
        else if (prev > sc && sc > next) out.push({ date, severity: 'info', message: '连续下降·信号衰退', score: sc });
      }
      // 第3组：市值分档 + 负分
      if (sc <= -0.15 && capTier === '微盘') out.push({ date, severity: 'danger', message: '微盘+负分·做空首选（下跌概率68-72%）', score: sc });
      else if (sc <= -0.15 && capTier === '大盘') out.push({ date, severity: 'info', message: '大盘+负分·可能错杀，关注', score: sc });
      if (negTag === '极端负分' && capTier === '微盘') out.push({ date, severity: 'danger', message: '极端负分微盘·下跌概率77.7%', score: sc });
      // 第4组：板块过滤
      if (board.includes('科创') && sc >= 0.15) out.push({ date, severity: 'warning', message: '科创板高分不追（胜率仅47%）', score: sc });
      else if (board.includes('北交')) out.push({ date, severity: 'warning', message: '北交所排除·流动性差', score: sc });
      else if (sc >= 0.12 && sc < 0.20 && !isMainBoard) out.push({ date, severity: 'warning', message: `非主板高分（${board || '未知'}）·谨慎`, score: sc });
    }
    // 同日去重：保留 severity 最高的
    const rank = { danger: 3, warning: 2, positive: 1, info: 0 };
    const byDate = new Map<string, AlertPoint>();
    for (const a of out) {
      const ex = byDate.get(a.date);
      if (!ex || rank[a.severity] > rank[ex.severity]) byDate.set(a.date, a);
    }
    return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
  }, [primaryScores, stock, capTier]);

  // 策略汇总统计：黄金/危险/负分/买入信号 各多少天
  const strategySummary = useMemo(() => {
    const scored = primaryScores.filter(p => p.fusion !== null && p.fusion !== undefined);
    let golden = 0, danger = 0, buyPoint = 0, neg = 0;
    for (const p of scored) {
      const sc = Number(p.fusion);
      if (sc >= 0.10 && sc < 0.12) golden++;
      if (sc <= -0.15) neg++;
      if (sc <= -0.20 || sc >= 0.20) danger++;
      if (p.side?.toUpperCase() === 'BUY') buyPoint++;
    }
    return { total: scored.length, golden, danger, buyPoint, neg };
  }, [primaryScores]);

  // 当前分数 + 最近推理日
  const latestScore = primaryScores.length
    ? primaryScores.reduce((a, b) => (a.date >= b.date ? a : b))
    : null;

  // 当前排名：用最近推理日 + 所选模型查全市场列表取名次
  useEffect(() => {
    if (!latestScore) { setScoreRank(null); return; }
    let cancelled = false;
    stockTerminalService.getStockList({
      date: latestScore.date,
      model: selectedModel !== 'all' ? selectedModel : undefined,
      page_size: 5000,
    }).then(resp => {
      if (cancelled) return;
      const idx = resp.items.findIndex(it => it.symbol === stock.symbol);
      setScoreRank(idx >= 0 ? idx + 1 : null);
    }).catch(() => { if (!cancelled) setScoreRank(null); });
    return () => { cancelled = true; };
  }, [latestScore?.date, selectedModel, stock.symbol]); // eslint-disable-line react-hooks/exhaustive-deps

  // 模拟交易统计（持仓/已实现/浮动/总收益）
  const tradeStats = useMemo(() => {
    let shares = 0, cost = 0, realizedPnl = 0;
    for (const t of trades) {
      if (t.side === 'buy') { shares += t.shares; cost += t.price * t.shares; }
      else if (shares > 0) {
        const avgCost = cost / shares;
        realizedPnl += (t.price - avgCost) * t.shares;
        cost -= avgCost * t.shares;
        shares -= t.shares;
      }
    }
    const curPrice = bars.length ? bars[bars.length - 1].close : 0;
    const holdingValue = curPrice * shares;
    const unrealizedPnl = holdingValue - cost;
    const totalInvested = trades.filter(t => t.side === 'buy').reduce((s, t) => s + t.price * t.shares, 0);
    const pnl = realizedPnl + unrealizedPnl;
    const pnlPct = totalInvested > 0 ? (pnl / totalInvested) * 100 : 0;
    return { realizedPnl, unrealizedPnl, holdingValue, curPrice, remainingShares: shares, pnl, pnlPct };
  }, [trades, bars]);

  const doBuy = () => {
    if (!tradeModal) return;
    const { bar } = tradeModal;
    setTrades([...trades, { date: bar.date, side: 'buy', price: bar.open, shares: tradeShares }]);
    setTradeModal(null);
    message.success(`买入 ${tradeShares} 股 @ ${bar.open.toFixed(2)}`);
  };
  const doSell = () => {
    if (!tradeModal) return;
    const { bar } = tradeModal;
    if (tradeShares > tradeStats.remainingShares) { message.warning('持仓不足'); return; }
    setTrades([...trades, { date: bar.date, side: 'sell', price: bar.close, shares: tradeShares }]);
    setTradeModal(null);
    message.success(`卖出 ${tradeShares} 股 @ ${bar.close.toFixed(2)}`);
  };

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* 工具条 */}
      <div className="flex items-center justify-between gap-2 px-3 py-2 border-b border-slate-100 flex-wrap">
        <div className="flex items-center gap-2 min-w-0">
          {/* 上一股 / 下一股 / 搜索 */}
          <div className="flex items-center gap-0.5 shrink-0">
            <Tooltip title={navIndex > 0 ? `上一股：${navList[navIndex - 1]?.name ?? ''}` : '已是第一只'}>
              <Button size="small" type="text" disabled={navIndex <= 0} onClick={prevStock}
                icon={<ChevronLeft className="w-3.5 h-3.5" />} className="h-6 w-6 p-0" />
            </Tooltip>
            <Tooltip title={navIndex >= 0 && navIndex < navList.length - 1 ? `下一股：${navList[navIndex + 1]?.name ?? ''}` : '已是最后一只'}>
              <Button size="small" type="text" disabled={navIndex < 0 || navIndex >= navList.length - 1} onClick={nextStock}
                icon={<ChevronRight className="w-3.5 h-3.5" />} className="h-6 w-6 p-0" />
            </Tooltip>
          </div>
          <AutoComplete
            value={searchText}
            onChange={setSearchText}
            onSelect={(sym) => {
              const item = navList.find(it => it.symbol === sym);
              if (item) gotoStock(item);
              setSearchText('');
            }}
            options={searchOptions}
            size="small"
            style={{ width: 180 }}
            popupMatchSelectWidth={280}
          >
            <Input size="small" placeholder="搜索股票切换" prefix={<Search className="w-3 h-3 text-slate-300" />}
              className="rounded-lg text-[11px]" allowClear />
          </AutoComplete>
          <div className="w-6 h-6 rounded-lg bg-blue-50 border border-blue-100 flex items-center justify-center text-blue-600 shrink-0">
            <CandlestickChart className="w-3.5 h-3.5" />
          </div>
          {/* 名称后的空白区：信号 / 模型选择 / 模拟交易 / 参考线（弹窗已无标题，名称不重复显示） */}
          <div className="flex items-center gap-1.5 shrink-0">
            <button onClick={() => setSignalOn(!signalOn)} disabled={!signals.length}
              className={`flex items-center gap-1 text-[10px] font-bold px-2 py-1 rounded-lg transition-colors ${signalOn && signals.length ? 'bg-rose-50 text-rose-600 border border-rose-100' : 'text-slate-400 hover:text-slate-600 border border-transparent'}`}
              title={signals.length ? '模型推理分数信号' : '无推理信号'}>
              <TrendingUp className="w-3 h-3" /> 信号{signals.length > 0 && <span className="text-[9px] bg-rose-100 rounded px-0.5">{signals.length}</span>}
            </button>
            {scoreModels.length > 0 && (
              <Select
                value={selectedModel}
                onChange={setSelectedModel}
                size="small"
                style={{ width: 120 }}
                popupMatchSelectWidth={false}
                options={[
                  { value: 'all', label: '全部模型' },
                  ...scoreModels.map(m => ({ value: m.model_id, label: m.display_name || m.model_id })),
                ]}
              />
            )}
            <Tooltip title="点击 K 线日期模拟买卖">
              <Button size="small" onClick={() => message.info('点击 K 线图任意日期即可买入/卖出')}
                className="rounded-lg text-[10px] font-bold h-6 px-2 text-amber-600 border-amber-200">
                模拟交易
              </Button>
            </Tooltip>
            <Tooltip title="分数参考线管理">
              <Button size="small" onClick={() => setRefLineModal(true)}
                className="rounded-lg text-[10px] font-bold h-6 px-2 text-violet-600 border-violet-200">
                参考线
              </Button>
            </Tooltip>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="grid grid-cols-5 gap-0.5 p-0.5 bg-slate-100/70 rounded-lg shrink-0">
            {([['daily', '日'], ['weekly', '周'], ['monthly', '月'], ['min5', '5分'], ['min1', '1分']] as const).map(([v, label]) => (
              <button key={v} disabled={v === 'min1' && minAvail.min1 === false && period !== 'min1'}
                onClick={() => setPeriod(v)} title={v === 'min1' && minAvail.min1 === false ? '本地无1分钟数据' : undefined}
                className={`px-1.5 py-0.5 rounded-md text-[10px] font-bold transition-colors ${period === v ? 'bg-white text-blue-600 shadow-2xs' : 'text-slate-400 hover:text-slate-600'} disabled:text-slate-200`}>
                {label}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-1 bg-slate-50 border border-slate-200 rounded-lg p-0.5">
            {([
              ['MA', 'ma', Activity],
              ['BOLL', 'boll', Activity],
            ] as const).map(([label, key]) => (
              <button key={key} onClick={() => setConfig({ ...config, [key]: !config[key as 'ma' | 'boll'] })}
                className={`px-1.5 py-0.5 rounded-md text-[10px] font-bold transition-colors ${config[key as 'ma' | 'boll'] ? 'bg-white text-blue-600 shadow-2xs' : 'text-slate-400 hover:text-slate-600'}`}>
                {label}
              </button>
            ))}
            {(['vol', 'macd', 'kdj', 'rsi'] as SubplotType[]).map(sp => (
              <button key={sp} onClick={() => toggleSubplot(sp)}
                className={`px-1.5 py-0.5 rounded-md text-[10px] font-bold transition-colors ${config.subplots.includes(sp) ? 'bg-white text-blue-600 shadow-2xs' : 'text-slate-400 hover:text-slate-600'}`}>
                {SUBPLOT_META[sp]}
              </button>
            ))}
          </div>
          <Select mode="multiple" allowClear maxCount={4} placeholder="叠加指数" value={overlayCodes}
            onChange={setOverlayCodes} options={INDEX_OPTIONS} size="small" style={{ minWidth: 100 }} maxTagTextLength={4} popupMatchSelectWidth={false} />
          <Tooltip title={isWatched ? '移出自选' : '加入自选'}>
            <Button size="small" type="text" onClick={toggleWatch}
              icon={<Star className={`w-3.5 h-3.5 ${isWatched ? 'fill-amber-400 text-amber-400' : 'text-slate-400'}`} />} />
          </Tooltip>
          <Tooltip title="添加到模拟盘">
            <Button size="small" type="text" onClick={() => navigate('/trading', { state: { symbol: stock.symbol } })}
              icon={<ArrowLeftRight className="w-3.5 h-3.5 text-slate-500" />} />
          </Tooltip>
        </div>
      </div>

      {/* 第二行：策略回测 + 回放 */}
      <div className="flex items-center gap-2 px-3 py-1 border-b border-slate-100 shrink-0">
        <ChartBacktestPanel symbol={stock.symbol} onResult={setBtData} />
        <KlineReplay active={replayActive} onToggle={() => { setReplayActive(!replayActive); setReplayCursor(0.5); setReplayPlaying(false); }}
          cursor={replayCursor} onCursor={setReplayCursor} playing={replayPlaying} onPlaying={setReplayPlaying}
          speed={replaySpeed} onSpeed={setReplaySpeed} totalBars={bars.length} cursorIndex={visibleBars.length} />
      </div>

      {/* 信息条：当前分数 / 排名 / 策略统计 / 大盘状态（同推理研究 K 线） */}
      {(latestScore || indexStatus) && (
        <div className="flex items-center gap-2 px-3 py-1 border-b border-slate-100 bg-slate-50/40 flex-wrap text-[10px] shrink-0">
          {latestScore && (
            <>
              <span className="text-slate-400">当前排名</span>
              <b className="text-slate-800 font-mono">#{scoreRank ?? '--'}</b>
              <span className="text-slate-400">当前分数</span>
              <b className="font-mono text-indigo-600">{Number(latestScore.fusion).toFixed(4)}</b>
              <span className="text-slate-300">|</span>
              <Tag color="green" className="m-0 rounded-full text-[9px] font-bold px-2">黄金区间 {strategySummary.golden}天</Tag>
              <Tag color="volcano" className="m-0 rounded-full text-[9px] font-bold px-2">危险分 {strategySummary.danger}天</Tag>
              <Tag color="red" className="m-0 rounded-full text-[9px] font-bold px-2">负分 {strategySummary.neg}天</Tag>
              <Tag color="blue" className="m-0 rounded-full text-[9px] font-bold px-2">买入信号 {strategySummary.buyPoint}天</Tag>
              <span className="text-slate-500">共 <b className="text-slate-700">{strategySummary.total}</b> 推理日</span>
            </>
          )}
          {indexStatus && (
            <>
              <span className="text-slate-300">|</span>
              <span className={indexStatus.below ? 'text-emerald-600 font-bold' : 'text-rose-600 font-bold'}>
                {indexStatus.below ? '📉 大盘空' : '📈 大盘多'}
              </span>
              <span className="text-slate-500 font-mono">上证{indexStatus.latestClose} / MA20 {indexStatus.ma20}</span>
            </>
          )}
          {strategyAlerts.length > 0 && (
            <Tooltip title={strategyAlerts.slice(-8).reverse().map(a => `${a.date} ${a.message}`).join('\n')}>
              <Tag color="purple" className="m-0 rounded-full text-[9px] font-bold px-2 cursor-pointer">策略提醒 {strategyAlerts.length}条</Tag>
            </Tooltip>
          )}
        </div>
      )}

      {/* 图表 */}
      <div className="flex-1 min-h-0">
        {loadingKline ? (
          <div className="h-full flex items-center justify-center text-[11px] text-slate-400 gap-2">
            <TrendingUp className="w-4 h-4 animate-pulse text-blue-400" /> 加载 K 线数据…
          </div>
        ) : bars.length ? (
          <KlineChart bars={visibleBars} config={config} overlays={overlays} height={height - 30} signals={signalOn ? signals : []} btEquity={btData?.points ?? []} scoreSeries={activeScoreSeries} alerts={strategyAlerts} trades={trades} refLines={refLines} onBarClick={(bar) => setTradeModal({ bar })} />
        ) : (
          <div className="h-full flex flex-col items-center justify-center gap-2 text-slate-400">
            <Activity className="w-8 h-8 opacity-40" />
            <span className="text-[11px]">暂无 K 线数据</span>
          </div>
        )}
      </div>

      {/* 模拟交易统计条 */}
      {trades.length > 0 && (
        <div className="flex items-center gap-3 px-3 py-1 border-t border-slate-100 text-[10px] font-mono text-slate-600 bg-slate-50/60 shrink-0">
          <span>持仓 <b className="text-slate-800">{tradeStats.remainingShares}</b> 股</span>
          {tradeStats.remainingShares > 0 && (
            <span>市值 <b className="text-slate-800">{tradeStats.holdingValue.toFixed(2)}</b>（现价 {tradeStats.curPrice.toFixed(2)}）</span>
          )}
          <span>已实现 <b className={tradeStats.realizedPnl >= 0 ? 'text-rose-600' : 'text-emerald-600'}>{tradeStats.realizedPnl >= 0 ? '+' : ''}{tradeStats.realizedPnl.toFixed(2)}</b></span>
          {tradeStats.remainingShares > 0 && (
            <span>浮动 <b className={tradeStats.unrealizedPnl >= 0 ? 'text-rose-600' : 'text-emerald-600'}>{tradeStats.unrealizedPnl >= 0 ? '+' : ''}{tradeStats.unrealizedPnl.toFixed(2)}</b></span>
          )}
          <span>总收益 <b className={tradeStats.pnl >= 0 ? 'text-rose-600' : 'text-emerald-600'}>{tradeStats.pnl >= 0 ? '+' : ''}{tradeStats.pnl.toFixed(2)} ({tradeStats.pnlPct >= 0 ? '+' : ''}{tradeStats.pnlPct.toFixed(2)}%)</b></span>
        </div>
      )}

      {/* 模拟交易 Modal */}
      <Modal
        open={!!tradeModal}
        onCancel={() => setTradeModal(null)}
        footer={null}
        width={360}
        title={<span className="text-sm font-black text-slate-800">模拟交易 · {tradeModal?.bar.date}</span>}
      >
        {tradeModal && (
          <div className="flex flex-col gap-3">
            <div className="text-[11px] text-slate-500">
              开盘 <b className="font-mono text-slate-800">{tradeModal.bar.open.toFixed(2)}</b>
              {'  '}收盘 <b className="font-mono text-slate-800">{tradeModal.bar.close.toFixed(2)}</b>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-[11px] font-bold text-slate-500">数量</span>
              <InputNumber value={tradeShares} onChange={(v) => setTradeShares(v ?? 100)} min={100} step={100} size="small" style={{ flex: 1 }} />
            </div>
            <div className="flex gap-2">
              <Button type="primary" size="small" className="flex-1 bg-rose-500" onClick={doBuy}>买入（开盘价）</Button>
              <Button danger size="small" className="flex-1" onClick={doSell}>卖出（收盘价）</Button>
            </div>
          </div>
        )}
      </Modal>

      {/* 参考线管理 Modal */}
      <Modal
        open={refLineModal}
        onCancel={() => setRefLineModal(false)}
        footer={null}
        width={420}
        title={<span className="text-sm font-black text-slate-800">分数参考线</span>}
      >
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <InputNumber value={newRefLine.value} onChange={(v) => setNewRefLine({ ...newRefLine, value: v ?? 0 })}
              size="small" step={0.05} style={{ width: 90 }} />
            <Input placeholder="标签（如 可买/热门/危险）" value={newRefLine.label} size="small"
              onChange={(e) => setNewRefLine({ ...newRefLine, label: e.target.value })} style={{ flex: 1 }} />
            <Select value={newRefLine.color} onChange={(c) => setNewRefLine({ ...newRefLine, color: c })} size="small"
              options={[
                { value: '#10b981', label: '绿·可买' },
                { value: '#f59e0b', label: '橙·谨慎' },
                { value: '#ef4444', label: '红·危险' },
                { value: '#6366f1', label: '紫·提示' },
              ]} style={{ width: 100 }} />
          </div>
          <Button size="small" type="primary" onClick={() => {
            saveRefLines([...refLines, { id: `rl-${Date.now()}`, value: newRefLine.value, label: newRefLine.label, color: newRefLine.color }]);
          }}>添加参考线</Button>
          {refLines.map(l => (
            <div key={l.id} className="flex items-center gap-2 text-[11px]">
              <Switch size="small" checked={l.visible !== false}
                onChange={(v) => saveRefLines(refLines.map(x => x.id === l.id ? { ...x, visible: v } : x))} />
              <span className="w-3 h-3 rounded-full" style={{ background: l.color }} />
              <span className="font-mono font-bold text-slate-700">{l.label} {l.value >= 0 ? '+' : ''}{l.value.toFixed(2)}</span>
              <Button size="small" type="text" danger className="ml-auto p-0 h-6 w-6"
                onClick={() => saveRefLines(refLines.filter(x => x.id !== l.id))}>删</Button>
            </div>
          ))}
        </div>
      </Modal>
    </div>
  );
}

/** 日线重采样为周/月线 */
function resampleBars(bars: KlineBar[], period: 'weekly' | 'monthly'): KlineBar[] {
  const map = new Map<string, KlineBar[]>();
  for (const b of bars) {
    const key = period === 'weekly' ? weekKey(b.date) : b.date.slice(0, 7);
    if (!map.has(key)) map.set(key, []);
    map.get(key)!.push(b);
  }
  return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([, grp]) => ({
    date: period === 'weekly' ? grp[grp.length - 1].date : `${grp[0].date.slice(0, 7)}-月末`,
    open: grp[0].open,
    high: Math.max(...grp.map(g => g.high)),
    low: Math.min(...grp.map(g => g.low)),
    close: grp[grp.length - 1].close,
    volume: grp.reduce((s, g) => s + (g.volume ?? 0), 0),
  }));
}
function weekKey(date: string): string {
  const d = new Date(date + 'T00:00:00');
  const day = (d.getDay() + 6) % 7;
  d.setDate(d.getDate() - day);
  return d.toISOString().slice(0, 10);
}
