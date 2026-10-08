/** 实盘栏（竞技场）数据层：对接 QuantMind 后端的 /api/v1/agent-arena 代理。
 *  生产与 dev 同路径：请求经下方 axios 拦截器指向 QuantMind 后端，
 *  前端只带自己的 Bearer（原独立 API 服务与 nginx token 注入已随平台退役）。
 */
import axios from 'axios';
import { SERVICE_ENDPOINTS } from '../../../../../config/services';
import { authService } from '../../../../auth/services/authService';

/**
 * 原 arena：baseURL '/api'，token 由 nginx 反代注入（浏览器不持凭证）。
 * 移植进 QuantMind 实盘栏后改走 QuantMind 后端代理 /api/v1/agent-arena，
 * 上游 token 由后端注入；前端只带 QuantMind 自己的 Bearer。
 */
export const api = axios.create({ timeout: 20000 });

api.interceptors.request.use((config) => {
  config.baseURL = `${SERVICE_ENDPOINTS.API_GATEWAY}/agent-arena`;
  const token = authService.getAccessToken();
  if (token) {
    (config.headers as Record<string, string>).Authorization = `Bearer ${token}`;
  }
  return config;
});

// ---------- 后端数据结构（与 api_server.py / agent_data.py 对齐） ----------

export type MarketId = 'us' | 'cn' | 'hk';

export const MARKETS: { id: MarketId; label: string; name: string; currency: string }[] = [
  { id: 'cn', label: 'CN', name: 'A股 · SSE 50', currency: '¥' },
  { id: 'hk', label: 'HK', name: '港股 · 恒指成分', currency: 'HK$' },
  { id: 'us', label: 'US', name: '美股 · NASDAQ 100', currency: '$' },
];

export const marketMeta = (id: MarketId) => MARKETS.find((m) => m.id === id) ?? MARKETS[0];

export interface AgentInfo {
  name: string;
  has_position: boolean;
  has_log: boolean;
  latest_date: string | null;
  total_records: number;
  cash: number | null;
}

/** performance 端点 summary（含 compute_extended_summary 扩展指标） */
export interface Summary {
  start_equity: number;
  end_equity: number;
  total_return: number;
  max_drawdown: number;
  records: number;
  sharpe: number | null;
  win_rate: number | null;
  profit_factor: number | null;
  closed_trades: number | null;
  total_fee: number | null;
  fee_ratio: number | null;
  avg_hold_days: number | null;
  position_time_ratio: number | null;
  biggest_win: number | null;
  biggest_loss: number | null;
  avg_trade_pnl: number | null;
  expectancy: number | null;
  avg_trade_size: number | null;
  median_trade_size: number | null;
  median_hold_days: number | null;
}

export interface EquityPoint {
  date: string;
  cash: number;
  market_value: number;
  equity: number;
  action?: string | null;
}

export interface Performance {
  agent: string;
  points: EquityPoint[];
  summary: Summary;
}

export interface PositionRecord {
  date: string;
  id: number;
  this_action?: { action: string; symbol: string; amount: number; price?: number } | null;
  positions: Record<string, number>;
}

/** 单轮 token 用量（usage_est=true 表示按字符估算，非供应商真实计量） */
export interface LogUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  usage_est?: boolean;
}

export interface LogLine {
  signature?: string;
  /** 日志写入时间 ISO（后端返回，如 2026-08-31T14:00:28） */
  timestamp?: string;
  /** 'review' = 盘后复盘轮（只读沉淀，不下单） */
  kind?: string;
  usage?: LogUsage | null;
  new_messages?: { role?: string; content?: string }[];
}

/** /agents/{name}/trades 返回：顶层 action/symbol/amount/cash_after（price/notional 由后端重算） */
export interface TradeRecord {
  date: string;
  action: string; // 'buy' | 'sell'
  symbol: string;
  amount: number;
  cash_after: number;
  price: number | null;
  notional: number | null;
}

export interface OverviewRow {
  name: string;
  latest_date: string | null;
  records: number;
  cash: number | null;
  summary: Summary | null;
}

export interface Overview {
  markets: Record<MarketId, OverviewRow[]>;
}

/** /api/metrics：服务健康 + 各市场统计 + 最近交易时间 */
export interface Metrics {
  services: Record<string, 'up' | 'down'>;
  markets?: Record<MarketId, { agents: number; position_records: number; memory_lines: number }>;
  latest_trade_age_sec: number | null;
  generated_at?: number;
}

