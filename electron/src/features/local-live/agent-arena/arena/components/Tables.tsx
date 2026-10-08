import { ClosedTradeDetail, Holdings, PositionRecord, TradeRecord } from '../api/client';
import { fmtDate, fmtMoneySigned, fmtNum } from '../utils/format';
import { fmtDateTime, fmtSpan } from '../utils/datetime';
import { stockName } from '../utils/symbols';
import { CHANGE_LABEL, diffSnapshots } from '../utils/positions';
import type { LiveAdjust, LiveFill } from '../utils/liveFills';

/** 证券单元格：有中文名 → 名称 + 小字灰代码；无 → 代码加粗（风格对齐 Live 实盘 pos-name/code）。
 *  代码写法跨源不一（SH600519 / 600519.SH），统一走 stockName 多格式匹配。 */
const SymCell = ({ sym, names }: { sym: string; names?: Record<string, string> }) => {
  const n = stockName(names, sym);
  if (!n) return <b>{sym}</b>;
  return (
    <span style={{ display: 'inline-flex', alignItems: 'baseline', gap: 6, whiteSpace: 'nowrap' }}>
      <b>{n}</b>
      <span className="faint" style={{ fontSize: 10, fontWeight: 400, fontVariantNumeric: 'tabular-nums' }}>{sym}</span>
    </span>
  );
};

