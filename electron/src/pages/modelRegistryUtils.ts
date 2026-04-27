import * as React from 'react';
import dayjs from 'dayjs';
import { Archive, CheckCircle2, Clock, XCircle } from 'lucide-react';
import { SystemModelRecord, UserModelRecord } from '../services/modelTrainingService';

export function systemModelToUserModel(s: SystemModelRecord): UserModelRecord {
  const perfTest  = s.performance_metrics?.test  ?? {};
  const perfValid = s.performance_metrics?.valid ?? {};
  const perfTrain = s.performance_metrics?.train ?? {};

  // 优先使用 API 已统一的平铺字段，回退旧格式（training_config.label）
  const labelFormula: string =
    (s as any).label_formula ?? String(s.training_config?.label ?? '');

  const targetHorizon: number = (() => {
    if ((s as any).target_horizon_days) return Number((s as any).target_horizon_days);
    const m = labelFormula.match(/-(\d+)/);
    return m ? parseInt(m[1]) : 5;
  })();

  const targetMode: string =
    (s as any).target_mode ??
    (s.model_type?.toLowerCase().includes('classif') ? 'classification' : 'return');

  return {
    tenant_id: 'system',
    user_id: 'system',
    model_id: s.model_id,
    source_run_id: '',
    status: 'active',
    storage_path: `models/production/${s.dir_name}`,
    model_file: s.files?.model_checkpoint ?? 'model.lgb',
    is_default: false,
    created_at: s.created_at ? new Date(s.created_at).toISOString() : undefined,
    updated_at: s.created_at ? new Date(s.created_at).toISOString() : undefined,
    activated_at: s.created_at ? new Date(s.created_at).toISOString() : undefined,
    metadata_json: {
      display_name:        s.display_name,
      model_name:          s.display_name,
      model_type:          s.model_type || s.framework,
      framework:           s.framework,
      feature_count:       s.feature_count,
      features:            s.feature_columns,
      feature_columns:     s.feature_columns,
      performance_metrics: s.performance_metrics,
      target_horizon_days: targetHorizon,
      target_mode:         targetMode,
      label_formula:       labelFormula,
      data_source:         (s as any).data_source ?? '',
      best_iteration:      (s as any).best_iteration,
      // 统一字段名（val_start/val_end 而非 valid_start/valid_end）
      train_start: s.train_start,
      train_end:   s.train_end,
      val_start:   s.valid_start,   // API 层已统一为 valid_start
      val_end:     s.valid_end,
      test_start:  s.test_start,
      test_end:    s.test_end,
      description: s.description,
      version:     s.version,
      algorithm:   s.algorithm,
      // metrics 平铺（与用户模型 metadata_json.metrics 保持一致）
      metrics: {
        train_ic:       perfTrain.mean_ic,
        train_rank_icir: perfTrain.icir,
        val_ic:         perfValid.mean_ic,
        val_rank_icir:  perfValid.icir,
        test_ic:        perfTest.mean_ic,
        test_rank_icir: perfTest.icir,
      },
    },
    metrics_json: {
      ic:       perfTest.mean_ic  ?? perfValid.mean_ic,
      icir:     perfTest.icir     ?? perfValid.icir,
      rank_ic:  perfTest.mean_ic  ?? perfValid.mean_ic,
      rank_icir: perfTest.icir    ?? perfValid.icir,
    },
  };
}

// ─── 辅助函数 ───────────────────────────────────────────────────────────────

export function getMeta(m: UserModelRecord) {
  return (m.metadata_json ?? {}) as Record<string, any>;
}
export function getMetrics(m: UserModelRecord) {
  const metrics = (m.metrics_json ?? {}) as Record<string, any>;
  const meta = getMeta(m);
  const metaMetrics = meta.metrics && typeof meta.metrics === 'object' ? meta.metrics as Record<string, any> : {};
  
  // 增加对嵌套 performance_metrics 的提取（针对某些个人模型或系统模型同步数据）
  const perf = meta.performance_metrics as any;
  const nestedMetrics: Record<string, any> = {};
  if (perf && typeof perf === 'object') {
    // 优先级调整：训练集最早进入，测试集最后进入以覆盖前面的键
    ['train', 'val', 'valid', 'test'].forEach(split => {
      if (perf[split] && typeof perf[split] === 'object') {
        Object.entries(perf[split]).forEach(([k, v]) => {
          // 这里的键名统一映射，如 mean_ic, icir, sharpe, annualized_return 等
          nestedMetrics[k] = v;
          nestedMetrics[`${split}_${k}`] = v;
          // 一些常见的别名处理
          if (k === 'ir') nestedMetrics.icir = v;
          if (k === 'return' || k === 'ret') nestedMetrics.annualized_return = v;
        });
      }
    });
  }

  return { ...metaMetrics, ...nestedMetrics, ...metrics };
}

export function resolveMetricNumber(source: Record<string, any> | null | undefined, keys: string[]) {
  if (!source) return null;
  for (const key of keys) {
    const value = source[key];
    if (value === null || value === undefined || value === '') continue;
    const num = Number(value);
    if (Number.isFinite(num)) return num;
  }
  return null;
}

export function formatSignedValue(value: number | null, digits = 4) {
  if (value === null) return '—';
  const rounded = value.toFixed(digits);
  return value > 0 ? `+${rounded}` : rounded;
}