export const SERVICE_NAMES: Record<string, string> = {
  api: 'API',
  mcp_us: '美股 MCP',
  mcp_cn: 'A股 MCP',
  mcp_hk: '港股 MCP',
  dsh: 'dsh 引擎',
};

// ---------- 端点封装 ----------

/** 解包 {success, data} 信封（先 await 再取 data） */
const unwrap = async <T>(promise: Promise<{ data: { success: boolean; data: T } }>): Promise<T> =>
  (await promise).data.data;

export const fetchOverview = () =>
  unwrap<Overview>(api.get('/overview')).then((d) => d);

export const fetchMetrics = () =>
  unwrap<Metrics>(api.get('/metrics')).then((d) => d);

export const fetchAgents = (market: MarketId) =>
  unwrap<AgentInfo[]>(api.get('/agents', { params: { market } }));

export const fetchPerformance = (agent: string, market: MarketId) =>
  unwrap<Performance>(api.get(`/agents/${encodeURIComponent(agent)}/performance`, { params: { market } }));

export const fetchPositions = (agent: string, market: MarketId) =>
  unwrap<PositionRecord[]>(api.get(`/agents/${encodeURIComponent(agent)}/positions`, { params: { market } }));

export const fetchTrades = (agent: string, market: MarketId) =>
  unwrap<TradeRecord[]>(api.get(`/agents/${encodeURIComponent(agent)}/trades`, { params: { market } }));

/** 分析日志（每行 = 一个分析回合）。limit>0 只取最近 N 条：
 *  全量日志累积 1MB+，对话 tab 每 30s 全量拉取是页面卡顿主因（2026-09-08）。 */
export const fetchLogs = (agent: string, market: MarketId, limit = 0) =>
  unwrap<LogLine[]>(api.get(`/agents/${encodeURIComponent(agent)}/logs`, {
    params: limit > 0 ? { market, limit } : { market },
  }));

// ---------- 最新价格（滚动价格条） ----------

export interface PriceQuote {
  price: number;
  date: string; // YYYY-MM-DD
  prev_close: number | null;
  change_pct: number | null; // 涨跌幅（昨收基准），无昨收为 null
}

/** 每只股票最新收盘价（键 = symbol，如 "600028.SH"） */
export const fetchPrices = (market: MarketId) =>
  unwrap<Record<string, PriceQuote>>(api.get('/prices', { params: { market } }));

/** 股票中文名表（键 = symbol） */
export const fetchStockNames = (market: MarketId) =>
  unwrap<Record<string, string>>(api.get('/stock-names', { params: { market } }));

// ---------- 通达信桥实盘（A股） ----------

export interface LivePosition {
  stock_code: string; // "600183.SH"
  name: string;
  cost_price: number;
  total_volume: number;
  available_volume: number;
  last_price: number;
  position_value: number;
  pnl_pct: number;
  pnl: number;
  buy_time: string; // "2026-08-31T11:13"
}

export interface LiveAccount {
  asset: number;
  positions: LivePosition[];
  channel_used?: string;
}

export interface LiveTradeLog {
  ts: string;
  mode: string; // "execute" | "execute_intraday" | ...
  /** 下单模型（早于归属改造的记录为 null） */
  agent?: string | null;
  code: string;
  /** 通达信桥回传的成本价（部分记录缺失 → 0） */
  cost_price?: number | null;
  name?: string; // 富途订单自带 stock_name；cn 由前端 stockNames 解析
  side?: string; // BUY/SELL（富途）；cn 由桥当日委托回报补全
  volume: number;
  price?: number | null; // 桥 filled_price（成交价）
  limit_price?: number | null;
  result?: { order_id?: string; status?: string; message?: string } | null;
  fill?: { order_id?: string; filled_price?: number; filled_volume?: number } | null;
  message?: string | null;
  /** 人工对账说明（mode="fill_adjust" 专用，如 09-08 误卖归还） */
  note?: string | null;
}

export const fetchLiveAccount = () => unwrap<LiveAccount>(api.get('/live/account'));
export const fetchLiveTrades = () => unwrap<LiveTradeLog[]>(api.get('/live/trades'));

// ---------- 港股富途实盘账户（自建 FutuOpenD 容器，经 /api/v1/agent-arena/futu/* 直连） ----------
// 富途模拟/实盘账户；futu 原始 positions 是 {code: {...}} dict，reshape 成 cn LiveAccount
// 同一 shape，Live 页持仓/实盘 tab 复用 cn 渲染逻辑（排版与 A 股一致）。
interface FutuPositionRaw {
  volume: number;
  available_volume: number;
  price: number;
  market_value: number;
  cost: number;
  name: string;
  currency: string;
}
interface FutuAccountRaw {
  total_asset: number;
  cash: number;
  market_value: number;
  positions: Record<string, FutuPositionRaw>;
}

