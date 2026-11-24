import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Spin } from 'antd';
import { clsx } from 'clsx';
import {
  Activity,
  BadgeCheck,
  Clock3,
  Crosshair,
  Gauge,
  Layers3,
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

const Stat: React.FC<{ label: string; value: React.ReactNode; hint?: string; tone?: string }> = ({
  label,
  value,
  hint,
  tone = 'text-slate-800',
}) => (
  <div className="min-w-0">
    <div className="text-[9px] font-black uppercase tracking-[0.16em] text-slate-400">{label}</div>
    <div className={clsx('mt-1 font-mono text-lg font-black tabular-nums leading-none', tone)}>{value}</div>
    {hint ? <div className="mt-1 truncate text-[9px] text-slate-400">{hint}</div> : null}
  </div>
);

const ModelCard: React.FC<{
  title: string;
  role: string;
  z: number | null | undefined;
  pred?: number | null;
  predLabel?: string;
  predFormat?: 'pct' | 'num';
  asOf: string;
  accent: 'indigo' | 'violet';
}> = ({ title, role, z, pred, predLabel, predFormat = 'pct', asOf, accent }) => (
  <div className="relative overflow-hidden rounded-2xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
    <span className={clsx('absolute inset-y-0 left-0 w-1', accent === 'indigo' ? 'bg-indigo-500' : 'bg-violet-500')} />
    <div className="flex items-start justify-between gap-2 pl-1">
      <div>
        <div className="text-[10px] font-black uppercase tracking-[0.18em] text-slate-400">{role}</div>
        <div className="mt-0.5 text-[13px] font-black text-slate-800">{title}</div>
      </div>
      <span className="rounded-md bg-slate-50 px-1.5 py-0.5 font-mono text-[9px] text-slate-400">{asOf}</span>
    </div>
    <div className="mt-3 flex items-end justify-between gap-3 pl-1">
      <div>
        <div className={clsx('font-mono text-3xl font-black tabular-nums leading-none', zTone(z))}>{fmtZ(z)}</div>
        <div className="mt-1 text-[9px] text-slate-400">60 日 expanding z</div>
      </div>
      <div className="text-right">
        <div className="font-mono text-[12px] font-bold text-slate-600">
          {predFormat === 'num' ? fmtNum(pred, 4) : fmtPct(pred, 3)}
        </div>
        <div className="text-[9px] text-slate-400">{predLabel || '聚合预测'}</div>
      </div>
    </div>
  </div>
);

const MetricRow: React.FC<{
  label: string;
  metrics: TimingV2Metrics;
  highlight?: boolean;
}> = ({ label, metrics, highlight }) => (
  <tr className={clsx('border-t border-slate-100', highlight && 'bg-emerald-50/40')}>
    <td className="py-2 pr-2 text-[11px] font-bold text-slate-600">{label}</td>
    <td className={clsx('px-2 py-2 text-right font-mono text-[11px] font-black tabular-nums', highlight ? 'text-emerald-700' : 'text-slate-700')}>
      {fmtPct(metrics.ann)}
    </td>
    <td className="px-2 py-2 text-right font-mono text-[11px] tabular-nums text-slate-500">{fmtNum(metrics.sharpe, 2)}</td>
    <td className="px-2 py-2 text-right font-mono text-[11px] tabular-nums text-slate-500">{fmtPct(metrics.mdd)}</td>
    <td className="px-2 py-2 text-right font-mono text-[11px] tabular-nums text-slate-500">{fmtNum(metrics.t, 2)}</td>
    <td className="px-2 py-2 text-right font-mono text-[11px] tabular-nums text-slate-400">{fmtPlainPct(metrics.exposure, 0)}</td>
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
      // 后端异步重算：轮询状态，完成后重新拉快照（避免 2-4 分钟请求超时）
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
      <div className="flex h-72 flex-col items-center justify-center gap-2 rounded-2xl border border-slate-200 bg-white">
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

  return (
    <div className="flex flex-col gap-3 pb-4">
      <header className="relative overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
        <div className="h-1 bg-gradient-to-r from-indigo-500 via-violet-500 to-emerald-400" />
        <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center gap-1 text-[9px] font-black uppercase tracking-[0.2em] text-indigo-500">
                <Crosshair size={11} /> 机构择时 v2
              </span>
              <span className={clsx('rounded-full px-2 py-0.5 text-[9px] font-black ring-1', tone.chip)}>
                {data.signal.label}
              </span>
              <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[9px] font-black text-amber-700 ring-1 ring-amber-100">
                影子盘 · 不构成投资建议
              </span>
            </div>
            <div className="mt-1 text-[13px] font-black text-slate-800">
              TFT 战术层 + MLP 战略确认；树模型只做截面选股
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-slate-400">
              <span className="inline-flex items-center gap-1"><Clock3 size={10} /> 信号日 {data.as_of}</span>
              <span>TFT {data.tft_as_of}</span>
              <span>MLP {data.mlp_as_of}</span>
              <span>行情 {data.data_as_of}</span>
              <span className="font-mono">{data.model_version}</span>
            </div>
          </div>
          <Button
            size="small"
            icon={<RefreshCw size={12} className={refreshing ? 'animate-spin' : ''} />}
            loading={refreshing}
            onClick={() => void refresh()}
            className="rounded-lg text-[11px]"
          >
            {refreshing ? '重算中…' : '重算快照'}
          </Button>
        </div>
      </header>

      {error ? (
        <Alert type="warning" showIcon className="rounded-xl" message={error} />
      ) : null}

      {stale > 0 ? (
        <div className="flex items-start gap-2 rounded-xl border border-amber-100 bg-amber-50/70 px-3 py-2 text-[10px] text-amber-700">
          <TriangleAlert size={13} className="mt-0.5 shrink-0" />
          <span>
            TFT 最新推理日落后行情 <b>{stale}</b> 个交易日；信号仍按 TFT 最新日与同期 MLP 计算。
            {stale > 5 ? ' 已超过 5 日，按 stale 处理，谨慎使用。' : ''}
          </span>
        </div>
      ) : null}

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,0.85fr)]">
        <section className="relative overflow-hidden rounded-2xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-[9px] font-black uppercase tracking-[0.18em] text-slate-400">建议暴露</div>
              <div className="mt-1 flex items-end gap-2">
                <span className={clsx('font-mono text-4xl font-black tabular-nums leading-none', tone.text)}>
                  {Math.round(exposure * 100)}%
                </span>
                <span className="pb-1 text-[11px] font-bold text-slate-500">{data.signal.label}</span>
              </div>
            </div>
            <span className="inline-flex items-center gap-1 rounded-lg bg-slate-50 px-2 py-1 text-[9px] font-bold text-slate-500">
              <Shield size={11} /> 风险预算
            </span>
          </div>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-slate-100">
            <div className={clsx('h-full rounded-full transition-all', tone.bar)} style={{ width: `${Math.max(2, exposure * 100)}%` }} />
          </div>
          <div className="mt-1 flex justify-between text-[9px] text-slate-400">
            <span>0% 空仓</span><span>50% 基准</span><span>100% 确认</span>
          </div>
          <p className="mt-3 rounded-xl bg-slate-50/80 px-3 py-2 text-[10px] leading-relaxed text-slate-500">
            {data.signal.rule}
            <br />
            <span className="text-slate-400">{data.signal.execution}</span>
          </p>
          <div className="mt-3 grid grid-cols-3 gap-2">
            <Stat label="TFT z" value={fmtZ(data.signal.tft_z)} tone={zTone(data.signal.tft_z)} hint="战术层" />
            <Stat label="MLP z" value={fmtZ(data.signal.mlp_z)} tone={zTone(data.signal.mlp_z)} hint="战略确认" />
            <Stat label="Placebo p" value={fmtNum(perf.placebo_p, 3)} hint="组合 Sharpe 置换" />
          </div>
        </section>

        <div className="grid gap-3">
          <ModelCard
            title="TFT 深度序列"
            role="战术层"
            z={data.tft_model.z}
            pred={data.tft_model.pred_mean}
            predLabel="聚合 rank 分"
            predFormat="num"
            asOf={data.tft_as_of}
            accent="indigo"
          />
          <ModelCard
            title="MLP 市场级"
            role="战略确认层"
            z={data.mlp_model.z}
            pred={data.mlp_model.pred}
            predLabel="5日预期收益"
            predFormat="pct"
            asOf={data.mlp_as_of}
            accent="violet"
          />
        </div>
      </div>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1.35fr)_minmax(0,0.65fr)]">
        <section className="rounded-2xl border border-slate-200/80 bg-white p-3 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="mb-1 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Activity size={13} className="text-indigo-500" />
              <span className="text-[11px] font-black text-slate-700">信号轨迹（近 120 个交易日）</span>
            </div>
            <span className="text-[9px] text-slate-400">TFT_z / MLP_z / 暴露</span>
          </div>
          <ResponsiveContainer width="100%" height={240}>
            <LineChart data={chartData} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
              <XAxis dataKey="date" tick={{ fontSize: 9, fill: '#94a3b8' }} axisLine={false} tickLine={false} minTickGap={24} />
              <YAxis yAxisId="z" tick={{ fontSize: 9, fill: '#94a3b8' }} axisLine={false} tickLine={false} domain={[-3, 3]} />
              <YAxis yAxisId="exp" orientation="right" tick={{ fontSize: 9, fill: '#94a3b8' }} axisLine={false} tickLine={false} domain={[0, 1]} tickFormatter={(v: number) => `${Math.round(v * 100)}%`} />
              <Tooltip contentStyle={{ borderRadius: 12, fontSize: 11 }} formatter={(value: number, name: string) => [name === 'exposure' ? `${Math.round(Number(value) * 100)}%` : Number(value).toFixed(2), name]} />
              <Legend wrapperStyle={{ fontSize: 10 }} />
              <ReferenceLine yAxisId="z" y={0} stroke="#cbd5e1" />
              <Line yAxisId="z" type="monotone" dataKey="TFT" stroke="#6366f1" strokeWidth={2} dot={false} connectNulls />
              <Line yAxisId="z" type="monotone" dataKey="MLP" stroke="#8b5cf6" strokeWidth={1.5} dot={false} connectNulls />
              <Line yAxisId="exp" type="stepAfter" dataKey="exposure" stroke="#10b981" strokeWidth={1.5} dot={false} connectNulls />
            </LineChart>
          </ResponsiveContainer>
        </section>

        <section className="rounded-2xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="mb-3 flex items-center gap-2">
            <Layers3 size={13} className="text-indigo-500" />
            <span className="text-[11px] font-black text-slate-700">结构面</span>
            <span className="text-[9px] text-slate-400">同日收盘</span>
          </div>
          <div className="grid grid-cols-2 gap-2">
            {[
              { label: '广度', value: fmtPlainPct(structure.breadth, 1), hint: '上涨家数占比' },
              { label: '大票−等权 20日', value: fmtPct(structure.gap_ie_20, 1), hint: '护盘/降温足迹' },
              { label: 'HS300−中证1000 20日', value: fmtPct(structure.gap_hz_20, 1), hint: '大小盘裂口' },
              { label: '20日年化波动', value: fmtPlainPct(structure.vol20_ann, 0), hint: 'HS300' },
              { label: 'PB 便宜分位', value: fmtPlainPct(structure.bp_capw_pct, 0), hint: '3年滚动' },
              { label: 'EP 便宜分位', value: fmtPlainPct(structure.ep_capw_pct, 0), hint: '3年滚动' },
            ].map((it) => (
              <div key={it.label} className="rounded-xl border border-slate-100 bg-slate-50/60 px-2.5 py-2">
                <div className="text-[9px] font-bold text-slate-400">{it.label}</div>
                <div className="mt-0.5 font-mono text-[13px] font-black tabular-nums text-slate-700">{it.value}</div>
                <div className="text-[8px] text-slate-400">{it.hint}</div>
              </div>
            ))}
          </div>
          <div className="mt-3 flex items-center gap-2 rounded-xl bg-slate-50 px-2.5 py-2 text-[9px] text-slate-400">
            <Gauge size={11} /> 结构面只做解释与风控，不单独触发仓位
          </div>
        </section>
      </div>

      <section className="rounded-2xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <BadgeCheck size={13} className="text-emerald-500" />
            <span className="text-[11px] font-black text-slate-700">样本外表现</span>
            <span className="text-[9px] text-slate-400">
              {perf.window.start} ~ {perf.window.end} · {perf.window.n} 日 · 5bp/次
            </span>
          </div>
          {perf.placebo_p !== null ? (
            <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[9px] font-black text-emerald-700 ring-1 ring-emerald-100">
              组合 placebo p = {fmtNum(perf.placebo_p, 3)}
            </span>
          ) : null}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[620px] text-xs">
            <thead>
              <tr className="text-[9px] font-bold text-slate-400">
                <th className="py-1 pr-2 text-left font-normal">规则</th>
                <th className="px-2 py-1 text-right font-normal">年化</th>
                <th className="px-2 py-1 text-right font-normal">Sharpe</th>
                <th className="px-2 py-1 text-right font-normal">MDD</th>
                <th className="px-2 py-1 text-right font-normal">t</th>
                <th className="px-2 py-1 text-right font-normal">平均暴露</th>
              </tr>
            </thead>
            <tbody>
              <MetricRow label="TFT 50% + MLP 确认" metrics={perf.combined} highlight />
              <MetricRow label="TFT 单独" metrics={perf.tft_only} />
              <MetricRow label="MLP 单独（同窗口）" metrics={perf.mlp_only} />
              <MetricRow label="买入持有 HS300" metrics={perf.buy_hold} />
            </tbody>
          </table>
        </div>
        <div className="mt-2 grid gap-2 text-[9px] text-slate-400 sm:grid-cols-3">
          <span>MLP 长历史（{perf.mlp_long_history.window.start} 起）年化 {fmtPct(perf.mlp_long_history.strategy.ann)} · Sharpe {fmtNum(perf.mlp_long_history.strategy.sharpe)} · MDD {fmtPct(perf.mlp_long_history.strategy.mdd)}</span>
          <span>同期买入持有年化 {fmtPct(perf.mlp_long_history.buy_hold.ann)} · MDD {fmtPct(perf.mlp_long_history.buy_hold.mdd)}</span>
          <span>生产快照使用实时 pred_daily 聚合分；与离线批量口径可能有差异</span>
        </div>
      </section>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,0.7fr)_minmax(0,1.3fr)]">
        <section className="rounded-2xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="mb-2 flex items-center gap-2">
            <Shield size={13} className="text-indigo-500" />
            <span className="text-[11px] font-black text-slate-700">模型分工</span>
          </div>
          <div className="space-y-2">
            {data.roles.map((r) => (
              <div key={r.model} className="rounded-xl border border-slate-100 bg-slate-50/60 px-2.5 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[10px] font-black text-slate-700">{r.model}</span>
                  <span className={clsx('rounded-full px-1.5 py-0.5 text-[8px] font-black ring-1', r.status === '参与' ? 'bg-emerald-50 text-emerald-700 ring-emerald-100' : 'bg-slate-100 text-slate-500 ring-slate-200')}>
                    {r.status}
                  </span>
                </div>
                <div className="mt-0.5 text-[9px] text-slate-400">{r.role} · {r.use}</div>
              </div>
            ))}
          </div>
        </section>

        <section className="rounded-2xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <div className="mb-2 flex items-center gap-2">
            <TriangleAlert size={13} className="text-amber-500" />
            <span className="text-[11px] font-black text-slate-700">使用边界</span>
          </div>
          <ul className="space-y-1.5 text-[10px] leading-relaxed text-slate-500">
            {data.notes.map((note) => (
              <li key={note} className="flex items-start gap-1.5">
                <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-slate-300" />
                <span>{note}</span>
              </li>
            ))}
            <li className="flex items-start gap-1.5">
              <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-slate-300" />
              <span>规则未经 1 年以上影子盘验证，不得直接接入真钱仓位；先观察，再评估。</span>
            </li>
          </ul>
        </section>
      </div>
    </div>
  );
};

export default MarketTimingV2Panel;
