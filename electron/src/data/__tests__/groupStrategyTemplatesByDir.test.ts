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
  it('pins 通用策略 first even when A-share dirs appear earlier', () => {
    const grouped = groupStrategyTemplatesByDir([
      template('as01_core', 'A股策略/01_宽基多因子'),
      template('standard_topk', undefined, 1),
      template('as02_core', 'A股策略/01_宽基多因子'),
      template('hk_core', '港股策略/核心'),
      template('StopLoss', '  '),
    ]);

    expect(grouped.map(([label]) => label)).toEqual([
      GENERAL_STRATEGY_DIR,
      'A股策略/01_宽基多因子',
      '港股策略/核心',
    ]);
    expect(grouped[0][1].map((item) => item.id)).toEqual(['standard_topk', 'StopLoss']);
  });

  it('does not put stray A-share templates into 通用策略', () => {
    const grouped = groupStrategyTemplatesByDir([
      template('standard_topk'),
      template('as09_dividend_value'),
    ]);
    expect(grouped.map(([label]) => label)).toEqual([
      GENERAL_STRATEGY_DIR,
      'A股策略/未分类',
    ]);
    expect(grouped[0][1]).toHaveLength(1);
  });
});

describe('groupStrategyTemplateSections', () => {
  it('keeps 10 A-share folders under one parent so 通用 does not make 11 top-level groups', () => {
    const templates = [
      template('standard_topk', undefined, 1),
      template('StopLoss'),
      template('as01', 'A股策略/01_宽基多因子'),
      template('as02', 'A股策略/01_宽基多因子'),
      template('as06', 'A股策略/02_价值与质量'),
    ];
    const sections = groupStrategyTemplateSections(templates);
    expect(sections.map((section) => section.label)).toEqual([
      GENERAL_STRATEGY_DIR,
      'A股策略',
    ]);
    expect(sections[0].items.map((item) => item.id)).toEqual(['standard_topk', 'StopLoss']);
    expect(sections[1].children.map((child) => child.label)).toEqual([
      '01_宽基多因子',
      '02_价值与质量',
    ]);
    expect(sections[1].children[0].items).toHaveLength(2);
  });
});