export function formatTrendLabel(current: number | null, previous: number | null, digits = 4) {
  if (current === null) return '—';
  if (previous === null) return '基线';
  if (previous === 0) return '基线';
  const deltaPct = ((current - previous) / previous) * 100;
  const deltaLabel = formatSignedValue(deltaPct, digits);
  return `较上段 ${deltaLabel}%`;
}

// 全局模型 ID -> 友好名称映射（在此添加更多条目以支持特殊显示）
export const MODEL_ID_NAME_MAP: Record<string, string> = {
  'mdl_train_20260426163638_c40ed8ff_314e9cfc': '26_T5_Alpha51_Base',
};

export function modelIdToDisplayName(modelId?: string | null, fallback?: string) {
  if (!modelId) return fallback || '—';
  return MODEL_ID_NAME_MAP[modelId] || modelId;
}

export function modelDisplayName(m: UserModelRecord): string {
  const meta = getMeta(m);
  return modelIdToDisplayName(m.model_id, meta.display_name || m.model_id);
}
export function extractModelType(m: UserModelRecord): string {
  const raw = String(getMeta(m).model_type ?? '').toLowerCase();
  if (raw.includes('lgb') || raw.includes('lightgbm')) return '轻量级GBDT';
  if (raw.includes('xgb') || raw.includes('xgboost')) return '极端梯度提升';
  if (raw.includes('tft')) return '时序融合变换器';
  if (raw.includes('lstm')) return '长短期记忆网络';
  if (raw) return '自定义模型';
  return '模型';
}
export function isSystemModel(m: UserModelRecord) {
  return m.tenant_id === 'system';
}

export function parseTrainingWindowRanges(raw: unknown): Array<[string, string]> {
  if (typeof raw !== 'string' || !raw.trim()) return [];
  const text = raw.replaceAll('→', '->').replaceAll('—', '-');
  const segments = text.split('|').map((item) => item.trim()).filter(Boolean);
  const ranges: Array<[string, string]> = [];
  for (const segment of segments) {
    const matched = segment.match(/(\d{4}-\d{2}-\d{2})\s*->\s*(\d{4}-\d{2}-\d{2})/);
    if (matched) {
      ranges.push([matched[1], matched[2]]);
    }
  }
  return ranges;
}

export function getStatusConfig(status: string) {
  switch (status) {
    case 'active':
    case 'ready':
      return { color: 'text-emerald-600', bg: 'bg-emerald-50', border: 'border-emerald-200', label: '已就绪', icon: React.createElement(CheckCircle2, { size: 9 }) };
    case 'candidate':
      return { color: 'text-blue-600', bg: 'bg-blue-50', border: 'border-blue-200', label: '候选', icon: React.createElement(Clock, { size: 9 }) };
    case 'syncing':
      return { color: 'text-indigo-600', bg: 'bg-indigo-50', border: 'border-indigo-200', label: '已同步', icon: React.createElement(CheckCircle2, { size: 9 }) };
    case 'failed':
      return { color: 'text-red-500', bg: 'bg-red-50', border: 'border-red-200', label: '失败', icon: React.createElement(XCircle, { size: 9 }) };
    case 'archived':
      return { color: 'text-slate-400', bg: 'bg-slate-100', border: 'border-slate-200', label: '已归档', icon: React.createElement(Archive, { size: 9 }) };
    default:
      return { color: 'text-slate-400', bg: 'bg-slate-100', border: 'border-slate-200', label: status || '未知', icon: React.createElement(Clock, { size: 9 }) };
  }
}

export function extractTimePeriods(meta: Record<string, any>) {
  const windowRanges = parseTrainingWindowRanges(meta.training_window ?? meta.trainingWindow);
  const ts = meta.train_start ?? meta.trainStart ?? windowRanges[0]?.[0];
  const te = meta.train_end ?? meta.trainEnd ?? windowRanges[0]?.[1];
  if (!ts || !te) return null;
  const vs = meta.val_start ?? meta.valStart ?? windowRanges[1]?.[0];
  const ve = meta.val_end ?? meta.valEnd ?? windowRanges[1]?.[1];
  const xs = meta.test_start ?? meta.testStart ?? windowRanges[2]?.[0];
  const xe = meta.test_end ?? meta.testEnd ?? windowRanges[2]?.[1];
  return {
    train: [String(ts).slice(0, 10), String(te).slice(0, 10)] as [string, string],
    val: vs && ve ? [String(vs).slice(0, 10), String(ve).slice(0, 10)] as [string, string] : null,
    test: xs && xe ? [String(xs).slice(0, 10), String(xe).slice(0, 10)] as [string, string] : null,
  };
}

export function calcTimeSplitStats(p: NonNullable<ReturnType<typeof extractTimePeriods>>) {
  const duration = (a: string, b: string) => Math.max(dayjs(b).diff(dayjs(a), 'day'), 1);
  const trainDays = duration(p.train[0], p.train[1]);
  const valDays = p.val ? duration(p.val[0], p.val[1]) : 0;
  const testDays = p.test ? duration(p.test[0], p.test[1]) : 0;
  const totalDays = trainDays + valDays + testDays || 1;
  const percentOf = (days: number) => Math.round((days / totalDays) * 100);

  return {
    totalDays,
    train: {
      days: trainDays,
      percent: percentOf(trainDays),
    },
    val: p.val
      ? {
          days: valDays,
          percent: percentOf(valDays),
        }
      : null,
    test: p.test
      ? {
          days: testDays,
          percent: percentOf(testDays),
        }
      : null,
  };
}