/** 纯函数：富途原始账户 → 面板 LiveAccount（export 供 vitest；单卡与双卡共用）。
 *  边界口径与 reshapeQmtAccount 对齐，两处富途侧约定不能想当然：
 *  - price=0 是「拿不到价」→ 不给盈亏段（0 值盈利会伪造「平盘」）；
 *  - cost<0 真实存在（摊薄成本法）→ 绝对盈亏仍有效，百分比无意义（(P-C)/C 在 C<0 时符号颠倒）→ pnl_pct 留 0。 */
export const reshapeFutuAccount = (
  raw: FutuAccountRaw | undefined,
  env: string,
): LiveAccount => {
  const channel = `futu-${env.toLowerCase()}`;
  if (!raw) return { asset: 0, positions: [], channel_used: channel };
  const positions: LivePosition[] = Object.entries(raw.positions ?? {})
    .filter(([, p]) => Number(p.volume) > 0)
    .map(([code, p]) => {
      const cost = Number(p.cost) || 0;
      const last = Number(p.price) || 0;
      const vol = Number(p.volume) || 0;
      const hasPrice = last > 0;
      const hasPnl = hasPrice && cost !== 0;
      const hasPct = hasPnl && cost > 0;
      return {
        stock_code: code,
        name: p.name || code,
        cost_price: cost,
        total_volume: vol,
        available_volume: Number(p.available_volume) || 0,
        last_price: last,
        position_value: Number(p.market_value) || (hasPrice ? last * vol : cost * vol),
        pnl_pct: hasPct ? +(((last - cost) / cost) * 100).toFixed(2) : 0,
        pnl: hasPnl ? +((last - cost) * vol).toFixed(2) : 0,
        buy_time: '',
      };
    });
  return { asset: Number(raw.total_asset) || 0, positions, channel_used: channel };
};

export const fetchFutuAccount = async (env = 'SIMULATE'): Promise<LiveAccount> => {
  const res = await api.get('/futu/account', { params: { env } });
  // 后端统一信封 {success, data:{...}}；account 在 data.data
  const raw = (res.data?.data ?? res.data) as FutuAccountRaw | undefined;
  return reshapeFutuAccount(raw, env);
};

/** 一次子进程拉 REAL+SIMULATE 两套账户（省一次 RSA 握手，降实盘 tab 延迟）。
 *  供 Live.tsx 挂在 15s 后台轮询，让实盘 tab 点击即见（数据始终 warm）。 */
export const fetchFutuAccountBoth = async (): Promise<{
  real: LiveAccount;
  simulate: LiveAccount;
}> => {
  const res = await api.get('/futu/account-both');
  const raw = (res.data?.data ?? res.data) as
    | { real?: FutuAccountRaw; simulate?: FutuAccountRaw }
    | undefined;
  return {
    real: reshapeFutuAccount(raw?.real, 'real'),
    simulate: reshapeFutuAccount(raw?.simulate, 'simulate'),
  };
};

// ---------- 迅投 QMT（A股，只读；/qmt/account 直连 Redis 桥） ----------
// 桥返回 asset{asset,cash,market_value,frozen_cash} + positions[]，reshape 成 cn LiveAccount
// 同形状（与富途同套路），总控面板直接复用 LivePosition 渲染。
export interface QmtAccount extends LiveAccount {
  cash: number;
  market_value: number;
  frozen_cash: number;
  account_id: string;
}

interface QmtPositionRaw {
  stock_code: string;
  stock_name?: string;
  cost_price: number;
  total_volume: number;
  available_volume: number;
  market_value: number;
  last_price: number;
}

interface QmtAccountRaw {
  account_id?: string;
  asset: { asset: number; cash: number; market_value: number; frozen_cash: number } | null;
  positions: QmtPositionRaw[] | null;
}

/** 纯函数：桥原始返回 → 面板数据（export 供 vitest）。
 *  两处桥侧约定，不能想当然：
 *  - last_price=0 是「拿不到价」的约定（见 qmt_bridge._account_query）→ 盈亏留 0；
 *  - cost_price<0 是真实存在的（摊薄成本法，分红累计超过原成本）→ 绝对盈亏仍有效，
 *    百分比无意义（(P-C)/C 在 C<0 时符号颠倒）→ pnl_pct 留 0；cost_price=0 则是
 *    字段缺失，两个都不算——0 值盈利会伪造「平盘」。 */
