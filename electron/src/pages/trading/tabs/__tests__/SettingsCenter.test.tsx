/**
 * 设置栏的**实盘配置闸门**。
 *
 * 用户原话：「模拟盘栏目，就搞模拟盘，实盘的都去掉吧、现在 2 个模块的。一个模拟、
 * 一个实盘。」——账户、下发、顶栏都已是模拟盘，设置里还留着「券商实盘接入」就等于
 * 把实盘搬回来了：改完凭证下一步就是下单。所以 `liveConfigVisible=false` 时这一栏
 * 必须连入口都不渲染。
 *
 * 为什么单开一个文件：`RealTradingPage.test.tsx` 把那三个面板整块桩掉了，只验得到
 * 「传下去的 prop 是 false」；**prop 真的把面板关掉**这件事只有渲染真组件才看得见。
 * 组件的外部件（三张卡片、后端接口）在这里桩掉，测的就是它自己那两处 JSX 分支。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/react';
import { Provider } from 'react-redux';
import React from 'react';

import store from '../../../../store';
import { setMarket } from '../../../../store/slices/uiSlice';
import SettingsCenter, { type SettingsExtraPanel } from '../SettingsCenter';

// 实盘开关默认**开**：前半段问的是「开关开着的时候，哪一栏还该不该看见实盘配置」；
// 关掉的话两种取值都只剩凭证页，断言会退化成恒真。追加面板那一组会临时改成关，
// 验的是反过来的事：追加面板**不跟**这个开关联动（它的内容由调用方负责）。
let liveFlag = true;
vi.mock('../../../../config/tradingFlags', () => ({
    isLiveTradingEnabled: () => liveFlag,
}));

vi.mock('../../components/BrokerConfigCard', () => ({
    default: () => <div data-testid="card-broker" />,
}));
vi.mock('../../components/BrokerChannelCard', () => ({
    default: () => <div data-testid="card-channel" />,
}));
vi.mock('../../components/QmtMirrorCard', () => ({
    default: () => <div data-testid="card-mirror" />,
}));

const renderSettings = (
    liveConfigVisible?: boolean,
    tradingMode?: 'real' | 'simulation',
) =>
    render(
        <Provider store={store}>
            <SettingsCenter
                userId="u-1"
                isActive
                liveConfigVisible={liveConfigVisible}
                tradingMode={tradingMode}
            />
        </Provider>,
    );

/** 顶部页签条上的按钮文案（按 `settings-tabs` 锚定；没有页签时返回空数组） */
const tabLabels = (): string[] => {
    const strip = document.querySelector('[data-testid="settings-tabs"]');
    if (!strip) return [];
    return Array.from(strip.querySelectorAll('button')).map((b) => (b.textContent || '').trim());
};

