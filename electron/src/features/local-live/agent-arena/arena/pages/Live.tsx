import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from '../arenaRouter';
import {
  BenchPoint,
  LogLine,
  MarketId,
  OverviewRow,
  PositionRecord,
  RealAccountChannel,
  TradeRecord,
  fetchBenchmark,
  fetchFutuAccountBoth,
  fetchIndices,
  fetchLiveAccountFor,
  fetchLiveEquity,
  fetchLiveLedger,
  fetchLiveTradesFor,
  fetchLogs,
  fetchOverview,
  fetchTokenUsage,
  triggerLiveAnalysis,
  fetchPerformance,
  fetchPositions,
  fetchPrices,
  fetchRealAccount,
  fetchStockNames,
  fetchTrades,
  marketMeta,
  triggerNewsAnalysis,
} from '../api/client';
import { usePolling } from '../hooks/usePolling';
import EquityChart, { toBenchLine, toChartLine, HoldingSpan, ChartLine } from '../components/EquityChart';
import RealAccountPanel from '../components/RealAccountPanel';
import HkOrderPanel from '../../hk-order-panel/HkOrderPanel';
import ModelCard, { modelColor, shortName } from '../components/ModelCard';
import ChatStream from '../components/ChatStream';
import NewsStream from '../components/NewsStream';
import NewsAgentChat, { NEWS_AGENTS } from '../components/NewsAgentChat';
import CompletedFeed from '../components/CompletedFeed';
import CompConfigPanel from '../components/CompConfigPanel';
import LiveDetails from '../components/LiveDetails';
import { MarketSwitcher } from '../components/Navbar';
import { fmtMoney, fmtPct, fmtPrice, pnlClass } from '../utils/format';
import { stockLabel, stockName } from '../utils/symbols';
import { toLiveAdjust, toLiveFill } from '../utils/liveFills';
// T6-3：账户快照时刻统一北京口径（aware 换算 / naive 原样）
import { fmtBeijingDateTime } from '../../../../../utils/timeBeijing';
import { displayAgentName, rankPerformers } from '../utils/agents';
import { dayHit, dayOf, dayOptions } from '../utils/dayFilter';
import { deriveBuyTimes } from '../utils/buyTime';
import './Live.css';
import { asUpdater } from '../reactCompat';

const BENCH_COLOR = '#10a37f';

/** 对话日志每次只拉最近 N 个回合：单条 ~6KB，全量可累积到 1MB+，
 *  而新回合每小时才产生一次——全量轮询是页面周期性卡顿的主因（2026-09-08）。 */
const LOG_LIMIT = 80;
/** 对话日志轮询间隔：一个回合/小时/模型，30s 轮一遍纯属浪费（每轮 ~1.3MB）。
 *  手动「立即分析」触发后会单独 refresh，不依赖这个节奏。 */
const LOG_POLL_MS = 120000;

/** 空仓时间段反推（成交事件 + 当前账本，时间戳毫秒；纯事实驱动——
 *  净值曲线本身是阶梯状（桥行情缓存分钟级刷新），按数值连段判空仓会整线误虚。
 *  账本只存当前快照，用成交事件倒走重建：穿越一笔清仓卖出 → 空仓区间开始；
 *  穿越一笔买入 → 空仓区间结束。无任何事件且现空仓 → 视为自数据起点空仓。 */
function emptyTsIntervals(
  events: { ts: string; agent?: string | null; code?: string; side?: string; volume?: number }[],
  ledgerAgents: Record<string, { positions?: { code: string; volume: number }[] }>,
): Record<string, [number, number][]> {
  const out: Record<string, [number, number][]> = {};
  const nowTs = Date.now();
  for (const [agent, rec] of Object.entries(ledgerAgents ?? {})) {
    const qty = new Map<string, number>();
    for (const p of rec?.positions ?? []) qty.set(p.code, p.volume);
    const total = () => [...qty.values()].reduce((s, v) => s + v, 0);
    const evts = (events ?? [])
      .filter((e) => e.agent === agent && e.side && Number(e.volume) > 0)
      .sort((a, b) => (a.ts < b.ts ? 1 : -1));
    if (!evts.length) {
      // 无成交事件：持仓状态即当前账本。空仓起点交给 trailingFlatFrom 兜底
      // （净值最后变动时刻），此处不制造区间
      continue;
    }
    const intervals: [number, number][] = [];
    let emptyEnd: number | null = null;
    for (const e of evts) {
      const t = new Date(e.ts).getTime();
      const stateAfter = total() > 0;
      const code = e.code ?? '';
      const vol = Number(e.volume) || 0;
      if (String(e.side).toLowerCase() === 'sell') qty.set(code, (qty.get(code) ?? 0) + vol);
      else qty.set(code, Math.max(0, (qty.get(code) ?? 0) - vol));
      const stateBefore = total() > 0;
      if (!stateAfter && stateBefore && code) {
        // 倒走穿越清仓卖出：此刻（正向）刚卖光 → 空仓区间起点
        intervals.push([t, emptyEnd ?? nowTs]);
      } else if (stateAfter && !stateBefore) {
        // 倒走穿越买入：此刻（正向）刚买回 → 空仓区间终点
        emptyEnd = t;
      }
    }
    if (total() === 0 && intervals.length === 0) {
      // 倒走到底仍空仓（历史无买入记录）→ 自数据起点空仓
      intervals.push([0, emptyEnd ?? nowTs]);
    }
    out[agent] = intervals;
  }
  return out;
}

/** 空仓时间区间 → 该 agent 净值序列中需画虚线的下标段（含两端）。
 *  groups 支持多组区间取并集（事件反推 + 空仓尾段兜底）。 */
function tsToDashSegs(
  pts: { t: number }[],
  groups: [number, number][][],
): [number, number][] {
  const all = groups.flat();
  if (!all.length) return [];
  const segs: [number, number][] = [];
  let cur: [number, number] | null = null;
  pts.forEach((p, idx) => {
    const inEmpty = all.some(([a, b]) => p.t >= a && p.t <= b);
    if (inEmpty && !cur) cur = [idx, idx];
    else if (inEmpty && cur) cur[1] = idx;
    else if (!inEmpty && cur) {
      segs.push(cur);
      cur = null;
    }
  });
  if (cur) segs.push(cur);
  return segs;
}

/** 净值序列中最后一个值变动点的下标（其后全部同值）→ 空仓尾段的起始。
 *  事件缺失（旧路径成交未留 fill 记录）时用净值本身的变动史兜底：
 *  当前空仓 → 从最后一次变动起虚线（变动前是有仓位的实线）。 */
function trailingFlatFrom(vals: number[]): number {
  for (let i = vals.length - 1; i >= 1; i--) {
    if (Math.abs(vals[i] - vals[i - 1]) > 1e-9) return i;
  }
  return 0;
}

/** 持仓时间线（悬停补充）：按 agent/代码 从当前账本出发倒走成交事件，
 *  重建每段持仓的 (from,to] 毫秒区间与数量；当前持仓以 buy_ts 为起点，
 *  已清仓的股票区间只由事件支撑，历史未知段不臆造。 */
function holdingsTimelineOf(
  events: { ts: string; agent?: string | null; side?: string; volume?: number; code?: string }[],
  ledgerAgents: Record<string, { positions?: { code: string; volume: number }[] }>,
): HoldingSpan[] {
  const spans: HoldingSpan[] = [];
  const now = Date.now();
  for (const [agent, rec] of Object.entries(ledgerAgents ?? {})) {
    const pos = rec?.positions ?? [];
    const qty = new Map<string, number>();
    const buyTs = new Map<string, number>();
    for (const p of pos) {
      qty.set(p.code, p.volume);
      const bt = new Date((p as { buy_ts?: string }).buy_ts ?? '').getTime();
      buyTs.set(p.code, Number.isFinite(bt) ? bt : 0);
    }
    const evts = (events ?? [])
      .filter((e) => e.agent === agent && e.side && Number(e.volume) > 0 && e.code)
      .sort((a, b) => (a.ts < b.ts ? 1 : -1));
    let prevT = now;
    for (const e of evts) {
      const t = new Date(e.ts).getTime();
      const code = e.code ?? '';
      const v = qty.get(code) ?? 0;
      if (t < prevT && v > 0) spans.push({ agent, code, vol: v, from: t, to: prevT });
      // 倒走复原：卖出前持有更多，买入前持有更少
      if (String(e.side).toLowerCase() === 'sell') qty.set(code, v + (Number(e.volume) || 0));
      else qty.set(code, Math.max(0, v - (Number(e.volume) || 0)));
      prevT = t;
    }
    // 事件之前仍持有的（当前账本有 buy_ts）→ 从买入时刻起
    for (const [code, v] of qty) {
      if (v <= 0) continue;
      const from = buyTs.get(code);
      spans.push({ agent, code, vol: v, from: from ?? 0, to: prevT });
    }
  }
  return spans;
}

/** 每秒自走北京时间钟——独立 state，不波及 Live 父组件的轮询/memo。 */
function LiveClock() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const s = new Date(now + 8 * 3600000).toISOString();
  return <span className="live-clock">{s.slice(0, 10)} {s.slice(11, 19)}</span>;
}

