/**
 * ChatInput 回填（挖掘历史「重跑」的落点）—— T-FM-04。
 *
 * 历史页点「重跑」把当时的 方向/市场/数据源 回填进输入框，用户可改再发。
 * 两条容易被写坏的边：
 *
 * 1. **key 变化必须重新应用**：连着重跑两个「同方向同市场」的任务时，
 *    若 effect 只依赖 config 对象内容，第二次回填会被 React 判成无变化而跳过
 *    （用户上次手动改过的选项不会被复原）——所以回填带独立的 key。
 * 2. **initialPrompt 为空不能清空输入框**：legacy 行方向为空是常态，
 *    不能因此把用户正打了一半的字擦掉。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ChatInput } from '../ChatInput';

const { listMarketsMock } = vi.hoisted(() => ({ listMarketsMock: vi.fn() }));

vi.mock('../../services/alphaAgentService', () => ({
  alphaAgentService: { listMarkets: listMarketsMock },
}));

vi.mock('../../services-v2/api', () => ({
  getUniverses: vi.fn().mockResolvedValue({ data: { universes: [] } }),
}));

beforeEach(() => {
  listMarketsMock.mockReset();
  listMarketsMock.mockResolvedValue([
    { market_id: 'a_share', market_name: 'A股', data_ready: true },
    { market_id: 'us_stock', market_name: '美股', data_ready: true },
  ]);
});

function typeAndSubmit() {
  fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
}

describe('ChatInput 重跑回填', () => {
  test('方向进输入框、市场与数据源进选择器，提交时全部带上', async () => {
    const onSubmit = vi.fn();
    render(
      <ChatInput
        onSubmit={onSubmit}
        initialPrompt="尾盘资金流背离"
        initialConfig={{ miningMarket: 'us_stock', dataSource: 'parquet' }}
        initialConfigKey={1}
      />,
    );

    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe(
      '尾盘资金流背离',
    );
    typeAndSubmit();

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        userInput: '尾盘资金流背离',
        miningMarket: 'us_stock',
        dataSource: 'parquet',
      }),
    );
  });

  test('key 前进时重新应用：用户手改过的数据源被回填复原', async () => {
    const onSubmit = vi.fn();
    const config = { miningMarket: 'us_stock', dataSource: 'parquet' as const };
    const { rerender } = render(
      <ChatInput onSubmit={onSubmit} initialConfig={config} initialConfigKey={1} />,
    );

    // 用户手动把它改回 Qlib（qlib_bin）
    fireEvent.click(screen.getByText('Qlib'));
    typeAndSubmit();
    expect(onSubmit.mock.calls.at(-1)?.[0]).toEqual(
      expect.objectContaining({ dataSource: 'qlib_bin' }),
    );

    // 再点一次「重跑」（同一个 config，key 前进）：回填必须重新生效
    rerender(<ChatInput onSubmit={onSubmit} initialConfig={config} initialConfigKey={2} />);
    typeAndSubmit();
    expect(onSubmit.mock.calls.at(-1)?.[0]).toEqual(
      expect.objectContaining({ dataSource: 'parquet' }),
    );
  });

  test('initialPrompt 为空（legacy 无方向）不清空用户已输入的内容', async () => {
    const { rerender } = render(<ChatInput onSubmit={vi.fn()} initialConfigKey={1} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '我自己写的方向' } });

    rerender(<ChatInput onSubmit={vi.fn()} initialPrompt="" initialConfigKey={2} />);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe(
      '我自己写的方向',
    );
  });
});
