/** 策略版本 diff 纯函数（T-FE-10）：参数差异 / 代码行级 diff / 参数来源标注。可单测无副作用。 */

export interface VersionRecord {
  version: number;
  name?: string | null;
  status?: string | null;
  code_hash?: string | null;
  created_at?: string | null;
  parameters?: Record<string, unknown> | null;
  execution_config?: Record<string, unknown> | null;
  code?: string | null;
}

export interface ParamDiffEntry {
  key: string;
  from: unknown;
  to: unknown;
  kind: 'changed' | 'added' | 'removed';
}

const _render = (v: unknown): string => {
  if (v === undefined) return '—';
  if (v === null) return 'null';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
};

export function renderParamValue(v: unknown): string {
  return _render(v);
}

/** 参数表 diff：变更/新增/删除三态，键按字典序稳定排序 */
export function paramDiffEntries(
  from: Record<string, unknown> | null | undefined,
  to: Record<string, unknown> | null | undefined
): ParamDiffEntry[] {
  const a = from || {};
  const b = to || {};
  const keys = Array.from(new Set([...Object.keys(a), ...Object.keys(b)])).sort();
  const out: ParamDiffEntry[] = [];
  for (const key of keys) {
    const hasA = Object.prototype.hasOwnProperty.call(a, key);
    const hasB = Object.prototype.hasOwnProperty.call(b, key);
    if (!hasA && hasB) out.push({ key, from: undefined, to: b[key], kind: 'added' });
    else if (hasA && !hasB) out.push({ key, from: a[key], to: undefined, kind: 'removed' });
    else if (JSON.stringify(a[key]) !== JSON.stringify(b[key])) {
      out.push({ key, from: a[key], to: b[key], kind: 'changed' });
    }
  }
  return out;
}

export type LineDiffKind = 'same' | 'added' | 'removed';
export interface LineDiffRow {
  kind: LineDiffKind;
  text: string;
  oldLine?: number;
  newLine?: number;
}

/** 代码行级 diff（LCS；行数超限时退化为整段替换提示，防大文件卡顿） */
export function lineDiff(oldCode: string, newCode: string, maxLines = 1500): LineDiffRow[] {
  const a = String(oldCode || '').split('\n');
  const b = String(newCode || '').split('\n');
  if (a.length > maxLines || b.length > maxLines) {
    return [
      { kind: 'removed', text: `（旧版 ${a.length} 行，超 diff 上限，整段视为变更）` },
      { kind: 'added', text: `（新版 ${b.length} 行）` },
    ];
  }
  // LCS 动态规划（表规模 ≤ maxLines²，可接受）
  const n = a.length;
  const m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const rows: LineDiffRow[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ kind: 'same', text: a[i], oldLine: i + 1, newLine: j + 1 });
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      rows.push({ kind: 'removed', text: a[i], oldLine: i + 1 });
      i += 1;
    } else {
      rows.push({ kind: 'added', text: b[j], newLine: j + 1 });
      j += 1;
    }
  }
  while (i < n) {
    rows.push({ kind: 'removed', text: a[i], oldLine: i + 1 });
    i += 1;
  }
  while (j < m) {
    rows.push({ kind: 'added', text: b[j], newLine: j + 1 });
    j += 1;
  }
  return rows;
}

export interface ParamSourceRow {
  key: string;
  value: string;
  source: 'template_default' | 'modified' | 'user_custom';
}

/** 生效参数 + 来源：与模板默认值比对——一致=模板默认、有值不同=已修改、模板无此键=用户自定义 */
export function paramSourceRows(
  params: Record<string, unknown> | null | undefined,
  templateParams: Record<string, unknown> | null | undefined
): ParamSourceRow[] {
  const current = params || {};
  const defaults = templateParams;
  return Object.keys(current)
    .sort()
    .map((key) => {
      const value = renderParamValue(current[key]);
      if (!defaults || !Object.prototype.hasOwnProperty.call(defaults, key)) {
        return { key, value, source: 'user_custom' as const };
      }
      const same = JSON.stringify(defaults[key]) === JSON.stringify(current[key]);
      return { key, value, source: same ? ('template_default' as const) : ('modified' as const) };
    });
}

/** 版本快照列表 + 两端 → 组装 diff 视图所需数据（供 Drawer 使用） */
export function pickVersions(list: VersionRecord[]): { latest: VersionRecord | null; previous: VersionRecord | null } {
  const sorted = [...(list || [])].sort((x, y) => y.version - x.version);
  return { latest: sorted[0] || null, previous: sorted[1] || null };
}
