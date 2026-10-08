/**
 * FactorDetailDrawer — 因子详情右侧抽屉（替代旧的 HoverCard 悬浮卡）。
 *
 * 全指标一律走 metricRegistry.formatMetricValue：缺失 → `—`，真 0 显示 0；
 * 有方向的 IC/收益族红涨绿跌，MDD/换手/RRE/PFS 中性单色。
 * 抽屉只读展示 + 三个动作出口（回测 / 看图表 / 物化），动作状态由宿主页注入。
 */

import React, { useEffect, useMemo, useState } from 'react';
import { Check, Copy, FlaskConical, Loader2, PlayCircle, X, Zap } from 'lucide-react';
import type { Factor } from '../types-v2';
import type { BacktestRunEntry } from '../context-v2/RunQueueContext';
import { formatMetricValue, getMetricDescriptor } from '../services-v2/metricRegistry';
import { cn, getQualityBadgeClass, metricToneClass } from '../utils-v2';
import { materializationChip } from './FactorTable';

const MARKET_LABELS: Record<string, string> = {
  a_share: 'A股',
  crypto: '加密货币',
  hong_kong: '港股',
  us_stock: '美股',
  futures: '期货',
};

const QUALITY_LABELS: Record<string, string> = { high: '高质量', medium: '中等', low: '低质量', unknown: '质量未知（IC 缺失）' };

interface MetricRow {
  key: string;
  get: (f: Factor) => number | undefined;
  toned?: boolean;
}

const METRIC_GROUPS: Array<{ title: string; rows: MetricRow[] }> = [
  {
    title: '预测能力',
    rows: [
      { key: 'ic', get: (f) => f.ic, toned: true },
      { key: 'rank_ic', get: (f) => f.rankIc, toned: true },
      { key: 'icir', get: (f) => f.icir, toned: true },
      { key: 'rank_icir', get: (f) => f.rankIcir, toned: true },
      { key: 'n_obs', get: (f) => f.nObs },
    ],
  },
  {
    title: '稳健性',
    rows: [
      { key: 'rre', get: (f) => f.rre },
      { key: 'quality.pfs', get: (f) => f.pfsQuality?.pfs },
      { key: 'quality.pfs_gauss', get: (f) => f.pfsQuality?.pfsGauss },
      { key: 'quality.pfs_t', get: (f) => f.pfsQuality?.pfsT },
    ],
  },
  {
    title: '交易属性（研究口径，双边 0.2%）',
    rows: [
      { key: 'turnover_daily', get: (f) => f.turnoverDaily },
      { key: 'ann_turnover', get: (f) => f.annTurnover },
      { key: 'ann_return_net', get: (f) => f.annReturnNet, toned: true },
      { key: 'sharpe_net', get: (f) => f.sharpeNet, toned: true },
      { key: 'max_drawdown_net', get: (f) => f.maxDrawdownNet },
    ],
  },
  {
    title: '组合收益（毛）',
    rows: [
      { key: 'annual_return', get: (f) => f.annualReturn, toned: true },
      { key: 'sharpe_ratio', get: (f) => f.sharpeRatio, toned: true },
      { key: 'max_drawdown', get: (f) => f.maxDrawdown },
    ],
  },
];

const BT_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '回测中',
  completed: '回测完成',
  failed: '回测失败',
  cancelled: '回测已取消',
};

export interface FactorDetailDrawerProps {
  factor: Factor | null;
  onClose: () => void;
  onBacktest: (factorId: string) => void;
  onMaterialize: (factorId: string) => void;
  onViewBacktest: (factorId: string) => void;
  btEntry?: BacktestRunEntry;
  isMaterializing: boolean;
  /** 训练（promoteByExpression 进训练特征集）；不传 = 不渲染该按钮 */
  onPromote?: (factor: Factor) => void;
  promoteState?: 'loading' | 'done' | 'error';
}

