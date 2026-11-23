import React, { useMemo, useState, useEffect, useCallback } from 'react';
import { Button, Card, Tag, Typography, Empty, Spin, Progress, Divider, Modal, Tabs, Table, Collapse, Select, message, Space, Alert, Checkbox, Tooltip, Input } from 'antd';
import { clsx } from 'clsx';
import {
  BarChart as ReBarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip as ReTooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts';
import type { EvalReport } from './training/trainingUtils';
import { EvalScoreBadge } from '../components/shared/EvalScoreBadge';
import { listScores } from '../features/eval-center/services/evalCenterService';
import dayjs from 'dayjs';
import {
  Layers, Star, RefreshCw,
  Archive, Brain, XCircle, Trash2,
  Play, Cpu,
  Shield, AlertCircle,
  Pin, Bookmark, Folder, Search,
} from 'lucide-react';
import {
  UserModelRecord,
  ModelTrainingRunStatus,
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
  modelFeatureList,
  resolveMetricNumber,
  resolveSplitMetrics,
} from './modelRegistryUtils';
import { ensembleMemberIds, isEnsembleModel } from './fusionUtils';
import { TrainingRunDetailTabs } from './training/TrainingRunDetailTabs';
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
    <div className="mx-4 mb-4 mt-1 rounded-xl border border-slate-100 bg-slate-50/60 p-3">
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
            : '多个维度未达标，建议调整特征/参数后重新训练。'}
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
  /** 置顶 / 收藏（本地偏好，见 modelRegistryListPrefs）。 */
  isPinned?: boolean;
  isFavorite?: boolean;
  onTogglePin?: () => void;
  onToggleFavorite?: () => void;
  /** 所属文件夹名（仅「全部模型」视图展示，避免在文件夹视图里重复）。 */
  folderName?: string | null;
  /** 「移动到文件夹」菜单（页面注入，卡片保持哑组件，不感知文件夹数据）。 */
  folderMenu?: React.ReactNode;
  /** 融合模式：卡片显示复选框，点击行为交回页面（选中/取消）。 */
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
  /** 融合模式下该模型不可作成员（系统模型/未就绪）：复选框置灰，点击给警告。 */
  selectDisabled?: boolean;
  /** 紧凑视图：一行一个模型，便于整理/筛选。 */
  compact?: boolean;
}> = ({
  model, isSelected, onClick, onSetDefault, canSetDefault,
  isPinned = false, isFavorite = false, onTogglePin, onToggleFavorite,
  folderName = null, folderMenu,
  selectable = false, selected = false, onToggleSelect, selectDisabled = false,
  compact = false,
}) => {
  const sc = getStatusConfig(model.status);
  const mt = extractModelType(model);
  const fc = getMeta(model).feature_count ?? null;
  const ensemble = isEnsembleModel(model);
  const memberCount = ensemble ? ensembleMemberIds(model).length : 0;
  const horizonRaw = Number(getMeta(model).target_horizon_days ?? getMeta(model).horizon_days);
  const horizon = Number.isFinite(horizonRaw) && horizonRaw > 0 ? horizonRaw : null;
  const createdLabel = model.created_at && dayjs(model.created_at).isValid()
    ? dayjs(model.created_at).format('YY/MM/DD')
    : null;
  const marketRaw = getMeta(model).market
    ? String(getMeta(model).market).toUpperCase()
    : null;
  const marketLabel = marketRaw
    ? MARKET_LABELS[marketRaw] || String(getMeta(model).market)
    : null;
  const marketTagClass =
    marketRaw === 'CN'
      ? 'bg-rose-50 text-rose-500'
      : marketRaw === 'HK'
        ? 'bg-orange-50 text-orange-500'
        : marketRaw === 'US'
          ? 'bg-blue-50 text-blue-500'
          : marketRaw === 'FUTURES'
            ? 'bg-amber-50 text-amber-600'
            : marketRaw === 'CRYPTO'
              ? 'bg-purple-50 text-purple-500'
              : 'bg-slate-100 text-slate-500';
  // 最近推理：没有成功记录 = 未推理（红）；最近一次运行失败 = 日期标黄提醒。
  const lastInf = model.last_inference ?? null;
  const lastInfDate = lastInf?.last_inference_date || null;
  const lastInfFailed = Boolean(lastInf?.last_status && lastInf.last_status !== 'completed');
  const lastInfTitle = lastInfDate
    ? [
        `最近推理基准日：${lastInfDate}`,
        lastInf?.last_prediction_date ? `预测日：${lastInf.last_prediction_date}` : null,
        lastInf?.last_success_at ? `落库：${formatPanelDateTime(lastInf.last_success_at)}` : null,
        lastInfFailed ? `注意：最近一次运行状态为 ${lastInf?.last_status}` : null,
      ].filter(Boolean).join(' · ')
    : lastInf?.last_status
      ? `从未成功推理（最近一次运行：${lastInf.last_status}）`
      : '从未推理';

  // 测试集指标（与详情面板同一口径）：IC / IR 缺失显示「—」，不伪造 0。
  const testSplit = resolveSplitMetrics(getMeta(model)).test;
  const testIcLabel = testSplit.ic === null ? '—' : testSplit.ic.toFixed(4);
  const testIcirLabel = testSplit.icir === null ? '—' : testSplit.icir.toFixed(3);
  // 标题写的是 Rank IC，有单独的 test_rank_ic 就用它；老模型只有 IC 时回落（不伪造）。
  const testRankIc = testSplit.rankIc ?? testSplit.ic;
  const testRankIcLabel = testRankIc === null ? '—' : testRankIc.toFixed(4);
  const testMetricsTitle = `测试集：Rank IC ${testRankIcLabel} · IR ${testIcirLabel}`;
  const metricChipTone = (value: number | null) =>
    value === null
      ? 'bg-slate-50 text-slate-300'
      : value > 0
        ? 'bg-emerald-50 text-emerald-600'
        : value < 0
          ? 'bg-rose-50 text-rose-500'
          : 'bg-slate-100 text-slate-500';

  const inferenceBadge = (
    <span
      title={lastInfTitle}
      className={clsx(
        'px-1 py-0.5 rounded-md text-[8px] font-black flex items-center whitespace-nowrap shrink-0',
        !lastInfDate
          ? 'bg-rose-50 text-rose-500'
          : lastInfFailed
            ? 'bg-amber-50 text-amber-600'
            : 'bg-emerald-50 text-emerald-600',
      )}
    >
      {lastInfDate ? `推理 ${dayjs(lastInfDate).format('MM-DD')}` : '未推理'}
    </span>
  );

  const actionsNode = (
    <div className="flex items-center gap-0.5 shrink-0">
      {folderMenu}
      {onTogglePin && (
        <Tooltip title={isPinned ? '取消置顶' : '置顶'}>
          <button
            type="button"
            aria-label={isPinned ? '取消置顶' : '置顶'}
            onClick={(e) => { e.stopPropagation(); onTogglePin(); }}
            className={clsx(
              'w-5 h-5 flex items-center justify-center rounded-md transition-colors',
              isPinned
                ? 'text-blue-600 bg-blue-50'
                : 'text-slate-300 hover:text-slate-500 hover:bg-slate-100',
            )}
          >
            <Pin size={10} fill={isPinned ? 'currentColor' : 'none'} />
          </button>
        </Tooltip>
      )}
      {onToggleFavorite && (
        <Tooltip title={isFavorite ? '取消收藏' : '收藏'}>
          <button
            type="button"
            aria-label={isFavorite ? '取消收藏' : '收藏'}
            onClick={(e) => { e.stopPropagation(); onToggleFavorite(); }}
            className={clsx(
              'w-5 h-5 flex items-center justify-center rounded-md transition-colors',
              isFavorite
                ? 'text-amber-500 bg-amber-50'
                : 'text-slate-300 hover:text-amber-500 hover:bg-amber-50',
            )}
          >
            <Bookmark size={10} fill={isFavorite ? 'currentColor' : 'none'} />
          </button>
        </Tooltip>
      )}
      {canSetDefault && (
        <Tooltip title="设为默认模型">
          <button
            type="button"
            aria-label="设为默认模型"
            onClick={(e) => { e.stopPropagation(); onSetDefault(); }}
            className="w-5 h-5 flex items-center justify-center rounded-md text-slate-300 hover:text-amber-500 hover:bg-amber-50 transition-colors"
          >
            <Star size={10} />
          </button>
        </Tooltip>
      )}
    </div>
  );

  if (compact) {
    return (
      <div
        onClick={onClick}
        className={clsx(
          'group grid grid-cols-[72px_1fr_auto] items-center gap-2 px-2.5 py-1.5 rounded-xl cursor-pointer transition-colors border select-none min-w-0',
          (selectable ? selected : isSelected)
            ? 'bg-white border-blue-400 ring-1 ring-blue-300 shadow-sm shadow-blue-100'
            : 'bg-white/60 border-slate-200/60 hover:bg-white hover:border-blue-200',
        )}
      >
        <div className="flex items-center gap-1 min-w-0">
          {selectable && (
            <Checkbox
              checked={selected}
              disabled={selectDisabled}
              onClick={(e) => e.stopPropagation()}
              onChange={() => (onToggleSelect ?? onClick)()}
            />
          )}
          <span className={clsx('shrink-0 px-1.5 py-0.5 rounded-md text-[8px] font-black tracking-wider flex items-center gap-0.5', sc.bg, sc.color)}>
            {sc.icon}{sc.label}
          </span>
        </div>
        <Text
          title={modelDisplayName(model)}
          className={clsx('font-bold text-[11px] truncate min-w-0', isSelected ? 'text-blue-700' : 'text-slate-800')}
        >
          {modelDisplayName(model)}
        </Text>
        <div className="flex items-center gap-1.5 shrink-0">
          {inferenceBadge}
          <EvalScoreBadge objectType="model" objectId={String(model.model_id).replace(/^sys-/, '')} />
          {actionsNode}
        </div>
      </div>
    );
  }

  return (
    <div
      onClick={onClick}
      className={clsx(
        'group rounded-2xl cursor-pointer transition-all duration-200 border select-none overflow-hidden',
        (selectable ? selected : isSelected)
          ? 'bg-white border-blue-500 shadow-lg shadow-blue-100 ring-1 ring-blue-400'
          : 'bg-white/90 border-slate-200/80 hover:border-blue-200 hover:shadow-sm',
      )}
    >
      {/* 三栏网格：左栏≈4 字宽（状态/推理/测试集指标/日期）、中栏（标签/名称/文件夹）、右栏（操作/评分）
          中栏保底 140px，标签行可换行——拖窄列表时标签不会被裁掉，名称不会挤塌 */}
      <div className="grid grid-cols-[68px_minmax(140px,1fr)_auto] grid-rows-[auto_56px_30px]">
        {/* 上：状态 + 默认标记 | 标签（可换行，全部可见） | 操作 */}
        <div className="flex items-center gap-1 px-1.5 py-1.5 min-w-0">
          {selectable && (
            <Checkbox
              checked={selected}
              disabled={selectDisabled}
              onClick={(e) => e.stopPropagation()}
              onChange={() => (onToggleSelect ?? onClick)()}
            />
          )}
          <span
            className={clsx(
              'shrink-0 py-0.5 rounded-md text-[8px] font-black tracking-wider flex items-center gap-0.5',
              selectable ? 'px-1' : 'px-1.5',
              sc.bg, sc.color,
            )}
          >
            {!selectable && sc.icon}{sc.label}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-1 px-2 py-1.5 min-w-0 border-l border-slate-100">
          {model.is_default && (
            <Star size={9} fill="#fbbf24" className="shrink-0 text-amber-400" aria-label="默认模型" />
          )}
          <span className="shrink-0 px-1.5 py-0.5 rounded-md text-[8px] font-black bg-indigo-50 text-indigo-600">{mt}</span>
          {ensemble && (
            <span className="shrink-0 px-1.5 py-0.5 rounded-md text-[8px] font-black bg-purple-50 text-purple-600">融合×{memberCount}</span>
          )}
          {marketLabel && (
            <span className={clsx('shrink-0 px-1.5 py-0.5 rounded-md text-[8px] font-black', marketTagClass)}>{marketLabel}</span>
          )}
          {fc && (
            <span className="shrink-0 px-1.5 py-0.5 rounded-md text-[8px] font-black bg-slate-100 text-slate-500">{fc}维</span>
          )}
          {horizon && (
            <span className="shrink-0 px-1.5 py-0.5 rounded-md text-[8px] font-black bg-blue-50 text-blue-500">T+{horizon}</span>
          )}
        </div>
        <div className="flex items-center justify-end px-2 border-l border-slate-100">
          {actionsNode}
        </div>

        {/* 中：推理 | 名称跨中右两栏（最多 2 行，名称可显示全） */}
        <div className="flex items-center px-1.5 border-t border-slate-100 min-w-0">
          {inferenceBadge}
        </div>
        <div className="col-span-2 flex items-center min-w-0 px-2 border-t border-l border-slate-100">
          <Text
            title={modelDisplayName(model)}
            className={clsx('font-black text-[12px] tracking-tight leading-4 line-clamp-2', isSelected ? 'text-blue-700' : 'text-slate-800')}
          >
            {modelDisplayName(model)}
          </Text>
        </div>

        {/* 下：创建日期 | 文件夹 + 测试集 IC/IR（彩色标签、右对齐） | 评分（居中） */}
        <div className="flex items-center px-1.5 border-t border-slate-100 min-w-0">
          {createdLabel && (
            <span className="text-[9px] text-slate-500 font-mono font-bold">{createdLabel}</span>
          )}
        </div>
        <div className="flex items-center min-w-0 px-2 border-t border-l border-slate-100">
          {folderName ? (
            <span
              title={`文件夹：${folderName}`}
              className="inline-flex items-center gap-1 min-w-0 max-w-full px-1.5 py-0.5 rounded-md bg-cyan-50 text-cyan-600 text-[8px] font-black leading-none"
            >
              <Folder size={8} className="shrink-0" />
              <span className="truncate [text-box:trim-both_cap_alphabetic]">{folderName}</span>
            </span>
          ) : (
            <span
              aria-label="未分组"
              title="未分组（未归入任何文件夹）"
              className="inline-block h-2.5 w-2.5 rounded-[3px] border border-slate-300 bg-slate-50 shrink-0"
            />
          )}
          <div title={testMetricsTitle} className="ml-auto pl-1 flex items-center gap-1 shrink-0">
            <span className={clsx('px-1 py-0.5 rounded text-[8px] font-black font-mono leading-none whitespace-nowrap', metricChipTone(testSplit.ic))}>
              IC {testIcLabel}
            </span>
            <span className={clsx('px-1 py-0.5 rounded text-[8px] font-black font-mono leading-none whitespace-nowrap', metricChipTone(testSplit.icir))}>
              IR {testIcirLabel}
            </span>
          </div>
        </div>
        <div className="flex items-center justify-center px-2 border-t border-l border-slate-100">
          {/* 评分居中；eval_scores 以模型目录名（无 sys- 前缀）落档，注册表 UI id 带 sys- 前缀 → 归一 */}
          <EvalScoreBadge objectType="model" objectId={String(model.model_id).replace(/^sys-/, '')} />
        </div>
      </div>
    </div>
  );
};

