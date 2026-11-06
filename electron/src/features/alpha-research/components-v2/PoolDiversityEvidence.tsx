import React from 'react';
import type { PoolResearchResult } from '../services-v2/poolResearchApi';

const REASONS: Record<string, string> = {
  source_numeric_dependency_missing: '旧作业尚无多样性计算；历史结果不会自动重算',
  requires_2_to_100_unique_members: '需要 2 至 100 个不同的池成员',
  incomplete_member_panels: '部分成员缺少可核验的面板；未剔除它们计算子池结果',
  bad_panel_columns: '成员面板缺少必要的列',
  duplicate_panel_observations: '面板存在重复日期与股票观测',
  panel_row_limit_exceeded: '完整面板超出单次计算范围',
  no_complete_case_observations: '没有全体成员共同具备有限数值的观测',
  insufficient_common_days: '全体成员的共同有效日期不足',
  nonfinite_daily_correlation: '日相关矩阵含无效数值',
  invalid_correlation_spectrum: '相关矩阵无法生成有效的谱度量',
};
const metric = (value: number | null | undefined) => typeof value === 'number' && Number.isFinite(value)
  ? (value !== 0 && Math.abs(value) < 0.00001 ? value.toExponential(4) : value.toFixed(6)) : '未知';

export const PoolDiversityEvidence: React.FC<{ result: PoolResearchResult }> = ({ result }) => {
  const evidence = result.diversity;
  if (!evidence) return <p className="text-xs text-muted-foreground">该历史作业没有多样性证据。</p>;
  return <section className="rounded border border-border/60 p-3 text-xs space-y-2">
    <h4 className="font-medium">全池多样性 · {evidence.status === 'computed' ? '已计算' : '不可用'}</h4>
    <p className="text-muted-foreground">使用全体成员共同观测的逐日相关矩阵，按谱熵计算有效因子数；每日至少 30 只股票、至少 20 个有效日。该指标描述信息结构，不是盈利能力评分。</p>
    {evidence.status === 'computed' ? <>
      <p>有效因子数 {metric(evidence.effective_factors)} / {result.factor_count} · 谱熵 {metric(evidence.entropy)} · 归一化熵 {metric(evidence.normalized_entropy)}</p>
      <p>共同有效日 {evidence.n_days} · 完整观测 {evidence.n_obs} · {evidence.start_date} 至 {evidence.end_date}</p>
      <p className="text-muted-foreground">留一贡献 = 全池有效因子数 − 去掉该成员后的有效因子数；始终使用同一批共同观测。数值可以为负，不等于该因子无效。清理建议会在全池版本仍一致时引用这份证据。</p>
      <div className="max-h-64 overflow-auto"><table className="w-full text-left"><thead><tr><th className="p-1">因子</th><th className="p-1">移除后有效因子数</th><th className="p-1">留一贡献</th></tr></thead><tbody>{result.results.map((row) => <tr key={row.factor_id} className="border-t border-border/40"><td className="p-1 break-all">{row.factor_name}</td><td className="p-1">{metric(evidence.contributions?.[row.factor_id]?.effective_without)}</td><td className="p-1">{metric(evidence.contributions?.[row.factor_id]?.delta)}</td></tr>)}</tbody></table></div>
    </> : <p>{REASONS[evidence.reason || ''] || '该作业未形成可用的多样性数值。'}</p>}
    {evidence.implementation_origin && <p className="text-muted-foreground">此项采用明确公式重新实现，未取得源项目依赖的原始计算脚本，数值不承诺与其相同。</p>}
  </section>;
};
