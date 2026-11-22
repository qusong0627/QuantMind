/** 行情回测（market lab）数据层。
 *
 * 后端族 = `/api/v1/agent-arena/market-lab/*`（`backend/services/api/routers/
 * agent_arena/market_lab_family.py` + `backend/services/agent_arena/`）。端点仍挂在
 * arena 命名空间下（后端未迁移），但调用方只有回测中心本模块，实盘交易栏不再引用。
 *
 * 响应信封统一 `{success, data}` / `{success:false, error}`；本文件只负责解包与类型。
 */
import axios from 'axios';
import { SERVICE_ENDPOINTS } from '../../../config/services';
import { authService } from '../../../features/auth/services/authService';
import { unwrapEnvelope } from './envelope';

const api = axios.create({ timeout: 20000 });

api.interceptors.request.use((config) => {
  config.baseURL = `${SERVICE_ENDPOINTS.API_GATEWAY}/agent-arena`;
  const token = authService.getAccessToken();
  if (token) {
    (config.headers as Record<string, string>).Authorization = `Bearer ${token}`;
  }
  return config;
});

/** 解包 {success, data} 信封（业务错误 success:false 一律 reject，见 envelope.ts） */
const unwrap = unwrapEnvelope;

// ---------- K线 / 标的 / 策略 ----------

export interface LabSymbol {
  code: string;
  name: string;
}

