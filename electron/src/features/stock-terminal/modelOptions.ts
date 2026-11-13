/**
 * 候选信号「推理模型」下拉选项构建。
 *
 * 后端 `/stock-terminal/list` 回传的 `models` 现在包含**注册表全量（非归档）**，
 * 其中部分模型近 90 天没跑过推理。直接铺平会让用户以为「模型漏了」，所以：
 * - `has_signals_90d !== false` 的项排在前（有信号 → 近 30 天无信号加后缀）；
 * - `has_signals_90d === false` 的项放进「近 90 天无信号」分组，仍然可选；
 * - 老后端没有 `has_signals_90d` 字段 → 保持平铺与旧后缀，不臆造分组。
 */

import type { StockModelOption } from '../stock-terminal-shared/types';

export interface ModelSelectOption {
  label: string;
  value: string;
}

export interface ModelSelectGroup {
  label: string;
  options: ModelSelectOption[];
}

/** 单个选项的文案（不含分组标题）。 */
export function modelOptionLabel(m: StockModelOption): string {
  const name = m.display_name || m.model_id;
  // 无 90 天信号的项已落在分组标题下，后缀由标题交代，避免重复。
  if (m.has_signals_90d === false) return name;
  return `${name}${m.has_signals_30d === false ? ' · 近 30 天无信号' : ''}`;
}

/** 构建 antd Select 的 options（平铺数组或分组数组）。 */
export function buildModelSelectOptions(
  models: StockModelOption[],
): ModelSelectOption[] | ModelSelectGroup[] {
  const toOption = (m: StockModelOption): ModelSelectOption => ({
    label: modelOptionLabel(m),
    value: m.model_id,
  });

  const withoutSignals = models.filter((m) => m.has_signals_90d === false);
  // 老后端无 has_signals_90d → 全部按「有信号」处理，保持旧渲染
  if (withoutSignals.length === 0) return models.map(toOption);

  const withSignals = models.filter((m) => m.has_signals_90d !== false);
  const groups: ModelSelectGroup[] = [];
  if (withSignals.length > 0) {
    groups.push({
      label: `近 90 天有信号（${withSignals.length}）`,
      options: withSignals.map(toOption),
    });
  }
  groups.push({
    label: `近 90 天无信号（${withoutSignals.length}）`,
    options: withoutSignals.map(toOption),
  });
  return groups;
}

/**
 * 个股终端「默认模型」下拉选项（与候选信号页的模型下拉分开）。
 *
 * 关键差异：
 * - `value=''` 代表默认模型，**不能**用字面量 `'default'`（那会被当作 model_id
 *   发给后端，默认模型反而失效）；
 * - 选项带真实训练周期 `T+N` 与 pred 产物可用性；无 pred 的模型置灰但仍可见，
 *   用户能分清「没有这款」与「有但没产物」。
 */
export interface TerminalModelMeta {
  model_id: string;
  display_name?: string;
  horizon?: number | null;
  is_default?: boolean;
  has_pred?: boolean;
  /** 精确最后分数日（后端读 Parquet footer 统计，毫秒级；缺失时前端显示 —） */
  pred_last_date?: string | null;
}

export interface TerminalModelOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export function buildTerminalModelOptions(
  models: TerminalModelMeta[],
  defaultModelName?: string,
): TerminalModelOption[] {
  const defaultOption: TerminalModelOption = {
    value: '',
    label: defaultModelName ? `默认模型 · ${defaultModelName}` : '默认模型',
  };
  return [
    defaultOption,
    ...models.map((m) => ({
      value: m.model_id,
      label: `${m.display_name || m.model_id}${m.horizon ? ` · T+${m.horizon}` : ''}${
        m.is_default ? ' · 默认' : ''
      }${
        m.has_pred === false
          ? ' · 无 pred'
          : m.pred_last_date
            ? ` · 分数至 ${m.pred_last_date.slice(5)}`
            : ''
      }`,
      ...(m.has_pred === false ? { disabled: true } : {}),
    })),
  ];
}
