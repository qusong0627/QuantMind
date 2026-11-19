/** 主题雷达（港股/美股/期货）：key_terms/商品品种热度 + 新闻流。 */

import { useEffect, useState } from 'react';
import { Empty, Segmented, Spin } from 'antd';
import { newsIntelService, type MarketKey, type NewsIntelItem, type TopicRow } from '../services/newsIntelService';
import { NewsTimeline } from './NewsTimeline';
import { DirectionBars } from './DirectionBars';
import { marketOf } from '../marketSymbol';

interface TopicRadarBoardProps {
  market: MarketKey;
  onOpenStock: (code: string) => void;
  onOpenArticle: (item: NewsIntelItem, code?: string | null) => void;
}

const MARKET_LABEL: Record<string, string> = { HK: '港股', US: '美股', FUTURES: '期货' };

export function TopicRadarBoard({ market, onOpenStock, onOpenArticle }: TopicRadarBoardProps) {
  const [days, setDays] = useState(5);
  const [rows, setRows] = useState<TopicRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [items, setItems] = useState<NewsIntelItem[]>([]);
  const [newsLoading, setNewsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const loader =
      market === 'FUTURES'
        ? newsIntelService.commodities(days, 40)
        : newsIntelService.topics(market, days, 40);
    loader
      .then((r) => {
        if (cancelled) return;
        setRows(r.items || []);
        setSelected((prev) =>
          prev && r.items.some((x) => x.name === prev) ? prev : r.items[0]?.name || null,
        );
      })
      .catch(() => {
        if (!cancelled) {
          setRows([]);
          setError('资讯数据暂不可用');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [market, days]);

  useEffect(() => {
    if (!selected) {
      setItems([]);
      return;
    }
    let cancelled = false;
    setNewsLoading(true);
    newsIntelService
      .topicNews(market, selected, days, 40)
      .then((r) => {
        if (!cancelled) setItems(r.items || []);
      })
      .catch(() => {
        if (!cancelled) setItems([]);
      })
      .finally(() => {
        if (!cancelled) setNewsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [market, selected, days]);

  const current = rows.find((x) => x.name === selected);
  const topTickers = (current?.top_tickers || []).filter(
    (t) => market === 'FUTURES' || marketOf(t.code) === market,
  );

  if (loading) {
    return (
      <div className="ni-loading">
        <Spin />
      </div>
    );
  }
  if (error) return <div className="ni-empty">{error}</div>;

  return (
    <div className="ni-board ni-board-topic">
      <aside className="ni-rail ni-rail-wide">
        <div className="ni-rail-head">
          <div className="ni-kicker">{market === 'FUTURES' ? '商品雷达' : `${MARKET_LABEL[market]}主题雷达`}</div>
          <Segmented
            size="small"
            value={String(days)}
            onChange={(v) => setDays(Number(v))}
            options={[
              { label: '1 日', value: '1' },
              { label: '5 日', value: '5' },
              { label: '20 日', value: '20' },
            ]}
          />
        </div>
        <div className="ni-topic-list">
          {rows.map((row) => (
            <button
              type="button"
              key={row.name}
              className={`ni-topic-row ${selected === row.name ? 'active' : ''}`}
              onClick={() => setSelected(row.name)}
            >
              <span className="ni-topic-name">{row.name}</span>
              <span className="ni-topic-n">{row.n_articles}</span>
              <span className={`ni-topic-risk ${row.n_risk > 0 ? 'is-hot' : ''}`}>风险 {row.n_risk}</span>
              <DirectionBars
                positive={row.n_positive}
                negative={row.n_negative}
                neutral={Math.max(0, row.n_articles - row.n_positive - row.n_negative)}
                height={4}
                showLabels={false}
                compact
              />
            </button>
          ))}
          {!rows.length && <Empty description="该窗口没有主题新闻" />}
        </div>
      </aside>

      <section className="ni-main">
        <header className="ni-main-head">
          <div>
            <div className="ni-kicker">{market === 'FUTURES' ? '商品品种' : '主题'}</div>
            <h2>{selected || '—'}</h2>
          </div>
          <div className="ni-main-meta">
            <span>{current?.n_articles ?? 0} 条</span>
            <span>·</span>
            <span>{days} 日窗口</span>
          </div>
        </header>
        <div className="ni-main-scroll">
          <div className="ni-callout is-research">
            <div className="ni-kicker">本页读法</div>
            <p>
              {market === 'FUTURES'
                ? '商品新闻只看热度与品种情绪；期货暂不接模型推理，不把 A 股的 P0 口径套到期货。'
                : '港股/美股只做事件分类与热度：A 股的 pre5/决策卡没有在这两个市场做过样本外验证，不自动外推。'}
            </p>
          </div>
          {current && (
            <div className="ni-topic-kpis">
              <DirectionBars
                positive={current.n_positive}
                negative={current.n_negative}
                neutral={Math.max(0, current.n_articles - current.n_positive - current.n_negative)}
                height={10}
              />
              {!!topTickers.length && (
                <div className="ni-chip-row">
                  {topTickers.map((t) => (
                    <button
                      type="button"
                      key={t.code}
                      className="ni-stock-chip"
                      onClick={() => onOpenStock(t.code)}
                      title={`${t.code} · ${t.n} 条`}
                    >
                      {t.name || t.code}
                      <em>{t.n}</em>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
          <NewsTimeline
            items={items}
            loading={newsLoading}
            emptyText="该主题在这个窗口没有匹配到新闻"
            onSelect={onOpenArticle}
          />
        </div>
      </section>

      <aside className="ni-context">
        <div className="ni-kicker">窗口读数</div>
        <dl className="ni-facts">
          <div>
            <dt>{market === 'FUTURES' ? '品种数' : '主题数'}</dt>
            <dd>{rows.length}</dd>
          </div>
          <div>
            <dt>新闻总量</dt>
            <dd>{rows.reduce((a, r) => a + r.n_articles, 0)}</dd>
          </div>
          <div>
            <dt>风险稿</dt>
            <dd>{rows.reduce((a, r) => a + r.n_risk, 0)}</dd>
          </div>
          <div>
            <dt>最热</dt>
            <dd>{rows[0]?.name || '—'}</dd>
          </div>
        </dl>
        <div className="ni-note">
          <div className="ni-kicker">口径</div>
          <p>
            主题来自 enrichment 的 key_terms；商品来自品种词表。热度是新闻口径，
            不是涨跌预测。
          </p>
        </div>
      </aside>
    </div>
  );
}
