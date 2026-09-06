import React, { useMemo, useState, useEffect, useCallback } from 'react';
import { Button, Card, Tag, Typography, Empty, Spin, Progress, Divider, Input, Modal, Tabs, Switch, DatePicker, Table, Drawer, Badge, Tooltip, Collapse, Select, Pagination, message, Space, Alert } from 'antd';
import { clsx } from 'clsx';
import dayjs from 'dayjs';
import {
  Layers, Star, RefreshCw, Search, Code, Calendar, Layers2,
  History, Archive, Brain, CheckCircle2, Clock, XCircle, Trash2,
  ChevronRight, Play, Cpu, TrendingUp, Download, ChevronDown,
  ChevronUp, Shield, Zap, Activity, ListFilter, BarChart3, Info, AlertCircle,
  Check,
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
  ModelShapSummaryItem, modelTrainingService,
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

const MARKET_LABELS: Record<string, string> = {
  CN: 'A股',
  HK: '港股',
  US: '美股',
  CRYPTO: '区块链',
  FUTURES: '期货',
};

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

/** WFA 诊断解读：基于 IC 均值/标准差/正窗占比/ICIR 组合判断，输出可读结论 */
export const WfaInterpretation: React.FC<{ wfa: any }> = ({ wfa }) => {
  if (!wfa || !wfa.enabled) return null;
  const icMean = Number(wfa.ic_mean);
  const icStd = Number(wfa.ic_std);
  const positiveRate = Number(wfa.positive_rate);
  const icir = Number(wfa.overall_icir);
  const hasIcir = Number.isFinite(icir) && !Number.isNaN(icir);

  // 逐维度判断
  const checks: Array<{ label: string; ok: boolean; text: string }> = [];
  // 1. IC 方向与强度
  if (icMean >= 0.05) checks.push({ label: 'IC 强度', ok: true, text: `IC均值 ${icMean.toFixed(4)} ≥ 0.05，信号强度良好` });
  else if (icMean >= 0) checks.push({ label: 'IC 强度', ok: true, text: `IC均值 ${icMean.toFixed(4)} 为正，信号有效但偏弱（<0.05）` });
  else checks.push({ label: 'IC 强度', ok: false, text: `IC均值 ${icMean.toFixed(4)} 为负，信号方向可能反了` });

  // 2. IC 稳定性
  if (icStd <= 0.02) checks.push({ label: 'IC 稳定性', ok: true, text: `标准差 ${icStd.toFixed(4)} ≤ 0.02，各窗口波动小` });
  else checks.push({ label: 'IC 稳定性', ok: false, text: `标准差 ${icStd.toFixed(4)} > 0.02，各窗口波动偏大` });

  // 3. 正向窗口占比
  if (positiveRate >= 0.75) checks.push({ label: '正窗占比', ok: true, text: `${Math.round(positiveRate * 100)}% 窗口 IC 为正，跨期一致性好` });
  else if (positiveRate >= 0.5) checks.push({ label: '正窗占比', ok: true, text: `${Math.round(positiveRate * 100)}% 窗口为正，存在少数走弱窗口` });
  else checks.push({ label: '正窗占比', ok: false, text: `仅 ${Math.round(positiveRate * 100)}% 窗口为正，多数窗口失效` });

  // 4. ICIR 综合
  if (hasIcir) {
    if (Math.abs(icir) >= 0.3) checks.push({ label: 'ICIR', ok: true, text: `ICIR ${icir.toFixed(3)}，收益/波动比合理` });
    else checks.push({ label: 'ICIR', ok: false, text: `ICIR ${icir.toFixed(3)} < 0.3，信号相对波动偏弱` });
  }

  const okCount = checks.filter(c => c.ok).length;

  return (
    <div className="mt-3 rounded-xl bg-white/70 border border-slate-100/60 p-3">
      <div className="flex items-center justify-between mb-2">
        <Text className="text-[9px] font-black text-slate-500 uppercase tracking-wider">判断解读</Text>
        <Text className={clsx('text-[9px] font-black', okCount === checks.length ? 'text-emerald-600' : okCount >= 2 ? 'text-amber-600' : 'text-rose-500')}>
          {okCount}/{checks.length} 项达标
        </Text>
      </div>
      <div className="space-y-1">
        {checks.map((c, i) => (
          <div key={i} className="flex items-start gap-1.5">
            <span className={clsx('mt-0.5 text-[8px] font-black', c.ok ? 'text-emerald-500' : 'text-rose-400')}>{c.ok ? '✓' : '✗'}</span>
            <Text className={clsx('text-[10px] leading-snug', c.ok ? 'text-slate-600' : 'text-rose-500/80')}>
              <span className="font-bold text-slate-500">{c.label}：</span>{c.text}
            </Text>
          </div>
        ))}
      </div>
      <Text className="block mt-2 text-[10px] text-slate-400 leading-relaxed">
        {okCount === checks.length
          ? '整体稳定可用，适合作为选股模型。'
          : okCount >= 2
            ? '多数维度达标，个别窗口波动可接受，建议关注 IC 表现最弱的区间。'
            : '多个维度未达标，建议调整特征/参数后重新训练，或考虑融合其他模型。'}
      </Text>
    </div>
  );
};

// ─── 左侧模型卡片 ────────────────────────────────────────────────────────────
export const ModelCard: React.FC<{
  model: UserModelRecord;
  isSelected: boolean;
  onClick: () => void;
  onSetDefault: () => void;
  canSetDefault: boolean;
  isChecked?: boolean;
  showCheckbox?: boolean;
}> = ({ model, isSelected, onClick, onSetDefault, canSetDefault, isChecked = false, showCheckbox = false }) => {
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
          : isChecked
            ? 'bg-blue-50/60 border-blue-300 shadow-sm'
            : 'bg-transparent border-transparent hover:bg-white hover:border-slate-200 hover:shadow-sm'
      )}
    >
      <div className="flex justify-between items-start mb-1.5 gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          {showCheckbox && (
            <span className={clsx(
              'inline-flex items-center justify-center h-3.5 w-3.5 rounded border transition-all flex-shrink-0',
              isChecked ? 'bg-blue-600 border-blue-600' : 'bg-white border-slate-300'
            )}>
              {isChecked && <Check size={9} className="text-white" strokeWidth={3.5} />}
            </span>
          )}
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
        {getMeta(model).market && (
          <Tag className="m-0 rounded-md border-0 px-1.5 py-0 text-[8px] font-black bg-slate-100 text-slate-500">
            {MARKET_LABELS[getMeta(model).market.toUpperCase()] || getMeta(model).market}
          </Tag>
        )}
        {getMeta(model).is_ensemble && (
          <Tag className="m-0 rounded-md border-0 px-1.5 py-0 text-[8px] font-black bg-indigo-50 text-indigo-600">多周期</Tag>
        )}
        {fc && <><span className="inline-block h-2 w-px bg-slate-300 mx-1" /><Text className="text-[9px] text-slate-400 font-mono font-bold">{fc}维</Text></>}
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
      trendLabel: formatTrendLabel(ic, previousIc, 2),
    });
    previousIc = ic ?? previousIc;
  }
  const hasSplitIC = splitMetrics.length > 0;
  const ic = metrics.ic ?? metrics.IC ?? metrics.mean_ic ?? resolveMetricNumber(metadataMetrics, ['test_ic', 'val_ic', 'train_ic']) ?? null;
  const icir = metrics.icir ?? metrics.ICIR ?? metrics.IC_IR ?? resolveMetricNumber(metadataMetrics, ['test_rank_icir', 'val_rank_icir', 'test_icir', 'val_icir', 'train_rank_icir', 'train_icir']) ?? null;
  const hasIC = [ic, icir].some(v => v !== null);

  const horizonDays = meta.target_horizon_days ?? meta.horizon_days;
  const targetMode = String(meta.target_mode ?? '').toLowerCase();
  const labelFormula = meta.label_formula ?? meta.labelFormula;
  const targetModeLabel = targetMode ? (targetMode === 'classification' ? '分类' : (targetMode === 'regression' || targetMode === 'return' ? '回归' : targetMode)) : '';
  const features: string[] = Array.isArray(meta.features) ? meta.features : [];
  const modelParams = meta.model_params && typeof meta.model_params === 'object' ? meta.model_params as Record<string, any> : null;
  const KEY_PARAMS = ['num_leaves', 'learning_rate', 'max_depth', 'n_estimators', 'num_boost_round', 'subsample', 'colsample_bytree', 'reg_alpha', 'reg_lambda'];
  const importantParams = modelParams ? KEY_PARAMS.filter(k => modelParams[k] !== undefined) : [];
  
  if (!hasSplitIC && !hasIC && !timePeriods) {
    return (
      <div className="pt-10">
        <Empty description={<span className="text-xs text-slate-400 font-medium tracking-wider">该模型暂无详细训练指标或时间轴数据</span>} />
      </div>
    );
  }

  return (
    <div className="pt-2">
      <div className="grid grid-cols-12 gap-6 items-start">
        <div className="col-span-7 space-y-5">
          <div className="grid grid-cols-3 gap-4">
            {splitMetrics.map((item) => (
              <div 
                key={item.key} 
                className={clsx(
                  "rounded-2xl p-5 border relative group transition-all duration-300 shadow-sm hover:shadow-md",
                  "bg-gradient-to-br",
                  item.color === 'blue' ? 'from-blue-50/50 to-slate-100/80 border-blue-100/60' : 
                  item.color === 'indigo' ? 'from-indigo-50/50 to-slate-100/80 border-indigo-100/60' : 
                  'from-emerald-50/50 to-slate-100/80 border-emerald-100/60'
                )}
              >
                <div className="flex items-center justify-between mb-3">
                  <Text className="text-[10px] font-black text-slate-500 uppercase tracking-widest opacity-80">{item.label}</Text>
                  <div className={clsx(
                    "h-2 w-2 rounded-full",
                    item.color === 'blue' && 'bg-blue-500 shadow-[0_0_8px_rgba(59,130,246,0.4)]',
                    item.color === 'indigo' && 'bg-indigo-500 shadow-[0_0_8px_rgba(99,102,241,0.4)]',
                    item.color === 'emerald' && 'bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.4)]',
                  )} />
                </div>
                
                <div className="flex flex-col items-center py-1">
                  <Text className="text-3xl font-black text-slate-800 font-mono tracking-tighter mb-3 drop-shadow-sm">
                    {item.ic === null ? '—' : item.ic.toFixed(4)}
                  </Text>
                  
                  <div className="flex flex-col items-center gap-1.5 w-full">
                    <div className={clsx(
                      "px-2 py-0.5 rounded text-[9px] font-black tracking-wider uppercase whitespace-nowrap shadow-sm",
                      item.color === 'blue' ? 'bg-blue-600 text-white' : 
                      item.color === 'indigo' ? 'bg-indigo-600 text-white' : 'bg-emerald-600 text-white'
                    )}>
                      IR {item.icir?.toFixed(3) || '—'}
                    </div>

                    <div className={clsx(
                      "text-[9px] font-bold flex items-center justify-center gap-1 px-2 py-0.5 rounded-full border whitespace-nowrap bg-white/60 shadow-inner",
                      item.trendLabel === '基线' ? 'border-slate-100 text-slate-400' :
                      item.trendLabel.includes('+') ? 'border-rose-100 text-rose-600 bg-rose-50/50' : 'border-emerald-100 text-emerald-600 bg-emerald-50/50'
                    )}>
                      {item.trendLabel === '基线' ? <Activity size={8} /> : item.trendLabel.includes('+') ? <ChevronUp size={8} /> : <ChevronDown size={8} />}
                      <span className="opacity-70">较上段</span>
                      <span className="font-black">
                        {item.trendLabel === '基线'
                          ? '基线'
                          : `${item.trendLabel.replace(/^较上段\s*/, '').replace('%', '')}%`}
                      </span>
                    </div>
                  </div>
                </div>
                <BarChart3 size={40} className="absolute -bottom-1 -right-1 text-slate-400/5 group-hover:text-slate-400/10 transition-colors pointer-events-none" />
              </div>
            ))}
          </div>

          <div className="bg-slate-50/50 rounded-2xl p-4 border border-slate-100 flex gap-3 items-start">
            <Info size={14} className="text-slate-400 mt-1" />
            <Text className="text-[11px] text-slate-500 leading-relaxed">
              <span className="font-bold text-slate-700">指标解读：</span>
              训练集反映拟合能力，验证集用于参数选择，测试集代表模拟泛化。IC 衰减控制在 10% 以内视为模型鲁棒性良好。
            </Text>
          </div>

          {features.length > 0 && (
            <div className="glass-panel rounded-3xl p-6 border border-slate-100/50">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2">
                  <div className="h-4 w-1 bg-violet-500 rounded-full" />
                  <Text className="text-xs font-black text-slate-800 uppercase">特征工程资产 ({features.length})</Text>
                </div>
              </div>
              <div className="flex flex-wrap gap-1.5 max-h-[300px] overflow-y-auto custom-scrollbar">
                {features.map((f, i) => (
                  <Tag key={i} className="m-0 px-2 py-0.5 rounded-md border-0 bg-slate-100/80 text-slate-600 text-[10px] font-mono hover:bg-violet-50 hover:text-violet-600 transition-colors cursor-default">
                    {f}
                  </Tag>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="col-span-5 space-y-5">
          <div className="glass-panel rounded-3xl p-5 border border-slate-100/50">
            <Text className="text-[10px] font-black text-slate-400 uppercase tracking-widest block mb-4">模型配置 Profile</Text>
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 text-slate-500"><Calendar size={13} /><Text className="text-xs font-medium">预测周期</Text></div>
                {meta.is_ensemble && (meta.source_models?.length ?? 0) > 0 ? (
                  <Tag className="m-0 bg-indigo-50 text-indigo-600 border-0 font-black rounded-md px-2 py-0.5">多周期融合</Tag>
                ) : (
                  <Text className="text-sm font-black text-slate-800">T + {horizonDays || '—'}</Text>
                )}
              </div>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 text-slate-500"><Zap size={13} /><Text className="text-xs font-medium">任务类型</Text></div>
                <Tag className="m-0 bg-slate-100 border-0 text-slate-600 font-bold rounded-md px-2 py-0.5">{targetModeLabel || '未知'}</Tag>
              </div>
              <div className="pt-2 border-t border-dashed border-slate-100">
                <div className="flex items-center gap-2 text-slate-500 mb-2"><Code size={13} /><Text className="text-xs font-medium">标签公式</Text></div>
                <div className="bg-slate-50 rounded-xl p-3 border border-slate-100/50">
                  <Text className="text-[10px] font-mono text-slate-500 break-all leading-relaxed block">{String(labelFormula || '—')}</Text>
                </div>
              </div>
            </div>
          </div>

          {timePeriods && (() => {
            const splitStats = calcTimeSplitStats(timePeriods);
            const segments = [
              { label: '训练集', range: timePeriods.train, days: splitStats.train.days, color: 'bg-blue-500' },
              ...(splitStats.val ? [{ label: '验证集', range: timePeriods.val, days: splitStats.val.days, color: 'bg-indigo-500' }] : []),
              ...(splitStats.test ? [{ label: '测试集', range: timePeriods.test, days: splitStats.test.days, color: 'bg-emerald-500' }] : []),
            ];
            return (
              <div className="glass-panel rounded-3xl p-5 border border-slate-100/50">
                <div className="flex items-center justify-between mb-6">
                  <Text className="text-[10px] font-black text-slate-400 uppercase tracking-widest">样本时间轴</Text>
                  <Text className="text-[10px] font-black text-slate-800">{splitStats.totalDays}D Total</Text>
                </div>
                <div className="relative pl-6 space-y-8 before:absolute before:left-[11px] before:top-2 before:bottom-2 before:w-0.5 before:bg-slate-100">
                  {segments.map((s, idx) => (
                    <div key={idx} className="relative">
                      <div className={clsx("absolute -left-[19px] top-1.5 h-2 w-2 rounded-full ring-4 ring-white", s.color)} />
                      <div className="flex flex-col">
                        <div className="flex items-center justify-between mb-1">
                          <Text className="text-xs font-black text-slate-800">{s.label}</Text>
                          <Text className="text-[10px] font-mono font-bold text-slate-400">{s.days} 天</Text>
                        </div>
                        <Text className="text-[10px] text-slate-400 font-mono mb-1">{s.range[0]} → {s.range[1]}</Text>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            );
          })()}

          {modelParams && importantParams.length > 0 && (
            <div className="glass-panel rounded-3xl p-5 border border-slate-100/50">
              <div className="flex items-center gap-2 mb-4">
                <Text className="text-[10px] font-black text-slate-400 uppercase tracking-widest">核心超参 PARAMS</Text>
              </div>
              <div className="grid grid-cols-2 gap-3">
                {importantParams.map(k => (
                  <div key={k} className="flex flex-col gap-0.5 p-2 bg-slate-50/50 rounded-xl border border-slate-100/50">
                    <Text className="text-[8px] text-slate-400 uppercase truncate">{k.replace(/_/g, ' ')}</Text>
                    <Text className="text-[11px] font-black text-slate-700">{String(modelParams[k])}</Text>
                  </div>
                ))}
              </div>
            </div>
          )}

          {meta.wfa?.enabled && meta.wfa.windows?.length > 0 && (
            <div className="glass-panel rounded-3xl p-5 border border-violet-100/60 bg-gradient-to-br from-violet-50/30 to-white">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2">
                  <Activity size={13} className="text-violet-500" />
                  <Text className="text-[10px] font-black text-slate-400 uppercase tracking-widest">WFA 稳定性诊断</Text>
                </div>
                <Tag className={clsx('m-0 rounded-full border-0 px-2 py-0.5 text-[9px] font-black', meta.wfa.stability === 'stable' ? 'bg-emerald-50 text-emerald-600' : 'bg-amber-50 text-amber-600')}>
                  {meta.wfa.stability === 'stable' ? '稳定' : '不稳定'}
                </Tag>
              </div>

              <div className="grid grid-cols-2 gap-2 mb-4">
                <div className="flex flex-col items-center gap-0.5 p-2 bg-white/70 rounded-xl border border-slate-100/60">
                  <Text className="text-[8px] text-slate-400 uppercase tracking-wider">IC 均值</Text>
                  <Text className={clsx('text-base font-black font-mono', Number(meta.wfa.ic_mean) >= 0.05 ? 'text-emerald-600' : Number(meta.wfa.ic_mean) >= 0 ? 'text-amber-600' : 'text-rose-500')}>
                    {Number(meta.wfa.ic_mean).toFixed(4)}
                  </Text>
                </div>
                <div className="flex flex-col items-center gap-0.5 p-2 bg-white/70 rounded-xl border border-slate-100/60">
                  <Text className="text-[8px] text-slate-400 uppercase tracking-wider">IC 标准差</Text>
                  <Text className={clsx('text-base font-black font-mono', Number(meta.wfa.ic_std) <= 0.02 ? 'text-emerald-600' : 'text-amber-600')}>
                    {Number(meta.wfa.ic_std).toFixed(4)}
                  </Text>
                </div>
                <div className="flex flex-col items-center gap-0.5 p-2 bg-white/70 rounded-xl border border-slate-100/60">
                  <Text className="text-[8px] text-slate-400 uppercase tracking-wider">ICIR</Text>
                  <Text className="text-base font-black font-mono text-slate-700">
                    {Number.isFinite(Number(meta.wfa.overall_icir)) ? Number(meta.wfa.overall_icir).toFixed(3) : '—'}
                  </Text>
                </div>
                <div className="flex flex-col items-center gap-0.5 p-2 bg-white/70 rounded-xl border border-slate-100/60">
                  <Text className="text-[8px] text-slate-400 uppercase tracking-wider">正窗占比</Text>
                  <Text className="text-base font-black font-mono text-slate-700">{Math.round(Number(meta.wfa.positive_rate) * 100)}%</Text>
                </div>
              </div>

              <div className="space-y-1.5 max-h-[240px] overflow-y-auto custom-scrollbar pr-1">
                {meta.wfa.windows.map((w: any) => {
                  const ic = Number(w.ic);
                  return (
                    <div key={w.window_idx} className="flex items-center gap-2 px-2 py-1.5 bg-white/60 rounded-lg border border-slate-100/50">
                      <Text className="text-[9px] font-black text-slate-500 font-mono w-7">W{w.window_idx + 1}</Text>
                      <div className="flex-1 min-w-0">
                        <Text className="text-[9px] text-slate-400 font-mono block truncate">{w.val_start} → {w.val_end}</Text>
                      </div>
                      <Text className={clsx('text-[10px] font-black font-mono', ic >= 0.05 ? 'text-emerald-600' : ic >= 0 ? 'text-amber-600' : 'text-rose-500')}>
                        {ic.toFixed(4)}
                      </Text>
                    </div>
                  );
                })}
              </div>

              <div className="mt-3 flex flex-wrap gap-1.5 text-[9px] text-slate-400">
                <span className="px-1.5 py-0.5 rounded bg-white/60 border border-slate-100/50 font-mono">{meta.wfa.windows.length} 窗</span>
                <span className="px-1.5 py-0.5 rounded bg-white/60 border border-slate-100/50">{meta.wfa.strategy === 'rolling' ? '滚动窗口' : '扩张窗口'}</span>
                <span className="px-1.5 py-0.5 rounded bg-white/60 border border-slate-100/50">模型 {meta.wfa.model_type}</span>
              </div>

              {/* 判断解读 */}
              <WfaInterpretation wfa={meta.wfa} />
            </div>
          )}

          {/* 多周期融合：源模型权重 + 各周期指标 */}
          {meta.is_ensemble && Array.isArray(meta.source_models) && meta.source_models.length > 0 && (
            <div className="glass-panel rounded-3xl p-5 border border-indigo-100/60 bg-gradient-to-br from-indigo-50/30 to-white">
              <div className="flex items-center gap-2 mb-4">
                <Layers2 size={13} className="text-indigo-500" />
                <Text className="text-[10px] font-black text-slate-400 uppercase tracking-widest">多周期融合源模型</Text>
                <Tag className="m-0 rounded-full border-0 px-2 py-0.5 bg-indigo-50 text-indigo-600 text-[9px] font-black">
                  {String(meta.weight_strategy || 'icir').toUpperCase()} 加权
                </Tag>
              </div>

              <div className="space-y-1.5">
                {meta.source_models.map((src: any, i: number) => {
                  const horizon = Number(src.target_horizon_days ?? 0);
                  const weightPct = Math.round(Number(src.weight ?? 0) * 100);
                  return (
                    <div key={src.model_id} className="flex items-center gap-2 px-2 py-1.5 bg-white/60 rounded-lg border border-slate-100/50">
                      <Text className="text-[9px] font-black text-indigo-600 font-mono w-12">
                        {horizon > 0 ? `T+${horizon}` : '—'}
                      </Text>
                      <Text className="text-[9px] font-mono text-slate-500 flex-1 truncate">{src.model_id}</Text>
                      <Text className="text-[9px] font-mono text-slate-400">
                        ICIR {Number(src.metrics?.val_rank_icir ?? src.metrics?.val_icir ?? 0).toFixed(3)}
                      </Text>
                      <div className="w-14 h-1.5 rounded-full bg-slate-100 overflow-hidden flex-shrink-0">
                        <div className="h-full bg-indigo-500 rounded-full" style={{ width: `${weightPct}%` }} />
                      </div>
                      <Text className="text-[9px] font-black text-indigo-600 font-mono w-9 text-right">{weightPct}%</Text>
                    </div>
                  );
                })}
              </div>

              <Text className="block mt-2 text-[10px] text-slate-400 leading-relaxed">
                融合模型推理时对每个周期模型预测做 z-score 归一化后按权重加权平均，跨周期一致确认的信号权重更高。
              </Text>
            </div>
          )}


        </div>
      </div>
    </div>
  );
};


export const TrainingSourcePanel: React.FC<{
  model: UserModelRecord;
  trainingRun?: ModelTrainingRunStatus | null;
  loading?: boolean;
}> = ({ model, trainingRun, loading: externalLoading = false }) => {
  const [runData, setRunData] = useState<ModelTrainingRunStatus | null>(null);
  const [localLoading, setLocalLoading] = useState(false);
  const runId = model.source_run_id || '—';
  const useExternalRun = trainingRun !== undefined;

  useEffect(() => {
    if (useExternalRun) {
      return;
    }
    let isMounted = true;
    if (model.source_run_id && model.source_run_id !== '—') {
      setLocalLoading(true);
      modelTrainingService.getTrainingRun(model.source_run_id)
        .then(data => {
          if (isMounted) {
            setRunData(data);
            setLocalLoading(false);
          }
        })
        .catch(err => {
          console.error("Failed to fetch logs:", err);
          if (isMounted) setLocalLoading(false);
        });
    }
    return () => { isMounted = false; };
  }, [model.source_run_id, useExternalRun]);

  const effectiveRunData = useExternalRun ? trainingRun : runData;
  const isLoading = useExternalRun ? externalLoading : localLoading;
  const logs = effectiveRunData?.logs || 'No logs available for this training run.';
  const status = effectiveRunData?.status || model.status || 'unknown';

  return (
    <div className="h-[calc(var(--app-h)-360px)] flex flex-col space-y-4 pt-6 pb-2 overflow-hidden">
      {/* 顶部任务概览 */}
      <div className="flex gap-4 shrink-0 px-1">
        <div className="glass-panel flex-1 rounded-2xl p-4 border border-slate-100/50 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <div className="bg-slate-500/10 p-2.5 rounded-xl text-slate-400">
              <History size={20} />
            </div>
            <div>
              <Text className="text-[10px] text-slate-400 font-black uppercase tracking-widest block mb-1">训练任务 ID</Text>
              <Text className="text-sm font-mono font-bold text-slate-700">{runId}</Text>
            </div>
          </div>
          <ChevronRight size={16} className="text-slate-300" />
        </div>

        <div className="glass-panel w-56 rounded-2xl p-4 border border-slate-100/50 flex items-center gap-3">
          <div className={clsx(
            "w-10 h-10 rounded-xl flex items-center justify-center",
            status === 'completed' ? "bg-emerald-500/10 text-emerald-500" : "bg-blue-500/10 text-blue-500"
          )}>
            {isLoading ? <Spin size="small" /> : (status === 'completed' ? <CheckCircle2 size={22} /> : <Clock size={22} />)}
          </div>
          <div>
            <Text className="text-[10px] text-slate-400 font-black uppercase tracking-widest block mb-0.5">任务状态</Text>
            <Text className="text-sm font-black text-slate-800 uppercase">{status}</Text>
          </div>
        </div>
      </div>

      {/* 核心日志区 - 终端模拟器风格 */}
      <div className="glass-panel flex-1 rounded-3xl border border-slate-100/50 flex flex-col overflow-hidden mx-1">
        <div className="bg-slate-900/90 px-5 py-3 flex items-center justify-between border-b border-white/5 shrink-0">
          <div className="flex items-center gap-2">
            <div className="flex gap-1.5 mr-2">
              <div className="w-2.5 h-2.5 rounded-full bg-rose-500/80" />
              <div className="w-2.5 h-2.5 rounded-full bg-amber-500/80" />
              <div className="w-2.5 h-2.5 rounded-full bg-emerald-500/80" />
            </div>
            <Code size={14} className="text-slate-400 ml-2" />
            <Text className="text-[10px] font-black text-slate-400 uppercase tracking-widest">Training Execution Logs</Text>
          </div>
          <div className="flex items-center gap-4">
             <Text className="text-[9px] text-slate-500 font-mono">UTF-8 · Python 3.9</Text>
             {status === 'running' && <div className="bg-emerald-500/20 px-2 py-0.5 rounded text-[9px] text-emerald-400 font-bold animate-pulse">LIVE</div>}
          </div>
        </div>
        
        <div className="flex-1 bg-slate-950/95 p-6 overflow-y-auto font-mono custom-scrollbar">
          {isLoading ? (
            <div className="h-full flex items-center justify-center">
              <Spin />
            </div>
          ) : (
            <pre className="text-[11px] leading-relaxed text-slate-300 whitespace-pre-wrap break-all">
              {logs.split('\n').map((line, i) => {
                const isNotice = line.includes('[NOTICE]');
                const isError = line.includes('[ERROR]') || line.includes('Error');
                return (
                  <div key={i} className="mb-0.5 flex gap-4 hover:bg-white/5 transition-colors group">
                    <span className="w-8 shrink-0 text-slate-600 text-right select-none text-[9px]">{i + 1}</span>
                    <span className={clsx(
                      "flex-1",
                      isNotice && "text-emerald-400",
                      isError && "text-rose-400 font-bold",
                      !isNotice && !isError && "text-slate-300"
                    )}>
                      {line}
                    </span>
                  </div>
                );
              })}
              <div className="h-4" />
            </pre>
          )}
        </div>
      </div>
    </div>
  );
};


export const AttributionAnalysisPanel: React.FC<{
  model: UserModelRecord;
  shapSummary: ModelShapSummaryResponse | null;
  loading: boolean;
  error?: string;
  featureLabelMap?: Record<string, string>;
  onRefresh: () => void;
}> = ({ model, shapSummary, loading, error, featureLabelMap = {}, onRefresh }) => {
  const meta = getMeta(model);
  const shapMeta = meta.shap && typeof meta.shap === 'object' ? meta.shap as Record<string, any> : {};
  
  const rows = (shapSummary?.items || shapMeta.items || []) as ModelShapSummaryItem[];
  const status = String(shapSummary?.status || shapMeta.status || 'missing').toLowerCase();
  const split = String(shapSummary?.split || shapMeta.split || '—');
  const rowsUsed = Number(shapSummary?.rows_used ?? shapMeta.rows_used ?? 0);

  const handleExport = () => {
    if (!rows || rows.length === 0) {
      message.warning('暂无数据可导出');
      return;
    }
    try {
      const headers = ['因子名称', '原始代码', '平均绝对贡献(SHAP)', '平均贡献(方向)', '正向比'];
      const csvRows = rows.map(r => [
        `"${featureLabelMap[r.feature] || r.feature}"`,
        `"${r.feature}"`,
        r.mean_abs_shap?.toFixed(8) || '0',
        r.mean_shap?.toFixed(8) || '0',
        ((r.positive_ratio || 0) * 100).toFixed(2) + '%'
      ]);
      const csvContent = [headers.join(','), ...csvRows.map(row => row.join(','))].join('\n');
      const blob = new Blob(['\uFEFF' + csvContent], { type: 'text/csv;charset=utf-8;' });
      const link = document.createElement('a');
      const url = URL.createObjectURL(blob);
      link.setAttribute('href', url);
      link.setAttribute('download', `attribution_${modelDisplayName(model)}_${dayjs().format('YYYYMMDD')}.csv`);
      link.style.visibility = 'hidden';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      message.success('导出成功');
    } catch (err) {
      message.error('导出失败');
    }
  };

  const [searchText, setSearchText] = useState('');
  const [currentPage, setCurrentPage] = useState(1);
  const pageSize = 10;

  const filteredRows = rows.filter(r => 
    r.feature.toLowerCase().includes(searchText.toLowerCase()) || 
    (featureLabelMap[r.feature] && featureLabelMap[r.feature].includes(searchText))
  );

  const maxAbsShap = Math.max(...rows.map(r => r.mean_abs_shap || 0), 0.0001);

  return (
    <div className="h-[calc(var(--app-h)-340px)] flex flex-col space-y-4 overflow-hidden pt-4">
      {/* 头部统计 */}
      <div className="glass-panel rounded-2xl p-5 border border-slate-200 bg-white shadow-sm flex items-center justify-between shrink-0">
        <div className="flex items-center gap-4">
          <div className="flex flex-col">
            <Text className="text-sm font-black text-slate-800 uppercase tracking-tight">归因分析报告</Text>
            <Text className="text-[11px] text-slate-400 font-medium">{split} 数据集 · {rowsUsed} 个训练样本</Text>
          </div>
          <Tag color={status === 'completed' ? 'green' : 'blue'} className="m-0 border-0 text-[9px] font-black uppercase rounded-md h-5 leading-5 px-3">
            {status === 'completed' ? '分析就绪' : status}
          </Tag>
        </div>
        <Space>
          <Button onClick={onRefresh} loading={loading} size="small" className="rounded-full h-8 text-[11px] font-bold border-slate-300 px-6 hover:border-violet-400 hover:text-violet-600 transition-all">刷新数据</Button>
          <Button onClick={handleExport} icon={<Download size={14} />} size="small" className="rounded-full h-8 text-[11px] font-bold border-slate-300 px-6 hover:border-blue-400 hover:text-blue-600 transition-all">数据导出</Button>
        </Space>
      </div>

      {/* 核心内容区 */}
      <div className="glass-panel rounded-2xl p-5 border border-slate-200 bg-white shadow-sm flex flex-col flex-1 overflow-hidden">
        <div className="flex items-center justify-between mb-5 shrink-0">
          <div className="flex items-center gap-2">
            <div className="h-4 w-1 bg-slate-300 rounded-full" />
            <Text className="text-[11px] font-black text-slate-400 uppercase tracking-widest">影响力排行 (SHAP FEATURE IMPORTANCE)</Text>
          </div>
          <Input
            size="small"
            placeholder="按因子名搜索..."
            prefix={<Search size={12} className="text-slate-400" />}
            className="w-56 h-8 rounded-xl border-slate-200 bg-slate-50/80 text-[11px]"
            value={searchText}
            onChange={e => setSearchText(e.target.value)}
          />
        </div>

        <div className="flex-1 overflow-hidden">
          <div className="grid grid-cols-12 gap-6 h-full">
            {/* 左侧：纯数据展示区 */}
            <div className="col-span-8 flex flex-col overflow-hidden h-full">
              <Table
                size="small"
                dataSource={filteredRows.slice((currentPage - 1) * pageSize, currentPage * pageSize)}
                loading={loading}
                tableLayout="fixed"
                pagination={false}
                rowKey="feature"
                className="research-table border border-slate-100 rounded-xl overflow-hidden flex-1"
                columns={[
                  {
                    title: <span className="text-[9px] font-black text-slate-400 uppercase tracking-widest text-center block">因子名称</span>,
                    key: 'feature',
                    width: 140,
                    align: 'center',
                    ellipsis: true,
                    render: (_, r) => (
                      <div className="text-center px-1">
                        <Text className="text-[11px] font-black text-slate-800 block truncate leading-tight mb-0.5">{featureLabelMap[r.feature] || r.feature}</Text>
                        <Text className="text-[8px] text-slate-400 font-mono font-bold block truncate leading-none opacity-80">{r.feature}</Text>
                      </div>
                    ),
                  },
                  {
                    title: <span className="text-[9px] font-black text-slate-400 uppercase tracking-widest block text-center">平均绝对贡献</span>,
                    dataIndex: 'mean_abs_shap',
                    key: 'mean_abs_shap',
                    width: 180,
                    align: 'center',
                    sorter: (a, b) => (a.mean_abs_shap || 0) - (b.mean_abs_shap || 0),
                    render: (v) => {
                      const percent = Math.min((v / maxAbsShap) * 100, 100);
                      return (
                        <div className="flex items-center w-full px-2 gap-3">
                          <BarChart3 size={14} className="text-violet-400 shrink-0" />
                          <div className="flex-1 h-1.5 bg-slate-100 rounded-full overflow-hidden relative">
                            <div className="h-full bg-violet-500 rounded-full" style={{ width: `${percent}%` }} />
                          </div>
                          <Text className="text-[10px] font-mono font-black text-violet-600 shrink-0 w-[60px] text-right">
                            {Number(v || 0).toFixed(6)}
                          </Text>
                        </div>
                      );
                    },
                  },
                  {
                    title: <span className="text-[9px] font-black text-slate-400 uppercase tracking-widest text-center block">方向</span>,
                    dataIndex: 'mean_shap',
                    key: 'mean_shap',
                    width: 90,
                    align: 'center',
                    sorter: (a, b) => (a.mean_shap || 0) - (b.mean_shap || 0),
                    render: (v) => (
                      <div className="flex flex-col items-center leading-none">
                        <Text className={clsx("text-[10px] font-mono font-black px-2 py-0.5 rounded", v >= 0 ? "text-rose-600 bg-rose-50" : "text-emerald-600 bg-emerald-50")}>
                          {v >= 0 ? '+' : ''}{Number(v || 0).toFixed(6)}
                        </Text>
                      </div>
                    ),
                  },
                  {
                    title: <span className="text-[9px] font-black text-slate-400 uppercase tracking-widest text-right block pr-3">正向比</span>,
                    dataIndex: 'positive_ratio',
                    key: 'positive_ratio',
                    width: 80,
                    align: 'right',
                    render: (v) => (
                      <div className="pr-3 text-right">
                        <Text className="text-[11px] font-black text-slate-700">{(Number(v || 0) * 100).toFixed(1)}%</Text>
                      </div>
                    ),
                  },
                ]}
              />
            </div>

            {/* 右侧：说明 + 操作区 */}
            <div className="col-span-4 flex flex-col gap-4">
              <div className="bg-slate-50 rounded-2xl p-5 border border-slate-200 flex flex-col shadow-inner">
                <div className="flex items-center gap-2 mb-4 text-violet-600">
                  <Info size={16} />
                  <Text className="text-xs font-black uppercase tracking-widest">说明</Text>
                </div>

                <div className="space-y-4 mb-4">
                  <div className="relative pl-3 border-l-2 border-violet-400">
                    <Text className="text-[11px] font-black text-slate-700 block mb-1">平均绝对贡献 (影响力)</Text>
                    <Text className="text-[10px] text-slate-500 leading-tight block">
                      代表因子的“话语权”。数值越高，说明该因子在模型判断中说话分量越重。
                    </Text>
                  </div>
                  <div className="relative pl-3 border-l-2 border-rose-400">
                    <Text className="text-[11px] font-black text-slate-700 block mb-1">方向 (因子脾气)</Text>
                    <Text className="text-[10px] text-slate-500 leading-tight block">
                      <span className="text-rose-600 font-bold">正向</span> 表示因子越大模型越看好；<span className="text-emerald-600 font-bold">负向</span> 则相反。
                    </Text>
                  </div>
                  <div className="relative pl-3 border-l-2 border-emerald-400">
                    <Text className="text-[11px] font-black text-slate-700 block mb-1">正向比 (靠谱度)</Text>
                    <Text className="text-[10px] text-slate-500 leading-tight block">
                      反映逻辑一致性。越高说明因子在不同样本下表现越稳健，不容易失效。
                    </Text>
                  </div>
                </div>


                <div className="p-3 bg-white rounded-xl border border-slate-200 shadow-sm">
                  <div className="flex items-start gap-2">
                    <Zap size={12} className="text-amber-500 mt-0.5 shrink-0" />
                    <Text className="text-[10px] text-slate-600 leading-normal italic">
                      选股建议仅供参考，模拟盘请结合市场环境判断。
                    </Text>
                  </div>
                </div>
              </div>

              {/* 翻页操作 */}
              <div className="mt-auto py-3 bg-slate-50/50 rounded-2xl border border-dashed border-slate-200 flex justify-center items-center shadow-inner">
                <Pagination
                  current={currentPage}
                  pageSize={pageSize}
                  total={filteredRows.length}
                  onChange={setCurrentPage}
                  size="small"
                  showSizeChanger={false}
                  className="research-pagination"
                />
              </div>
            </div>
          </div>

        </div>
      </div>
    </div>
  );
};


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
  isDefault?: boolean;
  lastRun: InferenceRunRecord | null;
  history: InferenceRunRecord[];
  historyLoading: boolean;
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
  onDeleteHistory?: (runId: string) => void;
}> = ({
  model, inferenceDate, onDateChange, targetDate, targetDateLoading, horizonDays,
  running, onRun, onRunAsDefault, isDefault, lastRun, history, historyLoading,
  autoSettings, autoSaving, onToggleAuto, latestInferenceRun, latestInferenceRunLoading, precheck, precheckLoading, onRefreshPrecheck,
  historyRunIdFilter, onHistoryRunIdFilterChange, historyStatusFilter, onHistoryStatusFilterChange, historyDateFilter, onHistoryDateFilterChange,
  onDeleteHistory,
}) => {
  const currentModelName = modelDisplayName(model);
  const latestRunModelLabel = latestInferenceRun?.model_id === model.model_id
    ? currentModelName
    : modelIdToDisplayName(latestInferenceRun?.model_id);

  // 本次推理排名：单日推理完成后自动拉取该 run 的排名结果并展示在右侧
  const [rankingResult, setRankingResult] = useState<InferenceRankingResult | null>(null);
  const [rankingLoading, setRankingLoading] = useState(false);

  useEffect(() => {
    const runId = lastRun?.run_id;
    if (!runId || lastRun?.status !== 'completed') {
      setRankingResult(null);
      return;
    }
    let cancelled = false;
    setRankingLoading(true);
    modelTrainingService
      .getInferenceResult(runId)
      .then((r) => {
        if (!cancelled) setRankingResult(r);
      })
      .catch(() => {
        if (!cancelled) setRankingResult(null);
      })
      .finally(() => {
        if (!cancelled) setRankingLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [lastRun?.run_id, lastRun?.status]);

  const topRankings = rankingResult?.rankings?.slice(0, 200) ?? [];

  return (
    <div className="pt-0 pb-10">
      {/* 这里的 items-stretch 是关键，让左右两列等高 */}
      <div className="grid grid-cols-12 gap-5 items-stretch">
        {/* 左侧：任务执行流 */}
        <div className="col-span-8 space-y-4 flex flex-col">
          {/* 前置检查 */}
          <div className="glass-panel rounded-3xl p-5 border border-slate-100/50">
             <div className="flex items-center justify-between mb-5">
                <div className="flex items-center gap-3">
                  <div className="bg-emerald-500/10 p-2 rounded-xl text-emerald-600">
                    <Shield size={18} />
                  </div>
                  <div className="flex flex-col">
                    <Text className="text-sm font-black text-slate-800 uppercase tracking-tight leading-none mb-1">推理前置预检</Text>
                    <Text className="text-xs text-slate-500">行情数据与模型依赖项状态</Text>
                  </div>
                </div>
                <Button 
                  onClick={onRefreshPrecheck} 
                  loading={precheckLoading}
                  className="rounded-full border-slate-200 text-xs font-bold h-8 px-4"
                >
                  刷新检查
                </Button>
             </div>

             <Spin spinning={precheckLoading}>
               {precheck ? (
                 <div className="space-y-4">
                    <div className={clsx(
                      "flex items-center justify-between p-3.5 rounded-2xl border",
                      precheck.passed ? "bg-emerald-50/40 border-emerald-100/60" : "bg-rose-50/40 border-rose-100/60"
                    )}>
                      <div className="flex items-center gap-3">
                        {precheck.passed ? <CheckCircle2 size={20} className="text-emerald-500" /> : <AlertCircle size={20} className="text-rose-500" />}
                        <div>
                          <Text className="text-xs font-black text-slate-800 block leading-tight">
                            {precheck.passed ? "环境就绪" : "预检阻断"}
                          </Text>
                           <Text className="text-xs text-slate-500">
                             数据截止: {precheck.data_trade_date}
                           </Text>
                         </div>
                       </div>
                       <Tag color={precheck.passed ? 'green' : 'red'} className="m-0 px-3 py-0 rounded-full border-0 font-black text-xs">
                        {precheck.passed ? 'PASS' : 'FAIL'}
                      </Tag>
                    </div>

                    <div className="grid grid-cols-2 gap-2.5">
                      {precheck.items.map(item => (
                        <div key={item.key} className="flex items-center justify-between p-2.5 bg-white/40 rounded-xl border border-slate-100/60">
                          <div className="flex items-center gap-2 min-w-0">
                            <div className={clsx("w-1 h-1 rounded-full shrink-0", item.passed ? "bg-emerald-500" : "bg-rose-500")} />
                            <Text className="text-xs font-bold text-slate-700 truncate">{item.label}</Text>
                           </div>
                           {item.passed ? <CheckCircle2 size={14} className="text-emerald-400" /> : <AlertCircle size={14} className="text-rose-400" />}
                        </div>
                      ))}
                    </div>
                 </div>
               ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-xs">暂无预检</span>} />}
             </Spin>
          </div>

          {/* 手动执行面板 */}
          <div className="glass-panel rounded-3xl p-5 border border-slate-100/50 bg-blue-50/5 flex-1 flex flex-col">
            <div className="flex items-center gap-3 mb-5">
              <div className="bg-blue-500/10 p-2 rounded-xl text-blue-600">
                <Play size={18} />
              </div>
              <Text className="text-sm font-black text-slate-800 uppercase tracking-tight leading-none">手动推理执行</Text>
            </div>

            <div className="grid grid-cols-12 gap-5 items-end mb-4">
              <div className="col-span-4">
                <Text className="text-xs font-bold text-slate-500 mb-1.5 block pl-1">行情基准日</Text>
                <DatePicker
                  value={inferenceDate}
                  onChange={onDateChange}
                  disabledDate={d => d.isAfter(dayjs())}
                  className="w-full rounded-xl h-10 border-slate-100 bg-white"
                />
              </div>
              <div className="col-span-4">
                <Text className="text-xs font-bold text-slate-500 mb-1.5 block pl-1">预测目标 T+{horizonDays}</Text>
                <div className="h-10 flex items-center px-4 bg-blue-50/20 rounded-xl border border-blue-100/40">
                  <Calendar size={14} className="text-blue-400 mr-2" />
                  <Text className="font-mono font-black text-sm text-blue-700">{targetDateLoading ? '...' : targetDate || '—'}</Text>
                </div>
              </div>
              <div className="col-span-4 flex gap-2">
                <Button 
                  type="primary" 
                  size="large"
                  onClick={onRun}
                  loading={running}
                  disabled={!precheck?.passed}
                  className="flex-1 rounded-xl h-10 bg-blue-600 border-0 font-bold shadow-md shadow-blue-100 text-xs"
                >
                  立即执行
                </Button>
<Tooltip title={isDefault ? '已是默认模型' : '设为默认模型'}>
                  <Button 
                    size="large"
                    icon={
                      <Star
                        size={15}
                        fill={isDefault ? '#fcd34d' : 'none'}
                        className={isDefault ? 'text-yellow-300' : 'text-slate-300'}
                      />
                    }
                    onClick={isDefault ? undefined : onRunAsDefault}
                    className={clsx(
                      'rounded-xl h-10 w-10 border transition-colors',
                      isDefault
                        ? 'border-yellow-100 bg-yellow-50/60 cursor-default shadow-none'
                        : 'border-slate-200 hover:border-yellow-200 hover:bg-yellow-50/40'
                    )}
                  />
                </Tooltip>
              </div>
            </div>

            <div className="mt-auto flex items-start gap-2.5 p-3.5 bg-blue-50/40 rounded-2xl border border-blue-100/30">
               <Info size={14} className="text-blue-400 mt-0.5 shrink-0" />
                <Text className="text-xs text-blue-700 leading-relaxed">
                 <span className="font-black mr-1">温馨提示：</span>
                 手动运行的结果会记录为”手动任务”。如果你点亮星星设为”默认”，模拟交易将直接使用本次推理的结果。
               </Text>
            </div>
          </div>
        </div>

        {/* 右侧：状态与历史 - 使用 flex-1 拉齐高度 */}
        <div className="col-span-4 space-y-4 flex flex-col h-full">
           <div className="glass-panel rounded-2xl p-4 border border-slate-100/50 bg-gradient-to-br from-white to-emerald-50/10">
              <div className="flex items-center justify-between mb-3">
                  <Text className="text-xs font-bold text-slate-500">当前模拟生效</Text>
                  {(() => {
                     const todayStr = dayjs().format('YYYY-MM-DD');
                     const isEffective = latestInferenceRun?.run_id && 
                                       latestInferenceRun.prediction_trade_date && 
                                       latestInferenceRun.prediction_trade_date >= todayStr;
                     
                     return isEffective ? (
                       <Badge status="processing" text={<span className="text-xs font-bold text-emerald-600 uppercase">Active</span>} />
                     ) : (
                       <Badge status="default" text={<span className="text-xs font-bold text-slate-400 uppercase">Inactive</span>} />
                     );
                 })()}
              </div>
              <Spin spinning={latestInferenceRunLoading}>
                {(() => {
                  const todayStr = dayjs().format('YYYY-MM-DD');
                  const isEffective = latestInferenceRun?.run_id && 
                                    latestInferenceRun.prediction_trade_date && 
                                    latestInferenceRun.prediction_trade_date >= todayStr;

                  return isEffective ? (
                     <div className="bg-white/60 rounded-xl p-3 border border-emerald-100/30">
                         <Text className="text-xs font-mono font-bold text-slate-800 break-all leading-tight block mb-2">
                            {latestInferenceRun.run_id.slice(0, 24)}...
                         </Text>
                         <div className="flex items-center gap-2">
                           <Tag className="m-0 bg-emerald-500 text-white border-0 text-xs font-bold px-1.5">{latestInferenceRun.prediction_trade_date}</Tag>
                           <Text className="text-xs text-slate-500 font-mono italic">{dayjs(latestInferenceRun.updated_at).format('HH:mm')}</Text>
                         </div>
                     </div>
                   ) : (
                     <div className="py-4 flex flex-col items-center justify-center bg-slate-50/50 rounded-xl border border-dashed border-slate-200">
                       <Clock size={16} className="text-slate-300 mb-2" />
                       <Text className="text-xs text-slate-500 font-bold">暂无当前生效推理</Text>
                       <Text className="text-xs text-slate-400 mt-0.5">请手动执行最新行情推理</Text>
                     </div>
                   );
                })()}
              </Spin>
           </div>

            {/* 本次推理排名 - 固定高度 420px + 内部滚动，展示前200 */}
            <div className="glass-panel rounded-2xl p-4 border border-slate-100/50 bg-white flex flex-col overflow-hidden shrink-0" style={{ height: 420 }}>
               <div className="flex items-center justify-between mb-3 shrink-0">
                  <Text className="text-xs font-bold text-slate-500">本次推理排名</Text>
                  {rankingLoading && <Spin size="small" />}
                  {!rankingLoading && rankingResult && (
                    <Tag className="m-0 border-0 text-xs font-bold px-2 rounded-md bg-blue-50 text-blue-600">
                     {rankingResult.target_date} · {rankingResult.rankings.length} 只 · 显示前200
                   </Tag>
                 )}
               </div>
               {rankingLoading ? (
                  <div className="flex-1 flex items-center justify-center py-8">
                    <Text className="text-xs text-slate-500">正在加载排名...</Text>
                  </div>
               ) : topRankings.length > 0 ? (
                 <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar -mx-1 px-1 overscroll-contain">
                  <div className="flex flex-col gap-1">
                    {topRankings.map((r) => (
                      <div
                        key={r.code}
                        className="flex items-center gap-2 px-2 py-1.5 rounded-lg bg-slate-50/70 border border-slate-100/60 hover:bg-blue-50/40 transition-colors"
                      >
                        <span className={clsx(
                          'w-6 h-6 rounded-md flex items-center justify-center text-xs font-bold shrink-0',
                          r.rank <= 3 ? 'bg-rose-500 text-white' : 'bg-slate-200 text-slate-600',
                        )}>
                          {r.rank}
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-1.5">
                            <Text className="text-xs font-bold text-slate-800 font-mono truncate">{r.code}</Text>
                            <Text className="text-xs text-slate-500 truncate">{r.name || ''}</Text>
                          </div>
                          {r.industry && (
                            <Text className="text-xs text-slate-400 truncate block">{r.industry}</Text>
                          )}
                        </div>
                        <div className="flex flex-col items-end shrink-0">
                          <Text className={clsx('text-sm font-mono font-bold', r.score >= 0 ? 'text-rose-600' : 'text-emerald-600')}>
                            {r.score.toFixed(4)}
                          </Text>
                          {r.signal === 'buy' ? (
                            <Text className="text-xs font-bold text-rose-500">↑</Text>
                          ) : r.signal === 'sell' ? (
                            <Text className="text-xs font-bold text-emerald-500">↓</Text>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="flex-1 flex flex-col items-center justify-center py-8 bg-slate-50/50 rounded-xl border border-dashed border-slate-200">
                  <TrendingUp size={16} className="text-slate-300 mb-2" />
                  <Text className="text-xs text-slate-500 font-bold">执行单日推理后显示排名</Text>
                </div>
              )}
           </div>

           <div className="glass-panel rounded-2xl p-4 border border-slate-100/50 flex items-center justify-between">
              <div className="flex items-center gap-3">
                <RefreshCw size={14} className={clsx("text-blue-500", autoSettings?.enabled && "animate-spin-slow")} />
                <div>
                  <Text className="text-xs font-bold text-slate-700 block leading-tight">自动调度</Text>
                  <Text className="text-xs text-slate-400">次日 00:00 起进入任务队列</Text>
                </div>
              </div>
              <Switch size="small" checked={autoSettings?.enabled} loading={autoSaving} onChange={onToggleAuto} className={autoSettings?.enabled ? 'bg-blue-600' : ''} />
           </div>
        </div>
      </div>
    </div>
  );
};


export const MetricCard: React.FC<{ label: string; value: any; digits?: number; color?: string; isLarge?: boolean }> = ({
  label, value, digits = 3, color = 'text-slate-800', isLarge = false,
}) => (
  <div className="bg-white border border-slate-100 rounded-2xl p-4 shadow-sm hover:shadow-md transition-shadow flex min-h-[112px] flex-col items-center justify-center text-center">
    <Text className="text-xs text-slate-500 font-bold uppercase tracking-widest block mb-1 w-full text-center">{label}</Text>
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

// ── 生产监控：滚动 IC + 漂移告警 ──────────────────────────────────────────────
function MiniSparkline({ values, color }: { values: number[]; color: string }) {
  if (values.length < 2) {
    return <Text type="secondary" className="text-xs">暂无数据</Text>;
  }
  const w = 280, h = 56, pad = 4;
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const pts = values.map((v, i) => {
    const x = pad + (i / (values.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - min) / range) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return (
    <svg width={w} height={h} className="overflow-visible">
      <polyline points={pts} fill="none" stroke={color} strokeWidth={1.8} strokeLinejoin="round" />
      <circle cx={pad + (w - pad * 2)} cy={h - pad - ((values[values.length - 1] - min) / range) * (h - pad * 2)} r={2.5} fill={color} />
    </svg>
  );
}

export const ProductionMonitorPanel: React.FC<{ model: UserModelRecord }> = ({ model }) => {
  const [loading, setLoading] = useState(true);
  const [quality, setQuality] = useState<any>(null);
  const [error, setError] = useState<string>('');

  const load = useCallback(() => {
    if (!model?.model_id) return;
    setLoading(true);
    setError('');
    modelTrainingService.getModelQuality(model.model_id, 60)
      .then((d) => setQuality(d))
      .catch((e: any) => setError(e?.message ?? '加载失败'))
      .finally(() => setLoading(false));
  }, [model?.model_id]);

  useEffect(() => { load(); }, [load]);

  const items: any[] = quality?.items ?? [];
  const summary: any = quality?.summary ?? {};
  const rankIcs = items.map((it: any) => it.rank_ic).filter((v: any) => v !== null && v !== undefined) as number[];
  const coverages = items.map((it: any) => it.coverage).filter((v: any) => v !== null && v !== undefined) as number[];
  const dates = items.map((it: any) => it.trade_date);

  const driftColor = summary.drift_status === 'healthy' ? 'emerald'
    : summary.drift_status === 'data_issue' ? 'orange' : 'red';

  return (
    <div className="space-y-4">
      {error && <Alert type="warning" showIcon message="生产数据加载失败" description={error} className="rounded-xl" />}
      {!error && loading && <div className="py-8 text-center"><Spin /></div>}
      {!error && !loading && items.length === 0 && (
        <Empty description="暂无生产数据。每日自动推理后，次日回填真实 IC（滞后 5 个交易日）" />
      )}
      {!error && !loading && items.length > 0 && (
        <>
          {/* 漂移告警横幅 */}
          {summary.drift_status !== 'healthy' && (
            <Alert
              type="error"
              showIcon
              message={`漂移告警：${summary.drift_status === 'degraded' ? '信号可能失效' : summary.drift_status === 'drifted' ? '信号衰减' : '数据问题'}`}
              description={summary.drift_reasons?.join('；') ?? ''}
              className="rounded-xl border-red-200"
            />
          )}
          {summary.drift_status === 'healthy' && (
            <div className="rounded-xl bg-emerald-50 border border-emerald-200 px-4 py-2.5 text-xs font-bold text-emerald-700">
              ✓ 模型运行健康：近{Math.min(rankIcs.length, 20)}日 Rank IC 正常
            </div>
          )}

          {/* 指标卡 */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <MetricCard label="生产天数" value={`${summary.days ?? 0} 天`} />
            <MetricCard label="Rank IC 均值" value={summary.rank_ic_mean !== null && summary.rank_ic_mean !== undefined ? summary.rank_ic_mean.toFixed(4) : '—'} color={summary.rank_ic_mean != null && summary.rank_ic_mean > 0 ? 'text-emerald-600' : 'text-red-500'} />
            <MetricCard label="30日 ICIR" value={summary.rank_icir_30d != null ? summary.rank_icir_30d.toFixed(3) : '—'} />
            <MetricCard label="覆盖率" value={summary.coverage_mean != null ? `${(summary.coverage_mean * 100).toFixed(0)}%` : '—'} />
          </div>

          {/* 滚动 IC 曲线 */}
          <div className="rounded-2xl border border-slate-200 bg-white p-4">
            <div className="flex items-center justify-between mb-3">
              <Text className="text-[10px] font-black uppercase tracking-[0.22em] text-slate-400">滚动 Rank IC 曲线</Text>
              <Text type="secondary" className="text-[10px] font-mono">{dates[0]} ~ {dates[dates.length - 1]}</Text>
            </div>
            <MiniSparkline values={rankIcs} color={summary.rank_ic_mean != null && summary.rank_ic_mean >= 0 ? '#10b981' : '#ef4444'} />
            <div className="mt-3 flex gap-4 text-[10px] text-slate-400">
              <span>数据截至 {dates[dates.length - 1]}（真实 IC 滞后 5 交易日）</span>
              {coverages.length > 0 && <span>覆盖率 {Math.min(...coverages).toFixed(2)}~{Math.max(...coverages).toFixed(2)}</span>}
            </div>
          </div>
        </>
      )}
    </div>
  );
};
