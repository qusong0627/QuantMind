import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { DEFAULT_STRATEGY_EDITOR_PARAMS, type StrategyEditorBacktestParams } from '../../../../utils/strategyEditorParams';
import BacktestSettingsPanel from '../BacktestSettingsPanel';
import type { ParamsState } from '../types';

const makeParams = (
  overrides: Partial<StrategyEditorBacktestParams> = {},
): ParamsState => ({
  value: { ...DEFAULT_STRATEGY_EDITOR_PARAMS, ...overrides },
  onChange: vi.fn(),
  onApply: vi.fn(),
  onApplyAndRun: vi.fn(),
  market: 'CN',
});

const MINIBT_CODE = 'from minibt import Bt\nSYMBOL = "600036"\n';
const CONFIG_CODE = 'STRATEGY_CONFIG = {"class": "X", "kwargs": {"topk": 50}}\n';

const renderPanel = (params: ParamsState, code: string) =>
  render(
    <BacktestSettingsPanel params={params} code={code} collapsed={false} onCollapsedChange={vi.fn()} />,
  );

describe('BacktestSettingsPanel', () => {
  it('minibt 代码只显示均线组，不显示 STRATEGY_CONFIG 的 topk 组', () => {
    const params = makeParams();
    renderPanel(params, MINIBT_CODE);
    expect(screen.getByText('minibt')).toBeInTheDocument();
    expect(screen.getByText('均线信号')).toBeInTheDocument();
    expect(screen.queryByText('组合构建')).not.toBeInTheDocument();
    expect(screen.getByLabelText('快线 fast')).toBeInTheDocument();
  });

  it('STRATEGY_CONFIG 代码显示组合构建，不显示均线组', () => {
    const params = makeParams();
    renderPanel(params, CONFIG_CODE);
    expect(screen.getByText('Qlib 配置')).toBeInTheDocument();
    expect(screen.getByText('组合构建')).toBeInTheDocument();
    expect(screen.queryByText('均线信号')).not.toBeInTheDocument();
  });

  it('未识别引擎明确提示「应用不会改动代码」', () => {
    const params = makeParams();
    renderPanel(params, 'print("hello")\n');
    expect(screen.getByText('未识别')).toBeInTheDocument();
    expect(screen.getByText(/不会改动代码/)).toBeInTheDocument();
  });

  it('修改变量时把 patch 交给上层（不直接改代码）', () => {
    const params = makeParams();
    renderPanel(params, MINIBT_CODE);
    const symbolInput = screen.getByDisplayValue('600036');
    fireEvent.change(symbolInput, { target: { value: '000001' } });
    expect(params.onChange).toHaveBeenCalledWith({ symbol: '000001' });
  });

  it('fast >= slow 时拦下并给出原因，主按钮禁用', () => {
    const params = makeParams({ fast: 30, slow: 20 });
    renderPanel(params, MINIBT_CODE);
    expect(screen.getByText('快线必须小于慢线（fast < slow）')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '应用参数' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '应用并运行' })).toBeDisabled();
  });

  it('日期倒置时拦下', () => {
    const params = makeParams({ startDate: '2025-01-01', endDate: '2024-01-01' });
    renderPanel(params, CONFIG_CODE);
    expect(screen.getByText('结束日期必须晚于开始日期')).toBeInTheDocument();
  });

  it('合法参数下点「应用参数」/「应用并运行」触发对应回调', () => {
    const params = makeParams();
    renderPanel(params, MINIBT_CODE);
    fireEvent.click(screen.getByRole('button', { name: '应用参数' }));
    expect(params.onApply).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: '应用并运行' }));
    expect(params.onApplyAndRun).toHaveBeenCalledTimes(1);
  });
});
