import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Spin } from 'antd';
import { clsx } from 'clsx';
import {
  Activity,
  BadgeCheck,
  Clock3,
  Crosshair,
  Gauge,
  RefreshCw,
  Shield,
  TriangleAlert,
} from 'lucide-react';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { SERVICE_ENDPOINTS } from '../../../config/services';
import type { TimingV2Metrics, TimingV2Snapshot } from '../types/timingV2';

const API = `${SERVICE_ENDPOINTS.USER_SERVICE}/market-analysis/timing-v2`;

const fmtPct = (value: number | null | undefined, digits = 1): string => {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${value > 0 ? '+' : ''}${(value * 100).toFixed(digits)}%`;
};

const fmtPlainPct = (value: number | null | undefined, digits = 1): string => {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
};

const fmtNum = (value: number | null | undefined, digits = 2): string => {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return value.toFixed(digits);
};

const fmtZ = (value: number | null | undefined): string => {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}`;
};

const zTone = (value: number | null | undefined): string => {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'text-slate-400';
  return value > 0 ? 'text-emerald-600' : value < 0 ? 'text-rose-500' : 'text-slate-500';
};

const exposureTone = (exposure: number) => {
  if (exposure >= 1) return { text: 'text-emerald-600', bar: 'bg-emerald-500', chip: 'bg-emerald-50 text-emerald-700 ring-emerald-100' };
  if (exposure > 0) return { text: 'text-amber-600', bar: 'bg-amber-400', chip: 'bg-amber-50 text-amber-700 ring-amber-100' };
  return { text: 'text-rose-500', bar: 'bg-rose-400', chip: 'bg-rose-50 text-rose-600 ring-rose-100' };
};

const Cell: React.FC<{ label: string; role?: string; children: React.ReactNode; hint?: React.ReactNode }> = ({
  label,
  role,
  children,
  hint,
}) => (
  <div className="min-w-0 px-3 py-2.5">
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-[9px] font-black uppercase tracking-[0.14em] text-slate-400">{label}</span>
      {role ? <span className="truncate text-[8px] font-bold text-slate-300">{role}</span> : null}
    </div>
    <div className="mt-1">{children}</div>
    {hint ? <div className="mt-0.5 truncate text-[9px] text-slate-400">{hint}</div> : null}
  </div>
);

const MetricRow: React.FC<{ label: string; metrics: TimingV2Metrics; highlight?: boolean }> = ({
  label,
  metrics,
  highlight,
}) => (
  <tr className={clsx('border-t border-slate-100', highlight && 'bg-emerald-50/40')}>
    <td className={clsx('py-1.5 pr-2 text-[10px] font-bold', highlight ? 'text-emerald-700' : 'text-slate-600')}>{label}</td>
    <td className={clsx('px-2 py-1.5 text-right font-mono text-[10px] font-black tabular-nums', highlight ? 'text-emerald-700' : 'text-slate-700')}>
      {fmtPct(metrics.ann)}
    </td>
    <td className="px-2 py-1.5 text-right font-mono text-[10px] tabular-nums text-slate-500">{fmtNum(metrics.sharpe)}</td>
    <td className="px-2 py-1.5 text-right font-mono text-[10px] tabular-nums text-slate-500">{fmtPct(metrics.mdd)}</td>
    <td className="px-2 py-1.5 text-right font-mono text-[10px] tabular-nums text-slate-500">{fmtNum(metrics.t)}</td>
    <td className="px-2 py-1.5 text-right font-mono text-[10px] tabular-nums text-slate-400">{fmtPlainPct(metrics.exposure, 0)}</td>
  </tr>
);

