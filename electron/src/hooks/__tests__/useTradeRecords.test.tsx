import { describe, it, expect, vi, beforeEach } from 'vitest';
import { waitFor } from '@testing-library/react';
import { renderHookWithProviders } from '../../test-utils/renderWithProviders';
import { useTradeRecords } from '../useTradeRecords';
import { tradingService } from '../../services/tradingService';
import { refreshOrchestrator } from '../../services/refreshOrchestrator';

vi.mock('../../services/tradingService', () => ({
    tradingService: {
        getRecentTrades: vi.fn(),
    },
}));

vi.mock('../../services/refreshOrchestrator', () => ({
    refreshOrchestrator: {
        register: vi.fn().mockReturnValue(() => {}),
    },
}));

describe('useTradeRecords', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.mocked(tradingService.getRecentTrades).mockResolvedValue({
            records: [],
            isOffline: false,
            isFallbackToOrders: false,
        });
    });

    it('实盘模式应透传 real 给服务层', async () => {
        renderHookWithProviders(() => useTradeRecords({ tradingMode: 'real', autoRefresh: false }));

        await waitFor(() => {
            expect(tradingService.getRecentTrades).toHaveBeenCalled();
        });
        expect(tradingService.getRecentTrades).toHaveBeenCalledWith(10, 'real');
    });

    it('模拟盘模式应透传 simulation 给服务层', async () => {
        renderHookWithProviders(() => useTradeRecords({ tradingMode: 'simulation', autoRefresh: false }));

        await waitFor(() => {
            expect(tradingService.getRecentTrades).toHaveBeenCalled();
        });
        expect(tradingService.getRecentTrades).toHaveBeenCalledWith(10, 'simulation');
    });

    it('autoRefresh=true 时应注册刷新协调器', async () => {
        renderHookWithProviders(() => useTradeRecords({ autoRefresh: true, refreshInterval: 5000 }));

        await waitFor(() => {
            expect(refreshOrchestrator.register).toHaveBeenCalled();
        });
        expect(refreshOrchestrator.register).toHaveBeenCalledWith(
            'trade-records',
            expect.any(Function),
            { minIntervalMs: 5000 },
        );
    });
});
