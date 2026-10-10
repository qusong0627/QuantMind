import { useMemo } from 'react';
import dayjs from 'dayjs';
import {
  L2FactorRow,
  LiveAccount,
  MarketId,
  RealAccountChannel,
  RealAccountSummary,
  fetchIbkrAccount,
  fetchL2Factors,
  fetchRealAccount,
  fetchRealAccounts,
  fetchRealLedger,
} from '../api/client';
import { fmtAgeSec, freshnessState, ChanState } from '../utils/channelStatus';
import { fmtMoney } from '../utils/format';
import { usePolling } from '../hooks/usePolling';
// T6-3：快照 ts 可能是 aware UTC（Z）——裸截断会把 UTC 当北京展示，统一走北京口径
import { fmtBeijingDateTime } from '../../../../../utils/timeBeijing';
import EquityChart, { ChartLine } from './EquityChart';

/** 通道状态灯文案（与 ChannelStatus 同阈值口径：≤3min 实时 / ≤1h 滞后 / 更久停更） */
const chanStatusOf = (s: RealAccountSummary | null): ChanState =>
  s == null ? 'unknown' : freshnessState(s.age_sec);

const chanStatusText = (st: ChanState, ageSec?: number | null): string => {
  if (st === 'unknown') return '无快照';
  if (st === 'ok') return '在线';
  if (st === 'warn') return `滞后 ${ageSec != null ? fmtAgeSec(ageSec) : ''}`.trim();
  return `停更 ${ageSec != null ? fmtAgeSec(ageSec) : ''}`.trim();
};

/** 实盘账户 tab。
 *  A股：两通道并列可选——通达信桥（系统实盘执行）/ 迅投 QMT（只读观测），
 *  各自带账户快照、日终账本曲线；L2 因子是市场微观结构数据，两通道共用。
 *  港股：富途实盘+模拟两套账户（持仓/资金/总资产），数据由 Live.tsx 挂在 15s
 *  后台轮询（fetchFutuAccountBoth，一次握手游走 REAL+SIMULATE），点击 tab 即见，
 *  不在本组件内单独起 futu 子进程（省一次 ~4s RSA 握手）。账本与 L2 为 A 股专属。 */
