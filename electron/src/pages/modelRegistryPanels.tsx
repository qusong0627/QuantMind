import React, { useMemo, useState } from 'react';
import { Button, Card, Tag, Typography, Empty, Spin, Progress, Divider, Input, Modal, Tabs, Switch, DatePicker, Table, Drawer, Badge, Tooltip, Collapse, Select, message } from 'antd';
import { clsx } from 'clsx';
import dayjs from 'dayjs';
import {
  Layers, Star, RefreshCw, Search, Code, Calendar, Layers2,
  History, Archive, Brain, CheckCircle2, Clock, XCircle,
  ChevronRight, Play, Cpu, TrendingUp, Download, ChevronDown,
  ChevronUp, Shield, Zap, Activity, ListFilter,
} from 'lucide-react';
import {
  UserModelRecord,
  ModelTrainingRunStatus,
  InferenceRunRecord,
  InferencePrecheckResult,
  InferenceRankingResult,
  AutoInferenceSettings,
  LatestInferenceRunInfo,
  ModelShapSummaryResponse,
  ModelShapSummaryItem,
} from '../services/modelTrainingService';
import {
  calcTimeSplitStats,
  extractModelType,
  extractTimePeriods,
  formatTrendLabel,
  getMeta,
  getMetrics,
  getStatusConfig,
  isSystemModel,
  modelDisplayName,
  modelIdToDisplayName,
  resolveMetricNumber,
} from './modelRegistryUtils';
const { Text } = Typography;

const formatPanelDateTime = (raw?: string | null, fallback = '—') => {
  const value = String(raw || '').trim();
  if (!value) return fallback;
  const parsed = dayjs(value);
  if (parsed.isValid()) return parsed.format('YYYY-MM-DD HH:mm');
  const native = new Date(value);
  if (!Number.isNaN(native.getTime())) {
    return dayjs(native).format('YYYY-MM-DD HH:mm');
  }
  return fallback;
};

// ─── 左侧模型卡片 ────────────────────────────────────────────────────────────
export const ModelCard: React.FC<{
  model: UserModelRecord;
  isSelected: boolean;
  onClick: () => void;
  onSetDefault: () => void;
  canSetDefault: boolean;
}> = ({ model, isSelected, onClick, onSetDefault, canSetDefault }) => {
  const sc = getStatusConfig(model.status);
  const mt = extractModelType(model);
  const fc = getMeta(model).feature_count ?? null;
  return (
    <div
      onClick={onClick}
      className={clsx(
        'p-3.5 rounded-2xl cursor-pointer transition-all duration-200 border select-none',
        isSelected
          ? 'bg-white border-blue-500 shadow-lg shadow-blue-100 ring-1 ring-blue-400'
          : 'bg-transparent border-transparent hover:bg-white hover:border-slate-200 hover:shadow-sm'
      )}
    >
      <div className="flex justify-between items-start mb-1.5 gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className={clsx('px-1.5 py-0.5 rounded-md text-[8px] font-black tracking-wider flex items-center gap-0.5', sc.bg, sc.color)}>
            {sc.icon}{sc.label}
          </span>
          {model.is_default && <Star size={9} fill="#fbbf24" className="text-amber-400" />}
        </div>
        <Text className="text-[8px] text-slate-400 font-mono">{dayjs(model.created_at).format('YY/MM/DD')}</Text>
      </div>
      <div className="flex items-start justify-between gap-2">
        <Text className={clsx('font-black text-[11px] tracking-tight truncate block leading-tight min-w-0', isSelected ? 'text-blue-700' : 'text-slate-800')}>
          {modelDisplayName(model)}
        </Text>
        {canSetDefault && (
          <Button
            size="small"
            type="text"
            className="h-5 px-2 text-[9px] font-black rounded-md text-slate-500 hover:text-blue-600 hover:bg-blue-50 flex-shrink-0"
            onClick={(e) => {
              e.stopPropagation();
              onSetDefault();
            }}
          >
            设默认
          </Button>
        )}
      </div>
      <div className="flex items-center gap-1.5 mt-1 opacity-70">
        <Text className="text-[9px] text-slate-400 font-mono font-bold">{mt}</Text>
        {fc && <><Divider type="vertical" className="m-0 h-2 border-slate-300" /><Text className="text-[9px] text-slate-400 font-mono font-bold">{fc}维</Text></>}
      </div>
    </div>
  );
};

