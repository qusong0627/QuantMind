/**
 * 术语翻译层覆盖度测试（T-FE-01 验收）：
 * 1) 组件里所有 `term="xxx"` 字面量必须在 glossary 存在（漏翻即红——防术语裸奔）；
 * 2) 机构核心术语清单必须齐备；
 * 3) 每条翻译人话/专业说明非空且不重复（"解释"不能敷衍）。
 */

import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { GLOSSARY, getTerm, hasTerm } from '../glossary';

const SRC_ROOT = path.resolve(__dirname, '../../..');

function listSourceFiles(dir: string, acc: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules' || entry.name === 'dist' || entry.name === 'dist-react') continue;
      listSourceFiles(full, acc);
    } else if (/\.(tsx|ts)$/.test(entry.name)) {
      acc.push(full);
    }
  }
  return acc;
}

function collectTermLiterals(): Map<string, string[]> {
  const found = new Map<string, string[]>();
  const patterns = [/term="([A-Za-z_][A-Za-z0-9_]*)"/g, /term=\{['"]([A-Za-z_][A-Za-z0-9_]*)['"]\}/g];
  for (const file of listSourceFiles(SRC_ROOT)) {
    if (file.includes('__tests__')) continue;
    // 术语表本体与其消费组件（工具提示自身有 term= 形参/示例文本）不进扫描
    if (file.endsWith('glossary.ts') || file.endsWith('TermTooltip.tsx')) continue;
    const content = fs.readFileSync(file, 'utf-8');
    for (const pattern of patterns) {
      for (const match of content.matchAll(pattern)) {
        const key = match[1];
        const list = found.get(key) || [];
        list.push(path.relative(SRC_ROOT, file));
        found.set(key, list);
      }
    }
  }
  return found;
}

describe('glossary 术语表', () => {
  it('组件中所有 term 字面量都有翻译（漏翻即红）', () => {
    const literals = collectTermLiterals();
    expect(literals.size).toBeGreaterThan(0);
    const missing: string[] = [];
    for (const [key, files] of literals) {
      if (!hasTerm(key)) {
        missing.push(`${key}（使用于 ${files.join(', ')}）`);
      }
    }
    expect(missing, `以下术语缺少 glossary 翻译：\n${missing.join('\n')}`).toEqual([]);
  });

  it('机构核心术语清单齐备', () => {
    const required = [
      // 信号/选股
      'rank_pct', 'fusion_score', 'signal_side',
      // 体检九项
      'factor_regression', 'dsr', 'psr', 'min_trl', 'pbo', 'bootstrap',
      'concentration', 'regime', 'cost_sensitivity',
      // 四分类与评分
      'verdict_a', 'verdict_b', 'verdict_l', 'verdict_e',
      'confidence_score', 'low_confidence', 'red_line', 'score_grade',
      // 执行/通用
      'dry_run', 'exit_rule', 'rebalance', 'slippage', 'fill_rate', 'tracking_error',
      'source', 'pipeline',
    ];
    const missing = required.filter((key) => !hasTerm(key));
    expect(missing, `缺少核心术语：${missing.join(', ')}`).toEqual([]);
  });

  it('每条翻译：人话与专业说明非空且不同', () => {
    for (const [key, entry] of Object.entries(GLOSSARY)) {
      expect(entry.plain.trim().length, `${key}.plain 为空`).toBeGreaterThan(0);
      expect(entry.detail.trim().length, `${key}.detail 为空`).toBeGreaterThan(0);
      expect(entry.plain, `${key}.plain 不应与 detail 相同`).not.toBe(entry.detail);
    }
  });

  it('getTerm 对未登记/空值返回 null（组件原样渲染不崩）', () => {
    expect(getTerm('rank_pct')?.plain).toContain('分位');
    expect(getTerm('nope')).toBeNull();
    expect(getTerm(undefined)).toBeNull();
    expect(getTerm('')).toBeNull();
  });
});