// ─── 融合模型详情（成员 / 权重 / 融合配置） ─────────────────────────────────
const WEIGHT_STRATEGY_LABELS: Record<string, string> = {
  icir_shrunk: '机构级 ICIR 收缩',
  equal: '等权',
  manual: '手工权重',
  recent_ic: '近期 IC（旧口径）',
};

export const EnsembleDetailPanel: React.FC<{ model: UserModelRecord }> = ({ model }) => {
  const meta = getMeta(model);
  const sourceModels = Array.isArray(meta.source_models) ? (meta.source_models as Array<Record<string, any>>) : [];
  const weights: Record<string, number> =
    meta.weights && typeof meta.weights === 'object' ? (meta.weights as Record<string, number>) : {};
  const strategy = String(meta.weight_strategy || '');
  const memberCount = sourceModels.length || ensembleMemberIds(model).length;

  const memberWeight = (m: Record<string, any>) => {
    const w = m.weight ?? weights[String(m.model_id)];
    const num = Number(w);
    return Number.isFinite(num) ? num : null;
  };

  return (
    <div className="pt-2 space-y-5">
      <div className="grid grid-cols-3 gap-4">
        {[
          { label: '权重策略', value: WEIGHT_STRATEGY_LABELS[strategy] || strategy || '—' },
          { label: '融合方式', value: String(meta.fusion_strategy || 'linear') === 'linear' ? '线性加权（rank 百分位）' : String(meta.fusion_strategy) },
          { label: '成员数量', value: `${memberCount} 个` },
        ].map((it) => (
          <div key={it.label} className="rounded-2xl p-4 border bg-gradient-to-br from-purple-50/50 to-slate-100/80 border-purple-100/60">
            <Text className="text-[10px] font-black text-slate-500 uppercase tracking-widest opacity-80 block mb-2">{it.label}</Text>
            <Text className="text-sm font-black text-slate-800">{it.value}</Text>
          </div>
        ))}
      </div>

      <div className="glass-panel rounded-3xl p-6 border border-slate-100/50">
        <div className="flex items-center gap-2 mb-4">
          <div className="h-4 w-1 bg-purple-500 rounded-full" />
          <Text className="text-xs font-black text-slate-800 uppercase">融合成员与权重 ({memberCount})</Text>
        </div>
        {sourceModels.length === 0 ? (
          <Text className="text-[11px] text-slate-400 block">成员明细缺失（模型元数据未携带 source_models）。</Text>
        ) : (
          <div className="space-y-3">
            {sourceModels.map((m) => {
              const w = memberWeight(m);
              return (
                <div key={String(m.model_id)} className="rounded-xl border border-slate-100 bg-white/60 px-4 py-3">
                  <div className="flex items-center justify-between gap-3 mb-2">
                    <div className="min-w-0">
                      <Text className="font-black text-[12px] text-slate-800 truncate block">{String(m.model_name || m.model_id)}</Text>
                      <Text className="text-[9px] text-slate-400 font-mono truncate block">{String(m.model_id)}</Text>
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      {m.model_type && <Tag className="m-0 rounded-md border-0 px-1.5 py-0 text-[8px] font-black bg-slate-100 text-slate-500">{String(m.model_type)}</Tag>}
                      <Text className="text-sm font-black font-mono text-purple-700">{w === null ? '—' : `${(w * 100).toFixed(1)}%`}</Text>
                    </div>
                  </div>
                  <div className="h-1.5 rounded-full bg-slate-100 overflow-hidden">
                    <div className="h-full rounded-full bg-gradient-to-r from-purple-400 to-purple-600" style={{ width: `${w === null ? 0 : Math.round(w * 100)}%` }} />
                  </div>
                </div>
              );
            })}
          </div>
        )}
        <Text className="block mt-4 text-[10px] text-slate-400 leading-relaxed">
          权重按滚动 ICIR × 多样性惩罚 × 样本量收缩在创建时计算，快照与审计流水随模型落盘（weight_snapshot.json / weight_history.jsonl）。创建即进日更推理名单，可在「自动推理」中关闭。
        </Text>
      </div>
    </div>
  );
};

