import React from 'react';
import { Card, Divider, Select, Button, InputNumber, Alert, DatePicker, Tag, Typography, Tooltip, Switch } from 'antd';
import { Target, ArrowRightLeft, Info, CalendarRange } from 'lucide-react';
import { clsx } from 'clsx';
import { Dayjs } from 'dayjs';
import dayjs from 'dayjs';
import {
  TrainingTarget,
  TimePeriodMap,
  TargetMode,
  SplitKey,
  TARGET_PRESETS,
  buildLabelFormula,
  buildEffectiveTradeDate,
  daysBetween,
  formatRange,
  TrainingFactorFilterConfig,
  DEFAULT_FACTOR_FILTER,
} from './trainingUtils';
import { AdminModelFeatureDataCoverage } from '../../features/admin/types';

const { RangePicker } = DatePicker;

interface TrainingTargetConfigProps {
  target: TrainingTarget;
  timePeriods: TimePeriodMap;
  onTargetChange: (target: TrainingTarget) => void;
  onTimeChange: (key: SplitKey, values: [Dayjs, Dayjs]) => void;
  dataCoverage?: AdminModelFeatureDataCoverage | null;
  // 因子筛选开关与阈值（默认开启，阈值对齐后端 ic_icir 默认）
  factorFilter?: TrainingFactorFilterConfig;
  onFactorFilterChange?: (filter: TrainingFactorFilterConfig) => void;
}

const SectionHeader: React.FC<{ title: string; desc: string; icon?: React.ReactNode }> = ({ title, desc, icon }) => (
  <div className="flex items-start justify-between gap-4">
    <div>
      <div className="flex items-center gap-2">
        {icon}
        <Typography.Title level={4} className="!mb-0 !text-slate-900">
          {title}
        </Typography.Title>
      </div>
      <Typography.Paragraph className="!mb-0 !mt-2 !text-xs !text-slate-500 leading-relaxed">
        {desc}
      </Typography.Paragraph>
    </div>
  </div>
);

