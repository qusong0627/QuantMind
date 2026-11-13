import { describe, expect, it } from 'vitest';
import type { StrategyTemplate } from '../qlibStrategyTemplates';
import {
  GENERAL_STRATEGY_DIR,
  groupStrategyTemplateSections,
  groupStrategyTemplatesByDir,
} from '../qlibStrategyTemplates';

function template(id: string, dir?: string, sort?: number): StrategyTemplate {
  return {
    id,
    name: id,
    description: '',
    category: 'basic',
    difficulty: 'beginner',
    code: '',
    params: [],
    dir,
    sort,
  };
}

describe('groupStrategyTemplatesByDir', () => {
  it('pins 通用策略 first even when other dirs appear earlier', () => {
    const grouped = groupStrategyTemplatesByDir([
      template('alpha', '港股策略/核心'),
      template('standard_topk', undefined, 1),
      template('beta', '港股策略/核心'),
      template('gamma', 'minibt策略'),
      template('StopLoss', '  '),
    ]);

    expect(grouped.map(([label]) => label)).toEqual([
      GENERAL_STRATEGY_DIR,
      '港股策略/核心',
      'minibt策略',
    ]);
    expect(grouped[0][1].map((item) => item.id)).toEqual(['standard_topk', 'StopLoss']);
  });
});

describe('groupStrategyTemplateSections', () => {
  it('collapses multi-level dirs under one top-level group', () => {
    const templates = [
      template('standard_topk', undefined, 1),
      template('StopLoss'),
      template('hk_a', '港股策略/核心'),
      template('hk_b', '港股策略/核心'),
      template('hk_c', '港股策略/红利'),
    ];
    const sections = groupStrategyTemplateSections(templates);
    expect(sections.map((section) => section.label)).toEqual([
      GENERAL_STRATEGY_DIR,
      '港股策略',
    ]);
    expect(sections[0].items.map((item) => item.id)).toEqual(['standard_topk', 'StopLoss']);
    expect(sections[1].children.map((child) => child.label)).toEqual(['核心', '红利']);
    expect(sections[1].children[0].items).toHaveLength(2);
  });
});
