import React from 'react';
import type { FactorJob } from '../services-v2/factorJobsApi';
import { formatNumber } from '../utils-v2';
import { Badge } from './ui/Badge';

const REPLACEMENT_REASONS: Record<string, string> = {
  ok: '满足单命中替换条件',
  not_single_hit: '重复命中数量不为一，不能替换',
  not_owned: '对照不属于本人的已发布特征，不能替换',
  incumbent_not_active: '原特征已归档或未发布，不能替换',
  new_ic_missing: '新因子的 IC 证据不足',
  incumbent_ic_missing: '原因子的 IC 证据不足',
  ic_below_floor: '新因子的绝对 IC 未达下限',
  ratio_below: '新因子的绝对 IC 未达替换倍数要求',
};

export const MaterializationResult: React.FC<{ job: FactorJob }> = ({ job }) => {
  if (job.status !== 'completed') return null;
  const result = job.result_snapshot;
  const rejected = result?.status === 'rejected_duplicate';
  const replaced = result?.status === 'replaced';
  const published = Boolean(result?.generation_id && result.library_gate && (!result.status || result.status === 'materialized' || replaced));
  const gate = rejected ? result : result?.library_gate;
  const evidence = gate?.evidence;
  const opponent = evidence?.opponent;
  const replacement = evidence?.replacement;
  return <div className="space-y-2 text-xs text-muted-foreground">
    <Badge variant={rejected ? 'warning' : published ? 'success' : 'outline'}>
      {rejected ? '重复拒绝，未发布' : published ? replaced ? '已替换发布' : '已发布' : '发布裁决未知'}
    </Badge>
    {!gate && <p>历史作业缺少查重裁决，请查看物化版本记录确认发布状态。</p>}
    {gate && <>
      {evidence?.automatic_check === 'skipped'
        ? <p>物化时跳过重复性检查；因子已发布，可在训练选因子时手动筛除重复项。</p>
        : <p>最大绝对相关性 {formatNumber(gate.corr, 4)} · 拒绝阈值 {formatNumber(evidence?.corr_threshold, 2)} · 样本 {evidence?.sample_days ?? '—'} 天 / 最少 {evidence?.min_sample_days ?? '—'} 天 · 对照 {evidence?.control_columns ?? '—'} 列</p>}
      {(opponent || gate.corr_against) && <p className="break-all">对照：{opponent?.kind === 'user_rd_mined' ? '本人已物化因子' : opponent?.kind === 'factory_l1' ? '工厂 L1 因子' : opponent?.kind || '来源未知'} · {opponent?.factor_id || gate.corr_against || '—'}</p>}
      {evidence?.corr_unverified && <p className="text-warning">相关性未完成核验；可能缺少对照或有效样本，不能据此认定已通过完整查重。</p>}
      {Boolean(evidence?.factory_skipped) && <p className="text-warning">工厂对照读取未完成，本次查重覆盖不完整。</p>}
      {replacement && <>
        <p>{REPLACEMENT_REASONS[replacement.decision || ''] || replacement.decision || '替换裁决未知'} · 命中 {replacement.hits?.length ?? '—'} 列</p>
        <p>新因子 |IC| {formatNumber(result?.new_ic ?? replacement.new_ic, 4)} · 原因子 |IC| {formatNumber(result?.incumbent_ic ?? replacement.incumbent_ic, 4)} · 下限 {formatNumber(evidence?.ic_floor, 2)} · 倍数 {formatNumber(evidence?.ic_ratio, 2)}</p>
      </>}
    </>}
    {result?.generation_id && <p className="break-all">{rejected ? '拒绝版本' : '物化版本'}：{result.generation_id}</p>}
    {(typeof result?.row_count === 'number' || typeof result?.byte_count === 'number' || typeof result?.partition_count === 'number') && (
      <p className="text-[11px]">
        {typeof result?.row_count === 'number' ? result.row_count.toLocaleString('en-US') : '—'} 行
        {' · '}{typeof result?.partition_count === 'number' ? result.partition_count : '—'} 列分区
        {' · '}{typeof result?.byte_count === 'number' ? (result.byte_count / 1048576).toFixed(2) : '—'} MiB
        {' · '}版本 {typeof result?.generation_id === 'string' ? result.generation_id.slice(0, 8) : '—'}
      </p>
    )}
    {replaced && <p className="break-all">已替换因子：{result?.replaced_factor_id || '—'} · 原版本：{result?.replaced_generation_id || '—'}</p>}
  </div>;
};
