/**
 * QuantBot 提示词库数据模型（纯函数，可单测）— 合并两个来源为一个库：
 * 1) 意图示例：quantbotIntents（34 条短示例，"点选即发"）；
 * 2) 技能模板：data/prompts.generated（24 条长模板，含 {占位符}，由 scripts/gen_prompts.py 从 prompts/*.md 生成）。
 */
import { PROMPTS } from '../data/prompts.generated';
import { QUANTBOT_INTENTS } from '../intents/quantbotIntents';

export type LibPromptKind = 'example' | 'template';

export interface LibPrompt {
  id: string;
  kind: LibPromptKind;
  title: string;
  category: string;
  description: string;
  /** 模板专属：产出说明 */
  outputs?: string;
  /** 模板专属：源文件名（prompts/*.md） */
  name?: string;
  body: string;
}

const EXAMPLE_ITEMS: LibPrompt[] = QUANTBOT_INTENTS.flatMap((intent) =>
  intent.examples.map((ex, i) => ({
    id: `example:${intent.key}:${i}`,
    kind: 'example' as const,
    title: ex.label,
    category: intent.label,
    description: intent.description,
    body: ex.prompt,
  })),
);

const TEMPLATE_ITEMS: LibPrompt[] = PROMPTS.map((p) => ({
  id: `template:${p.name}`,
  kind: 'template' as const,
  title: p.title,
  category: p.category,
  description: p.description,
  outputs: p.outputs,
  name: p.name,
  body: p.body,
}));

/** 示例在前、模板在后（与各自源文件的声明顺序一致） */
export const ALL_PROMPTS: LibPrompt[] = [...EXAMPLE_ITEMS, ...TEMPLATE_ITEMS];

export const EXAMPLE_TOTAL = EXAMPLE_ITEMS.length;

export const TEMPLATE_TOTAL = TEMPLATE_ITEMS.length;

export const PROMPT_LIBRARY_TOTAL = ALL_PROMPTS.length;

export const EXAMPLE_CATEGORY_ORDER: string[] = QUANTBOT_INTENTS.map((i) => i.label);

export const TEMPLATE_CATEGORY_ORDER: string[] = ['平台运营', '环境初始化', '研究分析', '策略·因子·模型·回测', '交易'];

/** 搜索：命中标题 / 描述 / 分类 / 正文，大小写不敏感；空查询原样返回 */
export function filterPrompts(items: LibPrompt[], query: string): LibPrompt[] {
  const q = query.trim().toLowerCase();
  if (!q) return items;
  return items.filter(
    (p) =>
      p.title.toLowerCase().includes(q) ||
      p.description.toLowerCase().includes(q) ||
      p.category.toLowerCase().includes(q) ||
      p.body.toLowerCase().includes(q),
  );
}

/** 按分类聚组并保持 order 顺序；order 之外的新分类排到最后 */
export function groupByCategory(items: LibPrompt[], order: string[]): Array<[string, LibPrompt[]]> {
  const map = new Map<string, LibPrompt[]>();
  for (const p of items) {
    if (!map.has(p.category)) map.set(p.category, []);
    map.get(p.category)!.push(p);
  }
  const rank = (name: string): number => {
    const i = order.indexOf(name);
    return i === -1 ? order.length : i;
  };
  return [...map.entries()].sort((a, b) => rank(a[0]) - rank(b[0]));
}
