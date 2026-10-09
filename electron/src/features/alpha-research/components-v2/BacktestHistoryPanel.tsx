/**
 * 回测历史面板（用户原话：「每次单个因子回测的历史数据，后面好对比」）。
 *
 * 后端 rd_agent_factor_backtests 一次运行一行，永不覆盖；本面板：
 * - 挂载即拉该因子的历史（taskStatus 变化时重取——运行→终态刷新出刚收口的那行）；
 * - 列表：时间/耗时/状态/池/源/区间 + 关键指标，缺失一律「—」（不补 0）；
 * - 勾选 2–5 条做并排对比：指标行 × 运行列，每行高亮最优值
 *   （收益/IC 类取大、回撤类取小；只有 ≥2 个有效值才判最优）。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { History, RefreshCw, AlertCircle, GitCompare } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from './ui/Card';
import { Button } from './ui/Button';
import { listFactorBacktests, UNIVERSE_LABELS } from '../services-v2/api';
import type { BacktestHistoryRun, BacktestMetricKey } from '../services-v2/api';
import { cn, formatNumber, formatPercent, formatShortTime } from '../utils-v2';

/** 同时对比的运行数上限（再多列宽就不可读了） */
const MAX_COMPARE = 5;

/**
 * 回测运行状态 → 徽标样式。与挖掘任务状态词表同形但**刻意独立**——
 * 跨域共用会在任一侧加状态时连坐（utils-v2 的 TASK_STATUS_META 只服务挖掘任务）。
 */