export const reshapeQmtAccount = (raw: QmtAccountRaw): QmtAccount => {
  const a = raw.asset ?? { asset: 0, cash: 0, market_value: 0, frozen_cash: 0 };
  const positions: LivePosition[] = (raw.positions ?? [])
    .filter((p) => Number(p.total_volume) > 0)
    .map((p) => {
      const cost = Number(p.cost_price) || 0;
      const vol = Number(p.total_volume) || 0;
      const last = Number(p.last_price) || 0;
      const hasPrice = last > 0;
      const hasPnl = hasPrice && cost !== 0;
      const hasPct = hasPnl && cost > 0;
      return {
        stock_code: p.stock_code,
        name: p.stock_name || p.stock_code,
        cost_price: cost,
        total_volume: vol,
        available_volume: Number(p.available_volume) || 0,
        last_price: last,
        position_value: Number(p.market_value) || (hasPrice ? last * vol : cost * vol),
        pnl_pct: hasPct ? +(((last - cost) / cost) * 100).toFixed(2) : 0,
        pnl: hasPnl ? +((last - cost) * vol).toFixed(2) : 0,
        buy_time: '',
      };
    });
  return {
    asset: Number(a.asset) || 0,
    cash: Number(a.cash) || 0,
    market_value: Number(a.market_value) || 0,
    frozen_cash: Number(a.frozen_cash) || 0,
    account_id: raw.account_id ?? '',
    positions,
    channel_used: 'qmt',
  };
};

/** QMT 未配置 / Windows 侧策略没跑时后端返回 success:false → 返回 null（面板降级为提示）。 */
export const fetchQmtAccount = async (): Promise<QmtAccount | null> => {
  const res = await api.get('/qmt/account');
  const body = res.data as { success?: boolean; data?: QmtAccountRaw };
  if (!body?.success || !body.data) return null;
  return reshapeQmtAccount(body.data);
};

/** QMT 桥自述（Redis RPC ping，纯只读）：两处下单总闸状态、RPC 版本、账号类型。
 *  Windows 侧 rpc_allow_order_methods 与本侧 allow_trading 是两处独立闸门，
 *  界面必须分开显示；字段缺失（后端未升级）保持 undefined，界面写「未知」不猜。 */
export interface QmtStatus {
  allow_order_methods?: boolean;
  allow_trading?: boolean;
  version: string;
  account_type: string;
  server_time: string;
  account_id: string;
}

/** 桥不通 / 未配置时返回 null（面板退回「未知」，不猜）。 */
export const fetchQmtStatus = async (): Promise<QmtStatus | null> => {
  const res = await api.get('/qmt/status');
  const body = res.data as { success?: boolean; data?: Partial<QmtStatus> };
  if (!body?.success || !body.data) return null;
  const tri = (v: unknown): boolean | undefined => (typeof v === 'boolean' ? v : undefined);
  return {
    allow_order_methods: tri(body.data.allow_order_methods),
    allow_trading: tri(body.data.allow_trading),
    version: body.data.version ?? '',
    account_type: body.data.account_type ?? '',
    server_time: body.data.server_time ?? '',
    account_id: body.data.account_id ?? '',
  };
};

// 市场感知实盘账户：cn 走通达信桥 /live/account；hk 走富途（自建 OpenD）；us 走 IBKR（通道已下线）
export const fetchLiveAccountFor = (market: MarketId): Promise<LiveAccount> =>
  market === 'hk'
    ? fetchFutuAccount('SIMULATE')
    : market === 'us'
      ? (fetchIbkrAccount() as unknown as Promise<LiveAccount>)
      : fetchLiveAccount();

// ---------- 港股富途订单历史（自建 FutuOpenD 容器，经 /futu/orders 直连） ----------
// order_list_query → LiveTradeLog（同 shape，复用 cn 成交渲染）。只取 dealt_qty>0 已成交。
export interface FutuOrderRaw {
  order_id: string;
  code: string;
  name: string;
  trd_side: string;
  order_type: string;
  order_status: string;
  qty: number;
  price: number;
  dealt_qty: number;
  dealt_avg_price: number;
  create_time: string;
  last_err_msg: string;
}

