import { useMemo, useState } from 'react';
import {
  BenchPoint,
  MarketId,
  fetchBenchmark,
  fetchHoldings,
  fetchLogs,
  fetchPerformance,
  fetchPositions,
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
import { LiveFill, toPgFill } from '../utils/liveFills';
import { fmtAgo, fmtDateTime, fmtSpan } from '../utils/datetime';
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

/** 指标分组的口径标记：明确这组数字来自哪个账本，避免实盘/模拟混看。 */
function ScopeChip({ children }: { children: React.ReactNode }) {
  return <span className="mdp-scope">{children}</span>;
}

/** 模型详情页：战报头 + KPI 带 + 指标矩阵（收益风险/交易行为/成本规模/数据时效）+ 明细四表。
 *
 *  A 股（market=cn）从 2026-10-20 起只展示 PG 分账实盘账本（`qm_agent_ledger_*`）：
 *  performance/holdings/positions/trades/trade-detail 五个端点全部走同一份账本，
 *  模拟盘回放不再混入任何统计。港股/美股仍走原有文件数据。 */
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
  // CN 台账全量成交（PG 账本行数有限，放大到 2000 保证「全部轮次」的成交归属
  // 不会被 200 条默认上限截断；远端上限 5000）
  const trades = usePolling(() => fetchTrades(name, m, m === 'cn' ? 2000 : 0), [name, m], 30000);
  // 懒加载：FIFO 平仓明细与决策日志数据量大，只在对应 tab 激活时才拉（首屏提速）
  const tradeDetail = usePolling(
    () => (tab === 'trades' ? fetchTradeDetail(name, m, 25) : Promise.resolve(null)),
    [name, m, tab],
    30000,
  );
  const logs = usePolling(
    // CN 台账取全量回合（limit=0）：历史轮次就是用户要看的账，截断会让「全部轮次」
    // 缺最早的几十轮。实测 flash 全量 338 轮 2.87MB，与原 300 轮仅差 61KB，
    // 因为早期回合文本短——放宽到全量几乎零成本。港美仍用 300 控体积。
    () => (tab === 'logs' ? fetchLogs(name, m, m === 'cn' ? 0 : 300) : Promise.resolve(null)),
    [name, m, tab],
    30000,
  );
  // 页头「最近决策时刻」用最小查询（1 行），与日志 tab 的全量懒加载无关
  const latestLog = usePolling(() => fetchLogs(name, m, 1), [name, m], 60000);
  // A 股实盘分账不对比指数：跳过基准请求（也避免 404 噪音）
  const bench = usePolling(
    () => (m === 'cn' ? Promise.resolve([] as BenchPoint[]) : fetchBenchmark(m)),
    [m],
    300000,
  );
  // 股票中文名表（上证50/恒指/纳指 + quantdb 全市场），10 分钟缓存
  const stockNames = usePolling(() => fetchStockNames(m), [m], 600000);
  const names = stockNames.data ?? {};

  /** A 股 = PG 分账实盘账本（后端 scope=live）；港美股 = 文件模拟盘。 */
  const isLive = m === 'cn';

  const s = perf.data?.summary;
  const points = perf.data?.points ?? [];
  const firstDate = points[0]?.date?.slice(0, 10) ?? '';
  const lastDate = points[points.length - 1]?.date?.slice(0, 10) ?? '';

  /** 实盘成交（秒级 ts）：图表标记 + 成交回报表共用同一份账本行。 */
  const liveFills = useMemo<LiveFill[]>(
    () =>
      isLive
        ? (trades.data ?? [])
            .map(toPgFill)
            .filter((f): f is LiveFill => f != null)
            .sort((a, b) => (a.ts < b.ts ? 1 : -1))
        : [],
    [trades.data, isLive],
  );

  const chartLine = useMemo(() => {
    if (!perf.data) return null;
    const color = MODEL_COLOR[name] ?? '#5a5a5a';
    const line = toChartLine(
      perf.data.agent,
      isLive ? `${shortName(name)} · 实盘` : perf.data.agent,
      color,
      perf.data.points,
    );
    if (!isLive) return line;
    return {
      ...line,
      // 名义本金 = 该 agent 的策略额度（PG summary 的 start_equity，TradingPolicy
      // 单一出处）；不再写死 ¥10 万——额度调到 ¥20 万后金额换算会少一半。
      notional: perf.data.summary?.start_equity ?? undefined,
      fills: liveFills.map((f) => ({ t: new Date(f.ts).getTime(), side: f.side })),
    };
  }, [perf.data, isLive, name, liveFills]);

  const benchLine = useMemo(
    () =>
      !isLive && bench.data && bench.data.length
        ? toBenchLine(m === 'us' ? 'NDX100' : '', BENCH_COLOR, bench.data)
        : null,
    [bench.data, m, isLive],
  );

  // ── 页头 KPI：实盘口径全部取 PG summary（与净值曲线/持仓同源） ──
  const headEquity = s?.end_equity ?? null;
  const headStart = isLive
    ? s?.series_start_equity ?? s?.start_equity ?? null
    : s?.start_equity ?? null;
  const headRecords = s?.records ?? points.length;
  const headCumPnl =
    headEquity != null && headStart != null ? headEquity - headStart : null;
  const headReturn = isLive ? s?.period_return ?? null : s?.total_return ?? null;
  const headDrawdown = s?.max_drawdown ?? null;
  const headSharpe = s?.sharpe ?? null;
  const benchRet = useMemo(
    () => (!isLive && firstDate && lastDate ? benchReturn(bench.data, firstDate, lastDate) : null),
    [bench.data, firstDate, lastDate, isLive],
  );
  const excess = !isLive && benchRet != null && s?.total_return != null ? s.total_return - benchRet : null;

  // ── 成交统计：实盘取账本全量 summary（不受表格条数上限影响） ──
  const fills = useMemo(
    () => (trades.data ?? []).filter((t) => t.action === 'buy' || t.action === 'sell'),
    [trades.data],
  );
  const buyCount = isLive ? s?.buy_count ?? 0 : fills.filter((t) => t.action === 'buy').length;
  const sellCount = isLive ? s?.sell_count ?? 0 : fills.length - fills.filter((t) => t.action === 'buy').length;
  const tradeCount = isLive ? s?.fills_count ?? fills.length : fills.length;
  const turnover = isLive
    ? s?.turnover ?? 0
    : fills.reduce((sum, t) => sum + Math.abs(t.notional ?? 0), 0);
  const firstTrade = isLive ? s?.first_trade_date ?? '' : fills.length ? fills[fills.length - 1].date : '';
  const lastTrade = isLive ? s?.last_trade_date ?? '' : fills.length ? fills[0].date : '';
  const totalFee = isLive ? null : s?.total_fee ?? null;
  const avgFee = !isLive && tradeCount > 0 ? (s?.total_fee ?? 0) / Math.max(tradeCount, 1) : null;
  const lastLog = latestLog.data?.length ? latestLog.data[latestLog.data.length - 1] : null;
  const holdingCount = holdings.data?.holdings.length ?? 0;

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
          <div className="mdp-hero-k">
            当前权益 {isLive ? '· 实盘（PG 分账账本）' : '· 模拟'}
          </div>
          <div className="mdp-hero-v">{fmtMoney(headEquity, meta.currency)}</div>
          <div className="mdp-hero-s">
            起始 {fmtMoney(headStart, meta.currency)} · {isLive ? '净值点' : '净值记录'} {headRecords}{' '}
            条 · 区间 {firstDate || '—'} → {lastDate || '—'}
          </div>
        </div>
        <div className="mdp-kpi">
          <div className="mdp-kpi-k">累计盈亏</div>
          <div className={`mdp-kpi-v ${pnlClass(headCumPnl)}`}>
            {headCumPnl == null ? '—' : `${headCumPnl >= 0 ? '+' : ''}${fmtMoney(headCumPnl, meta.currency, 1)}`}
          </div>
        </div>
        <div className="mdp-kpi">
          <div className="mdp-kpi-k">{isLive ? '区间收益率' : '总收益率'}</div>
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
            <ScopeChip>
              {isLive ? `实盘 ${firstDate || '—'} → ${lastDate || '—'}` : `模拟盘 ${firstDate || '—'} → ${lastDate || '—'}`}
            </ScopeChip>
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
            <ScopeChip>{isLive ? '实盘成交（PG 账本）' : `模拟盘 ${firstDate || '—'} → ${lastDate || '—'}`}</ScopeChip>
          </div>
          <MetricRow k="成交笔数" v={tradeCount} sub={`${buyCount} 买 / ${sellCount} 卖`} />
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
            <ScopeChip>{isLive ? '实盘 · 账本不记费用' : `模拟盘 ${firstDate || '—'} → ${lastDate || '—'}`}</ScopeChip>
          </div>
          <MetricRow
            k="累计费用"
            v={totalFee != null ? fmtMoney(totalFee, meta.currency, 1) : '—'}
            sub={isLive ? '成交账本不落费用字段' : s?.fee_ratio != null ? `占本金 ${fmtPct(s.fee_ratio, 3, false)}` : ''}
          />
          <MetricRow
            k="平均单笔费用"
            v={avgFee != null ? fmtMoney(avgFee, meta.currency, 2) : '—'}
          />
          <MetricRow k="平均单笔规模" v={s?.avg_trade_size != null ? fmtMoney(s.avg_trade_size, meta.currency, 0) : '—'} />
          <MetricRow k="中位单笔规模" v={s?.median_trade_size != null ? fmtMoney(s.median_trade_size, meta.currency, 0) : '—'} />
          <MetricRow k="累计成交额" v={tradeCount ? fmtMoney(turnover, meta.currency, 0) : '—'} sub="买入 + 卖出名义金额" />
        </section>

        <section className="mdp-cell">
          <div className="mdp-cell-head">数据与时效</div>
          <MetricRow
            k="净值区间"
            v={firstDate ? `${firstDate} → ${lastDate}` : '—'}
            sub={`${headRecords} 个净值点 · ${isLive ? 'PG 分账账本重建' : '文件回放'}`}
          />
          <MetricRow
            k="最近决策"
            v={fmtDateTime(lastLog?.timestamp)}
            sub={
              lastLog
                ? `${lastLog.kind === 'review' || lastLog.mode === 'review' ? '盘后复盘' : '盘中交易轮'} · ${fmtAgo(lastLog.timestamp)}`
                : '暂无日志'
            }
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
            <span className="accent" style={{ fontSize: 11 }}>PG 分账实盘 · 模拟盘不计入</span>
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
            持仓 ({holdingCount})
          </button>
          <button className={`tab ${tab === 'trades' ? 'active' : ''}`} onClick={() => setTab('trades')}>
            成交 ({tradeCount})
          </button>
          <button className={`tab ${tab === 'logs' ? 'active' : ''}`} onClick={() => setTab('logs')}>
            决策日志
          </button>
        </div>

        {tab === 'positions' && (
          <>
            {isLive ? (
              <>
                <div className="mdp-note">
                  实盘口径：当前持仓取自 PG 分账账本（现金 + 持仓），价格按 Redis 实时价 →
                  最近收盘 → 缺失标 —（不以成本价冒充现价）。下方时间线为账本逐日收盘持仓差分。
                </div>
                {holdings.data ? (
                  <HoldingsTable data={holdings.data} currency={meta.currency} names={names} />
                ) : (
                  <div className="empty-state">{holdings.error ? `持仓加载失败：${holdings.error}` : '暂无实盘持仓'}</div>
                )}
                <div className="panel-title" style={{ marginTop: 18 }}>
                  持仓变动时间线 <span className="faint">{(positions.data ?? []).length} 个快照</span>
                </div>
                <PositionHistory records={positions.data ?? []} names={names} />
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
              <span><em>成交</em>{tradeCount} 笔</span>
              <span><em>买入</em>{buyCount} 笔</span>
              <span><em>卖出</em>{sellCount} 笔</span>
              <span><em>累计成交额</em>{fmtMoney(turnover, meta.currency, 0)}</span>
              <span><em>累计费用</em>{fmtMoney(totalFee, meta.currency, 1)}</span>
              <span><em>已平仓</em>{s?.closed_trades ?? 0} 笔</span>
              {isLive && <span><em>账本</em>PG 分账 · 实盘</span>}
            </div>
            <div className="panel-title">
              LAST {tradeDetail.data?.length ?? 0} TRADES <span className="faint">FIFO 平仓明细 · 收益率 = 卖出价 / 买入价 − 1（未计费用）</span>
            </div>
            <LastTradesTable trades={tradeDetail.data ?? []} currency={meta.currency} names={names} />
            {isLive ? (
              <>
                <div className="panel-title" style={{ marginTop: 18 }}>
                  实盘成交流水 <span className="faint">PG 账本 · 秒级时间 · 仅本模型 · 最近 {liveFills.length} 笔</span>
                </div>
                <LiveFillsTable fills={liveFills} currency={meta.currency} names={names} />
              </>
            ) : (
              <>
                <div className="panel-title" style={{ marginTop: 18 }}>
                  原始成交记录 <span className="faint">模拟盘 · 按日期倒序 · 已剔除 no_trade 记录</span>
                </div>
                <TradesTable records={trades.data ?? []} currency={meta.currency} names={names} />
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
            liveFills={[]}
            liveOnly={isLive}
          />
        )}
      </div>
    </div>
  );
}