export interface Kline {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export type AdjMode = 'unadjusted' | 'forward' | 'backward';

/** K线周期：日线 / 5分钟 / 1分钟（分钟线仅 A股/加密有落盘） */
export type Freq = '1d' | '5m' | '1m';

/** 行情实验室可切换的市场（与后端 market_lab.MARKET_SPECS 对齐） */
export type LabMarket = 'cn' | 'hk' | 'us' | 'futures' | 'crypto';

export interface MarketInfo {
  id: LabMarket;
  label: string;
  currency: string;
  /** 该市场**真实可用**的复权档（不要渲染不存在的档） */
  adjs: AdjMode[];
  /** 界面可选的周期（三档都列；选到无数据的档由前端自动回退日线） */
  freqs: Freq[];
  /** 该市场**真实有落盘**的周期 */
  avail_freqs: Freq[];
  has_data: boolean;
}

export const fetchMarketMeta = () => unwrap<MarketInfo[]>(api.get('/market-lab/markets'));

export interface LabStrategyParam {
  k: string;
  label: string;
  default: number;
}

export interface LabStrategy {
  id: string;
  name: string;
  desc: string;
  params: LabStrategyParam[];
}

export interface BtStat {
  value: number | null;
  pct: number | null;
}

export interface BtTrade {
  entry_time: string;
  entry_price: number | null;
  qty: number | null;
  signal: string;
  exit_time: string | null;
  exit_price: number | null;
  profit: number | null;
  profit_pct: number | null;
}

export interface BtEquityPoint {
  date: string;
  value: number;
}

/** 回测时捕获的策略指标序列（与 K 线逐根对齐；缺值的 bar 为 null）。
 *  overlays 画在价格主图上，panes 各占一个副图（MACD/RSI 这类不同量纲的）。 */
export interface IndicatorSeries {
  key: string;
  /** 副图分组：同一条推导链的多条线共用一个 group（如 MACD 的 DIF/DEA/柱，
   *  以及 RSI 与其区间高低点）→ 同画在一个副图。叠加线画在主图上，没有这个字段。 */
  group?: string;
  label: string;
  kind: 'line' | 'hist';
  values: (number | null)[];
}

export interface IndicatorSet {
  bars: number;
  overlays: IndicatorSeries[];
  panes: IndicatorSeries[];
}

export interface BtResult {
  stats: Record<string, BtStat>;
  trades: BtTrade[];
  equity: BtEquityPoint[];
  indicators?: IndicatorSet | null;
  meta: {
    strategy: string;
    name: string;
    symbol: string;
    name_cn: string;
    adj: string;
    /** 回测所属市场（老产物无此字段 → 按 A 股） */
    market?: string;
    /** 回测周期（老产物无此字段 → 日线） */
    freq?: Freq;
    start: string;
    end: string;
    bars: number;
    params: Record<string, number>;
  };
}

export const searchLabSymbols = (q: string, limit = 30, market: LabMarket = 'cn') =>
  unwrap<LabSymbol[]>(api.get('/market-lab/symbols', { params: { q, limit, market } }));

export const fetchLabKlines = (
  symbol: string,
  adj: AdjMode = 'unadjusted',
  limit = 600,
  market: LabMarket = 'cn',
  freq: Freq = '1d',
) =>
  unwrap<{ market: LabMarket; symbol: string; name: string; adj: string; freq: Freq; count: number; bars: Kline[] }>(
    api.get('/market-lab/klines', { params: { symbol, adj, limit, market, freq } }),
  );

export const fetchLabStrategies = () => unwrap<LabStrategy[]>(api.get('/market-lab/strategies'));

export const runLabBacktest = (payload: {
  strategy: string;
  symbol: string;
  adj?: AdjMode;
  start?: string;
  end?: string;
  params?: Record<string, number>;
  market?: LabMarket;
  freq?: Freq;
}) =>
  // 模板回测是同步接口（全历史 3000 根也可能跑几十秒），单请求放宽到 60s；
  // 其余行情/列表接口仍走实例默认的 20s。
  unwrap<BtResult>(api.post('/market-lab/backtest', payload, { timeout: 60_000 }));

// ---------- 批量回测（股票池 × 策略，scripts/lab_batch_backtest.py 跑批产物） ----------

export interface LabBatchRow {
  symbol: string;
  name: string;
  strategy: string;
  net_pct: number | null;
  buy_hold_pct: number | null;
  dd_pct: number | null;
  sharpe: number | null;
  sortino: number | null;
  profit_factor: number | null;
  win_rate_pct: number | null;
  trades: number | null;
  avg_bars: number | null;
  bars: number;
  start: string;
  end: string;
}

export interface LabBatchRank {
  strategy: string;
  name: string;
  symbols: number;
  mean_net_pct: number | null;
  median_net_pct: number | null;
  mean_dd_pct: number | null;
  mean_sharpe: number | null;
  mean_profit_factor: number | null;
  mean_win_rate_pct: number | null;
  mean_trades: number | null;
  beat_bh_pct: number | null;
}

export interface LabBatchRun {
  run_id: string;
  created: string;
  pool: string;
  pool_label: string;
  adj: string;
  strategies: { id: string; name: string }[];
  universe: number;
  counts: { rows: number; skipped: number; errors: number };
  elapsed_sec?: number;
}

export interface LabBatchDetail extends LabBatchRun {
  ranking: LabBatchRank[];
  rows: LabBatchRow[];
  skipped: { symbol: string; name?: string; bars?: number; reason: string }[];
  errors: { symbol?: string; strategy?: string; error: string }[];
}

export const fetchLabBatchRuns = (limit = 20) =>
  unwrap<LabBatchRun[]>(api.get('/market-lab/batch/runs', { params: { limit } }));

export interface LabBatchPool {
  id: string;
  label: string;
  count: number;
}

export const fetchLabBatchPools = () =>
  unwrap<LabBatchPool[]>(api.get('/market-lab/batch/pools'));

/** 批量回测状态（宿主 worker 写 status/<run_id>.json，界面轮询） */
export type LabBatchState = 'queued' | 'running' | 'done' | 'failed';

export interface LabBatchStatus {
  run_id: string;
  status: LabBatchState;
  stage?: string;
  pool?: string;
  pool_label?: string;
  strategies?: string[];
  total?: number;
  done?: number;
  rows?: number;
  progress?: number | null;
  universe?: number;
  counts?: { rows: number; skipped: number; errors: number };
  elapsed_sec?: number;
  error?: string;
  created?: string;
  updated?: string;
}

export const startLabBatch = (payload: {
  pool: string;
  codes?: string[];
  strategies?: string[];
  adj?: AdjMode;
  limit?: number;
  min_bars?: number;
}) => unwrap<{ request: Record<string, unknown>; status: LabBatchStatus }>(
  api.post('/market-lab/batch', payload),
);

export const fetchLabBatchActive = () =>
  unwrap<LabBatchStatus | null>(api.get('/market-lab/batch/active'));

export const fetchLabBatchStatus = (runId: string) =>
  unwrap<LabBatchStatus>(
    api.get(`/market-lab/batch/${encodeURIComponent(runId)}/status`),
  );

export const fetchLabBatchRun = (runId: string, strategy = '', sort = 'net', limit = 200) =>
  unwrap<LabBatchDetail>(
    api.get(`/market-lab/batch/${encodeURIComponent(runId)}`, {
      params: { strategy, sort, limit },
    }),
  );

// ---------- Pine 策略库（桌面 TradingView 语料） ----------

export interface PineItem {
  id: string;
  category: string;
  title: string;
  title_en: string;
  file: string;
  url: string;
  version: number;
  lines: number;
  bytes: number;
  indent_ok: boolean;
  has_strategy: boolean;
  source_kind: 'desktop' | 'recrawl' | 'edited' | 'missing' | 'custom' | 'imported';
  /** 外部语料（pynecore-strategies）扩展字段 */
  source_id?: string;
  market_hint?: string;
  has_security?: boolean;
  security_timeframes?: string[];
  security_markets?: string[];
  import_status?: string;
  mtime: string;
}

export interface PineListItem extends PineItem {
  edited?: boolean;
  source?: string;
}

export interface PineList {
  generated: string;
  total: number;
  usable: number;
  filtered: number;
  categories: { name: string; count: number }[];
  items: PineItem[];
}

export const fetchPineList = (category = '', q = '', limit = 200, offset = 0) =>
  unwrap<PineList>(
    api.get('/market-lab/library', { params: { category, q, limit, offset } }),
  );

/** 新增自定义 Pine 策略（落 custom/，索引重建不丢） */
export const createPineItem = (payload: {
  title: string;
  category?: string;
  source: string;
  title_en?: string;
  url?: string;
}) => unwrap<PineItem>(api.post('/market-lab/library', payload));

/** 删除自定义 Pine 策略（内置/语料条目后端会拒绝） */
export const deletePineItem = (id: string) =>
  unwrap<{ id: string; deleted: boolean }>(
    api.delete(`/market-lab/library/${encodeURIComponent(id)}/custom`),
  );

export const fetchPineSource = (id: string) =>
  unwrap<PineListItem>(api.get(`/market-lab/library/${encodeURIComponent(id)}`));

export const savePineSource = (id: string, source: string) =>
  unwrap<{ id: string; saved: string; bytes: number; lines: number; indent_ok: boolean }>(
    api.put(`/market-lab/library/${encodeURIComponent(id)}`, { source }),
  );

export const resetPineSource = (id: string) =>
  unwrap<{ id: string; removed: boolean }>(
    api.delete(`/market-lab/library/${encodeURIComponent(id)}`),
  );

export interface PineTranspile {
  title: string;
  model: string;
  generated: string;
  problems: string[];
  has_candidate: boolean;
  has_report: boolean;
  symbol: string;
  trades: number | null;
  net_pct: number | null;
  dd_pct: number | null;
  bh_pct: number | null;
  job: { status: string; stage: string; error: string };
}

/** 转写产物一览，key = 策略库 id */
export const fetchPineTranspile = () =>
  unwrap<Record<string, PineTranspile>>(api.get('/market-lab/transpile'));

// ---------- 策略备注（data/pine_library/notes/<id>.json，索引重建不丢） ----------

export const NOTE_STATUSES = ['待研究', '观察中', '已采用', '已弃用'] as const;
export type NoteStatus = (typeof NOTE_STATUSES)[number];

export interface NoteDoc {
  id: string;
  note: string;
  tags: string[];
  rating: number | null;
  status: NoteStatus;
  by: 'user' | 'agent';
  updated: string;
}

/** 写备注的入参：tags 传数组或空格/逗号分隔的字符串都认（后端归一化） */
export interface NoteInput {
  note?: string;
  tags?: string[] | string;
  rating?: number | null;
  status?: NoteStatus;
  by?: 'user' | 'agent';
}

/** 同时覆盖库 id（0001 / x001）与内置模板 id（sma_cross） */
export const fetchNote = (id: string) =>
  unwrap<NoteDoc>(api.get(`/market-lab/library/${encodeURIComponent(id)}/note`));

export const saveNote = (id: string, payload: NoteInput) =>
  unwrap<NoteDoc>(api.put(`/market-lab/library/${encodeURIComponent(id)}/note`, payload));

/** 有备注的策略 id 集合（左栏 💬 角标） */
export const fetchNoteIds = () =>
  unwrap<{ ids: string[]; count: number }>(api.get('/market-lab/notes'));

// ---------- 策略对话（宿主 worker 消费队列，界面只投递 + 轮询） ----------
// 模型调用与沙箱回测都在宿主上发生（容器里没有 bwrap，也不执行模型产出的代码），
// API 只写请求文件、读结果文件。

export type ChatRole = 'user' | 'assistant';
/** ask = 只问答；edit = 产出候选 Pine（沙箱回测后由用户决定是否采用） */
export type ChatMode = 'ask' | 'edit';
/** staged = 候选已跑通沙箱回测，等 worker 落对比（还没到终态，界面要继续轮询） */
export type ChatStatus = 'queued' | 'running' | 'staged' | 'done' | 'failed';

export interface ChatMessage {
  role: ChatRole;
  content: string;
  timestamp?: string;
  job_id?: string;
  mode?: ChatMode;
  /** 界面本地的乐观消息：worker 还没把它落进 thread.jsonl */
  pending?: boolean;
}

/** 对比卡的一格：pct 是百分数（TradingView 口径），trades 只有 value */
export interface CompareCell {
  label: string;
  value?: number | null;
  pct?: number | null;
}

export interface ChatCandidate {
  pine: string;
  problems: string[];
  trades?: number | null;
  symbol?: string;
  adj?: string;
  stats: Record<string, { value?: number; pct?: number }>;
  compare: {
    symbol: string;
    adj: string;
    /** 候选回测所属市场（老任务没有这个字段 → 按 A 股） */
    market?: LabMarket;
    /** 候选回测周期（老任务没有这个字段 → 日线） */
    freq?: Freq;
    before: Record<string, CompareCell>;
    after: Record<string, CompareCell>;
    /** 「改前」那列取自最近一次正式回测报告；这是它生成的时间 */
    before_at?: string;
    /** 报告比当前源码还旧（手改过源码 / 闸门收紧后没重跑）→ 对比不可比 */
    before_stale?: boolean;
  };
}

export interface ChatJob {
  job_id: string;
  id?: string;
  status: ChatStatus;
  stage?: string;
  reply?: string;
  error?: string;
  truncated?: boolean;
  problems?: string[];
  /** edit 模式跑完才有：候选源码 + 前后指标对比 */
  candidate?: ChatCandidate;
}

export interface ApplyResult {
  id: string;
  job_id: string;
  applied: boolean;
  /** 采用前那版的快照名（回滚用） */
  snapshot: string;
  bytes: number;
  queued: boolean;
  queue_error: string;
}

export interface RevertResult {
  id: string;
  reverted: string;
  bytes: number;
  remaining: number;
  queued: boolean;
  queue_error: string;
}

export interface VersionRow {
  version: string;
  bytes: number;
}

export const fetchChatThread = (id: string, limit = 100) =>
  unwrap<{ id: string; count: number; messages: ChatMessage[] }>(
    api.get(`/market-lab/library/${encodeURIComponent(id)}/chat`, { params: { limit } }),
  );

export const sendChat = (id: string, message: string, mode: ChatMode = 'ask') =>
  unwrap<{ job_id: string; queued: boolean }>(
    api.post(`/market-lab/library/${encodeURIComponent(id)}/chat`, { message, mode }),
  );

export const fetchChatJob = (id: string, jobId: string) =>
  unwrap<ChatJob>(
    api.get(`/market-lab/library/${encodeURIComponent(id)}/chat/${encodeURIComponent(jobId)}`),
  );

/** 采用候选：后端先快照当前源码，再写 edited/<id>.pine 并自动重跑 */
export const applyChatCandidate = (
  id: string,
  jobId: string,
  symbol = '',
  adj = 'backward',
  market: LabMarket = 'cn',
  freq: Freq = '1d',
) =>
  unwrap<ApplyResult>(
    api.post(`/market-lab/library/${encodeURIComponent(id)}/chat/${encodeURIComponent(jobId)}/apply`,
      { symbol, adj, market, freq }),
  );

export const revertPineSource = (id: string, version = '') =>
  unwrap<RevertResult>(
    api.post(`/market-lab/library/${encodeURIComponent(id)}/revert`, { version }),
  );

export const fetchPineVersions = (id: string) =>
  unwrap<{ id: string; count: number; versions: VersionRow[] }>(
    api.get(`/market-lab/library/${encodeURIComponent(id)}/versions`),
  );

export interface PineTradeRow {
  entry_time: string;
  entry_price: number | null;
  qty: number | null;
  signal: string;
  exit_time: string | null;
  exit_price: number | null;
  profit: number | null;
  profit_pct: number | null;
}

export interface PineReport {
  id: string;
  symbol: string;
  adj: string;
  /** 沙箱回测所属市场（老报告无此字段 → 按 A 股） */
  market?: LabMarket;
  /** 沙箱回测周期（老报告无此字段 → 日线） */
  freq?: Freq;
  trades: number;
  stats: Record<string, { value?: number; pct?: number }>;
  trade_rows: PineTradeRow[];
  equity: { date: string; value: number }[];
  /** 策略真正算过的指标（沙箱回测时捕获）。老报告没有这个字段。 */
  indicators?: IndicatorSet | null;
}

export interface PineJob {
  status?: 'queued' | 'running' | 'done' | 'failed' | '';
  stage?: string;
  error?: string;
  problems?: string[];
  log?: string;
  updated?: string;
}

/** 入队「转写 + 沙箱回测」（宿主 worker 消费） */
export const runPineBacktest = (
  id: string,
  symbol: string,
  adj = 'backward',
  force = false,
  market: LabMarket = 'cn',
  freq: Freq = '1d',
) =>
  unwrap<{ queued: boolean; request: Record<string, unknown> }>(
    api.post(`/market-lab/library/${encodeURIComponent(id)}/backtest`, {
      symbol,
      adj,
      force,
      market,
      freq,
    }),
  );

/** 任务状态 + 报告。报告缺失时后端可能给 {} / null，统一收敛成 null
 *  （空对象是 truthy，直接当有报告用会在读 stats 时炸掉）。
 *  传 market/symbol/adj/freq → 只取该四元组的报告（不同市场/周期不互相覆盖）。 */
export const fetchPineJob = (
  id: string,
  opts?: { market?: LabMarket; symbol?: string; adj?: string; freq?: Freq },
) =>
  unwrap<{ job: PineJob | null; report: PineReport | null }>(
    api.get(`/market-lab/library/${encodeURIComponent(id)}/job`, { params: opts }),
  ).then((d) => ({
    job: d.job ?? {},
    report: d.report?.stats ? d.report : null,
  }));
