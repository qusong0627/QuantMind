import { describe, test, expect } from 'vitest';
import { buildDailyTrend, shanghaiDate } from '../inferenceTrend';
import type { AdminInferenceDispatchItem } from '../../types';

const item = (created_at: string, status: string): AdminInferenceDispatchItem => ({
    id: created_at + status,
    user_id: '10000001',
    status,
    created_at,
});

// 固定「现在」：2026-10-09 12:00 上海 = 04:00 UTC
const NOW = new Date('2026-10-09T04:00:00Z');

describe('shanghaiDate', () => {
    test('无时区串按上海墙钟解释（历史记录口径）', () => {
        expect(shanghaiDate('2026-10-09 08:05:20')).toBe('2026-10-09');
    });

    test('带时区串换算到上海日历日：UTC 深夜 = 上海次日', () => {
        // 2026-10-08T20:00:00Z = 上海 2026-10-09 04:00
        expect(shanghaiDate('2026-10-08T20:00:00Z')).toBe('2026-10-09');
        expect(shanghaiDate('2026-10-08 20:00:00+00:00')).toBe('2026-10-09');
    });

    test('空值与不可解析返回 null', () => {
        expect(shanghaiDate(null)).toBeNull();
        expect(shanghaiDate('')).toBeNull();
        expect(shanghaiDate('not-a-date')).toBeNull();
    });
});

describe('buildDailyTrend', () => {
    test('返回最近 N 天且缺日补零，旧→新有序', () => {
        const trend = buildDailyTrend([], 14, NOW);

        expect(trend).toHaveLength(14);
        expect(trend[0].date).toBe('2026-09-26');
        expect(trend[13].date).toBe('2026-10-09');
        expect(trend.every((d) => d.total === 0)).toBe(true);
    });

    test('按状态分桶计数；窗口外的记录被丢弃', () => {
        const trend = buildDailyTrend(
            [
                item('2026-10-09 08:05:20', 'success'),
                item('2026-10-09 08:06:00', 'failed'),
                item('2026-10-09 08:07:00', 'failed'),
                item('2026-10-09 08:08:00', 'skipped'),
                item('2026-08-01 08:00:00', 'failed'), // 窗口外
            ],
            14,
            NOW,
        );

        const last = trend[13];
        expect(last).toMatchObject({ success: 1, failed: 2, skipped: 1, total: 4 });
        expect(trend.reduce((n, d) => n + d.total, 0)).toBe(4);
    });

    test('未知状态计入 total 但不进任何状态类', () => {
        const trend = buildDailyTrend([item('2026-10-09 09:00:00', 'whatever')], 14, NOW);

        expect(trend[13]).toMatchObject({ success: 0, failed: 0, skipped: 0, total: 1 });
    });

    test('跨日边界：UTC 深夜的带时区串落到次日桶', () => {
        const trend = buildDailyTrend([item('2026-10-08T20:00:00Z', 'success')], 14, NOW);

        expect(trend[13].date).toBe('2026-10-09');
        expect(trend[13].success).toBe(1);
        expect(trend[12].total).toBe(0);
    });
});
