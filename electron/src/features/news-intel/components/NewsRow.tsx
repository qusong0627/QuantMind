/** 一条资讯：3px 权重条 + 一个主标签 + 标题；其余证据折叠。 */

import { useState } from 'react';
import type { NewsIntelItem } from '../services/newsIntelService';

interface NewsRowProps {
  item: NewsIntelItem;
  /** 是否显示所属股票（订阅合并流/板块流用） */
  showStock?: boolean;
  onOpenStock?: (code: string) => void;
  /** 点击标题 → 右侧阅读抽屉（不跳转） */
  onSelect?: (item: NewsIntelItem) => void;
}

const DECISION_CLASS: Record<string, string> = {
  P0: 'is-risk',
  P1: 'is-warn',
  P2: 'is-caution',
  WATCH: 'is-watch',
  INFO: 'is-info',
};

function importanceBars(importance: string): string {
  if (importance === 'high') return '●●●';
  if (importance === 'medium') return '●●○';
  return '●○○';
}

function timeText(value: string | null): string {
  if (!value) return '';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export function NewsRow({ item, showStock = false, onOpenStock, onSelect }: NewsRowProps) {
  const [expanded, setExpanded] = useState(false);
  const tone = DECISION_CLASS[item.decision] || 'is-info';
  const directionClass =
    item.direction === '利好' ? 'is-up' : item.direction === '利空' ? 'is-down' : 'is-flat';

  return (
    <article className={`ni-row ${tone} ${item.importance === 'high' ? 'is-important' : ''}`}>
      <div className="ni-row-gutter" title={item.published_at || ''}>
        {timeText(item.published_at)}
      </div>
      <span className="ni-row-bar" aria-hidden="true" />
      <div className="ni-row-main">
        <div className="ni-row-head">
          <span className={`ni-chip ${tone}`}>{item.primary_label}</span>
          {item.tier === 'L1' && <span className="ni-tier" title="标题/摘要出现股票代码">L1</span>}
          {item.tier === 'L2' && <span className="ni-tier" title="标题出现公司全名">L2</span>}
          {item.tier === 'L3' && (
            <span className="ni-tag-muted" title="只在正文提及，不参与个股决策">提及</span>
          )}
          {item.speculative && (
            <span className="ni-tag-spec" title="投机/打板类稿件，研究口径下会被降权">投机</span>
          )}
          {item.aggregator && (
            <span className="ni-tag-muted" title="聚合页来源，降权">聚合</span>
          )}
          {item.is_research && (
            <span className="ni-tag-muted" title="券商研报，不计个股事件">研报</span>
          )}
          {item.is_roundup && (
            <span className="ni-tag-muted" title="多票汇总稿，不计个股事件">汇总</span>
          )}
          {showStock && item.code && (
            <button
              type="button"
              className="ni-stock-link"
              onClick={() => onOpenStock?.(item.code as string)}
            >
              {item.name || item.code}
            </button>
          )}
        </div>
        {onSelect ? (
          <button
            type="button"
            className="ni-title ni-title-btn"
            title={`${item.title}（点击在右侧阅读）`}
            onClick={() => onSelect(item)}
          >
            {item.title}
          </button>
        ) : (
          <a
            className="ni-title"
            href={item.url || undefined}
            target="_blank"
            rel="noreferrer"
            title={item.title}
          >
            {item.title}
          </a>
        )}
        <div className="ni-meta">
          <span className={`ni-direction ${directionClass}`}>{item.direction}</span>
          <span>·</span>
          <span>{item.source || '未知来源'}</span>
          <span>·</span>
          <span className="ni-importance" title={`重要度 ${item.weight}/100`}>
            {importanceBars(item.importance)}
          </span>
          {item.reasons.length > 0 && (
            <button
              type="button"
              className="ni-reasons-toggle"
              aria-expanded={expanded}
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? '收起依据' : '依据'}
            </button>
          )}
        </div>
        {expanded && (
          <ul className="ni-reasons">
            {item.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        )}
      </div>
    </article>
  );
}
