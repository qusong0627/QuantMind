/**
 * 下钻容器（T-FE-03 v2）：逐层穿透——条目 drill 进入下一层、面包屑返回、载荷随层切换、
 * 关闭/换根复位。UI 行为验收对应设计 §一.4「处处可下钻」。
 *
 * v3：原始载荷默认折叠（核对时展开）；presentation=modal 为居中弹窗形态（交易台）。
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DrillDownDrawer, type DrillEntry } from '../DrillDownDrawer';

const makeEntries = (): DrillEntry[] => [
  { label: '总资产', value: '¥1,000,000' },
  {
    label: '累计收益',
    value: '¥12,345',
    hint: '收益率 1.23%',
    drill: {
      title: '快照 · 2026-09-16',
      subtitle: '资金快照行原样',
      entries: [
        { label: 'total_asset', value: '1000000' },
        {
          label: '原始行',
          value: '展开',
          drill: {
            title: '快照原始载荷',
            entries: [{ label: 'row', value: 'ok' }],
            raw: { level: 3 },
          },
        },
      ],
      raw: { level: 2 },
    },
  },
];

describe('DrillDownDrawer 逐层穿透（T-FE-03 v2）', () => {
  it('点击可下钻条目进入下一层；面包屑与原始载荷随层切换', async () => {
    const user = userEvent.setup();
    render(
      <DrillDownDrawer open title="账户盈亏 · 来源链" entries={makeEntries()} raw={{ level: 1 }} onClose={() => {}} />
    );
    // 根层：条目可见，原始载荷默认折叠
    expect(screen.getByText('总资产')).toBeTruthy();
    expect(screen.queryByText(/"level": 1/)).toBeNull();
    await user.click(screen.getByRole('button', { name: /原始载荷/ }));
    expect(screen.getByText(/"level": 1/)).toBeTruthy();

    // L2：标题、条目与原始载荷切换（换层后载荷收起，再展开）
    await user.click(screen.getByRole('button', { name: /累计收益/ }));
    expect(screen.getByRole('heading', { name: '快照 · 2026-09-16' })).toBeTruthy();
    expect(screen.getByText('total_asset')).toBeTruthy();
    expect(screen.queryByText(/"level": 2/)).toBeNull();
    await user.click(screen.getByRole('button', { name: /原始载荷/ }));
    expect(screen.getByText(/"level": 2/)).toBeTruthy();
    expect(screen.queryByText('总资产')).toBeNull();

    // L3：继续穿透
    await user.click(screen.getByRole('button', { name: /原始行/ }));
    expect(screen.getByRole('heading', { name: '快照原始载荷' })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: /原始载荷（核对用）/ }));
    expect(screen.getByText(/"level": 3/)).toBeTruthy();

    // 返回上一层 → 回到 L2
    await user.click(screen.getByRole('button', { name: /返回上一层/ }));
    expect(screen.getByRole('heading', { name: '快照 · 2026-09-16' })).toBeTruthy();

    // 面包屑点击根标题 → 回到根层
    await user.click(screen.getByRole('button', { name: /账户盈亏 · 来源链/ }));
    expect(screen.getByText('总资产')).toBeTruthy();
  });

  it('不可下钻条目渲染为静态块（无按钮语义）', () => {
    render(
      <DrillDownDrawer
        open
        title="t"
        entries={[{ label: '总资产', value: '¥1' }]}
        raw={{}}
        onClose={() => {}}
      />
    );
    expect(screen.queryByRole('button', { name: /总资产/ })).toBeNull();
    expect(screen.getByText('总资产')).toBeTruthy();
  });

  it('presentation=modal：居中弹窗形态渲染，标题/条目语义保留', () => {
    render(
      <DrillDownDrawer
        open
        presentation="modal"
        title="信号 · 600036"
        entries={[{ label: '标的', value: '600036' }]}
        raw={{}}
        onClose={() => {}}
      />
    );
    expect(document.querySelector('.ant-modal')).toBeTruthy();
    expect(screen.getByRole('heading', { name: '信号 · 600036' })).toBeTruthy();
    expect(screen.getByText('标的')).toBeTruthy();
  });
});
