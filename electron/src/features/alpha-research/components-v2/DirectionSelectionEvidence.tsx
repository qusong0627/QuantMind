import React from 'react';
import { formatDateTime } from '../utils-v2';

interface Selection {
  mode: 'random';
  weighting: 'blankness' | 'uniform_fallback';
  seed: number;
  picked: string;
  python_version: string;
  captured_at: string;
  scope: { market: 'a_share'; universe: string; algorithm: 'rdagent' };
  candidates: Array<{ direction: string; attempts: number; weight: number }>;
  quota_fallback?: boolean;
}

const QUOTA_TIERS = ['explore', 'exploit', 'mutate'] as const;
const QUOTA_WEIGHTINGS = ['blankness', 'proxy_score', 'uniform'] as const;
const QUOTA_TIER_LABEL: Record<string, string> = { explore: '探索档', exploit: '优选档', mutate: '变异档' };
const QUOTA_WEIGHTING_LABEL: Record<string, string> = { blankness: '历史空白度加权', proxy_score: '成功率加权', uniform: '均匀' };

interface QuotaCandidate {
  direction: string;
  value: number;
  weight: number;
}

interface QuotaSelection {
  mode: 'quota';
  tier: string;
  weighting: string;
  seed: number;
  picked: string;
  python_version: string;
  captured_at: string;
  scope: { market: 'a_share'; universe: string; algorithm: 'rdagent' };
  candidates: QuotaCandidate[];
  saturatedCount: number | null;
  tierFallback: string | null;
}

function quotaSelection(value: unknown): QuotaSelection | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  if (row.mode !== 'quota' || typeof row.tier !== 'string' || !QUOTA_TIERS.includes(row.tier as (typeof QUOTA_TIERS)[number])
    || typeof row.weighting !== 'string' || !QUOTA_WEIGHTINGS.includes(row.weighting as (typeof QUOTA_WEIGHTINGS)[number])
    || typeof row.seed !== 'number' || !Number.isSafeInteger(row.seed)
    || typeof row.picked !== 'string' || typeof row.python_version !== 'string'
    || typeof row.captured_at !== 'string' || !Number.isFinite(Date.parse(row.captured_at as string))) return null;
  const scope = row.scope as QuotaSelection['scope'] | undefined;
  if (scope?.market !== 'a_share' || typeof scope.universe !== 'string' || scope.algorithm !== 'rdagent') return null;
  const candidates = row.candidates;
  if (!Array.isArray(candidates) || !candidates.length || candidates.length > 100
    || candidates.some((item) => !item || typeof (item as QuotaCandidate).direction !== 'string'
      || typeof (item as QuotaCandidate).value !== 'number' || !Number.isFinite((item as QuotaCandidate).value)
      || typeof (item as QuotaCandidate).weight !== 'number' || !Number.isFinite((item as QuotaCandidate).weight)
      || (item as QuotaCandidate).weight < 0 || (item as QuotaCandidate).weight > 1)) return null;
  let saturatedCount: number | null = null;
  const snapshot = row.scores_snapshot;
  if (Array.isArray(snapshot)) {
    const rows = snapshot.filter((entry) => entry && typeof entry === 'object'
      && typeof (entry as { direction?: unknown }).direction === 'string'
      && typeof (entry as { saturated?: unknown }).saturated === 'boolean');
    if (rows.length === snapshot.length) {
      saturatedCount = rows.filter((entry) => (entry as { saturated: boolean }).saturated).length;
    }
  }
  const fallback = row.tier_fallback;
  return {
    mode: 'quota',
    tier: row.tier as string,
    weighting: row.weighting as string,
    seed: row.seed as number,
    picked: row.picked as string,
    python_version: row.python_version as string,
    captured_at: row.captured_at as string,
    scope: { market: 'a_share', universe: scope.universe, algorithm: 'rdagent' },
    candidates: candidates as QuotaCandidate[],
    saturatedCount,
    tierFallback: typeof fallback === 'string' ? fallback : null,
  };
}

function selection(value: unknown): Selection | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const row = value as Partial<Selection>;
  if (row.mode !== 'random' || !['blankness', 'uniform_fallback'].includes(row.weighting || '')
    || typeof row.seed !== 'number' || !Number.isSafeInteger(row.seed)
    || typeof row.picked !== 'string' || typeof row.python_version !== 'string'
    || typeof row.captured_at !== 'string' || !Number.isFinite(Date.parse(row.captured_at))
    || row.scope?.market !== 'a_share' || typeof row.scope.universe !== 'string' || row.scope.algorithm !== 'rdagent'
    || !Array.isArray(row.candidates) || !row.candidates.length || row.candidates.length > 100
    || row.candidates.some((item) => !item || typeof item.direction !== 'string'
      || typeof item.attempts !== 'number' || !Number.isSafeInteger(item.attempts) || item.attempts < 0
      || typeof item.weight !== 'number' || !Number.isFinite(item.weight) || item.weight <= 0 || item.weight > 1)) return null;
  return row as Selection;
}

