import React from 'react';
import { Card, Divider, Select, Button, InputNumber, Alert, DatePicker, Tag, Typography, Tooltip, Switch } from 'antd';
import { Target, ArrowRightLeft, Info, CalendarRange, HelpCircle } from 'lucide-react';
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
  buildLabelTransformSummary,
} from './trainingUtils';
import {
  LabelTargetSummary,
  TrainingCapabilities,
  applyTargetSpecToTarget,
  findTargetByKey,
  supportedTargetModes,
  targetFormLocks,
  targetModeUnsupportedReason,
  targetRefKey,
} from './trainingTargetCapabilities';
import { AdminModelFeatureDataCoverage } from '../../features/admin/types';
import { TrainingTargetHelpModal } from './TrainingTargetHelpModal';

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
  /** 已选模型（多模型集成取交集）；用于目标类型能力收敛 */
  modelTypes?: string[];
  /** 后端能力矩阵（GET /models/training/capabilities）；缺省时全部可选 */
  capabilities?: TrainingCapabilities | null;
  /** 目标注册表（active，按市场过滤） */
  availableTargets?: LabelTargetSummary[];
  /** 选中/切回 legacy；父层负责把 spec 灌进 TrainingTarget */
  onSelectTarget?: (target: LabelTargetSummary | null) => void;
}

const FAMILY_LABELS: Record<string, string> = {
  forward_return: '收益回归',
  classification_binary: '方向分类',
  quantile_return: '分位回归',
};

