import { useMemo } from 'react';
import { NewsArticle, fetchLiveNews } from '../api/client';
import { usePolling } from '../hooks/usePolling';
import './NewsStream.css';

interface Props {
  tickers: string[]; // 持仓/关注代码（A股 600519.SH 格式），空 = 全部新闻
  hours?: number;
  limit?: number;
  keyword?: string; // 港股无 HK 标签库，按关键词全文搜（腾讯/恒生/港股）；设了则跳过 ticker 匹配过滤
}

const SENTIMENT_LABEL: Record<string, string> = {
  bullish: '利好',
  bearish: '利空',
  neutral: '中性',
};

const SENTIMENT_CLASS: Record<string, string> = {
  bullish: 'bullish',
  bearish: 'bearish',
  neutral: 'neutral',
};

/** ISO 时间 → 北京时间（published_at 为 UTC）。
 *  用 UTC+8 平移后读 UTC 分量——getHours()+8 会把本机时区(如 JST)算进去，错一整天。 */
const bjTime = (iso: string | null | undefined): string => {
  if (!iso) return '';
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return '';
  const bj = new Date(t.getTime() + 8 * 3600 * 1000); // UTC → 北京
  const hh = String(bj.getUTCHours()).padStart(2, '0');
  const mm = String(bj.getUTCMinutes()).padStart(2, '0');
  const todayBj = new Date(Date.now() + 8 * 3600 * 1000);
  const sameDay =
    bj.getUTCFullYear() === todayBj.getUTCFullYear() &&
    bj.getUTCMonth() === todayBj.getUTCMonth() &&
    bj.getUTCDate() === todayBj.getUTCDate();
  return sameDay ? `${hh}:${mm}` : `${String(bj.getUTCMonth() + 1).padStart(2, '0')}-${String(bj.getUTCDate()).padStart(2, '0')} ${hh}:${mm}`;
};

/** 匹配到当前关注列表的 ticker（enrichment.tickers 与持仓代码对拍）。 */
const matchedTickers = (a: NewsArticle, watch: Set<string>): string[] =>
  (a.enrichment?.tickers ?? []).filter((t) => watch.has(t));

/** NEWS —— 盘中实时新闻流（Huntly/RSS 聚合 + LLM enrichment，只显示与持仓/关注相关的）。 */
export default function NewsStream({ tickers, hours = 12, limit = 30, keyword = '' }: Props) {
  const watch = useMemo(() => new Set(tickers), [tickers]);
  const key = tickers.join(',');
  const news = usePolling(
    () => fetchLiveNews(key ? tickers : [], hours, limit, keyword).catch(() => ({ articles: [] })),
    [key, hours, limit, keyword],
    60000,
  );

  const items: NewsArticle[] = useMemo(() => {
    const list = (news.data?.articles ?? []).filter((a) => a.title);
    // 关键词模式（港股）：服务端已按关键词全文搜，跳过 ticker 匹配过滤
    // （HK 文章 enrichment 标的是 A 股代码或为空，按 watch 过滤会全丢）
    if (keyword) return list.slice(0, limit);
    return list.filter((a) => matchedTickers(a, watch).length > 0).slice(0, limit);
  }, [news.data, watch, limit, keyword]);

  if (news.error && !news.data) {
    return <div className="empty-state">新闻源不可用：{news.error}</div>;
  }
  if (news.data && items.length === 0 && (tickers.length > 0 || keyword)) {
    return (
      <div className="empty-state">
        近 {hours} 小时暂无{keyword ? `「${keyword}」` : '持仓'}相关新闻
        <span className="news-empty-sub">（RSS 聚合 + 情绪标注，每 60s 刷新）</span>
      </div>
    );
  }
  if (!items.length) return <div className="empty-state">加载新闻…</div>;

  return (
    <div className="news-stream">
      {items.map((a) => {
        const sent = a.enrichment?.sentiment_label ?? null;
        const matched = matchedTickers(a, watch);
        const title = a.title ?? '';
        return (
          <article className="news-card" key={a.id}>
            <div className="news-card-head">
              {sent && (
                <span className={`news-sent ${SENTIMENT_CLASS[sent] ?? ''}`}>
                  {SENTIMENT_LABEL[sent] ?? sent}
                </span>
              )}
              <a
                className="news-title"
                href={a.url ?? undefined}
                target="_blank"
                rel="noopener noreferrer"
              >
                {title}
              </a>
            </div>
            {a.summary && <p className="news-summary">{a.summary}</p>}
            <div className="news-card-foot">
              <span className="news-tickers">
                {matched.map((t) => (
                  <span className="news-ticker" key={t}>{t}</span>
                ))}
              </span>
              <span className="news-source">{a.source_name ?? ''}</span>
              <span className="news-time">{bjTime(a.published_at)}</span>
            </div>
          </article>
        );
      })}
    </div>
  );
}
