/**
 * 筹码结构 / 成交价格分布（Volume Profile）。
 *
 * 回答：主力/散户净头寸成本在哪、成交最密集的价格带、14% 低分位成交价、
 * −14% 回撤位、日内高量时段（仅当本地分钟数据跟进最新日线时才展示）。
 *
 * 成本口径：按日净额累计的「净头寸成本」（起点零仓，卖出不改变剩余成本，
 * 翻仓按当日 VWAP 重开），不是账户真实持仓成本；另列净买日/净卖日/全活动
 * 加权价作对照，避免只看单边而误读。
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
const fmtYi = (v: number | null | undefined): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const abs = Math.abs(v);
  if (abs >= 10000) return `${v > 0 ? '+' : ''}${(v / 10000).toFixed(2)}万亿`;
  return `${v > 0 ? '+' : ''}${v.toFixed(0)}亿`;
};
const netLabel = (v: number | null | undefined): string => {
  if (v === null || v === undefined || !Number.isFinite(v) || v === 0) return '无净额';
  return v > 0 ? '净买入' : '净卖出';
};
const costKindLabel = (kind: string | null | undefined): string =>
  kind === 'short' ? '卖出均价' : kind === 'long' ? '持仓成本' : '无净头寸';

interface VolumeProfileCardProps {
  symbol: string;
  date?: string;
  name?: string;
  /** 窄容器（个股详情右栏）用两列瓦片、单列成本带，避免五列挤压 */
  compact?: boolean;
}

