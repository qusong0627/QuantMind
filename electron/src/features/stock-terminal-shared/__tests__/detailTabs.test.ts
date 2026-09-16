/**
 * 个股页签简单/专业降升维（T-FE-02 收尾）：过滤与回落纯函数。
 */

import { describe, expect, it } from 'vitest';
import { fallbackDetailTab, visibleDetailTabs, type DetailTabDef } from '../utils';

const TABS: DetailTabDef[] = [
  { id: 'overview', label: '概况' },
  { id: 'financials', label: '财务' },
  { id: 'l2', label: 'L2', proOnly: true },
  { id: 'news', label: '资讯' },
];

describe('visibleDetailTabs / fallbackDetailTab', () => {
  it('简单模式收起 proOnly；专业模式全量', () => {
    expect(visibleDetailTabs(TABS, true).map((t) => t.id)).toEqual(['overview', 'financials', 'news']);
    expect(visibleDetailTabs(TABS, false).map((t) => t.id)).toEqual([
      'overview',
      'financials',
      'l2',
      'news',
    ]);
  });

  it('被收起的当前页签回落到第一个可见页签；可见时保持不变', () => {
    expect(fallbackDetailTab('l2', TABS, true)).toBe('overview');
    expect(fallbackDetailTab('news', TABS, true)).toBe('news');
    expect(fallbackDetailTab('l2', TABS, false)).toBe('l2');
  });

  it('缺省/空定义不抛错', () => {
    expect(fallbackDetailTab('x' as never, [], true)).toBe('x');
    expect(visibleDetailTabs([], true)).toEqual([]);
  });
});