// ─── 模型详情面板 ────────────────────────────────────────────────────────────
/** 详情页统一分节卡：细描边 + 单色强调条，替代此前多色渐变面板。 */
const DetailSection: React.FC<{
  title: string;
  meta?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}> = ({ title, meta, className, children }) => (
  <section className={clsx('rounded-2xl border border-slate-200/70 bg-white overflow-hidden', className)}>
    <header className="flex items-center justify-between gap-2 px-5 py-3 border-b border-slate-100">
      <div className="flex items-center gap-2 min-w-0">
        <span className="h-3 w-0.5 rounded-full bg-blue-500 shrink-0" />
        <h3 className="m-0 text-[11px] font-black text-slate-600 tracking-wider">{title}</h3>
      </div>
      {meta}
    </header>
    {children}
  </section>
);

// ── 训练 run 图表数据（IC/RankIC 柱、十分位收益、多空净值）──────────────────
interface RunEvalPayload {
  metrics: {
    train?: { ic?: number; rank_ic?: number };
    val?: { ic?: number; rank_ic?: number };
    test?: { ic?: number; rank_ic?: number };
  } | null;
  report: EvalReport | null;
}

const runEvalCache = new Map<string, RunEvalPayload>();

async function loadRunEval(runId: string): Promise<RunEvalPayload> {
  const cached = runEvalCache.get(runId);
  if (cached) return cached;
  const run = await modelTrainingService.getTrainingRun(runId);
  const raw = (run?.result ?? {}) as Record<string, any>;
  const payload: RunEvalPayload = {
    metrics: raw.metrics ?? null,
    report: (raw.eval_report ?? raw.metadata?.eval_report ?? null) as EvalReport | null,
  };
  runEvalCache.set(runId, payload);
  return payload;
}

/** 每段样本窗口（训练/验证/测试）：卡片上标注训练时间、天数与占比。 */
type SplitWindowInfo = { range: [string, string]; days: number; percent: number };
type SplitWindows = Partial<Record<'train' | 'valid' | 'test', SplitWindowInfo>>;

type SplitMetricRow = { key: 'train' | 'valid' | 'test'; label: string; ic: number | null; rankIc: number | null; icir: number | null };

