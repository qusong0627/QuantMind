import { describe, it, expect, vi, beforeEach } from 'vitest';
import { waitFor, act } from '@testing-library/react';
import { renderHookWithProviders } from '../../test-utils/renderWithProviders';
import { useStrategies } from '../useStrategies';
import { strategyService, Strategy, StrategyActionResponse } from '../../services/strategyService';

vi.mock('../../services/strategyService', () => ({
    strategyService: {
        getStrategies: vi.fn(),
        startStrategy: vi.fn(),
        stopStrategy: vi.fn(),
    },
}));

describe('useStrategies', () => {
    const mockStrategies: Strategy[] = [
        {
            id: '1',
            name: 'Strategy 1',
            status: 'running',
            total_return: 10,
            today_return: 1,
            today_pnl: 1000,
            risk_level: 'medium',
            created_at: '2025-01-01T00:00:00Z',
            updated_at: '2025-01-01T00:00:00Z',
        },
        {
            id: '2',
            name: 'Strategy 2',
            status: 'stopped',
            total_return: -5,
            today_return: -0.5,
            today_pnl: -250,
            risk_level: 'high',
            created_at: '2025-01-01T00:00:00Z',
            updated_at: '2025-01-01T00:00:00Z',
        },
        {
            id: '3',
            name: 'Strategy 3',
            status: 'error',
            total_return: 0,
            today_return: 0,
            today_pnl: 0,
            risk_level: 'low',
            created_at: '2025-01-01T00:00:00Z',
            updated_at: '2025-01-01T00:00:00Z',
        },
    ];

    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('should fetch strategies and calculate stats successfully', async () => {
        vi.mocked(strategyService.getStrategies).mockResolvedValue({
            code: 200,
            message: 'Success',
            data: mockStrategies,
        });

        const { result } = renderHookWithProviders(() => useStrategies({ autoRefresh: false }));

        expect(result.current.loading).toBe(true);
        expect(result.current.strategies).toEqual([]);

        await waitFor(() => {
            expect(result.current.loading).toBe(false);
        });

        expect(result.current.strategies).toEqual(mockStrategies);
        expect(result.current.error).toBeNull();
        expect(result.current.stats).toEqual({
            totalStrategies: 3,
            activeStrategies: 1,
            stoppedStrategies: 1,
            errorStrategies: 1,
            totalReturn: 5,
            todayReturn: 0.5,
            todayPnL: 750,
        });
    });

    it('should handle API failure gracefully', async () => {
        vi.mocked(strategyService.getStrategies).mockResolvedValue({
            code: 503,
            message: 'Backend offline',
            data: [],
        });

        const { result } = renderHookWithProviders(() => useStrategies({ autoRefresh: false }));

        await waitFor(() => {
            expect(result.current.loading).toBe(false);
        });

        expect(result.current.error).toBe('Backend offline');
        expect(result.current.strategies).toEqual([]);
    });

    it('should start strategy with optimistic update and backend call', async () => {
        vi.mocked(strategyService.getStrategies).mockResolvedValue({
            code: 200,
            message: 'Success',
            data: mockStrategies,
        });
        // 用 deferred promise 把后端调用挂起，以便在「请求进行中」断言乐观更新。
        // 若直接 await 整个操作，随后的 fetchData 会用服务端返回的状态覆盖乐观值，
        // 那样断言到的就不是乐观更新了（乐观值只是临时占位）。
        let resolveStart!: (v: StrategyActionResponse) => void;
        vi.mocked(strategyService.startStrategy).mockReturnValue(
            new Promise<StrategyActionResponse>((resolve) => {
                resolveStart = resolve;
            }),
        );

        const { result } = renderHookWithProviders(() => useStrategies({ autoRefresh: false }));

        await waitFor(() => {
            expect(result.current.loading).toBe(false);
        });

        let pending!: Promise<boolean>;
        await act(async () => {
            pending = result.current.startStrategy('2');
        });

        expect(strategyService.startStrategy).toHaveBeenCalledWith('2');

        // 后端尚未返回：此时应看到乐观状态
        const optimistic = result.current.strategies.find((s) => s.id === '2');
        expect(optimistic?.status).toBe('starting');

        // 放行后端响应并收尾，避免留下悬挂的 act
        await act(async () => {
            resolveStart({
                code: 200,
                message: 'Started',
                data: { success: true, message: 'Started', status: 'running' },
            });
            await pending;
        });
    });

    it('should stop strategy with optimistic update and backend call', async () => {
        vi.mocked(strategyService.getStrategies).mockResolvedValue({
            code: 200,
            message: 'Success',
            data: mockStrategies,
        });
        // 同 start：挂起后端调用，在请求进行中断言乐观更新。
        let resolveStop!: (v: StrategyActionResponse) => void;
        vi.mocked(strategyService.stopStrategy).mockReturnValue(
            new Promise<StrategyActionResponse>((resolve) => {
                resolveStop = resolve;
            }),
        );

        const { result } = renderHookWithProviders(() => useStrategies({ autoRefresh: false }));

        await waitFor(() => {
            expect(result.current.loading).toBe(false);
        });

        let pending!: Promise<boolean>;
        await act(async () => {
            pending = result.current.stopStrategy('1');
        });

        expect(strategyService.stopStrategy).toHaveBeenCalledWith('1');

        // 后端尚未返回：此时应看到乐观状态
        const optimistic = result.current.strategies.find((s) => s.id === '1');
        expect(optimistic?.status).toBe('stopped');

        // 放行后端响应并收尾，避免留下悬挂的 act
        await act(async () => {
            resolveStop({
                code: 200,
                message: 'Stopped',
                data: { success: true, message: 'Stopped', status: 'paused' },
            });
            await pending;
        });
    });
});
