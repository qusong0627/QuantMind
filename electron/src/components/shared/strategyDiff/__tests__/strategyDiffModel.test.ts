import { describe, expect, it } from 'vitest';
import {
  lineDiff,
  paramDiffEntries,
  paramSourceRows,
  pickVersions,
  renderParamValue,
} from '../strategyDiffModel';

describe('paramDiffEntries（参数差异）', () => {
  it('变更/新增/删除三态 + 字典序稳定', () => {
    const diff = paramDiffEntries(
      { topk: 5, n_drop: 2, old_key: 'x' },
      { topk: 10, n_drop: 2, new_key: true }
    );
    expect(diff.map((d) => [d.key, d.kind])).toEqual([
      ['new_key', 'added'],
      ['old_key', 'removed'],
      ['topk', 'changed'],
    ]);
    expect(diff.find((d) => d.key === 'topk')).toMatchObject({ from: 5, to: 10 });
  });

  it('对象/数组值按 JSON 判定；null/undefined 安全', () => {
    const diff = paramDiffEntries({ a: [1, 2] }, { a: [1, 2] });
    expect(diff).toHaveLength(0);
    expect(paramDiffEntries(null, undefined)).toEqual([]);
    expect(renderParamValue(null)).toBe('null');
    expect(renderParamValue(undefined)).toBe('—');
    expect(renderParamValue({ x: 1 })).toBe('{"x":1}');
  });
});

describe('lineDiff（代码行级 LCS）', () => {
  it('增删改行与上下文', () => {
    const oldCode = 'a\nb\nc\nd';
    const newCode = 'a\nB\nc\nnew\nd';
    const rows = lineDiff(oldCode, newCode);
    const kinds = rows.map((r) => `${r.kind}:${r.text}`);
    expect(kinds).toContain('same:a');
    expect(kinds).toContain('removed:b');
    expect(kinds).toContain('added:B');
    expect(kinds).toContain('added:new');
    expect(kinds).toContain('same:d');
    // 行号：删除行取旧行号、新增行取新行号
    expect(rows.find((r) => r.kind === 'removed')?.oldLine).toBe(2);
    expect(rows.find((r) => r.kind === 'added')?.newLine).toBe(2);
  });

  it('相同代码零变更；超限走整段替换提示', () => {
    expect(lineDiff('x\ny', 'x\ny').every((r) => r.kind === 'same')).toBe(true);
    const big = Array.from({ length: 1600 }, (_, i) => `line${i}`).join('\n');
    const rows = lineDiff('a', big, 1500);
    expect(rows).toHaveLength(2);
    expect(rows[0].text).toContain('超 diff 上限');
  });
});

describe('paramSourceRows（生效参数及来源）', () => {
  it('模板一致=模板默认 / 不同=已修改 / 模板无此键=用户自定义；无模板全用户自定义', () => {
    const rows = paramSourceRows(
      { topk: 5, fast: 20, custom: 'v' },
      { topk: 5, fast: 10 }
    );
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r.source]));
    expect(byKey.topk).toBe('template_default');
    expect(byKey.fast).toBe('modified');
    expect(byKey.custom).toBe('user_custom');

    const noTemplate = paramSourceRows({ a: 1 }, null);
    expect(noTemplate[0].source).toBe('user_custom');
    expect(paramSourceRows(null, { a: 1 })).toEqual([]);
  });
});

describe('pickVersions', () => {
  it('降序取最新与上一版；单版本 previous=null；空列表安全', () => {
    const { latest, previous } = pickVersions([
      { version: 1 },
      { version: 3 },
      { version: 2 },
    ]);
    expect(latest?.version).toBe(3);
    expect(previous?.version).toBe(2);
    expect(pickVersions([{ version: 1 }]).previous).toBeNull();
    expect(pickVersions([]).latest).toBeNull();
  });
});
