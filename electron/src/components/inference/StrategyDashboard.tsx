import React from 'react';
import { Tag, Typography, Tooltip } from 'antd';
import { clsx } from 'clsx';
import {
  BarChart3, Shield, AlertTriangle, Repeat, ArrowDownRight,
} from 'lucide-react';
import type {
  InferenceRunRecord, IndustryTop1Stat, MarketMAFilter, NegativeAnalysis,
} from '../../services/modelTrainingService';

const { Text } = Typography;

interface Props {
  summary: InferenceRunRecord;
}

const fmt4 = (n: number | null | undefined): string =>
  n === null || n === undefined || !Number.isFinite(Number(n)) ? '—' : Number(n).toFixed(4);

/** 统一看板卡片外壳：大标题 + 宽松内容 */
function BoardCard({ icon, title, subtitle, right, children }: {
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
  right?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-3xl border border-slate-100 bg-white p-5 shadow-sm">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-2xl bg-slate-50 flex items-center justify-center">{icon}</div>
          <div>
            <Text className="block text-base font-black text-slate-800 leading-tight">{title}</Text>
            {subtitle && <Text className="block text-xs text-slate-400 mt-0.5">{subtitle}</Text>}
          </div>
        </div>
        {right}
      </div>
      {children}
    </div>
  );
}

/** 大盘均线过滤卡片：上证指数 vs MA20 → 强制空仓信号 */
function MarketMAFilterCard({ filter, isHk = false }: { filter?: MarketMAFilter; isHk?: boolean }) {
  const indexName = isHk ? '恒生指数' : '上证指数';
  if (!filter || filter.close === null || filter.close === undefined) {
    return (
      <BoardCard icon={<Shield size={16} className="text-slate-400" />} title="大盘均线过滤" subtitle={`${indexName} vs 20日均线`}>
        <div className="flex justify-center py-2">
          <Text className="text-xs text-slate-400">暂无指数数据</Text>
        </div>
      </BoardCard>
    );
  }
  const below = filter.below_ma20;
  const ma20 = filter.mavg.ma20 !== null && filter.mavg.ma20 !== undefined ? Number(filter.mavg.ma20).toFixed(2) : '—';
  return (
    <BoardCard
      icon={<Shield size={16} className={below ? 'text-rose-500' : 'text-emerald-500'} />}
      title="大盘均线过滤"
      subtitle={`${filter.ref_date} · ${isHk ? '恒生指数' : '上证指数'}`}
      right={below ? (
        <Tag color="red" className="m-0 rounded-full text-xs font-black px-3 py-0.5">跌破 MA20 · 强制空仓</Tag>
      ) : (
        <Tag color="green" className="m-0 rounded-full text-xs font-black px-3 py-0.5">MA20 上方 · 可持仓</Tag>
      )}
    >
      <div className="flex flex-wrap items-center gap-x-8 gap-y-3">
        <div className="flex items-baseline gap-2">
          <Text className="text-xs text-slate-400 font-bold">收盘</Text>
          <Text className="font-black font-mono text-xl text-slate-800">{Number(filter.close).toFixed(2)}</Text>
        </div>
        <div className="flex items-baseline gap-2">
          <Text className="text-xs text-slate-400 font-bold">MA20</Text>
          <Text className={clsx('font-black font-mono text-xl', below ? 'text-rose-600' : 'text-emerald-600')}>{ma20}</Text>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {(['ma5', 'ma10', 'ma20', 'ma30', 'ma60'] as const).map(k => (
            <Tooltip key={k} title={`MA${k.replace('ma', '')}`}>
              <span className="rounded-lg bg-slate-50 border border-slate-100 px-2 py-1 text-xs font-mono text-slate-500">
                MA{k.replace('ma', '')}: {filter.mavg[k] !== null && filter.mavg[k] !== undefined ? Number(filter.mavg[k]).toFixed(0) : '—'}
              </span>
            </Tooltip>
          ))}
        </div>
        <div className="flex-1 min-w-[160px]">
          <div className="h-2 rounded-full bg-slate-100 overflow-hidden">
            <div
              className={clsx('h-full rounded-full', below ? 'bg-rose-400' : 'bg-emerald-400')}
              style={{ width: '100%' }}
            />
          </div>
          <Text className={clsx('block text-xs font-bold mt-1', below ? 'text-rose-500' : 'text-emerald-600')}>
            {below ? '指数在 MA20 下方，模型信号不可靠，强制空仓' : '指数在 MA20 上方，可正常按信号操作'}
          </Text>
        </div>
      </div>
    </BoardCard>
  );
}

