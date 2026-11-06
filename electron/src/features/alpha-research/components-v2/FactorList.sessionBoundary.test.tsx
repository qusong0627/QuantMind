import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { invalidateSession } from '../../../store/sessionBoundary';
import { FactorList } from './FactorList';

const api = vi.hoisted(() => ({
  explainFactor: vi.fn(),
  exportFactorToIde: vi.fn(),
}));

vi.mock('../services-v2/api', () => api);

const metrics = {
  ic: 0, icir: 0, rankIc: 0.03, rankIcir: 0,
  annualReturn: 0, sharpeRatio: 0, maxDrawdown: 0,
  totalFactors: 1, highQualityFactors: 0, mediumQualityFactors: 1, lowQualityFactors: 0,
  top10Factors: [{
    factorId: 'owner-factor-1', factorName: 'A股因子', factorExpression: 'Ref($close, 1)',
    rankIc: 0.03, rankIcir: 0, ic: 0, icir: 0,
  }],
};

describe('mining dashboard factor actions', () => {
  afterEach(() => {
    localStorage.clear();
  });

  it('routes backtest selection and keeps private explanations out of browser storage', async () => {
    const onBacktestFactor = vi.fn();
    api.explainFactor.mockResolvedValue({ success: true, data: { explanation: '本人私有解释' } });
    render(<FactorList metrics={metrics} onBacktestFactor={onBacktestFactor} />);

    fireEvent.click(screen.getByTitle('回测此因子'));
    expect(onBacktestFactor).toHaveBeenCalledWith('owner-factor-1');

    fireEvent.click(screen.getByTitle('查看因子详情与解读'));
    fireEvent.click(screen.getByText('AI 解读因子'));
    await screen.findByText('本人私有解释');
    expect(localStorage.getItem('alpha_explain_owner-factor-1')).toBeNull();

    act(() => invalidateSession());
    await waitFor(() => expect(screen.queryByText('本人私有解释')).toBeNull());
  });
});