const targetLabel = (item: LabelTargetSummary): string =>
  `${item.id} @v${item.version} · ${FAMILY_LABELS[item.family] || item.family}`;

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
  modelTypes = ['lightgbm'],
  capabilities = null,
  availableTargets = [],
  onSelectTarget,
}) => {
  const labelFormula = buildLabelFormula(target);
  const effectiveTradeDate = buildEffectiveTradeDate(target, timePeriods.test[0]);

  const supportedModes = supportedTargetModes(modelTypes, capabilities);
  const classificationSupported = supportedModes.includes('classification');
  const classificationReason = targetModeUnsupportedReason(modelTypes, 'classification', capabilities);
  const targetKey = targetRefKey(target.targetRef);
  const selectedTarget = findTargetByKey(availableTargets, targetKey);
  const locks = targetFormLocks(target.targetSpec);
  const isRegistryTarget = Boolean(target.targetRef);
  const [showTransformHelp, setShowTransformHelp] = React.useState(false);
  const hideDropExtreme =
    target.mode === 'classification' ||
    locks.hideDropExtreme ||
    ['top_tail', 'size_excess_rank', 'size_excess_zscore'].includes(target.labelTransform);
  const hideTransform = target.mode === 'classification' || locks.lockTransform;

  const handleTargetSourceChange = (value: string) => {
    if (value === 'legacy') {
      onSelectTarget?.(null);
      onTargetChange({
        ...target,
        targetRef: null,
        targetSpec: null,
        targetOverrides: null,
        expectedTargetHash: null,
      });
      return;
    }
    const picked = findTargetByKey(availableTargets, value);
    if (!picked) return;
    onSelectTarget?.(picked);
    onTargetChange({
      ...applyTargetSpecToTarget(target, picked.spec),
      targetRef: { id: picked.id, version: picked.version },
      targetSpec: picked.spec,
      targetOverrides: null,
      expectedTargetHash: null,
    });
  };

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
            <div className="mb-2 text-sm font-semibold text-slate-800">目标来源</div>
            <Select
              value={targetKey}
              onChange={handleTargetSourceChange}
              className="w-full"
              options={[
                { label: '自定义（legacy 平铺口径）', value: 'legacy' },
                ...availableTargets.map((item) => ({
                  label: targetLabel(item),
                  value: targetRefKey(item),
                })),
              ]}
            />
            {selectedTarget ? (
              <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px] text-slate-500">
                <Tag className="m-0 rounded-lg border-0 bg-emerald-50 px-2 font-bold text-emerald-700">
                  {selectedTarget.status}
                </Tag>
                <span className="font-mono">hash {selectedTarget.spec_hash.slice(0, 8)}</span>
                <span>冻结口径；仅白名单字段可覆盖</span>
              </div>
            ) : (
              <div className="mt-2 text-xs text-slate-500">
                未引用注册表目标：按平铺字段训练，metadata 记为 legacy。
              </div>
            )}
          </div>

          <div>
            <div className="mb-2 text-sm font-semibold text-slate-800">目标类型</div>
            <Select
              value={target.mode}
              disabled={isRegistryTarget}
              onChange={(value) => {
                const mode = value as TargetMode;
                onTargetChange(
                  mode === 'classification'
                    ? {
                        ...target,
                        mode,
                        labelTransform: 'rank',
                        labelTopPct: 0.2,
                        labelDropExtremePct: 0,
                      }
                    : { ...target, mode },
                );
              }}
              className="w-full"
              options={[
                { label: '回归目标（未来收益率）', value: 'return' },
                {
                  label: classificationSupported
                    ? '分类目标（涨跌方向）'
                    : `分类目标（不可用：${classificationReason || '当前模型不支持'}）`,
                  value: 'classification',
                  disabled: !classificationSupported,
                },
              ]}
            />
            <div className="mt-2 text-xs text-slate-500">
              {isRegistryTarget
                ? '目标类型由注册表 spec 决定，本次训练不可切换；如需其它类型请换目标或走自定义。'
                : target.mode === 'classification'
                  ? '适合做方向预测、事件识别等离散标签任务。'
                  : '适合做未来收益率、rank score 等连续标签任务。'}
            </div>
            {!classificationSupported && !isRegistryTarget && (
              <Alert
                type="warning"
                showIcon
                className="mt-2 rounded-xl"
                message={classificationReason || '当前模型组合不支持分类目标'}
              />
            )}
          </div>

          <div>
            <div className="mb-2 flex items-center justify-between">
              <div className="text-sm font-semibold text-slate-800">T+N 参数</div>
              <div className="text-xs text-slate-500">
                {locks.lockHorizon ? '分位目标不允许覆盖 horizon' : '允许 1~30 个交易日'}
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              {TARGET_PRESETS.map((preset) => (
                <Button
                  key={preset}
                  size="small"
                  disabled={locks.lockHorizon}
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
                disabled={locks.lockHorizon}
                value={target.horizonDays}
                onChange={(value) => onTargetChange({ ...target, horizonDays: clamp(Number(value ?? target.horizonDays), 1, 30) })}
                className="w-28"
              />
              <span className="text-sm text-slate-500">交易日</span>
            </div>
          </div>

          {/* ── 标签与执行口径（严格实盘）── */}
          <div className="rounded-2xl border border-slate-200 bg-white p-4 space-y-3">
            <div className="flex items-center justify-between gap-3">
              <Tooltip title="训练标签直接决定模型学到的收益口径。open = T+1 开盘买、T+1+N 收盘卖；close = 旧口径。截面变换与 top-tail 只改标签，不改特征。">
                <div className="text-[10px] font-black uppercase tracking-[0.22em] text-slate-400 cursor-help">标签与执行口径</div>
              </Tooltip>
              <Tag className="m-0 rounded-lg border-0 bg-indigo-50 px-2 text-[10px] font-bold text-indigo-700">严格实盘</Tag>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <div className="mb-1 text-xs font-semibold text-slate-700">执行价</div>
                <Select
                  value={target.executionPrice}
                  disabled={isRegistryTarget}
                  onChange={(value) => onTargetChange({ ...target, executionPrice: value as TrainingTarget['executionPrice'] })}
                  className="w-full"
                  options={[
                    { label: '开盘价 · T+1 开盘买', value: 'open' },
                    { label: '收盘价 · T+1 收盘买（旧口径）', value: 'close' },
                  ]}
                />
                <div className="mt-1 text-[10px] leading-relaxed text-slate-500">
                  {isRegistryTarget
                    ? '执行价属于冻结目标口径，不允许在本次训练中覆盖。'
                    : '标签 = close(T+1+N) / 执行价(T+1) − 1'}
                </div>
              </div>
              <div>
                {hideTransform ? (
                  <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-xs leading-relaxed text-slate-600">
                    {target.mode === 'classification'
                      ? '分类目标不做截面变换（训练端跳过；rank 仅作 config 占位）。'
                      : '分位回归固定使用原始收益（raw），不允许截面变换。'}
                  </div>
                ) : (
                  <>
                    <div className="mb-1 flex items-center justify-between gap-2">
                      <div className="flex items-center gap-1 text-xs font-semibold text-slate-700">
                        标签截面变换
                        <HelpCircle size={12} className="text-indigo-500" />
                      </div>
                      <button
                        type="button"
                        onClick={() => setShowTransformHelp(true)}
                        className="inline-flex items-center gap-1 rounded-md border border-indigo-100 bg-indigo-50/50 px-1.5 py-0.5 text-[9px] font-bold text-indigo-600 transition-colors hover:bg-indigo-100"
                        title="查看六种变换的定义、适用边界与按模型建议"
                      >
                        <HelpCircle size={10} />
                        用法建议
                      </button>
                    </div>
                    <Select
                      value={target.labelTransform}
                      onChange={(value) => onTargetChange({ ...target, labelTransform: value as TrainingTarget['labelTransform'] })}
                      className="w-full"
                      options={[
                        { label: '截面 rank（默认）', value: 'rank' },
                        { label: '截面 zscore', value: 'zscore' },
                        { label: '原始收益', value: 'raw' },
                        { label: 'top-tail（只学最强尾部）', value: 'top_tail' },
                        { label: '同市值桶内超额 rank', value: 'size_excess_rank' },
                        { label: '同市值桶内超额 zscore', value: 'size_excess_zscore' },
                      ]}
                    />
                    <div className="mt-1 text-[10px] leading-relaxed text-slate-500">{buildLabelTransformSummary(target)}</div>
                    {target.labelTransform.startsWith('size_excess') && (
                      <div className="mt-1 text-[10px] leading-relaxed text-amber-600">
                        需要特征列表包含 total_cap，否则提交会被后端拒绝。
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              {target.labelTransform === 'top_tail' && !locks.hideTopPct && (
                <div>
                  <div className="mb-1 text-xs font-semibold text-slate-700">top-tail 尾部比例</div>
                  <InputNumber
                    min={5}
                    max={50}
                    step={5}
                    value={Math.round(target.labelTopPct * 100)}
                    onChange={(value) => onTargetChange({
                      ...target,
                      labelTopPct: clamp(Number(value ?? 20), 5, 50) / 100,
                    })}
                    addonAfter="%"
                    className="w-full"
                  />
                </div>
              )}
              {!hideDropExtreme && (
                <div>
                  <div className="mb-1 text-xs font-semibold text-slate-700">极端标签清洗（仅训练段）</div>
                  <InputNumber
                    min={0}
                    max={20}
                    step={1}
                    value={Math.round(target.labelDropExtremePct * 100)}
                    onChange={(value) => onTargetChange({
                      ...target,
                      labelDropExtremePct: clamp(Number(value ?? 0), 0, 20) / 100,
                    })}
                    addonAfter="%"
                    className="w-full"
                  />
                  <div className="mt-1 text-[10px] leading-relaxed text-slate-500">
                    按日剔除标签两端各 N%，0 = 关闭；只作用于训练段，验证/测试段不动。
                  </div>
                </div>
              )}
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

      <TrainingTargetHelpModal
        open={showTransformHelp}
        onClose={() => setShowTransformHelp(false)}
      />
    </div>
  );
};
