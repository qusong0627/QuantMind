import React, { useMemo, useState, useEffect } from 'react';
import { Button, Card, Tag, Typography, Empty, Spin, Progress, Divider, Input, Modal, Tabs, Switch, DatePicker, Table, Drawer, Badge, Tooltip, Collapse, Select, Pagination, message } from 'antd';
import { clsx } from 'clsx';
import dayjs from 'dayjs';
import {
  Layers, Star, RefreshCw, Search, Code, Calendar, Layers2,
  History, Archive, Brain, CheckCircle2, Clock, XCircle,
  ChevronRight, Play, Cpu, Trash2, TrendingUp, Download, ChevronDown,
  ChevronUp, Shield, Zap, Activity, ListFilter, BarChart3, Info, AlertCircle,
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
                      <span className="font-black">{item.trendLabel.replace(/[+-]/, '').replace('%', '')}{item.trendLabel === '基线' ? '' : '%'}</span>
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
              训练集反映拟合能力，验证集用于参数选择，测试集代表实盘泛化。IC 衰减控制在 10% 以内视为模型鲁棒性良好。
            </Text>
          </div>

          {features.length > 0 && (
            <div className="glass-panel rounded-3xl p-6 border border-slate-100/50">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2">
                  <div className="h-4 w-1 bg-violet-500 rounded-full" />
                  <Text className="text-xs font-black text-slate-800 uppercase">特征工程资产 ({features.length})</Text>
                </div>
                <Input size="small" placeholder="过滤特征..." prefix={<Search size={10} />} className="w-32 rounded-lg text-[10px] border-slate-200" />
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
                <Text className="text-sm font-black text-slate-800">T + {horizonDays || '—'}</Text>
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
        </div>
      </div>
    </div>
  );
};


export const TrainingSourcePanel: React.FC<{
  model: UserModelRecord;
}> = ({ model }) => {
  const [runData, setRunData] = useState<ModelTrainingRunStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const runId = model.source_run_id || '—';

  useEffect(() => {
    let isMounted = true;
    if (model.source_run_id && model.source_run_id !== '—') {
      setLoading(true);
      modelTrainingService.getTrainingRun(model.source_run_id)
        .then(data => {
          if (isMounted) {
            setRunData(data);
            setLoading(false);
          }
        })
        .catch(err => {
          console.error("Failed to fetch logs:", err);
          if (isMounted) setLoading(false);
        });
    }
    return () => { isMounted = false; };
  }, [model.source_run_id]);

  const logs = runData?.logs || 'No logs available for this training run.';
  const status = runData?.status || model.status || 'unknown';

  return (
    <div className="h-[calc(100vh-360px)] flex flex-col space-y-4 pt-6 pb-2 overflow-hidden">
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
            {loading ? <Spin size="small" /> : (status === 'completed' ? <CheckCircle2 size={22} /> : <Clock size={22} />)}
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
          {loading ? (
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

  const [searchText, setSearchText] = useState('');
  const [currentPage, setCurrentPage] = useState(1);
  const pageSize = 10;

  const filteredRows = rows.filter(r => 
    r.feature.toLowerCase().includes(searchText.toLowerCase()) || 
    (featureLabelMap[r.feature] && featureLabelMap[r.feature].includes(searchText))
  );

  const maxAbsShap = Math.max(...rows.map(r => r.mean_abs_shap || 0), 0.0001);

  return (
    <div className="h-[calc(100vh-340px)] flex flex-col space-y-4 overflow-hidden pt-4">
      {/* 头部统计 */}
      <div className="glass-panel rounded-2xl p-5 border border-slate-200 bg-white shadow-sm flex items-center justify-between shrink-0">
        <div className="flex items-center gap-4">
          <div className="bg-violet-500/10 p-2.5 rounded-xl text-violet-600">
            <Brain size={20} />
          </div>
          <div className="flex flex-col">
            <Text className="text-sm font-black text-slate-800 uppercase tracking-tight">归因分析报告</Text>
            <Text className="text-[11px] text-slate-400 font-medium">{split} 数据集 · {rowsUsed} 个训练样本</Text>
          </div>
          <Tag color={status === 'completed' ? 'green' : 'blue'} className="m-0 border-0 text-[9px] font-black uppercase rounded-md h-5 leading-5 px-3">
            {status === 'completed' ? '分析就绪' : status}
          </Tag>
        </div>
        <Button onClick={onRefresh} loading={loading} size="small" className="rounded-full h-8 text-[11px] font-bold border-slate-300 px-6 hover:border-violet-400 hover:text-violet-600 transition-all">刷新数据</Button>
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
                      选股建议仅供参考，实盘请结合市场环境判断。
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
  lastRun: InferenceRunRecord | null;
  history: InferenceRunRecord[];
  historyLoading: boolean;
  onViewRanking: (runId: string) => void;
  onDeleteRun?: (runId: string) => void;
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
  running, onRun, onRunAsDefault, lastRun, history, historyLoading, onViewRanking, onDeleteRun,
  autoSettings, autoSaving, onToggleAuto, latestInferenceRun, latestInferenceRunLoading, precheck, precheckLoading, onRefreshPrecheck,
  historyRunIdFilter, onHistoryRunIdFilterChange, historyStatusFilter, onHistoryStatusFilterChange, historyDateFilter, onHistoryDateFilterChange,
}) => {
  const currentModelName = modelDisplayName(model);
  const latestRunModelLabel = latestInferenceRun?.model_id === model.model_id
    ? currentModelName
    : modelIdToDisplayName(latestInferenceRun?.model_id);

  const [historyPage, setHistoryPage] = useState(1);
  const pageSize = 5;
  const paginatedHistory = history.slice((historyPage - 1) * pageSize, historyPage * pageSize);

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
                    <Text className="text-[10px] text-slate-400">行情数据与模型依赖项状态</Text>
                  </div>
                </div>
                <Button 
                  onClick={onRefreshPrecheck} 
                  loading={precheckLoading}
                  className="rounded-full border-slate-200 text-[10px] font-bold h-8 px-4"
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
                          <Text className="text-[10px] text-slate-500">
                             数据截止: {precheck.prediction_trade_date} · {dayjs(precheck.checked_at).format('HH:mm')}
                          </Text>
                        </div>
                      </div>
                      <Tag color={precheck.passed ? 'green' : 'red'} className="m-0 px-3 py-0 rounded-full border-0 font-black text-[10px]">
                        {precheck.passed ? 'PASS' : 'FAIL'}
                      </Tag>
                    </div>

                    <div className="grid grid-cols-2 gap-2.5">
                      {precheck.items.map(item => (
                        <div key={item.key} className="flex items-center justify-between p-2.5 bg-white/40 rounded-xl border border-slate-100/60">
                          <div className="flex items-center gap-2 min-w-0">
                            <div className={clsx("w-1 h-1 rounded-full shrink-0", item.passed ? "bg-emerald-500" : "bg-rose-500")} />
                            <Text className="text-[11px] font-bold text-slate-700 truncate">{item.label}</Text>
                          </div>
                          {item.passed ? <CheckCircle2 size={12} className="text-emerald-400" /> : <AlertCircle size={12} className="text-rose-400" />}
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
                <Text className="text-[10px] font-black text-slate-400 uppercase mb-1.5 block tracking-widest pl-1">行情基准日</Text>
                <DatePicker
                  value={inferenceDate}
                  onChange={onDateChange}
                  disabledDate={d => d.isAfter(dayjs())}
                  className="w-full rounded-xl h-10 border-slate-100 bg-white"
                />
              </div>
              <div className="col-span-4">
                <Text className="text-[10px] font-black text-slate-400 uppercase mb-1.5 block tracking-widest pl-1">预测目标 T+{horizonDays}</Text>
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
                <Button 
                  size="large"
                  icon={<Star size={16} />}
                  onClick={onRunAsDefault}
                  className="rounded-xl h-10 w-10 border-slate-200 text-slate-400"
                />
              </div>
            </div>

            <div className="mt-auto flex items-start gap-2.5 p-3.5 bg-blue-50/40 rounded-2xl border border-blue-100/30">
               <Info size={14} className="text-blue-400 mt-0.5 shrink-0" />
               <Text className="text-[10px] text-blue-600/80 leading-relaxed">
                 <span className="font-black mr-1">温馨提示：</span>
                 手动运行的结果会记录为“手动任务”。如果你点亮星星设为“默认”，实盘交易将直接使用本次推理的结果。
               </Text>
            </div>
          </div>
        </div>

        {/* 右侧：状态与历史 - 使用 flex-1 拉齐高度 */}
        <div className="col-span-4 space-y-4 flex flex-col h-full">
           <div className="glass-panel rounded-2xl p-4 border border-slate-100/50 bg-gradient-to-br from-white to-emerald-50/10">
              <div className="flex items-center justify-between mb-3">
                 <Text className="text-[9px] font-black text-slate-400 uppercase tracking-widest">当前实盘生效</Text>
                 {(() => {
                    const todayStr = dayjs().format('YYYY-MM-DD');
                    const isEffective = latestInferenceRun?.run_id && 
                                      latestInferenceRun.prediction_trade_date && 
                                      latestInferenceRun.prediction_trade_date >= todayStr;
                    
                    return isEffective ? (
                      <Badge status="processing" text={<span className="text-[9px] font-black text-emerald-500 uppercase">Active</span>} />
                    ) : (
                      <Badge status="default" text={<span className="text-[9px] font-black text-slate-400 uppercase">Inactive</span>} />
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
                        <Text className="text-[10px] font-mono font-black text-slate-800 break-all leading-tight block mb-2">
                           {latestInferenceRun.run_id.slice(0, 24)}...
                        </Text>
                        <div className="flex items-center gap-2">
                          <Tag className="m-0 bg-emerald-500 text-white border-0 text-[8px] font-black px-1.5">{latestInferenceRun.prediction_trade_date}</Tag>
                          <Text className="text-[8px] text-slate-400 font-mono italic">{dayjs(latestInferenceRun.updated_at).format('HH:mm')}</Text>
                        </div>
                    </div>
                  ) : (
                    <div className="py-4 flex flex-col items-center justify-center bg-slate-50/50 rounded-xl border border-dashed border-slate-200">
                      <Clock size={16} className="text-slate-300 mb-2" />
                      <Text className="text-[9px] text-slate-400 font-bold">暂无当前生效推理</Text>
                      <Text className="text-[8px] text-slate-300 mt-0.5">请手动执行最新行情推理</Text>
                    </div>
                  );
                })()}
              </Spin>
           </div>

           <div className="glass-panel rounded-2xl p-4 border border-slate-100/50 flex items-center justify-between">
              <div className="flex items-center gap-3">
                <RefreshCw size={14} className={clsx("text-blue-500", autoSettings?.enabled && "animate-spin-slow")} />
                <div>
                  <Text className="text-[11px] font-bold text-slate-700 block leading-tight">自动调度</Text>
                  <Text className="text-[9px] text-slate-400">22:00 自动执行</Text>
                </div>
              </div>
              <Switch size="small" checked={autoSettings?.enabled} loading={autoSaving} onChange={onToggleAuto} className={autoSettings?.enabled ? 'bg-blue-600' : ''} />
           </div>

           {/* 历史记录卡片：通过 flex-1 撑满高度，与左侧对齐 */}
           <div className="glass-panel rounded-2xl p-4 border border-slate-100/50 flex flex-col flex-1">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2 text-slate-400">
                  <History size={14} />
                  <Text className="text-[10px] font-black uppercase tracking-widest">推理历史</Text>
                </div>
                <Select
                  size="small"
                  value={historyStatusFilter}
                  onChange={onHistoryStatusFilterChange}
                  className="w-24 h-7 text-[10px]"
                  classNames={{ popup: { root: "rounded-xl text-[11px]" } }}
                >
                  <Select.Option value="all">全部批次</Select.Option>
                  <Select.Option value="completed">推理成功</Select.Option>
                  <Select.Option value="failed">执行失败</Select.Option>
                </Select>
              </div>

              <div className="space-y-1.5 flex-1 pr-0.5">
                {historyLoading ? <div className="text-center py-6"><Spin size="small" /></div> : 
                 history.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-[9px]">暂无记录</span>} /> :
                 paginatedHistory.map(run => (
                  <div key={run.run_id} className="group flex items-center justify-between p-2.5 rounded-xl bg-slate-50/40 border border-slate-100/30 hover:bg-white hover:border-blue-100 transition-all cursor-pointer" onClick={() => onViewRanking(run.run_id)}>
                    <div className="flex flex-col min-w-0 flex-1">
                      <div className="flex items-center gap-2 mb-0.5">
                        <div className={clsx("w-1.5 h-1.5 rounded-full shrink-0", run.status === 'completed' ? 'bg-emerald-500' : run.status === 'running' ? 'bg-blue-500' : 'bg-rose-500')} />
                        <Text className="text-[10px] font-mono font-bold text-slate-700 truncate w-24">{run.run_id}</Text>
                      </div>
                      <Text className="text-[9px] text-slate-400 pl-3.5">{run.prediction_trade_date}</Text>
                    </div>
                    <div className="flex items-center gap-1">
                      <Button
                        size="small"
                        type="text"
                        className="border-0 h-6 w-6 p-0 flex items-center justify-center text-slate-300 hover:!text-red-500"
                        onClick={(e) => { e.stopPropagation(); onDeleteRun?.(run.run_id); }}
                      >
                        <Trash2 size={13} />
                      </Button>
                      <ChevronRight size={12} className="text-slate-200 group-hover:text-blue-400" />
                    </div>
                  </div>
                ))}
              </div>

              {/* 分页器：固定在卡片底部 */}
              {history.length > pageSize && (
                <div className="mt-auto pt-3 border-t border-slate-50 flex justify-center">
                  <div className="flex items-center gap-3">
                    <Button 
                      size="small" 
                      disabled={historyPage === 1} 
                      onClick={(e) => { e.stopPropagation(); setHistoryPage(historyPage - 1); }}
                      className="border-0 bg-slate-100/50 hover:bg-slate-100 rounded-lg h-6 w-6 p-0 flex items-center justify-center text-slate-500"
                    >
                      <ChevronDown size={14} className="rotate-90" />
                    </Button>
                    <Text className="text-[10px] font-bold text-slate-400">{historyPage} / {Math.ceil(history.length / pageSize)}</Text>
                    <Button 
                      size="small" 
                      disabled={historyPage >= Math.ceil(history.length / pageSize)} 
                      onClick={(e) => { e.stopPropagation(); setHistoryPage(historyPage + 1); }}
                      className="border-0 bg-slate-100/50 hover:bg-slate-100 rounded-lg h-6 w-6 p-0 flex items-center justify-center text-slate-500"
                    >
                      <ChevronDown size={14} className="-rotate-90" />
                    </Button>
                  </div>
                </div>
              )}
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
