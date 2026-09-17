import { describe, expect, it } from 'vitest';
import {
  ALL_PROMPTS,
  EXAMPLE_CATEGORY_ORDER,
  EXAMPLE_TOTAL,
  PROMPT_LIBRARY_TOTAL,
  TEMPLATE_CATEGORY_ORDER,
  TEMPLATE_TOTAL,
  filterPrompts,
  groupByCategory,
  type LibPrompt,
} from '../promptLibraryModel';

describe('QuantBot 提示词库数据模型', () => {
  it('合并两类来源：示例在前、模板在后，id 唯一且字段非空', () => {
    // Arrange & Act
    const examples = ALL_PROMPTS.filter((p) => p.kind === 'example');
    const templates = ALL_PROMPTS.filter((p) => p.kind === 'template');

    // Assert
    expect(examples.length).toBeGreaterThanOrEqual(30);
    expect(templates.length).toBeGreaterThanOrEqual(20);
    expect(EXAMPLE_TOTAL).toBe(examples.length);
    expect(TEMPLATE_TOTAL).toBe(templates.length);
    expect(PROMPT_LIBRARY_TOTAL).toBe(EXAMPLE_TOTAL + TEMPLATE_TOTAL);
    expect(ALL_PROMPTS.slice(0, examples.length).every((p) => p.kind === 'example')).toBe(true);

    const ids = new Set(ALL_PROMPTS.map((p) => p.id));
    expect(ids.size).toBe(ALL_PROMPTS.length);
    for (const p of ALL_PROMPTS) {
      expect(p.title.length).toBeGreaterThan(0);
      expect(p.body.length).toBeGreaterThan(10);
      expect(p.category.length).toBeGreaterThan(0);
      expect(p.description.length).toBeGreaterThan(0);
    }
  });

  it('模板条目保留源文件元数据（outputs / name）', () => {
    const templates = ALL_PROMPTS.filter((p) => p.kind === 'template');
    for (const p of templates) {
      expect(p.name).toBeTruthy();
      expect(p.outputs).toBeTruthy();
    }
  });

  it('示例按意图声明顺序分组，模板按技能中心固定顺序分组', () => {
    // Arrange & Act
    const exampleGroups = groupByCategory(
      ALL_PROMPTS.filter((p) => p.kind === 'example'),
      EXAMPLE_CATEGORY_ORDER,
    );
    const templateGroups = groupByCategory(
      ALL_PROMPTS.filter((p) => p.kind === 'template'),
      TEMPLATE_CATEGORY_ORDER,
    );

    // Assert：示例五类顺序与 QUANTBOT_INTENTS 声明一致
    expect(exampleGroups.map(([name]) => name)).toEqual(EXAMPLE_CATEGORY_ORDER);
    // 模板分组保持 order 升序，且分类都在已知清单内
    const ranks = templateGroups.map(([name]) => TEMPLATE_CATEGORY_ORDER.indexOf(name));
    expect(ranks.every((r) => r >= 0)).toBe(true);
    expect([...ranks].sort((a, b) => a - b)).toEqual(ranks);
  });

  it('order 之外的新分类排到最后', () => {
    // Arrange
    const extra: LibPrompt = {
      id: 'template:unknown-new',
      kind: 'template',
      title: '新分类模板',
      category: '未来分类',
      description: '占位',
      body: '占位正文占位正文',
    };

    // Act
    const groups = groupByCategory([...ALL_PROMPTS, extra], TEMPLATE_CATEGORY_ORDER);

    // Assert
    expect(groups[groups.length - 1][0]).toBe('未来分类');
  });

  it('搜索命中标题/描述/正文且大小写不敏感，空查询原样返回', () => {
    // 空查询原样返回
    expect(filterPrompts(ALL_PROMPTS, '')).toHaveLength(ALL_PROMPTS.length);
    expect(filterPrompts(ALL_PROMPTS, '   ')).toHaveLength(ALL_PROMPTS.length);

    // 标题命中（示例）
    const byTitle = filterPrompts(ALL_PROMPTS, '双均线');
    expect(byTitle.some((p) => p.title === '双均线择时')).toBe(true);

    // 大小写不敏感
    expect(filterPrompts(ALL_PROMPTS, 'QUANTDB').length).toBe(filterPrompts(ALL_PROMPTS, 'quantdb').length);
    expect(filterPrompts(ALL_PROMPTS, 'quantdb').length).toBeGreaterThan(0);

    // 无匹配
    expect(filterPrompts(ALL_PROMPTS, '不存在的关键词xyz')).toHaveLength(0);
  });
});
