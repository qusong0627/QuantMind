import React, { useMemo, useState } from 'react';
import { Tag, Tooltip, Typography } from 'antd';
import clsx from 'clsx';
import type { ScoreDistribution, InferenceRankingItem } from '../../services/modelTrainingService';

const { Text } = Typography;

export interface ScoreBucketKey {
  key: string;
  label: string;
  action: string;
  test: (s: number) => boolean;
  color: string;
}

/** 选股策略的分数区间定义（与后端 score_buckets 一致） */
export const STRATEGY_BUCKETS: ScoreBucketKey[] = [
  { key: 'lt_010', label: '< 0.10', action: '分数 < 0.10', test: s => s < 0.10, color: 'slate' },
  { key: 'gold', label: '0.10-0.12', action: '分数 0.10-0.12', test: s => s >= 0.10 && s < 0.12, color: 'emerald' },
  { key: 'opt_012_015', label: '0.12-0.15', action: '分数 0.12-0.15', test: s => s >= 0.12 && s < 0.15, color: 'amber' },
  { key: 'warn_015_020', label: '0.15-0.20', action: '分数 0.15-0.20', test: s => s >= 0.15 && s < 0.20, color: 'orange' },
  { key: 'gte_020', label: '≥ 0.20', action: '分数 ≥ 0.20', test: s => s >= 0.20, color: 'rose' },
];

interface Props {
  dist: ScoreDistribution;
  /** 排名明细（用于负分×市值/板块/行业 交叉统计） */
  rankings?: InferenceRankingItem[];
  /** 当前选中的分数区间 key，null 表示未筛选 */
  activeBucket?: string | null;
  /** 点击区间时回调，null 表示清除筛选 */
  onSelectBucket?: (key: string | null) => void;
}

const fmt = (n: number | undefined | null, digits = 4): string => {
  if (n === undefined || n === null || !Number.isFinite(n)) return '—';
  return n.toFixed(digits);
};

const fmtPct = (n: number | undefined | null): string => {
  if (n === undefined || n === null || !Number.isFinite(n)) return '—';
  return `${n.toFixed(1)}%`;
};

const BUCKET_STYLE: Record<string, { active: string; idle: string; dot: string }> = {
  slate: { active: 'border-slate-300 bg-slate-100', idle: 'border-slate-100 bg-white hover:border-slate-200', dot: 'bg-slate-400' },
  emerald: { active: 'border-emerald-400 bg-emerald-50 ring-1 ring-emerald-200', idle: 'border-emerald-100 bg-white hover:border-emerald-200', dot: 'bg-emerald-500' },
  amber: { active: 'border-amber-400 bg-amber-50 ring-1 ring-amber-200', idle: 'border-amber-100 bg-white hover:border-amber-200', dot: 'bg-amber-400' },
  orange: { active: 'border-orange-400 bg-orange-50 ring-1 ring-orange-200', idle: 'border-orange-100 bg-white hover:border-orange-200', dot: 'bg-orange-400' },
  rose: { active: 'border-rose-400 bg-rose-50 ring-1 ring-rose-200', idle: 'border-rose-100 bg-white hover:border-rose-200', dot: 'bg-rose-500' },
};