export const VolumeProfileCard: React.FC<VolumeProfileCardProps> = ({
  symbol,
  date,
  name,
  compact = false,
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

  const windowVwap = data.market_vwap ?? data.vwap;
  const vwapPremium =
    data.last != null && windowVwap != null && windowVwap > 0
      ? (data.last / windowVwap - 1) * 100
      : null;
  const mainMark = data.main_cost_kind === 'short' ? '主卖' : '主仓';
  const retailMark = data.retail_cost_kind === 'short' ? '散卖' : '散仓';
  const vaRows = rows.filter(
    (bin) => data.val != null && data.vah != null && bin.high > data.val && bin.low < data.vah,
  );
  const vaTopMid = vaRows.length ? vaRows[0].mid : null;
  const vaBottomMid = vaRows.length ? vaRows[vaRows.length - 1].mid : null;
  const vaWidthPct =
    data.val != null && data.vah != null && data.val > 0
      ? (data.vah / data.val - 1) * 100
      : null;
  const vaPosition =
    data.last == null || data.val == null || data.vah == null
      ? null
      : data.last < data.val
        ? '带下方'
        : data.last > data.vah
          ? '带上方'
          : '带内';
  const vaWidthText = vaWidthPct == null ? '—' : `${vaWidthPct.toFixed(1)}%`;

  const markers = (bin: VolumeProfileBin): string[] => {
    const out: string[] = [];
    if (inRange(bin, data.poc)) out.push('POC');
    const isVa = data.val != null && data.vah != null && bin.high > data.val && bin.low < data.vah;
    if (isVa) {
      const isTop = vaTopMid != null && bin.mid === vaTopMid;
      const isBottom = vaBottomMid != null && bin.mid === vaBottomMid;
      out.push(isTop && !isBottom ? '价值区上沿' : isBottom && !isTop ? '价值区下沿' : '价值区');
    }
    if (inRange(bin, data.last)) out.push('现');
    if (inRange(bin, data.main_cost)) out.push(mainMark);
    if (inRange(bin, data.retail_cost)) out.push(retailMark);
    if (inRange(bin, data.p14_price)) out.push('14%');
    return out;
  };

  return (
    <div className="bg-white rounded-2xl border border-slate-200 shadow-sm px-4 py-3 flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <Layers3 className="w-3.5 h-3.5 text-purple-600" />
          <span className="text-xs font-extrabold text-slate-800">筹码结构 · 主力/散户成本</span>
          <span className="text-[9px] font-bold text-purple-700 bg-purple-50 border border-purple-100 rounded-full px-1.5 py-0.5">
            净头寸口径
          </span>
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

      <div className={`grid ${compact ? 'grid-cols-2' : 'grid-cols-2 lg:grid-cols-5'} gap-1.5 text-[10px]`}>
        <div className="rounded-lg bg-slate-50 border border-slate-100 px-2 py-1">
          <div className="text-slate-400">现价 / 窗口VWAP</div>
          <div className="font-mono font-extrabold text-slate-800 truncate">{fmt(data.last)} / {fmt(windowVwap)}</div>
          <div className="text-[9px] text-slate-500 font-mono">偏离 {pct(vwapPremium)}</div>
        </div>
        <div className="rounded-lg bg-red-50/50 border border-red-100 px-2 py-1">
          <div className="text-slate-400">主力 · {netLabel(data.main_net_yi)}</div>
          <div className="font-mono font-extrabold text-red-600">{fmtYi(data.main_net_yi)}</div>
          <div className={`text-[9px] text-slate-500 ${compact ? '' : 'truncate'}`} title={`${costKindLabel(data.main_cost_kind)} ${fmt(data.main_cost)} · 现价 ${pct(data.main_premium_pct)}`}>
            {costKindLabel(data.main_cost_kind)} <span className="font-mono text-slate-700">{fmt(data.main_cost)}</span>
            <span className="font-mono"> · 现价 {pct(data.main_premium_pct)}</span>
          </div>
        </div>
        <div className="rounded-lg bg-emerald-50/50 border border-emerald-100 px-2 py-1">
          <div className="text-slate-400">散户 · {netLabel(data.retail_net_yi)}</div>
          <div className="font-mono font-extrabold text-emerald-700">{fmtYi(data.retail_net_yi)}</div>
          <div className={`text-[9px] text-slate-500 ${compact ? '' : 'truncate'}`} title={`${costKindLabel(data.retail_cost_kind)} ${fmt(data.retail_cost)} · 现价 ${pct(data.retail_premium_pct)}`}>
            {costKindLabel(data.retail_cost_kind)} <span className="font-mono text-slate-700">{fmt(data.retail_cost)}</span>
            <span className="font-mono"> · 现价 {pct(data.retail_premium_pct)}</span>
          </div>
        </div>
        <div
          className="rounded-lg bg-amber-50/50 border border-amber-100 px-2 py-1"
          title="70% 价值区=窗口内 70% 成交额所在的价格带。带宽越窄，筹码越集中。现价在带下方：带内筹码整体浮亏，下沿是反弹压力；在带内：多空共识区；在带上方：带内筹码获利，上沿转支撑。"
        >
          <div className="text-slate-400">POC / 70% 价值区</div>
          <div className="font-mono font-extrabold text-amber-700">{fmt(data.poc)} <span className="text-[9px]">{fmt(data.val)}~{fmt(data.vah)}</span></div>
          <div className="text-[9px] text-slate-500 font-mono">现价在{vaPosition ?? '—'} · 带宽 {vaWidthText}</div>
        </div>
        <div className="rounded-lg bg-purple-50/50 border border-purple-100 px-2 py-1">
          <div className="text-slate-400">14% 低分位 / −14% 回撤</div>
          <div className="font-mono font-extrabold text-purple-700">{fmt(data.p14_price)} <span className="text-[9px]">({pct(data.p14_pct_from_last)}) / {fmt(data.drawdown14)}</span></div>
        </div>
      </div>

      <div className="flex items-start gap-1.5 text-[10px] leading-snug text-amber-900">
        <span className="mt-1 w-2 h-2 rounded-sm bg-amber-400 shrink-0" />
        <span>
          <b>黄 = 70% 价值区</b>：窗口内 70% 的成交额落在{' '}
          <span className="font-mono font-bold">{fmt(data.val)}~{fmt(data.vah)}</span>（带宽 {vaWidthText}）。
          <br />
          越窄=筹码越集中；现价在带下方=带内筹码整体浮亏、下沿是反弹压力；在带内=多空共识区；在带上方=带内获利、上沿转支撑。
        </span>
      </div>

      <div
        className={`grid ${compact ? 'grid-cols-1' : 'grid-cols-1 lg:grid-cols-2'} gap-x-3 gap-y-0.5 text-[9px] font-mono text-slate-500`}
        title="净买日=当日净流入为正的交易日 VWAP 加权；净卖日=净流出日的 VWAP 加权；全活动=|净额| 加权。三者都不是持仓成本，只作对照。"
      >
        <span className="truncate">
          <b className="text-red-600">主力双边</b> 净买日 {fmt(data.main_buy_cost)} · 净卖日 {fmt(data.main_sell_cost)} · 全活动 {fmt(data.main_activity_cost)}
        </span>
        <span className="truncate">
          <b className="text-emerald-700">散户双边</b> 净买日 {fmt(data.retail_buy_cost)} · 净卖日 {fmt(data.retail_sell_cost)} · 全活动 {fmt(data.retail_activity_cost)}
        </span>
      </div>

      <div className="flex flex-col gap-0.5">
        {rows.map((bin) => {
          const markersArr = markers(bin);
          const isPoc = markersArr.includes('POC');
          const inVa =
            data.val != null && data.vah != null && bin.high > data.val && bin.low < data.vah;
          return (
            <div
              key={`${bin.low}-${bin.high}`}
              className={`grid ${compact ? 'grid-cols-[52px_1fr_78px]' : 'grid-cols-[64px_1fr_92px]'} items-center gap-1.5 rounded px-1 ${
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
        <span className="inline-flex items-center gap-1"><span className="w-2 h-2 bg-amber-400" />黄=70% 价值区（上/下沿已标注）</span>
        <span className="inline-flex items-center gap-1"><span className="w-2 h-2 bg-slate-300" />其余成交价</span>
        <span className="inline-flex items-center gap-1"><b className="text-slate-600">主仓/散卖</b>净头寸均价所在箱（散户净卖出时标“散卖”）</span>
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

      <div className="text-[9px] leading-snug text-amber-800 bg-amber-50/80 border border-amber-100 rounded px-2 py-1">
        <span className="font-extrabold">成本口径</span>{' '}
        {data.position_note ??
          '主力=超大+大单、散户=中+小单，两组净额不互补；净头寸成本=窗口内逐日净额累计（起点零仓，卖出不减剩余成本，翻仓按当日 VWAP 重开），不是账户真实持仓成本；同日双边买卖不可还原。'}
      </div>

      <details className="text-[9px] text-slate-400">
        <summary className="cursor-pointer select-none font-bold text-slate-500">成交分布方法与局限</summary>
        <p className="mt-1 leading-snug">{data.method}</p>
      </details>
    </div>
  );
};
