/** 个股 RSS 资讯 Tab：Huntly 标题关键词检索 + 情绪标签（FinBERT/字典法）
 *  + 新闻情绪深度报告匹配（来源可信度 × 时段质量 × 事件标签），badge 一眼可见。 */

import { useEffect, useState } from 'react';
import { Rss, ExternalLink, TrendingUp, TrendingDown } from 'lucide-react';
import { message, Spin } from 'antd';
import { stockTerminalService } from '../../services/stockTerminalService';

interface NewsItem {
  id: number;
  title: string;
  link: string | null;
  published_at: string | null;
  source: string;
  source_name?: string;
  source_tier?: 'gold' | 'reverse' | 'neutral';
  sentiment_label?: 'bullish' | 'bearish' | 'neutral' | null;
  sentiment_score?: number | null;
  tickers?: string[];
  hour_tier?: 'gold' | 'evening' | 'morning' | 'close' | 'early' | 'weak' | 'noise' | 'normal' | null;
  hour_label?: string | null;
  event_tags?: { tag: string; dir: string }[];
  note?: string | null;
}

const SENT_TONE: Record<string, string> = {
  bullish: 'bg-rose-50 text-rose-600 border-rose-200',
  bearish: 'bg-emerald-50 text-emerald-600 border-emerald-200',
};

/** 来源可信度 badge：白名单=高质量源 / 黑名单=反向指标 */
function SourceBadge({ tier }: { tier?: string }) {
  if (tier === 'gold') return <span className="shrink-0 text-[9px] font-black rounded px-1 py-0.5 bg-amber-100 text-amber-700">高质量源</span>;
  if (tier === 'reverse') return <span className="shrink-0 text-[9px] font-black rounded px-1 py-0.5 bg-violet-50 text-violet-500" title="报告判定的反向指标：情绪标签不可信">反向源</span>;
  return null;
}

/** 时段质量 badge：只标注黄金/噪声两个极端 */
function HourBadge({ tier }: { tier?: string | null }) {
  if (tier === 'gold') return <span className="shrink-0 text-[9px] font-black rounded px-1 py-0.5 bg-amber-50 text-amber-600">黄金时段</span>;
  if (tier === 'noise') return <span className="shrink-0 text-[9px] font-bold rounded px-1 py-0.5 bg-slate-100 text-slate-400">噪声时段</span>;
  return null;
}

/** 事件标签：监管/立案=利空(绿)，业绩/涨停/增持=利好(红)，政策/财报=中性(灰) */
function EventTags({ tags }: { tags: NewsItem['event_tags'] }) {
  if (!tags?.length) return null;
  return (
    <>
      {tags.slice(0, 3).map(t => {
        const cls = t.dir === 'bearish'
          ? 'bg-emerald-50 text-emerald-600 border-emerald-200'
          : t.dir === 'bullish'
            ? 'bg-rose-50 text-rose-600 border-rose-200'
            : 'bg-slate-50 text-slate-400 border-slate-200';
        return (
          <span key={t.tag} className={`shrink-0 text-[9px] font-bold rounded border px-1 py-0.5 ${cls}`}>
            {t.tag}
          </span>
        );
      })}
    </>
  );
}

export function NewsTab({ symbol }: { symbol: string }) {
  const [items, setItems] = useState<NewsItem[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!symbol) return;
    let c = false;
    setLoading(true);
    stockTerminalService.getNews(symbol).then(r => {
      if (!c) setItems(r.items);
    }).catch(() => message.error('资讯加载失败')).finally(() => { if (!c) setLoading(false); });
    return () => { c = true; };
  }, [symbol]);

  return (
    <Spin spinning={loading}>
      <div className="flex flex-col gap-1">
        {!items.length && !loading && (
          <div className="py-8 text-center text-[11px] text-slate-400">暂无可匹配的资讯（标题检索）</div>
        )}
        {items.map(it => (
          <a
            key={it.id}
            href={it.link || undefined}
            target="_blank"
            rel="noreferrer"
            className="flex items-start gap-2 px-2 py-1.5 rounded-lg hover:bg-slate-50 transition-colors group"
          >
            <Rss className="w-3 h-3 text-orange-400 mt-0.5 shrink-0" />
            <span className="flex-1 min-w-0">
              <span className="flex flex-wrap items-center gap-1.5 min-w-0">
                {/* 情绪标签（红=利好 绿=利空） */}
                {it.sentiment_label === 'bullish' && (
                  <span className={`shrink-0 inline-flex items-center gap-0.5 text-[9px] font-bold rounded border px-1 py-0.5 ${SENT_TONE.bullish}`}>
                    <TrendingUp className="w-2.5 h-2.5" /> 利好{it.sentiment_score != null && Math.abs(it.sentiment_score) >= 0.5 ? ` ${it.sentiment_score.toFixed(2)}` : ''}
                  </span>
                )}
                {it.sentiment_label === 'bearish' && (
                  <span className={`shrink-0 inline-flex items-center gap-0.5 text-[9px] font-bold rounded border px-1 py-0.5 ${SENT_TONE.bearish}`}>
                    <TrendingDown className="w-2.5 h-2.5" /> 利空{it.sentiment_score != null && Math.abs(it.sentiment_score) >= 0.5 ? ` ${it.sentiment_score.toFixed(2)}` : ''}
                  </span>
                )}
                {/* 来源可信度 */}
                <SourceBadge tier={it.source_tier} />
                {/* 时段质量 */}
                <HourBadge tier={it.hour_tier} />
                {/* 事件标签 */}
                <EventTags tags={it.event_tags} />
              </span>
              <span className="block text-xs text-slate-700 group-hover:text-blue-600 leading-snug line-clamp-2 mt-0.5">
                {it.title}
              </span>
              <span className="text-[10px] text-slate-400 mt-1 block">
                {it.published_at?.replace('T', ' ') || ''} · {it.source_name || it.source || ''}
              </span>
              {it.note && (
                <span className="text-[10px] font-bold mt-0.5 block text-slate-400">{it.note}</span>
              )}
            </span>
            <ExternalLink className="w-3 h-3 text-slate-300 group-hover:text-blue-400 shrink-0 mt-1" />
          </a>
        ))}
      </div>
    </Spin>
  );
}