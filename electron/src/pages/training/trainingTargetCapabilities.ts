/**
 * 训练目标能力矩阵（前端）——纯函数，便于 vitest 直接覆盖。
 *
 * 后端 `GET /api/v1/models/training/capabilities` 是唯一事实源；接口失败时
 * 回退为“全部可选”，由提交侧 422 兜底（不允许前端静默放宽后端严格门）。
 */
import type { LabelTransform, ModelType, TargetMode, TrainingTarget } from './trainingUtils';

export interface ModelCapabilityRow {
  model_type: string;
  family: string;
  regression: boolean;
  classification: boolean;
  quantile: boolean;
  rank_loss: boolean;
  sample_weight: boolean;
  group_by_date: boolean;
  sequence: boolean;
}

export interface TrainingCapabilities {
  matrix_version: number;
  models: ModelCapabilityRow[];
  target_modes: {
    return: string[];
    classification: string[];
    quantile: string[];
  };
  rank_objectives?: Record<string, string[]>;
  binary_objectives?: Record<string, string[]>;
}

export interface LabelTargetSummary {
  id: string;
  version: number;
  market: string;
  family: string;
  mode: string;
  status: string;
  spec_hash: string;
  spec: Record<string, unknown>;
  created_by?: string | null;
  activated_at?: string | null;
  note?: string | null;
}

export interface TargetCapabilityView {
  regression: boolean;
  classification: boolean;
  quantile: boolean;
  rank_loss: boolean;
  sample_weight: boolean;
  group_by_date: boolean;
  sequence: boolean;
}

const ALL_TRUE: TargetCapabilityView = {
  regression: true,
  classification: true,
  quantile: true,
  rank_loss: true,
  sample_weight: true,
  group_by_date: true,
  sequence: true,
};

const MODEL_ORDER: ModelType[] = [
  'lightgbm', 'xgboost', 'catboost', 'linear', 'random_forest', 'mlp',
  'gru', 'lstm', 'alstm', 'transformer', 'tabnet', 'tcn', 'nativetft',
];

const rowFor = (
  capabilities: TrainingCapabilities | null | undefined,
  modelType: string,
): ModelCapabilityRow | null => {
  const row = capabilities?.models?.find((item) => item.model_type === modelType);
  return row ?? null;
};

export const intersectModelCapabilities = (
  modelTypes: string[],
  capabilities?: TrainingCapabilities | null,
): TargetCapabilityView => {
  const names = (modelTypes.length ? modelTypes : ['lightgbm']).filter(Boolean);
  if (!capabilities?.models?.length) return ALL_TRUE;
  const out: TargetCapabilityView = { ...ALL_TRUE };
  for (const name of names) {
    const row = rowFor(capabilities, name);
    if (!row) continue; // 未知模型交给后端 422，不在前端放宽成“不支持”
    out.regression = out.regression && row.regression;
    out.classification = out.classification && row.classification;
    out.quantile = out.quantile && row.quantile;
    out.rank_loss = out.rank_loss && row.rank_loss;
    out.sample_weight = out.sample_weight && row.sample_weight;
    out.group_by_date = out.group_by_date && row.group_by_date;
    out.sequence = out.sequence && row.sequence;
  }
  return out;
};

export const supportedTargetModes = (
  modelTypes: string[],
  capabilities?: TrainingCapabilities | null,
): TargetMode[] => {
  const view = intersectModelCapabilities(modelTypes, capabilities);
  const modes: TargetMode[] = [];
  if (view.regression) modes.push('return');
  if (view.classification) modes.push('classification');
  return modes;
};

export const unsupportedModelsForMode = (
  modelTypes: string[],
  mode: TargetMode,
  capabilities?: TrainingCapabilities | null,
): string[] => {
  if (!capabilities?.models?.length) return [];
  const field = mode === 'classification' ? 'classification' : 'regression';
  return (modelTypes.length ? modelTypes : ['lightgbm']).filter((name) => {
    const row = rowFor(capabilities, name);
    return row ? !Boolean(row[field]) : false;
  });
};

export const targetModeUnsupportedReason = (
  modelTypes: string[],
  mode: TargetMode,
  capabilities?: TrainingCapabilities | null,
): string | null => {
  const bad = unsupportedModelsForMode(modelTypes, mode, capabilities);
  if (!bad.length) return null;
  return `${bad.join('、')} 不支持${mode === 'classification' ? '分类' : '回归'}目标`;
};

export const targetRefKey = (ref?: { id: string; version: number } | null): string =>
  ref ? `${ref.id}@${ref.version}` : 'legacy';

