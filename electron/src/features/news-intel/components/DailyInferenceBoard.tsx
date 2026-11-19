/** 每日推理：选定模型的当日榜单 × 每只票的近 30 日新闻结构。 */

import { useEffect, useMemo, useState } from 'react';
import { Empty, Segmented, Spin, message } from 'antd';
import { ExternalLink, Pin, PinOff } from 'lucide-react';
import {
  modelTrainingService,
  type InferenceRankingItem,
  type LatestInferenceRunInfo,
} from '../../../services/modelTrainingService';
import {
  newsIntelService,
  type MarketKey,
  type NewsIntelItem,
  type StockSummary,
} from '../services/newsIntelService';
import { NewsTimeline } from './NewsTimeline';
import { DirectionBars } from './DirectionBars';
import { PriceSparkline } from './PriceSparkline';
import { ResearchKeyCard } from './ResearchKeyCard';
import { StockDossier } from './StockDossier';
import { marketOf, toMarketSymbol } from '../marketSymbol';

interface DailyInferenceBoardProps {
  market: MarketKey;
  modelId: string | null;
  modelName: string;
  onOpenStock: (code: string) => void;
  onOpenArticle: (item: NewsIntelItem, code?: string | null) => void;
  onOpenMethod?: () => void;
}

function positionLabel(rank: number, total: number): string {
  if (!total) return '—';
  const pct = rank / total;
  if (pct <= 0.2) return '靠前';
  if (pct <= 0.6) return '居中';
  return '靠后';
}

/** 模型名常带完整训练区间；侧栏只留前两段，完整值放 title。 */
function shortModelName(name: string): string {
  if (!name) return '默认模型';
  const parts = name
    .split('·')
    .map((x) => x.trim())
    .filter(Boolean);
  return parts.slice(0, 2).join(' · ') || name;
}

function decisionTone(decision?: string): string {
  if (decision === 'P0') return 'is-risk';
  if (decision === 'P1') return 'is-warn';
  if (decision === 'P2') return 'is-caution';
  if (decision === 'WATCH') return 'is-watch';
  return 'is-info';
}

