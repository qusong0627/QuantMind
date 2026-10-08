/**
 * `TopBar` 顶栏槽位护栏（2026-10-08 加「市场切换器」槽位时补，此前本组件零测试）。
 *
 * 钉两件事：
 * 1. **零扩展时不多一个节点** —— 公开树（模拟交易）与本机制引入前逐位相同，
 *    这条承诺靠 `{undefined}` 不产生 DOM 兑现，改错了不会有人发现；
 * 2. 注入节点落在**右侧状态徽章那一行**（与「行情已连接」「策略运行中」同簇）——
 *    挂到左簇/卡片区/顶栏外面，用户看到的就不是「顶部右侧的市场切换」了。
 */

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import React from 'react';

import TopBar from '../TopBar';

const baseProps = { isConnected: true, strategyStatus: 'running' as const };

describe('TopBar 顶栏槽位', () => {
    it('零扩展：资产概览照常渲染，不产生额外节点', () => {
        render(<TopBar {...baseProps} />);

        expect(document.body.textContent).toContain('资产概览');
        expect(document.querySelector('[data-testid="topbar-extras-probe"]')).toBeNull();
    });

    it('topBarExtras：注入节点与状态徽章同一行（右簇）', () => {
        render(
            <TopBar
                {...baseProps}
                topBarExtras={<div data-testid="topbar-extras-probe">市场切换</div>}
            />,
        );

        const probe = document.querySelector('[data-testid="topbar-extras-probe"]');
        expect(probe).not.toBeNull();

        // 同父 = 同一行同一簇：父节点里是三个右簇元素，不含左簇的「资产概览」
        const row = probe!.parentElement;
        expect(row?.textContent).toContain('市场切换');
        expect(row?.textContent).toContain('行情已连接');
        expect(row?.textContent).toContain('策略运行中');
        expect(row?.textContent).not.toContain('资产概览');
    });

    it('顶栏自身状态照常：未连接 + 策略停止时的文案不被槽位影响', () => {
        render(
            <TopBar
                isConnected={false}
                strategyStatus="stopped"
                topBarExtras={<span data-testid="topbar-extras-probe" />}
            />,
        );

        expect(document.body.textContent).toContain('未连接');
        expect(document.body.textContent).toContain('策略已停止');
        expect(document.querySelector('[data-testid="topbar-extras-probe"]')).not.toBeNull();
    });
});
