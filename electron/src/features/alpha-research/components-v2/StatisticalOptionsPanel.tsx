import React, { useState } from 'react';
import type { StatisticalMiningOptions } from '../types-v2';

const numberFields = [
  { key: 'top_k', label: '最多入选因子', min: 1, max: 32, step: 1 },
  { key: 'min_names_per_day', label: '每日最少标的', min: 3, max: 10000, step: 1 },
  { key: 'min_valid_days', label: '样本内最少有效日', min: 5, max: 5000, step: 1 },
  { key: 'quantiles', label: '分组数量', min: 2, max: 20, step: 1 },
  { key: 'fdr_q', label: 'FDR 显著性阈值', min: 0, max: 1, step: 0.01, exclusiveMin: true },
  { key: 'oos_min_ratio', label: 'OOS 最低 IC 保留比例', min: 0, max: 2, step: 0.05 },
  { key: 'oos_min_days', label: 'OOS 最少有效日', min: 1, max: 5000, step: 1 },
  { key: 'stability_folds', label: '稳定性子窗数量', min: 2, max: 20, step: 1 },
  { key: 'min_fold_sign_consistency', label: '子窗最低同号比例', min: 0, max: 1, step: 0.05, exclusiveMin: true },
  { key: 'min_fold_days', label: '子窗最少有效日', min: 1, max: 1000, step: 1 },
  { key: 'dedup_corr_warn', label: '相关性提示阈值', min: 0, max: 1, step: 0.01, exclusiveMax: true },
  { key: 'dedup_corr_reject', label: '相关性拒绝阈值', min: 0, max: 1, step: 0.01, exclusiveMin: true },
  { key: 'composite_z_window', label: '合成标准化窗口', min: 2, max: 250, step: 1 },
  { key: 'net_ic_cost_rate', label: '净 IC 成本率（小数）', min: 0, max: 1, step: 0.0001 },
] as const;

const booleanFields = [
  { key: 'require_fdr', label: '要求通过 FDR' },
  { key: 'require_stability', label: '要求子窗稳定' },
  { key: 'require_dedup', label: '按相关性去重' },
  { key: 'composite_enabled', label: '生成 IC 加权合成因子' },
] as const;

const tradabilityFields = [
  { key: 'min_list_days', label: '最少上市交易日', min: 0, max: 5000, step: 1 },
  { key: 'limit_tolerance', label: '涨跌停容差', min: 0, max: 0.05, step: 0.001 },
  { key: 'min_amount_quantile', label: '剔除成交额末端比例', min: 0, max: 1, step: 0.05, exclusiveMax: true },
  { key: 'min_amount_window', label: '成交额均值窗口', min: 1, max: 250, step: 1 },
] as const;

export function effectiveStatisticalOptions(
  defaults: StatisticalMiningOptions | undefined,
  overrides: StatisticalMiningOptions | undefined,
): StatisticalMiningOptions | undefined {
  if (!defaults && !overrides) return undefined;
  const defined = (value: object | undefined) => Object.fromEntries(
    Object.entries(value ?? {}).filter(([, item]) => item !== undefined),
  );
  return {
    ...defaults,
    ...defined(overrides),
    tradability: { ...defaults?.tradability, ...defined(overrides?.tradability) },
  };
}

export function statisticalOptionsError(value: StatisticalMiningOptions): string | null {
  if (value.horizons && (
    value.horizons.length < 1 || value.horizons.length > 8
    || new Set(value.horizons).size !== value.horizons.length
    || value.horizons.some((item) => !Number.isInteger(item) || item < 1 || item > 250)
  )) return '预测周期须为 1–250 的不重复整数，最多八项。';
  for (const field of numberFields) {
    const item = value[field.key];
    if (item === undefined || item === null) continue;
    if (!Number.isFinite(item) || item < field.min || item > field.max
      || ('exclusiveMin' in field && item === field.min)
      || ('exclusiveMax' in field && item === field.max)
      || (field.step === 1 && !Number.isInteger(item))) {
      return `${field.label}须在 ${field.min}–${field.max} 之间${field.step === 1 ? '且为整数' : ''}。`;
    }
  }
  if (value.dedup_corr_warn !== undefined && value.dedup_corr_reject !== undefined
    && value.dedup_corr_warn >= value.dedup_corr_reject) {
    return '相关性提示阈值必须小于拒绝阈值。';
  }
  for (const field of tradabilityFields) {
    const item = value.tradability?.[field.key];
    if (item === undefined) continue;
    if (!Number.isFinite(item) || item < field.min || item > field.max
      || ('exclusiveMax' in field && item === field.max)
      || (field.step === 1 && !Number.isInteger(item))) {
      return `${field.label}须在 ${field.min}–${field.max} 之间${field.step === 1 ? '且为整数' : ''}。`;
    }
  }
  return null;
}

const inputClass = 'w-full rounded border border-slate-200 bg-white px-2 py-1 text-slate-700 disabled:opacity-50';

