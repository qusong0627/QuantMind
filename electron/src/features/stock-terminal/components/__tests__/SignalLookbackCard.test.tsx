/**
 * 信号准确率回看卡 · 渲染纪律测试。
 *
 * 盯四件「渲染错了也不会报错、但会骗人」的事：
 * 1. 缺的数值必须显示 `—`，不能显示成 `0.00%`（0 涨幅是事实主张）；
 * 2. 一个回看点都算不出价差时，必须明说算不出来，不能渲染一张全 `—` 的汇总表；
 * 3. 表头必须写出价格来源与取数日，否则涨跌是拿哪个价算的没人知道；
 * 4. **默认必须是折叠的、且折叠条说得清自己是什么** —— 上一版默认展开、入口只有一个
 *    12px 灰图标，用户「差点没发现」这个块。这条测试就是防止它退回去。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const getSignalLookback = vi.fn();
vi.mock('../../services/stockTerminalService', () => ({
  stockTerminalService: { getSignalLookback: (...a: unknown[]) => getSignalLookback(...a) },
}));

import SignalLookbackCard from '../SignalLookbackCard';
import type { LookbackSummaryRow, SignalLookbackData } from '../../lookbackModel';

const HEIGHT_KEY = 'qm:stock-terminal:lookback:bodyH';

const row = (over: Partial<LookbackSummaryRow> = {}): LookbackSummaryRow => ({
  lookback: 3,
  label: 'T-3',
  signal_date: '2026-09-16',
  base_price_date: '2026-09-16',
  run_id: 'run_20260915_085d80c4',
  model_version: 'inference_script',
  comparable: true,
  sample: 3271,
  n_hi: 655,
  n_lo: 655,
  n_neg: 943,
  missing_price: 0,
  score_std: 0.1466,
  hi_avg: 0.0123,
  lo_avg: 0.0114,
  spread: 0.001,
  hi_hit: 0.647,
  lo_hit: 0.304,
  neg_avg: 0.0119,
  avg_score_hi: 0.258,
  avg_score_lo: -0.148,
  ...over,
});

const payload = (over: Partial<SignalLookbackData> = {}): SignalLookbackData => ({
  status: 'ok',
  as_of: '2026-09-21',
  price_as_of: '2026-09-18',
  price_source: 'close',
  live_count: 0,
  close_count: 3271,
  comparable: true,
  bucket_pct: 0.2,
  lookbacks: [3, 5, 10],
  summary: [row()],
  detail: {
    total: 1,
    page: 1,
    page_size: 50,
    items: [
      {
        symbol: '600606.SH',
        name: '绿地控股',
        score_now: 0.938,
        rank_now: 1,
        side_now: 'BUY',
        points: [
          {
            lookback: 3,
            signal_date: '2026-09-16',
            score: 0.1448,
            rank: 1127,
            rank_pct: 0.6557,
            day_n: 3271,
            ret: 0.1328125,
            price_source: 'close',
          },
          {
            lookback: 5,
            signal_date: '2026-09-14',
            score: -0.3414,
            rank: 3153,
            rank_pct: 0.036,
            day_n: 3271,
            ret: null, // 缺价 —— 必须显示 —
            price_source: 'close',
          },
        ],
      },
    ],
  },
  ...over,
});

/** 渲染并展开（默认是折叠的，断言表内容前必须先展开）。 */
function renderExpanded() {
  const r = render(<SignalLookbackCard />);
  fireEvent.click(screen.getByTitle('展开信号准确率回看'));
  return r;
}

beforeEach(() => {
  getSignalLookback.mockReset();
  window.localStorage.clear();
});

describe('SignalLookbackCard · 折叠与发现性', () => {
  it('默认折叠：不渲染表格，但折叠条说清它是什么、能展开', async () => {
    getSignalLookback.mockResolvedValue(payload());
    render(<SignalLookbackCard />);
    // 表格没渲染 = 真的折叠了（不是靠 CSS 藏起来）
    expect(screen.queryByText('高分档均涨')).toBeNull();
    expect(screen.queryByText('逐只明细')).toBeNull();
    // 折叠条本身要显眼到能被发现：标题 + 它回答什么问题 + 展开按钮
    expect(screen.getByText('信号准确率回看')).toBeTruthy();
    expect(screen.getByText(/给高分的票后来涨了还是跌了/)).toBeTruthy();
    expect(screen.getByTitle('展开信号准确率回看')).toBeTruthy();
    await waitFor(() => expect(screen.getByText('锚点 2026-09-21')).toBeTruthy());
  });

  it('点展开后渲染汇总与明细，点折叠后收回', async () => {
    getSignalLookback.mockResolvedValue(payload());
    const { container } = renderExpanded();
    await waitFor(() => expect(screen.getByText('高分档均涨')).toBeTruthy());
    expect(screen.getByText('逐只明细')).toBeTruthy();
    fireEvent.click(screen.getByTitle('折叠'));
    expect(container.querySelector('table')).toBeNull();
    expect(screen.getByTitle('展开信号准确率回看')).toBeTruthy();
  });

  it('展开后底边有拖动手柄，拖动改高度并记住', async () => {
    getSignalLookback.mockResolvedValue(payload());
    renderExpanded();
    await waitFor(() => expect(screen.getByText('高分档均涨')).toBeTruthy());
    const handle = screen.getByTitle('拖动调整高度');
    fireEvent.mouseDown(handle, { clientY: 500 });
    fireEvent.mouseMove(window, { clientY: 620 });
    fireEvent.mouseUp(window);
    // 记的是绝对高度，故只断言「变了且写进去了」
    const saved = Number(window.localStorage.getItem(HEIGHT_KEY));
    expect(Number.isFinite(saved)).toBe(true);
    expect(saved).toBeGreaterThan(240);
  });
});

