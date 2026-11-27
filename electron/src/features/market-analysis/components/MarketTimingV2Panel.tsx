import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Spin } from 'antd';
import { clsx } from 'clsx';
import {
  ArrowDownRight,
  ArrowUpRight,
  BadgeCheck,
  Clock3,
  Crosshair,
  Gauge,
  Layers,
  Minus,
  RefreshCw,
  Shield,
  TriangleAlert,
} from 'lucide-react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
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
  return `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value * 100).toFixed(digits)}%`;
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
  return `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(2)}`;
};

const zTone = (value: number | null | undefined): string => {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'text-slate-400';
  // A 股口径：涨/看多为红，跌/看空为绿
  return value > 0 ? 'text-rose-600' : value < 0 ? 'text-emerald-600' : 'text-slate-500';
};

/** 收益类数值统一口径：涨=红，跌=绿（A 股约定，与港股/美股相反） */
const retTone = (value: number | null | undefined): string => {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'text-slate-400';
  return value > 0 ? 'text-rose-600' : value < 0 ? 'text-emerald-600' : 'text-slate-600';
};

const UP_COLOR = '#e11d48';
const DOWN_COLOR = '#10b981';

/** 暴露档位：文字 + 图标双通道，不只靠颜色（WCAG 1.4.1） */
const exposureMeta = (exposure: number) => {
  if (exposure >= 1)
    return {
      text: 'text-emerald-600',
      bar: 'bg-emerald-500',
      chip: 'bg-emerald-50 text-emerald-700 ring-emerald-100',
      icon: <ArrowUpRight size={11} aria-hidden />,
      word: '满额',
    };
  if (exposure > 0)
    return {
      text: 'text-amber-600',
      bar: 'bg-amber-400',
      chip: 'bg-amber-50 text-amber-700 ring-amber-100',
      icon: <Minus size={11} aria-hidden />,
      word: '半额',
    };
  return {
    text: 'text-rose-500',
    bar: 'bg-rose-400',
    chip: 'bg-rose-50 text-rose-600 ring-rose-100',
    icon: <ArrowDownRight size={11} aria-hidden />,
    word: '空仓',
  };
};

const Cell_: React.FC<{ label: string; role?: string; children: React.ReactNode; hint?: React.ReactNode }> = ({
  label,
  role,
  children,
  hint,
}) => (
  <div className="min-w-0 px-3 py-2.5">
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-[9px] font-black uppercase tracking-[0.14em] text-slate-500">{label}</span>
      {role ? <span className="truncate text-[8px] font-bold text-slate-400">{role}</span> : null}
    </div>
    <div className="mt-1">{children}</div>
    {hint ? <div className="mt-0.5 truncate text-[9px] text-slate-500">{hint}</div> : null}
  </div>
);

const MetricRow: React.FC<{ label: string; metrics: TimingV2Metrics; highlight?: boolean }> = ({
  label,
  metrics,
  highlight,
}) => (
  <tr className={clsx('border-t border-slate-100', highlight && 'bg-emerald-50/40')}>
    <td className={clsx('py-1.5 pr-2 text-[10px] font-bold', highlight ? 'text-emerald-700' : 'text-slate-600')}>
      {label}
    </td>
    <td
      className={clsx(
        'px-2 py-1.5 text-right font-mono text-[10px] font-black tabular-nums',
        retTone(metrics.ann),
      )}
    >
      {fmtPct(metrics.ann)}
    </td>
    <td className="px-2 py-1.5 text-right font-mono text-[10px] tabular-nums text-slate-600">{fmtNum(metrics.sharpe)}</td>
    <td className="px-2 py-1.5 text-right font-mono text-[10px] tabular-nums text-slate-600">{fmtPct(metrics.mdd)}</td>
    <td className="px-2 py-1.5 text-right font-mono text-[10px] tabular-nums text-slate-500">{fmtNum(metrics.t)}</td>
    <td className="px-2 py-1.5 text-right font-mono text-[10px] tabular-nums text-slate-500">
      {fmtPlainPct(metrics.exposure, 0)}
    </td>
  </tr>
);

