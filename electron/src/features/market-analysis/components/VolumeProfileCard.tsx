/**
 * 筹码结构 / 成交价格分布（Volume Profile）。
 *
 * 回答：主力/散户成本在哪、成交最密集的价格带、14% 低分位成交价、−14% 回撤位、
 * 日内高量时段（仅当本地分钟数据跟进最新日线时才展示）。
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Layers3, Loader2 } from 'lucide-react';
import {
  getVolumeProfile,
  type VolumeProfileBin,
  type VolumeProfileResponse,
} from '../services/moneyFlowService';

const WINDOWS = [20, 60, 120] as const;
const fmt = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : v.toFixed(digits);
const pct = (v: number | null | undefined): string =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`;

export const VolumeProfileCard: React.FC<{ symbol: string; date?: string; name?: string }> = ({
  symbol,
  date,
  name,
}) => {
  const [windowDays, setWindowDays] = useState<number>(60);
  const [data, setData] = useState<VolumeProfileResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getVolumeProfile(symbol, windowDays, date)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch(() => {
        if (!cancelled) setError('筹码结构取数失败，请稍后重试');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, windowDays, date]);

  const rows = useMemo<VolumeProfileBin[]>(
    () => [...(data?.profile ?? [])].sort((a, b) => b.mid - a.mid),
    [data],
  );
  const maxShare = Math.max(...rows.map((r) => r.share), 0.001);
  const inRange = (bin: VolumeProfileBin, price: number | null | undefined): boolean =>
    price !== null && price !== undefined && price >= bin.low && price < bin.high;

  if (!loading && error) {
    return (
      <div className="bg-white rounded-2xl border border-slate-200 px-4 py-3 text-xs text-slate-400">
        {error}
      </div>
    );
  }
  if (!loading && data && !data.available) {
    return (
      <div className="bg-white rounded-2xl border border-slate-200 px-4 py-3 text-xs text-slate-400">
        筹码结构暂不可用：{data.reason ?? '数据不足'}
      </div>
    );
  }
  if (!data && loading) {
    return (
      <div className="bg-white rounded-2xl border border-slate-200 px-4 py-8 flex items-center justify-center">
        <Loader2 className="w-4 h-4 animate-spin text-purple-500" />
      </div>
    );
  }
  if (!data) return null;

  const markers = (bin: VolumeProfileBin): string[] => {
    const out: string[] = [];
    if (inRange(bin, data.poc)) out.push('POC');
    if (data.val != null && data.vah != null && bin.high > data.val && bin.low < data.vah) out.push('价值区');
    if (inRange(bin, data.last)) out.push('现');
    if (inRange(bin, data.main_cost)) out.push('主');
    if (inRange(bin, data.retail_cost)) out.push('散');
    if (inRange(bin, data.p14_price)) out.push('14%');
    return out;
  };

  return (
    <div className="bg-white rounded-2xl border border-slate-200 shadow-sm px-4 py-3 flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <Layers3 className="w-3.5 h-3.5 text-purple-600" />
          <span className="text-xs font-extrabold text-slate-800">筹码结构 · 主力/散户成本</span>
          <span className="text-[10px] text-slate-400">{name ?? data.name} · {data.trade_date} · {data.window}日</span>
        </div>
        <div className="flex rounded-full bg-slate-100 p-0.5">
          {WINDOWS.map((w) => (
            <button
              key={w}
              onClick={() => setWindowDays(w)}
              className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold ${
                windowDays === w ? 'bg-white text-purple-700 shadow-2xs' : 'text-slate-500'
              }`}
            >
              {w}日
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-5 gap-1.5 text-[10px]">
        <div className="rounded-lg bg-slate-50 border border-slate-100 px-2 py-1">
          <div className="text-slate-400">现价 / 区间高</div>
          <div className="font-mono font-extrabold text-slate-800">{fmt(data.last)} / {fmt(data.high)}</div>
        </div>
        <div className="rounded-lg bg-red-50/50 border border-red-100 px-2 py-1">
          <div className="text-slate-400">主力成本</div>
          <div className="font-mono font-extrabold text-red-600">{fmt(data.main_cost)} <span className="text-[9px]">{pct(data.main_premium_pct)}</span></div>
        </div>
        <div className="rounded-lg bg-emerald-50/50 border border-emerald-100 px-2 py-1">
          <div className="text-slate-400">散户成本</div>
          <div className="font-mono font-extrabold text-emerald-700">{fmt(data.retail_cost)} <span className="text-[9px]">{pct(data.retail_premium_pct)}</span></div>
        </div>
        <div className="rounded-lg bg-amber-50/50 border border-amber-100 px-2 py-1">
          <div className="text-slate-400">POC / 70% 价值区</div>
          <div className="font-mono font-extrabold text-amber-700">{fmt(data.poc)} <span className="text-[9px]">{fmt(data.val)}~{fmt(data.vah)}</span></div>
        </div>
        <div className="rounded-lg bg-purple-50/50 border border-purple-100 px-2 py-1">
          <div className="text-slate-400">14% 低分位 / −14% 回撤</div>
          <div className="font-mono font-extrabold text-purple-700">{fmt(data.p14_price)} <span className="text-[9px]">({pct(data.p14_pct_from_last)}) / {fmt(data.drawdown14)}</span></div>
        </div>
      </div>

      <div className="flex flex-col gap-0.5">
        {rows.map((bin) => {
          const markersArr = markers(bin);
          const isPoc = markersArr.includes('POC');
          const inVa = markersArr.includes('价值区');
          return (
            <div
              key={`${bin.low}-${bin.high}`}
              className={`grid grid-cols-[64px_1fr_38px] items-center gap-1.5 rounded px-1 ${
                isPoc ? 'bg-red-50' : inVa ? 'bg-amber-50/60' : ''
              }`}
              title={`${bin.low}~${bin.high} · 成交额 ${bin.amount_yi} 亿 (${(bin.share * 100).toFixed(1)}%)`}
            >
              <span className="text-[9px] font-mono text-slate-500 text-right">{fmt(bin.mid)}</span>
              <span className="relative h-2.5 rounded-sm bg-slate-100/80">
                <span
                  className={`absolute inset-y-0 left-0 rounded-sm ${isPoc ? 'bg-red-500' : inVa ? 'bg-amber-400' : 'bg-slate-300'}`}
                  style={{ width: `${Math.max(1, (bin.share / maxShare) * 100)}%` }}
                />
              </span>
              <span className="text-[8px] font-mono text-slate-400 truncate">
                {markersArr.length > 0 ? markersArr.join('/') : `${(bin.share * 100).toFixed(0)}%`}
              </span>
            </div>
          );
        })}
      </div>

      <div className="flex items-center gap-3 text-[9px] text-slate-400 flex-wrap">
        <span className="inline-flex items-center gap-1"><span className="w-2 h-2 bg-red-500" />POC 最大成交价</span>
        <span className="inline-flex items-center gap-1"><span className="w-2 h-2 bg-amber-400" />70% 价值区</span>
        <span className="inline-flex items-center gap-1"><span className="font-bold text-slate-600">现/主/散/14%</span>对应价位所在箱</span>
        <span className="ml-auto">{data.cost_scope}</span>
      </div>

      {data.time_windows && (
        <div className="text-[9px] text-slate-500 border-t border-slate-100 pt-1">
          {data.time_windows.available && data.time_windows.top?.length ? (
            <span className="flex items-center gap-2 flex-wrap">
              <span className="font-bold text-slate-600">高量时段</span>
              {data.time_windows.top.map((t) => (
                <span key={t.bucket} className="font-mono bg-slate-50 border border-slate-200 rounded px-1 py-0.5">
                  {t.bucket} · {(t.share * 100).toFixed(0)}% · {t.amount_yi}亿
                </span>
              ))}
              <span className="text-slate-400">{data.time_windows.note}</span>
            </span>
          ) : (
            <span className="text-slate-400">{data.time_windows.note}</span>
          )}
        </div>
      )}

      <p className="text-[9px] text-slate-400 leading-snug">{data.method}</p>
    </div>
  );
};