/** 三段指标卡（右栏纵向排列）：IC 大字 + 阈值刻度条 + RankIC/IR + 训练时间窗口。 */
const SplitMetricCards: React.FC<{
  metrics: RunEvalPayload['metrics'];
  windows?: SplitWindows | null;
  fallbackRows: SplitMetricRow[];
}> = ({ metrics, windows = null, fallbackRows }) => {
  const segmentOf = (key: 'train' | 'valid' | 'test') => (
    key === 'valid' ? metrics?.val : metrics?.[key]
  );
  const icValues = fallbackRows
    .flatMap((row) => {
      const seg = segmentOf(row.key);
      return [seg?.ic ?? row.ic, seg?.rank_ic ?? row.rankIc];
    })
    .filter((v): v is number => v != null && Number.isFinite(v));
  const dataMax = icValues.length ? Math.max(...icValues) : 0;
  const upper = Math.max(0.25, Math.ceil(dataMax / 0.05) * 0.05);

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
      {fallbackRows.map((row, idx) => {
        const seg = segmentOf(row.key);
        const icValue = seg?.ic ?? row.ic;
        const rankIc = seg?.rank_ic ?? row.rankIc;
        // 较上段：验证集对比训练集、测试集对比验证集（按 IC 相对变化，正负都显示）
        const prevRow = idx > 0 ? fallbackRows[idx - 1] : null;
        const prevSeg = prevRow ? segmentOf(prevRow.key) : null;
        const prevIc = prevRow ? (prevSeg?.ic ?? prevRow.ic) : null;
        const deltaPct = prevRow && icValue != null && prevIc != null && prevIc !== 0
          ? ((icValue - prevIc) / Math.abs(prevIc)) * 100
          : null;
        const win = windows?.[row.key];
        const theme = row.key === 'train'
          ? { bar: 'from-indigo-500 to-indigo-400', dot: 'bg-indigo-500' }
          : row.key === 'valid'
            ? { bar: 'from-sky-500 to-sky-400', dot: 'bg-sky-500' }
            : { bar: 'from-emerald-500 to-emerald-400', dot: 'bg-emerald-500' };
        // 阈值点：≥0.10 绿 / ≥0.05 琥珀 / 否则红（大数字用墨色，阈值用小圆点提示）
        const qualityDot = icValue == null
          ? 'bg-slate-200'
          : icValue >= 0.10 ? 'bg-emerald-500' : icValue >= 0.05 ? 'bg-amber-400' : 'bg-rose-400';
        const barPct = icValue == null ? 0 : Math.max(2, Math.min(100, (icValue / upper) * 100));
        return (
          <div
            key={row.key}
            className={clsx(
              'relative min-w-0 overflow-hidden rounded-2xl border bg-white p-4 shadow-sm transition-shadow hover:shadow-md',
              row.key === 'test' ? 'border-emerald-300 ring-1 ring-emerald-100' : 'border-slate-200/80',
            )}
          >
            <span className={clsx('absolute inset-x-0 top-0 h-[3px] bg-gradient-to-r', theme.bar)} />
            <div className="flex items-center justify-between gap-2">
              <div className="flex min-w-0 items-center gap-2">
                <span className={clsx('h-2 w-2 shrink-0 rounded-full', theme.dot)} />
                <span className="truncate text-[13px] font-black tracking-wider text-slate-600">{row.label}</span>
                {row.key === 'test' && (
                  <span className="shrink-0 rounded-full bg-emerald-600 px-1.5 py-0.5 text-[8px] font-black tracking-wider text-white">样本外</span>
                )}
              </div>
              {deltaPct != null && Number.isFinite(deltaPct) && (
                <span
                  title={`IC 较${fallbackRows[idx - 1].label}：${deltaPct >= 0 ? '+' : ''}${deltaPct.toFixed(1)}%`}
                  className={clsx(
                    'shrink-0 whitespace-nowrap rounded-full px-2 py-0.5 text-[10px] font-black tabular-nums',
                    deltaPct >= 0 ? 'bg-emerald-50 text-emerald-600' : 'bg-rose-50 text-rose-500',
                  )}
                >
                  较上段 {deltaPct >= 0 ? '+' : ''}{deltaPct.toFixed(1)}%
                </span>
              )}
            </div>

            <div className="mt-3 flex items-baseline gap-2">
              <span className="text-[9px] font-bold tracking-[0.18em] text-slate-400">IC</span>
              <span className={clsx('font-mono text-[32px] font-black leading-none tracking-tighter tabular-nums', icValue == null ? 'text-slate-300' : 'text-slate-900')}>
                {icValue == null ? '—' : icValue.toFixed(4)}
              </span>
              <span className={clsx('mb-1 h-1.5 w-1.5 rounded-full', qualityDot)} title="≥0.10 绿 / ≥0.05 琥珀 / 否则红" />
            </div>

            <div className="relative mt-2.5 h-1.5 overflow-hidden rounded-full bg-slate-100">
              <span
                className={clsx('absolute inset-y-0 left-0 rounded-full bg-gradient-to-r', theme.bar)}
                style={{ width: `${barPct}%` }}
              />
              <span className="absolute inset-y-0 w-px bg-slate-300/70" style={{ left: `${Math.min(100, (0.05 / upper) * 100)}%` }} />
              <span className="absolute inset-y-0 w-px bg-slate-300/70" style={{ left: `${Math.min(100, (0.10 / upper) * 100)}%` }} />
            </div>

            <div className="mt-3 flex items-center gap-6 border-t border-slate-100 pt-2.5">
              <div className="flex items-baseline gap-1.5">
                <span className="text-[9px] font-bold tracking-wider text-slate-400">RankIC</span>
                <span className="font-mono text-[15px] font-black tabular-nums text-slate-700">
                  {rankIc == null ? '—' : rankIc.toFixed(4)}
                </span>
              </div>
              <div className="flex items-baseline gap-1.5">
                <span className="text-[9px] font-bold tracking-wider text-slate-400">IR</span>
                <span className="font-mono text-[15px] font-black tabular-nums text-slate-700">
                  {row.icir == null ? '—' : row.icir.toFixed(3)}
                </span>
              </div>
            </div>

            {win && (
              <div className="mt-2.5 flex items-center justify-between gap-2 font-mono text-[10px] text-slate-400">
                <span className="truncate">{win.range[0]} → {win.range[1]}</span>
                <span className="shrink-0 whitespace-nowrap">{win.days} 天 · {win.percent.toFixed(1)}%</span>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
};

/** 性能指标柱状图：IC / RankIC 三段对比（纵轴常态 0–0.25，超出自动扩展）。 */
const ModelEvalCharts: React.FC<{
  metrics: RunEvalPayload['metrics'];
  loading: boolean;
  fallbackRows: Array<{ key: 'train' | 'valid' | 'test'; label: string; ic: number | null; rankIc: number | null; icir: number | null }>;
}> = ({ metrics, loading, fallbackRows }) => {
  const segmentOf = (key: 'train' | 'valid' | 'test') => (
    key === 'valid' ? metrics?.val : metrics?.[key]
  );
  const icData = fallbackRows.map((row) => {
    const seg = segmentOf(row.key);
    const rankIc = seg?.rank_ic ?? row.rankIc ?? null;
    return {
      name: row.label,
      IC: Number(seg?.ic ?? row.ic ?? 0),
      RankIC: Number(rankIc ?? 0),
      hasRankIc: rankIc !== null,
    };
  });
  const hasMetrics = Boolean(metrics?.train || metrics?.val || metrics?.test);
  const hasAnyChart = hasMetrics;
  // 纵轴基准上限 0.25：正常模型共用同一把尺子、柱高可直接比较；
  // 若确有 IC 超过 0.25，则自动扩展到能完整显示（不裁掉真实值）。
  const icValues = icData
    .flatMap((d) => [d.IC, d.RankIC])
    .filter((v) => Number.isFinite(v));
  const dataMax = icValues.length ? Math.max(...icValues) : 0;
  const hasNegativeIc = icValues.some((v) => v < 0);
  const lower = hasNegativeIc ? -0.05 : 0;
  const upper = Math.max(0.25, Math.ceil(dataMax / 0.05) * 0.05);
  const icDomain: [number, number] = [lower, upper];
  const icTicks: number[] = [];
  for (let tick = lower; tick <= upper + 1e-9; tick += 0.05) icTicks.push(Number(tick.toFixed(2)));

  return (
    <div className="px-4 pt-4 pb-3 space-y-3">
      {loading && (
        <div className="flex items-center gap-2 text-[10px] text-slate-400">
          <Spin size="small" /> 读取训练记录图表…
        </div>
      )}
      {fallbackRows.length > 0 && (
        <>
          {hasMetrics && (
            <div className="pt-1">
              <div className="mb-2 flex items-end justify-between gap-3">
                <div>
                  <div className="text-[11px] font-black tracking-wider text-slate-600">IC 评估</div>
                  <div className="mt-0.5 text-[9px] text-slate-400">
                    训练集 / 验证集 / 测试集 · 纵轴 0–0.25（超出自动扩展）· 虚线 0.05 / 0.10 参考
                  </div>
                </div>
                <div className="flex items-center gap-3 pb-0.5 text-[10px] font-bold text-slate-500">
                  <span className="flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-[3px] bg-[#6366f1]" />IC</span>
                  <span className="flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-[3px] bg-[#38bdf8]" />RankIC</span>
                </div>
              </div>
              <ResponsiveContainer width="100%" height={260}>
                <ReBarChart data={icData} barCategoryGap="22%" barGap={6} margin={{ top: 8, right: 10, left: -6, bottom: 0 }}>
                  <CartesianGrid vertical={false} strokeDasharray="3 3" stroke="#eef2f7" />
                  <XAxis
                    dataKey="name"
                    tick={{ fontSize: 12, fill: '#475569', fontWeight: 600 }}
                    axisLine={false}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{ fontSize: 10, fill: '#94a3b8' }}
                    axisLine={false}
                    tickLine={false}
                    domain={icDomain}
                    ticks={icTicks}
                    allowDataOverflow
                    tickFormatter={(v: number) => v.toFixed(2)}
                  />
                  <ReTooltip
                    contentStyle={{ borderRadius: 12, fontSize: 11, border: '1px solid #e2e8f0', boxShadow: '0 8px 24px rgba(15,23,42,0.08)' }}
                    formatter={(value: number) => Number(value).toFixed(4)}
                  />
                  <ReferenceLine y={0.05} stroke="#f59e0b" strokeDasharray="5 3" />
                  <ReferenceLine y={0.10} stroke="#10b981" strokeDasharray="5 3" />
                  <Bar dataKey="IC" fill="#6366f1" radius={[6, 6, 0, 0]} />
                  <Bar dataKey="RankIC" fill="#38bdf8" radius={[6, 6, 0, 0]} />
                </ReBarChart>
              </ResponsiveContainer>
            </div>
          )}
        </>
      )}
      {!loading && !hasAnyChart && (
        <div className="rounded-lg border border-dashed border-slate-200 bg-slate-50/40 px-3 py-4 text-center text-[10px] text-slate-400">
          该模型没有训练记录图表（IC 由训练 run 产物生成）
        </div>
      )}
    </div>
  );
};

/** 特征 → 使用它的模型（跨模型特征图谱的唯一口径）。 */
function buildFeatureUsage(models: UserModelRecord[]): Map<string, UserModelRecord[]> {
  const usage = new Map<string, UserModelRecord[]>();
  for (const m of models) {
    for (const feature of modelFeatureList(m)) {
      const owners = usage.get(feature);
      if (!owners) usage.set(feature, [m]);
      else if (!owners.some((owner) => owner.model_id === m.model_id)) owners.push(m);
    }
  }
  return usage;
}

/**
 * 特征 × 模型交互图谱：
 * - 按特征：所有特征 → 用过它的模型（点模型跳详情）；
 * - 按模型：所有模型 → 入模特征清单；
 * 搜索同时匹配特征名与模型名。
 */
const FEATURE_ROW_LIMIT = 60;
const MODEL_ROW_LIMIT = 40;
const OWNER_CHIP_LIMIT = 8;

export const FeatureMatrixPanel: React.FC<{
  open: boolean;
  onCollapse?: () => void;
  showTitle?: boolean;
  models: UserModelRecord[];
  currentModelId: string;
  focusFeature?: string | null;
  onSelectModel?: (modelId: string) => void;
  /** 统计口径说明（如「A 级 6 个模型」）——共用数不是全量模型统计 */
  scopeLabel?: string | null;
}> = ({ open, onCollapse, showTitle = true, models, currentModelId, focusFeature = null, onSelectModel, scopeLabel = null }) => {
  const [tab, setTab] = useState<'feature' | 'model' | 'matrix'>('feature');
  const [query, setQuery] = useState('');
  const [sharedOnly, setSharedOnly] = useState(false);
  const [sourceFilter, setSourceFilter] = useState<string | null>(null);
  const [expandedModels, setExpandedModels] = useState<string[]>([]);
  const [featureLimit, setFeatureLimit] = useState(FEATURE_ROW_LIMIT);
  const [modelLimit, setModelLimit] = useState(MODEL_ROW_LIMIT);

  useEffect(() => {
    if (open) setQuery(focusFeature ?? '');
  }, [open, focusFeature]);
  useEffect(() => {
    setFeatureLimit(FEATURE_ROW_LIMIT);
  }, [query, sharedOnly, sourceFilter, tab]);

  const featureRows = useMemo(() => {
    if (!open) return [] as Array<{ feature: string; owners: UserModelRecord[] }>;
    const usage = buildFeatureUsage(models);
    const latestOwner = (owners: UserModelRecord[]) => Math.max(
      ...owners.map((owner) => Date.parse(owner.created_at || '') || 0),
      0,
    );
    return Array.from(usage.entries())
      .map(([feature, owners]) => ({ feature, owners }))
      .sort((a, b) => (
        b.owners.length - a.owners.length
        || latestOwner(b.owners) - latestOwner(a.owners)
        || a.feature.localeCompare(b.feature)
      ));
  }, [models, open]);

  const modelRows = useMemo(
    () => (open
      ? models
        .map((m) => ({ model: m, features: modelFeatureList(m) }))
        .filter((row) => row.features.length > 0)
        .sort((a, b) => b.features.length - a.features.length)
      : []),
    [models, open],
  );

  // 特征来源族（feat_a_1 → feat），用于按因子库/前缀分组筛选
  const featureFamily = (name: string): string => {
    const prefix = name.toLowerCase().split(/[_\d.]/)[0] ?? '';
    return prefix || '其他';
  };
  const sourceFamilies = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of featureRows) {
      const family = featureFamily(row.feature);
      counts.set(family, (counts.get(family) ?? 0) + 1);
    }
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]).slice(0, 8);
  }, [featureRows]);

  const normalizedQuery = query.trim().toLowerCase();
  const visibleFeatureRows = featureRows.filter((row) => {
    if (sharedOnly && row.owners.length < 2) return false;
    if (sourceFilter && featureFamily(row.feature) !== sourceFilter) return false;
    if (!normalizedQuery) return true;
    return row.feature.toLowerCase().includes(normalizedQuery)
      || row.owners.some((owner) => modelDisplayName(owner).toLowerCase().includes(normalizedQuery));
  });
  const visibleModelRows = normalizedQuery
    ? modelRows.filter((row) => (
      modelDisplayName(row.model).toLowerCase().includes(normalizedQuery)
      || row.features.some((feature) => feature.toLowerCase().includes(normalizedQuery))
    ))
    : modelRows;
  const sharedFeatureCount = featureRows.filter((row) => row.owners.length > 1).length;
  // 行数分页：一次性渲染数百行（每行还挂模型 chips）会明显卡顿，默认只渲染前 N 行
  const shownFeatureRows = visibleFeatureRows.slice(0, featureLimit);
  const shownModelRows = visibleModelRows.slice(0, modelLimit);

  // 矩阵视图：共用特征 Top 40 × 拥有这些特征的模型（最多 30 列），仅矩阵 Tab 激活时计算
  const matrixFeatures = useMemo(
    () => (open && tab === 'matrix' ? featureRows.filter((row) => row.owners.length > 1).slice(0, 40) : []),
    [featureRows, open, tab],
  );
  const matrixModels = useMemo(() => {
    const ids = new Set<string>();
    matrixFeatures.forEach((row) => row.owners.forEach((owner) => ids.add(owner.model_id)));
    return modelRows.filter((row) => ids.has(row.model.model_id)).slice(0, 30);
  }, [matrixFeatures, modelRows]);

  const selectModel = (modelId: string) => {
    onSelectModel?.(modelId);
  };

  if (!open) return null;

  return (
    <div data-testid="feature-matrix" className="border-t border-slate-100 bg-slate-50/30 px-4 py-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="min-w-0">
          {showTitle && <div className="text-[11px] font-black text-slate-700">特征 × 模型图谱</div>}
          <div className="text-[9px] font-bold text-slate-400">
            {featureRows.length} 个特征 · {modelRows.length} 个模型 · {sharedFeatureCount} 个特征被 ≥2 个模型共用
            {scopeLabel ? ` · 口径：${scopeLabel}` : ''}
          </div>
        </div>
        {onCollapse && (
          <button
            type="button"
            onClick={onCollapse}
            className="shrink-0 rounded-md border border-slate-200 px-1.5 py-0.5 text-[10px] font-bold text-slate-500 hover:bg-white transition-colors"
          >
            收起
          </button>
        )}
      </div>
      <Input
        size="small"
        allowClear
        prefix={<Search size={13} className="text-slate-300" />}
        placeholder="搜索特征名 / 模型名…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        className="rounded-lg mb-2"
      />
      <div className="mb-2 flex flex-wrap items-center gap-1">
        <label className="flex cursor-pointer select-none items-center gap-1 rounded-md border border-slate-200 px-1.5 py-0.5 text-[10px] font-bold text-slate-600">
          <input
            type="checkbox"
            checked={sharedOnly}
            onChange={(e) => setSharedOnly(e.target.checked)}
            className="accent-blue-600"
          />
          只看共用（≥2 模型）
        </label>
        <span className="text-[9px] font-bold text-slate-300">|</span>
        <button
          type="button"
          onClick={() => setSourceFilter(null)}
          className={clsx(
            'rounded-md px-1.5 py-0.5 text-[10px] font-bold transition-colors',
            sourceFilter === null ? 'bg-slate-800 text-white' : 'text-slate-500 hover:bg-slate-100',
          )}
        >
          全部来源
        </button>
        {sourceFamilies.map(([family, count]) => (
          <button
            key={family}
            type="button"
            onClick={() => setSourceFilter((prev) => (prev === family ? null : family))}
            className={clsx(
              'rounded-md px-1.5 py-0.5 font-mono text-[10px] font-bold transition-colors',
              sourceFilter === family ? 'bg-blue-600 text-white' : 'text-slate-500 hover:bg-slate-100',
            )}
          >
            {family} · {count}
          </button>
        ))}
      </div>
      <Tabs
        size="small"
        activeKey={tab}
        destroyInactiveTabPane
        onChange={(key) => setTab(key as 'feature' | 'model' | 'matrix')}
        items={[
          {
            key: 'feature',
            label: `按特征 (${visibleFeatureRows.length})`,
            children: (
              <div className="max-h-[420px] overflow-y-auto custom-scrollbar pr-1 space-y-1.5">
                {shownFeatureRows.map(({ feature, owners }) => {
                  const usedByCurrent = owners.some((owner) => owner.model_id === currentModelId);
                  return (
                    <div
                      key={feature}
                      className={clsx(
                        'rounded-lg border px-3 py-2',
                        usedByCurrent ? 'border-blue-100 bg-blue-50/30' : 'border-slate-100 bg-white',
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate font-mono text-[11px] font-bold text-slate-700">{feature}</span>
                        <span className={clsx(
                          'shrink-0 px-1.5 py-0.5 rounded text-[9px] font-black',
                          owners.length > 1 ? 'bg-blue-50 text-blue-600' : 'bg-slate-100 text-slate-400',
                        )}>
                          {owners.length} 个模型
                        </span>
                      </div>
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        {owners.slice(0, OWNER_CHIP_LIMIT).map((owner) => (
                          <button
                            key={owner.model_id}
                            type="button"
                            title={owner.model_id}
                            onClick={() => selectModel(owner.model_id)}
                            className={clsx(
                              'px-1.5 py-0.5 rounded text-[9px] font-bold border transition-colors',
                              owner.model_id === currentModelId
                                ? 'bg-blue-600 text-white border-blue-600'
                                : 'bg-slate-50 text-slate-600 border-slate-100 hover:border-blue-200 hover:text-blue-600',
                            )}
                          >
                            {modelDisplayName(owner)}
                          </button>
                        ))}
                        {owners.length > OWNER_CHIP_LIMIT && (
                          <span className="px-1.5 py-0.5 rounded bg-slate-50 text-[9px] font-bold text-slate-400">
                            +{owners.length - OWNER_CHIP_LIMIT}
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })}
                {visibleFeatureRows.length > shownFeatureRows.length && (
                  <button
                    type="button"
                    onClick={() => setFeatureLimit((prev) => prev + FEATURE_ROW_LIMIT)}
                    className="w-full rounded-lg border border-dashed border-slate-300 py-1.5 text-[10px] font-bold text-slate-500 hover:border-blue-300 hover:text-blue-600 transition-colors"
                  >
                    显示更多（剩余 {visibleFeatureRows.length - shownFeatureRows.length}）
                  </button>
                )}
                {visibleFeatureRows.length === 0 && (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-[11px] text-slate-400">没有匹配的特征</span>} />
                )}
              </div>
            ),
          },
          {
            key: 'model',
            label: `按模型 (${visibleModelRows.length})`,
            children: (
              <div className="max-h-[420px] overflow-y-auto custom-scrollbar pr-1 space-y-1.5">
                {shownModelRows.map(({ model: owner, features: ownerFeatures }) => {
                  const isExpanded = Boolean(normalizedQuery) || expandedModels.includes(owner.model_id);
                  const shown = isExpanded ? ownerFeatures : ownerFeatures.slice(0, 12);
                  return (
                    <div
                      key={owner.model_id}
                      className={clsx(
                        'rounded-lg border px-3 py-2',
                        owner.model_id === currentModelId ? 'border-blue-100 bg-blue-50/30' : 'border-slate-100 bg-white',
                      )}
                    >
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => selectModel(owner.model_id)}
                          className="min-w-0 text-left truncate text-[11px] font-black text-slate-700 hover:text-blue-600"
                        >
                          {modelDisplayName(owner)}
                        </button>
                        <span className="ml-auto shrink-0 font-mono text-[9px] font-bold text-slate-400">{ownerFeatures.length} 维</span>
                        {ownerFeatures.length > 12 && !normalizedQuery && (
                          <button
                            type="button"
                            onClick={() => setExpandedModels((prev) => (
                              prev.includes(owner.model_id)
                                ? prev.filter((id) => id !== owner.model_id)
                                : [...prev, owner.model_id]
                            ))}
                            className="shrink-0 text-[9px] font-bold text-blue-600 hover:underline"
                          >
                            {isExpanded ? '收起' : '全部'}
                          </button>
                        )}
                      </div>
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        {shown.map((feature) => (
                          <span key={feature} className="px-1.5 py-0.5 rounded bg-slate-50 border border-slate-100 text-[9px] font-mono text-slate-600">
                            {feature}
                          </span>
                        ))}
                      </div>
                    </div>
                  );
                })}
                {visibleModelRows.length > shownModelRows.length && (
                  <button
                    type="button"
                    onClick={() => setModelLimit((prev) => prev + MODEL_ROW_LIMIT)}
                    className="w-full rounded-lg border border-dashed border-slate-300 py-1.5 text-[10px] font-bold text-slate-500 hover:border-blue-300 hover:text-blue-600 transition-colors"
                  >
                    显示更多（剩余 {visibleModelRows.length - shownModelRows.length}）
                  </button>
                )}
                {visibleModelRows.length === 0 && (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-[11px] text-slate-400">没有匹配的模型</span>} />
                )}
              </div>
            ),
          },
          {
            key: 'matrix',
            label: '矩阵',
            children: (
              <div className="h-[calc(var(--app-h)-360px)] overflow-auto custom-scrollbar">
                {matrixFeatures.length === 0 || matrixModels.length === 0 ? (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description={<span className="text-[11px] text-slate-400">暂无跨模型共用特征</span>}
                  />
                ) : (
                  <>
                    <div className="mb-2 text-[9px] text-slate-400">
                      共用特征 Top {matrixFeatures.length} × {matrixModels.length} 个模型（蓝格 = 该模型用过此特征）
                    </div>
                    <table className="border-separate border-spacing-0">
                      <thead>
                        <tr>
                          <th className="sticky left-0 z-10 bg-white px-2 py-1 text-left text-[9px] font-black text-slate-400">特征 \ 模型</th>
                          {matrixModels.map(({ model: owner }) => (
                            <th key={owner.model_id} className="px-0.5 py-1 align-bottom">
                              <button
                                type="button"
                                title={modelDisplayName(owner)}
                                onClick={() => selectModel(owner.model_id)}
                                className={clsx(
                                  'h-16 whitespace-nowrap font-mono text-[8px] font-bold [writing-mode:vertical-rl]',
                                  owner.model_id === currentModelId ? 'text-blue-600' : 'text-slate-400 hover:text-blue-600',
                                )}
                              >
                                {modelDisplayName(owner)}
                              </button>
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {matrixFeatures.map(({ feature, owners }) => {
                          const usedIds = new Set(owners.map((owner) => owner.model_id));
                          return (
                            <tr key={feature}>
                              <td className="sticky left-0 z-10 max-w-[200px] truncate bg-white px-2 py-0.5 font-mono text-[9px] font-bold text-slate-600">
                                {feature}
                              </td>
                              {matrixModels.map(({ model: owner }) => (
                                <td key={owner.model_id} className="px-0.5 py-0.5">
                                  <span className={clsx(
                                    'block h-2.5 w-2.5 rounded-[2px]',
                                    usedIds.has(owner.model_id) ? 'bg-blue-500' : 'bg-slate-100',
                                  )} />
                                </td>
                              ))}
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </>
                )}
              </div>
            ),
          },
        ]}
      />
    </div>
  );
};

export const ModelDetailPanel: React.FC<{
  model: UserModelRecord;
  /** 全部已加载模型（当前市场），用于特征 × 模型跨模型图谱。 */
  allModels?: UserModelRecord[];
  /** 图谱里点模型：切到该模型详情。 */
  onSelectModel?: (modelId: string) => void;
}> = ({ model, allModels, onSelectModel }) => {
  const meta = getMeta(model);
  const metrics = getMetrics(model);
  const timePeriods = extractTimePeriods(meta);
  // 训练/验证/测试的时间窗口与占比（原样本时间轴的信息，现直接标注在三段指标卡上）
  const splitWindows = useMemo<SplitWindows | null>(() => {
    if (!timePeriods) return null;
    const stats = calcTimeSplitStats(timePeriods);
    const windows: SplitWindows = {
      train: { range: timePeriods.train, days: stats.train.days, percent: stats.train.percent },
    };
    if (stats.val) windows.valid = { range: timePeriods.val, days: stats.val.days, percent: stats.val.percent };
    if (stats.test) windows.test = { range: timePeriods.test, days: stats.test.days, percent: stats.test.percent };
    return windows;
  }, [timePeriods]);
  // 特征 × 模型图谱：跨模型特征使用关系（当前市场已加载的全部模型）
  const [featureExplorer, setFeatureExplorer] = useState<{ open: boolean; focus: string | null }>({ open: false, focus: null });
  const features = useMemo(() => modelFeatureList(model), [model]);
  // 训练端落库的入模特征分类（feature_categories）：比逐条特征名更直观地说明模型用了哪类因子
  const featureCategories = useMemo(() => {
    const raw = (meta as Record<string, any>).feature_categories;
    return Array.isArray(raw) ? raw.map((item) => String(item)).filter(Boolean) : [];
  }, [meta]);
  const scopeModels = useMemo(() => {
    if (!allModels || allModels.length === 0) return [model];
    return allModels.some((item) => item.model_id === model.model_id) ? allModels : [model, ...allModels];
  }, [allModels, model]);
  // 跨模型共用口径：只统计评估评分 A 的模型（A 级不足 2 个时降到 A/B）。
  // 全量模型统计没有区分度——同市场几十个模型用同一批因子库时，「N 维全部被共用」
  // 是结构性结果，读不出任何信息；C 级及以下模型也不该参与「好模型共识」。
  const [modelGrades, setModelGrades] = useState<{ loaded: boolean; failed: boolean; byId: Record<string, string> }>({
    loaded: false,
    failed: false,
    byId: {},
  });
  useEffect(() => {
    let cancelled = false;
    listScores({ objectType: 'model', latestOnly: true, limit: 500 })
      .then((resp) => {
        if (cancelled) return;
        const byId: Record<string, string> = {};
        for (const row of resp?.data ?? []) {
          const id = String(row?.object_id || '').replace(/^sys-/, '');
          const grade = String(row?.grade || '').trim().toUpperCase().replace('†', '');
          if (id && grade) byId[id] = grade;
        }
        setModelGrades({ loaded: true, failed: false, byId });
      })
      .catch(() => {
        if (!cancelled) setModelGrades({ loaded: true, failed: true, byId: {} });
      });
    return () => { cancelled = true; };
  }, []);
  const qualityScope = useMemo(() => {
    const gradeOf = (item: UserModelRecord) => modelGrades.byId[String(item.model_id).replace(/^sys-/, '')] ?? null;
    const aModels = scopeModels.filter((item) => gradeOf(item) === 'A');
    if (aModels.length >= 2) {
      return { models: aModels, gradeLabel: 'A 级' };
    }
    const abModels = [...aModels, ...scopeModels.filter((item) => gradeOf(item) === 'B')];
    if (abModels.length >= 2) {
      return { models: abModels, gradeLabel: 'A/B 级' };
    }
    return { models: [] as UserModelRecord[], gradeLabel: 'A/B 级（暂无）' };
  }, [scopeModels, modelGrades.byId]);
  // 评分接口不可用 / 还在载入时退回全量模型，避免把「取不到评分」误读成「没有共用」。
  const scopedModels = useMemo(() => {
    if (!modelGrades.loaded || modelGrades.failed) return scopeModels;
    const ids = new Set(qualityScope.models.map((item) => item.model_id));
    ids.add(model.model_id); // 本模型始终作为锚点，便于对照它自己
    return scopeModels.filter((item) => ids.has(item.model_id));
  }, [modelGrades.loaded, modelGrades.failed, qualityScope.models, scopeModels, model.model_id]);
  const scopeLabel = !modelGrades.loaded
    ? '评分载入中（暂按全量）'
    : modelGrades.failed
      ? '全量（评分不可用）'
      : `${qualityScope.gradeLabel} ${qualityScope.models.length} 个模型`;
  const featureUsage = useMemo(() => buildFeatureUsage(scopedModels), [scopedModels]);
  const sharedFeatureCount = useMemo(
    () => features.filter((feature) => (featureUsage.get(feature)?.length ?? 0) > 1).length,
    [features, featureUsage],
  );
  const topSharedFeatures = useMemo(
    () => features
      .map((feature) => ({ feature, owners: featureUsage.get(feature) ?? [] }))
      .filter((row) => row.owners.length > 1)
      .sort((a, b) => b.owners.length - a.owners.length)
      .slice(0, 3),
    [features, featureUsage],
  );
  // 训练 run 图表数据：按 source_run_id 懒加载，带进程内缓存避免来回切换重复拉取
  const sourceRunId = model.source_run_id;
  const [runEval, setRunEval] = useState<RunEvalPayload | null>(null);
  const [runEvalLoading, setRunEvalLoading] = useState(false);
  useEffect(() => {
    setRunEval(null);
    if (!sourceRunId) {
      setRunEvalLoading(false);
      return;
    }
    let cancelled = false;
    setRunEvalLoading(true);
    loadRunEval(sourceRunId)
      .then((payload) => {
        if (!cancelled) setRunEval(payload);
      })
      .catch(() => { /* 图表是增强位：失败降级为「无图表」空态 */ })
      .finally(() => {
        if (!cancelled) setRunEvalLoading(false);
      });
    return () => { cancelled = true; };
  }, [sourceRunId]);
  if (isEnsembleModel(model)) {
    return <EnsembleDetailPanel model={model} />;
  }
  // 训练/验证/测试集指标口径与模型卡片共用（resolveSplitMetrics 是别名表唯一出处）
  const splitNumbers = resolveSplitMetrics(meta);
  const splitMetrics: Array<{
    key: 'train' | 'valid' | 'test';
    label: string;
    color: 'blue' | 'indigo' | 'emerald';
    ic: number | null;
    rankIc: number | null;
    icir: number | null;
    trendLabel: string;
  }> = [];
  let previousIc: number | null = null;
  for (const key of ['train', 'valid', 'test'] as const) {
    const { ic, rankIc, icir } = splitNumbers[key];
    if (ic === null && rankIc === null && icir === null) continue;
    splitMetrics.push({
      key,
      label: key === 'train' ? '训练集' : key === 'valid' ? '验证集' : '测试集',
      color: key === 'train' ? 'blue' : key === 'valid' ? 'indigo' : 'emerald',
      ic,
      rankIc,
      icir,
      trendLabel: formatTrendLabel(ic, previousIc, 2),
    });
    previousIc = ic ?? previousIc;
  }
  const metadataMetrics = meta.metrics && typeof meta.metrics === 'object'
    ? meta.metrics as Record<string, any>
    : null;
  const hasSplitIC = splitMetrics.length > 0;
  const ic = metrics.ic ?? metrics.IC ?? metrics.mean_ic ?? resolveMetricNumber(metadataMetrics, ['test_ic', 'val_ic', 'train_ic']) ?? null;
  const icir = metrics.icir ?? metrics.ICIR ?? metrics.IC_IR ?? resolveMetricNumber(metadataMetrics, ['test_rank_icir', 'val_rank_icir', 'test_icir', 'val_icir', 'train_rank_icir', 'train_icir']) ?? null;
  const hasIC = [ic, icir].some(v => v !== null);

  const horizonDays = meta.target_horizon_days ?? meta.horizon_days;
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

  // 无分集指标时用汇总口径兜底展示（metrics_json 已按 test → valid 顺序取值）
  const metricRows = splitMetrics.length > 0
    ? splitMetrics
    : [{ key: 'test' as const, label: '汇总', color: 'emerald' as const, ic, rankIc: null as number | null, icir, trendLabel: '基线' }];
  // WFA 窗口条统一比例尺
  const maxAbsWfaIc = Math.max(
    ...(Array.isArray(meta.wfa?.windows) ? meta.wfa.windows.map((w: any) => Math.abs(Number(w.ic) || 0)) : [0]),
    1e-9,
  );
  // 特征 chip：点击打开跨模型图谱
  const renderFeatureChip = (feature: string, key: React.Key) => {
    const ownerCount = featureUsage.get(feature)?.length ?? 1;
    return (
      <button
        key={key}
        type="button"
        onClick={() => setFeatureExplorer({ open: true, focus: feature })}
        title={ownerCount > 1 ? `${ownerCount} 个模型使用该特征，点击查看` : '点击查看跨模型特征图谱'}
        className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-slate-50 border border-slate-100 text-[10px] font-mono text-slate-600 hover:border-blue-200 hover:text-blue-600 hover:bg-blue-50/40 transition-colors"
      >
        {feature}
        {ownerCount > 1 && (
          <span className="px-1 rounded bg-blue-50 text-blue-600 text-[8px] font-black">{ownerCount}</span>
        )}
      </button>
    );
  };

  return (
    <div className="space-y-4 pt-1">
      {/* 第一行：三段指标卡（全宽一行，置于 IC 评估上方） */}
      <SplitMetricCards
        metrics={runEval?.metrics ?? null}
        windows={splitWindows}
        fallbackRows={metricRows.map((item) => ({
          key: item.key,
          label: item.label,
          ic: item.ic,
          rankIc: item.rankIc,
          icir: item.icir,
        }))}
      />

      {/* 第二行：左 性能指标柱状图 ｜ 右 特征工程（两卡统一 400px 高，上下边缘严格对齐） */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 items-stretch">
        <div className="lg:col-span-7">
          <DetailSection
            title="性能指标"
            className="lg:h-[400px] flex flex-col"
            meta={<span className="text-[9px] font-black tracking-wider text-slate-400">IC · RANK IC · IR</span>}
          >
            <div className="flex flex-1 flex-col justify-center">
              <ModelEvalCharts
                metrics={runEval?.metrics ?? null}
                loading={runEvalLoading}
                fallbackRows={metricRows.map((item) => ({
                  key: item.key,
                  label: item.label,
                  ic: item.ic,
                  rankIc: item.rankIc,
                  icir: item.icir,
                }))}
              />
            </div>
            <div className="px-5 py-2.5 border-t border-slate-100 bg-slate-50/40 text-[10px] leading-relaxed text-slate-400">
              <span className="font-bold text-slate-500">怎么用：</span>
              输出是<b className="text-slate-600">截面排名分</b>（同批可比、越靠前越强），T+{horizonDays || '—'}；跨模型/跨市场不可直接比较。
            </div>
          </DetailSection>
        </div>

        <div className="lg:col-span-5 flex flex-col gap-4">
          {/* 特征工程（右栏窄版）：与左图统一 400px 高，特征区自适应高度、超出部分右侧滚动条查看 */}
          <DetailSection
            title="特征工程"
            className="lg:h-[400px] flex flex-col"
            meta={<span className="text-[10px] font-mono font-bold text-slate-400">{features.length} 维</span>}
          >
            <div className="flex min-h-0 flex-1 flex-col gap-2 px-4 py-3">
              {featureCategories.length > 0 && (
                <div className="flex flex-wrap items-center gap-1">
                  {featureCategories.map((cat) => (
                    <span
                      key={cat}
                      className="rounded-md border border-indigo-100 bg-indigo-50/70 px-1.5 py-0.5 text-[9px] font-bold text-indigo-700"
                    >
                      {cat}
                    </span>
                  ))}
                </div>
              )}
              {features.length === 0 ? (
                <span className="text-[10px] text-slate-400">该模型未记录特征清单；可从「特征 × 模型图谱」查看其他模型。</span>
              ) : (
                <div className="flex min-h-0 flex-1 flex-wrap content-start gap-1 overflow-y-auto chip-scroll pr-2">
                  {features.map((f, i) => renderFeatureChip(f, i))}
                </div>
              )}
              <div className="flex items-center justify-between gap-3 border-t border-slate-100 pt-2">
                <div className="min-w-0">
                  <div
                    className="truncate text-[9px] font-bold tracking-wider text-slate-400"
                    title="共用统计只计入评估评分 A 的模型；A 级不足 2 个时降到 A/B，C 级及以下不计入。评分接口不可用时退回全量。"
                  >
                    跨模型共用 · {scopeLabel}
                  </div>
                  <div className="font-mono text-[17px] font-black leading-tight text-slate-800">
                    {sharedFeatureCount}<span className="ml-1 text-[9px] font-bold text-slate-400">个特征</span>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => setFeatureExplorer({ open: true, focus: null })}
                  className="shrink-0 rounded-md border border-blue-100 px-2.5 py-1.5 text-[10px] font-bold text-blue-600 hover:bg-blue-50 transition-colors"
                >
                  特征 × 模型图谱
                </button>
              </div>
              {topSharedFeatures.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {topSharedFeatures.map(({ feature, owners }) => (
                    <button
                      key={feature}
                      type="button"
                      onClick={() => setFeatureExplorer({ open: true, focus: feature })}
                      className="flex max-w-full items-center gap-1 rounded bg-blue-50/60 px-1.5 py-0.5 font-mono text-[9px] font-bold text-blue-600 hover:bg-blue-100 transition-colors"
                    >
                      <span className="truncate">{feature}</span>
                      <span className="shrink-0">×{owners.length}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </DetailSection>
        </div>
      </div>

      {modelParams && importantParams.length > 0 && (
        <DetailSection
          title="核心超参"
          meta={<span className="text-[10px] font-mono font-bold text-slate-400">{importantParams.length}</span>}
        >
          <div className="grid grid-cols-2 gap-1.5 p-4 md:grid-cols-4">
            {importantParams.map(k => (
              <div key={k} className="rounded-lg bg-slate-50/80 border border-slate-100 px-2.5 py-2">
                <div className="text-[8px] font-bold uppercase tracking-wide text-slate-400 truncate">{k.replace(/_/g, ' ')}</div>
                <div className="text-[11px] font-black font-mono text-slate-700 truncate">{String(modelParams[k])}</div>
              </div>
            ))}
          </div>
        </DetailSection>
      )}

      {/* 特征 × 模型图谱：居中弹窗（宽屏、可搜索/筛选/矩阵三视图） */}
      <Modal
        open={featureExplorer.open}
        onCancel={() => setFeatureExplorer((prev) => ({ ...prev, open: false }))}
        footer={null}
        centered
        width={1080}
        destroyOnHidden
        style={{ maxWidth: '94vw' }}
        title={<span className="text-sm font-black text-slate-800">特征 × 模型图谱</span>}
        styles={{ body: { padding: 16, maxHeight: '78vh', overflowY: 'auto' } }}
      >
        <FeatureMatrixPanel
          open={featureExplorer.open}
          showTitle={false}
          focusFeature={featureExplorer.focus}
          models={scopedModels}
          currentModelId={model.model_id}
          onSelectModel={onSelectModel}
          scopeLabel={scopeLabel}
        />
      </Modal>

      {/* WFA 稳定性诊断（全宽） */}
      {meta.wfa?.enabled && meta.wfa.windows?.length > 0 && (
            <DetailSection
              title="WFA 稳定性诊断"
              meta={(
                <span className={clsx(
                  'px-1.5 py-0.5 rounded-md text-[9px] font-black',
                  meta.wfa.stability === 'stable' ? 'bg-emerald-50 text-emerald-600' : 'bg-amber-50 text-amber-600',
                )}>
                  {meta.wfa.stability === 'stable' ? '稳定' : '不稳定'}
                </span>
              )}
            >
              <div className="grid grid-cols-4 gap-px bg-slate-100">
                {[
                  {
                    label: 'IC 均值',
                    value: Number(meta.wfa.ic_mean).toFixed(4),
                    tone: Number(meta.wfa.ic_mean) >= 0.05 ? 'text-emerald-600' : Number(meta.wfa.ic_mean) >= 0 ? 'text-amber-600' : 'text-rose-500',
                  },
                  {
                    label: 'IC 标准差',
                    value: Number(meta.wfa.ic_std).toFixed(4),
                    tone: Number(meta.wfa.ic_std) <= 0.02 ? 'text-emerald-600' : 'text-amber-600',
                  },
                  {
                    label: 'ICIR',
                    value: Number.isFinite(Number(meta.wfa.overall_icir)) ? Number(meta.wfa.overall_icir).toFixed(3) : '—',
                    tone: 'text-slate-700',
                  },
                  {
                    label: '正窗占比',
                    value: `${Math.round(Number(meta.wfa.positive_rate) * 100)}%`,
                    tone: 'text-slate-700',
                  },
                ].map((stat) => (
                  <div key={stat.label} className="bg-white px-2 py-2.5 text-center">
                    <div className="text-[8px] font-bold text-slate-400">{stat.label}</div>
                    <div className={clsx('mt-0.5 font-mono text-[13px] font-black tabular-nums', stat.tone)}>{stat.value}</div>
                  </div>
                ))}
              </div>

              <div className="px-4 py-3 space-y-1 max-h-[200px] overflow-y-auto custom-scrollbar">
                {meta.wfa.windows.map((w: any) => {
                  const wfaIc = Number(w.ic);
                  return (
                    <div key={w.window_idx} className="flex items-center gap-2 px-2 py-1.5 rounded-lg bg-slate-50/70">
                      <span className="w-7 shrink-0 font-mono text-[9px] font-black text-slate-500">W{w.window_idx + 1}</span>
                      <span className="flex-1 min-w-0 truncate font-mono text-[9px] text-slate-400">{w.val_start} → {w.val_end}</span>
                      <span className="w-14 h-1 shrink-0 rounded-full bg-slate-200/70 overflow-hidden">
                        <span
                          className={clsx('block h-full rounded-full', wfaIc >= 0 ? 'bg-emerald-500' : 'bg-rose-400')}
                          style={{ width: `${Math.min(100, (Math.abs(wfaIc) / maxAbsWfaIc) * 100)}%` }}
                        />
                      </span>
                      <span className={clsx(
                        'w-12 text-right font-mono text-[10px] font-black tabular-nums',
                        wfaIc >= 0.05 ? 'text-emerald-600' : wfaIc >= 0 ? 'text-amber-600' : 'text-rose-500',
                      )}>
                        {wfaIc.toFixed(4)}
                      </span>
                    </div>
                  );
                })}
              </div>

              <div className="px-4 pb-1 flex flex-wrap gap-1 text-[9px] text-slate-400">
                <span className="px-1.5 py-0.5 rounded bg-slate-50 border border-slate-100 font-mono">{meta.wfa.windows.length} 窗</span>
                <span className="px-1.5 py-0.5 rounded bg-slate-50 border border-slate-100">{meta.wfa.strategy === 'rolling' ? '滚动窗口' : '扩张窗口'}</span>
                <span className="px-1.5 py-0.5 rounded bg-slate-50 border border-slate-100">模型 {meta.wfa.model_type}</span>
              </div>

              <WfaInterpretation wfa={meta.wfa} />
            </DetailSection>
      )}
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

  return (
    <div className="h-[calc(var(--app-h)-340px)] overflow-y-auto custom-scrollbar px-1 pt-2 pb-2">
      <TrainingRunDetailTabs
        runId={model.source_run_id}
        meta={{
          display_name: modelDisplayName(model),
          created_at: model.created_at,
        }}
        detail={effectiveRunData ?? null}
        loading={isLoading}
        compact
      />
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
