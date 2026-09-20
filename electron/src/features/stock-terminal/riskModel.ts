/**
 * 候选列表风险闸 · 纯逻辑（与渲染分离，便于单测盯口径）。
 *
 * 三个排除开关 + 每行的风险/新闻徽章都由这里给出判据。两个「错了也不报错」的点：
 *
 * 1. **开关语义是「未显式关掉即开启」**（`EXCLUDE_ON`）。用 `?? true` 补默认值也行，
 *    但那样「清空筛选」（onChange({})）会把风险闸一起放行——用户点清空是想看得更多，
 *    不是想买风险股。判据收在这里，一处说清。
 * 2. **`pos_move` 不出徽章**。涨停/大涨/创新高这类行情标签近 20 天命中全市场约 39%，
 *    见谁都亮绿等于没信息；只有 `pos_strong`（业绩预增/回购/中标/增持…）才有区分度。
 */

import type { NewsTagBucket, StockRisk } from '../stock-terminal-shared/types';

/**
 * 排除闸的开启判据：**只有显式 `false` 才算关**。
 *
 * 调用方（候选列表页）不必写三个 `true`，而 `onChange({})` 这种「清空筛选」
 * 也不会顺手把风险闸放行。要放行只能点开关本身。
 */
export const EXCLUDE_ON = (v: boolean | undefined): boolean => v !== false;

/** 一枚徽章的渲染描述（`title` 是悬停说明，含证据） */
export interface RiskChip {
  key: string;
  label: string;
  cls: string;
  title: string;
}

/** 新闻标签桶 → 徽章（行长有限，只出三枚；`pos_move` 故意不出，见文件头） */
const NEWS_BADGE: { key: string; label: string; cls: string }[] = [
  { key: 'risk', label: '利空', cls: 'bg-rose-100 text-rose-700' },
  { key: 'warn', label: '提示', cls: 'bg-amber-100 text-amber-700' },
  { key: 'weak', label: '提示', cls: 'bg-amber-100 text-amber-700' },
  { key: 'pos_strong', label: '利好', cls: 'bg-emerald-100 text-emerald-700' },
];

/** 桶 → 悬停说明：标签×条数；利空另带证据标题（用户要能一眼看到凭什么说它利空） */
function bucketTitle(label: string, buckets: NewsTagBucket[]): string {
  return (
    `${label}：` +
    buckets
      .map((b) => {
        const when = b.last ? `（最近 ${b.last.slice(5, 10)}）` : '';
        const ev = b.samples?.length ? `\n　· ${b.samples.slice(0, 2).join('\n　· ')}` : '';
        return `${b.tag} ×${b.n}${when}${ev}`;
      })
      .join('\n')
  );
}

/**
 * 单行的风险/新闻徽章（无任何命中返回空数组，调用方据此不渲染徽章区）。
 *
 * 颜色是**风险语义**而不是行情语义：这一列回答的是「能不能买」，红=劝退、绿=加分，
 * 与同行的 ST 徽章（rose）一致；涨红跌绿只用于价格与信号列。
 * 利空与利好同时命中就都显示，不做优先级吞并——用户要看到全部理由再自己判断。
 */
export function riskChips(risk?: StockRisk | null): RiskChip[] {
  if (!risk) return [];
  const chips: RiskChip[] = [];
  const news = risk.news ?? {};

  // 名单命中：灰色（它不是「坏消息」，是「按既定纪律不买」）
  const hit = risk.hits?.[0];
  if (hit) {
    const labels = hit.source_labels?.length ? hit.source_labels.join('、') : (hit.sources ?? []).join('、');
    chips.push({
      key: 'list',
      label: '名单',
      cls: risk.excluded ? 'bg-slate-200 text-slate-600' : 'bg-slate-100 text-slate-400',
      title: `名单命中（${labels}）：${hit.reason ?? '—'}`,
    });
  }

  // 新闻桶：warn/weak 同色，合成一枚「提示」但条数分别累计
  const combined: { label: string; cls: string; buckets: NewsTagBucket[]; n: number }[] = [];
  for (const spec of NEWS_BADGE) {
    const b = (news[spec.key] ?? []).filter((x) => (x?.n ?? 0) > 0);
    if (!b.length) continue;
    const prev = combined.find((c) => c.label === spec.label);
    if (prev) {
      prev.buckets.push(...b);
      prev.n += b.reduce((s, x) => s + x.n, 0);
    } else {
      combined.push({ label: spec.label, cls: spec.cls, buckets: [...b], n: b.reduce((s, x) => s + x.n, 0) });
    }
  }
  for (const c of combined) {
    chips.push({
      key: c.label,
      label: `${c.label}${c.n > 1 ? ` ${c.n}` : ''}`,
      cls: c.cls,
      title: bucketTitle(c.label, c.buckets),
    });
  }
  return chips;
}

/** 风险条里单个通道的展示态（关掉时显示「放行」而不是 0，否则读起来像「一只都没命中」） */
export function channelText(on: boolean, n: number): string {
  return on ? String(n) : '放行';
}
