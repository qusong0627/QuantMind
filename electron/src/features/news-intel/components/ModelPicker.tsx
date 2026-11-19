/** 模型选择：显示备注名，切换后重载推理榜单。 */

import { Select } from 'antd';
import type { MarketKey } from '../services/newsIntelService';

export interface ModelOption {
  id: string;
  name: string;
  market: MarketKey | string;
  source: 'user' | 'system';
}

interface ModelPickerProps {
  options: ModelOption[];
  value: string | null;
  onChange: (id: string) => void;
  loading?: boolean;
  disabled?: boolean;
  disabledHint?: string;
}

export function ModelPicker({ options, value, onChange, loading, disabled, disabledHint }: ModelPickerProps) {
  return (
    <div className="ni-model-picker">
      <span className="ni-kicker">推理模型</span>
      <Select
        size="small"
        showSearch
        loading={loading}
        disabled={disabled}
        value={value || undefined}
        placeholder={disabled ? disabledHint || '暂无模型' : '选择模型'}
        optionFilterProp="label"
        onChange={onChange}
        popupMatchSelectWidth={360}
        options={options.map((m) => ({
          value: m.id,
          label: `${m.name}${m.source === 'system' ? ' · 系统' : ''}`,
          title: m.id,
        }))}
        style={{ minWidth: 220 }}
      />
    </div>
  );
}