export default function RealAccountPanel({
  market,
  currency = '¥',
  futuBoth = null,
  futuError = null,
  futuLoading = false,
  channel = 'tdx',
  onChannel,
}: {
  market: MarketId;
  currency?: string;
  futuBoth?: { real: LiveAccount | null; simulate: LiveAccount | null } | null;
  /** 富途双环境拉取失败原因（OpenD 未连接）；为空且无数据时不能停在「加载中」 */
  futuError?: string | null;
  /** 富途首轮是否仍在请求中（区分「正在读」与「读不到」） */
  futuLoading?: boolean;
  /** A 股通道选择（tdx=通达信桥 / qmt=迅投）；选中态由父组件持有，切 tab 不丢 */
  channel?: RealAccountChannel;
  onChannel?: (c: RealAccountChannel) => void;
}) {
  const isHk = market === 'hk';
  const isUs = market === 'us';
  const futuReal = futuBoth?.real ?? null;
  const futuSim = futuBoth?.simulate ?? null;
  const ibkr = usePolling(
    () => (isUs ? fetchIbkrAccount().catch(() => null) : Promise.resolve(null)),
    [isUs],
    20000,
  );
  // A股走 quantmind PG 快照，按 account_id 分账（tdx / qmt 两个真实账户）
  const acct = usePolling(
    () => (isHk || isUs ? Promise.resolve(null) : fetchRealAccount(channel)),
    [channel, isHk, isUs],
    20000,
  );
  const ledger = usePolling(
    () => (isHk || isUs ? Promise.resolve([]) : fetchRealLedger(channel)),
    [channel, isHk, isUs],
    30000,
  );
  const summaries = usePolling(
    () => (isHk || isUs ? Promise.resolve(null) : fetchRealAccounts().catch(() => null)),
    [isHk, isUs],
    30000,
  );
  const l2 = usePolling(() => fetchL2Factors(200), [], 20000);

  const acc = acct.data;

  // L2 因子：按 symbol 取最近一条
  const l2Latest = useMemo(() => {
    if (isHk) return [];
    const map = new Map<string, L2FactorRow>();
    for (const r of l2.data ?? []) {
      if (!map.has(r.symbol)) map.set(r.symbol, r);
    }
    return [...map.values()].sort((a, b) => a.symbol.localeCompare(b.symbol));
  }, [l2.data, isHk]);

  // ---------- 港股：富途实盘 + 模拟 两套账户卡 ----------
  if (isHk) {
    const renderFutuCard = (
      label: string,
      acctState: { data: LiveAccount | null; error?: string | null },
    ) => {
      const fa = acctState.data;
      const positions = (fa?.positions ?? []).filter((p) => Number(p.total_volume) > 0);
      const totalPnl = positions.reduce((s, p) => s + Number(p.pnl ?? 0), 0);
      return (
        <div className="real-account-card">
          <div className="real-account-head">
            <span className="real-account-title">{label}</span>
            <span className="real-account-ts">
              {fa ? `通道 ${fa.channel_used ?? 'futu'}` : futuLoading ? '读取中…' : '未读通'}
            </span>
          </div>
          {fa ? (
            <>
              <div className="real-asset-row">
                <span className="real-asset-label">总资产</span>
                <span className="real-asset-value">{fmtMoney(fa.asset, currency)}</span>
              </div>
              <div className="real-sub-row">
                <span>总浮盈 {totalPnl >= 0 ? '+' : ''}{fmtMoney(totalPnl, currency)}</span>
                <span>持仓 {positions.length} 只</span>
              </div>
              {positions.length > 0 && (
                <div className="real-positions">
                  {positions.map((p) => (
                    <div className="real-pos-row" key={p.stock_code}>
                      <span className="real-pos-id">
                        <span className="real-pos-name">{p.name || p.stock_code}</span>
                        <span className="real-pos-code">{p.stock_code}</span>
                      </span>
                      <span className="real-pos-qty">{Number(p.total_volume).toLocaleString('en-US')}</span>
                      <span className={`real-pos-val ${Number(p.pnl) >= 0 ? 'real-up' : 'real-down'}`}>
                        {Number(p.pnl) >= 0 ? '+' : ''}{fmtMoney(Number(p.pnl), currency)}
                        <span className="real-pos-pct"> {Number(p.pnl_pct) >= 0 ? '+' : ''}{Number(p.pnl_pct).toFixed(2)}%</span>
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <div className="real-pos-empty">
              {futuError || !futuLoading
                ? '富途账户读取失败：OpenD 未连接或未登录（港股行情/账户读不通）。'
                : '读取中…'}
            </div>
          )}
        </div>
      );
    };
    return (
      <div className="real-body">
        {renderFutuCard('实盘账户（富途实盘）', { data: futuReal })}
        {renderFutuCard('模拟账户（富途模拟）', { data: futuSim })}
        <div className="real-section">
          <div className="real-section-title">日终账本</div>
          <div className="empty-state" style={{ padding: '18px 0' }}>港股账本曲线待接入（富途历史净值）</div>
        </div>
      </div>
    );
  }

  // ---------- 美股：IBKR 实盘账户（ib_insync，Gateway 需本机运行） ----------
  if (isUs) {
    return (
      <div className="real-body">
        <div className="real-account-card">
          <div className="real-account-head">
            <span className="real-account-title">实盘账户（IBKR）</span>
            <span className="real-account-ts">
              {ibkr.data ? '盈透证券 Gateway' : ibkr.loading ? '加载中…' : '通道已下线'}
            </span>
          </div>
          {ibkr.data ? (
            <>
              <div className="real-asset-row">
                <span className="real-asset-label">总资产</span>
                <span className="real-asset-value">{fmtMoney(ibkr.data.total_asset, '$')}</span>
              </div>
              <div className="real-sub-row">
                <span>现金 {fmtMoney(ibkr.data.cash, '$')}</span>
                <span>市值 {fmtMoney(ibkr.data.market_value, '$')}</span>
                <span>持仓 {ibkr.data.positions.length} 只</span>
              </div>
              {ibkr.data.positions.length > 0 && (
                <div className="real-positions">
                  {ibkr.data.positions.map((p) => (
                    <div className="real-pos-row" key={p.symbol}>
                      <span className="real-pos-id">
                        <span className="real-pos-name">{p.name || p.symbol}</span>
                        <span className="real-pos-code">{p.symbol}</span>
                      </span>
                      <span className="real-pos-qty">{Number(p.volume).toLocaleString('en-US')}</span>
                      <span className="real-pos-val">{fmtMoney(Number(p.market_value), '$')}</span>
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            // 未连上时不能停在「加载中」：请求已被 catch 成 null，必须显式说没连上
            <div className="real-pos-empty">
              {ibkr.loading
                ? '读取中…'
                : '通道已下线：盈透证券 IB Gateway 未随平台迁移，账户数据不可用；美股比赛仍按本地数据集回放正常进行。'}
            </div>
          )}
        </div>
        <div className="real-section">
          <div className="real-section-title">说明</div>
          <div className="real-note">
            盈透证券通道已随原平台退役下线（未迁移）；本面板保留为历史入口。
            美股比赛仍按本地数据集回放正常进行。
          </div>
        </div>
      </div>
    );
  }

  // ---------- A股：两通道可选（通达信桥 = 系统实盘执行 / 迅投 QMT = 只读观测） ----------
  // 两账户资金量级完全不同（¥92 万 vs ¥2385 万），混着看会把 QMT 的资产当成
  // 系统实盘口径（2026-09-10 用户看到的「实盘有 2300 万」）。故并列可选，各看各的。
  const cur = channel;
  const tdxSum = summaries.data?.find((s) => s.account === 'tdx') ?? null;
  const qmtSum = summaries.data?.find((s) => s.account === 'qmt') ?? null;
  const curSum = cur === 'tdx' ? tdxSum : qmtSum;
  const chanRows = ledger.data ?? [];
  const chanTrading = chanRows.filter((r) => {
    const d = dayjs(r.date).day();
    return d !== 0 && d !== 6;
  });
  const curLedgerLine: ChartLine[] = (() => {
    const rows = chanTrading.filter((r) => r.total_asset > 0);
    if (rows.length < 2) return [];
    return [
      {
        id: `real-ledger-${cur}`,
        label: cur === 'tdx' ? '通达信账户总资产' : 'QMT 账户总资产',
        color: cur === 'tdx' ? '#111' : '#4b6cb7',
        points: rows.map((r) => ({ t: dayjs(r.date).valueOf(), v: r.total_asset })),
      },
    ];
  })();
  const lastChanLedger = chanTrading.length ? chanTrading[chanTrading.length - 1] : null;
  const chanDailyReturn = lastChanLedger?.daily_return_pct ?? null;

  return (
    <div className="real-body">
      {/* 通道选择器：各带状态灯（在线 / 停更 / 无快照），选中即切换下方全部内容 */}
      <div className="ra-chan-pick">
        {(['tdx', 'qmt'] as RealAccountChannel[]).map((k) => {
          const s = k === 'tdx' ? tdxSum : qmtSum;
          const st = chanStatusOf(s);
          const selected = cur === k;
          return (
            <button
              key={k}
              className={`ra-chan ${selected ? 'active' : ''} ra-chan-${st}`}
              onClick={() => onChannel?.(k)}
              title={s?.channel ?? ''}
            >
              <span className={`ra-chan-dot ra-chan-dot-${st}`} aria-hidden />
              <span className="ra-chan-name">{k === 'tdx' ? '通达信桥' : '迅投 QMT'}</span>
              <span className="ra-chan-meta">
                {s?.total_asset != null ? fmtMoney(s.total_asset, '¥') : '无快照'}
              </span>
              <span className={`ra-chan-tag ra-chan-tag-${st}`}>{chanStatusText(st, s?.age_sec)}</span>
            </button>
          );
        })}
      </div>
      <div className="ra-chan-note">
        {cur === 'tdx'
          ? '通达信桥：QuantMind 实盘决策的下单与成交回报通道（各模型 ¥10 万分账）。'
          : '迅投 QMT：账户读取通道，与通达信桥互为独立佐证；本系统未接其下单链路，不参与决策与执行。'}
      </div>

      {/* 账户卡 */}
      <div className="real-account-card">
        <div className="real-account-head">
          <span className="real-account-title">
            实盘账户（{cur === 'tdx' ? '通达信桥' : '迅投 QMT'}）
          </span>
          {acc?.ts && (
            <span className="real-account-ts">
              快照 {fmtBeijingDateTime(acc.ts, { withSeconds: false })}
              {curSum?.age_sec != null ? `（${fmtAgeSec(curSum.age_sec)}）` : ''}
            </span>
          )}
        </div>
        {acc ? (
          <>
            <div className="real-asset-row">
              <span className="real-asset-label">总资产</span>
              <span className="real-asset-value">{fmtMoney(acc.total_asset, '¥')}</span>
            </div>
            <div className="real-sub-row">
              <span>现金 {fmtMoney(acc.cash, '¥')}</span>
              <span>市值 {fmtMoney(acc.market_value, '¥')}</span>
              <span>持仓 {(acc.positions ?? []).length} 只</span>
              <span className={chanDailyReturn !== null && chanDailyReturn >= 0 ? 'real-up' : 'real-down'}>
                日收益{' '}
                {chanDailyReturn !== null ? `${chanDailyReturn >= 0 ? '+' : ''}${chanDailyReturn.toFixed(2)}%` : '—'}
              </span>
            </div>
            {(acc.positions ?? []).length > 0 && (
              <div className="real-positions">
                {acc.positions
                  .slice()
                  .sort((a, b) => Number(b.market_value) - Number(a.market_value))
                  .map((p) => (
                    <div className="real-pos-row" key={p.symbol}>
                      <span className="real-pos-id">
                        <span className="real-pos-name">{p.name || p.symbol}</span>
                        <span className="real-pos-code">{p.symbol}</span>
                      </span>
                      <span className="real-pos-qty">
                        {Number(p.volume).toLocaleString('en-US')}
                        {Number(p.available_volume) < Number(p.volume) && (
                          <span className="real-pos-avail">
                            {' '}可卖 {Number(p.available_volume).toLocaleString('en-US')}
                          </span>
                        )}
                      </span>
                      <span className="real-pos-val">
                        {fmtMoney(Number(p.market_value), '¥')}
                        {p.cost_price > 0 && p.price > 0 && (
                          <span className={`real-pos-pct ${p.price >= p.cost_price ? 'real-up' : 'real-down'}`}>
                            {' '}
                            {p.price >= p.cost_price ? '+' : ''}
                            {(((p.price - p.cost_price) / p.cost_price) * 100).toFixed(2)}%
                          </span>
                        )}
                      </span>
                    </div>
                  ))}
              </div>
            )}
          </>
        ) : (
          <div className="empty-state" style={{ padding: '14px 0' }}>
            {acct.error ? '实盘账户读取失败（quantmind PG 未连接）' : '加载中…'}
          </div>
        )}
      </div>

      {/* 日终账本曲线（按当前通道） */}
      <div className="real-section">
        <div className="real-section-title">
          日终账本 · {cur === 'tdx' ? '通达信桥' : '迅投 QMT'}
          <span className="real-section-sub">{chanTrading.length} 个交易日</span>
        </div>
        {curLedgerLine.length ? (
          <div className="real-chart">
            {/* 必须显式传 height：默认 380 会溢出 210px 容器压住下方 L2 表格 */}
            <EquityChart lines={curLedgerLine} benchmark={null} currency="¥" mode="dollar" timeRange="all" height={180} />
          </div>
        ) : (
          <div className="empty-state" style={{ padding: '18px 0' }}>
            {cur === 'qmt' ? '该通道账本不足（QMT 仅 1 个交易日快照）' : '账本数据不足'}
          </div>
        )}
      </div>

      {/* L2 因子（市场微观结构数据，与账户通道无关） */}
      <div className="real-section">
        <div className="real-section-title">
          L2 因子快照
          {l2.data?.length ? (
            <span className="real-section-sub">{l2.data[0].ts.slice(5, 16)} 最新</span>
          ) : null}
        </div>
        {l2Latest.length === 0 ? (
          <div className="empty-state" style={{ padding: '18px 0' }}>
            {l2.error ? 'L2 读取失败' : '暂无 L2 因子'}
          </div>
        ) : (
          <table className="real-l2-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>代码</th>
                <th>价</th>
                <th>VPIN</th>
                <th>分区</th>
                <th>价量背离</th>
                <th>冲击半衰</th>
              </tr>
            </thead>
            <tbody>
              {l2Latest.map((r) => (
                <tr key={r.symbol}>
                  <td className="real-l2-name">{r.name || r.stock_code}</td>
                  <td className="real-l2-code">{r.stock_code}</td>
                  <td>{r.now_price ?? '—'}</td>
                  <td>{fmtFactor(r.factors['micro_vpin_vol_ratio'])}</td>
                  <td>{fmtFactor(r.factors['micro_zone_distribution'])}</td>
                  <td>{fmtFactor(r.factors['vol_price_divergence'])}</td>
                  <td>{fmtFactor(r.factors['micro_impact_decay_half_life'])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

/** 因子值格式化：null → '—'，否则保留 3 位 */
const fmtFactor = (v: number | null | undefined): string =>
  v === null || v === undefined ? '—' : Number(v).toFixed(3);