export const fetchFutuTrades = async (env = 'SIMULATE'): Promise<LiveTradeLog[]> => {
  const res = await api.get('/futu/orders', { params: { env } });
  const data = (res.data?.data ?? res.data) as { orders: FutuOrderRaw[] } | undefined;
  const orders = data?.orders ?? [];
  return orders
    .filter((o) => Number(o.dealt_qty) > 0) // CANCELLED/未成交不计入成交流
    .map((o) => ({
      ts: String(o.create_time || '').replace(' ', 'T'),
      mode: 'execute',
      code: o.code,
      name: o.name,
      side: o.trd_side,
      volume: Number(o.dealt_qty) || 0,
      price: Number(o.dealt_avg_price) || null,
      limit_price: Number(o.price) || null,
      result: { order_id: o.order_id, status: o.order_status, message: o.last_err_msg || '' },
      message: o.last_err_msg || '',
    }))
    .sort((a, b) => (a.ts < b.ts ? 1 : -1));
};

// 市场感知成交：cn 走通达信 live_trade 日志 /live/trades；hk 走富途订单历史；us 走 IBKR 委托（通道已下线）
export const fetchLiveTradesFor = (market: MarketId): Promise<LiveTradeLog[]> =>
  market === 'hk'
    ? fetchFutuTrades('SIMULATE')
    : market === 'us'
      ? fetchIbkrOrders()
      : fetchLiveTrades();

// ---------- 港股富途已平仓（自建 FutuOpenD 容器，经 /futu/closed 直连） ----------
// position_list_query 已平仓行（qty==0, realized_pl!=0）→ Live「已完成」tab 港股面板。
export interface FutuClosedRow {
  code: string;
  name: string;
  cost_price: number; // 入场成本
  last_price: number; // 平仓参考价（nominal_price）
  realized_pl: number; // 实现盈亏
  currency: string;
}

export const fetchFutuClosed = async (env = 'SIMULATE'): Promise<FutuClosedRow[]> => {
  const res = await api.get('/futu/closed', { params: { env } });
  const data = (res.data?.data ?? res.data) as
    | { closed?: FutuClosedRow[] }
    | FutuClosedRow[]
    | undefined;
  // 双形状容错：本仓后端给 {closed:[...]}（旧栈解包成裸数组是 bug）；两种都认。
  return Array.isArray(data) ? data : (data?.closed ?? []);
};

// ---------- 富途下单/撤单/委托查询（QM 本地新增，2026-10-08：最小下单面板用） ----------
// 后端契约（/api/v1/agent-arena/futu/*）：
//   place  → {success, data:{success, order_id, status, filled_quantity, filled_price, message}}
//   cancel → {success, data:{success, message}}
//   orders → {success, data:{orders:[FutuOrderRaw]}}
// REAL 两条 fail-closed：闸门关 → 403 real_trading_disabled；未配置解锁 → 409 futu_unlock_required。
export interface FutuOrderInput {
  code: string;
  price: number;
  quantity: number;
  order_type: 'NORMAL' | 'MARKET';
  trd_side: 'BUY' | 'SELL';
}

export interface FutuPlaceResult {
  success: boolean;
  order_id: string;
  status: string;
  filled_quantity: number;
  filled_price: number;
  message: string;
}

export const placeFutuOrder = async (
  env: 'REAL' | 'SIMULATE',
  order: FutuOrderInput,
): Promise<FutuPlaceResult> => {
  const res = await api.post('/futu/place', { env, market: 'HK', order });
  return (res.data?.data ?? res.data) as FutuPlaceResult;
};

export const cancelFutuOrder = async (
  env: 'REAL' | 'SIMULATE',
  orderId: string,
): Promise<{ success: boolean; message: string }> => {
  const res = await api.post('/futu/cancel', { env, market: 'HK', order_id: orderId });
  return (res.data?.data ?? res.data) as { success: boolean; message: string };
};

/** 当日订单原始行（含未成交/已撤）——下单面板「当前委托」列表用。 */
export const fetchFutuOrders = async (
  env: 'REAL' | 'SIMULATE' = 'SIMULATE',
): Promise<FutuOrderRaw[]> => {
  const res = await api.get('/futu/orders', { params: { env } });
  const data = (res.data?.data ?? res.data) as { orders?: FutuOrderRaw[] } | undefined;
  return data?.orders ?? [];
};

// ---------- 实盘分账（每 agent ¥10 万虚拟子账户） ----------

export interface LedgerPosition {
  code: string;
  name: string;
  volume: number;
  cost_price: number;
  position_value: number;
  buy_ts: string;
}

export interface AgentLedger {
  quota: number;
  used: number;
  remaining: number;
  positions: LedgerPosition[];
}

export const fetchLiveLedger = () =>
  unwrap<{ agents: Record<string, AgentLedger> }>(api.get('/live/ledger'));

