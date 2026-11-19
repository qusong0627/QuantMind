/** 板块雷达：板块新闻热度 × 利空发酵；点击下钻成分股新闻。 */

import { useEffect, useMemo, useState } from 'react';
import { Empty, Segmented, Spin } from 'antd';
import type { SectorDetailResponse, SectorRow, NewsIntelItem } from '../services/newsIntelService';
import { newsIntelService } from '../services/newsIntelService';
import { NewsTimeline } from './NewsTimeline';
import { DirectionBars } from './DirectionBars';
import {
  getFlowPanorama,
  type FlowPeriod,
  type FlowSectorSet,
} from '../../market-analysis/services/moneyFlowService';

interface SectorRadarBoardProps {
  onOpenStock: (code: string) => void;
  onOpenArticle: (item: NewsIntelItem, code?: string | null) => void;
}

const TYPE_LABEL: Record<string, string> = {
  concept: '概念板块',
  industry1: '一级行业',
  industry2: '二级行业',
};

export function SectorRadarBoard({ onOpenStock, onOpenArticle }: SectorRadarBoardProps) {
  const [type, setType] = useState<'concept' | 'industry1' | 'industry2'>('concept');
  const [days, setDays] = useState(5);
  const [rows, setRows] = useState<SectorRow[]>([]);
  const [available, setAvailable] = useState(true);
  const [reason, setReason] = useState<string | undefined>();
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<SectorDetailResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [flowMap, setFlowMap] = useState<Record<string, number>>({});
  const [flowUnit, setFlowUnit] = useState('亿');

  useEffect(() => {
    const period: FlowPeriod = days === 1 ? '1d' : days === 5 ? '5d' : '20d';
    let cancelled = false;
    getFlowPanorama({
      period,
      sectorSet: type as FlowSectorSet,
      // 路由上限 le=30（桑基用）；sectors[] 仍是全集，够给整张板块榜配资金流。
      topSectors: 30,
      stocksPerSector: 1,
    })
      .then((r) => {
        if (cancelled) return;
        if (!r.available) {
          setFlowMap({});
          return;
        }
        const scale = r.meta?.unit === 'yuan' ? 1e8 : 1;
        const map: Record<string, number> = {};
        for (const s of r.sectors || []) {
          if (typeof s.net_main === 'number') map[s.name] = s.net_main / scale;
        }
        setFlowMap(map);
        setFlowUnit(r.meta?.unit === 'yuan' ? '亿' : r.meta?.unit || '亿');
      })
      .catch(() => {
        if (!cancelled) setFlowMap({});
      });
    return () => {
      cancelled = true;
    };
  }, [type, days]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    newsIntelService
      .sectors(type, days, 60)
      .then((r) => {
        if (cancelled) return;
        setAvailable(r.available);
        setReason(r.reason);
        setRows(r.items || []);
        setSelected((prev) => (prev && r.items.some((x) => x.code === prev) ? prev : r.items[0]?.code || null));
      })
      .catch(() => {
        if (!cancelled) {
          setRows([]);
          setAvailable(false);
          setReason('资讯数据暂不可用');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [type, days]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    newsIntelService
      .sectorDetail(selected, days, 40)
      .then((r) => {
        if (!cancelled) setDetail(r);
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected, days]);

  const stats = useMemo(() => {
    const totalArticles = rows.reduce((acc, r) => acc + r.n_articles, 0);
    const totalRisk = rows.reduce((acc, r) => acc + r.n_risk, 0);
    const hottest = [...rows].sort((a, b) => b.n_articles - a.n_articles)[0];
    const riskiest = [...rows].sort((a, b) => b.risk_ratio - a.risk_ratio)[0];
    return { totalArticles, totalRisk, hottest, riskiest };
  }, [rows]);

  if (loading) {
    return (
      <div className="ni-loading">
        <Spin />
      </div>
    );
  }

  if (!available) {
    return <div className="ni-empty">{reason || '板块成员表不可用'}</div>;
  }

  return (
    <div className="ni-board ni-board-sector">
      <aside className="ni-rail ni-rail-wide">
        <div className="ni-rail-head">
          <div className="ni-kicker">板块雷达</div>
          <Segmented
            size="small"
            value={type}
            onChange={(v) => setType(v as typeof type)}
            options={Object.entries(TYPE_LABEL).map(([value, label]) => ({ value, label }))}
          />
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
        <div className="ni-sector-list">
          {rows.map((row, idx) => {
            const total = Math.max(1, row.n_articles);
            const pos = row.n_positive / total;
            const neg = row.n_negative / total;
            const tone = pos > neg * 1.3 ? 'up' : neg > pos * 1.3 ? 'down' : 'flat';
            const toneLabel = tone === 'up' ? '偏多' : tone === 'down' ? '偏空' : '中性';
            const share = tone === 'up' ? pos : tone === 'down' ? neg : Math.max(0, 1 - pos - neg);
            return (
              <button
                type="button"
                key={row.code}
                className={`ni-sector-row tone-${tone} ${selected === row.code ? 'active' : ''}`}
                onClick={() => setSelected(row.code)}
                title={`${row.name}：${row.n_articles} 条；风险 ${row.n_risk}（${(row.risk_ratio * 100).toFixed(1)}%）；${toneLabel} ${Math.round(share * 100)}%`}
              >
                <span className="ni-sector-rank">{String(idx + 1).padStart(2, '0')}</span>
                <span className="ni-sector-name">{row.name}</span>
                <span className="ni-sector-n">{row.n_articles}</span>
                <span className={`ni-sector-risk ${row.risk_ratio >= 0.1 ? 'is-hot' : ''}`}>
                  风险 {row.n_risk}
                </span>
                <span className="ni-sector-sub">
                  <DirectionBars
                    positive={row.n_positive}
                    negative={row.n_negative}
                    neutral={Math.max(0, row.n_articles - row.n_positive - row.n_negative)}
                    height={4}
                    showLabels={false}
                    compact
                  />
                  <em className={`ni-sector-tone is-${tone}`}>
                    {toneLabel} {Math.round(share * 100)}%
                  </em>
                  {row.spec_ratio >= 0.2 && (
                    <em className="ni-sector-spec">投机 {Math.round(row.spec_ratio * 100)}%</em>
                  )}
                  {flowMap[row.name] != null && (
                    <em className={`ni-sector-flow ${flowMap[row.name] >= 0 ? 'is-up' : 'is-down'}`}>
                      主力 {flowMap[row.name] >= 0 ? '+' : ''}
                      {flowMap[row.name].toFixed(2)}
                      {flowUnit}
                    </em>
                  )}
                </span>
              </button>
            );
          })}
          {!rows.length && <Empty description="该窗口没有板块新闻" />}
        </div>
      </aside>

      <section className="ni-main">
        {detail ? (
          <>
            <header className="ni-main-head">
              <div>
                <div className="ni-kicker">{TYPE_LABEL[detail.sector.type] || detail.sector.type}</div>
                <h2>{detail.sector.name}</h2>
              </div>
              <div className="ni-main-meta">
                <span>{detail.n_articles} 条新闻</span>
                <span>·</span>
                <span>{days} 日窗口</span>
              </div>
            </header>
            <div className="ni-main-scroll">
              <div className="ni-callout is-research">
                <div className="ni-kicker">本页读法</div>
                <p>{detail.read_rule}</p>
              </div>
              <div className="ni-topstocks">
                <div className="ni-kicker">板块新闻命中股</div>
                <div className="ni-chip-row">
                  {detail.top_stocks.map((s) => (
                    <button
                      type="button"
                      key={s.code}
                      className="ni-stock-chip"
                      onClick={() => onOpenStock(s.code)}
                      title={`${s.code} · ${s.n} 条`}
                    >
                      {s.name || s.code}
                      <em>{s.n}</em>
                    </button>
                  ))}
                  {!detail.top_stocks.length && <span className="ni-muted">暂无</span>}
                </div>
              </div>
              <NewsTimeline
                items={detail.items}
                loading={detailLoading}
                emptyText="该板块在这个窗口没有匹配到新闻"
                onSelect={onOpenArticle}
              />
            </div>
          </>
        ) : (
          <div className="ni-empty">{detailLoading ? '加载中…' : '选择左侧任意板块'}</div>
        )}
      </section>

      <aside className="ni-context">
        <div className="ni-kicker">窗口读数</div>
        <dl className="ni-facts">
          <div>
            <dt>板块数</dt>
            <dd>{rows.length}</dd>
          </div>
          <div>
            <dt>新闻总量</dt>
            <dd>{stats.totalArticles}</dd>
          </div>
          <div>
            <dt>风险稿</dt>
            <dd>{stats.totalRisk}</dd>
          </div>
          {detail && (
            <div>
              <dt>偏多 / 偏空</dt>
              <dd>
                <span className="is-up">{detail.n_positive ?? 0}</span>
                {' / '}
                <span className="is-down">{detail.n_negative ?? 0}</span>
              </dd>
            </div>
          )}
          {detail && flowMap[detail.sector.name] != null && (
            <div>
              <dt>主力净流入</dt>
              <dd className={flowMap[detail.sector.name] >= 0 ? 'is-up' : 'is-down'}>
                {flowMap[detail.sector.name] >= 0 ? '+' : ''}
                {flowMap[detail.sector.name].toFixed(2)}
                {flowUnit}
              </dd>
            </div>
          )}
          <div>
            <dt>最热</dt>
            <dd>{stats.hottest ? `${stats.hottest.name}（${stats.hottest.n_articles}）` : '—'}</dd>
          </div>
          <div>
            <dt>风险密度最高</dt>
            <dd>{stats.riskiest ? `${stats.riskiest.name}（${Math.round(stats.riskiest.risk_ratio * 100)}%）` : '—'}</dd>
          </div>
        </dl>
        <div className="ni-note">
          <div className="ni-kicker">边界</div>
          <p>
            板块成员是当前快照，不是逐日 PIT；这里的热度是新闻口径，不是涨跌预测。
            板块利好不追，板块利空（尤其概念）才看发布后的延续。
          </p>
        </div>
      </aside>
    </div>
  );
}
