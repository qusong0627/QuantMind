import { useEffect, useMemo } from 'react';
import {
  ClosedTradeDetail,
  FutuClosedRow,
  LiveClosedRow,
  MarketId,
  LiveTradeLog,
  fetchFutuClosed,
  fetchFutuTrades,
  fetchLiveClosed,
  fetchTradeDetail,
} from '../api/client';
import { usePolling } from '../hooks/usePolling';
import { logoOf, shortName } from './ModelCard';
import { stockLabel } from '../utils/symbols';
import { availableDays, dayHit } from '../utils/dayFilter';
import { fmtMoneySigned, fmtPrice } from '../utils/format';
import './CompletedFeed.css';

interface Props {
  agents: string[]; // 当前市场全部 agent（外部可按模型筛选后传入）
  market: MarketId;
  currency: string;
  stockNames?: Record<string, string>; // symbol → 中文名（缺失时回退显示代码）
  onCount?: (n: number) => void; // 平仓消息条数回调（供父级 filter-bar 计数）
  date?: string; // 日期筛选：'all' 或 YYYY-MM-DD（按平仓日期 exit_date 收窄）
  onDates?: (days: string[]) => void; // 数据内出现的平仓日期回传（降序，供父级「日期」下拉）
}

interface AgentTrades {
  agent: string;
  trades: ClosedTradeDetail[];
}

interface FeedItem extends ClosedTradeDetail {
  agent: string;
}

/** 港股「已完成」feed 项：富途已平仓行 + 卖出成交（具体的平仓执行） */
interface HkFeedItem {
  code: string;
  name: string;
  qty: number; // 卖出数量（来自 SELL 订单；已平仓行无数量 → 0）
  entry_price: number; // 成本（来自已平仓行）
  exit_price: number; // 平仓价（SELL 成交均价 / 已平仓 nominal_price）
  realized_pl: number;
  exit_date: string; // SELL 成交时间；已平仓行无 → ''
  kind: 'closed' | 'sell'; // 已平仓持仓 / 卖出成交
}

/** 持仓时长：≥1 天按天、<1 天按小时（与 LastTradesTable 一致） */
const holdText = (days: number | null): string => {
  if (days == null) return '—';
  if (days < 1) return `${Math.round(days * 24)}H 0M`;
  return `${days}D 0H`;
};

const fmtQty = (v: number): string => v.toLocaleString('en-US', { maximumFractionDigits: 2 });

