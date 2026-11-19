/** 居中阅读弹层：正文 + 标签依据 + 新闻后走势，不跳转。 */

import { useEffect, useState } from 'react';
import { Modal, Spin } from 'antd';
import { ExternalLink, X } from 'lucide-react';
import { newsService, type NewsArticleDetail } from '../../news/services/newsService';
import { sanitizeHtml } from '../../../utils/sanitizeHtml';
import type { MarketKey, NewsIntelItem } from '../services/newsIntelService';
import { EventPathChart } from './EventPathChart';

interface ArticleDrawerProps {
  open: boolean;
  item: NewsIntelItem | null;
  market: MarketKey;
  code?: string | null;
  onClose: () => void;
}

export function ArticleDrawer({ open, item, market, code, onClose }: ArticleDrawerProps) {
  const [detail, setDetail] = useState<NewsArticleDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !item) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setDetail(null);
    newsService
      .getArticle(item.page_id)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : '正文加载失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, item]);

  const html = detail?.content_html || detail?.content || '';
  const summary = detail?.summary || '';
  const title = item?.title || detail?.title || '';
  const tone =
    item?.direction === '利好' ? 'up' : item?.direction === '利空' ? 'down' : 'flat';

  return (
    <Modal
      open={open}
      onCancel={onClose}
      centered
      footer={null}
      width={760}
      destroyOnClose
      closable={false}
      className="ni-article-modal"
      styles={{ body: { padding: 0 } }}
    >
      {item && (
        <div className={`ni-reader ni-reader-${tone}`}>
          <header className="ni-reader-head">
            <div className="ni-reader-top">
              <span className={`ni-chip ${item.decision === 'P0' ? 'is-risk' : item.decision === 'P1' ? 'is-warn' : item.decision === 'P2' ? 'is-caution' : item.decision === 'WATCH' ? 'is-watch' : 'is-info'}`}>
                {item.primary_label}
              </span>
              <span className="ni-reader-source">
                {item.source || '未知来源'} · {item.published_at?.replace('T', ' ').slice(0, 16) || ''}
              </span>
              <button type="button" className="ni-icon-btn ni-reader-close" onClick={onClose} title="关闭">
                <X size={16} />
              </button>
            </div>
            <h2>{title}</h2>
            <div className="ni-reader-tags">
              <span className={`ni-direction ${item.direction === '利好' ? 'is-up' : item.direction === '利空' ? 'is-down' : 'is-flat'}`}>
                {item.direction}
              </span>
              <span className="ni-tier">{item.tier}</span>
              {item.speculative && <span className="ni-tag-spec">投机</span>}
              {item.is_research && <span className="ni-tag-muted">研报</span>}
              {item.is_roundup && <span className="ni-tag-muted">汇总</span>}
              {item.layer > 0 && <span className="ni-tag-muted">转载第 {item.layer} 层</span>}
            </div>
          </header>

          {item.reasons.length > 0 && (
            <section className="ni-reader-block">
              <div className="ni-kicker">依据</div>
              <ul className="ni-reasons is-open">
                {item.reasons.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </section>
          )}

          {code && item.published_at && item.is_subject && (
            <section className="ni-reader-block">
              <EventPathChart
                market={market}
                code={code}
                date={item.published_at}
                direction={item.direction}
              />
            </section>
          )}
          {code && item.published_at && !item.is_subject && (
            <section className="ni-reader-block">
              <div className="ni-path-note">
                这条只在正文提及该股票（L3），不是个股事件；不做「新闻后走势」归属，避免把市场稿当成个股信号。
              </div>
            </section>
          )}

          <section className="ni-reader-block ni-reader-body">
            <div className="ni-kicker">正文</div>
            {loading ? (
              <div className="ni-reader-loading">
                <Spin />
              </div>
            ) : error ? (
              <div className="ni-reader-error">{error}</div>
            ) : html ? (
              <div
                className="ni-reader-content"
                dangerouslySetInnerHTML={{ __html: sanitizeHtml(html) }}
              />
            ) : (
              <div className="ni-reader-content">
                <p>{summary || '该来源未提供正文，可打开原文阅读。'}</p>
              </div>
            )}
          </section>

          {item.url && (
            <footer className="ni-reader-foot">
              <a href={item.url} target="_blank" rel="noreferrer">
                <ExternalLink size={14} /> 打开原文
              </a>
              <span>正文由 Huntly 抓取；右侧阅读不改变原始链接。</span>
            </footer>
          )}
        </div>
      )}
    </Modal>
  );
}
