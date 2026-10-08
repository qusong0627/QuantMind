import { useMemo } from 'react';
import { useSearchParams } from '../arenaRouter';
import { useArenaNav } from '../arenaNav';
import {
  AgentLedger,
  MarketId,
  SERVICE_NAMES,
  api,
  fetchLiveAccount,
  fetchLiveEquity,
  fetchLiveLedger,
  fetchLiveTrades,
  fetchMetrics,
  fetchOverview,
  fetchQmtAccount,
  marketMeta,
} from '../api/client';
import { usePolling } from '../hooks/usePolling';
import { fmtDate, fmtMoney, fmtPct, pnlClass } from '../utils/format';
import { fmtAgo, fmtDateTime } from '../utils/datetime';
import { shortName } from '../components/ModelCard';
import TradingSettings from './TradingSettings';
import './Control.css';

const MARKET_FLAG: Record<MarketId, string> = { us: '🇺🇸', cn: '🇨🇳', hk: '🇭🇰' };

// ---------- 交易所状态（经 /api/quantmind 代理 → quantmind 8000） ----------

interface TdxStatus {
  enabled: boolean;
  bridge_url: string;
  bridge_token_configured: boolean;
  real_trading_enabled: boolean;
  health: { error?: string; tdx_connected?: boolean } | null;
}

interface BrokerStatus {
  broker: string;
  label: string;
  fields: Record<string, string | boolean>;
  loaded: boolean;
}

interface TradingStatus {
  status: string;
  mode: string;
}

async function fetchExchangeStatus() {
  const [tdx, tiger, futu, ib, qmt, rt] = await Promise.all([
    api.get('/tdx/config').then((r) => r.data as TdxStatus).catch(() => null),
    api.get('/broker-config/tiger').then((r) => r.data as BrokerStatus).catch(() => null),
    api.get('/broker-config/futu').then((r) => r.data as BrokerStatus).catch(() => null),
    api.get('/broker-config/ib').then((r) => r.data as BrokerStatus).catch(() => null),
    api.get('/broker-config/qmt').then((r) => r.data as BrokerStatus).catch(() => null),
    api.get('/real-trading/status').then((r) => r.data as TradingStatus).catch(() => null),
  ]);
  const brokers = [tiger, futu, ib, qmt]
    .filter((b): b is BrokerStatus => !!b)
    .map((b) => ({ broker: b.broker, label: b.label, fields: b.fields, loaded: true }));
  return { tdx, brokers, rt };
}

const brokerConfigured = (b: { fields: Record<string, string | boolean> }) =>
  Object.entries(b.fields).some(([k, v]) => k.endsWith('_configured') && v === true) ||
  Object.entries(b.fields).some(([k, v]) => !k.endsWith('_configured') && typeof v === 'string' && v.length > 0);

/** 总控台 —— 参考 nof0 monitor.html：服务健康条 + 三市场汇总表 + 最近交易时间。
 *  交易所设置（原 /trading）并入本页 tab：/control?view=exchange。 */