type Tab = 'completed' | 'trades' | 'chat' | 'news' | 'positions' | 'comp' | 'real' | 'details';
type TimeRange = 'all' | '5d';

/** 右侧 tab：按语义分 4 组（决策流 / 交易事实 / 情报 / 系统），组间加分隔线。
 *  默认停在「模型对话」，故排第一；组内顺序=常用度。 */
const TAB_GROUPS: { id: Tab; label: string }[][] = [
  [{ id: 'chat', label: '模型对话' }],
  [
    { id: 'completed', label: '已完成' },
    { id: 'trades', label: '成交' },
    { id: 'positions', label: '持仓' },
    { id: 'real', label: '实盘' },
  ],
  [{ id: 'news', label: '新闻' }],
  [
    { id: 'comp', label: '比赛配置' },
    { id: 'details', label: '详情' },
  ],
];

interface TradeEvt {
  date: string;
  side: 'buy' | 'sell';
  symbol: string;
  name: string;
  amount: number;
  cash: number;
  price: number | null;
  notional: number | null;
  agent?: string | null; // 模拟盘成交归属（all 视图多模型聚合时标注）
}

// ---------- 市场交易时段（北京时间）与交易规则 ----------

/** 美股交易时段随夏令时切换（美国 3 月第二个周日 ~ 11 月第一个周日）。 */
const usDstActive = (d: Date): boolean => {
  const y = d.getFullYear();
  const secondSun = (m: number) => {
    const x = new Date(y, m, 1);
    while (x.getDay() !== 0) x.setDate(x.getDate() + 1);
    x.setDate(x.getDate() + 7);
    return x;
  };
  return d >= secondSun(2) && d < secondSun(10);
};

const MARKET_HOURS: Record<MarketId, { rule: string }> = {
  cn: { rule: 'T+1 · 主板 ±10% 涨跌停' },
  hk: { rule: 'T+0 · 无涨跌停' },
  us: { rule: 'T+0 · 无涨跌停' },
};

/** 交易时段标签（北京时间）：US 按当天是否夏令时切换。 */
const hoursLabelOf = (market: MarketId, now: Date): string => {
  if (market === 'cn') return '09:30–11:30 / 13:00–15:00';
  if (market === 'hk') return '09:30–12:00 / 13:00–16:00';
  return usDstActive(now) ? '夏令时 21:30–04:00(次日)' : '冬令时 22:30–05:00(次日)';
};

/** 当前盘中状态（北京时间）：盘前/盘中/中午休息/盘后/休市。 */
const marketStatusOf = (market: MarketId, now: Date): { text: string; open: boolean } => {
  const bj = new Date(now.getTime() + (480 + now.getTimezoneOffset()) * 60000); // 东八区（tzOffset 东负西正，如 JST=-540 → -60min）
  const mins = bj.getHours() * 60 + bj.getMinutes();
  const wd = bj.getDay();
  if (wd === 0 || wd === 6) return { text: '休市', open: false };
  const inRange = (a: number, b: number) => mins >= a && mins < b;
  if (market === 'cn') {
    if (inRange(9 * 60, 9 * 60 + 30)) return { text: '盘前', open: false }; // 集合竞价 9:15 前也算盘前
    if (inRange(9 * 60 + 30, 11 * 60 + 30) || inRange(13 * 60, 15 * 60)) return { text: '盘中', open: true };
    if (inRange(11 * 60 + 30, 13 * 60)) return { text: '中午休息', open: false };
    return { text: '盘后', open: false };
  }
  if (market === 'hk') {
    if (inRange(9 * 60, 9 * 60 + 30)) return { text: '盘前', open: false }; // 开市前竞价 9:00-9:30
    if (inRange(9 * 60 + 30, 12 * 60) || inRange(13 * 60, 16 * 60)) return { text: '盘中', open: true };
    if (inRange(12 * 60, 13 * 60)) return { text: '中午休息', open: false };
    return { text: '盘后', open: false };
  }
  // US（北京时间）：盘前 = 美东 04:00–09:30
  const preOpen = usDstActive(bj) ? 16 * 60 : 15 * 60; // 夏令时 16:00 / 冬令时 15:00
  const openAt = usDstActive(bj) ? 21 * 60 + 30 : 22 * 60 + 30;
  const closeAt = usDstActive(bj) ? 4 * 60 : 5 * 60; // 次日凌晨（北京时间）
  if (mins >= openAt || mins < closeAt) return { text: '盘中', open: true };
  if (mins >= preOpen && mins < openAt) return { text: '盘前', open: false };
  return { text: '盘后', open: false };
};

const benchLabelOf = (market: MarketId): string =>
  market === 'us' ? 'NDX100' : market === 'cn' ? 'SSE50' : 'HSI';

/** 成交卡一行：真实成交（kind 缺省）或人工对账（kind='adjust'，fill_adjust） */
type LiveCardRow = {
  kind?: 'adjust';
  ts: string;
  code: string;
  side?: string | null;
  volume: number;
  price: number | null;
  name: string;
  agent: string | null;
  note?: string;
};

/** Live 终端页 —— 终端风布局：
 *  顶部价格条 + HIGHEST/LOWEST → 左净值图 + 模型横排卡 → 右 540px 七 tab 面板 */