/** 行业信号强度卡片：Top20 → 各行业 Top1 → avg Top1 + 强行业数 + 入场判断 */
function IndustrySignalCard({
  stats, avg, strongCount, signal,
}: {
  stats?: IndustryTop1Stat[];
  avg?: number | null;
  strongCount?: number;
  signal?: InferenceRunRecord['market_signal'];
}) {
  const top = (stats || []).slice(0, 10);
  const strong = strongCount ?? 0;
  const signalLabel = signal?.label || '—';
  const signalColor = signal?.entry_signal === 'strong' ? 'red' : signal?.entry_signal === 'empty' ? 'default' : 'orange';
  // 强行业阈值：融合模型(score_scale=wide)时用后端自适应阈值，普通模型保持 0.10
  const strongThr = signal?.score_scale === 'wide' && signal?.strong_threshold != null
    ? Number(signal.strong_threshold) : 0.10;
  const entryThr = signal?.score_scale === 'wide' && signal?.entry_threshold != null
    ? Number(signal.entry_threshold) : 0.09;
  const emptyThr = signal?.score_scale === 'wide' && signal?.empty_threshold != null
    ? Number(signal.empty_threshold) : 0.06;
  return (
    <BoardCard
      icon={<BarChart3 size={16} className="text-indigo-500" />}
      title="行业信号强度"
      subtitle="每天推理后取 Top20 股票，按申万128行业分组统计各行业 Top1 分数"
      right={<Tag color={signalColor} className="m-0 rounded-full text-xs font-black px-3 py-0.5">{signalLabel}</Tag>}
    >
      {/* 顶部 3 个关键指标 */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-4">
        <div className="rounded-2xl bg-slate-50 border border-slate-100 px-4 py-3 text-center">
          <Text className="block text-xs text-slate-400 font-bold mb-1">行业 avg Top1</Text>
          <Text className={clsx('block font-black font-mono text-2xl', avg !== null && avg !== undefined && avg >= entryThr ? 'text-rose-600' : 'text-slate-800')}>
            {fmt4(avg)}
          </Text>
          <Text className="block text-xs text-slate-400 mt-0.5">≥{entryThr.toFixed(2)} 可入场 · ≥{emptyThr.toFixed(2)} 空仓线</Text>
        </div>
        <div className="rounded-2xl bg-slate-50 border border-slate-100 px-4 py-3 text-center">
          <Text className="block text-xs text-slate-400 font-bold mb-1">强行业数 (Top1 ≥ {strongThr.toFixed(2)})</Text>
          <Text className={clsx('block font-black font-mono text-2xl', strong >= 2 ? 'text-rose-600' : 'text-slate-800')}>{strong}</Text>
          <Text className="block text-xs text-slate-400 mt-0.5">≥2 个可参与 · ≥3 个有行情</Text>
        </div>
        <div className="rounded-2xl bg-slate-50 border border-slate-100 px-4 py-3 text-center">
          <Text className="block text-xs text-slate-400 font-bold mb-1">覆盖行业数</Text>
          <Text className="block font-black font-mono text-2xl text-slate-800">{stats?.length ?? 0}</Text>
          <Text className="block text-xs text-slate-400 mt-0.5">Top20 涉及的申万行业</Text>
        </div>
      </div>

      {/* 行业 Top1 列表：两列居中排布，避免宽屏下偏左显乱 */}
      <div className="mx-auto max-w-5xl flex flex-wrap justify-center gap-2.5">
        {top.map(x => (
          <div key={x.industry} className="w-full sm:w-[calc(50%-5px)] flex items-center justify-between gap-3 rounded-2xl bg-slate-50/60 border border-slate-100 px-4 py-3">
            <div className="min-w-0 flex items-center gap-3">
              <span className={clsx('w-1.5 h-1.5 rounded-full flex-shrink-0', Number(x.top1_score) >= strongThr ? 'bg-rose-500' : 'bg-slate-300')} />
              <div className="min-w-0">
                <Text className="block text-xs font-black text-slate-700 truncate">{x.industry}</Text>
                <Text className="block text-xs font-mono text-slate-400 truncate">{x.top1_symbol} · {x.top1_name}</Text>
              </div>
            </div>
            <Text className={clsx('font-black font-mono text-sm flex-shrink-0', Number(x.top1_score) >= strongThr ? 'text-rose-600' : 'text-slate-600')}>
              {Number(x.top1_score).toFixed(4)}
            </Text>
          </div>
        ))}
      </div>
      {(!stats || stats.length === 0) && (
        <div className="flex justify-center py-2">
          <Text className="text-xs text-slate-400">暂无行业数据</Text>
        </div>
      )}
    </BoardCard>
  );
}

/** 行业轮动提示：强行业数 + 主线方向 */
function RotationHint({ stats, strongCount, signal }: {
  stats?: IndustryTop1Stat[];
  strongCount?: number;
  signal?: InferenceRunRecord['market_signal'];
}) {
  const strongThr = signal?.score_scale === 'wide' && signal?.strong_threshold != null
    ? Number(signal.strong_threshold) : 0.10;
  const strong = (stats || []).filter(x => Number(x.top1_score) >= strongThr);
  const n = strongCount ?? 0;
  return (
    <BoardCard
      icon={<Repeat size={16} className="text-purple-500" />}
      title="行业轮动"
      subtitle={`看哪些行业 Top1 ≥ ${strongThr.toFixed(2)} 出现天数最多，就是当前主线`}
      right={
        <Tag color={n >= 3 ? 'red' : n >= 2 ? 'orange' : 'default'} className="m-0 rounded-full text-xs font-black px-3 py-0.5">
          强行业 {n} 个
        </Tag>
      }
    >
      <div className="flex items-start gap-4 mb-3">
        <div className="flex-1 rounded-2xl bg-slate-50 border border-slate-100 px-3.5 py-2.5">
          <Text className={clsx('block text-xs font-bold', n >= 3 ? 'text-rose-600' : n >= 2 ? 'text-amber-600' : 'text-slate-500')}>
            {n >= 3 ? '≥3 个强行业，有行情可做' : n >= 2 ? '2 个强行业，震荡可轻仓' : '≤1 个强行业，应空仓'}
          </Text>
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        {strong.slice(0, 8).map(x => (
          <Tag key={x.industry} color="red" className="m-0 rounded-full text-xs font-bold px-3 py-0.5">
            {x.industry} {Number(x.top1_score).toFixed(3)}
          </Tag>
        ))}
        {strong.length === 0 && (
          <div className="flex w-full justify-center py-2">
            <Text className="text-xs text-slate-400">暂无行业 Top1 ≥ {strongThr.toFixed(2)}</Text>
          </div>
        )}
      </div>
    </BoardCard>
  );
}

/** 避坑清单 */
const PITFALLS: Array<{ key: string; label: string; detail: string }> = [
  { key: 'chase', label: '追高分', detail: '个股 0.12-0.14 是死亡区，0 胜率，均亏 -12%' },
  { key: 'star', label: '科创板高分', detail: '科创板分数 >0.15 胜率仅 47%' },
  { key: 'up', label: '连续上升', detail: 'T-2→T-1→T 连续上升 = 过热，不追（强市除外）' },
  { key: 'weak', label: '弱市硬做', detail: '行业 avgTop1 < 0.06 时空仓，不赌' },
  { key: 'fake', label: '假信号区', detail: '个股 0.10-0.11 必须配合行业确认（avgTop1 ≥ 0.09）' },
  { key: 'sell', label: '开盘卖出', detail: '收盘卖全面优于开盘卖' },
  { key: 'hold', label: '持有太久', detail: '5 天开始下降，6 天以上亏钱' },
  { key: 'single', label: '单吊一只', detail: '每天选 3-5 只分散，避免单只爆雷' },
  { key: 'crash', label: '崩盘追高', detail: '模型暴跌时给高分是反向信号，须用大盘均线过滤' },
  { key: 'limit', label: '跌停卖不出', detail: '止损 5% 在跌停时无法执行，实际亏损可能远超 5%' },
];

function PitfallCard({ marketSignal, belowMa20, fakeCount }: {
  marketSignal?: InferenceRunRecord['market_signal'];
  belowMa20?: boolean;
  fakeCount?: number;
}) {
  const active: string[] = [];
  if (marketSignal?.entry_signal === 'empty' || marketSignal?.entry_signal === 'weak') active.push('weak');
  if (belowMa20) active.push('crash');
  if (fakeCount && fakeCount > 0) active.push('fake');
  return (
    <BoardCard
      icon={<AlertTriangle size={16} className="text-amber-500" />}
      title="避坑清单"
      subtitle="高亮项为当前批次已触发的风险"
      right={active.length > 0 && <Tag color="warning" className="m-0 rounded-full text-xs font-black px-3 py-0.5">{active.length} 项当前触发</Tag>}
    >
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
        {PITFALLS.map(p => {
          const isActive = active.includes(p.key);
          return (
            <Tooltip key={p.key} title={p.detail}>
              <div className={clsx('rounded-2xl border px-3 py-2.5 cursor-default transition-all',
                isActive
                  ? 'border-amber-200 bg-amber-50'
                  : 'border-slate-100 bg-slate-50/60')}>
                <Text className={clsx('block text-xs font-black', isActive ? 'text-amber-600' : 'text-slate-500')}>{p.label}</Text>
                <Text className="block text-[11px] text-slate-400 mt-0.5 leading-snug">{p.detail}</Text>
              </div>
            </Tooltip>
          );
        })}
      </div>
    </BoardCard>
  );
}

/** 负分分析卡片：做空候选 / 错杀候选 / 负分分布 / 行业差异 */
function NegativeAnalysisCard({ na, isHk = false }: { na?: NegativeAnalysis; isHk?: boolean }) {
  if (!na || na.negative_count === 0) return null;
  const tierColor: Record<string, string> = {
    微盘: 'text-rose-600 bg-rose-50 border-rose-100',
    小盘: 'text-orange-600 bg-orange-50 border-orange-100',
    中盘: 'text-amber-600 bg-amber-50 border-amber-100',
    大盘: 'text-blue-600 bg-blue-50 border-blue-100',
    超大盘: 'text-indigo-600 bg-indigo-50 border-indigo-100',
    未知: 'text-slate-500 bg-slate-50 border-slate-100',
  };
  return (
    <BoardCard
      icon={<ArrowDownRight size={16} className="text-rose-500" />}
      title="负分分析"
      subtitle={isHk ? "做空/回避决策矩阵 · 小市值+低分做空 · 高市值负分常被错杀" : "做空/回避决策矩阵 · 微盘+低分做空 · 大盘负分常被错杀"}
      right={
        <div className="flex items-center gap-2">
          <Tag color="red" className="m-0 rounded-full text-xs font-black px-3 py-0.5">负分 {na.negative_count} 只 ({na.negative_pct}%)</Tag>
          {na.extreme_neg_count > 0 && (
            <Tag className="m-0 rounded-full border-0 bg-rose-100 text-rose-700 font-bold text-xs px-3 py-0.5">极端 ≤-0.20 ×{na.extreme_neg_count}</Tag>
          )}
        </div>
      }
    >
      {/* 负分区间分布 */}
      <div className="grid grid-cols-3 gap-3 mb-4">
        {Object.entries(na.neg_buckets || {}).map(([label, count]) => (
          <div key={label} className="rounded-2xl bg-slate-50 border border-slate-100 px-4 py-3">
            <Text className="block text-xs text-slate-400 font-bold mb-1">{label}</Text>
            <Text className="block font-black font-mono text-2xl text-slate-800">{count}</Text>
          </div>
        ))}
      </div>

      {/* 做空候选 + 错杀候选 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
        <div className="rounded-2xl border border-rose-100 bg-rose-50/50 px-4 py-3">
          <div className="flex items-center justify-between mb-2">
            <Text className="text-xs font-black text-rose-600">做空候选（微盘/小盘 + ≤-0.15）</Text>
            <Text className="font-black font-mono text-sm text-rose-600">{na.short_candidates_count}</Text>
          </div>
          {na.short_candidates_count === 0 ? (
            <div className="flex justify-center py-2">
              <Text className="text-xs text-slate-400">本批次无微盘/小盘强负分股票</Text>
            </div>
          ) : (
            <div className="space-y-1.5">
              {na.short_candidates.slice(0, 6).map(c => (
                <div key={c.symbol} className="flex items-center justify-between gap-2 rounded-xl bg-white/70 border border-rose-100 px-2.5 py-1.5">
                  <div className="min-w-0">
                    <Text className="block text-xs font-black text-slate-700 truncate">{c.name}</Text>
                    <Text className="block text-[11px] font-mono text-slate-400 truncate">{c.symbol} · {c.tier}</Text>
                  </div>
                  <Text className="font-black font-mono text-xs text-rose-600 flex-shrink-0">{c.score.toFixed(4)}</Text>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="rounded-2xl border border-blue-100 bg-blue-50/50 px-4 py-3">
          <div className="flex items-center justify-between mb-2">
            <Text className="text-xs font-black text-blue-600">{isHk ? "错杀候选（高市值负分）" : "错杀候选（大盘/超大盘负分）"}</Text>
            <Text className="font-black font-mono text-sm text-blue-600">{na.mistake_candidates_count}</Text>
          </div>
          {na.mistake_candidates_count === 0 ? (
            <div className="flex justify-center py-2">
              <Text className="text-xs text-slate-400">本批次无大盘/超大盘负分</Text>
            </div>
          ) : (
            <div className="space-y-1.5">
              {na.mistake_candidates.slice(0, 6).map(c => (
                <div key={c.symbol} className="flex items-center justify-between gap-2 rounded-xl bg-white/70 border border-blue-100 px-2.5 py-1.5">
                  <div className="min-w-0">
                    <Text className="block text-xs font-black text-slate-700 truncate">{c.name}</Text>
                    <Text className="block text-[11px] font-mono text-slate-400 truncate">{c.symbol} · {c.tier} · {c.industry}</Text>
                  </div>
                  <Text className="font-black font-mono text-xs text-blue-600 flex-shrink-0">{c.score.toFixed(4)}</Text>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 市值分档负分分布 */}
      <div className="rounded-2xl bg-slate-50/60 border border-slate-100 px-4 py-3 mb-4">
        <Text className="block text-xs font-black text-slate-400 uppercase tracking-wide mb-2">负分 × 市值分布</Text>
        <div className="flex flex-wrap gap-2">
          {Object.entries(na.neg_by_tier || {}).map(([tier, count]) => (
            <Tag key={tier} className={clsx('m-0 rounded-full border px-3 py-0.5 text-xs font-black', tierColor[tier] || tierColor.未知)}>
              {tier} {count}
            </Tag>
          ))}
        </div>
      </div>

      {/* 行业差异：做空首选 vs 抗跌 */}
      {(na.short_industries.length > 0 || na.resistant_industries.length > 0) && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div>
            <Text className="block text-xs font-black text-slate-500 mb-2">负分集中行业（下跌持续 · 做空参考）</Text>
            <div className="flex flex-wrap gap-2">
              {na.short_industries.slice(0, 6).map(x => (
                <Tag key={x.industry} className="m-0 rounded-full border-0 bg-rose-50 text-rose-600 font-bold text-xs px-3 py-0.5">
                  {x.industry} ×{x.count}
                </Tag>
              ))}
            </div>
          </div>
          <div>
            <Text className="block text-xs font-black text-slate-500 mb-2">抗跌行业（负分常错杀 · 银行/半导体）</Text>
            <div className="flex flex-wrap gap-2">
              {na.resistant_industries.slice(0, 6).map(x => (
                <Tag key={x.industry} className="m-0 rounded-full border-0 bg-emerald-50 text-emerald-600 font-bold text-xs px-3 py-0.5">
                  {x.industry} ×{x.count}
                </Tag>
              ))}
              {na.resistant_industries.length === 0 && (
                <div className="flex w-full justify-center py-2">
                  <Text className="text-xs text-slate-400">无匹配抗跌行业</Text>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </BoardCard>
  );
}

export const StrategyDashboard: React.FC<Props & { market?: string }> = ({
  summary,
  market = 'CN',
}) => {
  const isHk = market === 'HK';
  const maFilter = summary.market_ma_filter;
  const maCard = maFilter ? <MarketMAFilterCard filter={maFilter} isHk={isHk} /> : null;
  return (
    <div className="space-y-4">
      {/* 第一行：市场总览（大盘均线 + 行业轮动） */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {maCard}
        <RotationHint stats={summary.industry_top1} strongCount={summary.strong_industry_count} signal={summary.market_signal} />
      </div>

      {/* 行业信号强度（整宽，内部两列） */}
      <IndustrySignalCard
        stats={summary.industry_top1}
        avg={summary.industry_avg_top1}
        strongCount={summary.strong_industry_count}
        signal={summary.market_signal}
      />

      {/* 负分分析 */}
      <NegativeAnalysisCard isHk={isHk} na={summary.negative_analysis} />

      {/* 避坑清单 */}
      <PitfallCard
        marketSignal={summary.market_signal}
        belowMa20={maFilter?.below_ma20}
        fakeCount={summary.fake_signal_count}
      />
    </div>
  );
};