// ---------- 盘中新闻（/live/news → quantmind /api/v1/news/articles，Huntly/RSS 聚合 + enrichment） ----------

export interface NewsEnrichment {
  tickers?: string[];
  industries?: string[];
  sentiment_label?: 'bullish' | 'bearish' | 'neutral' | null;
  sentiment_score?: number | null;
}

export interface NewsArticle {
  id: number;
  title: string;
  summary?: string | null;
  url?: string | null;
  source_name?: string | null;
  published_at?: string | null;
  enrichment?: NewsEnrichment | null;
}

export interface LiveNews {
  articles: NewsArticle[];
  error?: string | null;
}

/** 盘中实时新闻（tickers 逗号分隔；keyword 全文关键词；hours=回溯小时；按时间倒序）。
 *  A 股按代码 tickers 筛；港股无 HK 标签库，传 keyword（腾讯/恒生/港股）做标题全文搜。 */
export const fetchLiveNews = (
  tickers: string[],
  hours = 12,
  limit = 30,
  keyword = '',
) =>
  unwrap<LiveNews>(
    api.get('/live/news', {
      params: {
        tickers: tickers.join(','),
        hours,
        limit,
        ...(keyword ? { keyword } : {}),
      },
    }),
  );

// ---------- 实盘账户净值（总账户净值图） ----------

export interface LiveEquityPoint {
  date: string;
  ts: string;
  value: number;
}

export interface LiveEquity {
  /** 总账户（桥实时总资产） */
  total: LiveEquityPoint[];
  /** 每 agent 分账虚拟净值（¥10 万起：虚拟现金 + 名下持仓 × 实时价） */
  agents: Record<string, LiveEquityPoint[]>;
}

export const fetchLiveEquity = () => unwrap<LiveEquity>(api.get('/live/equity'));

// ---------- 实盘 LLM 分析 token 累计（/api/token-usage） ----------

export interface AgentTokenUsage {
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  /** 估算条数（回填数据无真实 usage） */
  estimated: number;
  last_ts?: string | null;
}

export const fetchTokenUsage = () =>
  unwrap<{ agents: Record<string, AgentTokenUsage> }>(api.get('/token-usage'));

// ---------- 平仓明细（LAST 25 TRADES） ----------

export interface ClosedTradeDetail {
  symbol: string;
  exit_date: string; // YYYY-MM-DD
  qty: number;
  /** 实盘（通达信桥）行可能缺成本价 → 0 */
  entry_price: number;
  exit_price: number;
  notional: number;
  fee: number;
  /** 缺成本价时为 null（不可计算盈亏） */
  pnl: number | null;
  hold_days: number | null;
  /** true = 通达信桥实盘成交（非模拟盘） */
  live?: boolean;
}

/** FIFO 重建已平仓逐笔，最新在前（最多 limit 笔） */
export const fetchTradeDetail = (agent: string, market: MarketId, limit = 25) =>
  unwrap<ClosedTradeDetail[]>(
    api.get(`/agents/${encodeURIComponent(agent)}/trade-detail`, { params: { market, limit } }),
  );

// ---------- 实盘已平仓流（/api/live/closed；右侧「已完成」feed 实盘口径） ----------

export interface LiveClosedRow extends ClosedTradeDetail {
  ts: string; // 完整成交时间（feed 排序/展示用）
  agent: string; // 分账 agent
}

/** 实盘全仓清仓事件（卖出后该 agent 该股归零），最新在前 */
export const fetchLiveClosed = (limit = 60) =>
  unwrap<LiveClosedRow[]>(api.get('/live/closed', { params: { limit } }));

// ---------- 手动触发分析（对话 tab「立即分析」按钮；2026-10-08 复活：走 QM 原生 /analysis/* 台账，宿主 cron worker 消费） ----------

export interface AnalysisJob {
  id: string;
  ts: string;
  agents: string | string[];
  status: 'pending' | 'running' | 'done' | 'failed';
  note?: string;
}

/** 触发一轮手动盘中分析：'all' = 全部分账 agent，或指定模型名数组。
 *  交易时段内与整点分析同权（可真下单）；盘外只出决策不交易。 */
export const triggerLiveAnalysis = (agents: 'all' | string[]) =>
  unwrap<AnalysisJob>(api.post('/analysis/trigger', { type: 'live', agents }));

/** 手动触发一轮新闻 agent 管线（增量窗口；worker 每分钟消费，约 3-5 分钟完成） */
export const triggerNewsAnalysis = () =>
  unwrap<AnalysisJob>(api.post('/analysis/trigger', { type: 'news' }));