export const findTargetByKey = (
  targets: LabelTargetSummary[] | undefined,
  key: string,
): LabelTargetSummary | null => {
  if (!key || key === 'legacy') return null;
  return (targets ?? []).find((item) => targetRefKey(item) === key) ?? null;
};

export const familyOfTargetMode = (mode: TargetMode): string =>
  mode === 'classification' ? 'classification_binary' : 'forward_return';

const asNumber = (value: unknown, fallback: number): number => {
  const num = Number(value);
  return Number.isFinite(num) ? num : fallback;
};

/** 选中注册表目标后，把 spec 的冻结口径灌进表单。 */
export const applyTargetSpecToTarget = (
  target: TrainingTarget,
  spec: Record<string, unknown>,
): TrainingTarget => {
  const family = String(spec.family || 'forward_return');
  const entry = (spec.entry || {}) as Record<string, unknown>;
  const transform = (spec.transform || {}) as Record<string, unknown>;
  const mode: TargetMode = family === 'classification_binary' ? 'classification' : 'return';
  return {
    ...target,
    mode,
    horizonDays: Math.min(30, Math.max(1, asNumber(spec.horizon_days, target.horizonDays))),
    executionPrice: entry.price === 'close' ? 'close' : 'open',
    labelTransform: String(transform.type || 'rank') as LabelTransform,
    labelTopPct: Math.min(0.5, Math.max(0.05, asNumber(transform.top_pct, target.labelTopPct))),
    labelDropExtremePct: Math.min(
      0.2,
      Math.max(0, asNumber(transform.drop_extreme_pct, target.labelDropExtremePct)),
    ),
  };
};

/** 选中目标后允许覆盖的字段（与后端 OVERRIDE_WHITELIST 同口径）。 */
export const targetOverrideWhitelist = (family: string): Set<string> => {
  if (family === 'classification_binary') return new Set(['horizon_days']);
  if (family === 'quantile_return') return new Set<string>();
  return new Set(['horizon_days', 'label_transform', 'label_top_pct', 'label_drop_extreme_pct']);
};

/**
 * 只发送“与冻结 spec 不同”的覆盖项；没有差异返回 undefined。
 * 目的：metadata 的 overrides 反映用户真实改动，而不是每次全量覆盖。
 */
export const buildTargetOverrides = (
  target: TrainingTarget,
  spec?: Record<string, unknown> | null,
): Record<string, unknown> | undefined => {
  if (!spec) return undefined;
  const family = String(spec.family || 'forward_return');
  const allowed = targetOverrideWhitelist(family);
  if (!allowed.size) return undefined;
  const entry = (spec.entry || {}) as Record<string, unknown>;
  const transform = (spec.transform || {}) as Record<string, unknown>;
  const overrides: Record<string, unknown> = {};

  if (allowed.has('horizon_days') && target.horizonDays !== asNumber(spec.horizon_days, target.horizonDays)) {
    overrides.horizon_days = target.horizonDays;
  }
  if (allowed.has('label_transform') && target.labelTransform !== String(transform.type || 'rank')) {
    overrides.label_transform = target.labelTransform;
  }
  if (allowed.has('label_top_pct') && Number(target.labelTopPct) !== Number(transform.top_pct ?? 0.2)) {
    overrides.label_top_pct = target.labelTopPct;
  }
  if (
    allowed.has('label_drop_extreme_pct') &&
    Number(target.labelDropExtremePct) !== Number(transform.drop_extreme_pct ?? 0)
  ) {
    overrides.label_drop_extreme_pct = target.labelDropExtremePct;
  }
  return Object.keys(overrides).length ? overrides : undefined;
};

/** 目标族对表单的锁定：quantile 不允许覆盖；classification 不允许变换/极端清洗。 */
export const targetFormLocks = (spec?: Record<string, unknown> | null) => {
  const family = String(spec?.family || 'forward_return');
  return {
    lockHorizon: family === 'quantile_return',
    lockTransform: family === 'classification_binary' || family === 'quantile_return',
    hideDropExtreme: family === 'classification_binary',
    hideTopPct: family !== 'forward_return',
  };
};

export const capabilityModelTypes = (params: {
  model_type?: string;
  model_types?: string[];
}): string[] => {
  const types = (params.model_types ?? []).filter(Boolean);
  if (types.length) return types;
  return params.model_type ? [params.model_type] : ['lightgbm'];
};

export const modelOrder = (): ModelType[] => [...MODEL_ORDER];