/** LAST N TRADES 平仓明细表（日期/方向/证券/买入价/卖出价/数量/持仓时长/买入金额/卖出金额/总费用/净盈亏/收益率）。 */
export function LastTradesTable({ trades, currency = '$', names }: { trades: ClosedTradeDetail[]; currency?: string; names?: Record<string, string> }) {
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>平仓日期</th>
            <th>平仓方向</th>
            <th>证券</th>
            <th>买入价</th>
            <th>卖出价</th>
            <th>数量</th>
            <th>持仓时长</th>
            <th>买入金额</th>
            <th>卖出金额</th>
            <th>总费用</th>
            <th>净盈亏</th>
            <th>收益率</th>
          </tr>
        </thead>
        <tbody>
          {trades.length === 0 && (
            <tr><td colSpan={12} className="faint">暂无已平仓记录</td></tr>
          )}
          {trades.map((t, i) => {
            const hasEntry = t.entry_price > 0;
            const entryNotional = hasEntry ? t.qty * t.entry_price : null;
            const grossRet = hasEntry && t.exit_price > 0 ? t.exit_price / t.entry_price - 1 : null;
            return (
              <tr key={`${t.exit_date}-${t.symbol}-${i}`}>
                <td className="faint">{fmtDate(t.exit_date)}</td>
                <td className="down">▼ 卖出{t.live && <span className="tag-live">实盘</span>}</td>
                <td><SymCell sym={t.symbol} names={names} /></td>
                <td>{hasEntry ? fmtNum(t.entry_price) : '—'}</td>
                <td>{fmtNum(t.exit_price)}</td>
                <td>{t.qty.toLocaleString('en-US')}</td>
                <td className="faint">{fmtSpan(t.hold_days)}</td>
                <td>{entryNotional != null ? fmtNum(entryNotional, 0) : '—'}</td>
                <td>{fmtNum(t.notional, 0)}</td>
                <td className="faint">{t.fee ? fmtNum(t.fee, 2) : '—'}</td>
                <td className={t.pnl == null ? 'dim' : t.pnl >= 0 ? 'up' : 'down'}>
                  {fmtMoneySigned(t.pnl, currency, 2)}
                </td>
                <td className={grossRet == null ? 'dim' : grossRet >= 0 ? 'up' : 'down'}>
                  {grossRet != null ? `${grossRet >= 0 ? '+' : ''}${(grossRet * 100).toFixed(2)}%` : '—'}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {trades.some((t) => t.live) && (
        <div className="faint" style={{ marginTop: 6, fontSize: 11 }}>
          含 {trades.filter((t) => t.live).length} 笔通达信桥实盘成交；桥未回传成本价的笔次，买入价 / 收益率 / 盈亏显示 —（不可计算）
        </div>
      )}
    </div>
  );
}

/** 持仓明细表：数量/成本/最新价/市值/浮动盈亏/占比（含现金行）。 */
export function HoldingsTable({ data, currency = '$', names }: { data: Holdings | null; currency?: string; names?: Record<string, string> }) {
  if (!data) return null;
  const rows = data.holdings;
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>证券</th>
            <th>数量</th>
            <th>最新价</th>
            <th>成本价</th>
            <th>市值</th>
            <th>浮动盈亏</th>
            <th>盈亏率</th>
            <th>占比</th>
          </tr>
        </thead>
        <tbody>
          <tr className="faint">
            <td><b>CASH</b></td>
            <td colSpan={5}>{currency}{data.cash.toLocaleString('en-US', { maximumFractionDigits: 0 })}</td>
            <td className="dim">—</td>
            <td className="dim">{data.total_equity ? fmtNum(data.cash / data.total_equity * 100, 1) + '%' : '—'}</td>
          </tr>
          {rows.length === 0 && (
            <tr><td colSpan={8} className="faint">空仓 — 无持仓</td></tr>
          )}
          {rows.map((h) => (
            <tr key={h.symbol}>
              <td><SymCell sym={h.symbol} names={names} /></td>
              <td>{h.qty.toLocaleString('en-US')}</td>
              <td>{fmtNum(h.price)}</td>
              <td className="faint">{fmtNum(h.entry_price)}</td>
              <td>{fmtNum(h.market_value, 0)}</td>
              <td className={h.pnl >= 0 ? 'up' : 'down'}>{fmtMoneySigned(h.pnl, currency, 0)}</td>
              <td className={h.pnl >= 0 ? 'up' : 'down'}>{h.pnl_pct != null ? (h.pnl_pct >= 0 ? '+' : '') + fmtNum(h.pnl_pct * 100, 2) + '%' : '—'}</td>
              <td className="dim">{h.weight_pct != null ? fmtNum(h.weight_pct * 100, 1) + '%' : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="faint" style={{ marginTop: 6, fontSize: 11 }}>
        总权益 {currency}{data.total_equity.toLocaleString('en-US', { maximumFractionDigits: 0 })} = 现金 {currency}{data.cash.toLocaleString('en-US', { maximumFractionDigits: 0 })} + 持仓市值 {currency}{data.total_market_value.toLocaleString('en-US', { maximumFractionDigits: 0 })}
      </div>
    </div>
  );
}

/** 持仓记录表：{date, positions}；positions: {CASH: number, SYMBOL: qty}。 */
export function PositionsTable({ records, currency = '$', names }: { records: PositionRecord[]; currency?: string; names?: Record<string, string> }) {
  const last = records[records.length - 1];
  const rows = last ? Object.entries(last.positions) : [];

  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>日期</th>
            <th>证券</th>
            <th>数量</th>
            <th>现金</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr><td colSpan={4} className="faint">无持仓（空仓）</td></tr>
          )}
          {rows.map(([sym, qty]) => (
            <tr key={sym}>
              <td>{fmtDate(last?.date)}</td>
              <td>{sym === 'CASH' ? <span className="accent">CASH</span> : <SymCell sym={sym} names={names} />}</td>
              <td>{qty === 0 ? '—' : Number(qty).toLocaleString('en-US')}</td>
              <td>{sym === 'CASH' ? `${currency}${Number(qty).toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 交易明细表：/trades 顶层字段 {date, action, symbol, amount, cash_after, price, notional} */
export function TradesTable({ records, currency = '$', names }: { records: TradeRecord[]; currency?: string; names?: Record<string, string> }) {
  const fills = records.filter((t) => t.action === 'buy' || t.action === 'sell');
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>日期</th>
            <th>方向</th>
            <th>证券</th>
            <th>数量</th>
            <th>成交价</th>
            <th>成交金额</th>
            <th>成交后现金</th>
          </tr>
        </thead>
        <tbody>
          {fills.length === 0 && (
            <tr><td colSpan={7} className="faint">暂无成交记录</td></tr>
          )}
          {fills.map((t, i) => {
            const buy = t.action === 'buy';
            return (
              <tr key={`${t.date}-${t.symbol}-${i}`}>
                <td>{fmtDate(t.date)}</td>
                <td className={buy ? 'up' : 'down'}>{buy ? '▲ BUY' : '▼ SELL'}</td>
                <td><SymCell sym={t.symbol} names={names} /></td>
                <td>{t.amount}</td>
                <td>{fmtNum(t.price)}</td>
                <td>{t.notional != null ? `${currency}${Math.abs(t.notional).toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '—'}</td>
                <td className="faint">{currency}{Number(t.cash_after ?? 0).toLocaleString('en-US', { maximumFractionDigits: 0 })}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {records.length > fills.length && (
        <div className="faint" style={{ marginTop: 6, fontSize: 11 }}>
          另有 {records.length - fills.length} 条 no_trade（当日未成交）记录未列出
        </div>
      )}
    </div>
  );
}

/** 持仓变动时间线：逐日快照差分（新开/加仓/减仓/清仓）+ 当日落库动作。
 *  复盘看「哪天动了什么」，比重复展示每日全量持仓有用。 */
export function PositionHistory({ records, names }: { records: PositionRecord[]; names?: Record<string, string> }) {
  const diffs = diffSnapshots(records ?? []);
  if (!diffs.length) return <div className="empty-state">暂无持仓快照</div>;
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>日期</th>
            <th>当日动作</th>
            <th>持仓变动</th>
            <th>收盘持仓</th>
            <th>收盘现金</th>
          </tr>
        </thead>
        <tbody>
          {diffs.map((d) => (
            <tr key={d.date}>
              <td className="faint" style={{ whiteSpace: 'nowrap' }}>{fmtDate(d.date)}</td>
              <td>
                {d.action ? (
                  <span className={`pos-act ${d.action.action === 'buy' ? 'up' : d.action.action === 'sell' ? 'down' : 'dim'}`}>
                    {d.action.action === 'buy' ? '▲ 买入' : d.action.action === 'sell' ? '▼ 卖出' : '— 未交易'}
                  </span>
                ) : (
                  <span className="dim">—</span>
                )}
              </td>
              <td>
                {d.changes.length === 0 ? (
                  <span className="dim">无变动</span>
                ) : (
                  <span className="pos-changes">
                    {d.changes.map((c) => (
                      <span key={c.symbol} className={`pos-change ${c.kind}`}>
                        <b>{stockName(names, c.symbol) ?? c.symbol}</b>
                        <span className="pos-change-code">{c.symbol}</span>
                        <em>{CHANGE_LABEL[c.kind]} {c.delta > 0 ? '+' : ''}{c.delta.toLocaleString('en-US')}</em>
                        <span className="pos-change-qty">{c.from.toLocaleString('en-US')} → {c.to.toLocaleString('en-US')}</span>
                      </span>
                    ))}
                  </span>
                )}
              </td>
              <td>{d.holdings} 只</td>
              <td className="faint">{d.cash != null ? d.cash.toLocaleString('en-US', { maximumFractionDigits: 0 }) : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 实盘成交回报表（通达信桥）：秒级时间 / 方向 / 证券 / 数量 / 成交价 / 成交金额 / 成本价 / 订单号。
 *  模拟盘成交只到日，桥的回报精确到秒——复盘对时以这张表为准。 */
export function LiveFillsTable({
  fills,
  adjusts = [],
  currency = '¥',
  names,
}: {
  fills: LiveFill[];
  /** 人工对账行（fill_adjust）：与成交同一时间轴渲染，但不计入买卖笔数/成交额 */
  adjusts?: LiveAdjust[];
  currency?: string;
  names?: Record<string, string>;
}) {
  const buy = fills.filter((f) => f.side === 'buy');
  const sell = fills.filter((f) => f.side === 'sell');
  const turnover = fills.reduce((sum, f) => sum + f.volume * (f.price ?? 0), 0);
  const rows = [
    ...fills.map((f) => ({ kind: 'fill' as const, ts: f.ts, fill: f })),
    ...adjusts.map((a) => ({ kind: 'adjust' as const, ts: a.ts, adjust: a })),
  ].sort((a, b) => (a.ts < b.ts ? 1 : -1));
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>成交时间</th>
            <th>方向</th>
            <th>证券</th>
            <th>数量</th>
            <th>成交价</th>
            <th>成交金额</th>
            <th>成本价</th>
            <th>订单号</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr><td colSpan={8} className="faint">暂无实盘成交回报</td></tr>
          )}
          {rows.map((r, i) => (r.kind === 'adjust' ? (
            <tr key={`adj-${r.ts}-${i}`}>
              <td style={{ whiteSpace: 'nowrap' }}>{fmtDateTime(r.adjust.ts)}</td>
              <td className="faint">⚖ 对账</td>
              <td><SymCell sym={r.adjust.code} names={names} /></td>
              <td>{r.adjust.volume ? r.adjust.volume.toLocaleString('en-US') : '—'}</td>
              <td>{r.adjust.price != null ? fmtNum(r.adjust.price) : '—'}</td>
              <td>
                {r.adjust.price != null && r.adjust.volume
                  ? `${currency}${(r.adjust.volume * r.adjust.price).toLocaleString('en-US', { maximumFractionDigits: 0 })}`
                  : '—'}
              </td>
              <td colSpan={2} className="faint" style={{ whiteSpace: 'normal', maxWidth: 380 }}>
                {r.adjust.note}
              </td>
            </tr>
          ) : (
            <tr key={`${r.fill.ts}-${r.fill.code}-${i}`}>
              <td style={{ whiteSpace: 'nowrap' }}>{fmtDateTime(r.fill.ts)}</td>
              <td className={r.fill.side === 'buy' ? 'up' : 'down'}>
                {r.fill.side === 'buy' ? '▲ 买入' : '▼ 卖出'}
              </td>
              <td><SymCell sym={r.fill.code} names={names} /></td>
              <td>{r.fill.volume.toLocaleString('en-US')}</td>
              <td>{r.fill.price != null ? fmtNum(r.fill.price) : '—'}</td>
              <td>{r.fill.price != null ? `${currency}${(r.fill.volume * r.fill.price).toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '—'}</td>
              <td className="faint">{r.fill.costPrice != null ? fmtNum(r.fill.costPrice) : '—'}</td>
              <td className="faint">{r.fill.orderId ?? '—'}</td>
            </tr>
          )))}
        </tbody>
      </table>
      <div className="faint" style={{ marginTop: 6, fontSize: 11 }}>
        合计 {fills.length} 笔（买 {buy.length} / 卖 {sell.length}）· 成交额 {currency}
        {turnover.toLocaleString('en-US', { maximumFractionDigits: 0 })}
        {adjusts.length > 0 && ` · 另有对账 ${adjusts.length} 笔（不计入成交）`}
        {' '}· 时间取自通达信桥回报（秒级）
      </div>
    </div>
  );
}
