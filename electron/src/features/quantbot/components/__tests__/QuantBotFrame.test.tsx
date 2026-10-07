/**
 * `useQuantBotFrame` 探活回归护栏（2026-10-07）。
 *
 * 线上事故：dsh 容器重建窗口内 iframe 恰好加载到浏览器「连接被拒」错误页 ——
 * 错误页也会触发 onLoad，旧实现直接置 connected=true → 错误页永久驻留，且不显示
 * 任何重试入口（实盘栏里连刷新按钮都没有，用户只能整页强刷，表现为「还是这样」）。
 * 修复 = onLoad 后 no-cors fetch 探活：不可达则自动重载（有限次），耗尽后亮
 * 「未响应 + 重新连接」遮罩。
 *
 * 这三条断言正是修复的承诺：正常路径不回归、错误页自愈有限次、手动重连复位额度。
 * 把 handleIframeLoad 退回「直接置 connected」或删掉 retryCountRef 上限，这里必红。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, cleanup } from '@testing-library/react';
import { useQuantBotFrame } from '../QuantBotFrame';

const fetchMock = vi.fn();

beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockReset();
});

afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
});

/** 触发一次 onLoad 并结算其探活 promise 链（fake timers 下 flush 微任务） */
async function fireLoad(result: { current: ReturnType<typeof useQuantBotFrame> }) {
    await act(async () => {
        result.current.handleIframeLoad();
        await vi.advanceTimersByTimeAsync(0);
    });
}

describe('useQuantBotFrame 探活', () => {
    it('探活成功 → connected（正常加载路径不回归）', async () => {
        fetchMock.mockResolvedValue({});
        const { result } = renderHook(() => useQuantBotFrame());

        await fireLoad(result);

        expect(result.current.connected).toBe(true);
        expect(result.current.loading).toBe(false);
        expect(result.current.timedOut).toBe(false);
    });

    it('探活失败 → 按间隔自动重载（iframeKey 变化），额度耗尽后进入 timedOut', async () => {
        fetchMock.mockRejectedValue(new Error('ECONNREFUSED'));
        const { result } = renderHook(() => useQuantBotFrame());

        await fireLoad(result);
        expect(result.current.connected).toBe(false); // 错误页不得被当成已连接

        // 3 次自动重载：每次间隔后 iframeKey 变化（iframe 重新挂载），重挂后仍是错误页
        for (let i = 0; i < 3; i++) {
            const before = result.current.iframeKey;
            await act(async () => {
                await vi.advanceTimersByTimeAsync(4_000);
            });
            expect(result.current.iframeKey).not.toBe(before);
            await fireLoad(result);
        }

        // 额度耗尽：亮「未响应 + 重新连接」，且不再无限重载
        expect(result.current.timedOut).toBe(true);
        expect(result.current.connected).toBe(false);
        expect(result.current.loading).toBe(false);
        const settledKey = result.current.iframeKey;
        await act(async () => {
            await vi.advanceTimersByTimeAsync(10_000);
        });
        expect(result.current.iframeKey).toBe(settledKey);
    });

    it('手动 reload() 重置自动重试额度', async () => {
        fetchMock.mockRejectedValue(new Error('ECONNREFUSED'));
        const { result } = renderHook(() => useQuantBotFrame());

        // 先耗尽自动重试额度
        await fireLoad(result);
        for (let i = 0; i < 3; i++) {
            await act(async () => {
                await vi.advanceTimersByTimeAsync(4_000);
            });
            await fireLoad(result);
        }
        expect(result.current.timedOut).toBe(true);

        // 手动重连 → 额度复位，错误页再次触发自动重载
        await act(async () => {
            result.current.reload();
            await vi.advanceTimersByTimeAsync(0);
        });
        await fireLoad(result);
        const before = result.current.iframeKey;
        await act(async () => {
            await vi.advanceTimersByTimeAsync(4_000);
        });
        expect(result.current.iframeKey).not.toBe(before);
    });
});