// ─── 模型详情面板 ────────────────────────────────────────────────────────────
export const ModelDetailPanel: React.FC<{ model: UserModelRecord }> = ({ model }) => {
  const meta = getMeta(model);
  const metrics = getMetrics(model);
  const timePeriods = extractTimePeriods(meta);
  const [featExpanded, setFeatExpanded] = useState(false);
  const splitPerformance = meta.performance_metrics && typeof meta.performance_metrics === 'object'
    ? meta.performance_metrics as Record<string, any>
    : null;
  const metadataMetrics = meta.metrics && typeof meta.metrics === 'object'
    ? meta.metrics as Record<string, any>
    : null;
  const resolveSplitSource = (key: 'train' | 'valid' | 'test') => {
    if (!splitPerformance) return null;
    const aliases = key === 'valid' ? ['valid', 'val'] : [key];
    for (const alias of aliases) {
      const source = splitPerformance[alias];
      if (source && typeof source === 'object') return source as Record<string, any>;
    }
    return null;
  };
  const splitMetrics: Array<{
    key: 'train' | 'valid' | 'test';
    label: string;
    color: 'blue' | 'indigo' | 'emerald';
    ic: number | null;
    icir: number | null;
    trendLabel: string;
  }> = [];
  let previousIc: number | null = null;
  for (const key of ['train', 'valid', 'test'] as const) {
    const source = resolveSplitSource(key);
    const aliases = key === 'valid' ? ['valid', 'val'] : [key];
    const fallbackIc = resolveMetricNumber(
      metadataMetrics,
      aliases.flatMap((alias) => [`${alias}_ic`, `${alias}_rank_ic`]),
    );
    const fallbackIcir = resolveMetricNumber(
      metadataMetrics,
      aliases.flatMap((alias) => [`${alias}_icir`, `${alias}_rank_icir`]),
    );
    const ic = resolveMetricNumber(source, ['ic', 'test_ic', 'val_ic', 'IC', 'mean_ic', 'rank_ic', 'train_ic']) ?? fallbackIc;
    const icir = resolveMetricNumber(source, ['icir', 'test_rank_icir', 'val_rank_icir', 'test_icir', 'val_icir', 'ICIR', 'IC_IR', 'rank_icir', 'train_rank_icir']) ?? fallbackIcir;
    if (ic === null && icir === null) continue;
    splitMetrics.push({
      key,
      label: key === 'train' ? '训练集' : key === 'valid' ? '验证集' : '测试集',
      color: key === 'train' ? 'blue' : key === 'valid' ? 'indigo' : 'emerald',
      ic,
      icir,
      trendLabel: formatTrendLabel(ic, previousIc, 4),
    });
    previousIc = ic ?? previousIc;
  }
  const hasSplitIC = splitMetrics.length > 0;
  const ic =
    metrics.ic ??
    metrics.IC ??
    metrics.mean_ic ??
    resolveMetricNumber(metadataMetrics, ['test_ic', 'val_ic', 'train_ic']) ??
    null;
  const icir =
    metrics.icir ??
    metrics.ICIR ??
    metrics.IC_IR ??
    resolveMetricNumber(metadataMetrics, ['test_rank_icir', 'val_rank_icir', 'test_icir', 'val_icir', 'train_rank_icir', 'train_icir']) ??
    null;
  const hasIC = [ic, icir].some(v => v !== null);
  const horizonDays = meta.target_horizon_days ?? meta.horizon_days;
  const targetMode = String(meta.target_mode ?? '').toLowerCase();
  const labelFormula = meta.label_formula ?? meta.labelFormula;
  const targetModeLabel = targetMode
    ? targetMode === 'classification'
      ? '分类'
      : targetMode === 'regression' || targetMode === 'return'
        ? '回归'
        : targetMode
    : '';
  const features: string[] = Array.isArray(meta.features) ? meta.features : [];
  const modelParams = meta.model_params && typeof meta.model_params === 'object' ? meta.model_params as Record<string, any> : null;
  const KEY_PARAMS = ['num_leaves', 'learning_rate', 'max_depth', 'n_estimators', 'num_boost_round', 'subsample', 'colsample_bytree', 'reg_alpha', 'reg_lambda'];
  const importantParams = modelParams ? KEY_PARAMS.filter(k => modelParams[k] !== undefined) : [];
  const otherParams = modelParams ? Object.entries(modelParams).filter(([k]) => !KEY_PARAMS.includes(k)) : [];
  const FEAT_LIMIT = 20;
  const shownFeatures = featExpanded ? features : features.slice(0, FEAT_LIMIT);
  const icDecayGuide = '这里的“衰减值”指当前区间 IC 相对上一区间的变化率。经验上，绝对变化率 ≤ 5% 可视为优秀，5% - 15% 可视为合格，> 15% 建议重点关注。若为正值，表示较上段有所提升。';
  return (
    <div className="pt-5 space-y-5">
      {/* IC 变化主视图 */}
      {hasSplitIC ? (
        <Card className="rounded-3xl border-slate-100 shadow-sm overflow-hidden" title={
          <div className="flex items-center gap-2">
            <Activity size={14} className="text-blue-600" />
            <span className="text-xs font-black text-slate-800 uppercase tracking-tighter">训练期指标（IC 变化）</span>
          </div>
        }>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="space-y-1">
              <div className="text-sm font-bold text-slate-800">训练集、验证集、测试集三段 IC 主线</div>
              <div className="text-xs text-slate-500 leading-relaxed">
                仅保留训练阶段可稳定计算的指标：各区间 IC 与 ICIR；回测型指标请在回测中心查看。
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              {splitMetrics.map((item) => (
                <Tag
                  key={item.key}
                  className={clsx(
                    'm-0 rounded-full border-0 font-bold',
                    item.color === 'blue' && 'bg-blue-50 text-blue-700',
                    item.color === 'indigo' && 'bg-indigo-50 text-indigo-700',
                    item.color === 'emerald' && 'bg-emerald-50 text-emerald-700',
                  )}
                >
                  {item.label} IC
                </Tag>
              ))}
            </div>
          </div>
          <div className="mt-5 grid gap-4 lg:grid-cols-3">
            {splitMetrics.map((item) => (
              <div
                key={item.key}
                className={clsx(
                  'relative overflow-hidden rounded-2xl border p-4 shadow-sm transition-shadow hover:shadow-md',
                  item.color === 'blue' && 'border-blue-100 bg-blue-50/70',
                  item.color === 'indigo' && 'border-indigo-100 bg-indigo-50/70',
                  item.color === 'emerald' && 'border-emerald-100 bg-emerald-50/70',
                )}
              >
                <div className={clsx(
                  'absolute inset-x-0 top-0 h-1',
                  item.color === 'blue' && 'bg-blue-500',
                  item.color === 'indigo' && 'bg-indigo-500',
                  item.color === 'emerald' && 'bg-emerald-500',
                )} />
                <div className="flex items-start justify-between gap-3">
                  <div className="text-sm font-black tracking-[0.18em] text-slate-700">{item.label}</div>
                  <div className={clsx(
                    'rounded-xl border px-2.5 py-1 text-[10px] font-black',
                    item.color === 'blue' && 'border-blue-100 bg-white text-blue-700',
                    item.color === 'indigo' && 'border-indigo-100 bg-white text-indigo-700',
                    item.color === 'emerald' && 'border-emerald-100 bg-white text-emerald-700',
                  )}>
                    ICIR {item.icir === null ? '—' : item.icir.toFixed(3)}
                  </div>
                </div>
                <div className="mt-5 flex flex-col items-center justify-center text-center">
                  <div className="text-2xl font-black text-slate-900 font-mono">
                    {item.ic === null ? '—' : item.ic.toFixed(4)}
                  </div>
                  <div className={clsx(
                    'mt-2 text-[10px] font-black',
                    item.ic !== null && (item.ic >= 0 ? 'text-emerald-600' : 'text-red-500')
                  )}>
                    {item.trendLabel}
                  </div>
                </div>
              </div>
            ))}
          </div>
          <div className="mt-4 rounded-2xl border border-amber-100 bg-amber-50/70 px-4 py-3 text-xs leading-relaxed text-amber-900">
            <span className="font-black">友情提示：</span>
            {icDecayGuide}
          </div>
        </Card>
      ) : hasIC ? (
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <MetricCard label="IC" value={ic} digits={4} />
            <MetricCard label="ICIR" value={icir} digits={3} />
          </div>
          <div className="rounded-2xl border border-amber-100 bg-amber-50/70 px-4 py-3 text-xs leading-relaxed text-amber-900">
            <span className="font-black">友情提示：</span>
            {icDecayGuide}
          </div>
        </div>
      ) : null}
      {/* 数据集划分 */}
      {timePeriods && (() => {
        const splitStats = calcTimeSplitStats(timePeriods);
        const splitSegments = [
          {
            key: 'train',
            label: '训练集',
            range: timePeriods.train,
            percent: splitStats.train.percent,
            days: splitStats.train.days,
            color: 'bg-blue-500',
            surface: 'bg-blue-50/70',
            border: 'border-blue-100',
            text: 'text-blue-700',
            note: '用于拟合模型权重',
          },
          ...(splitStats.val
            ? [{
                key: 'val',
                label: '验证集',
                range: timePeriods.val as [string, string],
                percent: splitStats.val.percent,
                days: splitStats.val.days,
                color: 'bg-indigo-400',
                surface: 'bg-indigo-50/70',
                border: 'border-indigo-100',
                text: 'text-indigo-700',
                note: '用于早停与调参',
              }]
            : []),
          ...(splitStats.test
            ? [{
                key: 'test',
                label: '测试集',
                range: timePeriods.test as [string, string],
                percent: splitStats.test.percent,
                days: splitStats.test.days,
                color: 'bg-emerald-400',
                surface: 'bg-emerald-50/70',
                border: 'border-emerald-100',
                text: 'text-emerald-700',
                note: '用于 OOS 检验',
              }]
            : []),
        ];
        return (
          <Card className="rounded-3xl border-slate-100 shadow-sm overflow-hidden" title={
            <div className="flex items-center gap-2">
              <Layers2 size={14} className="text-blue-600" />
              <span className="text-xs font-black text-slate-800 uppercase tracking-tighter">数据集划分</span>
            </div>
          }>
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="space-y-1">
                <div className="text-sm font-bold text-slate-800">训练 / 验证 / 测试三段式切分</div>
                <div className="text-xs text-slate-500 leading-relaxed">
                  将数据窗口拆成可读的分区卡片，配合总览条直观看出样本配比、时间跨度和每段用途。
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Tag className="m-0 rounded-full border-0 bg-blue-50 text-blue-700 font-bold">总窗口 {splitStats.totalDays} 天</Tag>
                <Tag className="m-0 rounded-full border-0 bg-slate-100 text-slate-600 font-bold">训练 {splitStats.train.percent}%</Tag>
                {splitStats.val && <Tag className="m-0 rounded-full border-0 bg-slate-100 text-slate-600 font-bold">验证 {splitStats.val.percent}%</Tag>}
                {splitStats.test && <Tag className="m-0 rounded-full border-0 bg-slate-100 text-slate-600 font-bold">测试 {splitStats.test.percent}%</Tag>}
              </div>
            </div>
            <div className="mt-5 rounded-2xl border border-slate-100 bg-slate-50/80 p-4">
              <div className="flex items-center justify-between text-[10px] font-black uppercase tracking-[0.22em] text-slate-400">
                <span>配比总览</span>
                <span>
                  训练 {splitStats.train.percent}% ·
                  {splitStats.val ? ` 验证 ${splitStats.val.percent}% ·` : ''}
                  {splitStats.test ? ` 测试 ${splitStats.test.percent}%` : ''}
                </span>
              </div>
              <div className="mt-3 h-3 w-full overflow-hidden rounded-full bg-slate-200">
                <div className="flex h-full w-full">
                  <div className="h-full bg-blue-500 transition-all" style={{ width: `${splitStats.train.percent}%` }} />
                  {splitStats.val && <div className="h-full bg-indigo-400 transition-all" style={{ width: `${splitStats.val.percent}%` }} />}
                  {splitStats.test && <div className="h-full bg-emerald-400 transition-all" style={{ width: `${splitStats.test.percent}%` }} />}
                </div>
              </div>
              <div className="mt-3 grid grid-cols-1 gap-2 text-[10px] text-slate-500 sm:grid-cols-3">
                <span>训练集：拟合模型权重</span>
                <span>验证集：早停与调参</span>
                <span className="sm:text-right">测试集：OOS 检验</span>
              </div>
            </div>
            <div className="mt-5 grid gap-4 lg:grid-cols-3">
              {splitSegments.map((segment) => (
                <TimeItem
                  key={segment.key}
                  label={segment.label}
                  range={segment.range}
                  color={segment.color}
                  percent={segment.percent}
                  days={segment.days}
                  note={segment.note}
                  surface={segment.surface}
                  border={segment.border}
                  text={segment.text}
                />
              ))}
            </div>
          </Card>
        );
      })()}
      {/* 训练目标 */}
      {(horizonDays || targetMode || labelFormula) && (
        <Card className="rounded-3xl border-slate-100 shadow-sm overflow-hidden" title={
          <div className="flex items-center gap-2">
            <Zap size={14} className="text-amber-500" />
            <span className="text-xs font-black text-slate-800 uppercase tracking-tighter">训练目标</span>
          </div>
        }>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="space-y-1">
              <div className="text-sm font-bold text-slate-800">标签口径、任务类型与目标周期统一展示</div>
              <div className="text-xs text-slate-500 leading-relaxed">
                训练目标与数据切分保持同一套视觉语言，减少页面上方信息块的割裂感。
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              {horizonDays && <Tag className="m-0 rounded-full border-0 bg-amber-50 text-amber-700 font-bold">T+{horizonDays} 天</Tag>}
              {targetMode && (
                <Tag className="m-0 rounded-full border-0 bg-slate-100 text-slate-600 font-bold">
                  {targetMode === 'classification' ? '分类目标' : targetMode === 'regression' || targetMode === 'return' ? '回归目标' : targetMode}
                </Tag>
              )}
              {labelFormula && <Tag className="m-0 rounded-full border-0 bg-blue-50 text-blue-700 font-bold">标签公式</Tag>}
            </div>
          </div>
          <div className="mt-5 rounded-2xl border border-slate-100 bg-slate-50/80 p-4">
            <div className="flex items-center justify-between text-[10px] font-black uppercase tracking-[0.22em] text-slate-400">
              <span>训练目标总览</span>
              <span>
                {horizonDays ? `T+${horizonDays}` : '未设置'}
                {targetModeLabel ? ` · ${targetModeLabel}` : ''}
              </span>
            </div>

            <div className="mt-3 grid gap-4 lg:grid-cols-[0.82fr_0.82fr_1.36fr]">
              {horizonDays && (
                <div className="relative overflow-hidden rounded-2xl border border-amber-100 bg-amber-50/70 p-4 shadow-sm">
                  <div className="absolute inset-x-0 top-0 h-1 bg-amber-500" />
                  <div className="text-[10px] font-black uppercase tracking-[0.22em] text-amber-500">预测周期</div>
                  <div className="mt-2 text-xl font-black text-slate-900">T+{horizonDays}</div>
                  <div className="mt-1 text-xs text-slate-500">标签向前滚动的交易日数</div>
                </div>
              )}
              {targetMode && (
                <div className="relative overflow-hidden rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
                  <div className="absolute inset-x-0 top-0 h-1 bg-slate-400" />
                  <div className="text-[10px] font-black uppercase tracking-[0.22em] text-slate-400">任务类型</div>
                  <div className="mt-2 text-xl font-black text-slate-900">
                    {targetModeLabel}
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    {targetMode === 'classification' ? '方向判断 / 离散标签' : '连续收益 / 数值标签'}
                  </div>
                </div>
              )}
              {labelFormula && (
                <div className="relative overflow-hidden rounded-2xl border border-blue-100 bg-blue-50/70 p-4 shadow-sm">
                  <div className="absolute inset-x-0 top-0 h-1 bg-blue-500" />
                  <div className="text-[10px] font-black uppercase tracking-[0.22em] text-blue-500">标签公式</div>
                  <div className="mt-2 rounded-xl border border-white bg-white px-3 py-2">
                    <Text className="block text-[10px] font-black uppercase tracking-[0.22em] text-slate-400">FORMULA</Text>
                    <Text className="mt-1 block text-[11px] font-mono font-black text-blue-700 break-all">
                      {String(labelFormula)}
                    </Text>
                  </div>
                  <div className="mt-1 text-xs text-slate-500">会同步写入训练请求和模型元数据</div>
                </div>
              )}
            </div>
          </div>
        </Card>
      )}

      {/* 特征列表 */}
      {features.length > 0 && (
        <Card className="rounded-3xl border-slate-100 shadow-sm" title={
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <ListFilter size={14} className="text-purple-500" />
              <span className="text-xs font-black text-slate-800 uppercase tracking-tighter">特征列表</span>
            </div>
            <Tag color="purple" className="font-bold text-[10px]">{features.length} 个特征</Tag>
          </div>
        }>
          <div className="flex flex-wrap gap-1.5">
            {shownFeatures.map((f, i) => (
              <Tag key={i} className="rounded-lg bg-purple-50 border-purple-100 text-purple-700 text-[10px] font-mono font-bold">{f}</Tag>
            ))}
          </div>
          {features.length > FEAT_LIMIT && (
            <button
              onClick={() => setFeatExpanded(!featExpanded)}
              className="mt-3 flex items-center gap-1 text-[10px] font-black text-purple-500 hover:text-purple-700 transition-colors"
            >
              {featExpanded ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
              {featExpanded ? '收起' : `展开全部 ${features.length} 个特征`}
            </button>
          )}
        </Card>
      )}

      {/* 模型超参 */}
      {modelParams && importantParams.length > 0 && (
        <Card className="rounded-3xl border-slate-100 shadow-sm" title={
          <div className="flex items-center gap-2">
            <Zap size={14} className="text-orange-500" />
            <span className="text-xs font-black text-slate-800 uppercase tracking-tighter">模型超参</span>
          </div>
        }>
          <div className="grid grid-cols-3 gap-[25px]">
            {importantParams.map(k => (
              <div key={k} className="bg-orange-50 rounded-xl p-3">
                <Text className="text-[9px] text-orange-400 font-black uppercase block">{k.replace(/_/g, ' ')}</Text>
                <Text className="text-sm font-black text-slate-800">{String(modelParams[k])}</Text>
              </div>
            ))}
          </div>
          {otherParams.length > 0 && (
            <Collapse ghost size="small" className="mt-3" items={[{
              key: '1',
              label: <span className="text-[10px] font-bold text-slate-400">其他参数（{otherParams.length}）</span>,
              children: (
                <div className="grid grid-cols-3 gap-2">
                  {otherParams.map(([k, v]) => (
                    <div key={k} className="bg-slate-50 rounded-lg p-2">
                      <Text className="text-[9px] text-slate-400 font-black block">{k}</Text>
                      <Text className="text-[10px] font-black text-slate-700 font-mono">{String(v)}</Text>
                    </div>
                  ))}
                </div>
              ),
            }]} />
          )}
        </Card>
      )}

      {!hasIC && !timePeriods && (
        <Empty description={<span className="text-xs text-slate-400">暂无详细指标数据</span>} />
      )}
    </div>
  );
};

// ─── 训练溯源面板 ────────────────────────────────────────────────────────────

export const TrainingSourcePanel: React.FC<{
  model: UserModelRecord;
  trainingRun: ModelTrainingRunStatus | null;
  loading: boolean;
}> = ({ model, trainingRun, loading }) => {
  const result = (trainingRun?.result ?? {}) as Record<string, any>;
  const reqPayload = (result.request_payload ?? {}) as Record<string, any>;

  return (
    <div className="pt-5 space-y-4">
      <div className="flex items-center gap-3 p-4 bg-slate-50 rounded-2xl border border-slate-100">
        <History size={15} className="text-slate-400" />
        <div>
          <Text className="text-[10px] text-slate-400 font-black uppercase block">训练任务 ID</Text>
          <Text className="text-sm font-black text-slate-800 font-mono">{model.source_run_id}</Text>
        </div>
        <ChevronRight size={13} className="text-slate-300 ml-auto" />
      </div>
      {loading ? (
        <div className="flex items-center justify-center py-16"><Spin /></div>
      ) : trainingRun ? (
        <>
          <Card className="rounded-3xl border-slate-100 shadow-sm">
            <div className="flex items-center gap-3 mb-4">
              {trainingRun.status === 'completed'
                ? <CheckCircle2 size={16} className="text-emerald-500" />
                : trainingRun.status === 'failed'
                  ? <XCircle size={16} className="text-red-500" />
                  : <Activity size={16} className="text-blue-500" />}
              <Text className="text-xs font-black text-slate-800">
                {trainingRun.status === 'completed' ? '训练已完成' : trainingRun.status === 'failed' ? '训练失败' : `状态: ${trainingRun.status}`}
              </Text>
              {trainingRun.status === 'running' && <Progress percent={trainingRun.progress} size="small" className="flex-1 ml-4" />}
            </div>
            {Object.keys(reqPayload).length > 0 && (
              <div className="grid grid-cols-3 gap-[25px]">
                {reqPayload.model_type && <InfoCell label="模型类型" value={String(reqPayload.model_type)} />}
                {reqPayload.num_boost_round && <InfoCell label="迭代轮数" value={String(reqPayload.num_boost_round)} />}
                {Array.isArray(reqPayload.features) && <InfoCell label="特征数量" value={`${reqPayload.features.length} 个`} />}
              </div>
            )}
          </Card>
          {trainingRun.logs && (
            <Card className="rounded-3xl border-slate-100 shadow-sm" title={<span className="text-xs font-black uppercase text-slate-700">训练日志</span>}>
              <div className="bg-slate-900 rounded-xl p-4 max-h-[360px] overflow-y-auto custom-scrollbar">
                <pre className="text-[10px] font-mono text-green-400 leading-relaxed whitespace-pre-wrap">
                  {trainingRun.logs.split('\n').slice(-50).join('\n')}
                </pre>
              </div>
            </Card>
          )}
        </>
      ) : (
        <Empty description={<span className="text-xs text-slate-400">无法加载训练任务详情</span>} />
      )}
    </div>
  );
};

// ─── 归因分析面板 ────────────────────────────────────────────────────────────

export const AttributionAnalysisPanel: React.FC<{
  model: UserModelRecord;
  shapSummary: ModelShapSummaryResponse | null;
  loading: boolean;
  error?: string;
  featureLabelMap?: Record<string, string>;
  onRefresh: () => void;
}> = ({ model, shapSummary, loading, error, featureLabelMap = {}, onRefresh }) => {
  const meta = getMeta(model);
  const shapMeta = meta.shap && typeof meta.shap === 'object'
    ? meta.shap as Record<string, any>
    : {};
  const [featureQuery, setFeatureQuery] = useState('');
  const [exporting, setExporting] = useState(false);

  const status = String(shapSummary?.status || shapMeta.status || 'missing').toLowerCase();
  const split = String(shapSummary?.split || shapMeta.split || '—');
  const rowsUsed = Number(shapSummary?.rows_used ?? shapMeta.rows_used ?? 0);
  const rowsRequested = Number(shapSummary?.rows_requested ?? shapMeta.rows_requested ?? 0);
  const file = String(shapSummary?.file || shapMeta.file || '—');
  const errorText = String(shapSummary?.error || shapMeta.error || error || '');
  const rows = (shapSummary?.items ?? []).filter((item) => item.feature);
  const getFeatureLabel = (feature: string) => featureLabelMap[feature] || feature;
  const filteredRows = useMemo(() => {
    const query = featureQuery.trim().toLowerCase();
    if (!query) return rows;
    return rows.filter((item) => {
      const feature = item.feature.toLowerCase();
      const label = getFeatureLabel(item.feature).toLowerCase();
      return feature.includes(query) || label.includes(query);
    });
  }, [rows, featureQuery, featureLabelMap]);

  const statusCfg: Record<string, { text: string; cls: string }> = {
    completed: { text: '已完成', cls: 'bg-emerald-50 text-emerald-700' },
    disabled: { text: '已关闭', cls: 'bg-slate-100 text-slate-600' },
    skipped: { text: '已跳过', cls: 'bg-amber-50 text-amber-700' },
    failed: { text: '失败', cls: 'bg-rose-50 text-rose-700' },
    missing: { text: '未产出', cls: 'bg-slate-100 text-slate-600' },
  };
  const currentStatus = statusCfg[status] ?? statusCfg.missing;
  const handleExportCsv = async () => {
    if (!filteredRows.length) {
      message.warning('当前筛选结果为空，无可导出数据');
      return;
    }
    setExporting(true);
    try {
      const escapeCsvCell = (value: unknown) => {
        const text = value === null || value === undefined ? '' : String(value);
        if (/[",\n\r]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
        return text;
      };
      const csvRows = [
        ['rank', 'feature', 'feature_label', 'mean_abs_shap', 'mean_shap', 'positive_ratio'],
        ...filteredRows.map((item) => [
          String(item.rank),
          item.feature,
          getFeatureLabel(item.feature),
          Number(item.mean_abs_shap || 0).toFixed(8),
          Number(item.mean_shap || 0).toFixed(8),
          Number(item.positive_ratio || 0).toFixed(8),
        ]),
      ];
      const csv = csvRows.map((row) => row.map((cell) => escapeCsvCell(cell)).join(',')).join('\n');
      const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `shap_summary_${model.model_id}_${split || 'unknown'}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      message.success(`已导出 ${filteredRows.length} 条 SHAP 因子贡献记录`);
    } catch (err: any) {
      message.error(`导出失败: ${err?.message || '未知错误'}`);
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="pt-5 space-y-4">
      <Card
        className="rounded-3xl border-slate-100 shadow-sm"
        title={
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <Brain size={14} className="text-violet-600" />
              <span className="text-xs font-black text-slate-800 uppercase tracking-tighter">归因分析（SHAP）</span>
            </div>
            <Button size="small" className="rounded-xl font-bold text-xs" onClick={onRefresh} loading={loading}>
              刷新
            </Button>
          </div>
        }
      >
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Tag className={clsx('m-0 rounded-full border-0 font-bold', currentStatus.cls)}>{currentStatus.text}</Tag>
            <Tag className="m-0 rounded-full border-0 bg-violet-50 text-violet-700 font-bold">样本来源 {split}</Tag>
            <Tag className="m-0 rounded-full border-0 bg-slate-100 text-slate-600 font-bold">
              样本 {rowsUsed}/{rowsRequested || '—'}
            </Tag>
            <Tag className="m-0 rounded-full border-0 bg-slate-100 text-slate-600 font-bold">{file}</Tag>
          </div>
          <Text className="block text-xs text-slate-700 leading-relaxed">
            平均绝对贡献：因子影响幅度（越大越重要）｜ 平均方向贡献：正负抵消后的净效果（红=推高预测，绿=压低预测）｜ 正向占比：推高预测的样本比例
          </Text>
          {errorText && status !== 'completed' && (
            <div className="rounded-2xl border border-rose-100 bg-rose-50 px-3 py-2">
              <Text className="text-[11px] text-rose-700">错误信息：{errorText}</Text>
            </div>
          )}
        </div>
      </Card>

      <Card
        className="rounded-3xl border-slate-100 shadow-sm"
        title={
          <div className="flex items-center justify-between gap-3">
            <span className="text-xs font-black uppercase text-slate-700">因子贡献榜</span>
            <div className="flex items-center gap-2">
              <Tag className="m-0 rounded-full border-0 bg-slate-100 text-slate-600 font-bold">
                {filteredRows.length}/{rows.length}
              </Tag>
              <Button
                size="small"
                icon={<Download size={12} className={exporting ? 'animate-pulse' : ''} />}
                className="rounded-xl text-xs font-bold"
                onClick={handleExportCsv}
                disabled={exporting || filteredRows.length === 0}
                loading={exporting}
              >
                {exporting ? '导出中...' : '导出 CSV'}
              </Button>
            </div>
          </div>
        }
      >
        <Spin spinning={loading}>
          <div className="mb-3">
            <Input
              value={featureQuery}
              onChange={(e) => setFeatureQuery(e.target.value)}
              prefix={<Search size={12} className="text-slate-400" />}
              placeholder="搜索因子名或中文解释（支持模糊匹配）"
              className="rounded-xl h-9 text-xs border-slate-200"
            />
          </div>
          {rows.length > 0 ? (
            <Table<ModelShapSummaryItem>
              size="small"
              rowKey="feature"
              pagination={{ pageSize: 12, showSizeChanger: false }}
              dataSource={filteredRows}
              tableLayout="fixed"
              locale={{ emptyText: '没有匹配的因子，请调整搜索条件' }}
              columns={[
                {
                  title: <span className="text-center block">排名</span>,
                  dataIndex: 'rank',
                  width: 60,
                  align: 'center',
                  render: (value: number) => <Text className="text-xs font-black text-slate-700">{value}</Text>,
                },
                {
                  title: <span className="text-center block">因子</span>,
                  dataIndex: 'feature',
                  width: 220,
                  align: 'center',
                  render: (value: string) => {
                    const label = getFeatureLabel(value);
                    const translated = label !== value;
                    return (
                      <Tooltip title={translated ? `${label} (${value})` : value}>
                        <div className="mx-auto w-full max-w-[200px] min-w-0 overflow-hidden">
                          <Text className="block w-full truncate text-xs font-black text-slate-800">{label}</Text>
                          {translated && (
                            <Text className="block w-full truncate text-[10px] font-mono font-bold text-slate-400">{value}</Text>
                          )}
                        </div>
                      </Tooltip>
                    );
                  },
                },
                {
                  title: <span className="text-center block">平均绝对贡献</span>,
                  dataIndex: 'mean_abs_shap',
                  width: 140,
                  align: 'center',
                  sorter: (a, b) => a.mean_abs_shap - b.mean_abs_shap,
                  defaultSortOrder: 'descend',
                  render: (value: number) => <Text className="text-xs font-black text-violet-700">{Number(value || 0).toFixed(6)}</Text>,
                },
                {
                  title: <span className="text-center block">平均方向贡献</span>,
                  dataIndex: 'mean_shap',
                  width: 140,
                  align: 'center',
                  sorter: (a, b) => a.mean_shap - b.mean_shap,
                  render: (value: number) => (
                    <Text className={clsx('text-xs font-black', value >= 0 ? 'text-rose-500' : 'text-emerald-600')}>
                      {Number(value || 0).toFixed(6)}
                    </Text>
                  ),
                },
                {
                  title: <span className="text-center block">正向占比</span>,
                  dataIndex: 'positive_ratio',
                  width: 100,
                  align: 'center',
                  sorter: (a, b) => a.positive_ratio - b.positive_ratio,
                  render: (value: number) => <Text className="text-xs text-slate-600">{(Number(value || 0) * 100).toFixed(2)}%</Text>,
                },
              ]}
            />
          ) : (
            <Empty description={<span className="text-xs text-slate-400">当前模型暂无可展示的 SHAP 因子贡献数据</span>} />
          )}
        </Spin>
      </Card>
    </div>
  );
};

// ─── 推理中心面板 ────────────────────────────────────────────────────────────

export const InferenceCenterPanel: React.FC<{
  model: UserModelRecord;
  inferenceDate: dayjs.Dayjs | null;
  onDateChange: (d: dayjs.Dayjs | null) => void;
  targetDate: string;
  targetDateLoading: boolean;
  horizonDays: number;
  running: boolean;
  onRun: () => void;
  onRunAsDefault?: () => void;
  lastRun: InferenceRunRecord | null;
  history: InferenceRunRecord[];
  historyLoading: boolean;
  onViewRanking: (runId: string) => void;
  autoSettings: AutoInferenceSettings | null;
  autoSaving: boolean;
  onToggleAuto: (enabled: boolean) => void;
  latestInferenceRun: LatestInferenceRunInfo | null;
  latestInferenceRunLoading: boolean;
  precheck: InferencePrecheckResult | null;
  precheckLoading: boolean;
  onRefreshPrecheck: () => void;
  historyRunIdFilter: string;
  onHistoryRunIdFilterChange: (value: string) => void;
  historyStatusFilter: 'all' | 'running' | 'completed' | 'failed';
  onHistoryStatusFilterChange: (value: 'all' | 'running' | 'completed' | 'failed') => void;
  historyDateFilter: dayjs.Dayjs | null;
  onHistoryDateFilterChange: (value: dayjs.Dayjs | null) => void;
}> = ({
  model, inferenceDate, onDateChange, targetDate, targetDateLoading, horizonDays,
  running, onRun, onRunAsDefault, lastRun, history, historyLoading, onViewRanking,
  autoSettings, autoSaving, onToggleAuto, latestInferenceRun, latestInferenceRunLoading, precheck, precheckLoading, onRefreshPrecheck,
  historyRunIdFilter, onHistoryRunIdFilterChange, historyStatusFilter, onHistoryStatusFilterChange, historyDateFilter, onHistoryDateFilterChange,
}) => {
  const currentModelName = modelDisplayName(model);
  const latestRunModelLabel = latestInferenceRun?.model_id === model.model_id
    ? currentModelName
    : modelIdToDisplayName(latestInferenceRun?.model_id);

  return (
  <div className="pt-5 space-y-5">

    {/* ── 前置检查 ── */}
    <Card
      className="rounded-3xl border-slate-100 shadow-sm"
      title={
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Shield size={14} className="text-emerald-500" />
            <span className="text-xs font-black text-slate-800 uppercase tracking-tighter">前置检查</span>
            {precheck && (
              <Tag color={precheck.passed ? 'green' : 'red'} className="text-[9px] font-black">
                {precheck.passed ? '通过' : '阻断'}
              </Tag>
            )}
          </div>
          <Button size="small" className="rounded-xl font-bold text-xs" onClick={onRefreshPrecheck} loading={precheckLoading}>
            重新检查
          </Button>
        </div>
      }
    >
      <Spin spinning={precheckLoading}>
        {precheck ? (
          <div className="space-y-4">
            <div className={clsx(
              'rounded-2xl border p-4',
              precheck.passed ? 'border-emerald-100 bg-emerald-50/70' : 'border-rose-100 bg-rose-50/70',
            )}>
              <div className="flex items-center justify-between gap-3">
                <div>
                  <Text className={clsx('text-xs font-black block', precheck.passed ? 'text-emerald-700' : 'text-rose-700')}>
                    {precheck.passed ? '全部硬门禁通过，可以执行推理' : '存在阻断项，请先处理后再执行'}
                  </Text>
                  <Text className="text-[10px] text-slate-500">
                    检查时间：{new Date(precheck.checked_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
                  </Text>
                </div>
                <div className="flex flex-wrap justify-end gap-2">
                  <Tag className="m-0 rounded-full border-0 bg-slate-100 text-slate-600 font-bold">模型 {modelIdToDisplayName(precheck.model_id)}</Tag>
                  <Tag className="m-0 rounded-full border-0 bg-blue-50 text-blue-700 font-bold">{precheck.prediction_trade_date}</Tag>
                </div>
              </div>
            </div>

            <div className="grid gap-2">
              {precheck.items.map((item) => (
                <div
                  key={item.key}
                  className={clsx(
                    'flex items-start justify-between gap-3 rounded-2xl border px-4 py-3',
                    item.passed ? 'border-slate-100 bg-white' : 'border-rose-100 bg-rose-50/50',
                  )}
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      {item.passed ? <CheckCircle2 size={12} className="text-emerald-500 flex-shrink-0" /> : <XCircle size={12} className="text-rose-500 flex-shrink-0" />}
                      <Text className="text-xs font-black text-slate-800">{item.label}</Text>
                      <Tag className={clsx('m-0 rounded-full border-0 text-[9px] font-bold', item.severity === 'hard' ? 'bg-rose-50 text-rose-500' : 'bg-slate-100 text-slate-500')}>
                        {item.severity === 'hard' ? '硬门禁' : '提示'}
                      </Tag>
                    </div>
                    <Text className="mt-1 block text-[10px] text-slate-500 break-all">{item.detail}</Text>
                  </div>
                  <Tag color={item.passed ? 'green' : 'red'} className="m-0 rounded-full text-[9px] font-black">
                    {item.passed ? '通过' : '未通过'}
                  </Tag>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <Empty description={<span className="text-xs text-slate-400">暂无检查结果，点击右上角刷新</span>} />
        )}
      </Spin>
    </Card>

    {/* ── 当前生效推理批次 ── */}
    <Card
      className="rounded-3xl border-slate-100 shadow-sm"
      title={
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Zap size={14} className="text-emerald-500" />
            <span className="text-xs font-black text-slate-800 uppercase tracking-tighter">当前生效推理批次</span>
          </div>
          <Tag className="m-0 rounded-full border-0 bg-emerald-50 text-emerald-600 font-black text-[9px]">
            交易侧生效版本
          </Tag>
        </div>
      }
    >
      <Spin spinning={latestInferenceRunLoading}>
        {latestInferenceRun?.run_id ? (
          <div className={clsx(
            'rounded-2xl border p-4',
            latestInferenceRun.matched_model === false ? 'border-emerald-100 bg-emerald-50/60' : 'border-slate-100 bg-slate-50',
          )}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <Text className="text-[10px] text-slate-400 font-black uppercase block">run_id</Text>
                <Text className="text-sm font-black text-slate-900 font-mono break-all">
                  {latestInferenceRun.run_id}
                </Text>
              </div>
              <div className="flex flex-wrap justify-end gap-2">
                {latestInferenceRun.status && (
                  <Tag className="m-0 rounded-full border-0 bg-blue-50 text-blue-700 font-bold">
                    {latestInferenceRun.status}
                  </Tag>
                )}
                {latestInferenceRun.prediction_trade_date && (
                  <Tag className="m-0 rounded-full border-0 bg-emerald-50 text-emerald-700 font-bold">
                    {latestInferenceRun.prediction_trade_date}
                  </Tag>
                )}
                {latestInferenceRun.matched_model !== null && (
                  <Tag className={clsx(
                    'm-0 rounded-full border-0 font-bold',
                    latestInferenceRun.matched_model ? 'bg-emerald-50 text-emerald-700' : 'bg-emerald-50 text-emerald-700',
                  )}>
                    {latestInferenceRun.matched_model ? '当前模型匹配' : '当前模型不匹配'}
                  </Tag>
                )}
              </div>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2">
              <div className="rounded-xl bg-white px-3 py-2 border border-slate-100">
                <Text className="text-[10px] text-slate-400 font-black uppercase block">模型</Text>
                <Text className="text-xs font-black text-slate-800 break-all" title={latestInferenceRun.model_id || ''}>{latestRunModelLabel}</Text>
              </div>
              <div className="rounded-xl bg-white px-3 py-2 border border-slate-100">
                <Text className="text-[10px] text-slate-400 font-black uppercase block">更新时间</Text>
                <Text className="text-xs font-black text-slate-800 font-mono break-all">{formatPanelDateTime(latestInferenceRun.updated_at)}</Text>
              </div>
            </div>
          </div>
        ) : (
          <Empty description={<span className="text-xs text-slate-400">暂无当前生效推理批次</span>} />
        )}
      </Spin>
    </Card>

    {/* ── 手动推理卡片 ── */}
    <Card
      className="rounded-3xl border-slate-100 shadow-sm"
      title={
        <div className="flex items-center gap-2">
          <Play size={14} className="text-blue-600" />
          <span className="text-xs font-black text-slate-800 uppercase tracking-tighter">手动推理</span>
        </div>
      }
    >
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <div>
            <Text className="text-[10px] font-black text-slate-400 uppercase block mb-1.5">推理基准日期</Text>
            <DatePicker
              value={inferenceDate}
              onChange={onDateChange}
              disabledDate={d => d.isAfter(dayjs())}
              className="w-full rounded-xl h-10 border-slate-200 text-xs"
              placeholder="选择行情日期"
            />
          </div>
          <div>
            <Text className="text-[10px] font-black text-slate-400 uppercase block mb-1.5">
              预测目标日期（T+{horizonDays}）
            </Text>
            <div className="h-10 flex items-center px-4 bg-blue-50/60 rounded-xl border border-blue-100">
              <Calendar size={12} className="text-blue-400 mr-2 flex-shrink-0" />
              <Text className="font-black text-sm text-blue-700">{targetDateLoading ? '计算中…' : targetDate}</Text>
              <Tag className="ml-2 text-[8px] font-black bg-blue-100 border-none text-blue-500">下一交易日</Tag>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex-1 bg-slate-50 rounded-xl px-4 py-2 flex items-center gap-2 min-w-0">
            <Brain size={11} className="text-slate-400 flex-shrink-0" />
            <Text className="text-[10px] font-bold text-slate-400">模型：</Text>
            <Text className="text-[10px] font-black text-slate-800 truncate" title={model.model_id}>{currentModelName}</Text>
            {model.is_default && <Tag color="gold" className="text-[8px] font-black flex-shrink-0">默认</Tag>}
          </div>
          {model.is_default && onRunAsDefault && (
            <Button
              type="primary"
              icon={<Zap size={13} />}
              loading={running}
              disabled={!inferenceDate || precheckLoading || !precheck?.passed}
              className="rounded-xl h-10 px-4 bg-emerald-600 border-none font-black text-xs shadow-lg shadow-emerald-200 flex-shrink-0"
              onClick={onRunAsDefault}
            >
              {running ? '推理中…' : '生成生产批次'}
            </Button>
          )}
          <Button
            type="primary"
            icon={<Play size={13} />}
            loading={running}
            disabled={!inferenceDate || precheckLoading || !precheck?.passed}
            className="rounded-xl h-10 px-6 bg-blue-600 border-none font-black text-xs shadow-lg shadow-blue-200 flex-shrink-0"
            onClick={onRun}
          >
            {running ? '推理中…' : '生成调试批次'}
          </Button>
        </div>

        {model.is_default && (
          <div className="rounded-2xl border border-emerald-100 bg-emerald-50/60 px-4 py-3">
            <Text className="block text-[10px] font-black uppercase tracking-widest text-emerald-600">生产 / 调试批次说明</Text>
            <Text className="mt-1 block text-xs leading-relaxed text-emerald-800">
              "生成生产批次"会按用户默认模型链路记账，产出的批次可直接进入自动托管；"生成调试批次"会按当前手动指定模型记账，只用于调试、比对和人工核查，不会被自动托管直接消费。
            </Text>
          </div>
        )}

        {running && (
          <div className="space-y-1">
            <Text className="text-[10px] text-slate-400 font-bold">正在计算股票因子排名…</Text>
            <Progress percent={99} status="active" size="small" strokeColor="#3b82f6" />
          </div>
        )}

        {lastRun && !running && (
          <div className={clsx(
            'flex items-center justify-between px-4 py-3 rounded-2xl border',
            lastRun.status === 'failed' ? 'bg-rose-50 border-rose-200' : 'bg-emerald-50 border-emerald-200',
          )}>
            <div className="flex items-center gap-2">
              {lastRun.status === 'failed'
                ? <XCircle size={15} className="text-rose-500" />
                : <CheckCircle2 size={15} className="text-emerald-500" />}
              <div>
                <Text className={clsx(
                  'text-xs font-black block',
                  lastRun.status === 'failed' ? 'text-rose-800' : 'text-emerald-800',
                )}>
                  {lastRun.status === 'failed' ? '推理失败' : '推理完成'} · {lastRun.signals_count} 支信号
                </Text>
                <Text className={clsx(
                  'text-[10px]',
                  lastRun.status === 'failed' ? 'text-rose-500' : 'text-emerald-500',
                )}>
                  目标日：{lastRun.target_date} · 耗时 {(lastRun.duration_ms / 1000).toFixed(1)}s
                  {lastRun.fallback_used ? ` · 已兜底${lastRun.fallback_reason ? `：${lastRun.fallback_reason}` : ''}` : ''}
                </Text>
              </div>
            </div>
            <Button size="small" className={clsx(
              'rounded-xl font-black text-xs',
              lastRun.status === 'failed' ? 'border-rose-300 text-rose-700' : 'border-emerald-300 text-emerald-700',
            )}
              onClick={() => onViewRanking(lastRun.run_id)}>
              查看详情
            </Button>
          </div>
        )}
      </div>
    </Card>

    {/* ── 自动推理设置 ── */}
    <Card
      className="rounded-3xl border-slate-100 shadow-sm"
      title={
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Clock size={14} className="text-indigo-500" />
            <span className="text-xs font-black text-slate-800 uppercase tracking-tighter">自动生产批次</span>
            {autoSettings?.enabled && (
              <Badge status="processing" text={<span className="text-[9px] font-bold text-blue-500">已开启</span>} />
            )}
          </div>
          <Switch
            checked={autoSettings?.enabled ?? false}
            loading={autoSaving}
            onChange={onToggleAuto}
            checkedChildren="开启"
            unCheckedChildren="关闭"
          />
        </div>
      }
    >
      <div className="space-y-3">
        {autoSettings?.schedule_desc && (
          <div className="flex items-start gap-4 p-4 bg-indigo-50/70 rounded-2xl border border-indigo-100">
            <Calendar size={14} className="text-indigo-400 mt-1 flex-shrink-0" />
            <div className="flex-1">
              <span className="text-xs font-black text-indigo-800 block mb-1">执行时机</span>
              <span className="text-[11px] text-indigo-500 leading-relaxed block">
                {autoSettings.schedule_desc}
              </span>
              <div className="mt-2 py-1.5 px-2 bg-white/60 rounded-lg border border-indigo-100/50">
                <span className="text-[10px] text-indigo-400 font-medium flex items-center gap-1.5">
                  <Activity size={10} /> 
                  数据就绪后自动按默认模型链路生成生产批次
                </span>
              </div>
            </div>
          </div>
        )}
        <div className="grid grid-cols-2 gap-3">
          <div className="bg-slate-50 rounded-xl p-3">
            <Text className="text-[10px] text-slate-400 font-black uppercase block mb-1">上次生产批次</Text>
            {autoSettings?.last_run ? (
              <>
                <Text className="text-[10px] font-black text-slate-700 block">
                  {formatPanelDateTime(autoSettings.last_run.created_at, '时间未记录')}
                </Text>
                <Tag color={autoSettings.last_run.status === 'failed' ? 'red' : 'green'} className="text-[9px] font-bold mt-1">
                  {autoSettings.last_run.status === 'failed' ? '失败' : '成功'} · {autoSettings.last_run.signals_count} 支
                </Tag>
                {autoSettings.last_run.run_id && (
                  <div className="mt-2">
                    <Text className="text-[10px] text-slate-400 font-black uppercase block">run_id</Text>
                    <Text className="text-[10px] font-mono text-slate-600 break-all">{autoSettings.last_run.run_id}</Text>
                  </div>
                )}
              </>
            ) : <Text className="text-[10px] text-slate-300">尚未执行</Text>}
          </div>
          <div className="bg-slate-50 rounded-xl p-3">
            <Text className="text-[10px] text-slate-400 font-black uppercase block mb-1">下次生产计划</Text>
            {autoSettings?.enabled && autoSettings.next_run
              ? <Text className="text-[10px] font-black text-slate-700">{formatPanelDateTime(autoSettings.next_run)}</Text>
              : <Text className="text-[10px] text-slate-300">—（未开启）</Text>}
          </div>
        </div>
      </div>
    </Card>

    {/* ── 推理历史 ── */}
    <Card
      className="rounded-3xl border-slate-100 shadow-sm"
      title={
        <div className="flex items-center gap-2">
          <History size={14} className="text-slate-500" />
          <span className="text-xs font-black text-slate-800 uppercase tracking-tighter">推理历史</span>
        </div>
      }
    >
      <div className="mb-4 grid grid-cols-1 gap-3 lg:grid-cols-3">
        <Input
          value={historyRunIdFilter}
          onChange={e => onHistoryRunIdFilterChange(e.target.value)}
          placeholder="按 run_id 过滤"
          className="rounded-xl h-9 text-xs border-slate-200"
        />
        <Select
          value={historyStatusFilter}
          onChange={value => onHistoryStatusFilterChange(value as 'all' | 'running' | 'completed' | 'failed')}
          className="w-full"
          size="middle"
          options={[
            { value: 'all', label: '全部状态' },
            { value: 'running', label: '进行中' },
            { value: 'completed', label: '成功' },
            { value: 'failed', label: '失败' },
          ]}
        />
        <DatePicker
          value={historyDateFilter}
          onChange={onHistoryDateFilterChange}
          className="w-full rounded-xl h-9 border-slate-200 text-xs"
          placeholder="按日期过滤"
        />
      </div>
      <Spin spinning={historyLoading}>
          <Table
          size="small"
          rowKey="run_id"
          pagination={false}
          dataSource={history}
          locale={{ emptyText: historyLoading ? ' ' : '暂无推理记录，可点击"生成生产批次"或"生成调试批次"开始' }}
          columns={[
            {
              title: '状态', dataIndex: 'status', width: 72, align: 'center',
              render: (s: string) => {
                const cfgMap: Record<string, { icon: React.ReactNode; text: string; cls: string }> = {
                  completed: { icon: <CheckCircle2 size={11} />, text: '成功', cls: 'text-emerald-600' },
                  failed: { icon: <XCircle size={11} />, text: '失败', cls: 'text-red-500' },
                  running: { icon: <Activity size={11} />, text: '进行中', cls: 'text-blue-500' },
                };
                const c = cfgMap[s] ?? { icon: <Clock size={11} />, text: s, cls: 'text-slate-400' };
                return <span className={clsx('flex items-center justify-center gap-1 font-black text-[10px]', c.cls)}>{c.icon}{c.text}</span>;
              },
            },
            {
              title: '基准日期', dataIndex: 'inference_date',
              align: 'center',
              render: (d: string) => <Text className="block text-center text-xs font-mono text-slate-600">{d}</Text>,
            },
            {
              title: '预测目标', dataIndex: 'target_date',
              align: 'center',
              render: (d: string) => <Text className="block text-center text-xs font-mono font-black text-blue-600">{d}</Text>,
            },
            {
              title: '信号数', dataIndex: 'signals_count',
              align: 'center',
              render: (n: number, r: any) => r.status === 'failed'
                ? <div className="text-center"><Tooltip title={r.error_msg}><Tag color="red" className="text-[9px] font-bold cursor-help">失败</Tag></Tooltip></div>
                : <Text className="block text-center text-xs font-black text-slate-700">{n}</Text>,
            },
            {
              title: '耗时', dataIndex: 'duration_ms',
              align: 'center',
              render: (ms: number) => <Text className="block text-center text-[10px] text-slate-400">{(ms / 1000).toFixed(1)}s</Text>,
            },
            {
              title: '操作', key: 'action', align: 'center',
              render: (_: any, r: InferenceRunRecord) => (
                <div className="text-center">
                  <Button type="link" size="small" className="font-black text-blue-600 p-0 text-[11px]" onClick={() => onViewRanking(r.run_id)}>
                    查看详情
                  </Button>
                </div>
              ),
            },
          ]}
        />
      </Spin>
    </Card>
  </div>
  );
};

// ─── 通用子组件 ──────────────────────────────────────────────────────────────

export const MetricCard: React.FC<{ label: string; value: any; digits?: number; color?: string; isLarge?: boolean }> = ({
  label, value, digits = 3, color = 'text-slate-800', isLarge = false,
}) => (
  <div className="bg-white border border-slate-100 rounded-2xl p-4 shadow-sm hover:shadow-md transition-shadow flex min-h-[112px] flex-col items-center justify-center text-center">
    <Text className="text-[10px] text-slate-400 font-black uppercase tracking-widest block mb-1 w-full text-center">{label}</Text>
    <Text className={clsx('font-black tracking-tighter block w-full', isLarge ? 'text-2xl' : 'text-xl', color)}>
      {value === null || value === undefined ? '—' : typeof value === 'number' ? value.toFixed(digits) : value}
    </Text>
  </div>
);

export const TimeItem: React.FC<{
  label: string;
  range: [string, string];
  color: string;
  percent: number;
  days: number;
  note: string;
  surface: string;
  border: string;
  text: string;
}> = ({
  label, range, color, percent, days, note, surface, border, text,
}) => (
  <div className={clsx('relative overflow-hidden rounded-2xl border p-4 shadow-sm transition-shadow hover:shadow-md', surface, border)}>
    <div className={clsx('absolute inset-x-0 top-0 h-1', color)} />
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <div className={clsx('h-2.5 w-2.5 rounded-full flex-shrink-0', color)} />
          <Text className={clsx('text-[10px] font-black uppercase tracking-[0.22em] truncate', text)}>{label}</Text>
        </div>
        <div className="mt-2 text-xs text-slate-500 leading-relaxed">{note}</div>
      </div>
      <Tag className="m-0 rounded-full border-0 bg-white text-slate-700 font-bold">{percent}%</Tag>
    </div>

    <div className="mt-4 grid grid-cols-2 gap-2">
      <InfoCell label="FROM" value={range[0]} />
      <InfoCell label="TO" value={range[1]} />
    </div>

    <div className="mt-3 flex items-center justify-between">
      <span className="text-[10px] font-black uppercase tracking-[0.22em] text-slate-400">区间长度</span>
      <span className="text-xs font-black text-slate-800">{days} 天</span>
    </div>
  </div>
);

export const InfoCell: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div className="rounded-xl border border-slate-200 bg-white px-3 py-2">
    <Text className="block text-[9px] font-black uppercase tracking-[0.22em] text-slate-400">{label}</Text>
    <Text className="mt-1 block text-[11px] font-black text-slate-800 font-mono">{value}</Text>
  </div>
);