/** 最近手动分析任务状态（按钮回显） */
export const fetchAnalysisJobs = (limit = 5) =>
  unwrap<AnalysisJob[]>(api.get('/analysis/jobs', { params: { limit } }));

// ---------- 持仓明细（数量/成本/市值/盈亏） ----------

export interface HoldingRow {
  symbol: string;
  qty: number;
  entry_price: number;
  price: number;
  market_value: number;
  pnl: number;
  pnl_pct: number | null;
  change_pct: number | null;
  weight_pct: number | null;
}

export interface Holdings {
  holdings: HoldingRow[];
  cash: number;
  total_market_value: number;
  total_equity: number;
}

export const fetchHoldings = (agent: string, market: MarketId) =>
  unwrap<Holdings>(api.get(`/agents/${encodeURIComponent(agent)}/holdings`, { params: { market } }));

// ---------- 基准（指数） ----------

export interface BenchPoint {
  time: string; // YYYY-MM-DD
  close: number;
}

/** 基准文件统一为 AlphaVantage 风格 {"Meta Data", "Time Series (Daily)"|"(60min)": {ts: {"4. close"}}}。
 *  US 用脚本生成的等权 NASDAQ-100（data/benchmark_nasdaq100.json，与 agent 数据同步）；
 *  CN 用 SSE50 指数；HK 暂无指数文件。
 */
const parseBenchFile = (doc: unknown): BenchPoint[] => {
  const series = (doc as { 'Time Series (Daily)'?: Record<string, Record<string, string>> })[
    'Time Series (Daily)'
  ];
  if (!series) return [];
  return Object.entries(series)
    .map(([time, bar]) => ({ time, close: Number(bar['4. close']) }))
    .filter((p) => Number.isFinite(p.close));
};

// ---------- 比赛配置（每模型多选分析配置） ----------

export interface CompMode {
  id: string;
  name: string;
  prompt: string;
}

export type CompSelection = Record<string, string[]>;

export const fetchCompConfig = (market: MarketId = 'cn') =>
  unwrap<{ market: string; catalog: CompMode[]; selection: CompSelection }>(
    api.get('/comp-config', { params: { market } }),
  );

export const saveCompConfig = (market: MarketId, selection: CompSelection) =>
  unwrap<{ market: string; selection: CompSelection }>(
    api.put('/comp-config', { selection }, { params: { market } }),
  );

// ---------- 实盘同步数据（quantmind PG，通达信实盘账户） ----------

export interface RealAccountPosition {
  symbol: string;
  name: string;
  volume: number;
  cost_price: number;
  price: number;
  market_value: number;
  available_volume: number;
}

export interface RealAccount {
  ts: string | null;
  total_asset: number;
  cash: number;
  market_value: number;
  today_pnl: number;
  total_pnl: number;
  positions: RealAccountPosition[];
  /** 通道 key（tdx|qmt）与展示名；老响应可能没有 */
  account?: RealAccountChannel;
  account_id?: string;
  account_label?: string;
}

/** A 股实盘通道：通达信桥（系统实盘执行）/ 迅投 QMT（账户只读观测）。 */
export type RealAccountChannel = 'tdx' | 'qmt';

/** 两通道最新快照摘要（/live/real-accounts），供通道选择器与在线状态块。 */
export interface RealAccountSummary {
  account: RealAccountChannel;
  account_id: string;
  label: string;
  channel: string;
  ts: string | null;
  age_sec: number | null;
  total_asset: number | null;
  cash: number | null;
  market_value: number | null;
  source: string | null;
  position_count: number;
}

export interface RealLedgerRow {
  date: string;
  total_asset: number;
  cash: number;
  market_value: number;
  daily_return_pct: number | null;
  total_return_pct: number | null;
  position_count: number | null;
  source: string;
}

export interface L2FactorRow {
  ts: string;
  symbol: string;
  stock_code: string;
  name: string | null;
  now_price: number | null;
  factors: Record<string, number | null>;
}

export const fetchRealAccount = (account: RealAccountChannel = 'tdx') =>
  unwrap<RealAccount>(api.get('/live/real-account', { params: { account } }));
export const fetchRealLedger = (account: RealAccountChannel = 'tdx') =>
  unwrap<RealLedgerRow[]>(api.get('/live/real-ledger', { params: { account } }));
export const fetchRealAccounts = () =>
  unwrap<RealAccountSummary[]>(api.get('/live/real-accounts'));

// ---------- 美股实盘（通道已下线：盈透 IB Gateway 未随平台迁移，函数直接失败进占位分支） ----------

