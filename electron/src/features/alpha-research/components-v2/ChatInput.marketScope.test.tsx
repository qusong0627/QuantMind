import React from 'react';
import { Provider } from 'react-redux';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import store from '../../../store';
import { getUniverses } from '../services-v2/api';
import { getMiningAlgorithms, getMiningDatasets } from '../services-v2/algorithmApi';
import { ChatInput } from './ChatInput';

vi.mock('../services-v2/api', () => ({
  getUniverses: vi.fn().mockResolvedValue({ data: { universes: [] } }),
}));
vi.mock('../services-v2/algorithmApi', () => ({
  getMiningAlgorithms: vi.fn().mockResolvedValue({ data: { available: true, algorithms: [
    { key: 'rdagent', name: 'RD-Agent', uses_datasets: true },
    { key: 'statistical', name: '统计派' },
  ] } }),
  getMiningDatasets: vi.fn().mockResolvedValue({ data: { available: true, datasets: [
    { key: 'features_daily', name: '日线因子宽表', markets: ['a_share'], freqs: ['day'] },
    { key: 'l1_factors', name: 'L1 因子库', markets: ['a_share'], freqs: ['day'] },
  ] } }),
}));

const previewOk = async () => ({
  algorithm: { key: 'rdagent', name: 'RD-Agent', version: '1.0', uses_llm: true, uses_datasets: true },
  market: 'a_share', freq: 'day', universe: 'csi300', data_source: 'qlib_bin', loop_n: 3,
  direction: '', directions: [], direction_mode: 'selected' as const,
  datasets: { features_daily: '日线因子宽表', l1_factors: 'L1 因子库' },
  window: { mode: 'server_default' as const }, requires_llm: true,
});

async function confirmPreview() {
  fireEvent.click(await screen.findByRole('button', { name: '确认并开始' }));
}