export const FactorDetailDrawer: React.FC<FactorDetailDrawerProps> = ({
  factor,
  onClose,
  onBacktest,
  onMaterialize,
  onViewBacktest,
  btEntry,
  isMaterializing,
  onPromote,
  promoteState,
}) => {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!factor) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [factor, onClose]);

  useEffect(() => {
    setCopied(false);
  }, [factor?.factorId]);

  const matChip = useMemo(
    () => (factor ? materializationChip(factor, isMaterializing) : null),
    [factor, isMaterializing],
  );

  if (!factor) return null;

  const readOnly = Boolean(factor.ownerless || factor.readOnly);

  const copyFormula = async () => {
    const text = factor.factorExpression || '';
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-modal="true" aria-label="因子详情">
      <button
        type="button"
        aria-label="关闭详情"
        className="absolute inset-0 bg-black/30 backdrop-blur-[1px]"
        onClick={onClose}
      />
      <aside className="relative z-10 flex h-full w-[440px] max-w-full flex-col border-l border-border/60 bg-card shadow-2xl">
        {/* 头部 */}
        <header className="flex items-start justify-between gap-2 border-b border-border/50 px-4 py-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="truncate text-sm font-semibold" title={factor.factorName}>
                {factor.factorName}
              </h3>
              <span
                className={cn(
                  'shrink-0 rounded border px-1 text-[10px] leading-4',
                  getQualityBadgeClass(factor.quality),
                )}
              >
                {QUALITY_LABELS[factor.quality] ?? factor.quality}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-muted-foreground">
              {factor.market && <span>市场：{MARKET_LABELS[factor.market] || factor.market}</span>}
              {factor.universe && <span>股票池：{factor.universe}</span>}
              {factor.createdAt && <span>产出：{factor.createdAt.slice(0, 19).replace('T', ' ')}</span>}
              {readOnly && (
                <span className="text-amber-600">
                  {factor.ownerless ? '历史因子（无归属），只读' : '共享因子，只读'}
                </span>
              )}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground"
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          {/* 公式 */}
          <section>
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[11px] font-medium text-muted-foreground">因子公式</span>
              <button
                type="button"
                onClick={copyFormula}
                disabled={!factor.factorExpression}
                className="inline-flex items-center gap-1 rounded border border-border/60 px-1.5 py-0.5 text-[10px] text-muted-foreground transition-colors hover:bg-muted/60 disabled:opacity-40"
              >
                {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                {copied ? '已复制' : '复制'}
              </button>
            </div>
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-md border border-border/50 bg-muted/20 px-2.5 py-2 font-mono text-[11px] leading-relaxed">
              {factor.factorExpression || '—（无公式）'}
            </pre>
            {factor.factorDescription && (
              <p className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
                {factor.factorDescription}
              </p>
            )}
          </section>

          {/* 指标 */}
          {METRIC_GROUPS.map((group) => (
            <section key={group.title} className="mt-4">
              <div className="mb-1 text-[11px] font-medium text-muted-foreground">{group.title}</div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1 rounded-md border border-border/50 bg-background/40 px-2.5 py-2">
                {group.rows.map((row) => {
                  const descriptor = getMetricDescriptor(row.key);
                  const value = row.get(factor);
                  return (
                    <div key={row.key} className="flex items-baseline justify-between gap-2" title={descriptor?.description}>
                      <span className="truncate text-[10px] text-muted-foreground">
                        {descriptor?.label ?? row.key}
                      </span>
                      <span
                        className={cn(
                          'font-mono text-[11px] tabular-nums',
                          value == null && 'text-muted-foreground/50',
                          row.toned && metricToneClass(value),
                        )}
                      >
                        {formatMetricValue(row.key, value)}
                      </span>
                    </div>
                  );
                })}
              </div>
            </section>
          ))}

          {/* 物化状态 */}
          <section className="mt-4">
            <div className="mb-1 text-[11px] font-medium text-muted-foreground">物化（训练库）</div>
            <div className="rounded-md border border-border/50 bg-background/40 px-2.5 py-2 text-[11px]">
              {matChip ? (
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <span className={cn('rounded border px-1 text-[10px] leading-4', matChip.className)}>
                      {matChip.text}
                    </span>
                    {factor.materialization?.column && (
                      <span className="font-mono text-muted-foreground">列：{factor.materialization.column}</span>
                    )}
                  </div>
                  {factor.materialization?.corr != null && (
                    <div className="text-muted-foreground">
                      与库内最高相关：|ρ|={factor.materialization.corr.toFixed(4)}
                      {factor.materialization.corrAgainst ? `（${factor.materialization.corrAgainst}）` : ''}
                    </div>
                  )}
                  {factor.materialization?.error && (
                    <div className="text-rose-500">{factor.materialization.error}</div>
                  )}
                  {factor.materialization?.at && (
                    <div className="text-muted-foreground">最近裁决：{factor.materialization.at}</div>
                  )}
                </div>
              ) : (
                <span className="text-muted-foreground">尚未物化——可选中后「物化选中」，或在下方物化此因子。</span>
              )}
            </div>
          </section>

          {/* 回测行状态 */}
          {btEntry && btEntry.status !== 'idle' && (
            <section className="mt-4">
              <div className="mb-1 text-[11px] font-medium text-muted-foreground">回测</div>
              <div className="rounded-md border border-border/50 bg-background/40 px-2.5 py-2 text-[11px]">
                <span>{BT_STATUS_LABELS[btEntry.status] ?? btEntry.status}</span>
                {btEntry.error && <div className="mt-1 text-rose-500">{btEntry.error}</div>}
              </div>
            </section>
          )}
        </div>

        {/* 动作 */}
        <footer className="flex items-center gap-2 border-t border-border/50 px-4 py-3">
          {btEntry?.status === 'completed' ? (
            <button
              type="button"
              onClick={() => onViewBacktest(factor.factorId)}
              className="inline-flex items-center gap-1 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1.5 text-xs text-emerald-600 transition-colors hover:bg-emerald-500/20"
            >
              <PlayCircle className="h-3.5 w-3.5" /> 看图表
            </button>
          ) : (
            <button
              type="button"
              onClick={() => onBacktest(factor.factorId)}
              disabled={readOnly || btEntry?.status === 'running' || btEntry?.status === 'queued'}
              className="inline-flex items-center gap-1 rounded-md border border-border/60 px-2.5 py-1.5 text-xs text-foreground/80 transition-colors hover:bg-muted/60 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <PlayCircle className="h-3.5 w-3.5" /> 回测此因子
            </button>
          )}
          {!readOnly && (
            <button
              type="button"
              onClick={() => onMaterialize(factor.factorId)}
              disabled={isMaterializing}
              className="inline-flex items-center gap-1 rounded-md border border-primary/40 bg-primary/10 px-2.5 py-1.5 text-xs text-primary transition-colors hover:bg-primary/20 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <FlaskConical className="h-3.5 w-3.5" /> 物化此因子
            </button>
          )}
          {!readOnly && onPromote && promoteState !== 'done' && (
            <button
              type="button"
              onClick={() => onPromote(factor)}
              disabled={promoteState === 'loading' || !factor.factorExpression}
              title="将此因子表达式加入训练特征集"
              className="inline-flex items-center gap-1 rounded-md border border-purple-500/30 bg-purple-500/10 px-2.5 py-1.5 text-xs text-purple-600 transition-colors hover:bg-purple-500/20 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {promoteState === 'loading' ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Zap className="h-3.5 w-3.5" />
              )}
              {promoteState === 'error' ? '训练失败，重试' : '加入训练'}
            </button>
          )}
          {promoteState === 'done' && (
            <span className="inline-flex items-center gap-1 text-xs text-purple-600">
              <Check className="h-3.5 w-3.5" /> 已加入训练
            </span>
          )}
          <button
            type="button"
            onClick={onClose}
            className="ml-auto rounded-md px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted/60"
          >
            关闭
          </button>
        </footer>
      </aside>
    </div>
  );
};

export default FactorDetailDrawer;
