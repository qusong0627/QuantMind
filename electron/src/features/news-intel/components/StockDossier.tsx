/** 个股档案：时间优先的全部新闻 + 30 日结构 + 价格 sparkline + 右侧阅读。 */

import { useEffect, useMemo, useState } from 'react';
import { Empty, Input, Segmented, Spin, message } from 'antd';
import { Pin, PinOff, Search } from 'lucide-react';
import {
  newsIntelService,
  type MarketKey,
  type NewsIntelItem,
  type StockListItem,
  type StockProfile,
  type StockScoreInfo,
} from '../services/newsIntelService';
import { getFlowStock } from '../../market-analysis/services/moneyFlowService';
import {
  modelTrainingService,
  type InferenceRankingItem,
} from '../../../services/modelTrainingService';
import { NewsTimeline } from './NewsTimeline';
import { DirectionBars } from './DirectionBars';
import { PriceSparkline } from './PriceSparkline';
import { ResearchKeyCard } from './ResearchKeyCard';
import { marketOf, toMarketSymbol } from '../marketSymbol';

interface StockDossierProps {
  initialCode?: string | null;
  market: MarketKey;
  modelId: string | null;
  modelName: string;
  notice?: string;
  onOpenArticle: (item: NewsIntelItem, code?: string | null) => void;
  onOpenMethod?: () => void;
}

type Bucket = 'all' | 'important' | 'risk' | 'spec';

const MARKET_LABEL: Record<string, string> = { CN: 'A股', HK: '港股', US: '美股', FUTURES: '期货' };

function shortTime(value: string | null): string {
  if (!value) return '—';
  return value.replace('T', ' ').slice(5, 16);
}

