/** 我的订阅：实盘持仓自动订阅 + 手动置顶 + 合并动态。 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Empty, Input, Spin, message } from 'antd';
import { Pin, PinOff, Plus, RefreshCw, Trash2 } from 'lucide-react';
import {
  newsIntelService,
  type MarketKey,
  type NewsIntelItem,
  type SubscriptionRow,
} from '../services/newsIntelService';
import { NewsTimeline } from './NewsTimeline';
import { marketOf } from '../marketSymbol';

interface SubscriptionsBoardProps {
  market: MarketKey;
  onOpenStock: (code: string) => void;
  onOpenArticle: (item: NewsIntelItem, code?: string | null) => void;
}

export function SubscriptionsBoard({ market, onOpenStock, onOpenArticle }: SubscriptionsBoardProps) {
  const [subs, setSubs] = useState<SubscriptionRow[]>([]);
  const [items, setItems] = useState<NewsIntelItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [addCode, setAddCode] = useState('');
  const [adding, setAdding] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [syncNote, setSyncNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [s, f] = await Promise.all([
        newsIntelService.listSubscriptions(),
        newsIntelService.feed(14, 80),
      ]);
      setSubs(s.items || []);
      setItems(f.items || []);
    } catch {
      setSubs([]);
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const sync = useCallback(async () => {
    setSyncing(true);
    try {
      const r = await newsIntelService.syncPositions();
      setSyncNote(
        r.synced > 0
          ? `已同步持仓 ${r.synced} 只（实盘 ${r.live ?? 0} · 模拟盘 ${r.sim ?? 0}）`
          : r.reason || '暂无可用持仓（实盘快照近 3 日 / 模拟盘持仓）',
      );
      await load();
    } catch {
      setSyncNote('实盘快照同步失败');
      await load();
    } finally {
      setSyncing(false);
    }
  }, [load]);

  useEffect(() => {
    if (market === 'CN') void sync();
    else void load();
    // 只在切市场时同步/加载一次；手动刷新按钮可再触发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market]);

  const visibleSubs = useMemo(
    () => subs.filter((x) => marketOf(x.symbol) === market),
    [subs, market],
  );
  const visibleItems = useMemo(
    () => items.filter((x) => !x.code || marketOf(x.code) === market),
    [items, market],
  );
  const live = useMemo(() => visibleSubs.filter((x) => x.source === 'live'), [visibleSubs]);
  const sim = useMemo(() => visibleSubs.filter((x) => x.source === 'sim'), [visibleSubs]);
  const manual = useMemo(
    () => visibleSubs.filter((x) => x.source !== 'live' && x.source !== 'sim'),
    [visibleSubs],
  );

  const add = async () => {
    const code = addCode.trim();
    if (!code) return;
    setAdding(true);
    try {
      await newsIntelService.addSubscription(code, undefined, true);
      setAddCode('');
      await load();
    } catch {
      message.error('订阅失败：请检查代码格式');
    } finally {
      setAdding(false);
    }
  };

  const togglePin = async (row: SubscriptionRow) => {
    try {
      await newsIntelService.updateSubscription(row.symbol, { pinned: !row.pinned });
      await load();
    } catch {
      message.error('置顶操作失败');
    }
  };

  const remove = async (row: SubscriptionRow) => {
    try {
      await newsIntelService.removeSubscription(row.symbol);
      await load();
    } catch {
      message.error('取消订阅失败');
    }
  };

  if (loading) {
    return (
      <div className="ni-loading">
        <Spin />
      </div>
    );
  }

  return (
    <div className="ni-board ni-board-subs">
      <aside className="ni-rail ni-rail-wide">
        <div className="ni-rail-head">
          <div className="ni-kicker">我的订阅</div>
          <div className="ni-sub-add">
            <Input
              size="small"
              value={addCode}
              onChange={(e) => setAddCode(e.target.value)}
              onPressEnter={add}
              placeholder="股票代码（如 600519.SH / 0700.HK / AAPL）"
            />
            <button type="button" className="ni-ghost-btn" onClick={add} disabled={adding}>
              <Plus size={14} /> 订阅
            </button>
          </div>
          <div className="ni-sub-sync">
            {market === 'CN' ? (
              <>
                <button type="button" className="ni-ghost-btn" onClick={() => void sync()} disabled={syncing}>
                  <RefreshCw size={13} className={syncing ? 'is-spinning' : ''} /> 同步持仓
                </button>
                {syncNote && <span className="ni-muted">{syncNote}</span>}
              </>
            ) : (
              <span className="ni-muted">自动订阅目前覆盖 A 股实盘/模拟盘持仓；当前显示{market === 'HK' ? '港股' : '美股'}手动订阅。</span>
            )}
          </div>
        </div>
        <div className="ni-sub-list">
          {!!live.length && (
            <div className="ni-sub-section">
              <div className="ni-kicker">实盘自动 · {live.length}</div>
              {live.map((row) => (
                <div className="ni-sub-row" key={row.symbol}>
                  <button type="button" className="ni-sub-name" onClick={() => onOpenStock(row.symbol)}>
                    {row.name || row.symbol}
                    <em>{row.symbol}</em>
                  </button>
                  <span className="ni-source-badge is-live">实盘</span>
                  <button
                    type="button"
                    className={`ni-icon-btn ${row.pinned ? 'is-on' : ''}`}
                    onClick={() => togglePin(row)}
                    title={row.pinned ? '取消置顶' : '置顶'}
                  >
                    {row.pinned ? <Pin size={14} /> : <PinOff size={14} />}
                  </button>
                  <button type="button" className="ni-icon-btn" onClick={() => remove(row)} title="取消订阅">
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}
          {!!sim.length && (
            <div className="ni-sub-section">
              <div className="ni-kicker">模拟盘自动 · {sim.length}</div>
              {sim.map((row) => (
                <div className="ni-sub-row" key={row.symbol}>
                  <button type="button" className="ni-sub-name" onClick={() => onOpenStock(row.symbol)}>
                    {row.name || row.symbol}
                    <em>{row.symbol}</em>
                  </button>
                  <span className="ni-source-badge is-sim">模拟盘</span>
                  <button
                    type="button"
                    className={`ni-icon-btn ${row.pinned ? 'is-on' : ''}`}
                    onClick={() => togglePin(row)}
                    title={row.pinned ? '取消置顶' : '置顶'}
                  >
                    {row.pinned ? <Pin size={14} /> : <PinOff size={14} />}
                  </button>
                  <button type="button" className="ni-icon-btn" onClick={() => remove(row)} title="取消订阅">
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="ni-sub-section">
            <div className="ni-kicker">手动置顶 · {manual.length}</div>
            {manual.map((row) => (
              <div className="ni-sub-row" key={row.symbol}>
                <button type="button" className="ni-sub-name" onClick={() => onOpenStock(row.symbol)}>
                  {row.name || row.symbol}
                  <em>{row.symbol}</em>
                </button>
                <span className="ni-source-badge">手动</span>
                <button
                  type="button"
                  className={`ni-icon-btn ${row.pinned ? 'is-on' : ''}`}
                  onClick={() => togglePin(row)}
                  title={row.pinned ? '取消置顶' : '置顶'}
                >
                  {row.pinned ? <Pin size={14} /> : <PinOff size={14} />}
                </button>
                <button type="button" className="ni-icon-btn" onClick={() => remove(row)} title="取消订阅">
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
            {!manual.length && <Empty description="还没有手动订阅" />}
          </div>
        </div>
      </aside>

      <section className="ni-main">
        <header className="ni-main-head">
          <div>
            <div className="ni-kicker">订阅动态</div>
            <h2>近 14 日</h2>
          </div>
          <div className="ni-main-meta">{visibleItems.length} 条</div>
        </header>
        <div className="ni-main-scroll">
          <NewsTimeline
            items={visibleItems}
            showStock
            onOpenStock={onOpenStock}
            onSelect={onOpenArticle}
            emptyText="订阅的股票近 14 日没有进入决策层的新闻"
          />
        </div>
      </section>

      <aside className="ni-context">
        <div className="ni-kicker">自动订阅</div>
        <div className="ni-note">
          <p>
            进入本页会自动同步两类持仓并置顶订阅：实盘快照（近 3 日）与模拟盘未平仓持仓。
            同一标的两个来源都持有时按实盘记。订阅幂等，不会重复；实盘标「实盘」、
            模拟盘标「模拟盘」，手动订阅标「手动」。
          </p>
        </div>
        <div className="ni-kicker">怎么用</div>
        <div className="ni-note">
          <p>
            订阅是「置顶关注」，不是买入清单。决策等级仍按研究口径计算：
            P0 回避、P2 不追、WATCH 观察。当前版本不设「已读」状态。
          </p>
        </div>
      </aside>
    </div>
  );
}