describe('SettingsCenter 实盘配置闸门', () => {
    beforeEach(() => {
        liveFlag = true;
        // 真单镜像只在 A 股出现，钉住市场免得到别的机器上少一个页签
        store.dispatch(setMarket('CN'));
        // 挂载即拉 /api-keys/init。**永不落地**：本文件只断言页签，凭证数据回来与否
        // 无关，而落地后的 setState 会落在 act() 之外刷一屏警告（真返回也没意义）。
        vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
    });

    afterEach(() => {
        cleanup();
        vi.unstubAllGlobals();
    });

    it('缺省（公开树）：实盘面板照常出现——不传就是维持老行为', () => {
        renderSettings();

        expect(screen.queryByText('券商实盘接入')).not.toBeNull();
        expect(screen.queryByText('真单镜像')).not.toBeNull();
        expect(tabLabels()).toEqual(['券商实盘接入', '真单镜像']);
    });

    it('显式 true：与缺省一致', () => {
        renderSettings(true);

        expect(screen.queryByText('券商实盘接入')).not.toBeNull();
        expect(screen.queryByText('真单镜像')).not.toBeNull();
    });

    it('显式 false（模拟栏）：实盘两个入口一个都不渲染，凭证页也不再出现在这里', () => {
        renderSettings(false);

        expect(screen.queryByText('券商实盘接入')).toBeNull();
        expect(screen.queryByText('真单镜像')).toBeNull();
        // 凭证已迁后台管理系统设置：这里没有页签，只有迁移说明
        expect(tabLabels()).toEqual([]);
        expect(screen.queryByText('接入凭证 / API 密钥已迁至后台管理')).not.toBeNull();
    });

    it('显式 false：说明文案指路后台管理，不承诺实盘', () => {
        renderSettings(false);

        expect(screen.queryByText('接入凭证 / API 密钥已迁至「后台管理 → 系统设置」。')).not.toBeNull();
        expect(screen.queryByText(/券商实盘通道/)).toBeNull();
    });

    it('标题跟随本栏模式取词（实盘栏里不许顶着「模拟交易设置」配真券商）', () => {
        renderSettings(true, 'real');
        expect(screen.queryByText('实盘交易设置')).not.toBeNull();
        // 实盘栏里连标题都不该出现「模拟」字样
        expect(screen.queryByText('模拟交易设置')).toBeNull();
        cleanup();

        renderSettings(true, 'simulation');
        expect(screen.queryByText('模拟交易设置')).not.toBeNull();
        expect(screen.queryByText('实盘交易设置')).toBeNull();
    });

    it('标题缺省按模拟盘——未知模式一律判为模拟（宁可少认一个实盘）', () => {
        renderSettings(true);

        expect(screen.queryByText('模拟交易设置')).not.toBeNull();
    });
});

/**
 * 追加面板（`extraPanels`）：公开树不感知调用方，本机实盘栏用它把 arena 的
 * 「总控 / 数据」嵌进设置里。契约有三条，都在这里钉住。
 */
describe('SettingsCenter 追加面板', () => {
    const panels: SettingsExtraPanel[] = [
        { id: 'arena-control', label: '总控', render: () => <div data-testid="panel-control" /> },
        { id: 'arena-data', label: '数据', render: () => <div data-testid="panel-data" /> },
    ];

    const renderWithPanels = (extraPanels?: readonly SettingsExtraPanel[]) =>
        render(
            <Provider store={store}>
                <SettingsCenter
                    userId="u-1"
                    isActive
                    liveConfigVisible={false}
                    extraPanels={extraPanels}
                />
            </Provider>,
        );

    beforeEach(() => {
        liveFlag = true;
        store.dispatch(setMarket('CN'));
        vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
    });

    afterEach(() => {
        cleanup();
        vi.unstubAllGlobals();
    });

    it('不传 = 公开树形态：没有实盘面板也没有追加面板时，只有迁移说明', () => {
        renderWithPanels();

        expect(tabLabels()).toEqual([]);
        expect(screen.queryByText('接入凭证 / API 密钥已迁至后台管理')).not.toBeNull();
    });

    it('页签只列追加面板，点了才挂载内容', () => {
        renderWithPanels(panels);

        expect(tabLabels()).toEqual(['总控', '数据']);
        expect(screen.queryByTestId('panel-control')).toBeNull();

        fireEvent.click(screen.getByText('总控'));
        expect(screen.queryByTestId('panel-control')).not.toBeNull();
        expect(screen.queryByTestId('panel-data')).toBeNull();
    });

    it('切走即卸载（不留后台轮询）', () => {
        renderWithPanels(panels);

        fireEvent.click(screen.getByText('数据'));
        expect(screen.queryByTestId('panel-data')).not.toBeNull();

        fireEvent.click(screen.getByText('总控'));
        expect(screen.queryByTestId('panel-data')).toBeNull();
    });

    it('不跟实盘开关联动：开关关掉时追加面板仍可点开', () => {
        liveFlag = false;
        renderWithPanels(panels);

        // 内置两个已经不在（liveConfigVisible=false）
        expect(screen.queryByText('券商实盘接入')).toBeNull();

        fireEvent.click(screen.getByText('总控'));
        expect(screen.queryByTestId('panel-control')).not.toBeNull();
        // 闸门只踢内置栏：追加面板选中后不会被迁移说明顶掉
        expect(tabLabels()).toEqual(['总控', '数据']);
    });
});