export function StockDossier({
  initialCode,
  market,
  modelId,
  modelName,
  notice,
  onOpenArticle,
  onOpenMethod,
}: StockDossierProps) {
  const [query, setQuery] = useState(initialCode || '');
  const [code, setCode] = useState(initialCode || '');
  const [days, setDays] = useState(30);
  const [bucket, setBucket] = useState<Bucket>('all');
  const [sort, setSort] = useState<'weight' | 'time'>('time');
  const [profile, setProfile] = useState<StockProfile | null>(null);
  const [news, setNews] = useState<NewsIntelItem[]>([]);
  const [stockList, setStockList] = useState<StockListItem[]>([]);
  const [searchHits, setSearchHits] = useState<StockListItem[] | null>(null);
  const [searchKeyword, setSearchKeyword] = useState('');
  const [searching, setSearching] = useState(false);
  const [rankMode, setRankMode] = useState<'news' | 'model'>('news');
  const [modelRows, setModelRows] = useState<InferenceRankingItem[]>([]);
  const [modelRankLoading, setModelRankLoading] = useState(false);
  const [listLoading, setListLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [newsLoading, setNewsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [subscribed, setSubscribed] = useState(false);
  const [modelScore, setModelScore] = useState<StockScoreInfo | null>(null);
  const [stockFlow, setStockFlow] = useState<{
    w1: number | null;
    w5: number | null;
    unit: string;
  } | null>(null);
  const [direction, setDirection] = useState('');
  const [decision, setDecision] = useState('');
  const [riskType, setRiskType] = useState('');
  const [subjectOnly, setSubjectOnly] = useState(false);
  const [specOnly, setSpecOnly] = useState(false);

  useEffect(() => {
    setQuery(initialCode || '');
    setCode(initialCode || '');
    setProfile(null);
    setNews([]);
    setStockList([]);
    setError(null);
    setSubscribed(false);
    setSearchHits(null);
    setSearchKeyword('');
    setRankMode('news');
    setDirection('');
    setDecision('');
    setRiskType('');
    setSubjectOnly(false);
    setSpecOnly(false);
  }, [initialCode, market]);

  useEffect(() => {
    let cancelled = false;
    setListLoading(true);
    newsIntelService
      .listStocks(market, 7, 6000)
      .then((r) => {
        if (!cancelled) setStockList(r.items || []);
      })
      .catch(() => {
        if (!cancelled) setStockList([]);
      })
      .finally(() => {
        if (!cancelled) setListLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [market]);

  useEffect(() => {
    if (!code && stockList.length) {
      setCode(stockList[0].code);
      setQuery(stockList[0].code);
    }
  }, [code, stockList]);

  // 模型排名视图：切模型即时重载该模型最近一批推理的截面排序。
  useEffect(() => {
    if (rankMode !== 'model' || !modelId) {
      setModelRows([]);
      return;
    }
    let cancelled = false;
    setModelRankLoading(true);
    (async () => {
      try {
        const latest = await modelTrainingService.getLatestInferenceRun(modelId, { perModel: true });
        if (cancelled) return;
        if (!latest?.run_id) {
          setModelRows([]);
          return;
        }
        const result = await modelTrainingService.getInferenceResult(latest.run_id, 500);
        if (cancelled) return;
        setModelRows(
          (result.rankings || []).filter((r) => {
            const m = marketOf(r.code);
            return m === market || m === 'UNKNOWN';
          }),
        );
      } catch {
        if (!cancelled) setModelRows([]);
      } finally {
        if (!cancelled) setModelRankLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [rankMode, modelId, market]);

  useEffect(() => {
    if (!code) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setNewsLoading(true);
    Promise.all([
      newsIntelService.stockProfile(code, Math.max(days, 30)),
      newsIntelService.stockNews(code, {
        days,
        limit: 200,
        sort,
        bucket,
        direction,
        decision,
        risk_type: riskType,
        subject_only: subjectOnly,
        spec_only: specOnly,
      }),
    ])
      .then(([p, n]) => {
        if (cancelled) return;
        setProfile(p);
        setNews(n.items || []);
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : '加载失败');
          setProfile(null);
          setNews([]);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
          setNewsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [code, days, bucket, sort, direction, decision, riskType, subjectOnly, specOnly]);

  useEffect(() => {
    if (!code) return;
    const canonical = profile?.code || toMarketSymbol(market, code);
    let cancelled = false;
    newsIntelService
      .listSubscriptions()
      .then((r) => {
        if (!cancelled) setSubscribed(r.items.some((x) => x.symbol === canonical));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [code, market, profile?.code]);

  useEffect(() => {
    if (!profile?.code || !modelId) {
      setModelScore(null);
      return;
    }
    let cancelled = false;
    newsIntelService
      .stockScore(profile.code, modelId)
      .then((r) => {
        if (!cancelled) setModelScore(r);
      })
      .catch(() => {
        if (!cancelled) setModelScore(null);
      });
    return () => {
      cancelled = true;
    };
  }, [profile?.code, modelId]);

  useEffect(() => {
    const m = (profile?.market || market) as MarketKey;
    if (!profile?.code || m !== 'CN') {
      setStockFlow(null);
      return;
    }
    let cancelled = false;
    getFlowStock(profile.code, { days: 5 })
      .then((r) => {
        if (cancelled) return;
        if (!r.available) {
          setStockFlow(null);
          return;
        }
        // 接口单位是元（meta.unit=yuan），界面统一按亿元显示。
        const scale = r.meta?.unit === 'yuan' ? 1e8 : 1;
        const w1 = r.windows?.['1d']?.net_main;
        const w5 = r.windows?.['5d']?.net_main;
        setStockFlow({
          w1: typeof w1 === 'number' ? w1 / scale : null,
          w5: typeof w5 === 'number' ? w5 / scale : null,
          unit: r.meta?.unit === 'yuan' ? '亿' : r.meta?.unit || '亿',
        });
      })
      .catch(() => {
        if (!cancelled) setStockFlow(null);
      });
    return () => {
      cancelled = true;
    };
  }, [profile?.code, profile?.market, market]);

  const submit = async () => {
    const next = query.trim();
    if (!next) return;
    // 代码形态（含 A 股 prefix/裸码、港股 .HK）直接开档案；其余当关键词检索。
    const codeLike = /^(\d{4,6})(\.(SH|SZ|BJ|HK))?$/i.test(next) || /^(SH|SZ|BJ)\d{6}$/i.test(next);
    if (codeLike) {
      setSearchHits(null);
      setSearchKeyword('');
      setCode(next);
      return;
    }
    setSearching(true);
    try {
      const r = await newsIntelService.searchStocks(next, market, 30, 30);
      const hits = r.items || [];
      setSearchKeyword(next);
      setSearchHits(hits);
      // 唯一命中，或关键词唯一命中某只票的名称/别名（如「茅台」→ 贵州茅台）时直接开档案。
      const named = hits.filter((h) => h.is_name);
      const target = hits.length === 1 ? hits[0] : named.length === 1 ? named[0] : null;
      if (!hits.length) {
        message.info(`没有匹配「${next}」的股票或新闻`);
      } else if (target) {
        setCode(target.code);
        setQuery(target.code);
        setSearchHits(null);
      }
    } catch {
      message.error('关键词检索失败');
    } finally {
      setSearching(false);
    }
  };

  const toggleSubscribe = async () => {
    if (!code) return;
    const canonical = profile?.code || toMarketSymbol(market, code);
    try {
      if (subscribed) {
        await newsIntelService.removeSubscription(canonical);
        setSubscribed(false);
      } else {
        await newsIntelService.addSubscription(canonical, profile?.name, true);
        setSubscribed(true);
      }
    } catch {
      message.error('订阅操作失败');
    }
  };

  const heatMax = useMemo(
    () => Math.max(1, ...(profile?.heat || []).map((x) => x.n)),
    [profile],
  );
  const events = profile?.events || {};
  const marketKey = (profile?.market || market) as MarketKey;
  const positionText = (p?: string | null) =>
    p === 'top' ? '靠前' : p === 'bottom' ? '靠后' : p === 'mid' ? '居中' : '—';
  const DIR_KEY: Record<string, string> = { positive: '利好', negative: '利空', neutral: '中性' };
  const activeDirKey =
    direction === '利好' ? 'positive' : direction === '利空' ? 'negative' : direction === '中性' ? 'neutral' : null;
  const filterCount =
    [direction, decision, riskType].filter(Boolean).length +
    (subjectOnly ? 1 : 0) +
    (specOnly ? 1 : 0);
  const toggleDirection = (key: string) => {
    const value = DIR_KEY[key];
    setDirection((prev) => (prev === value ? '' : value));
  };
  const toggleDecision = (key: string) => {
    setDecision((prev) => (prev === key ? '' : key));
  };
  const toggleRisk = (name: string) => {
    setRiskType((prev) => (prev === name ? '' : name));
  };
  const clearFilters = () => {
    setDirection('');
    setDecision('');
    setRiskType('');
    setSubjectOnly(false);
    setSpecOnly(false);
  };

  const railRows = searchHits
    ? searchHits.map((s) => ({
        code: s.code,
        name: s.name,
        meta1: `${s.n_articles} 条`,
        meta2: shortTime(s.latest_at),
      }))
    : rankMode === 'model'
      ? modelRows.map((r) => ({
          code: toMarketSymbol(market, r.code),
          name: r.name || r.code,
          meta1: `#${r.rank}`,
          meta2: r.score != null ? r.score.toFixed(3) : '—',
        }))
      : stockList.map((s) => ({
          code: s.code,
          name: s.name,
          meta1: `${s.n_articles} 条`,
          meta2: shortTime(s.latest_at),
        }));
  const railLoading =
    listLoading || searching || (rankMode === 'model' && modelRankLoading);

  return (
    <div className="ni-board ni-board-stock">
      <aside className="ni-rail ni-stock-rail">
        <div className="ni-rail-head">
          <div className="ni-kicker">
            {searchHits ? `关键词「${searchKeyword}」` : '股票列表'}
          </div>
          <div className="ni-rail-sub" title="有新闻的在前，按最近一条匹配新闻时间倒序">
            {searchHits ? (
              <>
                命中 {searchHits.length} 只 ·{' '}
                <button
                  type="button"
                  className="ni-link-btn"
                  onClick={() => {
                    setSearchHits(null);
                    setSearchKeyword('');
                  }}
                >
                  返回列表
                </button>
              </>
            ) : (
              `${MARKET_LABEL[market] || market} · ${rankMode === 'model' ? '模型截面排名' : '新闻优先'}`
            )}
          </div>
          {!searchHits && modelId && (
            <Segmented
              size="small"
              block
              value={rankMode}
              onChange={(v) => setRankMode(v as 'news' | 'model')}
              options={[
                { label: '新闻优先', value: 'news' },
                { label: '模型排名', value: 'model' },
              ]}
            />
          )}
        </div>
        <div className="ni-stock-list">
          {railLoading ? (
            <div className="ni-loading">
              <Spin size="small" />
            </div>
          ) : railRows.length ? (
            railRows.map((s) => (
              <button
                type="button"
                key={s.code}
                className={`ni-stock-row ${code === s.code ? 'active' : ''}`}
                onClick={() => {
                  setCode(s.code);
                  setQuery(s.code);
                  setSearchHits(null);
                }}
              >
                <span className="ni-stock-row-name">
                  {s.name || s.code}
                  <em>{s.code}</em>
                </span>
                <span className="ni-stock-row-meta">
                  <span>{s.meta1}</span>
                  <span>{s.meta2}</span>
                </span>
              </button>
            ))
          ) : (
            <div className="ni-empty">
              {searchHits
                ? '没有匹配的股票；换个关键词试试'
                : rankMode === 'model'
                  ? '该模型最近批次未覆盖当前市场'
                  : '该市场近 7 日没有匹配到股票新闻'}
            </div>
          )}
        </div>
      </aside>

      <section className="ni-main">
        {notice && <div className="ni-fallback-notice">{notice}</div>}
        <header className="ni-main-head ni-stock-head">
          <div className="ni-stock-search">
            <Search size={15} />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onPressEnter={submit}
              placeholder="代码 / 名称 / 新闻关键词，如 600519.SH、0700.HK、AAPL、英伟达"
              variant="borderless"
            />
            <button type="button" className="ni-ghost-btn" onClick={submit}>
              查看
            </button>
          </div>
          {profile && (
            <div className="ni-stock-ident">
              <span className={`ni-market-badge ni-market-${marketKey.toLowerCase()}`}>
                {MARKET_LABEL[marketKey] || marketKey}
              </span>
              <h2>{profile.name || profile.code}</h2>
              <span>{profile.code}</span>
              {modelScore?.found && (
                <span
                  className="ni-score-chip"
                  title={`${modelName} · 批次 ${modelScore.trade_date || ''}`}
                >
                  模型 {modelScore.score != null ? modelScore.score.toFixed(3) : '—'} ·{' '}
                  {positionText(modelScore.position)}
                </span>
              )}
              {modelScore && !modelScore.found && (
                <span className="ni-score-chip is-muted" title={modelScore.reason || ''}>
                  模型未覆盖
                </span>
              )}
              {stockFlow?.w5 != null && (
                <span
                  className={`ni-flow-chip ${stockFlow.w5 >= 0 ? 'is-up' : 'is-down'}`}
                  title={`主力资金 5 日 / 1 日（${stockFlow.unit}）`}
                >
                  主力5日 {stockFlow.w5 >= 0 ? '+' : ''}
                  {stockFlow.w5.toFixed(2)}
                  {stockFlow.unit}
                  {stockFlow.w1 != null && (
                    <em>
                      {' '}
                      / 1日 {stockFlow.w1 >= 0 ? '+' : ''}
                      {stockFlow.w1.toFixed(2)}
                      {stockFlow.unit}
                    </em>
                  )}
                </span>
              )}
              <PriceSparkline market={marketKey} code={profile.code} days={40} height={30} />
              <button
                type="button"
                className={`ni-ghost-btn ${subscribed ? 'is-on' : ''}`}
                onClick={toggleSubscribe}
              >
                {subscribed ? <PinOff size={14} /> : <Pin size={14} />}
                {subscribed ? '取消置顶' : '订阅置顶'}
              </button>
            </div>
          )}
        </header>

        <div className="ni-filter-row">
          <Segmented
            size="small"
            value={String(days)}
            onChange={(v) => setDays(Number(v))}
            options={[
              { label: '30 日', value: '30' },
              { label: '90 日', value: '90' },
              { label: '全部', value: '730' },
            ]}
          />
          <Segmented
            size="small"
            value={bucket}
            onChange={(v) => setBucket(v as Bucket)}
            options={[
              { label: '全部', value: 'all' },
              { label: '重要', value: 'important' },
              { label: '风险', value: 'risk' },
              { label: '投机', value: 'spec' },
            ]}
          />
          <Segmented
            size="small"
            value={sort}
            onChange={(v) => setSort(v as 'weight' | 'time')}
            options={[
              { label: '按时间', value: 'time' },
              { label: '按权重', value: 'weight' },
            ]}
          />
          <span className="ni-filter-hint">
            当前模型：{modelName || '默认模型'} · 新闻只做风险/不追
          </span>
        </div>

        <div className="ni-main-scroll">
          {loading ? (
            <div className="ni-loading">
              <Spin />
            </div>
          ) : error ? (
            <div className="ni-empty">{error}</div>
          ) : !code ? (
            <div className="ni-empty">输入代码或名称查看个股新闻档案</div>
          ) : (
            <>
              {profile && (
                <div className="ni-stock-stats">
                  <div className="ni-stat-tile">
                    <span>30 日新闻</span>
                    <strong>{profile.n_articles}</strong>
                  </div>
                  <div className="ni-stat-tile is-up">
                    <span>利好</span>
                    <strong>{profile.directions?.['利好'] || 0}</strong>
                  </div>
                  <div className="ni-stat-tile is-down">
                    <span>利空</span>
                    <strong>{profile.directions?.['利空'] || 0}</strong>
                  </div>
                  <div className="ni-stat-tile is-risk">
                    <span>硬利空</span>
                    <strong>{events.hard_negative || 0}</strong>
                  </div>
                  <div className="ni-stat-tile is-risk">
                    <span>软利空</span>
                    <strong>{events.soft_negative || 0}</strong>
                  </div>
                  <div className="ni-stat-tile is-up">
                    <span>硬利好</span>
                    <strong>{events.hard_positive || 0}</strong>
                  </div>
                  <div className="ni-stat-tile is-up">
                    <span>泛利好</span>
                    <strong>{events.generic_positive || 0}</strong>
                  </div>
                </div>
              )}
              {filterCount > 0 && (
                <div className="ni-active-filters">
                  <span className="ni-kicker">筛选</span>
                  {direction && (
                    <button type="button" onClick={() => setDirection('')}>
                      方向 {direction} ×
                    </button>
                  )}
                  {decision && (
                    <button type="button" onClick={() => setDecision('')}>
                      等级 {decision} ×
                    </button>
                  )}
                  {riskType && (
                    <button type="button" onClick={() => setRiskType('')}>
                      风险 {riskType} ×
                    </button>
                  )}
                  {subjectOnly && (
                    <button type="button" onClick={() => setSubjectOnly(false)}>
                      仅主体 ×
                    </button>
                  )}
                  {specOnly && (
                    <button type="button" onClick={() => setSpecOnly(false)}>
                      仅投机 ×
                    </button>
                  )}
                  <button type="button" className="ni-clear-filter" onClick={clearFilters}>
                    清除全部
                  </button>
                </div>
              )}
              {profile?.read_rule && (
                <div className="ni-callout is-research">
                  <div className="ni-kicker">本页读法</div>
                  <p>{profile.read_rule}</p>
                  {profile.ambiguous && (
                    <p className="ni-method-note">
                      高歧义名称：正文提及只当「提及」，不做个股事件归属；风险/决策计数已按此口径收敛。
                    </p>
                  )}
                  {marketKey !== 'CN' && (
                    <p className="ni-method-note">
                      港股/美股只做事件分类与热度；A 股的 pre5/决策卡没有在这两个市场做过样本外验证，不自动外推。
                    </p>
                  )}
                </div>
              )}
              <NewsTimeline
                items={news}
                loading={newsLoading}
                emptyText={
                  filterCount > 0 ? '当前筛选下没有资讯；清除筛选看全部' : '这个时间窗内没有匹配的资讯'
                }
                onSelect={(item) => onOpenArticle(item, profile?.code || code)}
              />
            </>
          )}
        </div>
      </section>

      <aside className="ni-context ni-context-stock">
        {profile ? (
          <>
            <div className="ni-kicker">方向结构</div>
            <DirectionBars
              positive={profile.directions?.['利好'] || 0}
              negative={profile.directions?.['利空'] || 0}
              neutral={profile.directions?.['中性'] || 0}
              height={10}
              active={activeDirKey}
              onSelect={toggleDirection}
            />
            <div className="ni-kicker">决策等级</div>
            <div className="ni-decision-bars">
              {(['P0', 'P1', 'P2', 'WATCH', 'INFO'] as const).map((key) => {
                const n = profile.decisions?.[key] || 0;
                const max = Math.max(1, ...Object.values(profile.decisions || {}));
                return (
                  <button
                    type="button"
                    className={`ni-decision-bar ${decision === key ? 'is-active' : ''}`}
                    key={key}
                    onClick={() => toggleDecision(key)}
                  >
                    <span className={`ni-chip is-${key.toLowerCase()}`}>{key}</span>
                    <i style={{ width: `${Math.round((n / max) * 100)}%` }} />
                    <em>{n}</em>
                  </button>
                );
              })}
            </div>

            <div className="ni-kicker">风险类型</div>
            <ul className="ni-risk-list">
              {Object.entries(profile.risks || {})
                .sort((a, b) => b[1] - a[1])
                .slice(0, 6)
                .map(([name, n]) => (
                  <li key={name}>
                    <button
                      type="button"
                      className={`ni-risk-item ${riskType === name ? 'is-active' : ''}`}
                      onClick={() => toggleRisk(name)}
                    >
                      <span>{name}</span>
                      <em>{n}</em>
                    </button>
                  </li>
                ))}
              {!Object.keys(profile.risks || {}).length && <li className="ni-muted">窗口内无风险稿</li>}
            </ul>

            <div className="ni-kicker">新闻热度</div>
            <div className="ni-heat" aria-label="近 30 日新闻热度">
              {profile.heat.slice(-30).map((x) => (
                <span
                  key={x.date}
                  title={`${x.date}: ${x.n} 条`}
                  style={{ height: `${Math.max(6, Math.round((x.n / heatMax) * 40))}px` }}
                />
              ))}
              {!profile.heat.length && <span className="ni-muted">无</span>}
            </div>

            <div className="ni-facts">
              <div>
                <dt>模型分数</dt>
                <dd>
                  {modelScore?.found
                    ? `${modelScore.score != null ? modelScore.score.toFixed(3) : '—'} · ${positionText(modelScore.position)}`
                    : '—'}
                </dd>
              </div>
              <div>
                <dt>主力 5 日</dt>
                <dd className={stockFlow?.w5 != null ? (stockFlow.w5 >= 0 ? 'is-up' : 'is-down') : ''}>
                  {stockFlow?.w5 != null
                    ? `${stockFlow.w5 >= 0 ? '+' : ''}${stockFlow.w5.toFixed(2)}${stockFlow.unit}`
                    : '—'}
                </dd>
              </div>
              <div>
                <dt>主体（L1/L2）</dt>
                <dd>
                  <button
                    type="button"
                    className={`ni-fact-btn ${subjectOnly ? 'is-active' : ''}`}
                    onClick={() => setSubjectOnly((v) => !v)}
                  >
                    {profile.n_subject}
                  </button>
                </dd>
              </div>
              <div>
                <dt>来源</dt>
                <dd>{profile.n_sources}</dd>
              </div>
              <div>
                <dt>投机稿</dt>
                <dd>
                  <button
                    type="button"
                    className={`ni-fact-btn ${specOnly ? 'is-active' : ''}`}
                    onClick={() => setSpecOnly((v) => !v)}
                  >
                    {profile.n_speculative}
                  </button>
                </dd>
              </div>
            </div>
          </>
        ) : (
          <Empty description="暂无档案" />
        )}
        <ResearchKeyCard onOpen={onOpenMethod} />
      </aside>
    </div>
  );
}
