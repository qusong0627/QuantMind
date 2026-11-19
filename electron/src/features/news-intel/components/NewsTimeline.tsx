/** 资讯时间轴：日期槽 + 竖发丝线 + 权重条。 */

import dayjs from 'dayjs';
import { Spin } from 'antd';
import { NewsRow } from './NewsRow';
import type { NewsIntelItem } from '../services/newsIntelService';

interface NewsTimelineProps {
  items: NewsIntelItem[];
  loading?: boolean;
  showStock?: boolean;
  emptyText?: string;
  onOpenStock?: (code: string) => void;
  onSelect?: (item: NewsIntelItem) => void;
}

function dayLabel(key: string): string {
  const d = dayjs(key);
  if (!d.isValid()) return '日期未知';
  const today = dayjs().startOf('day');
  const diff = today.diff(d.startOf('day'), 'day');
  if (diff === 0) return '今天';
  if (diff === 1) return '昨天';
  return d.format('MM-DD ddd');
}

export function NewsTimeline({
  items,
  loading = false,
  showStock = false,
  emptyText = '这个时间窗内没有匹配的资讯',
  onOpenStock,
  onSelect,
}: NewsTimelineProps) {
  const groups = new Map<string, NewsIntelItem[]>();
  for (const item of items) {
    const key = item.published_at ? dayjs(item.published_at).format('YYYY-MM-DD') : 'unknown';
    const bucket = groups.get(key);
    if (bucket) bucket.push(item);
    else groups.set(key, [item]);
  }

  if (loading) {
    return (
      <div className="ni-loading">
        <Spin />
      </div>
    );
  }

  if (!items.length) {
    return <div className="ni-empty">{emptyText}</div>;
  }

  return (
    <div className="ni-timeline">
      {[...groups.entries()].map(([key, rows]) => (
        <section className="ni-day" key={key}>
          <header className="ni-day-head">
            <span className="ni-day-label">{dayLabel(key)}</span>
            <span className="ni-day-count">{rows.length} 条</span>
          </header>
          <div className="ni-day-body">
            {rows.map((item) => (
              <NewsRow
                key={`${item.page_id}-${item.code || ''}`}
                item={item}
                showStock={showStock}
                onOpenStock={onOpenStock}
                onSelect={onSelect}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
