/**
 * 推理派发记录的按日趋势（推理监控页 2026-10-09 机构版改版）。
 *
 * 页面只有一张流水表时，「最近是不是越来越糟」要靠人逐行数。这里把已加载的
 * 记录按**上海日历日**分桶成最近 N 天（缺日补零，保证横轴连续），供堆叠小柱图。
 *
 * created_at 的口径陷阱与 formatTime 相同：历史记录里混着带时区
 *（+08:00 / Z）与不带时区的两种串；不带时区的一律按上海墙钟解释。
 */
import type { AdminInferenceDispatchItem } from '../types';

export interface TrendDay {
    /** 上海日历日 YYYY-MM-DD */
    date: string;
    success: number;
    failed: number;
    skipped: number;
    /** 该日全部记录数（含未知状态），柱高以此为基准 */
    total: number;
}

const SHANGHAI = 'Asia/Shanghai';

const dayFmt = new Intl.DateTimeFormat('sv-SE', {
    timeZone: SHANGHAI,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
});

/** created_at → 上海日历日；空值/不可解析返回 null（该条不进桶，页面上仍有汇总兜底）。 */
export function shanghaiDate(value?: string | null): string | null {
    if (!value) return null;
    const raw = value.trim().replace('T', ' ');
    if (!raw) return null;
    const hasZone = /[zZ]|[+-]\d{2}:?\d{2}$/.test(raw);
    const parsed = new Date(hasZone ? raw : `${raw.replace(' ', 'T')}+08:00`);
    if (Number.isNaN(parsed.getTime())) return null;
    return dayFmt.format(parsed); // sv-SE 输出即 YYYY-MM-DD
}

/** 最近 days 个自然日（含 now 当天）逐日桶，旧→新有序。 */
export function buildDailyTrend(
    items: readonly AdminInferenceDispatchItem[],
    days = 14,
    now: Date = new Date(),
): TrendDay[] {
    const order: string[] = [];
    const buckets = new Map<string, TrendDay>();
    for (let i = days - 1; i >= 0; i--) {
        const key = dayFmt.format(new Date(now.getTime() - i * 86_400_000));
        order.push(key);
        buckets.set(key, { date: key, success: 0, failed: 0, skipped: 0, total: 0 });
    }
    for (const item of items) {
        const key = shanghaiDate(item.created_at);
        const bucket = key ? buckets.get(key) : undefined;
        if (!bucket) continue;
        bucket.total += 1;
        if (item.status === 'success') bucket.success += 1;
        else if (item.status === 'failed') bucket.failed += 1;
        else if (item.status === 'skipped') bucket.skipped += 1;
    }
    return order.map((key) => buckets.get(key) as TrendDay);
}