export const MarketTimingV2Panel: React.FC = () => {
  const [data, setData] = useState<TimingV2Snapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    const token = localStorage.getItem('access_token') || '';
    try {
      const res = await fetch(API, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
      if (res.status === 404) {
        setData(null);
        setError('盘后快照尚未生成（每日 17:10 自动生成）');
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData((await res.json()) as TimingV2Snapshot);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '机构择时快照加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    setError('');
    const token = localStorage.getItem('access_token') || '';
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    try {
      const res = await fetch(`${API}/refresh`, { method: 'POST', headers });
      if (res.status === 403) throw new Error('需要管理员权限才能重算快照');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      for (let i = 0; i < 120; i += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 2500));
        const st = await fetch(`${API}/refresh/status`, { headers });
        if (!st.ok) continue;
        const state = (await st.json()) as { running?: boolean; error?: string | null };
        if (state.running) continue;
        if (state.error) throw new Error(state.error);
        await load();
        return;
      }
      throw new Error('重算超时（5 分钟），请稍后刷新页面');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '重算失败');
    } finally {
      setRefreshing(false);
    }
  }, [load]);

  const chartData = useMemo(
    () =>
      (data?.history || []).map((p) => ({
        date: p.date.slice(5),
        TFT: p.tft_z,
        MLP: p.mlp_z,
        exposure: p.exposure,
      })),
    [data],
  );

  if (loading) {
    return (
      <div className="flex h-64 flex-col items-center justify-center gap-2 rounded-2xl border border-slate-200 bg-white">
        <Spin />
        <span className="text-[11px] text-slate-400">加载机构择时快照…</span>
      </div>
    );
  }

  if (!data || data.status === 'missing') {
    return (
      <Alert
        type="info"
        showIcon
        className="rounded-2xl"
        message="机构择时 v2 尚未就绪"
        description={error || '等待盘后任务生成快照。'}
        action={
          <Button size="small" icon={<RefreshCw size={12} />} loading={refreshing} onClick={() => void refresh()}>
            立即重算
          </Button>
        }
      />
    );
  }

  const exposure = Number(data.signal.exposure) || 0;
  const tone = exposureTone(exposure);
  const stale = Number(data.stale_trading_days) || 0;
  const perf = data.performance;
  const structure = data.structure;
  const inference = data.inference;

  return (
    <div className="flex flex-col gap-2 pb-2">
      <header className="relative overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
        <div className="h-0.5 bg-gradient-to-r from-indigo-500 via-violet-500 to-emerald-400" />
        <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1 text-[9px] font-black uppercase tracking-[0.18em] text-indigo-500">
              <Crosshair size={11} /> 机构择时 v2
            </span>
            <span className={clsx('rounded-full px-2 py-0.5 text-[9px] font-black ring-1', tone.chip)}>{data.signal.label}</span>
            <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[9px] font-black text-amber-700 ring-1 ring-amber-100">
              影子盘 · 不构成投资建议
            </span>
            <span className="inline-flex items-center gap-1 text-[9px] text-slate-400">
              <Clock3 size={9} /> 信号 {data.as_of} · TFT {data.tft_as_of} · MLP {data.mlp_as_of}
            </span>
            {inference?.succeeded ? (
              <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[8px] font-bold text-emerald-700">
                自动补推理 {inference.succeeded} 天
              </span>
            ) : null}
          </div>
          <Button
            size="small"
            icon={<RefreshCw size={12} className={refreshing ? 'animate-spin' : ''} />}
            loading={refreshing}
            onClick={() => void refresh()}
            className="rounded-lg text-[10px]"
          >
            {refreshing ? '重算中…' : '重算'}
          </Button>
        </div>
      </header>

      {error ? <Alert type="warning" showIcon className="rounded-xl !py-1.5" message={error} /> : null}
      {stale > 0 ? (
        <div className="flex items-center gap-2 rounded-xl border border-amber-100 bg-amber-50/70 px-3 py-1.5 text-[9px] text-amber-700">
          <TriangleAlert size={11} className="shrink-0" />
          <span>
            TFT 信号落后行情 <b>{stale}</b> 个交易日；盘后任务会先自动补推理，若仍落后按 stale 处理。
          </span>
        </div>
      ) : null}

      <section className="grid grid-cols-2 divide-x divide-slate-100 overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)] lg:grid-cols-4">
        <Cell label="建议暴露" role="TFT 50% + MLP 确认">
          <div className="flex items-baseline gap-1.5">
            <span className={clsx('font-mono text-3xl font-black tabular-nums leading-none', tone.text)}>{Math.round(exposure * 100)}%</span>
            <span className="text-[9px] font-bold text-slate-400">{data.signal.label}</span>
          </div>
          <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-slate-100">
            <div className={clsx('h-full rounded-full', tone.bar)} style={{ width: `${Math.max(2, exposure * 100)}%` }} />
          </div>
        </Cell>
        <Cell label="TFT z" role="战术层" hint={`rank 分 ${fmtNum(data.tft_model.pred_mean, 4)}`}>
          <span className={clsx('font-mono text-2xl font-black tabular-nums leading-none', zTone(data.signal.tft_z))}>{fmtZ(data.signal.tft_z)}</span>
        </Cell>
        <Cell label="MLP z" role="战略确认" hint={`5日预期 ${fmtPct(data.mlp_model.pred, 3)}`}>
          <span className={clsx('font-mono text-2xl font-black tabular-nums leading-none', zTone(data.signal.mlp_z))}>{fmtZ(data.signal.mlp_z)}</span>
        </Cell>
        <Cell label="OOS" role={`${perf.window.n} 日`} hint={`placebo p=${fmtNum(perf.placebo_p, 3)}`}>
          <span className="font-mono text-2xl font-black tabular-nums leading-none text-slate-700">{fmtNum(perf.combined.sharpe)}</span>
          <span className="ml-1 text-[9px] text-slate-400">Sharpe</span>
        </Cell>
      </section>

      <div className="grid gap-2 xl:grid-cols-[minmax(0,1.48fr)_minmax(0,0.52fr)]">
        <section className="rounded-2xl border border-slate-200/80 bg-white p-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="mb-0.5 flex items-center justify-between px-1">
            <div className="flex items-center gap-1.5">
              <Activity size={11} className="text-indigo-500" />
              <span className="text-[10px] font-black text-slate-600">信号轨迹 · 近 120 日</span>
            </div>
            <span className="text-[8px] text-slate-400">TFT / MLP z · 右轴暴露</span>
          </div>
          <ResponsiveContainer width="100%" height={176}>
            <LineChart data={chartData} margin={{ top: 6, right: 6, left: -22, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
              <XAxis dataKey="date" tick={{ fontSize: 8, fill: '#94a3b8' }} axisLine={false} tickLine={false} minTickGap={28} />
              <YAxis yAxisId="z" tick={{ fontSize: 8, fill: '#94a3b8' }} axisLine={false} tickLine={false} domain={[-3, 3]} />
              <YAxis yAxisId="exp" orientation="right" tick={{ fontSize: 8, fill: '#94a3b8' }} axisLine={false} tickLine={false} domain={[0, 1]} tickFormatter={(v: number) => `${Math.round(v * 100)}%`} />
              <Tooltip contentStyle={{ borderRadius: 10, fontSize: 10 }} formatter={(value: number, name: string) => [name === 'exposure' ? `${Math.round(Number(value) * 100)}%` : Number(value).toFixed(2), name]} />
              <Legend wrapperStyle={{ fontSize: 9 }} />
              <ReferenceLine yAxisId="z" y={0} stroke="#cbd5e1" />
              <Line yAxisId="z" type="monotone" dataKey="TFT" stroke="#6366f1" strokeWidth={1.8} dot={false} connectNulls />
              <Line yAxisId="z" type="monotone" dataKey="MLP" stroke="#8b5cf6" strokeWidth={1.3} dot={false} connectNulls />
              <Line yAxisId="exp" type="stepAfter" dataKey="exposure" stroke="#10b981" strokeWidth={1.2} dot={false} connectNulls />
            </LineChart>
          </ResponsiveContainer>
        </section>

        <section className="rounded-2xl border border-slate-200/80 bg-white px-2.5 py-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="mb-1 flex items-center gap-1.5">
            <Gauge size={11} className="text-indigo-500" />
            <span className="text-[10px] font-black text-slate-600">结构面</span>
            <span className="text-[8px] text-slate-400">同日收盘</span>
          </div>
          <div className="grid grid-cols-2 gap-x-2 gap-y-1.5">
            {[
              { label: '广度', value: fmtPlainPct(structure.breadth, 1) },
              { label: '大票−等权 20日', value: fmtPct(structure.gap_ie_20, 1) },
              { label: 'HS300−中证1000', value: fmtPct(structure.gap_hz_20, 1) },
              { label: '20日年化波动', value: fmtPlainPct(structure.vol20_ann, 0) },
              { label: 'PB 便宜分位', value: fmtPlainPct(structure.bp_capw_pct, 0) },
              { label: 'EP 便宜分位', value: fmtPlainPct(structure.ep_capw_pct, 0) },
            ].map((it) => (
              <div key={it.label} className="rounded-lg bg-slate-50/70 px-2 py-1.5">
                <div className="text-[8px] font-bold text-slate-400">{it.label}</div>
                <div className="font-mono text-[11px] font-black tabular-nums text-slate-700">{it.value}</div>
              </div>
            ))}
          </div>
          <p className="mt-1.5 rounded-lg bg-slate-50 px-2 py-1 text-[8px] leading-relaxed text-slate-400">
            {data.signal.rule}
          </p>
        </section>
      </div>

      <section className="rounded-2xl border border-slate-200/80 bg-white px-3 py-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
        <div className="mb-1 flex flex-wrap items-center justify-between gap-1">
          <div className="flex items-center gap-1.5">
            <BadgeCheck size={11} className="text-emerald-500" />
            <span className="text-[10px] font-black text-slate-600">样本外表现</span>
            <span className="text-[8px] text-slate-400">
              {perf.window.start} ~ {perf.window.end} · 5bp/次
            </span>
          </div>
          <span className="rounded-full bg-emerald-50 px-1.5 py-0.5 text-[8px] font-black text-emerald-700">
            placebo p = {fmtNum(perf.placebo_p, 3)}
          </span>
        </div>
        <table className="w-full text-[9px]">
          <thead>
            <tr className="text-slate-400">
              <th className="py-0.5 pr-2 text-left font-normal">规则</th>
              <th className="px-2 py-0.5 text-right font-normal">年化</th>
              <th className="px-2 py-0.5 text-right font-normal">Sharpe</th>
              <th className="px-2 py-0.5 text-right font-normal">MDD</th>
              <th className="px-2 py-0.5 text-right font-normal">t</th>
              <th className="px-2 py-0.5 text-right font-normal">暴露</th>
            </tr>
          </thead>
          <tbody>
            <MetricRow label="TFT 50% + MLP 确认" metrics={perf.combined} highlight />
            <MetricRow label="TFT 单独" metrics={perf.tft_only} />
            <MetricRow label="MLP 单独" metrics={perf.mlp_only} />
            <MetricRow label="买入持有 HS300" metrics={perf.buy_hold} />
          </tbody>
        </table>
      </section>

      <div className="grid gap-2 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <section className="rounded-2xl border border-slate-200/80 bg-white px-3 py-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="mb-1 flex items-center gap-1.5">
            <Shield size={11} className="text-indigo-500" />
            <span className="text-[10px] font-black text-slate-600">模型分工</span>
          </div>
          <div className="space-y-1">
            {data.roles.map((r) => (
              <div key={r.model} className="flex items-center justify-between gap-2 text-[9px]">
                <span className="truncate font-bold text-slate-600">
                  {r.model} <span className="font-normal text-slate-400">· {r.role}</span>
                </span>
                <span className={clsx('shrink-0 rounded-full px-1.5 py-0.5 text-[8px] font-black ring-1', r.status === '参与' ? 'bg-emerald-50 text-emerald-700 ring-emerald-100' : 'bg-slate-100 text-slate-500 ring-slate-200')}>
                  {r.status}
                </span>
              </div>
            ))}
          </div>
        </section>

        <section className="rounded-2xl border border-slate-200/80 bg-white px-3 py-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="mb-1 flex items-center gap-1.5">
            <TriangleAlert size={11} className="text-amber-500" />
            <span className="text-[10px] font-black text-slate-600">使用边界</span>
          </div>
          <div className="grid gap-x-3 gap-y-0.5 text-[8px] leading-relaxed text-slate-400 sm:grid-cols-2">
            {data.notes.map((note) => (
              <span key={note} className="flex items-start gap-1">
                <span className="mt-1 h-0.5 w-0.5 shrink-0 rounded-full bg-slate-300" />
                {note}
              </span>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
};

export default MarketTimingV2Panel;