export const StatisticalOptionsPanel: React.FC<{
  value: StatisticalMiningOptions;
  disabled: boolean;
  onChange: (value: StatisticalMiningOptions) => void;
}> = ({ value, disabled, onChange }) => {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const error = statisticalOptionsError(value);
  const setDraft = (key: string, text: string) => setDrafts((current) => ({ ...current, [key]: text }));
  return (
    <details className="border-t border-slate-100 px-4 py-2 text-xs">
      <summary className="cursor-pointer font-semibold text-slate-600">统计挖掘参数</summary>
      <fieldset disabled={disabled} className="mt-3 max-h-[45vh] space-y-3 overflow-y-auto pr-1">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <label className="space-y-1 text-slate-500">预测周期（交易日，逗号分隔）
            <input aria-label="预测周期" className={inputClass} value={drafts.horizons ?? value.horizons?.join(', ') ?? ''}
              onChange={(event) => {
                const text = event.target.value;
                setDraft('horizons', text);
                onChange({ ...value, horizons: text.trim() ? text.split(/[,，\s]+/).map((item) => item ? Number(item) : Number.NaN) : undefined });
              }} />
          </label>
          <label className="space-y-1 text-slate-500">中性化
            <select aria-label="中性化" className={inputClass} value={value.neutralize ?? 'industry'} onChange={(event) => onChange({ ...value, neutralize: event.target.value as StatisticalMiningOptions['neutralize'] })}>
              <option value="none">不进行中性化</option><option value="industry">行业中性</option><option value="industry_size">行业与市值中性</option>
            </select>
          </label>
          {numberFields.map((field) => (
            <label key={field.key} className="space-y-1 text-slate-500">{field.label}
              <input aria-label={field.label} className={inputClass} type="number" min={field.min} max={field.max} step={field.step}
                placeholder={field.key === 'net_ic_cost_rate' ? '使用算法默认成本' : '使用服务器默认值'}
                value={drafts[field.key] ?? value[field.key] ?? ''} onChange={(event) => {
                  const text = event.target.value;
                  setDraft(field.key, text);
                  onChange({ ...value, [field.key]: text === '' ? undefined : Number(text) });
                }} />
            </label>
          ))}
        </div>
        <div className="flex flex-wrap gap-3">
          {booleanFields.map((field) => <label key={field.key} className="flex items-center gap-1 text-slate-600">
            <input type="checkbox" checked={value[field.key] ?? true} onChange={(event) => onChange({ ...value, [field.key]: event.target.checked })} />{field.label}
          </label>)}
        </div>
        <p className="text-slate-400">成本率以小数填写，例如 0.001 为千分之一；留空使用算法默认成本。OOS 门槛仅在填写检验区间后应用。</p>
        <div className="border-t border-slate-100 pt-3">
          <p className="mb-2 font-semibold text-slate-600">可交易性过滤</p>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {tradabilityFields.map((field) => <label key={field.key} className="space-y-1 text-slate-500">{field.label}
              <input aria-label={field.label} className={inputClass} type="number" min={field.min} max={field.max} step={field.step}
                placeholder="使用服务器默认值" value={drafts[field.key] ?? value.tradability?.[field.key] ?? ''} onChange={(event) => {
                  const text = event.target.value;
                  setDraft(field.key, text);
                  onChange({ ...value, tradability: { ...value.tradability, [field.key]: text === '' ? undefined : Number(text) } });
                }} />
            </label>)}
          </div>
          <div className="mt-3 flex flex-wrap gap-3">
            {([{ key: 'exclude_suspended', label: '剔除停牌和无成交样本' }, { key: 'exclude_limit_locked', label: '剔除收盘涨跌停样本' }] as const).map((field) => (
              <label key={field.key} className="flex items-center gap-1 text-slate-600"><input type="checkbox" checked={value.tradability?.[field.key] ?? true} onChange={(event) => onChange({ ...value, tradability: { ...value.tradability, [field.key]: event.target.checked } })} />{field.label}</label>
            ))}
          </div>
          <p className="mt-2 text-slate-400">成交额末端比例为 0 时不做流动性剔除；ST 当前快照不代表历史 ST 状态。</p>
        </div>
        {error && <p role="alert" className="text-red-600">{error}</p>}
      </fieldset>
    </details>
  );
};

export const StatisticalOptionsSummary: React.FC<{ value: StatisticalMiningOptions }> = ({ value }) => (
  <details className="mt-3 rounded-lg border border-slate-200 p-3 text-xs text-slate-600" open>
    <summary className="cursor-pointer font-semibold">实际统计参数</summary>
    <dl className="mt-2 grid grid-cols-[1fr_1fr] gap-2">
      <dt>预测周期（交易日）</dt><dd>{value.horizons?.join('、') || '默认'}</dd>
      <dt>中性化</dt><dd>{({ none: '无', industry: '行业', industry_size: '行业与市值' })[value.neutralize ?? 'industry']}</dd>
      {numberFields.map((field) => <React.Fragment key={field.key}><dt>{field.label}</dt><dd>{value[field.key] ?? '算法默认值'}</dd></React.Fragment>)}
      {booleanFields.map((field) => <React.Fragment key={field.key}><dt>{field.label}</dt><dd>{value[field.key] ? '开启' : '关闭'}</dd></React.Fragment>)}
      {tradabilityFields.map((field) => <React.Fragment key={field.key}><dt>{field.label}</dt><dd>{value.tradability?.[field.key] ?? '默认'}</dd></React.Fragment>)}
      <dt>剔除停牌和无成交</dt><dd>{value.tradability?.exclude_suspended ? '开启' : '关闭'}</dd>
      <dt>剔除收盘涨跌停</dt><dd>{value.tradability?.exclude_limit_locked ? '开启' : '关闭'}</dd>
      <dt>ST 当前快照标的数</dt><dd>{value.tradability?.st_snapshot_symbols?.length ?? 0}（不代表历史状态）</dd>
    </dl>
  </details>
);
