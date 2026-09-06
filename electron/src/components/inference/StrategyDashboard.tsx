import React from 'react';
import { Tag, Typography, Tooltip } from 'antd';
import { clsx } from 'clsx';
import {
  BarChart3, Shield, Repeat,
} from 'lucide-react';
import type {
  InferenceRunRecord, IndustryTop1Stat, MarketMAFilter,
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
function MarketMAFilterCard({ filter }: { filter?: MarketMAFilter }) {
  if (!filter || filter.close === null || filter.close === undefined) {
    return (
      <BoardCard icon={<Shield size={16} className="text-slate-400" />} title="大盘均线过滤" subtitle="上证指数 vs 20日均线">
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
      subtitle={`${filter.ref_date} · 上证指数`}
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
            {below ? '指数在 MA20 下方' : '指数在 MA20 上方'}
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
  // 强行业阈值：融合模型(score_scale=wide)时用后端自适应阈值，普通模型保持 0.10
  const strongThr = signal?.score_scale === 'wide' && signal?.strong_threshold != null
    ? Number(signal.strong_threshold) : 0.10;
  const entryThr = signal?.score_scale === 'wide' && signal?.entry_threshold != null
    ? Number(signal.entry_threshold) : 0.09;
  return (
    <BoardCard
      icon={<BarChart3 size={16} className="text-indigo-500" />}
      title="行业信号强度"
      subtitle="每天推理后取 Top20 股票，按申万128行业分组统计各行业 Top1 分数"
    >
      {/* 顶部 3 个关键指标 */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-4">
        <div className="rounded-2xl bg-slate-50 border border-slate-100 px-4 py-3 text-center">
          <Text className="block text-xs text-slate-400 font-bold mb-1">行业 avg Top1</Text>
          <Text className={clsx('block font-black font-mono text-2xl', avg !== null && avg !== undefined && avg >= entryThr ? 'text-rose-600' : 'text-slate-800')}>
            {fmt4(avg)}
          </Text>
          <Text className="block text-xs text-slate-400 mt-0.5">阈值 ≥{entryThr.toFixed(2)}</Text>
        </div>
        <div className="rounded-2xl bg-slate-50 border border-slate-100 px-4 py-3 text-center">
          <Text className="block text-xs text-slate-400 font-bold mb-1">强行业数 (Top1 ≥ {strongThr.toFixed(2)})</Text>
          <Text className={clsx('block font-black font-mono text-2xl', strong >= 2 ? 'text-rose-600' : 'text-slate-800')}>{strong}</Text>
          <Text className="block text-xs text-slate-400 mt-0.5">Top1 ≥ 阈值 的行业记数</Text>
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
      subtitle={`统计各行业 Top1 ≥ ${strongThr.toFixed(2)} 的出现天数`}
      right={
        <Tag color={n >= 3 ? 'red' : n >= 2 ? 'orange' : 'default'} className="m-0 rounded-full text-xs font-black px-3 py-0.5">
          强行业 {n} 个
        </Tag>
      }
    >
      <div className="flex items-start gap-4 mb-3">
        <div className="flex-1 rounded-2xl bg-slate-50 border border-slate-100 px-3.5 py-2.5">
          <Text className={clsx('block text-xs font-bold', n >= 3 ? 'text-rose-600' : n >= 2 ? 'text-amber-600' : 'text-slate-500')}>
            {n >= 3 ? '强行业数量较多' : n >= 2 ? '强行业数量中等' : '强行业数量较少'}
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


export const StrategyDashboard: React.FC<Props> = ({ summary }) => {
  const maFilter = summary.market_ma_filter;
  return (
    <div className="space-y-4">
      {/* 第一行：市场总览（大盘均线 + 行业轮动） */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <MarketMAFilterCard filter={maFilter} />
        <RotationHint stats={summary.industry_top1} strongCount={summary.strong_industry_count} signal={summary.market_signal} />
      </div>

      {/* 行业信号强度（整宽，内部两列） */}
      <IndustrySignalCard
        stats={summary.industry_top1}
        avg={summary.industry_avg_top1}
        strongCount={summary.strong_industry_count}
        signal={summary.market_signal}
      />
    </div>
  );
};
