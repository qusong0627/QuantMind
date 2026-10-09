/**
 * 模型融合（机构级 v2）展示层纯函数。
 *
 * 只做「后端码 → 人话」的映射与格式化：未知警告码原样透出，绝不编造解释；
 * 缺失值一律显示「—」，绝不显示成 0（0 是真实权重/真实 IC 的合法值）。
 */
import type {
  FusionDiagnosticRow,
  FusionReplayVerdict,
  UserModelRecord,
} from '../services/modelTrainingService';
import { getMeta } from './modelRegistryUtils';

/** 已知警告码 → 人话（backend: fusion_weights / fusion_orchestrator 产码）。 */
const WARNING_TEXT: Record<string, string> = {
  all_insufficient_days_fallback_equal:
    '成员 IC 覆盖天数均不足，本次暂按等权融合（样本积累后自动倾斜）',
  all_icir_nonpositive_fallback_equal:
    '成员 ICIR 均非正——单成员倾斜无统计优势，本次按等权融合',
};

export function describeFusionWarning(warning: string): string {
  const w = String(warning || '').trim();
  if (!w) return '';
  if (WARNING_TEXT[w]) return WARNING_TEXT[w];
  if (w.startsWith('horizon_mismatch')) {
    return `成员持有周期不一致（${w.split(':').slice(1).join(':').trim()} 天）——融合按众数周期评估`;
  }
  if (w.startsWith('evidence_unavailable')) {
    return `成员证据不可得，已降级为等权预览：${w.split(':').slice(1).join(':').trim()}`;
  }
  if (w.startsWith('replay_failed')) {
    return `OOS 回放失败（不阻断创建）：${w.split(':').slice(1).join(':').trim()}`;
  }
  if (w.startsWith('mixed_data_source')) {
    return `成员数据面不一致（${w}）——融合取数按多数成员的数据源`;
  }
  if (w.startsWith('mixed_quantdb_dir')) {
    return `成员 QuantDB 目录不一致（${w}）——融合取数按多数成员的目录`;
  }
  if (w.startsWith('factor_field_sources_conflict')) {
    return `成员特征字段映射存在分歧（${w}）——按多数票合并`;
  }
  // 后端中文自由文本警告（如「成员 X 窗口内无分数桶数据」）与未知码：原样透出
  return w;
}

/** 诊断 reason → 徽标文案（空串 = 无异常）。 */
export function describeDiagnosticReason(reason: string): string {
  switch (String(reason || '')) {
    case 'insufficient_days':
      return '样本不足';
    case 'nonpositive_icir':
      return 'ICIR≤0';
    case 'duplicate':
      return '近似重复';
    default:
      return '';
  }
}

export function formatWeightPct(w: number | null | undefined): string {
  if (w === null || w === undefined || !Number.isFinite(Number(w))) return '—';
  return `${(Number(w) * 100).toFixed(1)}%`;
}

export function formatMetric(v: number | null | undefined, digits = 3): string {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—';
  return Number(v).toFixed(digits);
}

export type FusionVerdictLevel = 'beats_best' | 'beats_median' | 'below_median' | 'unknown';

/** OOS 结论分级：优于最优成员 / 仅优于中位 / 低于中位 / 证据不足。 */
export function classifyVerdict(verdict: FusionReplayVerdict | null | undefined): FusionVerdictLevel {
  if (!verdict || !Number.isFinite(Number(verdict.fused_icir))) return 'unknown';
  if (verdict.beats_best) return 'beats_best';
  if (verdict.beats_median) return 'beats_median';
  return 'below_median';
}

export function isEnsembleModel(m: UserModelRecord): boolean {
  const meta = getMeta(m);
  return Boolean(meta.is_ensemble) || String(meta.model_type || '').toLowerCase() === 'ensemble';
}

export function ensembleMemberIds(m: UserModelRecord): string[] {
  const ids = getMeta(m).source_model_ids;
  return Array.isArray(ids) ? ids.map(String) : [];
}

/** 诊断行按成员对齐（preview.members 与 diagnostics 两路取参的粘合）。 */
export function diagnosticFor(
  diagnostics: FusionDiagnosticRow[],
  memberId: string,
): FusionDiagnosticRow | null {
  return diagnostics.find((d) => d.member_id === memberId) ?? null;
}
