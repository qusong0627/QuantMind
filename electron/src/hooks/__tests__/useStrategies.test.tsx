import { describe, it, expect, vi, beforeEach } from 'vitest';
import { waitFor, act } from '@testing-library/react';
import { renderHookWithProviders } from '../../test-utils/renderWithProviders';
import { useStrategies } from '../useStrategies';
import { strategyService, Strategy } from '../../services/strategyService';

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
        vi.mocked(strategyService.startStrategy).mockResolvedValue({
            code: 200,
            message: 'Started',
            data: { success: true, message: 'Started', status: 'running' },
        });

        const { result } = renderHookWithProviders(() => useStrategies({ autoRefresh: false }));

        await waitFor(() => {
            expect(result.current.loading).toBe(false);
        });

        await act(async () => {
            await result.current.startStrategy('2');
        });

        expect(strategyService.startStrategy).toHaveBeenCalledWith('2');

        const strategy2 = result.current.strategies.find((s) => s.id === '2');
        expect(strategy2?.status).toBe('starting');
    });

    it('should stop strategy with optimistic update and backend call', async () => {
        vi.mocked(strategyService.getStrategies).mockResolvedValue({
            code: 200,
            message: 'Success',
            data: mockStrategies,
        });
        vi.mocked(strategyService.stopStrategy).mockResolvedValue({
            code: 200,
            message: 'Stopped',
            data: { success: true, message: 'Stopped', status: 'paused' },
        });

        const { result } = renderHookWithProviders(() => useStrategies({ autoRefresh: false }));

        await waitFor(() => {
            expect(result.current.loading).toBe(false);
        });

        await act(async () => {
            await result.current.stopStrategy('1');
        });

        expect(strategyService.stopStrategy).toHaveBeenCalledWith('1');

        const strategy1 = result.current.strategies.find((s) => s.id === '1');
        expect(strategy1?.status).toBe('stopped');
    });
});
