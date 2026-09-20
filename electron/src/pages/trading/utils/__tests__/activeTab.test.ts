/**
 * 模拟交易初始页签解析。
 *
 * 回归点：**默认页签是「系统健康」(desk)**，不再是「策略管理」(manage)。
 * 这是一个用户可见的约定——进页第一眼先看平台能不能用；被改回去时要有测试拦住。
 */
import { describe, expect, it } from 'vitest';
import { DEFAULT_ACTIVE_TAB, resolveInitialTab } from '../activeTab';

describe('resolveInitialTab', () => {
  it('默认落在「系统健康」', () => {
    expect(DEFAULT_ACTIVE_TAB).toBe('desk');
    expect(resolveInitialTab('')).toBe('desk');
    expect(resolveInitialTab('#/trading')).toBe('desk');
    expect(resolveInitialTab('#/trading?foo=bar')).toBe('desk');
  });

  it('SSR / 无 location 时同样是「系统健康」，不是 undefined', () => {
    expect(resolveInitialTab(undefined)).toBe('desk');
    expect(resolveInitialTab(null)).toBe('desk');
  });

  it('深链 tab=eval / tab=signals 仍然直达（评估徽章与候选信号跳转）', () => {
    expect(resolveInitialTab('#/trading?tab=eval')).toBe('eval');
    expect(resolveInitialTab('#/trading?tab=signals')).toBe('signals');
  });

  it('hash 不带 # 前缀也能解析（部分入口直接拼 ?tab=）', () => {
    expect(resolveInitialTab('?tab=eval')).toBe('eval');
  });

  it('未列入深链白名单的页签回落默认，不盲信 URL', () => {
    expect(resolveInitialTab('#/trading?tab=position')).toBe('desk');
    expect(resolveInitialTab('#/trading?tab=manage')).toBe('desk');
    expect(resolveInitialTab('#/trading?tab=__evil__')).toBe('desk');
  });

  it('query 里 tab 排在别的参数之后也能取到', () => {
    expect(resolveInitialTab('#/trading?market=CN&tab=signals')).toBe('signals');
  });
});
