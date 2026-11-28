import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { clsx } from 'clsx';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { Switch } from 'antd';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip as ReTooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { modelTrainingService } from '../services/modelTrainingService';
import { SignalEvalTopNTable } from './SignalEvalTopNTable';
import type {
  SignalEvalAdmission,
  SignalEvalHistoryRow,
  SignalEvalReport,
  SignalEvalState,
} from '../services/modelSignalEvalTypes';

/** 测试集实盘力：滚动 3 个月的截面排序 + TopN 长多体检。
 *
 * 数据来自「推理过的 test 切分」，窗口随推理与行情自动前移；首次打开会后台重算，
 * 计算中保留旧统计并在顶部显示进度条。
 *
 * 开关「排除黑名单」：TopN 选股按后台管理·交易核心的黑名单逐只跳过、顺延补足，
 * 即实盘候选池口径（独立缓存，首次约 1 分钟重算）。名单是当前快照、回溯整窗非 PIT，
 * IC/分位仍按全样本统计 —— 页面用徽章与 tooltip 明示，不把两种口径混着读。
 */

const STEP_LABELS: Record<string, string> = {
  window: '确定窗口',
  load: '加载行情',
  ic: '截面 IC',
  decile: '分位 / 稳定性',
  portfolio: '模拟长多',
  done: '完成',
};

const UNKNOWN_ADMISSION: SignalEvalAdmission = {
  grade: 'unknown',
  checks: {},
  reasons: ['未完成标准化评估'],
  window_end: null,
  checked_at: null,
  stale: true,
  eval_version: 0,
};

const ADMISSION_STYLE: Record<
  SignalEvalAdmission['grade'],
  { label: string; className: string }
> = {
  green: { label: '标准化评估通过', className: 'bg-emerald-100 text-emerald-700' },
  yellow: { label: '评估黄色预警', className: 'bg-amber-100 text-amber-700' },
  red: { label: '评估未通过', className: 'bg-rose-100 text-rose-600' },
  unknown: { label: '未评估', className: 'bg-slate-100 text-slate-500' },
};

function fmtPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${value > 0 ? '+' : ''}${(value * 100).toFixed(digits)}%`;
}

function fmtNum(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return value.toFixed(digits);
}

/** A 股习惯：红涨绿跌（收益类数字专用；通过/失败徽章仍用绿/红语义）。 */
function returnTone(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'text-slate-400';
  return value > 0 ? 'text-rose-500' : value < 0 ? 'text-emerald-600' : 'text-slate-500';
}

const Flag: React.FC<{ ok: boolean; label: string }> = ({ ok, label }) => (
  <span
    className={clsx(
      'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[9px] font-black tracking-wide',
      ok ? 'bg-emerald-50 text-emerald-600' : 'bg-rose-50 text-rose-500',
    )}
  >
    <span className="text-[10px] leading-none">{ok ? '✓' : '✕'}</span>
    {label}
  </span>
);

const Kpi: React.FC<{
  label: string;
  value: string;
  sub?: React.ReactNode;
  tone?: 'good' | 'bad' | 'neutral';
}> = ({ label, value, sub, tone = 'neutral' }) => (
  <div className="relative overflow-hidden rounded-xl border border-slate-200/70 bg-white px-3.5 py-3">
    <div className="text-[9px] font-bold tracking-wider text-slate-400">{label}</div>
    <div
      className={clsx(
        'mt-1 font-mono text-[23px] font-black leading-none tabular-nums',
        tone === 'good' ? 'text-emerald-600' : tone === 'bad' ? 'text-rose-500' : 'text-slate-800',
      )}
    >
      {value}
    </div>
    {sub ? <div className="mt-1.5 text-[9px] leading-relaxed text-slate-400">{sub}</div> : null}
  </div>
);

const PendingState: React.FC<{
  state: SignalEvalState | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}> = ({ state, loading, error, onRefresh }) => {
  if (loading) {
    return (
      <div className="flex items-center gap-2 px-1 py-8 text-[11px] text-slate-400">
        <RefreshCw size={13} className="animate-spin" />
        正在读取统计…
      </div>
    );
  }
  if (error) {
    return (
      <div className="flex items-center justify-between gap-3 rounded-xl border border-rose-100 bg-rose-50/40 px-4 py-4">
        <div className="flex items-center gap-2 text-[11px] text-rose-600">
          <AlertTriangle size={14} />
          统计读取失败：{error}
        </div>
        <button
          type="button"
          onClick={onRefresh}
          className="shrink-0 rounded-md border border-rose-200 px-2.5 py-1.5 text-[10px] font-bold text-rose-600 hover:bg-rose-50"
        >
          重试
        </button>
      </div>
    );
  }
  if (state?.status === 'unavailable') {
    return (
      <div className="rounded-xl border border-slate-100 bg-slate-50/60 px-4 py-4 text-[11px] text-slate-500">
        {state.reason}
      </div>
    );
  }
  const running = state?.status === 'running';
  const hint =
    !running && state && (state.status === 'missing' || state.status === 'stale')
      ? state.reason
      : '';
  return (
    <div className="rounded-xl border border-indigo-100 bg-indigo-50/30 px-4 py-5">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[11px] font-black text-slate-700">
            {running ? '正在统计测试集实盘力…' : '还没有生成过滚动统计'}
          </div>
          <div className="mt-1 text-[10px] text-slate-400">
            {running
              ? `${STEP_LABELS[state.step] ?? (state.step || '准备中')} · ${state.progress}%`
              : hint || '首次统计需读取 test 推理与行情，约 1 分钟；完成后随推理自动更新。'}
          </div>
        </div>
        {!running ? (
          <button
            type="button"
            onClick={onRefresh}
            className="shrink-0 rounded-md bg-indigo-600 px-3 py-1.5 text-[10px] font-black text-white hover:bg-indigo-500"
          >
            开始统计
          </button>
        ) : null}
      </div>
      {running ? (
        <div className="mt-3 h-1 overflow-hidden rounded-full bg-indigo-100">
          <div
            className="h-full rounded-full bg-indigo-500 transition-all duration-500"
            style={{ width: `${Math.max(4, state.progress)}%` }}
          />
        </div>
      ) : null}
    </div>
  );
};

export interface SignalEvalPanelProps {
  modelId: string;
}

export const SignalEvalPanel: React.FC<SignalEvalPanelProps> = ({ modelId }) => {
  const [state, setState] = useState<SignalEvalState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 实盘口径开关：TopN 选股逐只跳过交易黑名单并顺延补足（候选池里本来就没有这些票）。
  // 默认关 = 模型原始口径；打开走独立缓存（ui_63d_excl），首次需要约 1 分钟重算。
  const [excludeBlacklist, setExcludeBlacklist] = useState(false);
  const autoAsked = useRef(false);

  useEffect(() => {
    autoAsked.current = false;
    setState(null);
    setError(null);
    setLoading(true);
  }, [modelId, excludeBlacklist]);

  const load = useCallback(async () => {
    try {
      const next = await modelTrainingService.getModelSignalEval(modelId, {
        excludeBlacklist,
      });
      setState(next);
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '请求失败');
    } finally {
      setLoading(false);
    }
  }, [modelId, excludeBlacklist]);

  const triggerRefresh = useCallback(async () => {
    autoAsked.current = true;
    setState((prev) => {
      const cached =
        prev?.status === 'ready'
          ? prev.report
          : prev?.status === 'stale'
            ? prev.report
            : prev?.status === 'running'
              ? prev.cached_report ?? null
              : null;
      return {
        status: 'running',
        progress: 1,
        step: 'window',
        message: '已提交刷新',
        cached_report: cached,
        admission:
          prev && 'admission' in prev ? prev.admission : UNKNOWN_ADMISSION,
        history: prev && 'history' in prev ? prev.history : [],
      };
    });
    try {
      await modelTrainingService.refreshModelSignalEval(modelId, { excludeBlacklist });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '刷新失败');
      return;
    }
    await load();
  }, [excludeBlacklist, load, modelId]);

  useEffect(() => {
    void load();
  }, [load]);

  // 首次进入：缓存缺失/过期时自动发起一次后台重算（每个模型只自动发一次）
  useEffect(() => {
    if (!state || autoAsked.current) return;
    if (state.status === 'missing' || state.status === 'stale') {
      void triggerRefresh();
    }
  }, [state, triggerRefresh]);

  // 计算中轮询
  useEffect(() => {
    if (state?.status !== 'running') return;
    const timer = window.setInterval(() => {
      void load();
    }, 2500);
    return () => window.clearInterval(timer);
  }, [state?.status, load]);

  const report: SignalEvalReport | null =
    state?.status === 'ready'
      ? state.report
      : state?.status === 'stale'
        ? state.report
        : state?.status === 'running'
          ? state.cached_report ?? null
          : null;
  const isRunning = state?.status === 'running';
  const isStale = state?.status === 'stale';
  const progress = state?.status === 'running' ? state.progress : 0;
  const admission: SignalEvalAdmission =
    state && 'admission' in state
      ? state.admission
      : report?.admission ?? UNKNOWN_ADMISSION;
  const history = useMemo<SignalEvalHistoryRow[]>(
    () => (state && 'history' in state ? state.history : []),
    [state],
  );
  const admissionStyle = ADMISSION_STYLE[admission.grade] ?? ADMISSION_STYLE.unknown;
  const coverage = report?.coverage;
  const coverageWarn =
    coverage && (coverage.days_below_50 > 0 || (coverage.avg ?? 1) < 0.8);

  const weekly = useMemo(
    () =>
      (report?.series.weekly_ic ?? []).map((row) => ({
        week: row.week,
        ic: row.ic,
        ic_neutral: row.ic_neutral,
      })),
    [report],
  );
  const quantiles = useMemo(
    () =>
      (report?.metrics.quantiles.buckets ?? []).map((value, index) => ({
        q: `Q${index + 1}`,
        ret: value,
      })),
    [report],
  );
  const historySeries = useMemo(
    () =>
      history.map((row) => ({
        window_end: row.window_end?.slice(5) ?? '',
        rank_ic: row.rank_ic,
        neutral_ic: row.neutral_ic,
      })),
    [history],
  );
  const overall = useMemo(() => {
    if (!report) return { label: '—', tone: 'neutral' as const };
    const { ranking_pass, neutral_pass, long_pass } = report.verdict;
    if (ranking_pass && neutral_pass) {
      return long_pass
        ? { label: '排序 + 长多都过线', tone: 'good' as const }
        : { label: '排序有效 · 长多未过线', tone: 'warn' as const };
    }
    return { label: '排序证据不足', tone: 'bad' as const };
  }, [report]);

  const headerMeta = report
    ? `${report.window.start} ~ ${report.window.end} · ${report.window.signal_days} 个交易日 · 推理至 ${report.window.pred_end}`
    : '滚动 3 个月 · 仅统计推理过的测试集';

  return (
    <section className="rounded-2xl border border-slate-200/70 bg-white overflow-hidden">
      <header className="flex flex-wrap items-center justify-between gap-2 px-5 py-3 border-b border-slate-100">
        <div className="flex min-w-0 items-center gap-2">
          <span className="h-3 w-0.5 shrink-0 rounded-full bg-indigo-500" />
          <h3 className="m-0 text-[11px] font-black tracking-wider text-slate-600">测试集实盘力</h3>
          <span className="rounded-md bg-indigo-50 px-1.5 py-0.5 text-[9px] font-bold text-indigo-600">
            {report?.window.label ?? '滚动 3 个月'}
          </span>
          <span
            className={clsx(
              'rounded-md px-1.5 py-0.5 text-[9px] font-black',
              admissionStyle.className,
            )}
            title={admission.reasons.length ? admission.reasons.join(' · ') : undefined}
          >
            {admissionStyle.label}
          </span>
          {admission.stale && admission.grade !== 'unknown' ? (
            <span className="rounded-md bg-amber-50 px-1.5 py-0.5 text-[9px] font-bold text-amber-600">
              评估过期
            </span>
          ) : null}
          {coverageWarn ? (
            <span
              className="rounded-md bg-amber-50 px-1.5 py-0.5 text-[9px] font-bold text-amber-600"
              title={`覆盖日均 ${fmtPct(coverage?.avg ?? null, 1)}；${coverage?.days_below_50 ?? 0} 天低于 50%`}
            >
              覆盖 {fmtPct(coverage?.avg ?? null, 0).replace('+', '')}
            </span>
          ) : null}
          {isRunning ? (
            <span className="rounded-md bg-blue-50 px-1.5 py-0.5 text-[9px] font-bold text-blue-600">
              刷新中 {Math.max(1, progress)}%
            </span>
          ) : null}
          {isStale && !isRunning ? (
            <span className="rounded-md bg-amber-50 px-1.5 py-0.5 text-[9px] font-bold text-amber-600">
              有新推理/行情，待刷新
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-2 text-[9px] text-slate-400">
          <label
            className="flex shrink-0 cursor-pointer items-center gap-1.5 text-[10px] font-bold text-slate-500"
            title="TopN 选股逐只跳过交易黑名单并顺延补足（实盘候选池口径）。名单只有当前快照，回溯整窗非 PIT；IC/分位仍按全样本统计。"
          >
            <Switch
              size="small"
              checked={excludeBlacklist}
              disabled={isRunning}
              onChange={(checked) => setExcludeBlacklist(checked)}
            />
            排除黑名单
          </label>
          <span className="tabular-nums">{headerMeta}</span>
          <button
            type="button"
            onClick={() => void triggerRefresh()}
            disabled={isRunning || state?.status === 'unavailable'}
            title="重新统计"
            className="inline-flex h-6 w-6 items-center justify-center rounded-md border border-slate-200 text-slate-400 transition-colors hover:border-indigo-200 hover:text-indigo-600 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <RefreshCw size={11} className={isRunning ? 'animate-spin' : ''} />
          </button>
        </div>
      </header>

      {isRunning ? (
        <div className="h-0.5 bg-slate-100">
          <div
            className="h-full bg-indigo-500 transition-all duration-500"
            style={{ width: `${Math.max(3, progress)}%` }}
          />
        </div>
      ) : null}

      <div className="space-y-4 px-4 py-4">
        {!report ? (
          <PendingState
            state={state}
            loading={loading}
            error={error}
            onRefresh={() => void triggerRefresh()}
          />
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-100 bg-slate-50/50 px-3.5 py-2.5">
              <div className="flex min-w-0 items-center gap-2">
                <span
                  className={clsx(
                    'shrink-0 rounded-md px-2 py-0.5 text-[9px] font-black',
                    overall.tone === 'good'
                      ? 'bg-emerald-100 text-emerald-700'
                      : overall.tone === 'warn'
                        ? 'bg-amber-100 text-amber-700'
                        : 'bg-rose-100 text-rose-600',
                  )}
                >
                  {overall.label}
                </span>
                <span className="truncate text-[10px] leading-relaxed text-slate-500">
                  {report.verdict.summary}
                </span>
              </div>
              <span className="shrink-0 text-[9px] text-slate-400">
                统计于 {report.computed_at} · 样本 {report.sample.rows.toLocaleString()} 行 /{' '}
                {report.sample.symbols.toLocaleString()} 只
              </span>
            </div>

            {admission.reasons.length > 0 && admission.grade !== 'green' ? (
              <div className="-mt-2 flex flex-wrap items-center gap-1 text-[9px] text-slate-400">
                <span className="font-bold text-slate-500">未过项：</span>
                {admission.reasons.map((reason) => (
                  <span key={reason} className="rounded bg-slate-100 px-1.5 py-0.5">
                    {reason}
                  </span>
                ))}
              </div>
            ) : null}

            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <Kpi
                label="截面 RankIC"
                value={fmtNum(report.metrics.rank_ic.mean, 4)}
                tone={report.verdict.ranking_pass ? 'good' : 'bad'}
                sub={
                  <>
                    ICIR {fmtNum(report.metrics.rank_ic.icir)} · 胜率{' '}
                    {fmtPct(report.metrics.rank_ic.win_rate, 0).replace('+', '')} · HAC t{' '}
                    {fmtNum(report.metrics.rank_ic.hac_t)}
                  </>
                }
              />
              <Kpi
                label="中性化 RankIC"
                value={fmtNum(report.metrics.neutral_ic.mean, 4)}
                tone={report.verdict.neutral_pass ? 'good' : 'bad'}
                sub={
                  <>
                    剔除市值/行业后 · HAC t {fmtNum(report.metrics.neutral_ic.hac_t)}
                  </>
                }
              />
              <Kpi
                label="分位价差 Q10−Q1"
                value={fmtPct(report.metrics.quantiles.spread_mean)}
                tone={report.verdict.decile_pass ? 'good' : 'bad'}
                sub={
                  <>
                    单调性 {fmtNum(report.metrics.quantiles.monotonicity)} · t{' '}
                    {fmtNum(report.metrics.quantiles.spread_t)}
                  </>
                }
              />
              <Kpi
                label="Top20 近 3 月"
                value={fmtPct(report.topn.find((row) => row.n === 20)?.total_return ?? null)}
                tone={report.verdict.long_pass ? 'good' : 'bad'}
                sub={
                  <>
                    超额 {fmtPct(report.topn.find((row) => row.n === 20)?.excess ?? null)} · 回撤{' '}
                    {fmtPct(report.topn.find((row) => row.n === 20)?.mdd ?? null)}
                  </>
                }
              />
            </div>

            <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
              <div className="lg:col-span-7">
                <div className="mb-1 flex items-center justify-between">
                  <span className="text-[10px] font-black text-slate-600">周度 IC 走势</span>
                  <span className="text-[9px] text-slate-400">柱=原始 · 线=中性化</span>
                </div>
                <ResponsiveContainer width="100%" height={190}>
                  <ComposedChart data={weekly} margin={{ top: 6, right: 6, left: -16, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" vertical={false} />
                    <XAxis
                      dataKey="week"
                      tick={{ fontSize: 9, fill: '#94a3b8' }}
                      axisLine={false}
                      tickLine={false}
                      interval="preserveStartEnd"
                    />
                    <YAxis
                      tickFormatter={(value: number) => `${(value * 100).toFixed(1)}%`}
                      tick={{ fontSize: 9, fill: '#94a3b8' }}
                      axisLine={false}
                      tickLine={false}
                    />
                    <ReTooltip
                      formatter={(value: number | string, name: string) => [
                        `${(Number(value) * 100).toFixed(2)}%`,
                        name === 'ic' ? '原始 IC' : '中性化 IC',
                      ]}
                      labelFormatter={(label) => `周 ${label}`}
                      contentStyle={{ fontSize: 10, borderRadius: 8, borderColor: '#e2e8f0' }}
                    />
                    <ReferenceLine y={0} stroke="#cbd5e1" />
                    <Bar dataKey="ic" fill="#a5b4fc" radius={[2, 2, 0, 0]} maxBarSize={18} />
                    <Line
                      type="monotone"
                      dataKey="ic_neutral"
                      stroke="#4f46e5"
                      strokeWidth={1.5}
                      dot={false}
                      connectNulls
                    />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
              <div className="lg:col-span-5">
                <div className="mb-1 flex items-center justify-between">
                  <span className="text-[10px] font-black text-slate-600">分位收益</span>
                  <span className="text-[9px] text-slate-400">Q1 靠后 → Q10 靠前</span>
                </div>
                <ResponsiveContainer width="100%" height={190}>
                  <BarChart data={quantiles} margin={{ top: 6, right: 6, left: -16, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" vertical={false} />
                    <XAxis
                      dataKey="q"
                      tick={{ fontSize: 9, fill: '#94a3b8' }}
                      axisLine={false}
                      tickLine={false}
                    />
                    <YAxis
                      tickFormatter={(value: number) => `${(value * 100).toFixed(1)}%`}
                      tick={{ fontSize: 9, fill: '#94a3b8' }}
                      axisLine={false}
                      tickLine={false}
                    />
                    <ReTooltip
                      formatter={(value: number | string) => [`${(Number(value) * 100).toFixed(2)}%`, '平均收益']}
                      labelFormatter={(label) => `${label} 分位`}
                      contentStyle={{ fontSize: 10, borderRadius: 8, borderColor: '#e2e8f0' }}
                    />
                    <ReferenceLine y={0} stroke="#cbd5e1" />
                    <Bar dataKey="ret" radius={[2, 2, 0, 0]} maxBarSize={26}>
                      {quantiles.map((row, index) => (
                        <Cell
                          key={row.q}
                          fill={
                            row.ret === null
                              ? '#e2e8f0'
                              : row.ret >= 0
                                ? index === quantiles.length - 1
                                  ? '#fb7185'
                                  : '#fda4af'
                                : '#86efac'
                          }
                        />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>

            <div>
              <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
                <span className="flex items-center gap-1.5 text-[10px] font-black text-slate-600">
                  TopN 长多（可执行口径）
                  <span
                    className="rounded bg-emerald-50 px-1.5 py-0.5 text-[8px] font-black text-emerald-700"
                    title={
                      report.execution
                        ? [
                            `${report.execution.entry}买 → ${report.execution.exit}卖`,
                            `${report.execution.cost_bps_per_side}bp/边（${report.execution.cost_note}）`,
                            report.execution.limit_up_entry_excluded ? '开盘涨停买不进剔除' : '',
                            report.execution.limit_down_exit_extended ? '跌停卖不出顺延' : '',
                            report.execution.st_pit_excluded ? 'PIT ST 剔除' : '含 ST',
                            report.execution.adv_floor_yuan
                              ? `20日ADV≥${(report.execution.adv_floor_yuan / 1e4).toFixed(0)}万`
                              : '',
                            report.execution.missing_price_skipped ? '停牌/缺价跳过' : '',
                          ]
                            .filter(Boolean)
                            .join(' · ')
                        : 'T+1 开盘/收盘买、持有期末同价卖、涨跌停/ST/流动性过滤'
                    }
                  >
                    严格实盘
                  </span>
                  {report.blacklist?.applied ? (
                    <span
                      className="rounded bg-rose-50 px-1.5 py-0.5 text-[8px] font-black text-rose-600"
                      title={
                        report.blacklist.note ||
                        'TopN 选股已剔除交易黑名单（跳过并顺延补足）'
                      }
                    >
                      黑名单生效 {report.blacklist.excluded} 只
                    </span>
                  ) : null}
                </span>
                <span className="text-[9px] text-slate-400">
                  {report.execution?.entry ?? 'T+1 开盘'} · 持 {report.window.horizon} 日 ·{' '}
                  {report.execution?.cost_bps_per_side ?? 15}bp/边 · 基准{' '}
                  {report.execution?.benchmark_basis ?? 'open→open'}
                </span>
              </div>
              <SignalEvalTopNTable rows={report.topn} execution={report.execution} />
            </div>

            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-[10px] font-black text-slate-600">滚动体检历史</span>
                <span className="text-[9px] text-slate-400">
                  {history.length >= 2 ? `最近 ${history.length} 次快照` : '每交易日 16:50 自动快照'}
                </span>
              </div>
              {historySeries.length >= 2 ? (
                <ResponsiveContainer width="100%" height={150}>
                  <ComposedChart data={historySeries} margin={{ top: 6, right: 6, left: -16, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" vertical={false} />
                    <XAxis
                      dataKey="window_end"
                      tick={{ fontSize: 9, fill: '#94a3b8' }}
                      axisLine={false}
                      tickLine={false}
                      interval="preserveStartEnd"
                    />
                    <YAxis
                      tickFormatter={(value: number) => `${(value * 100).toFixed(1)}%`}
                      tick={{ fontSize: 9, fill: '#94a3b8' }}
                      axisLine={false}
                      tickLine={false}
                    />
                    <ReTooltip
                      formatter={(value: number | string, name: string) => [
                        `${(Number(value) * 100).toFixed(2)}%`,
                        name === 'rank_ic' ? '原始 RankIC' : '中性化 RankIC',
                      ]}
                      labelFormatter={(label) => `窗口止 ${label}`}
                      contentStyle={{ fontSize: 10, borderRadius: 8, borderColor: '#e2e8f0' }}
                    />
                    <ReferenceLine y={0} stroke="#cbd5e1" />
                    <Line type="monotone" dataKey="rank_ic" stroke="#a5b4fc" strokeWidth={1.5} dot={false} connectNulls />
                    <Line type="monotone" dataKey="neutral_ic" stroke="#4f46e5" strokeWidth={1.5} dot={false} connectNulls />
                  </ComposedChart>
                </ResponsiveContainer>
              ) : (
                <div className="rounded-lg border border-dashed border-slate-200 px-3 py-4 text-[10px] leading-relaxed text-slate-400">
                  历史不足（当前 {history.length} 次）。日更任务会按交易日写入快照，积累后可看滚动 3 个月指标的趋势，而不是只看某一帧。
                </div>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-1.5 border-t border-slate-100 pt-3">
              <Flag ok={report.verdict.ranking_pass} label="排序" />
              <Flag ok={report.verdict.neutral_pass} label="中性化" />
              <Flag ok={report.verdict.decile_pass} label="分位" />
              <Flag ok={report.verdict.long_pass} label="长多" />
              <Flag ok={report.verdict.stability_pass} label="稳定性" />
              <span className="ml-1 text-[9px] leading-relaxed text-slate-400">
                ADV≥2000 万、PIT ST 剔除；≈{report.window.independent_periods ?? '—'} 个独立{' '}
                {report.window.horizon} 日周期；3 个月样本不估 FF5 α，风格剥离看离线研究报告。
              </span>
            </div>
          </>
        )}
      </div>
    </section>
  );
};