const SectionTitle: React.FC<{ icon: React.ReactNode; title: string; hint?: string }> = ({ icon, title, hint }) => (
  <div className="mb-1 flex items-center justify-between gap-2 px-0.5">
    <div className="flex items-center gap-1.5">
      {icon}
      <span className="text-[10px] font-black text-slate-700">{title}</span>
    </div>
    {hint ? <span className="text-[8px] text-slate-500">{hint}</span> : null}
  </div>
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
        setError('盘后快照尚未生成（A 股日线同步完成后自动生成，每日 17:10 兜底一次）');
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

  const equityData = useMemo(() => {
    const eq = data?.performance?.equity;
    if (!eq || !eq.dates?.length) return [];
    return eq.dates.map((d, i) => ({
      date: d.slice(5),
      combined: eq.combined[i],
      tft: eq.tft_only[i],
      shadow: eq.shadow_mlp_raw?.[i],
      hold: eq.buy_hold[i],
    }));
  }, [data]);

  const monthlyData = useMemo(
    () => (data?.performance?.monthly || []).map((m) => ({ month: m.month.slice(2), ret: m.ret })),
    [data],
  );

  const signalData = useMemo(
    () =>
      (data?.history || []).map((p) => ({
        date: p.date.slice(5),
        TFT: p.tft_z,
        MLP: p.mlp_z,
        exposure: p.exposure,
        alt: p.exposure_alt ?? null,
      })),
    [data],
  );

  if (loading) {
    return (
      <div className="flex h-64 flex-col items-center justify-center gap-2 rounded-2xl border border-slate-200 bg-white">
        <Spin />
        <span className="text-[11px] text-slate-500">加载机构择时快照…</span>
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
  const exposureAlt = data.signal.exposure_alt ?? null;
  const tone = exposureMeta(exposure);
  const stale = Number(data.stale_trading_days) || 0;
  const perf = data.performance;
  const structure = data.structure;
  const inference = data.inference;
  const combined = perf.combined;
  const altMetrics = perf.shadow_mlp_raw;

  return (
    <div className="flex flex-col gap-2 pb-2">
      {/* ── 顶栏 ─────────────────────────────────────────── */}
      <header className="relative overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
        <div className="h-0.5 bg-gradient-to-r from-indigo-500 via-violet-500 to-emerald-400" />
        <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1 text-[9px] font-black uppercase tracking-[0.18em] text-indigo-500">
              <Crosshair size={11} aria-hidden /> 机构择时 v2
            </span>
            <span className={clsx('inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[9px] font-black ring-1', tone.chip)}>
              {tone.icon}
              {data.signal.label}
            </span>
            <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[9px] font-black text-amber-700 ring-1 ring-amber-100">
              影子盘 · 不构成投资建议
            </span>
            <span className="inline-flex items-center gap-1 text-[9px] text-slate-500">
              <Clock3 size={9} aria-hidden /> 信号 {data.as_of} · TFT {data.tft_as_of} · MLP {data.mlp_as_of}
            </span>
            {inference?.succeeded ? (
              <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[8px] font-bold text-emerald-700">
                自动补推理 {inference.succeeded} 天
              </span>
            ) : null}
            {data.mode !== 'mlp_only' && data.tft_model?.model_id ? (
              <span
                className="rounded-full bg-slate-100 px-2 py-0.5 text-[8px] font-bold text-slate-600 ring-1 ring-slate-200"
                title={`战术层模型来源：${data.tft_model.source ?? '—'}`}
              >
                战术层 {String(data.tft_model.model_id).replace(/^mdl_/, '').slice(0, 20)}
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

      {data.mode === 'mlp_only' ? (
        <div className="flex items-start gap-2 rounded-xl border border-amber-100 bg-amber-50/70 px-3 py-2 text-[9px] leading-relaxed text-amber-700">
          <TriangleAlert size={11} className="mt-0.5 shrink-0" aria-hidden />
          <span>
            没找到可用的<b>战术层模型</b>，已自动降级为 <b>MLP 单层</b>（只用 45 个市场级特征）。
            训练一个 CN 深度/序列模型并跑过日更推理后会自动接上，无需改配置。
          </span>
        </div>
      ) : null}

      {error ? <Alert type="warning" showIcon className="rounded-xl !py-1.5" message={error} /> : null}
      {stale > 0 ? (
        <div className="flex items-center gap-2 rounded-xl border border-amber-100 bg-amber-50/70 px-3 py-1.5 text-[9px] text-amber-700">
          <TriangleAlert size={11} className="shrink-0" aria-hidden />
          <span>
            TFT 信号落后行情 <b>{stale}</b> 个交易日；盘后任务会先自动补推理，若仍落后按 stale 处理。
          </span>
        </div>
      ) : null}

      {/* ── KPI ──────────────────────────────────────────── */}
      <section className="grid grid-cols-2 divide-x divide-slate-100 overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)] lg:grid-cols-4">
        <Cell_ label="建议暴露" role="TFT 50% + MLP 确认">
          <div className="flex items-baseline gap-1.5">
            <span className={clsx('font-mono text-3xl font-black tabular-nums leading-none', tone.text)}>
              {Math.round(exposure * 100)}%
            </span>
            <span className="inline-flex items-center gap-0.5 text-[9px] font-bold text-slate-500">
              {tone.icon}
              {tone.word}
            </span>
          </div>
          <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-slate-100">
            <div className={clsx('h-full rounded-full', tone.bar)} style={{ width: `${Math.max(2, exposure * 100)}%` }} />
          </div>
        </Cell_>
        <Cell_ label="TFT z" role="战术层" hint={`rank 分 ${fmtNum(data.tft_model.pred_mean, 4)}`}>
          <span className={clsx('font-mono text-2xl font-black tabular-nums leading-none', zTone(data.signal.tft_z))}>
            {fmtZ(data.signal.tft_z)}
          </span>
        </Cell_>
        <Cell_
          label="MLP z"
          role="战略确认"
          hint={`5日预期 ${fmtPct(data.mlp_model.pred, 3)} · 影子用原始符号 ${exposureAlt === null ? '—' : `${Math.round(exposureAlt * 100)}%`}`}
        >
          <span className={clsx('font-mono text-2xl font-black tabular-nums leading-none', zTone(data.signal.mlp_z))}>
            {fmtZ(data.signal.mlp_z)}
          </span>
        </Cell_>
        <Cell_ label="OOS Sharpe" role={`${perf.window.n} 日`} hint={`placebo p=${fmtNum(perf.placebo_p, 3)} · t=${fmtNum(combined.t)}`}>
          <span className="font-mono text-2xl font-black tabular-nums leading-none text-slate-700">
            {fmtNum(combined.sharpe)}
          </span>
          <span className={clsx('ml-1 text-[9px]', retTone(combined.ann))}>年化 {fmtPct(combined.ann)}</span>
        </Cell_>
      </section>

      {/* ── 净值曲线（主图）────────────────────────────── */}
      <section className="rounded-2xl border border-slate-200/80 bg-white p-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
        <SectionTitle
          icon={<BadgeCheck size={11} className="text-emerald-500" aria-hidden />}
          title="样本外净值 · 起点 = 1"
          hint={`${perf.window.start} ~ ${perf.window.end} · 5bp/次 · 不含杠杆`}
        />
        {equityData.length ? (
          <ResponsiveContainer width="100%" height={248}>
            <LineChart data={equityData} margin={{ top: 6, right: 8, left: -20, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
              <XAxis dataKey="date" tick={{ fontSize: 8, fill: '#64748b' }} axisLine={false} tickLine={false} minTickGap={40} />
              <YAxis
                tick={{ fontSize: 8, fill: '#64748b' }}
                axisLine={false}
                tickLine={false}
                domain={['auto', 'auto']}
                tickFormatter={(v: number) => v.toFixed(2)}
              />
              <Tooltip
                contentStyle={{ borderRadius: 10, fontSize: 10 }}
                formatter={(value: number, name: string) => [Number(value).toFixed(3), name]}
              />
              <Legend wrapperStyle={{ fontSize: 9 }} iconType="plainline" />
              <ReferenceLine y={1} stroke="#cbd5e1" strokeDasharray="4 4" />
              <Line type="monotone" dataKey="combined" name="生产规则" stroke="#10b981" strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="tft" name="TFT 单独" stroke="#6366f1" strokeWidth={1.4} dot={false} />
              <Line
                type="monotone"
                dataKey="shadow"
                name="影子（MLP 原始符号）"
                stroke="#0ea5e9"
                strokeWidth={1.4}
                strokeDasharray="5 3"
                dot={false}
              />
              <Line type="monotone" dataKey="hold" name="买入持有 HS300" stroke="#94a3b8" strokeWidth={1.2} strokeDasharray="2 3" dot={false} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-40 items-center justify-center text-[10px] text-slate-400">快照未含净值曲线</div>
        )}
      </section>

      {/* ── 信号轨迹 + 月度收益 ─────────────────────────── */}
      <div className="grid gap-2 xl:grid-cols-[minmax(0,1.35fr)_minmax(0,0.65fr)]">
        <section className="rounded-2xl border border-slate-200/80 bg-white p-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <SectionTitle
            icon={<Layers size={11} className="text-indigo-500" aria-hidden />}
            title="信号轨迹 · 近 120 日"
            hint="左轴 z 分数 · 右轴建议暴露"
          />
          <ResponsiveContainer width="100%" height={186}>
            <LineChart data={signalData} margin={{ top: 6, right: 6, left: -22, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
              <XAxis dataKey="date" tick={{ fontSize: 8, fill: '#64748b' }} axisLine={false} tickLine={false} minTickGap={28} />
              <YAxis yAxisId="z" tick={{ fontSize: 8, fill: '#64748b' }} axisLine={false} tickLine={false} domain={[-3, 3]} />
              <YAxis
                yAxisId="exp"
                orientation="right"
                tick={{ fontSize: 8, fill: '#64748b' }}
                axisLine={false}
                tickLine={false}
                domain={[0, 1]}
                tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
              />
              <Tooltip
                contentStyle={{ borderRadius: 10, fontSize: 10 }}
                formatter={(value: number, name: string) =>
                  name.startsWith('暴露') ? `${Math.round(Number(value) * 100)}%` : Number(value).toFixed(2)
                }
              />
              <Legend wrapperStyle={{ fontSize: 9 }} iconType="plainline" />
              <ReferenceLine yAxisId="z" y={0} stroke="#cbd5e1" />
              <Line yAxisId="z" type="monotone" dataKey="TFT" stroke="#6366f1" strokeWidth={1.8} dot={false} connectNulls />
              <Line yAxisId="z" type="monotone" dataKey="MLP" stroke="#8b5cf6" strokeWidth={1.3} dot={false} connectNulls />
              <Line yAxisId="exp" type="stepAfter" dataKey="exposure" name="暴露（生产）" stroke="#10b981" strokeWidth={1.2} dot={false} connectNulls />
              <Line
                yAxisId="exp"
                type="stepAfter"
                dataKey="alt"
                name="暴露（影子）"
                stroke="#0ea5e9"
                strokeWidth={1}
                strokeDasharray="4 3"
                dot={false}
                connectNulls
              />
            </LineChart>
          </ResponsiveContainer>
        </section>

        <section className="rounded-2xl border border-slate-200/80 bg-white p-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <SectionTitle icon={<Gauge size={11} className="text-emerald-500" aria-hidden />} title="月度收益" hint="生产规则" />
          <ResponsiveContainer width="100%" height={186}>
            <BarChart data={monthlyData} margin={{ top: 6, right: 6, left: -24, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
              <XAxis dataKey="month" tick={{ fontSize: 8, fill: '#64748b' }} axisLine={false} tickLine={false} interval={1} />
              <YAxis
                tick={{ fontSize: 8, fill: '#64748b' }}
                axisLine={false}
                tickLine={false}
                tickFormatter={(v: number) => `${(v * 100).toFixed(0)}%`}
              />
              <Tooltip
                contentStyle={{ borderRadius: 10, fontSize: 10 }}
                formatter={(value: number) => [`${value > 0 ? '+' : '−'}${Math.abs(Number(value) * 100).toFixed(2)}%`, '月收益']}
              />
              <ReferenceLine y={0} stroke="#cbd5e1" />
              <Bar dataKey="ret" radius={[2, 2, 0, 0]} maxBarSize={18}>
                {monthlyData.map((m) => (
                  <Cell key={m.month} fill={m.ret >= 0 ? UP_COLOR : DOWN_COLOR} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </section>
      </div>

      {/* ── 表现表 + 结构面 ─────────────────────────────── */}
      <div className="grid gap-2 xl:grid-cols-[minmax(0,1.25fr)_minmax(0,0.75fr)]">
        <section className="rounded-2xl border border-slate-200/80 bg-white px-3 py-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <SectionTitle
            icon={<BadgeCheck size={11} className="text-emerald-500" aria-hidden />}
            title="样本外表现"
            hint={`placebo p = ${fmtNum(perf.placebo_p, 3)}`}
          />
          <table className="w-full text-[9px]">
            <thead>
              <tr className="text-slate-500">
                <th className="py-0.5 pr-2 text-left font-normal">规则</th>
                <th className="px-2 py-0.5 text-right font-normal">年化</th>
                <th className="px-2 py-0.5 text-right font-normal">Sharpe</th>
                <th className="px-2 py-0.5 text-right font-normal">MDD</th>
                <th className="px-2 py-0.5 text-right font-normal">t</th>
                <th className="px-2 py-0.5 text-right font-normal">暴露</th>
              </tr>
            </thead>
            <tbody>
              <MetricRow label="生产：TFT 50% + MLP 确认" metrics={combined} highlight />
              {altMetrics ? <MetricRow label="影子：MLP 原始符号" metrics={altMetrics} /> : null}
              <MetricRow label="TFT 单独" metrics={perf.tft_only} />
              <MetricRow label="MLP 单独" metrics={perf.mlp_only} />
              <MetricRow label="买入持有 HS300" metrics={perf.buy_hold} />
            </tbody>
          </table>
          <p className="mt-1.5 rounded-lg bg-slate-50 px-2 py-1 text-[8px] leading-relaxed text-slate-500">
            {data.signal.rule}
            {data.signal.rule_alt ? ` ｜ ${data.signal.rule_alt}` : ''}
          </p>
        </section>

        <section className="rounded-2xl border border-slate-200/80 bg-white px-2.5 py-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <SectionTitle icon={<Gauge size={11} className="text-indigo-500" aria-hidden />} title="结构面" hint={`${data.data_as_of} 收盘`} />
          <div className="grid grid-cols-2 gap-x-2 gap-y-1.5">
            {[
              { label: '广度', value: fmtPlainPct(structure.breadth, 1), meter: structure.breadth ?? 0, tone: 'bg-indigo-400' },
              { label: '大票−等权 20日', value: fmtPct(structure.gap_ie_20, 1), meter: null, tone: 'bg-slate-300' },
              { label: 'HS300−中证1000', value: fmtPct(structure.gap_hz_20, 1), meter: null, tone: 'bg-slate-300' },
              { label: '20日年化波动', value: fmtPlainPct(structure.vol20_ann, 0), meter: null, tone: 'bg-slate-300' },
              { label: 'PB 便宜分位', value: fmtPlainPct(structure.bp_capw_pct, 0), meter: structure.bp_capw_pct ?? 0, tone: 'bg-emerald-400' },
              { label: 'EP 便宜分位', value: fmtPlainPct(structure.ep_capw_pct, 0), meter: structure.ep_capw_pct ?? 0, tone: 'bg-emerald-400' },
            ].map((it) => (
              <div key={it.label} className="rounded-lg bg-slate-50/70 px-2 py-1.5">
                <div className="text-[8px] font-bold text-slate-500">{it.label}</div>
                <div className="font-mono text-[11px] font-black tabular-nums text-slate-700">{it.value}</div>
                {it.meter !== null ? (
                  <div className="mt-1 h-0.5 overflow-hidden rounded-full bg-slate-200">
                    <div className={clsx('h-full rounded-full', it.tone)} style={{ width: `${Math.min(100, Math.max(0, it.meter * 100))}%` }} />
                  </div>
                ) : null}
              </div>
            ))}
          </div>
          <div className="mt-1.5 rounded-lg bg-slate-50 px-2 py-1 text-[8px] leading-relaxed text-slate-500">
            结构面只做解释与风控，不单独触发仓位。
          </div>
        </section>
      </div>

      {/* ── 分工 + 边界 ─────────────────────────────────── */}
      <div className="grid gap-2 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <section className="rounded-2xl border border-slate-200/80 bg-white px-3 py-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <SectionTitle icon={<Shield size={11} className="text-indigo-500" aria-hidden />} title="模型分工" />
          <div className="space-y-1">
            {data.roles.map((r) => (
              <div key={r.model} className="flex items-center justify-between gap-2 text-[9px]">
                <span className="truncate font-bold text-slate-600">
                  {r.model} <span className="font-normal text-slate-500">· {r.role}</span>
                </span>
                <span
                  className={clsx(
                    'shrink-0 rounded-full px-1.5 py-0.5 text-[8px] font-black ring-1',
                    r.status === '参与'
                      ? 'bg-emerald-50 text-emerald-700 ring-emerald-100'
                      : 'bg-slate-100 text-slate-500 ring-slate-200',
                  )}
                >
                  {r.status}
                </span>
              </div>
            ))}
          </div>
        </section>

        <section className="rounded-2xl border border-slate-200/80 bg-white px-3 py-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <SectionTitle icon={<TriangleAlert size={11} className="text-amber-500" aria-hidden />} title="使用边界" />
          <div className="grid gap-x-3 gap-y-0.5 text-[8px] leading-relaxed text-slate-500 sm:grid-cols-2">
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
