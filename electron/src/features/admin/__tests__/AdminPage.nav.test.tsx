/**
 * 后台管理导航骨架的接线护栏（2026-11-18 评估中心迁入）。
 *
 * 用户可见约定：
 * 1. 「交易核心」组内的顺序是 订单管理 → 风险控制 → **评估中心**（用户原话
 *    「风险控制后面」），评估中心不得缺席、不得排到风险控制之前；
 * 2. 评估中心在后台 shell 右侧内容区真的挂载（路由 `eval` 有 Outlet 出口）。
 *
 * 这两条任何一条被改回交易控制台或改错位置，本文件都要红。
 */

import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AdminPage from '../AdminPage';

// 外壳轮询负载/更新版本都是副作用，测试里不打网络
vi.mock('../services/adminService', () => ({
    adminService: { getSystemLoad: vi.fn().mockResolvedValue({ workload: null }) },
}));
vi.mock('../../../hooks/useSystemUpdateCheck', () => ({
    useSystemUpdateCheck: () => ({ update: null, isFetching: false, refresh: vi.fn() }),
}));

const renderAdmin = (path: string) =>
    render(
        <MemoryRouter initialEntries={[path]}>
            <Routes>
                <Route path="/admin" element={<AdminPage />}>
                    <Route path="eval" element={<div data-testid="pane-eval" />} />
                    <Route path="risk" element={<div data-testid="pane-risk" />} />
                </Route>
            </Routes>
        </MemoryRouter>,
    );

/** 侧栏按钮文案（顺序即渲染顺序） */
const navLabels = (): string[] =>
    Array.from(document.querySelectorAll('nav.admin-dark-scrollbar button')).map((b) =>
        (b.textContent || '').trim(),
    );

describe('后台管理导航（交易核心）', () => {
    it('交易核心组：订单管理 → 风险控制 → 评估中心', () => {
        renderAdmin('/admin/risk');
        const labels = navLabels();

        const orders = labels.indexOf('订单管理');
        const risk = labels.indexOf('风险控制');
        const evalIdx = labels.indexOf('评估中心');

        expect(orders).toBeGreaterThanOrEqual(0);
        expect(risk).toBeGreaterThan(orders);
        expect(evalIdx).toBeGreaterThan(risk);
    });

    it('/admin/eval 渲染评估中心面板（不是空白内容区）', () => {
        renderAdmin('/admin/eval');

        expect(document.querySelector('[data-testid="pane-eval"]')).not.toBeNull();
        // 顶栏面包屑跟随路由（分组名做弱化尾标）
        expect(document.body.textContent).toContain('交易核心');
    });
});