/** COMPLETED —— nof1 风格"completed a trade"平仓消息流（当前市场全部模型，最新在前）。 */
export default function CompletedFeed({
  agents,
  market,
  currency,
  stockNames = {},
  onCount,
  date = 'all',
  onDates,
}: Props) {
  const isHk = market === 'hk';
  // A股实盘口径：已完成 = /api/live/closed 真实清仓事件（模拟盘平仓文件已停更）
  const isCnLive = market === 'cn';
  const key = agents.join('|');

  // cn（实盘）/us：模拟盘 FIFO 平仓明细 —— cn 已废弃此源，仅 us 继续用
  const feeds = usePolling(
    () =>
      isCnLive
        ? Promise.resolve([] as AgentTrades[])
        : Promise.all(
            agents.map(
              async (a): Promise<AgentTrades> => ({
                agent: a,
                trades: await fetchTradeDetail(a, market, 25).catch(() => [] as ClosedTradeDetail[]),
              }),
            ),
          ),
    [key, market, isCnLive],
    30000,
  );

  // cn 实盘：真实清仓流（卖出后该 agent 该股归零；30s 轮询随成交滚动）
  const cnClosed = usePolling(
    () =>
      isCnLive
        ? fetchLiveClosed(60).catch(() => [] as LiveClosedRow[])
        : Promise.resolve([] as LiveClosedRow[]),
    [isCnLive],
    30000,
  );

  // hk：富途已平仓行 + 卖出成交（一次轮询拉两路，按 code 关联实现盈亏）
  const hkFeed = usePolling(
    async (): Promise<HkFeedItem[]> => {
      const [closed, sells] = await Promise.all([
        fetchFutuClosed('SIMULATE').catch(() => [] as FutuClosedRow[]),
        fetchFutuTrades('SIMULATE')
          .then((ts) => ts.filter((t) => String(t.side).toUpperCase() === 'SELL' && Number(t.volume) > 0))
          .catch(() => [] as LiveTradeLog[]),
      ]);
      const plByCode = new Map<string, number>();
      const entryByCode = new Map<string, number>();
      const items: HkFeedItem[] = [];
      for (const c of closed) {
        plByCode.set(c.code, Number(c.realized_pl) || 0);
        entryByCode.set(c.code, Number(c.cost_price) || 0);
        items.push({
          code: c.code,
          name: c.name || c.code,
          qty: 0,
          entry_price: Number(c.cost_price) || 0,
          exit_price: Number(c.last_price) || 0,
          realized_pl: Number(c.realized_pl) || 0,
          exit_date: '',
          kind: 'closed',
        });
      }
      for (const s of sells) {
        items.push({
          code: s.code,
          name: s.name || s.code,
          qty: Number(s.volume) || 0,
          entry_price: entryByCode.get(s.code) ?? 0,
          exit_price: Number(s.price) || 0,
          realized_pl: plByCode.get(s.code) ?? 0,
          exit_date: s.ts,
          kind: 'sell',
        });
      }
      return items.sort((a, b) => (a.exit_date < b.exit_date ? 1 : -1));
    },
    [isHk],
    30000,
  );

  const items: FeedItem[] = useMemo(() => {
    if (isCnLive) {
      // 实盘口径：真实清仓事件直接成 feed（agent 字段自带）；
      // 按顶部模型筛选收窄（'all' 时 agents=全部模型名，天然全过）
      return (cnClosed.data ?? [])
        .filter((t) => agents.length === 0 || agents.includes(t.agent))
        .map((t) => ({ ...t, agent: t.agent }));
    }
    const out: FeedItem[] = [];
    for (const f of feeds.data ?? []) {
      for (const t of f.trades) out.push({ ...t, agent: f.agent });
    }
    out.sort((a, b) => (a.exit_date < b.exit_date ? 1 : -1));
    return out;
  }, [feeds.data, cnClosed.data, isCnLive, agents]);

  const hkItems = hkFeed.data ?? [];

  // 日期候选：只随数据/模型变化（不受已选日期影响），经 effect 回传父级——渲染期
  // 直接回调会触发「渲染中更新别的组件」告警；按内容串判重避免每轮轮询空转。
  const allDays = useMemo(
    () => availableDays((isHk ? hkItems : items).map((t) => t.exit_date)),
    [isHk, hkItems, items],
  );
  const daysKey = allDays.join(',');
  useEffect(() => {
    onDates?.(daysKey ? daysKey.split(',') : []);
  }, [daysKey, onDates]);

  // 日期筛选（'all' = 不筛）；无 exit_date 的行（富途已平仓行等）只在「全部日期」下显示
  const shownItems = date === 'all' ? items : items.filter((t) => dayHit(t.exit_date, date));
  const shownHk = date === 'all' ? hkItems : hkItems.filter((t) => dayHit(t.exit_date, date));
  const count = isHk ? shownHk.length : shownItems.length;
  // 回传经 effect（渲染期直接 setState 父组件会触发 React「渲染中更新」告警）
  useEffect(() => {
    onCount?.(count);
  }, [count, onCount]);

  if (!agents.length && !isHk) return <div className="empty-state">该市场暂无 Agent</div>;

  if (isHk) {
    if (hkFeed.loading && !hkItems.length) {
      return <div className="loading"><div className="spinner" />加载中…</div>;
    }
    if (hkFeed.error && !hkItems.length) {
      return <div className="empty-state">加载失败：{hkFeed.error}</div>;
    }
    if (!hkItems.length) {
      return (
        <div className="empty-state">暂无已平仓交易（富途模拟账户：无完全平仓持仓或卖出成交）</div>
      );
    }
    if (!shownHk.length) {
      return (
        <div className="empty-state">
          该日期暂无平仓记录（富途「已平仓持仓」行无成交时间，只在「全部日期」下显示）
        </div>
      );
    }
    return (
      <div className="completed-feed">
        {shownHk.map((t, i) => {
          const pnl = t.realized_pl;
          return (
            <div className="feed-card" key={`${t.code}-${t.exit_date}-${i}`}>
              <div className="feed-head">
                <span className="feed-logo">🐂</span>
                <span className="feed-msg">
                  <b className="feed-agent">富途</b> 在{' '}
                  <b className="feed-symbol">
                    {t.name}
                    <span className="feed-code">{t.code}</span>
                  </b>{' '}
                  {t.kind === 'closed' ? '完成了一笔已平仓交易！' : '完成了一笔卖出成交！'}
                </span>
                <span className="feed-date">{t.exit_date ? t.exit_date.slice(5, 16) : '—'}</span>
              </div>
              <div className="feed-price">
                成本: {fmtPrice(t.entry_price, currency)}
                <span className="feed-arrow"> → </span>
                平仓: {fmtPrice(t.exit_price, currency)}
              </div>
              <div className="feed-grid">
                <div className="feed-cell">
                  <span className="feed-k">数量</span>
                  <span className="feed-v">{t.qty > 0 ? fmtQty(t.qty) : '—'}</span>
                </div>
                <div className="feed-cell">
                  <span className="feed-k">成交金额</span>
                  <span className="feed-v">
                    {t.qty > 0 && t.exit_price > 0
                      ? `${currency}${(t.qty * t.exit_price).toLocaleString('en-US', { maximumFractionDigits: 0 })}`
                      : '—'}
                  </span>
                </div>
                <div className="feed-cell">
                  <span className="feed-k">类型</span>
                  <span className="feed-v">{t.kind === 'closed' ? '已平仓持仓' : '卖出成交'}</span>
                </div>
              </div>
              <div className={`feed-pnl ${pnl >= 0 ? 'up' : 'down'}`}>
                实现盈亏: {fmtMoneySigned(pnl, currency, 2)}
              </div>
            </div>
          );
        })}
      </div>
    );
  }

  if ((isCnLive ? cnClosed.loading : feeds.loading) && !items.length) {
    return <div className="loading"><div className="spinner" />加载中…</div>;
  }
  if ((isCnLive ? cnClosed.error : feeds.error) && !items.length) {
    return <div className="empty-state">加载失败：{isCnLive ? cnClosed.error : feeds.error}</div>;
  }
  if (!items.length)
    return (
      <div className="empty-state">
        {isCnLive ? '暂无已平仓交易（实盘清仓成交会自动出现在这里）' : '暂无已平仓交易'}
      </div>
    );
  if (!shownItems.length)
    return <div className="empty-state">该日期暂无平仓记录（切回「全部日期」看全部）</div>;

  return (
    <div className="completed-feed">
      {shownItems.map((t, i) => {
        const pnl = t.pnl ?? 0;
        const entryNotional = t.qty * t.entry_price;
        return (
          <div className="feed-card" key={`${t.agent}-${t.exit_date}-${t.symbol}-${i}`}>
            <div className="feed-head">
              <span className="feed-logo">{logoOf(t.agent)}</span>
              <span className="feed-msg">
                <b className="feed-agent">{shortName(t.agent)}</b> 在{' '}
                <b className="feed-symbol">
                  {stockLabel(stockNames, t.symbol)}
                  <span className="feed-code">{t.symbol}</span>
                </b>{' '}
                完成了一笔交易！
              </span>
              <span className="feed-date">{t.exit_date.slice(5)}</span>
            </div>
            <div className="feed-price">
              成本: {fmtPrice(t.entry_price, currency)}
              <span className="feed-arrow"> → </span>
              平仓: {fmtPrice(t.exit_price, currency)}
            </div>
            <div className="feed-grid">
              <div className="feed-cell">
                <span className="feed-k">数量</span>
                <span className="feed-v">{fmtQty(t.qty)}</span>
              </div>
              <div className="feed-cell">
                <span className="feed-k">名义金额</span>
                <span className="feed-v">
                  {t.entry_price > 0 ? `${currency}${entryNotional.toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '—'} →{' '}
                  {t.notional > 0 ? `${currency}${t.notional.toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '—'}
                </span>
              </div>
              <div className="feed-cell">
                <span className="feed-k">持仓时长</span>
                <span className="feed-v">{holdText(t.hold_days)}</span>
              </div>
            </div>
            {/* 成本价缺失（桥回报未带成本）时盈亏算不出来，不能显示成 +¥0 */}
            <div className={`feed-pnl ${t.entry_price > 0 ? (pnl >= 0 ? 'up' : 'down') : 'dim'}`}>
              净盈亏:{' '}
              {t.entry_price > 0 ? fmtMoneySigned(pnl, currency, 2) : '—（成本价缺失）'}
            </div>
          </div>
        );
      })}
    </div>
  );
}