export const fetchIbkrAccount = async (): Promise<RealAccount> => {
  throw new Error('通道已下线：盈透证券 IB Gateway 未迁移');
};

export const fetchIbkrOrders = async (): Promise<LiveTradeLog[]> => {
  throw new Error('通道已下线：盈透证券 IB Gateway 未迁移');
};
export const fetchL2Factors = (limit = 200) =>
  unwrap<L2FactorRow[]>(api.get('/live/l2-factors', { params: { limit } }));

// ---------- 当日实时指数（顶部行情条） ----------

export interface IndexQuote {
  code: string;
  name: string;
  last: number; // 点位/最新价
  change_pct: number; // 百分数（0.86 = +0.86%），与实盘接口口径一致
}

/** CN：通达信桥日K聚合 6 个主流指数（盘中实时）；US：NDX100 基准文件；HK 空。 */
export const fetchIndices = (market: MarketId) =>
  unwrap<{ indices: IndexQuote[] }>(api.get('/live/indices', { params: { market } }));

export const fetchBenchmark = async (market: MarketId): Promise<BenchPoint[]> => {
  try {
    const file =
      market === 'us'
        ? '/data/benchmark_nasdaq100.json'
        : market === 'cn'
          ? '/data/A_stock/index_daily_sse_50.json'
          : null;
    if (!file) return [];
    const res = await api.get(file);
    return parseBenchFile(res.data);
  } catch {
    return [];
  }
};

// ---------- 市场→交易所映射（总控可配：哪个市场用哪个券商执行） ----------

export interface BrokerMarketInfo {
  mapping: Record<string, string>;
  choices: Record<string, string[]>;
}

export const fetchBrokerMarket = () => unwrap<BrokerMarketInfo>(api.get('/broker-market'));
export const saveBrokerMarket = (values: Record<string, string>) =>
  unwrap<BrokerMarketInfo>(api.put('/broker-market', { values }));

// ---------- 行情实验室（quantdb K线 + 策略回测） ----------
// 后端：/api/market-lab/*（backend/services/market_lab.py，PyneCore 运行时）

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
    start: string;
    end: string;
    bars: number;
    params: Record<string, number>;
  };
}

export const searchLabSymbols = (q: string, limit = 30) =>
  unwrap<LabSymbol[]>(api.get('/market-lab/symbols', { params: { q, limit } }));

export const fetchLabKlines = (symbol: string, adj: AdjMode = 'unadjusted', limit = 600) =>
  unwrap<{ symbol: string; name: string; adj: string; count: number; bars: Kline[] }>(
    api.get('/market-lab/klines', { params: { symbol, adj, limit } }),
  );

export const fetchLabStrategies = () => unwrap<LabStrategy[]>(api.get('/market-lab/strategies'));

export const runLabBacktest = (payload: {
  strategy: string;
  symbol: string;
  adj?: AdjMode;
  start?: string;
  end?: string;
  params?: Record<string, number>;
}) => unwrap<BtResult>(api.post('/market-lab/backtest', payload));

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

export interface LabPool {
  id: string;
  label: string;
  count: number;
}

export const fetchLabBatchRuns = (limit = 20) =>
  unwrap<LabBatchRun[]>(api.get('/market-lab/batch/runs', { params: { limit } }));

export const fetchLabBatchPools = () =>
  unwrap<LabPool[]>(api.get('/market-lab/batch/pools'));

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
  source_kind: 'desktop' | 'recrawl' | 'edited' | 'missing';
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
export const applyChatCandidate = (id: string, jobId: string, symbol = '', adj = 'backward') =>
  unwrap<ApplyResult>(
    api.post(`/market-lab/library/${encodeURIComponent(id)}/chat/${encodeURIComponent(jobId)}/apply`,
      { symbol, adj }),
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
) =>
  unwrap<{ queued: boolean; request: Record<string, unknown> }>(
    api.post(`/market-lab/library/${encodeURIComponent(id)}/backtest`, {
      symbol,
      adj,
      force,
    }),
  );

/** 任务状态 + 报告。报告缺失时后端可能给 {} / null，统一收敛成 null
 *  （空对象是 truthy，直接当有报告用会在读 stats 时炸掉）。 */
export const fetchPineJob = (id: string) =>
  unwrap<{ job: PineJob | null; report: PineReport | null }>(
    api.get(`/market-lab/library/${encodeURIComponent(id)}/job`),
  ).then((d) => ({
    job: d.job ?? {},
    report: d.report?.stats ? d.report : null,
  }));