export const DirectionSelectionEvidence: React.FC<{ value: unknown }> = ({ value }) => {
  if (value == null) return null;
  const quota = quotaSelection(value);
  if (quota) {
    const total = quota.candidates.reduce((sum, item) => sum + item.weight, 0);
    return <section className="rounded border border-border/60 p-3 space-y-2 text-xs">
      <h4 className="font-medium">方向配额凭证 · {QUOTA_TIER_LABEL[quota.tier]}（{QUOTA_WEIGHTING_LABEL[quota.weighting]}）</h4>
      <p className="break-all">抽中：{quota.picked} · 股票池 {quota.scope.universe}</p>
      <p className="text-muted-foreground">{formatDateTime(quota.captured_at)} · 种子 {quota.seed} · Python {quota.python_version}</p>
      <p className="text-muted-foreground">按成功率×饱和惩罚三档分配：探索/优选/变异各取其池内加权。饱和来自本人 90 日物化台账的重复命中。以下是当次快照，后续历史变化不会改写它。同 Python 实现可按有序候选、权重和种子重放抽取。</p>
      {quota.tierFallback && <p className="text-amber-600">本档候选不足，已按规则降级：{quota.tierFallback === 'saturated' ? '全池饱和，仍在全池内加权' : '变异档无近期成功方向，改用优选档'}。</p>}
      {quota.saturatedCount !== null && quota.saturatedCount > 0 && <p className="text-muted-foreground">当次快照中有 {quota.saturatedCount} 条方向已饱和（90 日重复命中≥2）。</p>}
      <div className="max-h-64 overflow-auto"><table className="w-full text-left"><thead><tr><th className="p-1">候选方向</th><th className="p-1">口径值</th><th className="p-1">权重</th><th className="p-1">相对概率</th></tr></thead><tbody>{quota.candidates.map((item, index) => <tr key={`${index}:${item.direction}`} className="border-t border-border/40"><td className="p-1 break-all">{item.direction}{item.direction === quota.picked ? ' ✓' : ''}</td><td className="p-1">{String(item.value)}</td><td className="p-1">{String(item.weight)}</td><td className="p-1">{total > 0 ? `${(100 * item.weight / total).toFixed(2)}%` : '—'}</td></tr>)}</tbody></table></div>
    </section>;
  }
  const record = selection(value);
  if (!record) return <p className="text-xs text-muted-foreground">方向抽样凭证不完整，无法展示候选和权重。</p>;
  const total = record.candidates.reduce((sum, item) => sum + item.weight, 0);
  return <section className="rounded border border-border/60 p-3 space-y-2 text-xs">
    <h4 className="font-medium">方向抽样凭证 · {record.weighting === 'blankness' ? '本人历史空白度加权' : '历史读取失败，均匀兜底'}</h4>
    {record.quota_fallback === true && <p className="text-amber-600">配额输入不可用，已退回历史空白度加权；不代表配额成功。</p>}
    <p className="break-all">抽中：{record.picked} · 股票池 {record.scope.universe}</p>
    <p className="text-muted-foreground">{formatDateTime(record.captured_at)} · 种子 {record.seed} · Python {record.python_version}</p>
    <p className="text-muted-foreground">权重为 1/(1+n)。n 是本人在此股票池的 RD-Agent 历史提交数，含排队、失败、取消和已归档任务；不等于实际运行次数或有效因子数。以下是当次快照，后续历史变化不会改写它。同 Python 实现可按有序候选、权重和种子重放抽取。</p>
    {record.weighting === 'uniform_fallback' && <p className="text-amber-600">本次没有取得可用历史，记录中的零次数用于均匀兜底，不代表没有历史任务。</p>}
    <div className="max-h-64 overflow-auto"><table className="w-full text-left"><thead><tr><th className="p-1">候选方向</th><th className="p-1">历史提交</th><th className="p-1">权重</th><th className="p-1">相对概率</th></tr></thead><tbody>{record.candidates.map((item, index) => <tr key={`${index}:${item.direction}`} className="border-t border-border/40"><td className="p-1 break-all">{item.direction}{item.direction === record.picked ? ' ✓' : ''}</td><td className="p-1">{record.weighting === 'uniform_fallback' ? '未知' : item.attempts}</td><td className="p-1">{String(item.weight)}</td><td className="p-1">{(100 * item.weight / total).toFixed(2)}%</td></tr>)}</tbody></table></div>
  </section>;
};
