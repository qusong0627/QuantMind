/** KPI 带：窗口读数，机构资讯台的第一眼。 */

import { Spin } from 'antd';
import type { MarketKey, OverviewData } from '../services/newsIntelService';

interface KpiStripProps {
  market: MarketKey;
  data: OverviewData | null;
  loading?: boolean;
}

const MARKET_LABEL: Record<MarketKey, string> = {
  CN: 'A股',
  HK: '港股',
  US: '美股',
  FUTURES: '期货',
};

export function KpiStrip({ market, data, loading }: KpiStripProps) {
  const isFutures = market === 'FUTURES';
  const topics = (data?.topics || []).slice(0, 3).map((x) => x.name).join(' / ') || '—';
  const tiles = [
    { label: isFutures ? '商品新闻' : '相关新闻', value: data?.n_articles ?? 0, tone: 'ink' },
    { label: isFutures ? '品种风险' : '硬利空', value: data?.n_risk ?? 0, tone: 'risk' },
    { label: '偏空', value: data?.n_negative ?? 0, tone: 'down' },
    { label: '偏多', value: data?.n_positive ?? 0, tone: 'up' },
    { label: '投机稿', value: data?.n_speculative ?? 0, tone: 'muted' },
  ];
  return (
    <section className={`ni-kpi ni-market-${market.toLowerCase()}`} aria-label="资讯读数">
      <div className="ni-kpi-market">
        <span>{MARKET_LABEL[market]}</span>
        <em>{data?.days ?? 1} 日窗口</em>
      </div>
      {tiles.map((t) => (
        <div className={`ni-kpi-tile tone-${t.tone}`} key={t.label}>
          <span className="ni-kpi-label">{t.label}</span>
          <strong className="ni-kpi-value">{loading ? '—' : t.value.toLocaleString()}</strong>
        </div>
      ))}
      <div className="ni-kpi-topic">
        <span className="ni-kpi-label">热门{isFutures ? '品种' : '主题'}</span>
        <strong>{loading ? '—' : topics}</strong>
      </div>
      {loading && <Spin size="small" className="ni-kpi-spin" />}
    </section>
  );
}