export function DailyInferenceBoard({
  market,
  modelId,
  modelName,
  onOpenStock,
  onOpenArticle,
  onOpenMethod,
}: DailyInferenceBoardProps) {
  const [run, setRun] = useState<LatestInferenceRunInfo | null>(null);
  const [rankings, setRankings] = useState<InferenceRankingItem[]>([]);
  const [limit, setLimit] = useState(100);
  const [selected, setSelected] = useState<InferenceRankingItem | null>(null);
  const [summary, setSummary] = useState<Record<string, StockSummary>>({});
  const [news, setNews] = useState<NewsIntelItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [newsLoading, setNewsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [subscribed, setSubscribed] = useState<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (market === 'FUTURES') {
        setLoading(false);
        return;
      }
      if (!modelId) {
        setError('该市场暂无可用模型；请先在模型管理注册模型并设为默认');
        setLoading(false);
        return;
      }
      setLoading(true);
      setError(null);
      setRankings([]);
      setSelected(null);
      setNews([]);
      setSummary({});
      try {
        const latest = await modelTrainingService.getLatestInferenceRun(modelId, { perModel: true });
        if (cancelled) return;
        if (!latest?.run_id) {
          setError('该模型暂无可用推理批次；请先在推理中心运行一次推理');
          setRankings([]);
          return;
        }
        setRun(latest);
        const result = await modelTrainingService.getInferenceResult(latest.run_id, 300);
        if (cancelled) return;
        const rows = (result.rankings || [])
          .filter((r) => {
            const m = marketOf(r.code);
            return m === market || m === 'UNKNOWN';
          })
          // 推理表里港股是裸 4 位码（2380），新闻接口要规范后缀（2380.HK）。
          .map((r) => ({ ...r, code: toMarketSymbol(market, r.code) }));
        if (!rows.length) {
          setError(
            `该模型最近批次不是${market === 'HK' ? '港股' : market === 'US' ? '美股' : 'A股'}标的；请选择对应市场的模型`,
          );
          setRankings([]);
          setLoading(false);
          return;
        }
        setRankings(rows);
        setSelected(rows[0]);
        // 摘要（120 只 × 30 日）比榜单慢一个量级；先放榜单出来，摘要异步补齐。
        setLoading(false);
        const codes = rows.slice(0, 120).map((r) => r.code);
        if (codes.length) {
          newsIntelService
            .stocksSummary(codes, 30)
            .then((s) => {
              if (!cancelled) setSummary(s.items || {});
            })
            .catch(() => {
              /* 新闻摘要失败不影响榜单 */
            });
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : '加载失败');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [market, modelId]);

  useEffect(() => {
    let cancelled = false;
    newsIntelService
      .listSubscriptions()
      .then((r) => {
        if (!cancelled) setSubscribed(new Set(r.items.map((x) => x.symbol)));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selected?.code) return;
    let cancelled = false;
    setNewsLoading(true);
    newsIntelService
      .stockNews(selected.code, { days: 30, limit: 14, sort: 'weight' })
      .then((r) => {
        if (!cancelled) setNews(r.items || []);
      })
      .catch(() => {
        if (!cancelled) setNews([]);
      })
      .finally(() => {
        if (!cancelled) setNewsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected?.code]);

  const visible = useMemo(() => rankings.slice(0, limit), [rankings, limit]);
  const total = rankings.length;
  const keyOf = (code: string) => toMarketSymbol(market, code);
  const selectedSummary = selected ? summary[keyOf(selected.code)] : undefined;
  const isSubscribed = selected ? subscribed.has(keyOf(selected.code)) : false;

  const toggleSubscribe = async (row: InferenceRankingItem) => {
    const key = keyOf(row.code);
    try {
      if (subscribed.has(key)) {
        await newsIntelService.removeSubscription(key);
        setSubscribed((prev) => {
          const next = new Set(prev);
          next.delete(key);
          return next;
        });
      } else {
        await newsIntelService.addSubscription(key, row.name, true);
        setSubscribed((prev) => new Set(prev).add(key));
      }
    } catch {
      message.error('订阅操作失败');
    }
  };

  if (market === 'FUTURES') {
    return <div className="ni-empty">期货市场暂不接模型推理；请用「商品雷达」看品种新闻热度。</div>;
  }
  if (loading) {
    return (
      <div className="ni-loading">
        <Spin />
      </div>
    );
  }
  if (error) {
    // 模型不覆盖该市场（港/美常见）时不再留白：直接落「新闻优先」的个股档案板，
    // 模型口径提示挂在顶部，新闻、主题雷达、个股档案照常可用。
    return (
      <StockDossier
        market={market}
        modelId={modelId}
        modelName={modelName}
        notice={`${error}。已切换为「新闻优先」视图：左侧按最近新闻排序，右侧看新闻结构与资金流；模型分数不可用时显示为「—」。`}
        onOpenArticle={onOpenArticle}
        onOpenMethod={onOpenMethod}
      />
    );
  }

  return (
    <div className="ni-board ni-board-daily">
      <aside className="ni-rail">
        <div className="ni-rail-head">
          <div className="ni-kicker">每日推理</div>
          <div className="ni-rail-sub" title={modelName || run?.model_id || modelId || ''}>
            {run?.prediction_trade_date || run?.target_date || '—'} ·{' '}
            {shortModelName(modelName || run?.model_id || '默认模型')}
          </div>
          <Segmented
            size="small"
            value={String(limit)}
            onChange={(v) => setLimit(Number(v))}
            options={[
              { label: '50', value: '50' },
              { label: '100', value: '100' },
              { label: '200', value: '200' },
            ]}
          />
        </div>
        <div className="ni-rank-list">
          {visible.map((row) => {
            const key = keyOf(row.code);
            const s = summary[key];
            return (
              <div
                className={`ni-rank-row ${selected?.code === row.code ? 'active' : ''}`}
                key={`${row.rank}-${row.code}`}
              >
                <button type="button" className="ni-rank-main" onClick={() => setSelected(row)}>
                  <span className="ni-rank-no">{row.rank}</span>
                  <span className="ni-rank-name">
                    {row.name}
                    <em>{row.code}</em>
                  </span>
                  <span className="ni-rank-pos" title="本次推理截面位置">
                    {positionLabel(row.rank, total)}
                  </span>
                  <span className="ni-rank-dir">
                    <DirectionBars
                      positive={s?.n_positive ?? 0}
                      negative={s?.n_negative ?? 0}
                      neutral={s?.n_neutral ?? 0}
                      height={4}
                      showLabels={false}
                      compact
                    />
                    <em className="is-up">{s?.n_positive ?? 0}</em>
                    <em className="is-down">{s?.n_negative ?? 0}</em>
                  </span>
                  {s && s.n_articles > 0 && (
                    <span
                      className={`ni-news-dot ${decisionTone(s.decision)}`}
                      title={`近 30 日 ${s.n_articles} 条；${s.top_label || '信息'}`}
                    >
                      {s.n_articles}
                    </span>
                  )}
                </button>
                <button
                  type="button"
                  className={`ni-icon-btn ni-rank-pin ${subscribed.has(key) ? 'is-on' : ''}`}
                  onClick={() => toggleSubscribe(row)}
                  title={subscribed.has(key) ? '取消订阅' : '订阅置顶'}
                >
                  {subscribed.has(key) ? <PinOff size={13} /> : <Pin size={13} />}
                </button>
              </div>
            );
          })}
          {!visible.length && <Empty description="本次推理没有标的" />}
        </div>
      </aside>

      <section className="ni-main">
        {selected ? (
          <>
            <header className="ni-main-head">
              <div>
                <div className="ni-kicker">近 30 日新闻决策</div>
                <h2>
                  {selected.name}
                  <span>{selected.code}</span>
                </h2>
              </div>
              <div className="ni-main-actions">
                <button type="button" className="ni-ghost-btn" onClick={() => onOpenStock(keyOf(selected.code))}>
                  <ExternalLink size={14} /> 个股档案
                </button>
                <button
                  type="button"
                  className={`ni-ghost-btn ${isSubscribed ? 'is-on' : ''}`}
                  onClick={() => toggleSubscribe(selected)}
                >
                  {isSubscribed ? <PinOff size={14} /> : <Pin size={14} />}
                  {isSubscribed ? '取消置顶' : '订阅置顶'}
                </button>
              </div>
            </header>
            <div className="ni-main-scroll">
              {selectedSummary && selectedSummary.n_articles > 0 ? (
                <div className="ni-callout">
                  <span className={`ni-chip ${decisionTone(selectedSummary.decision)}`}>
                    {selectedSummary.top_label || '信息'}
                  </span>
                  <span>
                    近 30 日 {selectedSummary.n_articles} 条；截面位置 {positionLabel(selected.rank, total)}。
                  </span>
                </div>
              ) : (
                <div className="ni-callout is-muted">
                  近 30 日没有主体新闻；不拿中性稿凑数。
                </div>
              )}
              <NewsTimeline
                items={news}
                loading={newsLoading}
                emptyText="该票近 30 日没有进入决策层的新闻"
                onSelect={(item) => onOpenArticle(item, selected.code)}
              />
            </div>
          </>
        ) : (
          <div className="ni-empty">选择左侧任意一只股票</div>
        )}
      </section>

      <aside className="ni-context">
        {selected && (
          <>
            <div className="ni-kicker">价格</div>
            <PriceSparkline market={market} code={keyOf(selected.code)} days={60} />
            <div className="ni-kicker">30 日新闻结构</div>
            {selectedSummary ? (
              <>
                <DirectionBars
                  positive={selectedSummary.n_positive}
                  negative={selectedSummary.n_negative}
                  neutral={selectedSummary.n_neutral}
                  height={10}
                />
                <dl className="ni-facts">
                  <div>
                    <dt>硬利空</dt>
                    <dd className={selectedSummary.n_hard_negative ? 'is-risk' : ''}>
                      {selectedSummary.n_hard_negative}
                    </dd>
                  </div>
                  <div>
                    <dt>硬利好</dt>
                    <dd className={selectedSummary.n_hard_positive ? 'is-up' : ''}>
                      {selectedSummary.n_hard_positive}
                    </dd>
                  </div>
                  <div>
                    <dt>软利空</dt>
                    <dd className={selectedSummary.n_soft_negative ? 'is-warn' : ''}>
                      {selectedSummary.n_soft_negative}
                    </dd>
                  </div>
                  <div>
                    <dt>投机稿</dt>
                    <dd>{selectedSummary.n_speculative}</dd>
                  </div>
                </dl>
              </>
            ) : (
              <div className="ni-muted">近 30 日无主体新闻</div>
            )}
          </>
        )}
        <ResearchKeyCard onOpen={onOpenMethod} />
        <div className="ni-note is-tight">
          分数位置只描述本次推理的截面排序；新闻只做回避/不追/观察，不产生下单动作。
        </div>
      </aside>
    </div>
  );
}
