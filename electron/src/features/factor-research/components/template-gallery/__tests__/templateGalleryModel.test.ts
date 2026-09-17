import { describe, expect, it } from 'vitest';
import {
  difficultyLabel,
  filterTemplates,
  marketLabel,
  normalizeTemplate,
  templateLines,
  templateStats,
} from '../templateGalleryModel';

const backendTpl = (over: Record<string, unknown> = {}) => ({
  id: 'minibt_trend',
  name: '均线趋势',
  description: '双均线上穿买入，下穿卖出，适合趋势行情。',
  category: 'basic',
  difficulty: 'beginner',
  markets: ['a_share', 'hong_kong'],
  dir: 'minibt策略/趋势跟踪',
  params: [{ name: 'SYMBOL' }, { name: 'FAST' }],
  live_config_tips: ['尾盘 14:50 执行；港股用 15:50'],
  code: 'import minibt\nCFG = {"FAST": 5}',
  ...over,
});

describe('normalizeTemplate（两代字段容错）', () => {
  it('后端形态：字段映射 + minibt 判定（import/dir/参数三路任一命中）', () => {
    const t = normalizeTemplate(backendTpl());
    expect(t.id).toBe('minibt_trend');
    expect(t.markets).toEqual(['a_share', 'hong_kong']);
    expect(t.isMinibt).toBe(true);
    expect(t.paramCount).toBe(2);
    expect(t.tips).toHaveLength(1);
  });

  it('缺字段给安全默认，不抛错；import 缺失但目录含 minibt 也判 minibt', () => {
    const t = normalizeTemplate({ id: 'x', code: '', dir: 'Minibt研究/', params: [] });
    expect(t.name).toBe('x');
    expect(t.description).toBe('');
    expect(t.isMinibt).toBe(true);
    const plain = normalizeTemplate({ id: 'y' });
    expect(plain.isMinibt).toBe(false);
    expect(plain.paramCount).toBe(0);
  });
});

describe('templateLines（三行说明）', () => {
  it('描述 + 首条提示 + 规模行；超过三行截断', () => {
    const lines = templateLines(normalizeTemplate(backendTpl()));
    expect(lines[0]).toContain('双均线');
    expect(lines[1]).toContain('使用提示：尾盘');
    expect(lines[2]).toContain('参数 2 个');
    expect(lines[2]).toContain('入门');
    expect(lines[2]).toContain('A股/港股');
    expect(lines.length).toBeLessThanOrEqual(3);
  });
});

describe('filterTemplates / templateStats', () => {
  const list = [
    normalizeTemplate(backendTpl()),
    normalizeTemplate(backendTpl({ id: 'hk_dividend', name: '港股高股息', difficulty: 'intermediate', markets: ['hong_kong'], dir: '', code: 'x', params: [] })),
    normalizeTemplate(backendTpl({ id: 'legacy', name: '经典双均线', difficulty: 'advanced', markets: [], code: 'x', params: [] })),
  ];

  it('难度/市场/关键词过滤；空 markets 视为 A 股', () => {
    expect(filterTemplates(list, { difficulty: 'beginner' })).toHaveLength(1);
    expect(filterTemplates(list, { market: 'hong_kong' })).toHaveLength(2);
    // 空 markets → A股：市场筛 A股 命中 2（template1 + legacy）
    expect(filterTemplates(list, { market: 'a_share' })).toHaveLength(2);
    expect(filterTemplates(list, { keyword: '股息' })).toHaveLength(1);
    expect(filterTemplates(list, { keyword: '不存在' })).toHaveLength(0);
  });

  it('统计：总数/minibt/入门', () => {
    const stats = templateStats(list);
    expect(stats.total).toBe(3);
    expect(stats.minibt).toBe(2); // 前两个导入 minibt / dir 含 minibt
    expect(stats.beginner).toBe(1);
  });

  it('标签函数容错', () => {
    expect(marketLabel('us_stock')).toBe('美股');
    expect(marketLabel('weird')).toBe('weird');
    expect(difficultyLabel('')).toBe('通用');
  });
});
