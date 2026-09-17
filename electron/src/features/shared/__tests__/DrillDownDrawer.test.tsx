/**
 * 下钻容器（T-FE-03 v2）：逐层穿透——条目 drill 进入下一层、面包屑返回、载荷随层切换、
 * 关闭/换根复位。UI 行为验收对应设计 §一.4「处处可下钻」。
 *
 * v3：新增 presentation=modal（居中弹窗，交易台）——原始载荷默认折叠；
 * drawer（默认，个股终端沿用）保持展开的既有行为。
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
    // 根层（drawer 形态：载荷保持展开）
    expect(screen.getByText('总资产')).toBeTruthy();
    expect(screen.getByText(/"level": 1/)).toBeTruthy();

    await user.click(screen.getByRole('button', { name: /累计收益/ }));
    // L2：标题、条目与原始载荷切换
    expect(screen.getByRole('heading', { name: '快照 · 2026-09-16' })).toBeTruthy();
    expect(screen.getByText('total_asset')).toBeTruthy();
    expect(screen.getByText(/"level": 2/)).toBeTruthy();
    expect(screen.queryByText('总资产')).toBeNull();

    // L3：继续穿透
    await user.click(screen.getByRole('button', { name: /原始行/ }));
    expect(screen.getByRole('heading', { name: '快照原始载荷' })).toBeTruthy();
    expect(screen.getByText(/"level": 3/)).toBeTruthy();

    // 返回上一层 → 回到 L2
    await user.click(screen.getByRole('button', { name: /返回上一层/ }));
    expect(screen.getByRole('heading', { name: '快照 · 2026-09-16' })).toBeTruthy();

    // 面包屑点击根标题 → 回到根层
    await user.click(screen.getByRole('button', { name: /账户盈亏 · 来源链/ }));
    expect(screen.getByText('总资产')).toBeTruthy();
    expect(screen.getByText(/"level": 1/)).toBeTruthy();
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

  it('presentation=modal：居中弹窗形态，标题/条目语义保留、载荷默认折叠可展开', async () => {
    const user = userEvent.setup();
    render(
      <DrillDownDrawer
        open
        presentation="modal"
        title="信号 · 600036"
        entries={[{ label: '标的', value: '600036' }]}
        raw={{ level: 9 }}
        onClose={() => {}}
      />
    );
    expect(document.querySelector('.ant-modal')).toBeTruthy();
    expect(screen.getByRole('heading', { name: '信号 · 600036' })).toBeTruthy();
    expect(screen.getByText('标的')).toBeTruthy();
    // modal 形态：载荷默认折叠 → 展开后可见
    expect(screen.queryByText(/"level": 9/)).toBeNull();
    await user.click(screen.getByRole('button', { name: /原始载荷（核对用）/ }));
    expect(screen.getByText(/"level": 9/)).toBeTruthy();
  });
});