export default function Control() {
  const nav = useArenaNav();
  const [params, setParams] = useSearchParams();
  const view = params.get('view') === 'exchange' ? 'exchange' : 'overview';

  const metrics = usePolling(() => fetchMetrics(), [], 30000);
  const overview = usePolling(() => fetchOverview(), [], 30000);
  const exchange = usePolling(fetchExchangeStatus, [], 30000);
  // 实盘分账(A股, 通达信桥): 总资产 + 每 agent ¥10 万虚拟子账户, 20 秒刷新
  const liveAcct = usePolling(() => fetchLiveAccount(), [], 20000);
  const liveLedger = usePolling(() => fetchLiveLedger(), [], 20000);
  const liveEquity = usePolling(() => fetchLiveEquity(), [], 20000);
  // 迅投 QMT(A股, 只读镜像账户): QMT 桥不可达时为 null → 面板显示降级提示
  const qmtAcct = usePolling(fetchQmtAccount, [], 30000);
  // 实盘成交回报（通达信桥，秒级）：页头「最近成交」用它，别拿模拟盘落库时间冒充
  const liveTrades = usePolling(() => fetchLiveTrades(), [], 30000);

  // 最近一笔实盘成交时刻（ISO 混杂时区时按时间戳比大小，不比字符串）
  const lastFillTs = useMemo(() => {
    let best: string | null = null;
    let bestMs = -Infinity;
    for (const t of liveTrades.data ?? []) {
      const ms = Date.parse(t.ts ?? '');
      if (Number.isFinite(ms) && ms > bestMs) {
        bestMs = ms;
        best = t.ts ?? null;
      }
    }
    return best;
  }, [liveTrades.data]);

  // QMT 持仓按市值降序（50 只全列，大仓位在前）
  const qmtPositions = useMemo(
    () => [...(qmtAcct.data?.positions ?? [])].sort((a, b) => b.position_value - a.position_value),
    [qmtAcct.data],
  );

  const ageText = useMemo(() => {
    const age = metrics.data?.latest_trade_age_sec;
    if (age == null) return '无记录';
    if (age < 60) return `${age} 秒前`;
    if (age < 3600) return `${Math.floor(age / 60)} 分钟前`;
    if (age < 86400) return `${Math.floor(age / 3600)} 小时前`;
    return `${Math.floor(age / 86400)} 天前`;
  }, [metrics.data]);

  /** 服务端数据时刻（不是浏览器渲染时刻 —— 页面卡住时两者会差很多） */
  const generatedAt = useMemo(() => {
    const g = metrics.data?.generated_at;
    return g ? new Date(g * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '—';
  }, [metrics.data]);

  if (overview.error) {
    return <div className="error-box">API 连接失败：{overview.error}</div>;
  }

  return (
    <div className="page">
      {/* 紧凑页头：标题 / 视图 tab / 服务灯 / 数据时效 一行排布，窄屏自动换行 */}
      <div className="control-bar">
        <h1 className="control-title">总控台</h1>
        <div className="tabs control-tabs">
          <button
            className={`tab ${view === 'overview' ? 'active' : ''}`}
            onClick={() => setParams({})}
          >
            总控
          </button>
          <button
            className={`tab ${view === 'exchange' ? 'active' : ''}`}
            onClick={() => setParams({ view: 'exchange' })}
          >
            交易所设置
          </button>
        </div>
        <div className="svc-row">
          {Object.entries(metrics.data?.services ?? {}).length === 0 && (
            <span className="dim" style={{ fontSize: 11 }}>服务状态加载中…</span>
          )}
          {Object.entries(metrics.data?.services ?? {}).map(([k, v]) => (
            <span className="svc-chip" key={k} title={`${SERVICE_NAMES[k] ?? k}${v === 'up' ? ' 正常' : ' 掉线'}`}>
              <span className={`svc-dot ${v === 'up' ? 'svc-up' : 'svc-down'}`} />
              {SERVICE_NAMES[k] ?? k}
            </span>
          ))}
        </div>
        <span className="control-refresh">
          <em>数据时刻</em>
          {generatedAt}
          <em>最近实盘成交</em>
          {lastFillTs ? `${fmtDateTime(lastFillTs)}（${fmtAgo(lastFillTs)}）` : '—'}
          <em>模拟盘落库</em>
          {ageText}
          <em>自动刷新</em>20–30s
        </span>
      </div>

      {view === 'exchange' ? (
        <TradingSettings embedded />
      ) : (
      <>
      {/* 交易所状态 + A股实盘分账：两列并排 */}
      <div className="control-grid control-grid-2">
      <section className="mk-section" style={{ border: '2px solid #000', padding: '10px 14px' }}>
        <div className="mk-head" style={{ marginBottom: 8 }}>
          <span>🏦 交易所状态</span>
          <span className="mk-count">
            {exchange.data?.tdx
              ? `通达信桥 ${exchange.data.tdx.health?.error ? '不可达' : '在线'}`
              : '交易所数据加载中…'}
          </span>
        </div>
        {exchange.data && (
          <div className="exch-row">
            {exchange.data.tdx && (
              <span className={`svc-chip ${exchange.data.tdx.health?.error ? 'svc-down' : 'svc-up'}`}>
                <span className={`svc-dot ${exchange.data.tdx.health?.error ? 'svc-down' : 'svc-up'}`} />
                通达信桥
                <span className="exch-sub">
                  {exchange.data.tdx.health?.error ? '不可达' : '在线'}
                  {exchange.data.tdx.health?.tdx_connected ? '· 客户端已连' : ''}
                </span>
                <span className="exch-sub">
                  实盘{exchange.data.tdx.real_trading_enabled ? '开' : '关'} · 推送{exchange.data.tdx.enabled ? '开' : '关'}
                </span>
              </span>
            )}
            {exchange.data.brokers.map((b) => (
              <span key={b.broker} className={`svc-chip ${brokerConfigured(b) ? 'svc-up' : 'svc-down'}`}>
                <span className={`svc-dot ${brokerConfigured(b) ? 'svc-up' : 'svc-down'}`} />
                {b.label}
                <span className="exch-sub">{brokerConfigured(b) ? '已配置' : '未配置'}</span>
              </span>
            ))}
            {exchange.data.rt && (
              <span className={`svc-chip ${exchange.data.rt.status === 'running' ? 'svc-up' : 'svc-down'}`}>
                <span className={`svc-dot ${exchange.data.rt.status === 'running' ? 'svc-up' : 'svc-down'}`} />
                实时交易
                <span className="exch-sub">
                  {exchange.data.rt.status === 'running' ? '运行中' : '未运行'}
                  · {exchange.data.rt.mode === 'REAL' ? '实盘' : exchange.data.rt.mode === 'SIMULATION' ? '模拟盘' : exchange.data.rt.mode}
                </span>
              </span>
            )}
          </div>
        )}
      </section>

      {/* A股实盘分账(通达信桥) —— 总账户持仓 + 每 agent ¥10 万虚拟子账户 */}
      {liveAcct.data ? (
        <section className="mk-section">
          <div className="mk-head">
            <span>🇨🇳 A股实盘(通达信桥)</span>
            <span className="mk-count">
              总资产 {fmtMoney(liveAcct.data.asset, '¥', 0)}
              {' '}· {liveAcct.data.positions.length} 只持仓 · 实盘分账
            </span>
          </div>
          <div className="table-wrap mk-table">
            <table className="data">
              <thead>
                <tr>
                  <th>Agent</th>
                  <th>虚拟净值</th>
                  <th>收益率</th>
                  <th>额度已用</th>
                  <th>名下持仓</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(liveLedger.data?.agents ?? {}).map(([name, ag]: [string, AgentLedger]) => {
                  const pts = liveEquity.data?.agents?.[name] ?? [];
                  const nav = pts.length ? pts[pts.length - 1].value : null;
                  // 百分点数（5 = +5%）：与 Live.tsx / pnl_pct 全局约定一致，勿再喂给 fmtPct（它会 ×100）
                  const ret = nav != null ? (nav / (ag.quota || 100000) - 1) * 100 : null;
                  const posCount = Object.keys(ag.positions ?? {}).length;
                  return (
                    <tr key={name}>
                      <td style={{ fontWeight: 700 }}>{name}</td>
                      <td className={ret != null ? pnlClass(ret) : 'dim'}>
                        {nav != null ? fmtMoney(nav, '¥', 0) : '—'}
                      </td>
                      <td className={ret != null ? pnlClass(ret) : 'dim'}>
                        {ret != null ? `${ret >= 0 ? '+' : ''}${ret.toFixed(2)}%` : '—'}
                      </td>
                      <td className="dim">
                        ¥{Math.round(ag.used).toLocaleString('zh-CN')} / ¥{Math.round(ag.quota).toLocaleString('zh-CN')}
                      </td>
                      <td className="dim">{posCount > 0 ? `${posCount} 只` : '空仓'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      ) : (
        /* 数据未回来时占位，保持两列布局稳定（否则 grid 空半边更乱） */
        <section className="mk-section">
          <div className="mk-head">
            <span>🇨🇳 A股实盘(通达信桥)</span>
            <span className="mk-count">等待实盘账户数据…</span>
          </div>
          <div className="mk-empty">实盘账户连接中…</div>
        </section>
      )}
      </div>

      {/* 迅投 QMT 只读镜像账户：总资产卡 + 持仓明细（阶段一只读，不下单） */}
      {qmtAcct.data ? (
        <section className="mk-section">
          <div className="mk-head">
            <span>⚡ 迅投 QMT（只读）</span>
            <span className="mk-count">
              {qmtAcct.error ? '⚠ 刷新失败，显示上次数据 · ' : ''}
              账号 {qmtAcct.data.account_id} · 总资产 {fmtMoney(qmtAcct.data.asset, '¥', 0)}
              {' '}· 可用 {fmtMoney(qmtAcct.data.cash, '¥', 0)}
              {' '}· 市值 {fmtMoney(qmtAcct.data.market_value, '¥', 0)}
              {' '}· {qmtAcct.data.positions.length} 只持仓
            </span>
          </div>
          {qmtPositions.length === 0 ? (
            <div className="mk-empty">账户当前空仓</div>
          ) : (
            <div className="table-wrap mk-table qmt-pos-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>代码</th>
                    <th>名称</th>
                    <th>持仓</th>
                    <th>可用</th>
                    <th>成本</th>
                    <th>现价</th>
                    <th>市值</th>
                    <th>浮动盈亏</th>
                  </tr>
                </thead>
                <tbody>
                  {qmtPositions.map((p) => {
                    // 成本为负（摊薄成本法）时绝对盈亏有效、百分比无意义 → 只显 ¥；
                    // 成本/现价任一为 0（桥侧「拿不到」）→ 整格留白
                    const showPnl = p.last_price > 0 && p.cost_price !== 0;
                    const showPct = showPnl && p.cost_price > 0;
                    return (
                      <tr key={p.stock_code}>
                        <td className="dim">{p.stock_code}</td>
                        <td style={{ fontWeight: 700 }}>{p.name}</td>
                        <td>{p.total_volume.toLocaleString('zh-CN')}</td>
                        <td className="dim">{p.available_volume.toLocaleString('zh-CN')}</td>
                        <td className="dim">{p.cost_price !== 0 ? p.cost_price.toFixed(3) : '—'}</td>
                        <td>{p.last_price > 0 ? p.last_price.toFixed(3) : '—'}</td>
                        <td>{fmtMoney(p.position_value, '¥', 0)}</td>
                        <td className={showPnl ? pnlClass(p.pnl) : 'dim'}>
                          {showPnl
                            ? `${p.pnl >= 0 ? '+' : ''}${fmtMoney(p.pnl, '¥', 0)}${showPct ? ` (${p.pnl_pct >= 0 ? '+' : ''}${p.pnl_pct.toFixed(2)}%)` : ''}`
                            : '—'}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      ) : (
        <section className="mk-section">
          <div className="mk-head">
            <span>⚡ 迅投 QMT（只读）</span>
            <span className="mk-count">
              {qmtAcct.loading ? '加载中' : qmtAcct.error ? '查询失败' : '未连接'}
            </span>
          </div>
          <div className="mk-empty">
            {/* 三种状态分开：首轮在途时别报「未连接」误警 */}
            {qmtAcct.loading
              ? 'QMT 账户连接中…'
              : qmtAcct.error
                ? `QMT 查询失败：${qmtAcct.error}`
                : 'QMT 桥未连接——确认 Windows 大 QMT 策略在运行，或在「交易所设置 → 迅投 QMT」检查配置。'}
          </div>
        </section>
      )}

      {/* 三市场区块：cn/hk/us 三列并排 */}
      <div className="control-grid control-grid-3">
      {(['cn', 'hk', 'us'] as MarketId[]).map((m) => {
        const rows = overview.data?.markets[m] ?? [];
        const running = rows.filter((r) => r.summary).length;
        const meta = marketMeta(m);
        return (
          <section className="mk-section" key={m}>
            <div className="mk-head">
              <span>{MARKET_FLAG[m]} {meta.name}</span>
              <span className="mk-count">{running}/{rows.length} 个已交易</span>
            </div>
            {rows.length === 0 ? (
              <div className="mk-empty">暂无 agent 数据</div>
            ) : (
              <div className="table-wrap mk-table">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Agent</th>
                      <th>当前权益</th>
                      <th>收益率</th>
                      <th>最大回撤</th>
                      <th>记录数</th>
                      <th>回放截止</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => {
                      const s = r.summary;
                      return (
                        <tr
                          key={r.name}
                          className="clickable"
                          onClick={() => s && nav(`/model/${m}/${encodeURIComponent(r.name)}`)}
                        >
                          <td style={{ fontWeight: 700 }} title={r.name}>{shortName(r.name)}</td>
                          <td>{s ? fmtMoney(s.end_equity, meta.currency) : '—'}</td>
                          <td className={s ? pnlClass(s.total_return) : 'dim'}>
                            {s ? fmtPct(s.total_return) : '—'}
                          </td>
                          <td className="dim">{s ? fmtPct(s.max_drawdown, 2, false) : '—'}</td>
                          <td className="dim">{s ? s.records : r.records}</td>
                          <td className="dim">{fmtDate(s ? r.latest_date : r.latest_date)}</td>
                          <td>
                            <span className={`status-badge ${s ? 'active' : 'stopped'}`}>
                              {s ? '已交易' : '未交易'}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        );
      })}
      </div>
      </>
      )}
    </div>
  );
}
