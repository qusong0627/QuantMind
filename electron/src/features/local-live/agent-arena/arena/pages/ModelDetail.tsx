import { useMemo, useState } from 'react';
import dayjs from 'dayjs';
import {
  BenchPoint,
  Holdings,
  MarketId,
  fetchBenchmark,
  fetchHoldings,
  fetchLiveEquity,
  fetchLiveTrades,
  fetchLogs,
  fetchPerformance,
  fetchPositions,
  fetchRealAccount,
  fetchStockNames,
  fetchTradeDetail,
  fetchTrades,
  marketMeta,
} from '../api/client';
import { usePolling } from '../hooks/usePolling';
import EquityChart, { toBenchLine, toChartLine } from '../components/EquityChart';
import { logoOf, modelColor, shortName } from '../components/ModelCard';
import { HoldingsTable, LastTradesTable, LiveFillsTable, PositionHistory, TradesTable } from '../components/Tables';
import ModelChat from '../components/ModelChat';
import { LiveAdjust, LiveFill, toLiveAdjust, toLiveFill } from '../utils/liveFills';
import { fmtAgo, fmtDateTime, fmtSpan } from '../utils/datetime';
import { seriesStats, dailyCloses } from '../utils/equity';
import { fmtMoney, fmtNum, fmtPct, pnlClass } from '../utils/format';
import './ModelDetail.css';

const MODEL_COLOR: Record<string, string> = {
  'deepseek-v4-flash': '#4d6bfe',
  'deepseek-v4-pro': '#8b5cf6',
};
const BENCH_COLOR = '#10a37f';
const BENCH_LABEL: Partial<Record<MarketId, string>> = { us: '纳指100', cn: '上证50' };

/** 指标矩阵的一行：左标签 + 右侧对齐值（副说明小字垫在值下方） */
function MetricRow({
  k, v, sub, cls,
}: {
  k: string;
  v: React.ReactNode;
  sub?: React.ReactNode;
  cls?: string;
}) {
  return (
    <div className="mdp-metric">
      <span className="mdp-metric-k">{k}</span>
      <span className={`mdp-metric-v ${cls ?? ''}`}>
        {v}
        {sub != null && sub !== '' && <em>{sub}</em>}
      </span>
    </div>
  );
}

/** 基准同期收益：用净值首末日期去基准序列里取最近收盘价（复盘看相对大盘）。 */function benchReturn(bench: BenchPoint[] | null | undefined, from: string, to: string): number | null {
  if (!bench?.length) return null;
  const closeOn = (date: string): number | null => {
    let best: { d: string; close: number } | null = null;
    for (const p of bench) {
      const d = String(p.time ?? '').slice(0, 10);
      if (!d || d > date) continue;
      if (!best || d > best.d) best = { d, close: p.close };
    }
    return best?.close ?? null;
  };
  const a = closeOn(from);
  const b = closeOn(to);
  if (a == null || b == null || !a) return null;
  return b / a - 1;
}

/** 指标分组的口径标记：实盘模型下，收益/行为/成本三组仍来自模拟盘回放，不能混着看。 */
function ScopeChip({ children }: { children: React.ReactNode }) {
  return <span className="mdp-scope">{children}</span>;
}

/** 模型详情页：战报头 + KPI 带 + 指标矩阵（收益风险/交易行为/成本规模/数据时效）+ 明细四表。 */
export interface ModelDetailProps {
  market?: MarketId;
  agent?: string;
  /** 返回列表（原先回「模型排行榜」，排行榜未移植，改为回本栏） */
  onBack: () => void;
}

