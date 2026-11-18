/**
 * 模拟交易初始页签解析。
 *
 * 回归点：**默认页签是「系统健康」(desk)**，不再是「策略管理」(manage)。
 * 这是一个用户可见的约定——进页第一眼先看平台能不能用；被改回去时要有测试拦住。
 */
import { describe, expect, it } from 'vitest';
import { DEFAULT_ACTIVE_TAB, isDeepLinkableTab, resolveInitialSymbol, resolveInitialTab } from '../activeTab';

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

  it('深链 tab=signals 仍然直达（候选信号跳转）', () => {
    expect(resolveInitialTab('#/trading?tab=signals')).toBe('signals');
  });

  it('tab=eval 不再是交易页签（评估中心 2026-11-18 迁至后台管理）', () => {
    // 旧链接/旧书签不再把人留在空白栏，而是回落默认；徽章改跳 /admin/eval
    expect(resolveInitialTab('#/trading?tab=eval')).toBe('desk');
  });

  it('hash 不带 # 前缀也能解析（部分入口直接拼 ?tab=）', () => {
    expect(resolveInitialTab('?tab=signals')).toBe('signals');
  });

  it('持仓预警深链 tab=position 直达持仓监控（哨兵「去持仓」落点）', () => {
    expect(resolveInitialTab('#/trading?tab=position')).toBe('position');
    expect(resolveInitialTab('#/trading?tab=position&symbol=SZ000001')).toBe('position');
    // 归一后实盘栏深链同样直达（本机实盘栏目存在）
    expect(resolveInitialTab('#/live?tab=position&symbol=SZ000001')).toBe('position');
  });

  it('未列入深链白名单的页签回落默认，不盲信 URL', () => {
    expect(resolveInitialTab('#/trading?tab=manage')).toBe('desk');
    expect(resolveInitialTab('#/trading?tab=__evil__')).toBe('desk');
  });

  it('query 里 tab 排在别的参数之后也能取到', () => {
    expect(resolveInitialTab('#/trading?market=CN&tab=signals')).toBe('signals');
  });

  it('深链 symbol 只在 tab=position 时消费（哨兵「去持仓」带标的）', () => {
    expect(resolveInitialSymbol('#/trading?tab=position&symbol=SZ000001')).toBe('SZ000001');
    expect(resolveInitialSymbol('#/live?tab=position&symbol=SH601000')).toBe('SH601000');
    expect(resolveInitialSymbol('#/trading?tab=position')).toBeNull();
    expect(resolveInitialSymbol('#/trading?tab=signals&symbol=SZ000001')).toBeNull();
    expect(resolveInitialSymbol('#/trading?tab=manage&symbol=SZ000001')).toBeNull();
    expect(resolveInitialSymbol('')).toBeNull();
    expect(resolveInitialSymbol(null)).toBeNull();
  });
});

/**
 * 页内 URL 变化（RealTradingPage 的 searchParams 监听）与挂载解析必须同一把尺子：
 * 白名单放宽会让「手输 ?tab=settings 生效、刷新却不生效」，收紧会让页内跳转失效。
 */
describe('isDeepLinkableTab', () => {
  it('只认白名单两条（与挂载口径一致）', () => {
    expect(isDeepLinkableTab('signals')).toBe(true);
    expect(isDeepLinkableTab('position')).toBe(true);
  });

  it('其余页签与空值一律不认（含迁走的 eval）', () => {
    expect(isDeepLinkableTab('eval')).toBe(false);
    expect(isDeepLinkableTab('desk')).toBe(false);
    expect(isDeepLinkableTab('manage')).toBe(false);
    expect(isDeepLinkableTab('__evil__')).toBe(false);
    expect(isDeepLinkableTab('')).toBe(false);
    expect(isDeepLinkableTab(null)).toBe(false);
    expect(isDeepLinkableTab(undefined)).toBe(false);
  });
});
