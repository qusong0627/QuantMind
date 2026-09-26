/**
 * 测试渲染工具：为需要 redux 的 hook/组件补上 <Provider>
 *
 * 背景：部分 hook 在**渲染期**直接读 redux store（如 useTradeRecords 用 useSelector
 * 取当前用户），而测试里裸用 @testing-library/react 的 renderHook 会因为缺少
 * <Provider> 抛出：
 *   could not find react-redux context value; please ensure the component is wrapped in a <Provider>
 *
 * 这里统一提供带 Provider 的包装，避免每个测试文件各写一遍样板。
 *
 * @author QuantMind Team
 */

import React from 'react';
import { Provider } from 'react-redux';
import { renderHook } from '@testing-library/react';
import type { RenderHookOptions } from '@testing-library/react';
import store from '../store';

export const ReduxProviderWrapper = ({ children }: { children: React.ReactNode }) => (
  <Provider store={store}>{children}</Provider>
);

/**
 * 与 renderHook 同签名，但自动包上 redux <Provider>。
 * 调用方若自带 wrapper，会与 Provider 组合（Provider 在外层）。
 */
export const renderHookWithProviders = <TProps, TResult>(
  hook: (props: TProps) => TResult,
  options?: Omit<RenderHookOptions<TProps>, 'wrapper'>,
) => renderHook(hook, { wrapper: ReduxProviderWrapper, ...options });