export default function Live() {
  const [params, setParams] = useSearchParams();
  const rawMarket = params.get('market');
  const market: MarketId = (['cn', 'hk', 'us'] as MarketId[]).includes(rawMarket as MarketId)
    ? (rawMarket as MarketId)
    : 'cn';
  const switchMarket = useCallback(
    (m: MarketId) => setParams({ market: m }, { replace: true }),
    [setParams],
  );
  const meta = marketMeta(market);

  const [tab, setTab] = useState<Tab>('chat'); // 默认=模型对话（用户口径）
  // A 股实盘通道（通达信桥 / 迅投 QMT）：父层持有，切 tab 回来不丢选择
  const [realChannel, setRealChannel] = useState<RealAccountChannel>('tdx');
  const [chartRange, setChartRange] = useState<TimeRange>('all');
  const [chartMode, setChartMode] = useState<'pct' | 'dollar'>('pct');
  const [selectedModel, setSelectedModel] = useState<string>('all');
  const [newsAgent, setNewsAgent] = useState<string>('all');
  const [newsMsg, setNewsMsg] = useState<string>('');
  // 「立即分析」手动触发状态（对话 tab 筛选栏按钮）
  const [analyzeState, setAnalyzeState] = useState<'idle' | 'busy' | 'sent' | 'error'>('idle');
  const [analyzeMsg, setAnalyzeMsg] = useState('');
  const runManualAnalysis = async () => {
    setAnalyzeState('busy');
    setAnalyzeMsg('');
    try {
      await triggerLiveAnalysis(selectedModel === 'all' ? 'all' : [selectedModel]);
      setAnalyzeMsg(
        `已触发${selectedModel === 'all' ? '全部分账模型' : ` ${selectedModel}`}分析，约 1 分钟内开跑`,
      );
      setAnalyzeState('sent');
      window.setTimeout(() => setAnalyzeState('idle'), 25000);
      // 日志轮询已放宽到 2 分钟（LOG_POLL_MS）：手动触发后补一次拉取，
      // 让新回合 ~90s 内出现，不必等下一个轮询周期。
      window.setTimeout(() => {
        void logs.refresh();
        void chatAll.refresh();
      }, 90000);
    } catch (e) {
      setAnalyzeMsg(`触发失败：${e instanceof Error ? e.message : String(e)}`);
      setAnalyzeState('error');
    }
  };
  const [completedCount, setCompletedCount] = useState(0);
  // 「日期」筛选：已完成 / 成交 / 持仓 三 tab 共用（'all' = 全部日期，不筛）
  const [dateFilter, setDateFilter] = useState<string>('all');
  const [completedDates, setCompletedDates] = useState<string[]>([]); // 已完成 feed 回传的候选平仓日期
  // 候选日期回传只在内容变化时落 state（每轮渲染都是新数组，直接 set 会反复重渲）。
  // 不做「切市场即清空」：父 effect 在子 effect 之后执行，会把 feed 刚回传的新日期擦掉，
  // 内容串没再变就永不重发（子先父后的 effect 顺序竞争）；旧值由 feed 重新回传覆盖。
  const onCompletedDates = useCallback((days: string[]) => {
    setCompletedDates(asUpdater((prev) =>
      prev.length === days.length && prev.every((d, i) => d === days[i]) ? prev : days,
    ));
  }, []);
  /** 北京时间的今天（日期下拉的「当年」判断用） */
  const todayCn = new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);

  // 总控聚合（三市场一次拉取）
  const overview = usePolling(() => fetchOverview(), [], 30000, 0);
  const rows: OverviewRow[] = useMemo(
    () => overview.data?.markets[market] ?? [],
    [overview.data, market],
  );
  const agentsKey = rows.map((r) => r.name).join('|');

  // 基准指数（US=等权 NDX100 / CN=SSE50 / HK 暂无）
  const bench = usePolling(() => fetchBenchmark(market), [market], 300000);
  // 当日实时指数（顶部行情条）：CN 桥日K 6 指数 / US NDX100 基准 / HK 空
  const indices = usePolling(() => fetchIndices(market), [market], 30000, 4000);

  // 当前市场全部 agent 净值序列
  const perfs = usePolling(
    () =>
      Promise.all(
        rows.map((r) => fetchPerformance(r.name, market).catch(() => null)),
      ).then((list) => list.filter(Boolean) as NonNullable<Awaited<ReturnType<typeof fetchPerformance>>>[]),
    [market, agentsKey],
    30000,
  );

  // 实盘账户净值（A股：每分钟采样，前端 20s 轮询尽量实时）
  // 首轮不延后（phase=0）：它是净值图的数据源，延后 15s 会让图表先画一屏模拟盘
  // 再换源（用户口径「刷新后图表先是乱的」）。其余端点错峰即可。
  const liveEquity = usePolling(() => fetchLiveEquity(), [], 60000, 0);
  // 实盘数据成功过一次后保留最后一帧：单轮轮询失败/延迟时仍画实盘旧帧，
  // 不再整图回退到模拟盘序列（启动后换源闪变的根因之一）
  const liveEqRef = useRef(liveEquity.data);
  useEffect(() => {
    if (liveEquity.data) liveEqRef.current = liveEquity.data;
  }, [liveEquity.data]);
  const liveEq = liveEquity.data ?? liveEqRef.current;
  // 最近一次成功绘制的实盘线帧：轮询空 payload/失败期间保持旧帧，不回退 perfs 模拟盘
  const liveLinesRef = useRef<ChartLine[] | null>(null);
  // 实盘 LLM 分析 token 累计（30s 刷新，模型卡显示）
  const tokenUsage = usePolling(() => fetchTokenUsage(), [], 30000, 8000);
  // 实盘账本/成交（上移：空仓段反推在 lines memo 里要用）
  // 账本/成交同样 phase=0：它们是 cn 净值图的虚线/事件标注数据源，错峰反而让首屏
  // 先画一条没有事件标注的线、再补上（2026-09-08）。
  const liveLedger = usePolling(() => fetchLiveLedger(), [], 30000, 0);
  const liveTrades = usePolling(() => fetchLiveTradesFor(market), [market], 30000, 0);

  const lines = useMemo(() => {
    const eq = liveEq;
    // 实盘采样必须用完整时间戳 ts（含时刻），date 只是 YYYY-MM-DD 会把当天所有点挤到零点
    const toEq = (v: number, ts: string) => ({ date: ts, cash: 0, market_value: 0, equity: v });
    // A股实盘优先：每 agent 分账虚拟净值线（¥10 万起，通达信桥实时价）
    // + 总账户线（桥实时总资产）。序列 ≥2 点才画。
    if (market === 'cn' && eq) {
      // 空仓段虚线：成交事件反推 + （无事件时）净值最后变动点兜底
      const emptyTs = emptyTsIntervals(
        (liveTrades.data ?? []) as unknown as Parameters<typeof emptyTsIntervals>[0],
        (liveLedger.data?.agents ?? {}) as unknown as Parameters<typeof emptyTsIntervals>[1],
      );
      const emptyNowAgents = new Set(
        Object.entries(liveLedger.data?.agents ?? {})
          .filter(([, rec]) => !(rec?.positions ?? []).length)
          .map(([a]) => a),
      );
      // 成交标记（▲买/▼卖）：与空仓段反推同源同筛选（side+volume>0），
      // 标记点即「虚线↔实线」的界点——全览 900 点里今天的买回只有几像素宽，
      // 靠标记才能一眼看见（用户 2026-09-11）。
      const fillsOfAgent = (agent: string) => {
        const out: { t: number; side?: string; kind?: 'adjust'; note?: string }[] = [];
        for (const rec of liveTrades.data ?? []) {
          if (rec.agent !== agent) continue;
          const f = toLiveFill(rec);
          if (f) {
            const t = new Date(f.ts).getTime();
            if (Number.isFinite(t)) out.push({ t, side: f.side });
            continue;
          }
          // 对账行（fill_adjust）：菱形标记 + 说明，让「误卖→归还」在图上可见
          // （2026-09-08 实录：pro 误卖 flash 的 688183，13:24 对账归回）
          const adj = toLiveAdjust(rec);
          if (adj) {
            const t = new Date(adj.ts).getTime();
            if (Number.isFinite(t)) out.push({ t, kind: 'adjust', note: adj.note });
          }
        }
        return out.sort((a, b) => a.t - b.t);
      };
      // 分账线：仅空仓那一段虚线（保留信息量），持仓段一律实线
      const agentLines = Object.entries(eq.agents ?? {})
        .filter(([, pts]) => pts.length >= 2)
        .map(([name, pts]) => {
          const line = toChartLine(
            `live-${name}`,
            name,
            modelColor(name),
            pts.map((e) => toEq(e.value, e.ts)),
          );
          const groups: [number, number][][] = [emptyTs[name] ?? []];
          if (emptyNowAgents.has(name)) {
            // 现空仓且无（或已有）事件：净值最后变动点之后 = 空仓尾段。
            // 下标必须在降采样后的 line.points（≤900）上找——line.points 已按
            // MAX_CHART_POINTS 抽稀，拿全量 pts 的下标会越界取 undefined.t
            // → render 抛异常整页白屏（2026-09-08 事故：净值 1484 点 > 900）
            const from = trailingFlatFrom(line.points.map((p) => p.v));
            groups.push([[line.points[from].t, Date.now()]]);
          }
          return {
            ...line,
            notional: 100000, // 分账名义基准: hover 换算金额盈亏
            dashSegs: tsToDashSegs(line.points, groups),
            fills: fillsOfAgent(name),
          };
        });
      // 总账户线（¥92.5 万量级）只兜底：没有任何分账线可画时才显示。
      // 用户口径 = 分账 ¥10 万，总账户已买很多、与 10 万不具可比性。
      const totalLine =
        agentLines.length === 0 && (eq.total ?? []).length >= 2
          ? toChartLine(
              'live-total',
              '总账户',
              '#999',
              eq.total.map((e) => toEq(e.value, e.ts)),
            )
          : null;
      if (agentLines.length || totalLine) {
        const liveLines = [...agentLines, ...(totalLine ? [totalLine] : [])];
        liveLinesRef.current = liveLines; // 留存本帧: 空档期(空仓休息/盘中故障)保持展示
        return liveLines;
      }
    }
    // 曾成功画过实盘帧 → 空 payload/请求失败期间保持旧帧（此时后端常回空 agents/total，
    // 直接回退 perfs 会让图上内容整图换源闪变）。从未有过实盘帧才走模拟盘兜底。
    if (market === 'cn' && liveLinesRef.current) return liveLinesRef.current;
    return (perfs.data ?? []).map((p) =>
      toChartLine(p.agent, p.agent, modelColor(p.agent), p.points),
    );
  }, [perfs.data, liveEquity.data, liveLedger.data, liveTrades.data, market]);

  // 悬停补充（时序事实）：当时持仓时间线 + 成交事件（仅 cn 实盘数据可支撑）
  const heldSpans = useMemo(
    () =>
      market === 'cn'
        ? holdingsTimelineOf(
            liveTrades.data as unknown as Parameters<typeof holdingsTimelineOf>[0],
            liveLedger.data?.agents as unknown as Parameters<typeof holdingsTimelineOf>[1],
          )
        : [],
    [market, liveTrades.data, liveLedger.data],
  );
  // 当前单价（持仓金额估算用）：账本 position_value/volume（桥实时价口径）
  const holderPriceMap = useMemo(() => {
    const m: Record<string, number> = {};
    for (const rec of Object.values(liveLedger.data?.agents ?? {})) {
      for (const p of (rec as { positions?: { code: string; volume: number; position_value?: number }[] }).positions ?? []) {
        if (p.position_value != null && p.volume > 0) {
          m[p.code] = Number(p.position_value) / p.volume;
        }
      }
    }
    return m;
  }, [liveLedger.data]);

  // 实盘 5 分钟净值模式（CN 有实盘点）：不画基准线——SSE50 日线会把时间轴拉到 8 月初
  const hasLiveLine = useMemo(() => {
    const eq = liveEq;
    if (market !== 'cn' || !eq) return false;
    if ((eq.total ?? []).length >= 2) return true;
    return Object.values(eq.agents ?? {}).some((pts) => pts.length >= 2);
  }, [market, liveEq]);

  const benchLine = useMemo(
    () =>
      !hasLiveLine && bench.data && bench.data.length
        ? toBenchLine(benchLabelOf(market), BENCH_COLOR, bench.data)
        : null,
    [bench.data, market, hasLiveLine],
  );

  // 右侧面板数据源（FILTER 选中模型；'all' → 第一个 agent）
  const effectiveModel = selectedModel === 'all' ? (rows[0]?.name ?? null) : selectedModel;

  const positions = usePolling<PositionRecord[]>(
    () => (effectiveModel ? fetchPositions(effectiveModel, market) : Promise.resolve([])),
    [effectiveModel, market],
    30000,
  );
  const trades = usePolling<TradeRecord[]>(
    () =>
      effectiveModel
        ? selectedModel === 'all'
          ? Promise.all(
              rows.map((r) =>
                fetchTrades(r.name, market).catch(() => [] as TradeRecord[]),
              ),
            ).then((lists) =>
              lists.flatMap((list, i) =>
                list.map((t) => ({ ...t, agent: rows[i]?.name ?? null })),
              ),
            )
          : fetchTrades(effectiveModel, market)
        : Promise.resolve([]),
    // deps 用 agentsKey（名字串）而非 rows 数组：overview 每 30s 换一次 data 引用，
    // 用 rows 会让本钩子每 30s 重启一次、把间隔设成多少都白搭（2026-09-08）。
    [effectiveModel, selectedModel, agentsKey, market],
    30000,
  );
  // 对话 tab「全部模型」视图会自己拉全部 agent 日志，此时 logs 钩子再拉一遍
  // 就是纯重复（同一份 1MB+ payload 一次加载发两遍，2026-09-08）。这里让位。
  const chatAllActive = selectedModel === 'all' && tab === 'chat';
  const logs = usePolling<LogLine[]>(
    () =>
      effectiveModel && !chatAllActive
        ? fetchLogs(effectiveModel, market, LOG_LIMIT)
        : Promise.resolve([]),
    [effectiveModel, market, chatAllActive],
    LOG_POLL_MS,
  );
  // 对话 tab「全部模型」视图：并行拉各模型日志 → 混合时间流
  // 注意用 selectedModel 判断（effectiveModel 会把 all 降级成第一个模型）
  const chatAll = usePolling<{ name: string; lines: LogLine[] }[] | null>(() => {
    if (!chatAllActive || !rows.length) return Promise.resolve(null);
    // 展示名走 displayAgentName（market-research → 市场研究）；取数仍用签名
    const units = rows.map((r) => ({
      name: displayAgentName(r.name),
      pull: () => fetchLogs(r.name, market, LOG_LIMIT),
    }));
    // cn 的 overview 已把 market-research 计入 rows（研究总控有独立净值线），
    // 这里只在缺失时补一张中文名对话卡——否则同一份日志拉两遍、卡片也重影。
    // 判据看签名（displayAgentName 之后 name 已是中文，不能再拿来比）。
    if (market === 'cn' && !rows.some((r) => r.name === 'market-research')) {
      units.push({
        name: displayAgentName('market-research'),
        pull: () => fetchLogs('market-research', market, LOG_LIMIT),
      });
    }
    return Promise.all(units.map((u) => u.pull().catch(() => [] as LogLine[]))).then(
      (lists) => units.map((u, i) => ({ name: u.name, lines: lists[i] })),
    );
  }, [chatAllActive, agentsKey, market], LOG_POLL_MS);

  // ---------- 实盘账户（A股：通达信桥 /live/account；港股：富途（自建 OpenD，经 /futu/* 代理）；us：IBKR 已下线） ----------
  const liveAcct = usePolling(() => fetchLiveAccountFor(market), [market], 30000, 0);
  const livePositions = useMemo(() => {
    const list = (liveAcct.data?.positions ?? []).filter((p) => Number(p.total_volume) > 0);
    // 港股富途：桥回报不带买入时刻（富途映射器 buy_time 恒为空）→ 用订单历史（已轮询的
    // liveTrades）FIFO 回填建仓时间，让「持仓」tab 的日期筛选可用；历史窗口外推不出
    // 仍为空（按「买入日未知」展示，不参与筛选）。
    if (market !== 'hk') return list;
    const derived = deriveBuyTimes(liveTrades.data ?? []);
    if (!Object.keys(derived).length) return list;
    return list.map((p) => (p.buy_time ? p : { ...p, buy_time: derived[p.stock_code] ?? '' }));
  }, [liveAcct.data, market, liveTrades.data]);
  /** 实盘持仓展示集：cn 分账时按选中模型收窄（hk 单一共享账户无分账，不按模型筛），
   *  大仓位在前。『持仓』tab 渲染与「日期」候选共用同一出处（见 renderList / dateOptions）。 */
  const livePosView = useMemo(() => {
    const ag =
      market === 'cn' && selectedModel !== 'all'
        ? (liveLedger.data?.agents?.[selectedModel] ?? null)
        : null;
    const mine = ag ? new Set(ag.positions.map((lp) => lp.code)) : null;
    const shown = (mine ? livePositions.filter((p) => mine.has(p.stock_code)) : livePositions)
      .slice()
      .sort((a, b) => Number(b.position_value) - Number(a.position_value));
    return { ag, shown };
  }, [market, selectedModel, liveLedger.data, livePositions]);
  /** 实盘持仓按「日期」收窄（买入日）：持仓卡列表与计数共用一处 */
  const livePosDated = useMemo(
    () =>
      dateFilter === 'all'
        ? livePosView.shown
        : livePosView.shown.filter((p) => dayHit(p.buy_time, dateFilter)),
    [livePosView, dateFilter],
  );
  // 通达信账户的 quantmind 落库快照：桥实时通道读不通时的兜底持仓来源（带快照时刻）
  const realTdxAcct = usePolling(
    () => (market === 'cn' ? fetchRealAccount().catch(() => null) : Promise.resolve(null)),
    [market],
    30000,
  );
  const tdxPos = useMemo(
    () =>
      market === 'cn'
        ? (realTdxAcct.data?.positions ?? [])
            .filter((p) => Number(p.volume) > 0)
            .slice()
            .sort((a, b) => Number(b.market_value) - Number(a.market_value))
        : [],
    [market, realTdxAcct.data],
  );
  // 港股实盘 tab 双卡（富途 REAL+SIMULATE，一次握手游走）后台 15s 轮询 → 点击 tab 即见，
  // 不在 RealAccountPanel 内单独起子进程（省一次 ~4s RSA 握手）
  const futuBoth = usePolling(
    () => (market === 'hk' ? fetchFutuAccountBoth() : Promise.resolve(null)),
    [market],
    15000,
  );
  // 模拟盘最新快照（去零持仓：SSE50 成分快照里 0 股是噪声，不占列表）
  const lastSimSnapshot = positions.data?.[positions.data.length - 1];
  const simEntries = useMemo(
    () =>
      Object.entries(lastSimSnapshot?.positions ?? {}).filter(
        ([sym, qty]) => sym !== 'CASH' && Number(qty) > 0,
      ),
    [lastSimSnapshot],
  );
  const simCash = Number(lastSimSnapshot?.positions?.CASH ?? 0);
  /** 「持仓」tab 计数：与 renderList 分支同口径（实盘卡按日期收窄；通达信快照 /
   *  模拟盘回放无买入日，整体展示不参与日期筛选）。角标与 filter-bar 计数共用。 */
  const posCount = useMemo(() => {
    if ((market === 'cn' || market === 'hk') && livePositions.length > 0) return livePosDated.length;
    if (market === 'cn' && tdxPos.length > 0) return tdxPos.length;
    return simEntries.length;
  }, [market, livePositions, livePosDated, simEntries, tdxPos]);
  // 实盘分账账本（每 agent ¥10 万虚拟子账户，按模型显示各自持仓）
  // （hook 上移：空仓虚线判定在 lines memo 里要用）

  // ---------- 滚动价格条（当前市场全部 agent 持仓股票最新价） ----------
  const prices = usePolling(() => fetchPrices(market), [market], 30000, 16000);
  const stockNames = usePolling(() => fetchStockNames(market), [market], 600000);
  const marketPositions = usePolling(
    () =>
      Promise.all(
        rows.map((r) => fetchPositions(r.name, market).catch(() => [] as PositionRecord[])),
      ).then((lists) => lists.flat()),
    [market, agentsKey],
    30000,
  );

  /** 今日实盘成交（execute 成功，最新在前）；按 ledger 归属标注 agent */
  const ledgerHolderOf = useMemo(() => {
    const map: Record<string, string> = {};
    for (const [agent, rec] of Object.entries(liveLedger.data?.agents ?? {})) {
      for (const p of rec.positions ?? []) map[p.code] = agent;
    }
    return map;
  }, [liveLedger.data]);
  const liveTradeEvents: LiveCardRow[] = (liveTrades.data ?? [])
    .filter((t) => {
      // 新格式：wait_fill 成交回报（fill 字段）；旧格式：result.status
      const hasFill = t.fill && Number(t.fill.filled_volume) > 0;
      const hasResult =
        t.result &&
        typeof t.result.status === 'string' &&
        t.result.status !== 'rejected' &&
        !String(t.result.message ?? '').includes('签名');
      return hasFill || hasResult;
    })
    .map((t) => ({
      ts: t.ts,
      code: t.code,
      side: t.side,
      volume: t.volume,
      price: t.price ?? null,
      name: t.name || stockLabel(stockNames.data, t.code),
      // 记录自带的 agent 优先（卖出后该股已不在任何账本，当前账本反查会丢归属）
      agent: (t as { agent?: string | null }).agent ?? ledgerHolderOf[t.code] ?? null,
    }))
    .sort((a, b) => (a.ts < b.ts ? 1 : -1));
  // 对账行（fill_adjust）：与成交同轴排在成交卡里，标「⚖ 对账」+ note，
  // 但**不计入**成交笔数/成交额（台账校正不是成交）。09-08 实录见 liveFills.ts。
  const liveAdjustEvents: LiveCardRow[] = (liveTrades.data ?? [])
    .flatMap((rec) => {
      const a = toLiveAdjust(rec);
      return a ? [a] : [];
    })
    .map((a) => ({
      kind: 'adjust' as const,
      ts: a.ts,
      code: a.code,
      side: null,
      volume: a.volume,
      price: a.price,
      name: stockLabel(stockNames.data, a.code),
      agent: a.agent,
      note: a.note,
    }))
    .sort((a, b) => (a.ts < b.ts ? 1 : -1));
  /** 按选中模型过滤实盘成交（'all' = 全部） */
  const liveTradesFiltered = liveTradeEvents.filter(
    (e) => selectedModel === 'all' || e.agent === selectedModel,
  );
  const liveAdjustFiltered = liveAdjustEvents.filter(
    (e) => selectedModel === 'all' || e.agent === selectedModel,
  );
  /** 成交卡列表 = 成交 + 对账（按时间倒序合并；笔数统计仍只算成交） */
  const liveCardRows = [...liveTradesFiltered, ...liveAdjustFiltered].sort((a, b) =>
    a.ts < b.ts ? 1 : -1,
  );
  // 日期筛选（'all' = 不筛）只作用于这三个 tab 的列表展示；净值图悬停成交、
  // 对话流里的成交标记仍用全量，不受影响。
  const liveTradesDated = liveTradesFiltered.filter((e) => dayHit(e.ts, dateFilter));
  const liveCardRowsDated = dateFilter === 'all' ? liveCardRows : liveCardRows.filter((e) => dayHit(e.ts, dateFilter));
  /** 实盘成交按日期分组（今日 → 9/1 …），日期头分组展示历史 */
  const liveGroups = useMemo(() => {
    const today = new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
    const out: { label: string; rows: LiveCardRow[] }[] = [];
    for (const e of liveCardRowsDated) {
      const d = String(e.ts).slice(0, 10);
      const last = out[out.length - 1];
      if (last && last.rows[0] && String(last.rows[0].ts).slice(0, 10) === d) {
        last.rows.push(e);
      } else {
        out.push({
          label: d === today ? `今日实盘成交 · ${d.slice(5)}` : `实盘成交 · ${d.slice(5)}`,
          rows: [e],
        });
      }
    }
    return out;
  }, [liveCardRowsDated]);
  const heldSymbols = useMemo(() => {
    const set = new Set<string>();
    for (const rec of marketPositions.data ?? []) {
      for (const [sym, qty] of Object.entries(rec.positions ?? {})) {
        if (sym !== 'CASH' && Number(qty) > 0) set.add(sym);
      }
    }
    return [...set];
  }, [marketPositions.data]);
  // newsTickers 已随新闻文章流下线移除（新闻 tab 现为 agent 对话）
  // 港股新闻关键词：富途无 HK 标签文章库，改用持仓短名（腾讯控股→腾讯）全文搜；
  // 无持仓则用「港股」泛搜恒生/港交所等。A股走 tickers 不用 keyword。
  const hkNewsKeyword = useMemo(() => {
    if (market !== 'hk') return '';
    const top = livePositions
      .slice()
      .sort((a, b) => Number(b.position_value) - Number(a.position_value))[0];
    if (!top?.name) return '港股';
    const short = top.name
      .replace(/(控股|实业|集团|股份.*|有限公司|Limited|Inc\.?|Corp\.?)$/i, '')
      .trim();
    return short || '港股';
  }, [market, livePositions]);
  const tickerItems = useMemo(() => {
    // 实盘：优先滚动实盘持仓实时价（A股通达信桥 / 港股富途，livePositions 同 shape）
    if ((market === 'cn' || market === 'hk') && livePositions.length > 0) {
      return livePositions
        .filter((p) => Number(p.last_price) > 0)
        .map((p) => ({
          sym: p.stock_code,
          name: p.name,
          quote: {
            price: Number(p.last_price),
            date: '',
            prev_close: Number(p.cost_price),
            // 后端 pnl_pct 是百分数(1.43), fmtPct 期望小数(0.0143) → 除 100
            change_pct: Number(p.pnl_pct) / 100,
          },
        }));
    }
    return heldSymbols
      .map((sym) => ({ sym, quote: prices.data?.[sym] ?? null, name: stockName(stockNames.data, sym) }))
      .filter((t) => t.quote != null);
  }, [market, livePositions, heldSymbols, prices.data, stockNames.data]);

  // ---------- 事件流（成交，/trades 顶层字段） ----------
  const tradeEvents: TradeEvt[] = useMemo(
    () =>
      (trades.data ?? [])
        .map((r) => ({
          date: r.date,
          side: (r.action ?? '').toLowerCase() === 'buy' ? 'buy' as const : 'sell' as const,
          symbol: r.symbol,
          name: stockLabel(stockNames.data, r.symbol),
          amount: r.amount,
          cash: r.cash_after ?? 0,
          price: r.price ?? null,
          notional: r.notional ?? null,
          agent: (r as { agent?: string | null }).agent ?? null,
        }))
        .sort((a, b) => (a.date < b.date ? 1 : -1)),
    [trades.data, stockNames.data],
  );
  /** 模拟盘成交按选中模型过滤（'all' = 全部，事件已带 agent 标注） */
  const tradeEventsFiltered = tradeEvents.filter(
    (e) => selectedModel === 'all' || e.agent === selectedModel,
  );
  const tradeEventsDated = tradeEventsFiltered.filter((e) => dayHit(e.date, dateFilter));

  /** 「日期」下拉候选：按当前 tab 取各自数据源（成交=实盘+模拟成交日，持仓=实盘买入日，
   *  已完成=feed 回传的平仓日）；含当前选中值，切 tab / 切市场后筛选不悬空。 */
  const dateOptions = useMemo(() => {
    const pool: (string | null | undefined)[] = [];
    if (tab === 'trades') {
      pool.push(...liveCardRows.map((e) => e.ts), ...tradeEventsFiltered.map((e) => e.date));
    } else if (tab === 'positions') {
      pool.push(...livePosView.shown.map((p) => p.buy_time)); // 快照兜底 / 模拟盘持仓无买入日
    } else if (tab === 'completed') {
      pool.push(...completedDates);
    }
    return dayOptions(pool, dateFilter);
  }, [tab, liveCardRows, tradeEventsFiltered, livePosView, completedDates, dateFilter]);

  // ---------- 顶部价格条（基准 + 最高/最低表演者） ----------
  const benchStats = useMemo(() => {
    const pts: BenchPoint[] = bench.data ?? [];
    if (pts.length < 2) return { last: null, dayChange: null };
    const last = pts[pts.length - 1].close;
    const prev = pts[pts.length - 2].close;
    return { last, dayChange: prev ? (last - prev) / prev : null };
  }, [bench.data]);

  // 展示名映射在 rankPerformers 内：研究线在 cn 下跌行情里常排最高，
  // 直接渲染 r.name 会裸露英文签名（2026-09-12 审查 LOW）
  const performers = useMemo(
    () => rankPerformers(rows.map((r) => ({ name: r.name, ret: r.summary?.total_return ?? null }))),
    [rows],
  );

  // ---------- 右侧列表渲染 ----------
  const renderList = () => {
    if (tab === 'comp') {
      return <CompConfigPanel models={rows.map((r) => r.name)} market={market} />;
    }

    if (tab === 'real') {
      return (
        <>
          <RealAccountPanel
            market={market}
            currency={meta.currency}
            futuBoth={futuBoth.data}
            futuError={futuBoth.error}
            futuLoading={futuBoth.loading}
            channel={realChannel}
            onChannel={setRealChannel}
          />
          {/* QM 本地新增（2026-10-08）：港股最小下单/撤单面板（含「当前委托」撤单） */}
          {market === 'hk' && <HkOrderPanel />}
        </>
      );
    }

    if (tab === 'details') {
      return (
        <LiveDetails
          market={market}
          rows={rows}
          currency={meta.currency}
          futuBoth={futuBoth.data}
          ibkr={market === 'us' && liveAcct.data ? { total_asset: liveAcct.data.asset } : null}
        />
      );
    }

    if (tab === 'positions') {
      // 实盘持仓置顶展示（A股通达信桥 / 港股富途，同 shape）；展示集见 livePosView
      // （cn 分账按选中模型收窄；hk 单一共享账户不按模型筛）
      if ((market === 'cn' || market === 'hk') && livePositions.length > 0) {
        const { ag } = livePosView;
        // 「日期」筛选 = 按买入日（持仓卡「买入 MM-DD」）收窄实盘持仓；'all' 不筛。
        // 买入日未知的行（回填后仍推不出的：成交日志/订单历史窗口外）不参与筛选，明示数量。
        const shownPositions = livePosDated;
        const unknownPos =
          dateFilter === 'all'
            ? 0
            : livePosView.shown.filter((p) => !dayOf(p.buy_time)).length;
        const unknownNote = unknownPos > 0 ? ` · 另有 ${unknownPos} 只买入日未知，未参与筛选` : '';
        const shownValue = shownPositions.reduce((s, p) => s + Number(p.position_value ?? 0), 0);
        const shownPnl = shownPositions.reduce((s, p) => s + Number(p.pnl ?? 0), 0);
        return (
          <div style={{ padding: '8px 12px' }}>
            <div className="pos-section-title">
              {ag ? `模型 ${selectedModel} 名下持仓` : market === 'hk' ? '实盘持仓（富途）' : '实盘持仓（通达信桥）'}
              <span className="pos-section-sub">
                {ag
                  ? `${shownPositions.length} 只 · 额度已用 ¥${ag.used.toLocaleString('en-US')} / ¥${ag.quota.toLocaleString('en-US')}`
                  : `${shownPositions.length} 只 · 市值 ${fmtMoney(shownValue, meta.currency)} · 浮盈 ${shownPnl >= 0 ? '+' : ''}${fmtMoney(shownPnl, meta.currency)} · 总资产 ${fmtMoney(liveAcct.data?.asset ?? 0, meta.currency)}`}
                {dateFilter !== 'all' && unknownNote}
              </span>
              <LiveClock />
            </div>
            {shownPositions.length === 0 && (
              <div className="empty-state" style={{ padding: '12px 0' }}>
                {dateFilter !== 'all'
                  ? `该日期（${dateFilter.slice(5)}）无买入的实盘持仓${unknownPos > 0 ? `（另有 ${unknownPos} 只买入日未知，未参与筛选）` : ''}`
                  : ag
                    ? '该模型名下暂无实盘持仓'
                    : '暂无持仓'}
              </div>
            )}
            {shownPositions.map((p) => (
              <div className="live-pos-card" key={p.stock_code}>
                <div className="live-pos-main">
                  <span className="live-pos-name">{p.name}</span>
                  <span className="live-pos-code">{p.stock_code}</span>
                  <span className={`live-pos-pnl ${Number(p.pnl) >= 0 ? 'up' : 'down'}`}>
                    {Number(p.pnl) >= 0 ? '+' : ''}{fmtMoney(Number(p.pnl), meta.currency)}
                    {' '}({Number(p.pnl_pct) >= 0 ? '+' : ''}{Number(p.pnl_pct).toFixed(2)}%)
                  </span>
                </div>
                <div className="live-pos-sub">
                  <span>买入 {p.buy_time.slice(5)}</span>
                  <span>{Number(p.total_volume).toLocaleString('en-US')} 股{Number(p.available_volume) < Number(p.total_volume) ? `（可卖 ${Number(p.available_volume).toLocaleString('en-US')}）` : ''}</span>
                  <span>成本 {fmtPrice(Number(p.cost_price), meta.currency)}</span>
                  <span>现价 {fmtPrice(Number(p.last_price), meta.currency)}</span>
                  <span>持仓 {fmtMoney(Number(p.position_value), meta.currency)}</span>
                  <span>占比 {shownValue > 0 ? `${((Number(p.position_value) / shownValue) * 100).toFixed(1)}%` : '—'}</span>
                </div>
              </div>
            ))}
            {market !== 'hk' && (
              <>
                <div className="pos-section-title" style={{ marginTop: 14 }}>
                  模拟盘持仓
                  <span className="pos-section-sub">
                    {simEntries.length} 只（仅非零）
                    {dateFilter !== 'all' && ' · 快照回放无买入日，不受日期筛选'}
                  </span>
                </div>
                <div className="pos-row">
                  <span className="pos-sym">现金 CASH</span>
                  <span className="pos-cash">{fmtMoney(simCash, meta.currency)}</span>
                </div>
                {simEntries.length === 0 && (
                  <div className="empty-state" style={{ padding: '24px 0' }}>空仓 — 无持仓</div>
                )}
                {simEntries.map(([sym, qty]) => (
                  <div className="pos-row" key={sym}>
                    <span className="pos-sym">
                      <span className="pos-name">{stockLabel(stockNames.data, sym)}</span>
                      <span className="pos-code">{sym}</span>
                    </span>
                    <span className="pos-qty">{Number(qty).toLocaleString('en-US')}</span>
                  </div>
                ))}
              </>
            )}
          </div>
        );
      }
      // 桥实时通道读不到持仓时的 A 股兜底：用 quantmind 侧落库的账户快照
      // （含快照时刻），比直接掉到模拟盘回放有信息量——那是别的账户。
      if (market === 'cn' && tdxPos.length > 0) {
        const snapTs = realTdxAcct.data?.ts ? fmtBeijingDateTime(realTdxAcct.data.ts, { withSeconds: false }) : '—';
        const snapValue = tdxPos.reduce((s, p) => s + Number(p.market_value ?? 0), 0);
        return (
          <div style={{ padding: '8px 12px' }}>
            <div className="pos-section-title">
              实盘持仓（通达信桥 · 最近快照）
              <span className="pos-section-sub">
                {tdxPos.length} 只 · 市值 {fmtMoney(snapValue, meta.currency)} · 总资产{' '}
                {fmtMoney(realTdxAcct.data?.total_asset ?? 0, meta.currency)}
              </span>
              <LiveClock />
            </div>
            <div className="mdp-note" style={{ marginBottom: 10 }}>
              桥实时账户通道未返回持仓，以下为 quantmind 落库的最近一帧账户快照（{snapTs}），
              明细字段比桥少（无买入时刻/浮盈，成本价来自快照）。
              {dateFilter !== 'all' && ' 快照无买入日期，不受日期筛选。'}
            </div>
            {tdxPos.map((p) => {
              const qty = Number(p.volume);
              const pnl = p.price > 0 && p.cost_price > 0 ? (p.price - p.cost_price) * qty : null;
              const pnlPct = p.cost_price > 0 && p.price > 0 ? (p.price / p.cost_price - 1) * 100 : null;
              return (
                <div className="live-pos-card" key={p.symbol}>
                  <div className="live-pos-main">
                    <span className="live-pos-name">{p.name || stockLabel(stockNames.data, p.symbol)}</span>
                    <span className="live-pos-code">{p.symbol}</span>
                    <span className={`live-pos-pnl ${pnl == null ? 'dim' : pnl >= 0 ? 'up' : 'down'}`}>
                      {pnl == null ? '—' : `${pnl >= 0 ? '+' : ''}${fmtMoney(pnl, meta.currency)}`}
                      {pnlPct != null && ` (${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%)`}
                    </span>
                  </div>
                  <div className="live-pos-sub">
                    <span>{qty.toLocaleString('en-US')} 股</span>
                    <span>可卖 {Number(p.available_volume).toLocaleString('en-US')}</span>
                    <span>成本 {fmtPrice(Number(p.cost_price), meta.currency)}</span>
                    <span>现价 {fmtPrice(Number(p.price), meta.currency)}</span>
                    <span>持仓 {fmtMoney(Number(p.market_value), meta.currency)}</span>
                  </div>
                </div>
              );
            })}
          </div>
        );
      }
      if (!lastSimSnapshot) return <div className="empty-state">暂无持仓数据</div>;
      return (
        <div style={{ padding: '8px 12px' }}>
          {market === 'cn' && (
            /* 桥账户与最近快照都读不到持仓时别默默退化：说清是通道问题，下面只是模拟盘回放 */
            <div className="mdp-note" style={{ marginBottom: 10 }}>
              实盘账户无持仓可展示（通达信桥实时通道未返回，最近快照也为空），
              以下为模拟盘回放快照
              {lastSimSnapshot.date ? ` · ${String(lastSimSnapshot.date).slice(0, 10)}` : ''}。
              {dateFilter !== 'all' && '（快照回放无买入日期，不受日期筛选）'}
            </div>
          )}
          <div className="pos-row">
            <span className="pos-sym">现金 CASH</span>
            <span className="pos-cash">{fmtMoney(simCash, meta.currency)}</span>
          </div>
          {simEntries.length === 0 && (
            <div className="empty-state" style={{ padding: '24px 0' }}>空仓 — 无持仓</div>
          )}
          {simEntries.map(([sym, qty]) => (
            <div className="pos-row" key={sym}>
              <span className="pos-sym">
                <span className="pos-name">{stockLabel(stockNames.data, sym)}</span>
                <span className="pos-code">{sym}</span>
              </span>
              <span className="pos-qty">{Number(qty).toLocaleString('en-US')}</span>
            </div>
          ))}
        </div>
      );
    }

    if (tab === 'chat') {
      // 统一用 ChatStream：'all' = 各模型混合时间流；筛选单模型 = 同组件
      // 只喂该模型（界面与「全部」一致，仅数据收窄）
      const agents =
        selectedModel === 'all'
          ? chatAll.data
          : [{ name: displayAgentName(selectedModel), lines: logs.data ?? [] }];
      if (!agents) return <div className="empty-state">加载对话…</div>;
      return (
        <ChatStream
          agents={agents}
          fills={liveTradesFiltered}
          heldCodes={new Set(livePositions.map((p) => p.stock_code))}
        />
      );
    }

    if (tab === 'news') {
      // A股：新闻 agent 对话（管线 scripts/news_brief.py，与「模型对话」同款渲染）；
      // 港股无新闻 agent 管线 → 保留原文关键词新闻流
      if (market === 'cn') return <NewsAgentChat agent={newsAgent} />;
      return (
        <NewsStream
          tickers={[]}
          hours={12}
          limit={30}
          keyword={hkNewsKeyword}
        />
      );
    }

    // COMPLETED —— 当前市场平仓消息流（nof1 风格；按筛选模型过滤，'all' = 全部）
    if (tab === 'completed') {
      return (
        <CompletedFeed
          agents={selectedModel === 'all' ? rows.map((r) => r.name) : [selectedModel]}
          market={market}
          currency={meta.currency}
          stockNames={stockNames.data ?? {}}
          onCount={setCompletedCount}
          date={dateFilter}
          onDates={onCompletedDates}
        />
      );
    }

    // TRADES —— 原始成交详细卡片（选中模型的全部成交；A股置顶今日实盘成交）
    if (!tradeEventsDated.length && !liveCardRowsDated.length) {
      return (
        <div className="empty-state">
          {dateFilter !== 'all' ? `该日期（${dateFilter.slice(5)}）暂无成交` : '暂无成交'}
        </div>
      );
    }
    return (
      <>
        {(market === 'cn' || market === 'hk' || market === 'us') && liveGroups.length > 0 && (
          <>
            {liveGroups.map((g) => (
              <div key={g.label}>
                <div className="pos-section-title">
                  {g.label}
                  {g.rows[0] && (market === 'hk' ? '（富途）' : market === 'us' ? '（IBKR）' : '（通达信桥）')}
                </div>
                {g.rows.map((e, i) => {
                  // 对账行（fill_adjust）：独立样式，不显示买/卖方向，附 note 说明
                  if (e.kind === 'adjust') {
                    return (
                      <div className="trade-card" key={`adj-${e.ts}-${i}`}>
                        <div className="trade-card-head">
                          <span className="trade-side info">⚖ 对账</span>
                          <b className="trade-card-symbol">{e.name}</b>
                          <span className="trade-card-code">{e.code}</span>
                          <span className="trade-card-date">{e.ts.slice(5, 16)}</span>
                        </div>
                        <div className="trade-card-grid">
                          <span>归属{' '}
                            <b style={{ color: e.agent ? modelColor(e.agent) : '#000' }}>
                              {e.agent ?? '总账户'}
                            </b>
                          </span>
                          <span>数量 <b>{e.volume.toLocaleString('en-US')}</b></span>
                          <span>价格 <b>{e.price != null ? fmtPrice(e.price, meta.currency) : '—'}</b></span>
                          <span>金额 <b>{e.price != null ? fmtMoney(e.price * e.volume, meta.currency) : '—'}</b></span>
                        </div>
                        {e.note && (
                          <div className="faint" style={{ marginTop: 6, fontSize: 11 }}>{e.note}</div>
                        )}
                      </div>
                    );
                  }
                  const isSell = String(e.side ?? '').toUpperCase() === 'SELL';
                  const isBuy = String(e.side ?? '').toUpperCase() === 'BUY';
                  // 卖出口径区分：卖后桥仍持有该股 → 减仓；已不持有 → 清仓
                  const heldNow = livePositions.some((p) => p.stock_code === e.code);
                  const sideLabel = !isBuy && !isSell ? '成交' : isBuy ? '买入' : heldNow ? '减仓' : '清仓';
                  return (
                    <div className="trade-card" key={`live-${e.ts}-${i}`}>
                      <div className="trade-card-head">
                        <span
                          className={`trade-side ${
                            isBuy ? 'buy' : isSell ? (heldNow ? 'sell partial' : 'sell') : 'info'
                          }`}
                        >
                          {sideLabel}
                        </span>
                        <b className="trade-card-symbol">{e.name}</b>
                        <span className="trade-card-code">{e.code}</span>
                        <span className="trade-card-date">{e.ts.slice(5, 16)}</span>
                      </div>
                      <div className="trade-card-grid">
                        <span>归属{' '}
                          <b style={{ color: e.agent ? modelColor(e.agent) : '#000' }}>
                            {e.agent ?? '总账户'}
                          </b>
                        </span>
                        <span>数量 <b>{e.volume.toLocaleString('en-US')}</b></span>
                        <span>成交价 <b>{e.price != null ? fmtPrice(e.price, meta.currency) : '—'}</b></span>
                        <span>成交金额 <b>{e.price != null ? fmtMoney(e.price * e.volume, meta.currency) : '—'}</b></span>
                      </div>
                    </div>
                  );
                })}
              </div>
            ))}
            {/* 模拟盘成交标题只在有行时出现（日期筛选下常有一整天全是实盘成交） */}
            {tradeEventsDated.length > 0 && (
              <div className="pos-section-title" style={{ marginTop: 10 }}>模拟盘成交</div>
            )}
          </>
        )}
        {tradeEventsDated.map((e, i) => (
          <div className="trade-card" key={`${e.date}-${i}`}>
            <div className="trade-card-head">
              <span className={`trade-side ${e.side}`}>{e.side === 'buy' ? '买入' : '卖出'}</span>
              {e.agent && (
                <span className="mc-mode-chip" style={{ marginLeft: 0, marginRight: 6 }}>
                  {shortName(e.agent ?? '')}
                </span>
              )}
              <b className="trade-card-symbol">{e.name}</b>
              <span className="trade-card-code">{e.symbol}</span>
              <span className="trade-card-date">{e.date.slice(5)}</span>
            </div>
            <div className="trade-card-grid">
              <span>价格 <b>{e.price != null ? fmtPrice(e.price, meta.currency) : '—'}</b></span>
              <span>数量 <b>{e.amount.toLocaleString('en-US')}</b></span>
              <span>成交金额 <b>{e.notional != null ? fmtMoney(e.notional, meta.currency) : '—'}</b></span>
              <span>现金 <b>{fmtMoney(e.cash, meta.currency)}</b></span>
            </div>
          </div>
        ))}
      </>
    );
  };

  if (overview.error) {
    return (
      <div className="error-box">
        API 连接失败：{overview.error}
        <br /><br />
        实盘栏数据不可达：请确认 QuantMind 后端（api 容器）已启动，然后刷新重试
      </div>
    );
  }

  // cn 首帧实盘净值未到前不画图：perfs 是模拟盘回放，先画出来再换源就是用户看到的
  // 「刷新后图表先是乱的」（X 轴 08-03…08-27 那串）。phase 已归零，等待只剩一次请求。
  const chartPending = market === 'cn' && liveEquity.loading && !liveEqRef.current;

  // tab 角标：一眼看出哪块有内容（0 也显示，省得点进去才发现是空的）；
  // 日期筛选生效时随之收窄（与筛完实际看到的条数一致）
  const tabBadges: Partial<Record<Tab, number>> = {
    completed: completedCount,
    trades: tradeEventsDated.length + liveTradesDated.length,
    chat:
      selectedModel === 'all'
        ? (chatAll.data ?? []).reduce((n, a) => n + a.lines.length, 0)
        : (logs.data ?? []).length,
    positions: posCount,
  };

  return (
    <>
      {/* 导航栏横线正下方的独立条：交易时段（北京时间）+ 交易规则 + 盘中状态 +
          最高/最低表演者。切换市场时随市场更新 */}
      <div className="mh-bar">
        {/* 左：交易时段 + 规则 + 状态 + 最高/最低（单行，窄屏横向滚动） */}
        <div className="mh-content">
        {(() => {
          const st = marketStatusOf(market, new Date());
          return (
            <>
              <span className="mh-label">交易时间(北京)</span>
              <span className="mh-hours">{hoursLabelOf(market, new Date())}</span>
              <span className={`mh-status ${st.open ? 'open' : 'closed'}`}>{st.text}</span>
              <span className="mh-divider">·</span>
              <span className="mh-rule">{MARKET_HOURS[market].rule}</span>
              <span className="mh-divider">·</span>
              <div className="performers">
                <div className="performer">
                  <span className="performer-label">最高</span>
                  <span className="performer-value">
                    {performers.highest ? (
                      <>{performers.highest.name} <b className="up">{fmtPct(performers.highest.ret)}</b></>
                    ) : '—'}
                  </span>
                </div>
                <div className="performer">
                  <span className="performer-label">最低</span>
                  <span className="performer-value">
                    {performers.lowest ? (
                      <>{performers.lowest.name} <b className="down">{fmtPct(performers.lowest.ret)}</b></>
                    ) : '—'}
                  </span>
                </div>
              </div>
            </>
          );
        })()}
        </div>
        {/* 右：市场切换 chips（固定靠右） */}
        <MarketSwitcher market={market} onChange={switchMarket} />
      </div>
      <div className="live">
        {/* 顶部状态条：当日实时指数(2×3) + 市场切换 */}
        <div className="top-status-bar">
          <div className="status-group">
            {indices.data?.indices?.length ? (
              <div className="index-bar">
                {indices.data.indices.map((q) => (
                  <div className="index-item" key={q.code}>
                    <span className="index-name">{q.name}</span>
                    <span className="index-last">
                      {q.last.toLocaleString('en-US', { maximumFractionDigits: 2 })}
                    </span>
                    <span className={`index-chg ${pnlClass(q.change_pct / 100)}`}>
                      {fmtPct(q.change_pct / 100)}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="price-item">
                <span className="price-label">{benchLabelOf(market)} 指数</span>
                <span className="price-value">{benchStats.last != null ? fmtMoney(benchStats.last) : '—'}</span>
                <span className={`price-change ${benchStats.dayChange != null ? pnlClass(benchStats.dayChange) : 'dim'}`}>
                  {benchStats.dayChange != null ? fmtPct(benchStats.dayChange) : '无行情'}
                </span>
              </div>
            )}
          </div>
        </div>

      {/* 持仓股票滚动价格条（hover 暂停；速度随持仓数自适应） */}
      {tickerItems.length > 0 && (
        <div
          className="ticker"
          aria-label="持仓股票最新价格"
          style={{ ['--ticker-dur' as string]: `${Math.max(60, tickerItems.length * 4)}s` }}
        >
          <div className="ticker-track">
            {[...tickerItems, ...tickerItems].map((t, i) => {
              const q = t.quote!;
              return (
                <span className="ticker-item" key={`${t.sym}-${i}`}>
                  <span className="ticker-sym">{t.sym}</span>
                  {t.name && <span className="ticker-name">{t.name}</span>}
                  <span className="ticker-price">{fmtMoney(q.price, meta.currency)}</span>
                  <span className={`ticker-chg ${q.change_pct != null ? pnlClass(q.change_pct) : 'dim'}`}>
                    {q.change_pct != null ? fmtPct(q.change_pct) : '—'}
                  </span>
                </span>
              );
            })}
          </div>
        </div>
      )}

      <div className="main-content">
        {/* 左：图表 + 模型卡 */}
        <div className="chart-area">
          <div className="chart-header">
            <div className="chart-title">总账户净值</div>
            <div className="chart-controls">
              <button className={`time-btn ${chartRange === 'all' ? 'active' : ''}`} onClick={() => setChartRange('all')}>
                全部
              </button>
              <button className={`time-btn ${chartRange === '5d' ? 'active' : ''}`} onClick={() => setChartRange('5d')}>
                近5日
              </button>
              <span style={{ width: 1, height: 16, background: '#000', margin: '0 2px' }} />
              <button className={`time-btn ${chartMode === 'dollar' ? 'active' : ''}`} onClick={() => setChartMode('dollar')}>
                $
              </button>
              <button className={`time-btn ${chartMode === 'pct' ? 'active' : ''}`} onClick={() => setChartMode('pct')}>
                %
              </button>
            </div>
          </div>
          {overview.loading && !rows.length ? (
            <div className="loading"><div className="spinner" />加载中…</div>
          ) : chartPending ? (
            // 占位高度与图表一致，避免实盘首帧到达时布局跳动
            <div className="loading" style={{ height: 'clamp(360px, 44vw, 560px)' }}>
              <div className="spinner" />实盘净值加载中…
            </div>
          ) : (
            <>
              <EquityChart
                lines={lines}
                benchmark={benchLine}
                currency={meta.currency}
                // 悬停时序补充：当时持仓（账本反推时间线）+ 附近 ±3 分钟成交（cn 实盘）
                events={market === 'cn' ? (liveTradesFiltered as unknown as import('../components/EquityChart').HoverEvent[]) : undefined}
                holdings={market === 'cn' && heldSpans.length ? heldSpans : undefined}
                names={stockNames.data ?? undefined}
                priceMap={market === 'cn' && holderPriceMap ? holderPriceMap : undefined}
                mode={chartMode}
                timeRange={chartRange}
                height="clamp(360px, 44vw, 560px)"
              />
              <div className="model-cards-section">
                {(perfs.data ?? []).map((p) => {
                  // A股实盘: 模型卡显示实盘分账收益(虚拟净值/¥10万基准), 替代模拟盘回放
                  const eqPts = market === 'cn' ? liveEquity.data?.agents?.[p.agent] : null;
                  const liveNav = eqPts && eqPts.length ? eqPts[eqPts.length - 1].value : null;
                  const isLive = market === 'cn' && liveNav != null;
                  return (
                    <ModelCard
                      key={p.agent}
                      market={market}
                      agent={p.agent}
                      balance={isLive ? liveNav : (p.summary?.end_equity ?? null)}
                      // fmtPct 期望小数（内部 ×100）；这里只算净值/¥10万 的比率
                      ret={isLive ? liveNav! / 100000 - 1 : (p.summary?.total_return ?? null)}
                      selected={p.agent === effectiveModel}
                      onClick={() =>
                        setSelectedModel(asUpdater((cur) => (cur === p.agent ? 'all' : p.agent)))
                      }
                      tokens={tokenUsage.data?.agents?.[p.agent] ?? null}
                    />
                  );
                })}
                {!perfs.data?.length && <div className="empty-state">该市场暂无 Agent</div>}
              </div>
            </>
          )}
        </div>

        {/* 右：540px 面板 */}
        <div className="right-section">
          <div className="trade-tabs">
            {TAB_GROUPS.map((group, gi) => (
              <div className="trade-tab-group" key={gi} style={{ flex: group.length }}>
                {group.map((t) => (
                  <button
                    key={t.id}
                    className={`trade-tab ${tab === t.id ? 'active' : ''}`}
                    onClick={() => setTab(t.id)}
                  >
                    {t.label}
                    {tabBadges[t.id] != null && (
                      <span className={`trade-tab-badge ${tabBadges[t.id] ? '' : 'zero'}`}>
                        {tabBadges[t.id]}
                      </span>
                    )}
                  </button>
                ))}
              </div>
            ))}
          </div>
          <div className="filter-bar">
            <span className="filter-label">{tab === 'news' ? '关注' : '模型'}</span>
            {tab === 'completed' || tab === 'trades' || tab === 'chat' || tab === 'positions' ? (
              <>
              <select
                className="filter-select"
                value={selectedModel}
                onChange={(e) => setSelectedModel(e.target.value)}
              >
                <option value="all">全部模型</option>
                {rows
                  // market-research 由下方硬编码中文条目承担（目录名是英文，避免双条目）
                  .filter((r) => r.name !== 'market-research')
                  .map((r) => (
                    <option key={r.name} value={r.name}>{r.name}</option>
                  ))}
                {market === 'cn' && (
                  <option key="market-research" value="market-research">市场研究（研究总控）</option>
                )}
              </select>
              {(tab === 'completed' || tab === 'trades' || tab === 'positions') && dateOptions.length > 0 && (
                /* 「日期」筛选：翻历史记录。候选=当前数据源出现过的日期；该数据源没有任何
                   日期（如港股持仓无买入时刻）时整只下拉隐藏，不摆一个筛不出东西的控件。
                   当年条目只显 MM-DD（与卡片一致），跨年条目带年份。 */
                <select
                  className="filter-select filter-date"
                  aria-label="按日期筛选"
                  title="按日期筛选（历史记录）"
                  value={dateFilter}
                  onChange={(e) => setDateFilter(e.target.value)}
                >
                  <option value="all">全部日期</option>
                  {dateOptions.map((d) => (
                    <option key={d} value={d}>
                      {d.slice(0, 4) === todayCn.slice(0, 4) ? d.slice(5) : d}
                    </option>
                  ))}
                </select>
              )}
              {tab === 'chat' && (
                <>
                  <button
                    className={`analyze-trigger ${analyzeState === 'busy' ? 'busy' : ''}`}
                    disabled={analyzeState === 'busy'}
                    onClick={() => void runManualAnalysis()}
                    title="立即跑一轮模型分析（宿主队列，约 1 分钟内开跑；只出观点，不下单）"
                  >
                    {analyzeState === 'busy' ? '提交中…' : '⚡ 立即分析'}
                  </button>
                  {analyzeMsg && <span className="analyze-msg">{analyzeMsg}</span>}
                </>
              )}
              </>
            ) : tab === 'news' ? (
              market === 'cn' ? (
                <>
                  <select
                    className="filter-select"
                    value={newsAgent}
                    onChange={(e) => setNewsAgent(e.target.value)}
                  >
                    <option value="all">全部新闻</option>
                    {NEWS_AGENTS.map((a) => (
                      <option key={a.id} value={a.id}>{a.cn}</option>
                    ))}
                  </select>
                  <button
                    className={`analyze-trigger ${newsMsg.includes('已触发') ? 'busy' : ''}`}
                    disabled={newsMsg.includes('已触发')}
                    onClick={() => {
                      setNewsMsg('已触发新闻分析，约 3-5 分钟内完成');
                      triggerNewsAnalysis()
                        .then(() => setTimeout(() => setNewsMsg(''), 8000))
                        .catch(() => setNewsMsg('触发失败：后端不可达'));
                    }}
                    title="手动跑一轮新闻 agent 管线（增量窗口；只读新闻与行情，不下单）"
                  >
                    ⚡ 立即分析
                  </button>
                  {newsMsg && <span className="analyze-msg">{newsMsg}</span>}
                </>
              ) : (
                <span className="filter-static">关键词「{hkNewsKeyword}」</span>
              )
            ) : (
              <span className="filter-static">全部模型</span>
            )}
            <span className="filter-count">
              {tab === 'completed'
                ? completedCount
                : tab === 'trades'
                  ? tradeEventsDated.length + liveTradesDated.length
                  : tab === 'chat'
                    ? selectedModel === 'all'
                      ? (chatAll.data ?? []).reduce((n, a) => n + a.lines.length, 0)
                      : (logs.data ?? []).length
                    : tab === 'positions'
                      ? posCount
                      : ''}
            </span>
          </div>
          <div className="trade-list">{renderList()}</div>
        </div>
      </div>
    </div>
    </>
  );
}