describe('factor mining market scope', () => {
  beforeEach(() => {
    store.dispatch({ type: 'auth/login/fulfilled', payload: {
      require_mfa: false,
      user: { user_id: 'chat-input-test' },
      token: 'test-token',
      refreshToken: null,
      tokenExpiryTime: null,
    } });
  });

  afterEach(cleanup);

  it('defaults the mining stock pool to all A-shares', async () => {
    render(<Provider store={store}><ChatInput onPreview={previewOk} onSubmit={vi.fn()} /></Provider>);
    expect(screen.getByLabelText('选择因子挖掘的股票池')).toHaveProperty('value', 'all_a');
    expect(screen.getByRole('option', { name: '全部A股' })).toBeTruthy();
  });

  it('submits only A-share mining requests', async () => {
    const onSubmit = vi.fn();
    render(<Provider store={store}><ChatInput onPreview={previewOk} onSubmit={onSubmit} /></Provider>);
    await waitFor(() => expect(getUniverses).toHaveBeenCalled());
    await waitFor(() => expect(getMiningAlgorithms).toHaveBeenCalled());
    await waitFor(() => expect(getMiningDatasets).toHaveBeenCalled());

    expect(screen.getByText('A股')).toBeTruthy();
    expect(screen.queryByText('加密货币')).toBeNull();
    expect(screen.queryByText('港股')).toBeNull();
    expect(screen.queryByText('美股')).toBeNull();
    expect(screen.queryByText('期货')).toBeNull();

    fireEvent.change(screen.getByRole('textbox'), { target: { value: '测试A股因子' } });
    fireEvent.click(screen.getByTitle('发送 (Enter)'));
    await confirmPreview();
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      userInput: '测试A股因子',
      algorithm: 'rdagent',
      miningMarket: 'a_share',
      datasets: ['features_daily', 'l1_factors'],
      freq: 'day',
    }));
  });

  it('shows the server preview and submits only after explicit confirmation', async () => {
    const onSubmit = vi.fn();
    const onPreview = vi.fn().mockResolvedValue({
      algorithm: { key: 'rdagent', name: 'RD-Agent', version: '1.0', uses_llm: true, uses_datasets: true },
      market: 'a_share', freq: 'day', universe: 'csi300', data_source: 'qlib_bin', loop_n: 3,
      direction: '预览后提交', directions: [], direction_mode: 'selected',
      datasets: { l1_factors: 'L1 因子库' },
      window: { mode: 'server_default' }, requires_llm: true,
    });
    render(<Provider store={store}><ChatInput onPreview={onPreview} onSubmit={onSubmit} /></Provider>);
    await waitFor(() => expect(getMiningAlgorithms).toHaveBeenCalled());
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '预览后提交' } });
    fireEvent.click(screen.getByTitle('发送 (Enter)'));

    await screen.findByRole('dialog', { name: '挖掘参数预览' });
    expect(onPreview).toHaveBeenCalledWith(expect.objectContaining({
      userInput: '预览后提交', algorithm: 'rdagent', miningMarket: 'a_share', freq: 'day',
    }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(within(screen.getByRole('dialog')).getByText('L1 因子库')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '确认并开始' }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ userInput: '预览后提交' }));
  });

  it('submits the selected registered datasets for daily RD-Agent mining', async () => {
    const onSubmit = vi.fn();
    render(<Provider store={store}><ChatInput onPreview={previewOk} onSubmit={onSubmit} /></Provider>);
    await waitFor(() => expect(getMiningDatasets).toHaveBeenCalled());
    fireEvent.click(screen.getByLabelText('数据集 L1 因子库'));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '测试数据集选择' } });
    fireEvent.click(screen.getByTitle('发送 (Enter)'));
    await confirmPreview();
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      datasets: ['features_daily'],
      freq: 'day',
    }));
  });

  it('submits the selected RD-Agent mining and out-of-sample windows', async () => {
    const onSubmit = vi.fn();
    render(<Provider store={store}><ChatInput onPreview={previewOk} onSubmit={onSubmit} /></Provider>);
    await waitFor(() => expect(getMiningDatasets).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('RD-Agent挖掘开始日期'), { target: { value: '2020-01-01' } });
    fireEvent.change(screen.getByLabelText('RD-Agent挖掘结束日期'), { target: { value: '2022-12-31' } });
    fireEvent.change(screen.getByLabelText('RD-Agent验证开始日期'), { target: { value: '2023-01-01' } });
    fireEvent.change(screen.getByLabelText('RD-Agent验证结束日期'), { target: { value: '2023-12-31' } });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '验证日期窗口' } });
    fireEvent.click(screen.getByTitle('发送 (Enter)'));
    await confirmPreview();
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      miningStart: '2020-01-01',
      miningEnd: '2022-12-31',
      validStart: '2023-01-01',
      validEnd: '2023-12-31',
    }));
  });

  it('requires a valid date window and submits statistical mining on Qlib only', async () => {
    const onSubmit = vi.fn();
    render(<Provider store={store}><ChatInput onPreview={previewOk} onSubmit={onSubmit} /></Provider>);
    await waitFor(() => expect(getUniverses).toHaveBeenCalled());
    await waitFor(() => expect(getMiningAlgorithms).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: '统计派' }));
    const submit = screen.getByTitle('发送 (Enter)');
    expect(submit).toHaveProperty('disabled', true);
    expect(screen.queryByRole('button', { name: 'Parquet' })).toBeNull();
    expect(screen.getByRole('textbox')).toHaveProperty('disabled', true);

    fireEvent.change(screen.getByLabelText('统计挖掘开始日期'), { target: { value: '2020-01-01' } });
    fireEvent.change(screen.getByLabelText('统计挖掘结束日期'), { target: { value: '2022-12-31' } });
    expect(submit).toHaveProperty('disabled', false);
    fireEvent.click(submit);
    await confirmPreview();

    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      algorithm: 'statistical',
      miningStart: '2020-01-01',
      miningEnd: '2022-12-31',
      validStart: undefined,
      validEnd: undefined,
      miningMarket: 'a_share',
      dataSource: 'qlib_bin',
      userInput: '',
    }));
  });

  it('fails closed when the server capability directory is unavailable', async () => {
    vi.mocked(getMiningAlgorithms).mockResolvedValueOnce({
      success: true,
      data: { algorithms: [], available: false },
    });
    const onSubmit = vi.fn();
    render(<Provider store={store}><ChatInput onPreview={previewOk} onSubmit={onSubmit} /></Provider>);

    await waitFor(() => expect(getMiningAlgorithms).toHaveBeenCalled());
    expect(screen.getByRole('status').textContent).toContain('算法能力暂不可用');
    expect(screen.getByTitle('发送 (Enter)')).toHaveProperty('disabled', true);
    fireEvent.click(screen.getByTitle('发送 (Enter)'));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('rejects reversed or incomplete statistical date windows', async () => {
    const onSubmit = vi.fn();
    render(<Provider store={store}><ChatInput onPreview={previewOk} onSubmit={onSubmit} /></Provider>);
    await waitFor(() => expect(getUniverses).toHaveBeenCalled());
    await waitFor(() => expect(getMiningAlgorithms).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: '统计派' }));
    const submit = screen.getByTitle('发送 (Enter)');

    fireEvent.change(screen.getByLabelText('统计挖掘开始日期'), { target: { value: '2022-01-01' } });
    fireEvent.change(screen.getByLabelText('统计挖掘结束日期'), { target: { value: '2020-12-31' } });
    expect(submit).toHaveProperty('disabled', true);
    fireEvent.change(screen.getByLabelText('统计挖掘结束日期'), { target: { value: '2023-12-31' } });
    fireEvent.change(screen.getByLabelText('统计验证开始日期'), { target: { value: '2024-01-01' } });
    expect(submit).toHaveProperty('disabled', true);
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