export const TrainingTargetConfig: React.FC<TrainingTargetConfigProps> = ({
  target,
  timePeriods,
  onTargetChange,
  onTimeChange,
  dataCoverage,
  factorFilter,
  onFactorFilterChange,
}) => {
  const labelFormula = buildLabelFormula(target);
  const effectiveTradeDate = buildEffectiveTradeDate(target, timePeriods.test[0]);

  const trainDays = daysBetween(timePeriods.train);
  const valDays = daysBetween(timePeriods.val);
  const testDays = daysBetween(timePeriods.test);
  const totalDays = trainDays + valDays + testDays;
  
  const minDataDate = dataCoverage?.min_date ? dayjs(dataCoverage.min_date) : null;
  const maxDataDate = dataCoverage?.max_date ? dayjs(dataCoverage.max_date) : null;

  const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

  const handleRangeChange = (key: SplitKey, values: any) => {
    if (values && values[0] && values[1]) {
      onTimeChange(key, [values[0], values[1]]);
    }
  };

  return (
    <div className="grid gap-4 xl:grid-cols-[1.05fr_0.95fr]">
      <Card className="rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20 } }}>
        <SectionHeader
          title="第二步：训练目标 T+N"
          desc="把训练目标与标签 horizon 独立出来，不再复用回测周期字段。"
          icon={<Target size={18} className="text-indigo-500" />}
        />
        <Divider className="my-4" />
        <div className="space-y-4">
          <div>
            <div className="mb-2 text-sm font-semibold text-slate-800">目标类型</div>
            <Select
              value={target.mode}
              onChange={(value) => onTargetChange({ ...target, mode: value as TargetMode })}
              className="w-full"
              options={[
                { label: '回归目标（未来收益率）', value: 'return' },
                { label: '分类目标（涨跌方向）', value: 'classification' },
              ]}
            />
            <div className="mt-2 text-xs text-slate-500">
              {target.mode === 'classification'
                ? '适合做方向预测、事件识别等离散标签任务。'
                : '适合做未来收益率、rank score 等连续标签任务。'}
            </div>
          </div>

          <div>
            <div className="mb-2 flex items-center justify-between">
              <div className="text-sm font-semibold text-slate-800">T+N 参数</div>
              <div className="text-xs text-slate-500">允许 1~30 个交易日</div>
            </div>
            <div className="flex flex-wrap gap-2">
              {TARGET_PRESETS.map((preset) => (
                <Button
                  key={preset}
                  size="small"
                  type={target.horizonDays === preset ? 'primary' : 'default'}
                  className={clsx('h-8 rounded-xl font-bold px-3', target.horizonDays === preset && 'bg-indigo-600')}
                  onClick={() => onTargetChange({ ...target, horizonDays: preset })}
                >
                  T+{preset}
                </Button>
              ))}
            </div>
            <div className="mt-3 flex items-center gap-3">
              <InputNumber
                min={1}
                max={30}
                value={target.horizonDays}
                onChange={(value) => onTargetChange({ ...target, horizonDays: clamp(Number(value ?? target.horizonDays), 1, 30) })}
                className="w-28"
              />
              <span className="text-sm text-slate-500">交易日</span>
            </div>
          </div>

          {/* ── 因子筛选（IC/ICIR）── */}
          <div className="rounded-2xl border border-slate-200 bg-slate-50/60 p-4">
            <div className="flex items-center justify-between gap-3">
              <Tooltip title="日频 Rank IC 初筛 → 不足时按综合评分自动回填 → 相关性剪枝 → 稳定性检验。筛选理由与每特征 IC/ICIR/覆盖率在训练日志与结果页展示，不再黑盒。">
                <div className="text-[10px] uppercase font-bold text-slate-400 cursor-help">因子筛选（IC/ICIR）</div>
              </Tooltip>
              <Switch
                size="small"
                checked={factorFilter?.enabled ?? DEFAULT_FACTOR_FILTER.enabled}
                onChange={(v) => onFactorFilterChange?.({ ...DEFAULT_FACTOR_FILTER, ...factorFilter, enabled: v })}
              />
            </div>
            {factorFilter?.enabled ?? DEFAULT_FACTOR_FILTER.enabled ? (
              <ol className="mt-3 space-y-1.5 text-[11px] text-slate-600 leading-relaxed list-decimal list-inside">
                <li>
                  日频 Rank IC 初筛：|IC| ≥ {factorFilter?.icThreshold ?? DEFAULT_FACTOR_FILTER.icThreshold} 且
                  |ICIR| ≥ {factorFilter?.icirThreshold ?? DEFAULT_FACTOR_FILTER.icirThreshold} 者进入优选池
                </li>
                <li>
                  优选不足时按综合评分（|IC| × |ICIR|）自动回填，保底约 {Math.min(factorFilter?.nTop ?? DEFAULT_FACTOR_FILTER.nTop, 20)} 个，
                  弱行情窗口不再只剩一两个特征
                </li>
                <li>
                  相关性 ≥ {factorFilter?.correlationThreshold ?? DEFAULT_FACTOR_FILTER.correlationThreshold} 去冗余，上限 top-{factorFilter?.nTop ?? DEFAULT_FACTOR_FILTER.nTop}，
                  再过滚动 60 日稳定性检验
                </li>
                <li>每特征的去留原因写入训练日志与结果页，可逐个核查</li>
              </ol>
            ) : (
              <div className="mt-2 text-[10px] text-amber-600 leading-relaxed">已关闭：全部选中特征直接进入训练，不做筛选。</div>
            )}
          </div>

          <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
            <div className="text-[10px] font-black uppercase tracking-[0.22em] text-slate-400">标签预览</div>
            <div className="mt-2 text-sm font-medium text-slate-800">{target.mode === 'classification' ? '预测未来 N 日涨跌方向' : `预测未来 ${target.horizonDays} 日收益率`}</div>
            <div className="mt-3 rounded-xl bg-white p-3 font-mono text-xs text-slate-700">{labelFormula}</div>
          </div>

          <div className="rounded-2xl border border-slate-200 bg-white p-4">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-[10px] font-black uppercase tracking-[0.22em] text-slate-400">生效日期预览</div>
                <div className="mt-1 text-sm font-semibold text-slate-900">{effectiveTradeDate}</div>
              </div>
              <div className="rounded-2xl bg-indigo-50 px-3 py-2 text-xs text-indigo-700">
                按交易日历校正时将由后端覆盖
              </div>
            </div>
          </div>

          <Alert
            type="info"
            showIcon
            message="设计说明"
            description="T+N 是训练标签 horizon，不是回测周期。训练请求、模型元数据和模型管理页都应使用同一字段口径。"
            className="rounded-2xl border-blue-100 bg-blue-50/70"
          />
        </div>
      </Card>

      <Card className="rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20 } }}>
        <SectionHeader
          title="时空切分预览 (Time Split)"
          desc="设置样本的训练、验证与测试区间，各阶段日期自动防重叠。"
          icon={<ArrowRightLeft size={18} className="text-indigo-500" />}
        />
        <Divider className="my-4" />
        
        {dataCoverage && (
          <div className="mb-4 rounded-xl border border-slate-200 bg-gradient-to-r from-slate-50 to-indigo-50/30 p-3">
            <div className="flex items-center gap-2 text-xs">
              <CalendarRange size={14} className="text-indigo-500" />
              <span className="font-semibold text-slate-700">数据有效期</span>
              <Tag className="m-0 rounded-lg border-0 bg-white/80 text-slate-600 font-mono text-[11px]">
                {dataCoverage.min_date} ~ {dataCoverage.max_date}
              </Tag>
              <Tooltip title={`共 ${dataCoverage.total_rows?.toLocaleString() ?? 0} 条记录，${dataCoverage.file_count ?? 0} 个 parquet 文件`}>
                <Info size={12} className="text-slate-400 cursor-help" />
              </Tooltip>
            </div>
          </div>
        )}
        
        <div className="space-y-4">
          {([
            { key: 'train', label: '训练集 (Training)', color: 'indigo', desc: '用于拟合模型参数' },
            { key: 'val', label: '验证集 (Validation)', color: 'amber', desc: '用于早停逻辑与超参调优' },
            { key: 'test', label: '测试集 (Testing)', color: 'emerald', desc: '用于样本外(OOS)最终检验' },
          ] as const).map((item) => {
            const range = timePeriods[item.key];
            const days = daysBetween(range);
            const width = (days / totalDays) * 100;
            const colorMap: Record<string, string> = {
              indigo: 'bg-indigo-500',
              amber: 'bg-amber-400',
              emerald: 'bg-emerald-500',
            };
            const barBgMap: Record<string, string> = {
              indigo: 'bg-indigo-50/50',
              amber: 'bg-amber-50/50',
              emerald: 'bg-emerald-50/50',
            };

            return (
              <div key={item.key} className={clsx('rounded-2xl border p-4 transition-colors', barBgMap[item.color], 'border-slate-200 hover:border-indigo-300')}>
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-sm font-bold text-slate-800">{item.label}</div>
                    <div className="text-[11px] text-slate-500">{item.desc}</div>
                  </div>
                  <Tag className="m-0 rounded-lg border-0 bg-white shadow-sm text-slate-700 font-mono px-2">{days}d</Tag>
                </div>
                <RangePicker
                  value={range as [Dayjs, Dayjs]}
                  onChange={(values) => handleRangeChange(item.key, values)}
                  className="mt-3 w-full rounded-xl border-slate-200 shadow-sm"
                  allowClear={false}
                  placeholder={['开始日期', '结束日期']}
                  disabledDate={(current) => {
                    if (!current) return false;
                    if (minDataDate && current.isBefore(minDataDate, 'day')) return true;
                    if (maxDataDate && current.isAfter(maxDataDate, 'day')) return true;
                    if (item.key === 'train') {
                      return current.isAfter(timePeriods.val[0].subtract(1, 'day'));
                    }
                    if (item.key === 'val') {
                      return (
                        current.isBefore(timePeriods.train[1].add(1, 'day')) ||
                        current.isAfter(timePeriods.test[0].subtract(1, 'day'))
                      );
                    }
                    if (item.key === 'test') {
                      return current.isBefore(timePeriods.val[1].add(1, 'day'));
                    }
                    return false;
                  }}
                />
                <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-white/80">
                  <div className={clsx('h-full rounded-full transition-all duration-500', colorMap[item.color])} style={{ width: `${width}%` }} />
                </div>
                <div className="mt-2 flex items-center justify-between text-[10px] font-medium text-slate-400 uppercase tracking-tighter">
                  <span>{formatRange(range)}</span>
                  <span className="text-slate-500">{width.toFixed(1)}% 占比</span>
                </div>
              </div>
            );
          })}
        </div>

      </Card>
    </div>
  );
};