export const ScoreDistributionPanel: React.FC<Props> = ({ dist, rankings, activeBucket, onSelectBucket }) => {
  const [hover, setHover] = useState<number | null>(null);

  // 负分细分统计（基于排名明细）
  const negStats = useMemo(() => {
    if (!rankings) return null;
    const neg = rankings.filter(r => r.score < 0);
    if (neg.length === 0) return null;
    // 负分分数段（按研究结论分档）
    const segments = [
      { key: 'extreme', label: '极端 ≤-0.20', test: (s: number) => s <= -0.20, cls: 'text-rose-700 bg-rose-100 border-rose-200' },
      { key: 'deep', label: '深负 -0.15~-0.20', test: (s: number) => s > -0.20 && s <= -0.15, cls: 'text-rose-600 bg-rose-50 border-rose-200' },
      { key: 'mid', label: '中负 -0.06~-0.15', test: (s: number) => s > -0.15 && s <= -0.06, cls: 'text-orange-600 bg-orange-50 border-orange-200' },
      { key: 'light', label: '轻负 -0.06~0', test: (s: number) => s > -0.06, cls: 'text-slate-500 bg-slate-100 border-slate-200' },
    ];
    const segStats = segments.map(seg => ({
      ...seg,
      count: neg.filter(r => seg.test(r.score)).length,
    }));
    // 负分×市值
    const tierCounts: Record<string, number> = {};
    neg.forEach(r => {
      const t = r.market_cap_tier || '未知';
      tierCounts[t] = (tierCounts[t] || 0) + 1;
    });
    // 负分×板块
    const boardCounts: Record<string, number> = {};
    neg.forEach(r => {
      const b = r.board || '其他';
      boardCounts[b] = (boardCounts[b] || 0) + 1;
    });
    // 负分×行业 Top（做空集中行业）
    const indCounts: Record<string, number> = {};
    neg.forEach(r => {
      if (!r.industry) return;
      indCounts[r.industry] = (indCounts[r.industry] || 0) + 1;
    });
    const topIndustries = Object.entries(indCounts)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map(([industry, count]) => ({ industry, count }));
    return { segStats, tierCounts, boardCounts, topIndustries };
  }, [rankings]);

  const W = 480;
  const H = 68;
  const PL = 4;
  const PR = 4;
  const PT = 4;
  const PB = 4;
  const IW = W - PL - PR;
  const IH = H - PT - PB;

  const maxCount = useMemo(() => {
    return dist.histogram.reduce((m, b) => (b.count > m ? b.count : m), 0) || 1;
  }, [dist.histogram]);

  const lo = dist.min;
  const hi = dist.max;
  const span = hi - lo || 1;
  const zeroX = lo < 0 && hi > 0 ? PL + ((0 - lo) / span) * IW : null;
  const barW = IW / Math.max(dist.histogram.length, 1);

  const bucketColor = (b: { x0: number; x1: number }): string => {
    if (b.x0 >= 0) return '#fb7185'; // rose-400（正分 → 红，A股习惯涨红）
    if (b.x1 <= 0) return '#34d399'; // emerald-400（负分 → 绿）
    return '#cbd5e1'; // slate-300
  };

  // 黄金区间在直方图上的 x 范围
  const bucketRange = (loVal: number, hiVal: number): { x: number; w: number } => {
    const x = PL + ((loVal - lo) / span) * IW;
    const x2 = PL + ((hiVal - lo) / span) * IW;
    return { x, w: Math.max(x2 - x, 0) };
  };
  const goldRange = bucketRange(0.10, 0.12);

  return (
    <div className="rounded-2xl border border-slate-100 bg-slate-50 p-3 space-y-3">
      <div className="flex items-center justify-between">
        <Text className="text-xs text-slate-400 font-black uppercase tracking-wide">分数分布</Text>
        <Text className="text-xs text-slate-400 font-mono">N={dist.count.toLocaleString()}</Text>
      </div>

      {/* 策略分数区间：可点击筛选 */}
      {onSelectBucket && (
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
          {STRATEGY_BUCKETS.map(b => {
            const isActive = activeBucket === b.key;
            const st = BUCKET_STYLE[b.color] || BUCKET_STYLE.slate;
            return (
              <Tooltip key={b.key} title={`${b.action}${isActive ? '（点击取消筛选）' : '（点击筛选排名）'}`}>
                <button
                  type="button"
                  onClick={() => onSelectBucket(isActive ? null : b.key)}
                  className={clsx('w-full rounded-xl border px-2 py-2 text-left transition-all cursor-pointer', isActive ? st.active : st.idle)}
                >
                  <div className="flex items-center gap-1.5 mb-0.5">
                    <span className={clsx('w-1.5 h-1.5 rounded-full', st.dot)} />
                    <Text className="text-xs font-black text-slate-700">{b.label}</Text>
                  </div>
                  <Text className="block text-[11px] text-slate-400 truncate">{b.action}</Text>
                </button>
              </Tooltip>
            );
          })}
        </div>
      )}

      {/* 4 张紧凑统计卡 */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <div className="rounded-2xl bg-rose-50 border border-rose-100 px-3 py-2">
          <div className="text-xs text-rose-600 font-black uppercase">正分占比</div>
          <div className="flex items-baseline gap-1 mt-0.5">
            <span className="text-lg font-black text-rose-700 font-mono">{fmtPct(dist.positive_pct)}</span>
            <span className="text-xs text-rose-500 font-mono">{dist.positive_count.toLocaleString()}</span>
          </div>
        </div>
        <div className="rounded-2xl bg-emerald-50 border border-emerald-100 px-3 py-2">
          <div className="text-xs text-emerald-600 font-black uppercase">负分占比</div>
          <div className="flex items-baseline gap-1 mt-0.5">
            <span className="text-lg font-black text-emerald-700 font-mono">{fmtPct(dist.negative_pct)}</span>
            <span className="text-xs text-emerald-500 font-mono">{dist.negative_count.toLocaleString()}</span>
          </div>
        </div>
        <div className="rounded-2xl bg-white border border-slate-100 px-3 py-2">
          <div className="text-xs text-slate-500 font-black uppercase">中位数</div>
          <div className="text-lg font-black text-slate-800 font-mono mt-0.5">{fmt(dist.median)}</div>
        </div>
        <div className="rounded-2xl bg-white border border-slate-100 px-3 py-2">
          <Tooltip title="Top 10% 股票的分数门槛">
            <div className="text-xs text-slate-500 font-black uppercase cursor-help">Top10% 门槛</div>
          </Tooltip>
          <div className="text-lg font-black text-slate-800 font-mono mt-0.5">{fmt(dist.p90)}</div>
        </div>
      </div>

      {/* 直方图 */}
      <div className="rounded-2xl bg-white border border-slate-100 px-3 py-2">
        <div className="flex items-center justify-between mb-1">
          <Text className="text-xs text-slate-400 font-mono">{fmt(lo)}</Text>
          <Text className="text-xs text-slate-400 font-black">分布直方图（20 桶）</Text>
          <Text className="text-xs text-slate-400 font-mono">{fmt(hi)}</Text>
        </div>
        <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
          {dist.histogram.map((b, i) => {
            const h = (b.count / maxCount) * IH;
            const x = PL + i * barW;
            const y = PT + (IH - h);
            const active = hover === i;
            return (
              <Tooltip
                key={i}
                title={
                  <div className="font-mono text-xs">
                    <div>区间: [{fmt(b.x0)} , {fmt(b.x1)}]</div>
                    <div>数量: {b.count.toLocaleString()} ({((b.count / dist.count) * 100).toFixed(1)}%)</div>
                  </div>
                }
              >
                <rect
                  x={x + 0.5}
                  y={y}
                  width={Math.max(barW - 1, 0.5)}
                  height={h}
                  fill={bucketColor(b)}
                  opacity={active ? 1 : 0.85}
                  onMouseEnter={() => setHover(i)}
                  onMouseLeave={() => setHover(null)}
                  style={{ cursor: 'pointer', transition: 'opacity 120ms' }}
                />
              </Tooltip>
            );
          })}
          {/* 黄金区间带 0.10-0.12 */}
          {goldRange.w > 0 && (
            <>
              <rect
                x={goldRange.x}
                y={PT - 1}
                width={goldRange.w}
                height={H - PT - PB + 2}
                fill="#10b981"
                opacity={0.08}
              />
              <line
                x1={goldRange.x}
                x2={goldRange.x}
                y1={PT - 1}
                y2={H - PB + 1}
                stroke="#10b981"
                strokeWidth={1}
                strokeDasharray="2,3"
              />
              <line
                x1={goldRange.x + goldRange.w}
                x2={goldRange.x + goldRange.w}
                y1={PT - 1}
                y2={H - PB + 1}
                stroke="#10b981"
                strokeWidth={1}
                strokeDasharray="2,3"
              />
            </>
          )}
          {/* 0 分线 */}
          {zeroX !== null && (
            <line
              x1={zeroX}
              x2={zeroX}
              y1={PT - 1}
              y2={H - PB + 1}
              stroke="#64748b"
              strokeWidth={1}
              strokeDasharray="2,2"
            />
          )}
        </svg>
      </div>

      {/* 负分细分统计 */}
      {negStats && (
        <div className="rounded-2xl bg-white border border-slate-100 px-3 py-2.5 space-y-2.5">
          <div className="flex items-center justify-between">
            <Text className="text-xs text-slate-400 font-black uppercase tracking-wide">负分细分（{dist.negative_count.toLocaleString()} 只）</Text>
            <Text className="text-[11px] text-slate-400">点击分数段可筛选</Text>
          </div>

          {/* 负分分数段 */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            {negStats.segStats.map(seg => {
              const isActive = activeBucket !== null && (
                (activeBucket === 'lt_010' && seg.key === 'light') ||
                (activeBucket === 'warn_015_020' && seg.key === 'mid') ||
                (activeBucket === 'gte_020' && (seg.key === 'deep' || seg.key === 'extreme'))
              );
              return (
                <button
                  key={seg.key}
                  type="button"
                  onClick={() => {
                    // 负分段的筛选映射到 bucketFilter（仅当有 onSelectBucket 时）
                    if (onSelectBucket) {
                      if (seg.key === 'extreme' || seg.key === 'deep') onSelectBucket(isActive ? null : 'gte_020');
                      else if (seg.key === 'mid') onSelectBucket(isActive ? null : 'warn_015_020');
                      else onSelectBucket(isActive ? null : 'lt_010');
                    }
                  }}
                  className={clsx('rounded-xl border px-2.5 py-2 text-left transition-all', seg.cls, isActive && 'ring-2 ring-rose-400')}
                >
                  <Text className="block text-[11px] font-black opacity-90">{seg.label}</Text>
                  <Text className="block font-black font-mono text-base mt-0.5">{seg.count} 只</Text>
                </button>
              );
            })}
          </div>

          {/* 负分×市值 */}
          <div>
            <Text className="block text-[11px] text-slate-400 font-bold mb-1">按市值</Text>
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(negStats.tierCounts).map(([tier, count]) => (
                <Tag key={tier} className={clsx('m-0 rounded-full border px-2 py-0.5 text-[11px] font-black',
                  tier === '微盘' ? 'text-rose-600 bg-rose-50 border-rose-100'
                  : tier === '小盘' ? 'text-orange-600 bg-orange-50 border-orange-100'
                  : tier === '中盘' ? 'text-amber-600 bg-amber-50 border-amber-100'
                  : tier === '大盘' ? 'text-blue-600 bg-blue-50 border-blue-100'
                  : tier === '超大盘' ? 'text-indigo-600 bg-indigo-50 border-indigo-100'
                  : 'text-slate-500 bg-slate-50 border-slate-100')}>
                  {tier} {count}
                </Tag>
              ))}
            </div>
          </div>

          {/* 负分×板块 */}
          <div>
            <Text className="block text-[11px] text-slate-400 font-bold mb-1">按板块</Text>
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(negStats.boardCounts).map(([board, count]) => (
                <Tag key={board} className="m-0 rounded-full border-0 bg-slate-100 text-slate-600 font-bold text-[11px] px-2 py-0.5">
                  {board} {count}
                </Tag>
              ))}
            </div>
          </div>

          {/* 负分集中行业 */}
          {negStats.topIndustries.length > 0 && (
            <div>
              <Text className="block text-[11px] text-slate-400 font-bold mb-1">负分集中行业（下跌持续）</Text>
              <div className="flex flex-wrap gap-1.5">
                {negStats.topIndustries.map(x => (
                  <Tag key={x.industry} className="m-0 rounded-full border-0 bg-rose-50 text-rose-600 font-bold text-[11px] px-2 py-0.5">
                    {x.industry} ×{x.count}
                  </Tag>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* 分位点 */}
      <div className="grid grid-cols-5 gap-1.5">
        {[
          { label: 'P10', value: dist.p10 },
          { label: 'P25', value: dist.p25 },
          { label: '中位', value: dist.median },
          { label: 'P75', value: dist.p75 },
          { label: 'P90', value: dist.p90 },
        ].map((pt) => (
          <div
            key={pt.label}
            className={clsx(
              'rounded-xl border px-2 py-1 text-center',
              pt.value >= 0 ? 'border-rose-100 bg-rose-50/40' : 'border-emerald-100 bg-emerald-50/40',
            )}
          >
            <div className="text-[11px] text-slate-500 font-black uppercase">{pt.label}</div>
            <div className={clsx('text-xs font-black font-mono', pt.value >= 0 ? 'text-rose-700' : 'text-emerald-700')}>
              {fmt(pt.value)}
            </div>
          </div>
        ))}
      </div>

      {/* 辅助行：均值/标准差/极值 */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500 font-mono">
        <span>均值 <span className="text-slate-800 font-black">{fmt(dist.mean)}</span></span>
        <span>σ <span className="text-slate-800 font-black">{fmt(dist.stdev)}</span></span>
        <span>极小 <span className="text-rose-700 font-black">{fmt(dist.min)}</span></span>
        <span>极大 <span className="text-emerald-700 font-black">{fmt(dist.max)}</span></span>
        {dist.zero_count > 0 && (
          <span>零分 <span className="text-slate-800 font-black">{dist.zero_count.toLocaleString()}</span></span>
        )}
      </div>
    </div>
  );
};

export default ScoreDistributionPanel;