describe('SignalLookbackCard · 呈现纪律', () => {
  it('表头写出价格来源与取数日（不写就没人知道涨跌是拿哪个价算的）', async () => {
    getSignalLookback.mockResolvedValue(payload());
    renderExpanded();
    // 表头徽章与脚注都会写来源，故用 getAllByText
    await waitFor(() => expect(screen.getAllByText(/收盘价 · 3271 只/).length).toBeGreaterThan(0));
    expect(screen.getByText('锚点 2026-09-21')).toBeTruthy();
  });

  it('明细里缺失的涨跌显示 --，不显示 0.00%', async () => {
    getSignalLookback.mockResolvedValue(payload());
    renderExpanded();
    // 绿地控股 T-5 的 ret 是 null，且它是全表唯一的缺失值
    await waitFor(() => expect(screen.getByText('绿地控股')).toBeTruthy());
    expect(screen.getAllByText('--')).toHaveLength(1); // PctText 的缺失占位
    expect(screen.queryByText('+0.00%')).toBeNull();
    // T-3 有值则照常显示（PctText 把符号/数值/后缀拆成多节点，按整串匹配）
    expect(screen.getByText('+13.28%')).toBeTruthy();
  });

  it('分数缺失显示 —，不显示 0.000', async () => {
    const p = payload();
    p.detail!.items[0].points[0].score = null;
    p.detail!.items[0].points[0].rank = null;
    getSignalLookback.mockResolvedValue(p);
    renderExpanded();
    await waitFor(() => expect(screen.getByText('绿地控股')).toBeTruthy());
    expect(screen.queryByText('0.000')).toBeNull();
  });

  it('所有回看点都算不出价差时明说算不出来，不渲染全 — 的汇总表', async () => {
    getSignalLookback.mockResolvedValue(payload({ summary: [row({ spread: null, hi_avg: null, lo_avg: null })] }));
    renderExpanded();
    await waitFor(() => expect(screen.getByText(/算不出涨跌/)).toBeTruthy());
    // 没有渲染汇总表头
    expect(screen.queryByText('高分档均涨')).toBeNull();
  });

  it('价差为 0 是真实结论，照常渲染（不能被当成「算不出来」）', async () => {
    getSignalLookback.mockResolvedValue(payload({ summary: [row({ spread: 0 })] }));
    renderExpanded();
    await waitFor(() => expect(screen.getByText('高分档均涨')).toBeTruthy());
    expect(screen.queryByText(/算不出涨跌/)).toBeNull();
    expect(screen.getByText('0.00pp')).toBeTruthy();
  });

  it('行不可比时给出三角警示与说明，但涨跌照常显示', async () => {
    getSignalLookback.mockResolvedValue(
      payload({ summary: [row({ comparable: false, spread: -0.0205, hi_avg: -0.0191 })] }),
    );
    renderExpanded();
    await waitFor(() => expect(screen.getByText(/另一套分数尺/)).toBeTruthy());
  });

  it('后端说覆盖不足时，原样转述原因而不是显示空表', async () => {
    getSignalLookback.mockResolvedValue({
      status: 'unavailable',
      reason: '最近 180 天内没有覆盖充分的信号日',
    });
    renderExpanded();
    await waitFor(() => expect(screen.getByText(/没有覆盖充分的信号日/)).toBeTruthy());
  });

  it('取数失败时给出错误与重试，不静默留白', async () => {
    getSignalLookback.mockRejectedValue(new Error('boom'));
    renderExpanded();
    await waitFor(() => expect(screen.getByText('boom')).toBeTruthy());
    expect(screen.getByText('重试')).toBeTruthy();
  });

  it('请求带上 side/model 联动参数', async () => {
    getSignalLookback.mockResolvedValue(payload());
    render(<SignalLookbackCard side="BUY" model="mdl_x" />);
    await waitFor(() => expect(getSignalLookback).toHaveBeenCalled());
    expect(getSignalLookback.mock.calls[0][0]).toMatchObject({
      lookbacks: '3,5,10',
      side: 'BUY',
      model: 'mdl_x',
    });
  });
});