const RUN_STATUS_META: Record<string, { label: string; cls: string }> = {
  running: { label: '运行中', cls: 'bg-indigo-500/10 text-indigo-400 border-indigo-500/30' },
  completed: { label: '已完成', cls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/30' },
  failed: { label: '失败', cls: 'bg-rose-500/10 text-rose-500 border-rose-500/30' },
  cancelled: { label: '已取消', cls: 'bg-amber-500/10 text-amber-500 border-amber-500/30' },
};

function runStatusMeta(status: string) {
  return (
    RUN_STATUS_META[status] ?? {
      label: status || '未知',
      cls: 'bg-slate-500/10 text-slate-400 border-slate-500/30',
    }
  );
}

function dataSourceLabel(dataSource: string | null): string {
  if (dataSource === 'qlib_bin') return 'Qlib';
  if (dataSource === 'h5') return 'H5';
  return dataSource ?? '—';
}

function universeLabel(universe: string | null): string {
  if (!universe) return '—';
  return UNIVERSE_LABELS[universe as keyof typeof UNIVERSE_LABELS] ?? universe;
}

/** 运行耗时（秒）；未收口返回 null（缺数据诚实留白，不伪造） */
function elapsedSeconds(run: BacktestHistoryRun): number | null {
  if (!run.finishedAt || !run.startedAt) return null;
  const ms = new Date(run.finishedAt).getTime() - new Date(run.startedAt).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  return Math.round(ms / 1000);
}

function formatElapsed(seconds: number | null): string {
  if (seconds == null) return '—';
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s ? `${m}m${String(s).padStart(2, '0')}s` : `${m}m`;
}

interface MetricSpec {
  key: BacktestMetricKey;
  label: string;
  fmt: (value: number) => string;
  /** 对比时哪边更优；缺省不判最优（换手/天数这类无明显方向） */
  direction?: 'higher' | 'lower';
}

/** 列表里的关键指标（概览列） */
const LIST_METRICS: MetricSpec[] = [
  { key: 'ic', label: 'IC', fmt: (v) => formatNumber(v, 4) },
  { key: 'icir', label: 'ICIR', fmt: (v) => formatNumber(v, 4) },
  { key: 'annualReturn', label: '年化（毛）', fmt: (v) => formatPercent(v) },
  { key: 'sharpeRatio', label: '夏普', fmt: (v) => formatNumber(v, 2) },
  { key: 'maxDrawdown', label: '回撤（毛）', fmt: (v) => formatPercent(v) },
];

/** 对比表的全指标行（与回测结果卡同词表） */
const COMPARE_ROWS: MetricSpec[] = [
  { key: 'ic', label: 'IC', fmt: (v) => formatNumber(v, 4), direction: 'higher' },
  { key: 'icir', label: 'ICIR', fmt: (v) => formatNumber(v, 4), direction: 'higher' },
  { key: 'rankIc', label: 'Rank IC', fmt: (v) => formatNumber(v, 4), direction: 'higher' },
  { key: 'rankIcir', label: 'Rank ICIR', fmt: (v) => formatNumber(v, 4), direction: 'higher' },
  { key: 'annualReturn', label: '年化收益（毛）', fmt: (v) => formatPercent(v), direction: 'higher' },
  { key: 'sharpeRatio', label: '夏普比率', fmt: (v) => formatNumber(v, 2), direction: 'higher' },
  { key: 'maxDrawdown', label: '最大回撤（毛）', fmt: (v) => formatPercent(v), direction: 'lower' },
  { key: 'rre', label: 'RRE 排序可靠度', fmt: (v) => formatNumber(v, 4), direction: 'higher' },
  { key: 'pfs', label: 'PFS 扰动保真度', fmt: (v) => formatNumber(v, 4), direction: 'higher' },
  { key: 'turnoverDaily', label: '日均换手', fmt: (v) => formatNumber(v, 4) },
  { key: 'annTurnover', label: '年化换手', fmt: (v) => formatNumber(v, 2) },
  { key: 'annReturnNet', label: '扣费年化收益', fmt: (v) => formatPercent(v), direction: 'higher' },
  { key: 'sharpeNet', label: '扣费夏普', fmt: (v) => formatNumber(v, 2), direction: 'higher' },
  { key: 'maxDrawdownNet', label: '扣费最大回撤', fmt: (v) => formatPercent(v), direction: 'lower' },
  { key: 'nObs', label: '有效天数', fmt: (v) => formatNumber(v, 0) },
];

function metricValue(run: BacktestHistoryRun, spec: MetricSpec): string {
  const value = run.metrics[spec.key];
  return typeof value === 'number' && Number.isFinite(value) ? spec.fmt(value) : '—';
}

/** 每行最优值归属：只有 ≥2 个有效值才判（单值不称「最优」） */
function bestRunIdsForRow(
  spec: MetricSpec,
  runs: BacktestHistoryRun[],
): string | null {
  if (!spec.direction) return null;
  const defined = runs.filter((r) => {
    const v = r.metrics[spec.key];
    return typeof v === 'number' && Number.isFinite(v);
  });
  if (defined.length < 2) return null;
  let best = defined[0];
  for (const r of defined.slice(1)) {
    const a = best.metrics[spec.key] as number;
    const b = r.metrics[spec.key] as number;
    if (spec.direction === 'higher' ? b > a : b < a) best = r;
  }
  return best.runId;
}

interface BacktestHistoryPanelProps {
  /** 因子 id（=回测任务句柄）；为空不渲染 */
  factorId: string | null;
  /** 任务状态：变化即重取（运行→终态时刷新出刚收口的那行） */
  taskStatus?: string | null;
}

export const BacktestHistoryPanel: React.FC<BacktestHistoryPanelProps> = ({
  factorId,
  taskStatus,
}) => {
  const [runs, setRuns] = useState<BacktestHistoryRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedRunIds, setSelectedRunIds] = useState<string[]>([]);
  /** 请求序号：只有最新一次请求的响应允许写状态（连点刷新/换因子时防陈旧覆盖） */
  const loadSeqRef = useRef(0);

  const load = useCallback(async () => {
    if (!factorId) return;
    const seq = ++loadSeqRef.current;
    setLoading(true);
    try {
      const resp = await listFactorBacktests(factorId, 20);
      if (seq !== loadSeqRef.current) return; // 过期响应：整份丢弃
      if (resp.success && resp.data) {
        const next = resp.data.runs;
        setRuns(next);
        // 勾选随可见集收敛：滑出 20 条窗口的旧勾选必须清掉——否则对比区
        // 没数据可渲染（消失），而复选框又因达上限全禁用，界面被永久锁死。
        setSelectedRunIds((prev) =>
          prev.filter((id) => next.some((r) => r.runId === id)),
        );
        setError(null);
      } else {
        setError(resp.error ?? '查询回测历史失败');
      }
    } catch {
      if (seq === loadSeqRef.current) setError('查询回测历史失败');
    } finally {
      if (seq === loadSeqRef.current) setLoading(false);
    }
  }, [factorId]);

  useEffect(() => {
    void load();
  }, [load, taskStatus]);

  const toggleRun = (runId: string) => {
    setSelectedRunIds((prev) => {
      if (prev.includes(runId)) return prev.filter((id) => id !== runId);
      if (prev.length >= MAX_COMPARE) return prev;
      return [...prev, runId];
    });
  };

  // 对比列按时间升序（旧→新读趋势）；filter 已返回新数组，无需再 slice
  const selectedRuns = useMemo(
    () =>
      runs
        .filter((r) => selectedRunIds.includes(r.runId))
        .sort((a, b) => Date.parse(a.startedAt) - Date.parse(b.startedAt)),
    [runs, selectedRunIds],
  );

  if (!factorId) return null;

  // 上限判据用「可见且被勾选」的条数：滑出窗口的勾选不计入，界面与
  // toggle 守卫（对 selectedRunIds）在收敛后一致
  const atCap = selectedRuns.length >= MAX_COMPARE;

  return (
    <Card className="glass card-hover" data-testid="backtest-history">
      <CardHeader>
        <CardTitle className="flex items-center justify-between">
          <span className="flex items-center gap-2">
            <History className="h-5 w-5" />
            回测历史
            {runs.length > 0 && (
              <span className="text-xs text-muted-foreground font-normal">
                最近 {runs.length} 次记录 · 勾选 2–{MAX_COMPARE} 条对比
              </span>
            )}
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void load()}
            disabled={loading}
            title="刷新回测历史"
            className="px-2.5"
          >
            <RefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} />
          </Button>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {error && (
          <div className="flex items-center gap-2 text-sm text-destructive">
            <AlertCircle className="h-4 w-4 flex-shrink-0" />
            {error}
          </div>
        )}

        {runs.length === 0 && !loading && !error && (
          <p className="text-sm text-muted-foreground py-2">
            暂无回测历史——每次发起回测都会在这里留一行记录，供前后对比。
          </p>
        )}

        {runs.length === 0 && loading && (
          <p className="text-sm text-muted-foreground py-2">加载中…</p>
        )}

        {runs.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-muted-foreground border-b border-border/50">
                  <th className="py-2 pr-2 w-8" aria-label="勾选对比" />
                  <th className="py-2 pr-3 text-left font-medium">开始时间</th>
                  <th className="py-2 pr-3 text-left font-medium">状态</th>
                  <th className="py-2 pr-3 text-left font-medium">股票池 · 数据源</th>
                  <th className="py-2 pr-3 text-left font-medium">回测区间</th>
                  {LIST_METRICS.map((m) => (
                    <th key={m.key} className="py-2 pr-3 text-right font-medium">
                      {m.label}
                    </th>
                  ))}
                  <th className="py-2 text-right font-medium">耗时</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const meta = runStatusMeta(run.status);
                  const elapsed = elapsedSeconds(run);
                  return (
                    <tr
                      key={run.runId}
                      className={cn(
                        'border-b border-border/30 transition-colors',
                        selectedRunIds.includes(run.runId)
                          ? 'bg-primary/5'
                          : 'hover:bg-muted/20',
                      )}
                    >
                      <td className="py-2 pr-2">
                        <input
                          type="checkbox"
                          checked={selectedRunIds.includes(run.runId)}
                          disabled={!selectedRunIds.includes(run.runId) && atCap}
                          onChange={() => toggleRun(run.runId)}
                          aria-label={`选择 ${formatShortTime(run.startedAt)} 的回测`}
                          title={
                            atCap && !selectedRunIds.includes(run.runId)
                              ? `最多对比 ${MAX_COMPARE} 次`
                              : '加入对比'
                          }
                          className="h-3.5 w-3.5 accent-primary cursor-pointer disabled:cursor-not-allowed"
                        />
                      </td>
                      <td className="py-2 pr-3 whitespace-nowrap">
                        {formatShortTime(run.startedAt)}
                      </td>
                      <td className="py-2 pr-3">
                        <span
                          className={cn(
                            'inline-flex items-center rounded-md border px-1.5 py-0.5 text-[11px] font-medium',
                            meta.cls,
                          )}
                        >
                          {meta.label}
                        </span>
                        {run.error && (
                          <div
                            className="mt-1 max-w-[220px] truncate text-[11px] text-destructive"
                            title={run.error}
                          >
                            {run.error}
                          </div>
                        )}
                      </td>
                      <td className="py-2 pr-3 whitespace-nowrap text-muted-foreground">
                        {universeLabel(run.universe)} · {dataSourceLabel(run.dataSource)}
                      </td>
                      <td className="py-2 pr-3 whitespace-nowrap text-muted-foreground">
                        {run.dateRange ?? '—'}
                      </td>
                      {LIST_METRICS.map((m) => (
                        <td key={m.key} className="py-2 pr-3 text-right font-mono">
                          {metricValue(run, m)}
                        </td>
                      ))}
                      <td className="py-2 text-right font-mono text-muted-foreground">
                        {run.status === 'running' ? '进行中' : formatElapsed(elapsed)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {atCap && (
          <p className="text-xs text-muted-foreground">
            最多对比 {MAX_COMPARE} 次，先取消一个再选。
          </p>
        )}

        {selectedRuns.length >= 2 && (
          <div className="pt-3 border-t border-border/50" data-testid="backtest-compare">
            <h4 className="mb-3 flex items-center gap-2 text-sm font-medium">
              <GitCompare className="h-4 w-4 text-primary" />
              对比（{selectedRuns.length} 次 · 按时间先后排列，最优值已高亮）
            </h4>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border/50 text-xs">
                    <th className="py-2 pr-4 text-left font-medium text-muted-foreground">
                      指标
                    </th>
                    {selectedRuns.map((run) => (
                      <th key={run.runId} className="py-2 pr-4 text-left font-medium">
                        <div className="whitespace-nowrap">{formatShortTime(run.startedAt)}</div>
                        <div className="font-normal text-muted-foreground whitespace-nowrap">
                          {universeLabel(run.universe)} · {dataSourceLabel(run.dataSource)}
                        </div>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  <tr className="border-b border-border/30">
                    <td className="py-2 pr-4 text-muted-foreground">状态</td>
                    {selectedRuns.map((run) => {
                      const meta = runStatusMeta(run.status);
                      return (
                        <td key={run.runId} className="py-2 pr-4">
                          <span
                            className={cn(
                              'inline-flex items-center rounded-md border px-1.5 py-0.5 text-[11px] font-medium',
                              meta.cls,
                            )}
                          >
                            {meta.label}
                          </span>
                        </td>
                      );
                    })}
                  </tr>
                  <tr className="border-b border-border/30">
                    <td className="py-2 pr-4 text-muted-foreground">回测区间</td>
                    {selectedRuns.map((run) => (
                      <td key={run.runId} className="py-2 pr-4 whitespace-nowrap">
                        {run.dateRange ?? '—'}
                      </td>
                    ))}
                  </tr>
                  {COMPARE_ROWS.map((spec) => {
                    const bestRunId = bestRunIdsForRow(spec, selectedRuns);
                    return (
                      <tr key={spec.key} className="border-b border-border/30">
                        <td className="py-2 pr-4 text-muted-foreground">{spec.label}</td>
                        {selectedRuns.map((run) => {
                          const isBest = bestRunId === run.runId;
                          return (
                            <td
                              key={run.runId}
                              data-best={isBest ? '1' : undefined}
                              title={isBest ? '最优' : undefined}
                              className={cn(
                                'py-2 pr-4 font-mono',
                                isBest && 'text-primary font-semibold',
                              )}
                            >
                              {metricValue(run, spec)}
                            </td>
                          );
                        })}
                      </tr>
                    );
                  })}
                  <tr>
                    <td className="py-2 pr-4 text-muted-foreground">耗时</td>
                    {selectedRuns.map((run) => (
                      <td key={run.runId} className="py-2 pr-4 font-mono">
                        {run.status === 'running'
                          ? '进行中'
                          : formatElapsed(elapsedSeconds(run))}
                      </td>
                    ))}
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
};
