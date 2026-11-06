// @vitest-environment jsdom
import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ listUserModels: vi.fn(), listReviewRuns: vi.fn(), getInferenceReview: vi.fn() }));
vi.mock('react-redux', () => ({ useSelector: (select: (state: unknown) => unknown) => select({ auth: { user: { id: 1, tenant_id: 't1' } } }) }));
vi.mock('../../services/marketService', () => ({ marketService: { getTencentMarketData: vi.fn().mockResolvedValue({ success: false }) } }));
vi.mock('echarts-for-react', () => ({ default: () => <div data-testid="chart" /> }));
vi.mock('../../services/modelTrainingService', () => ({ modelTrainingService: api }));
import ReviewCenterPage from './ReviewCenterPage';

afterEach(() => { cleanup(); vi.useRealTimers(); vi.clearAllMocks(); });

it('keeps the current view while refreshing in the background', async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-11-06T08:00:00Z'));
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  api.listUserModels.mockResolvedValue({ items: [{ model_id: 'model', metadata_json: {} }] });
  api.listReviewRuns.mockResolvedValue({ items: [{ run_id: 'run', data_trade_date: '2026-11-05', prediction_trade_date: '2026-11-06' }], has_more: false });
  const first = { stocks: [], days: [{ date: '2026-11-06', cumulative: 1, benchmark: 0, coverage: 0, source: '日线' }], quote_coverage: 0, unknown_st: 0, message: '' };
  let finish: (value: unknown) => void = () => {};
  api.getInferenceReview.mockResolvedValueOnce(first).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  await act(async () => { render(<ReviewCenterPage />); });
  await act(async () => {});
  expect(screen.getAllByText('+1.00%').length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole('button', { name: '每日表现' }));
  await act(async () => { vi.advanceTimersByTime(60000); });
  expect(api.getInferenceReview).toHaveBeenCalledTimes(2);
  expect(screen.queryByText('正在加载复盘数据…')).toBeNull();
  expect(screen.getByText('交易日期')).toBeTruthy();
  await act(async () => { finish({ ...first, days: [{ ...first.days[0], cumulative: 2 }] }); });
  expect(screen.getAllByText('+2.00%').length).toBeGreaterThan(0);
  expect(screen.getByText('交易日期')).toBeTruthy();
});