export default function ModelDetail({ market = 'cn', agent = '', onBack }: ModelDetailProps) {
  const m = (['cn', 'hk', 'us'] as MarketId[]).includes(market as MarketId) ? (market as MarketId) : 'cn';
  const name = decodeURIComponent(agent);
  const meta = marketMeta(m);
  const [tab, setTab] = useState<'positions' | 'trades' | 'logs'>('positions');

  const perf = usePolling(() => fetchPerformance(name, m), [name, m], 30000);
  const holdings = usePolling(() => fetchHoldings(name, m), [name, m], 30000);
  const positions = usePolling(() => fetchPositions(name, m), [name, m], 30000);
  const trades = usePolling(() => fetchTrades(name, m), [name, m], 30000);
  // 懒加载：FIFO 平仓明细与决策日志数据量大，只在对应 tab 激活时才拉（首屏提速）
  const tradeDetail = usePolling(
    () => (tab === 'trades' ? fetchTradeDetail(name, m, 25) : Promise.resolve(null)),
    [name, m, tab],
    30000,
  );
  const logs = usePolling(
    // 只取最近 80 个回合：全量日志可累积到 1MB+（2026-09-08 卡顿治理）
    () => (tab === 'logs' ? fetchLogs(name, m, 80) : Promise.resolve(null)),
    [name, m, tab],
    30000,
  );
  // 页头「最近决策时刻」用最小查询（1 行），与日志 tab 的 80 行懒加载无关
  const latestLog = usePolling(() => fetchLogs(name, m, 1), [name, m], 60000);
  // 通达信桥实盘成交回报（仅 A 股）：秒级时间，复盘对时用
  const liveTrades = usePolling(() => (m === 'cn' ? fetchLiveTrades() : Promise.resolve([])), [m], 60000);
  const myLiveFills = useMemo<LiveFill[]>(
    () =>
      (liveTrades.data ?? [])
        .filter((t) => (t.agent ?? null) === name)
        .map(toLiveFill)
        .filter((f): f is LiveFill => f != null)
        .sort((a, b) => (a.ts < b.ts ? 1 : -1)),
    [liveTrades.data, name],
  );
  /** 人工对账行（fill_adjust）：09-08 误卖归还这类台账校正，独立成一类 */
  const myLiveAdjusts = useMemo<LiveAdjust[]>(
    () =>
      (liveTrades.data ?? [])
        .filter((t) => (t.agent ?? null) === name)
        .map(toLiveAdjust)
        .filter((a): a is LiveAdjust => a != null)
        .sort((a, b) => (a.ts < b.ts ? 1 : -1)),
    [liveTrades.data, name],
  );
  const bench = usePolling(() => fetchBenchmark(m), [m], 300000);
  // 股票中文名表（上证50/恒指/纳指 + quantdb 全市场），10 分钟缓存
  const stockNames = usePolling(() => fetchStockNames(m), [m], 600000);
  const names = stockNames.data ?? {};
  // 通达信桥实盘（A股）：净值分账线 + 实盘账户持仓 —— 用户口径"有 TDX 数据，模拟盘不计入"
  const liveEquity = usePolling(() => fetchLiveEquity(), [], 20000);
  const realAccount = usePolling(() => fetchRealAccount(), [], 20000);

  // 该模型是否有 TDX 实盘分账序列（≥2 采样点才算有效）
  const livePts = m === 'cn' ? (liveEquity.data?.agents?.[name] ?? null) : null;
  const isLive = !!livePts && livePts.length >= 2;
  /** 实盘分账序列统计：有实盘时页头 KPI 一律走实盘口径，模拟盘只做对照 */
  const liveStats = useMemo(() => (livePts ? seriesStats(livePts) : null), [livePts]);
  // 该模型是否有 TDX 实盘持仓（账户连通且非空）
  const realAcc = m === 'cn' ? realAccount.data : null;
  const hasReal = !!realAcc && (realAcc.positions ?? []).length > 0;

  const benchLine = useMemo(
    () =>
      !isLive && bench.data && bench.data.length
        ? toBenchLine(m === 'us' ? 'NDX100' : m === 'cn' ? 'SSE50' : '', BENCH_COLOR, bench.data)
        : null,
    [bench.data, m, isLive],
  );

  const chartLine = useMemo(() => {
    // A股实盘优先：模型有 TDX 分账序列 → 实盘净值（¥10 万名义基准），模拟盘回放不计入
    if (livePts && livePts.length >= 2) {
      return {
        id: `live-${name}`,
        label: name,
        color: MODEL_COLOR[name] ?? '#5a5a5a',
        points: livePts.map((p) => ({ t: dayjs(p.ts).valueOf(), v: p.value })),
        notional: 100000,
        // 成交标记（▲买/▼卖）：与 Live 页同口径，点在净值线上的实际成交时刻；
        // 对账行（◆）单独一类，画「台账几点归位」而不是成交
        fills: [
          ...myLiveFills.map((f) => ({ t: new Date(f.ts).getTime(), side: f.side })),
          ...myLiveAdjusts.map((a) => ({ t: new Date(a.ts).getTime(), kind: 'adjust' as const, note: a.note })),
        ],
      };
    }
    return perf.data
      ? toChartLine(perf.data.agent, perf.data.agent, MODEL_COLOR[name] ?? '#5a5a5a', perf.data.points)
      : null;
  }, [livePts, perf.data, name, myLiveFills, myLiveAdjusts]);

  // A股实盘持仓（通达信桥账户）→ Holdings 结构；有实盘则模拟盘持仓/快照不计入
  const tdxHoldings = useMemo<Holdings | null>(() => {
    if (!realAcc || !(realAcc.positions ?? []).length) return null;
    const total = realAcc.total_asset || 1;
    return {
      cash: realAcc.cash,
      total_market_value: realAcc.market_value,
      total_equity: realAcc.total_asset,
      holdings: (realAcc.positions ?? []).map((p) => {
        const qty = Number(p.volume);
        return {
          symbol: p.symbol,
          qty,
          entry_price: p.cost_price,
          price: p.price,
          market_value: p.market_value,
          pnl: p.market_value - qty * p.cost_price,
          pnl_pct: p.cost_price ? p.price / p.cost_price - 1 : null,
          change_pct: null,
          weight_pct: p.market_value / total,
        };
      }),
    };
  }, [realAcc]);

  const s = perf.data?.summary;
  const cumPnl = (s?.end_equity ?? 0) - (s?.start_equity ?? 0);
  const points = perf.data?.points ?? [];
  const firstDate = points[0]?.date?.slice(0, 10) ?? '';
  const lastDate = points[points.length - 1]?.date?.slice(0, 10) ?? '';
  // 页头 KPI 口径：有实盘分账 → 用实盘序列（模拟盘回放只作对照，避免拿 08-28 的旧值当"当前权益"）
  const headEquity = liveStats ? liveStats.last : s?.end_equity;
  const headStart = liveStats ? liveStats.first : s?.start_equity;
  const headRecords = liveStats ? liveStats.points : s?.records ?? 0;
  const headFrom = liveStats ? liveStats.from : firstDate;
  const headTo = liveStats ? liveStats.to : lastDate;
  const headCumPnl = liveStats ? liveStats.last - liveStats.first : cumPnl;
  const headReturn = liveStats ? liveStats.ret : s?.total_return ?? null;
  const headDrawdown = liveStats ? liveStats.maxDrawdown : s?.max_drawdown ?? null;
  const headSharpe = liveStats ? liveStats.sharpe : s?.sharpe ?? null;
  const benchRet = useMemo(
    () => (!isLive && firstDate && lastDate ? benchReturn(bench.data, firstDate, lastDate) : null),
    [bench.data, firstDate, lastDate, isLive],
  );
  const excess = benchRet != null && s?.total_return != null ? s.total_return - benchRet : null;

  const fills = useMemo(
    () => (trades.data ?? []).filter((t) => t.action === 'buy' || t.action === 'sell'),
    [trades.data],
  );
  const buyCount = fills.filter((t) => t.action === 'buy').length;
  const sellCount = fills.length - buyCount;
  const turnover = fills.reduce((sum, t) => sum + Math.abs(t.notional ?? 0), 0);
  const firstTrade = fills.length ? fills[fills.length - 1].date : '';
  const lastTrade = fills.length ? fills[0].date : '';
  const lastLog = latestLog.data?.length ? latestLog.data[latestLog.data.length - 1] : null;

  if (perf.error) {
    return (
      <div className="page">
        <div className="error-box">加载失败：{perf.error}</div>
      </div>
    );
  }
  if (perf.loading && !perf.data) {
    return (
      <div className="page">
        <div className="loading"><div className="spinner" />LOADING…</div>
      </div>
    );
  }

  return (
    <div className="page mdp-page" style={{ ['--mdp-accent' as string]: modelColor(name) }}>
      <div className="mdp-head">
        <button type="button" className="mdp-back" onClick={onBack}>
          ← 返回
        </button>
        <span className="mdp-logo">{logoOf(name)}</span>
        <h1 className="mdp-name">{shortName(name)}</h1>
        <span className="chip" style={{ borderRadius: 0 }}>{meta.label}</span>
        <span className="mdp-head-sub">{name}</span>
        {isLive ? <b className="mdp-live">通达信桥实盘</b> : <span className="mdp-sim">模拟盘</span>}
        <span style={{ flex: 1 }} />
        <span className="mdp-head-meta">
          <em>最近决策</em> {fmtDateTime(lastLog?.timestamp)}
          <em>距今</em> {lastLog?.timestamp ? fmtAgo(lastLog.timestamp) : '—'}
          <em>刷新</em> 30s
        </span>
      </div>

      {/* 战报头：当前权益当主角，其余降为 KPI 带；明细进下方矩阵 */}
      <div className="mdp-hero">
        <div className="mdp-hero-main">
          <div className="mdp-hero-k">当前权益 {isLive ? '· 实盘（通达信桥分账）' : '· 模拟'}</div>
          <div className="mdp-hero-v">{fmtMoney(headEquity, meta.currency)}</div>
          <div className="mdp-hero-s">
            起始 {fmtMoney(headStart, meta.currency)} · {isLive ? '采样' : '净值记录'} {headRecords}{' '}
            {isLive ? '点' : '条'} · 区间 {headFrom || '—'} → {headTo || '—'}
            {isLive && liveStats && (
              <>
                {' '}· 日频样本 {dailyCloses(livePts ?? []).length} 天
              </>
            )}
          </div>
        </div>
        <div className="mdp-kpi">
          <div className="mdp-kpi-k">累计盈亏</div>
          <div className={`mdp-kpi-v ${pnlClass(headCumPnl)}`}>
            {headCumPnl >= 0 ? '+' : ''}{fmtMoney(headCumPnl, meta.currency, 1)}
          </div>
        </div>
        <div className="mdp-kpi">
          <div className="mdp-kpi-k">总收益率</div>
          <div className={`mdp-kpi-v ${pnlClass(headReturn)}`}>{fmtPct(headReturn)}</div>
        </div>
        <div className="mdp-kpi">
          <div className="mdp-kpi-k">最大回撤</div>
          <div className="mdp-kpi-v down">{fmtPct(headDrawdown, 2, false)}</div>
        </div>
        <div className="mdp-kpi">
          <div className="mdp-kpi-k">夏普比率</div>
          <div className="mdp-kpi-v">{fmtNum(headSharpe)}</div>
        </div>
        <div className="mdp-kpi">
          <div className="mdp-kpi-k">超额收益</div>
          <div className={`mdp-kpi-v ${pnlClass(excess)}`}>{fmtPct(excess)}</div>
        </div>
      </div>

      {/* 指标矩阵：分组列，替代原 18 张等权卡片 */}
      <div className="mdp-matrix">
        <section className="mdp-cell">
          <div className="mdp-cell-head">
            收益与风险
            {isLive && <ScopeChip>模拟盘 {firstDate || '—'} → {lastDate || '—'}</ScopeChip>}
          </div>
          <MetricRow k="胜率" v={s?.win_rate != null ? fmtPct(s.win_rate, 1, false) : '—'} sub={`已平仓 ${s?.closed_trades ?? 0} 笔`} />
          <MetricRow k="盈亏比" v={s?.profit_factor != null ? fmtNum(s.profit_factor) : '—'} sub="盈利合计 / 亏损合计" />
          <MetricRow k="最大单笔盈利" v={s?.biggest_win != null ? fmtMoney(s.biggest_win, meta.currency, 1) : '—'} cls="up" />
          <MetricRow k="最大单笔亏损" v={s?.biggest_loss != null ? fmtMoney(s.biggest_loss, meta.currency, 1) : '—'} cls="down" />
          <MetricRow
            k="平均单笔盈亏"
            v={s?.avg_trade_pnl != null ? fmtMoney(s.avg_trade_pnl, meta.currency, 1) : '—'}
            cls={pnlClass(s?.avg_trade_pnl)}
          />
          <MetricRow
            k="期望值"
            v={s?.expectancy != null ? fmtMoney(s.expectancy, meta.currency, 1) : '—'}
            cls={pnlClass(s?.expectancy)}
            sub="赢率×平均盈 − 输率×平均亏"
          />
        </section>

        <section className="mdp-cell">
          <div className="mdp-cell-head">
            交易行为
            {isLive && <ScopeChip>模拟盘 {firstDate || '—'} → {lastDate || '—'}</ScopeChip>}
          </div>
          <MetricRow
            k="成交笔数"
            v={fills.length}
            sub={`${buyCount} 买 / ${sellCount} 卖`}
          />
          <MetricRow k="平均持仓" v={fmtSpan(s?.avg_hold_days)} sub={s?.avg_hold_days != null ? `${fmtNum(s.avg_hold_days, 2)} 天` : ''} />
          <MetricRow k="持仓中位数" v={fmtSpan(s?.median_hold_days)} />
          <MetricRow
            k="持仓时间占比"
            v={s?.position_time_ratio != null ? fmtPct(s.position_time_ratio, 1, false) : '—'}
          />
          <MetricRow k="成交跨度" v={firstTrade ? `${firstTrade} → ${lastTrade}` : '—'} sub={firstTrade ? `最近成交 ${lastTrade}` : ''} />
        </section>

        <section className="mdp-cell">
          <div className="mdp-cell-head">
            成本与规模
            {isLive && <ScopeChip>模拟盘 {firstDate || '—'} → {lastDate || '—'}</ScopeChip>}
          </div>
          <MetricRow k="累计费用" v={s?.total_fee != null ? fmtMoney(s.total_fee, meta.currency, 1) : '—'} sub={s?.fee_ratio != null ? `占本金 ${fmtPct(s.fee_ratio, 3, false)}` : ''} />
          <MetricRow
            k="平均单笔费用"
            v={fills.length ? fmtMoney((s?.total_fee ?? 0) / Math.max(fills.length, 1), meta.currency, 2) : '—'}
          />
          <MetricRow k="平均单笔规模" v={s?.avg_trade_size != null ? fmtMoney(s.avg_trade_size, meta.currency, 0) : '—'} />
          <MetricRow k="中位单笔规模" v={s?.median_trade_size != null ? fmtMoney(s.median_trade_size, meta.currency, 0) : '—'} />
          <MetricRow k="累计成交额" v={fills.length ? fmtMoney(turnover, meta.currency, 0) : '—'} sub="买入 + 卖出名义金额" />
        </section>

        <section className="mdp-cell">
          <div className="mdp-cell-head">数据与时效</div>
          {isLive && liveStats ? (
            <>
              <MetricRow
                k="实盘净值区间"
                v={`${liveStats.from} → ${liveStats.to}`}
                sub={`${liveStats.points} 个采样点 · 日频 ${dailyCloses(livePts ?? []).length} 天（通达信桥）`}
              />
              <MetricRow
                k="模拟盘区间"
                v={firstDate ? `${firstDate} → ${lastDate}` : '—'}
                sub={`${s?.records ?? 0} 个净值点 · 仅作对照`}
              />
            </>
          ) : (
            <MetricRow k="净值区间" v={firstDate ? `${firstDate} → ${lastDate}` : '—'} sub={`${s?.records ?? 0} 个净值点`} />
          )}
          <MetricRow
            k="最近决策"
            v={fmtDateTime(lastLog?.timestamp)}
            sub={lastLog ? `${lastLog.kind === 'review' ? '盘后复盘' : '盘中交易轮'} · ${fmtAgo(lastLog.timestamp)}` : '暂无日志'}
          />
          <MetricRow
            k="基准同期"
            v={benchRet != null ? fmtPct(benchRet) : '—'}
            cls={pnlClass(benchRet)}
            sub={
              isLive
                ? '实盘分账口径不对比指数'
                : BENCH_LABEL[m]
                  ? `${BENCH_LABEL[m]}（${firstDate || '—'} → ${lastDate || '—'}）`
                  : '无基准数据'
            }
          />
          <MetricRow
            k="超额收益"
            v={excess != null ? fmtPct(excess) : '—'}
            cls={pnlClass(excess)}
            sub={isLive ? '实盘分账口径，不对比指数' : '策略收益 − 基准同期'}
          />
        </section>
      </div>

      <div className="panel" style={{ marginBottom: 20 }}>
        <div className="panel-title">
          账户净值{' '}
          {isLive ? (
            <span className="accent" style={{ fontSize: 11 }}>通达信桥实盘 · 模拟盘不计入</span>
          ) : (
            <span className="faint">
              虚线 = 基准指数{benchLine ? `（${BENCH_LABEL[m]}）` : ''} · 区间 {firstDate} → {lastDate}
            </span>
          )}
        </div>
        <EquityChart lines={chartLine ? [chartLine] : []} benchmark={benchLine} currency={meta.currency} height={340} />
      </div>

      <div className="panel">
        <div className="tabs">
          <button className={`tab ${tab === 'positions' ? 'active' : ''}`} onClick={() => setTab('positions')}>
            持仓 {hasReal ? `(${(realAcc?.positions ?? []).length})` : `(${holdings.data?.holdings.length ?? 0})`}
          </button>
          <button className={`tab ${tab === 'trades' ? 'active' : ''}`} onClick={() => setTab('trades')}>
            成交 ({fills.length})
          </button>
          <button className={`tab ${tab === 'logs' ? 'active' : ''}`} onClick={() => setTab('logs')}>
            决策日志
          </button>
        </div>

        {tab === 'positions' && (
          <>
            {hasReal ? (
              /* 通达信桥实盘持仓为准；模拟盘持仓不计入（"持仓历史快照"也隐藏） */
              <>
                <div className="mdp-note">
                  实盘口径：以下为通达信桥账户实时持仓，模拟盘持仓与历史快照不计入。
                </div>
                <HoldingsTable data={tdxHoldings} currency={meta.currency} names={names} />
              </>
            ) : (
              <>
                <div className="mdp-note">
                  模拟盘口径：当前持仓取自最新净值快照；下方时间线为逐日快照差分（新开 / 加仓 / 减仓 / 清仓）。
                </div>
                <HoldingsTable data={holdings.data ?? null} currency={meta.currency} names={names} />
                <div className="panel-title" style={{ marginTop: 18 }}>
                  持仓变动时间线 <span className="faint">{(positions.data ?? []).length} 个快照</span>
                </div>
                <PositionHistory records={positions.data ?? []} names={names} />
              </>
            )}
          </>
        )}

        {tab === 'trades' && (
          <>
            <div className="mdp-strip">
              <span><em>成交</em>{fills.length} 笔</span>
              <span><em>买入</em>{buyCount} 笔</span>
              <span><em>卖出</em>{sellCount} 笔</span>
              <span><em>累计成交额</em>{fmtMoney(turnover, meta.currency, 0)}</span>
              <span><em>累计费用</em>{fmtMoney(s?.total_fee ?? null, meta.currency, 1)}</span>
              <span><em>已平仓</em>{s?.closed_trades ?? 0} 笔</span>
              {m === 'cn' && (
                <span>
                  <em>实盘成交</em>{myLiveFills.length} 笔（通达信桥）
                  {myLiveAdjusts.length > 0 && ` · 对账 ${myLiveAdjusts.length} 笔`}
                </span>
              )}
            </div>
            <div className="panel-title">
              LAST {tradeDetail.data?.length ?? 0} TRADES <span className="faint">FIFO 平仓明细 · 收益率 = 卖出价 / 买入价 − 1（未计费用）</span>
            </div>
            <LastTradesTable trades={tradeDetail.data ?? []} currency={meta.currency} names={names} />
            <div className="panel-title" style={{ marginTop: 18 }}>
              原始成交记录 <span className="faint">模拟盘 · 按日期倒序 · 已剔除 no_trade 记录</span>
            </div>
            <TradesTable records={trades.data ?? []} currency={meta.currency} names={names} />
            {m === 'cn' && (
              <>
                <div className="panel-title" style={{ marginTop: 18 }}>
                  实盘成交回报 <span className="faint">通达信桥 · 秒级时间 · 仅本模型</span>
                </div>
                <LiveFillsTable fills={myLiveFills} adjusts={myLiveAdjusts} currency={meta.currency} names={names} />
              </>
            )}
          </>
        )}

        {tab === 'logs' && (
          <ModelChat
            logs={logs.data ?? []}
            trades={trades.data ?? []}
            positions={positions.data ?? []}
            model={name}
            currency={meta.currency}
            names={names}
            liveFills={myLiveFills}
          />
        )}
      </div>
    </div>
  );
}
