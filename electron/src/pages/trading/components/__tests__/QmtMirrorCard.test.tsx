/**
 * 真单镜像控制卡的**失败可见性**护栏。
 *
 * 为什么需要这一层：后端的 `mirror:rejects` 是**连续**计数，成功一笔就清零。于是
 * 「失败一笔、成功一笔、再失败一笔」在它上面永远是 0——卡片一声不响，而那是真钱
 * 委托。`daily_failures`（当日台账）是补上的那一格，这条用例钉住「计数归零也要
 * 显示今天失败过几笔」，以及「没有这个字段的老后端不许把卡片炸掉」（前端与后端
 * 分开部署，滚动期间会出现这个组合）。
 *
 * 网络一律打桩：本文件测的是渲染判据，不是镜像控制面本身（后端在
 * `backend/tests/test_qmt_exec_mirror.py`）。
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';

import QmtMirrorCard from '../QmtMirrorCard';

const baseStatus = (over: Record<string, unknown> = {}) => ({
    enabled: true,
    env_enabled: true,
    kill_switch: false,
    whitelist: ['*'],
    blacklist: [],
    config: {
        max_order_value: 10000,
        max_daily_value: 50000,
        max_daily_symbols: 5,
        max_daily_orders: 20,
        max_slippage_pct: 0.02,
        max_consecutive_rejects: 3,
        queue_outside_hours: true,
        markets: ['CN'],
    },
    quota: { date: '20260924', daily_value: 0, daily_orders: 0, daily_symbols: 0 },
    queue_length: 0,
    consecutive_rejects: 0,
    daily_failures: {
        date: '20260924',
        count: 2,
        ledger: { 'SH600519:broker_rejected': 2 },
    },
    trading_time: false,
    broker_selected: 'qmt_exec',
    real_trading_ready: true,
    blocked_reason: '',
    ...over,
});

const okResponse = (payload: unknown) =>
    ({ ok: true, json: async () => payload }) as unknown as Response;

function stubFetch(statusPayload: unknown) {
    return vi.fn(async (url: string) => {
        if (String(url).includes('/qmt-mirror/status')) {
            return okResponse(statusPayload);
        }
        return okResponse({ date: '20260924', summary: {}, items: [] });
    }) as unknown as typeof fetch;
}

const originalFetch = globalThis.fetch;

afterEach(() => {
    globalThis.fetch = originalFetch;
});

describe('QmtMirrorCard 失败可见性', () => {
    it('连续计数归零时仍显示今日失败笔数', async () => {
        globalThis.fetch = stubFetch(baseStatus({ consecutive_rejects: 0 }));
        render(<QmtMirrorCard />);
        await waitFor(() =>
            expect(screen.getByText(/今日下单失败 2/)).toBeInTheDocument(),
        );
    });

    it('今天没失败就不显示那枚标签', async () => {
        globalThis.fetch = stubFetch(
            baseStatus({ daily_failures: { date: '20260924', count: 0, ledger: {} } }),
        );
        render(<QmtMirrorCard />);
        await waitFor(() =>
            expect(screen.getByText('镜像已开启')).toBeInTheDocument(),
        );
        expect(screen.queryByText(/今日下单失败/)).toBeNull();
    });

    it('老后端没有这个字段也不炸（滚动部署）', async () => {
        const legacy = baseStatus();
        delete (legacy as Record<string, unknown>).daily_failures;
        globalThis.fetch = stubFetch(legacy);
        render(<QmtMirrorCard />);
        await waitFor(() =>
            expect(screen.getByText('镜像已开启')).toBeInTheDocument(),
        );
        expect(screen.queryByText(/今日下单失败/)).toBeNull();
    });
});
