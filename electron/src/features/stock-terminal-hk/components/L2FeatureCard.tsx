/** L2 微观结构因子卡：预测日前一交易日的 14 个推荐因子（值 + 全市场百分位）。
 *  hover 因子芯片显示该特征含义（desc）。 */

import { Tooltip } from 'antd';
import { Layers } from 'lucide-react';

/** 因子值格式化：量纲差异大，用有效位数自适应 */
export function fmtFactor(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '--';
  const a = Math.abs(v);
  if (a >= 100) return v.toFixed(1).replace(/(\.[1-9]?)0+$/, '$1');
  if (a >= 1) return v.toFixed(3).replace(/\.?0+$/, '');
  return v.toFixed(4);
}

/** 因子强度徽标：14 因子均为正向 alpha，百分位越高信号越强 */
export function StrengthBadge({ pct }: { pct: number | null }) {
  if (pct == null) {
    return <span className="text-[9px] leading-none px-1.5 py-0.5 rounded bg-slate-50 text-slate-300 font-bold shrink-0">--</span>;
  }
  if (pct >= 0.8) {
    return <span className="text-[9px] leading-none px-1.5 py-0.5 rounded bg-rose-100 text-rose-600 font-black shrink-0">强·{Math.round(pct * 100)}%</span>;
  }
  if (pct >= 0.5) {
    return <span className="text-[9px] leading-none px-1.5 py-0.5 rounded bg-amber-100 text-amber-600 font-bold shrink-0">中·{Math.round(pct * 100)}%</span>;
  }
  return <span className="text-[9px] leading-none px-1.5 py-0.5 rounded bg-slate-100 text-slate-400 font-bold shrink-0">{Math.round(pct * 100)}%</span>;
}

export interface L2FeatureData {
  feature_date: string;
  factors: {
    name: string;
    label: string;
    category: string;
    icir: number;
    value: number | null;
    pct_rank: number | null;
    desc?: string;
  }[];
}

interface Props {
  l2: L2FeatureData | null;
  signalDate?: string | null;
}

/** 单因子卡片：名称+强度徽标 / 值+ICIR / 全市场百分位进度条，hover 看含义 */
function FactorTile({ f }: { f: L2FeatureData['factors'][number] }) {
  const pct = f.pct_rank;
  const barColor = pct == null ? 'bg-slate-200' : pct >= 0.8 ? 'bg-rose-400' : pct >= 0.5 ? 'bg-amber-400' : 'bg-slate-300';
  const content = (
    <div className="text-[10px] leading-relaxed max-w-60">
      <div className="font-black text-slate-800">
        {f.label} <span className="font-normal text-slate-400">· {f.category} · ICIR {f.icir}</span>
      </div>
      <div className="mt-0.5 text-slate-600">{f.desc || '暂无该因子说明'}</div>
      <div className="mt-1 text-slate-400">{`值 ${fmtFactor(f.value)} · 全市场百分位 ${pct != null ? Math.round(pct * 100) + '%' : '--'}（越高=信号越强）`}</div>
    </div>
  );
  return (
    <Tooltip title={content} placement="top" mouseEnterDelay={0.15}>
      <div className="rounded-xl border border-slate-100 bg-slate-50/60 p-2.5 hover:border-slate-200 hover:bg-slate-50 transition-colors cursor-help">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] font-bold text-slate-600 truncate">{f.label}</span>
          <StrengthBadge pct={pct} />
        </div>
        <div className="mt-1.5 flex items-end justify-between gap-2">
          <span className="text-base font-black text-slate-900 leading-none">{fmtFactor(f.value)}</span>
          <span className="text-[10px] text-slate-400 font-bold shrink-0">ICIR {f.icir}</span>
        </div>
        <div className="mt-1.5 h-1 rounded-full bg-slate-200/70 overflow-hidden">
          <div className={`h-full rounded-full ${barColor}`} style={{ width: `${pct != null ? Math.round(pct * 100) : 0}%` }} />
        </div>
      </div>
    </Tooltip>
  );
}

export function L2FeatureCard({ l2, signalDate }: Props) {
  // 因子按类别分组（保留推荐顺序）
  const catOrder = Array.from(new Set((l2?.factors ?? []).map(f => f.category)));
  const byCat = catOrder.map(cat => ({
    cat,
    items: (l2?.factors ?? []).filter(f => f.category === cat),
  }));

  return (
    <div className="flex flex-col gap-3">
      {/* 标题卡：名称 + 预测日/特征日 + 口径说明 */}
      <div className="bg-white/70 rounded-2xl border border-slate-100 p-3">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-1.5">
            <span className="w-6 h-6 rounded-lg bg-rose-50 border border-rose-100 flex items-center justify-center text-rose-500">
              <Layers className="w-3.5 h-3.5" />
            </span>
            <span className="text-xs font-black text-slate-700">L2 微观结构因子</span>
          </div>
          <span className="text-[10px] font-bold text-slate-400">
            {'预测日 '}<b className="text-slate-600">{signalDate ?? '--'}</b>
            <span className="mx-1 text-slate-300">→</span>
            {'特征日 '}<b className="text-slate-600">{l2?.feature_date ?? '--'}</b>
          </span>
        </div>
        <p className="mt-2 text-[10px] text-slate-400 leading-relaxed">
          14 个推荐因子（VPIN / 时段 / 资金流等微观结构，单因子 ICIR 0.16~0.56）；徽标与进度条为当日全市场百分位，越高信号越强（正向）。悬停因子卡查看含义。
        </p>
      </div>

      {l2 ? (
        byCat.map(({ cat, items }) => (
          <div key={cat} className="bg-white/70 rounded-2xl border border-slate-100 p-3">
            <div className="text-[11px] font-bold text-slate-500 mb-2">{cat}</div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {items.map(f => <FactorTile key={f.name} f={f} />)}
            </div>
          </div>
        ))
      ) : (
        <div className="bg-white/70 rounded-2xl border border-slate-100 p-6 text-center text-[11px] text-slate-400">
          该股无推理信号，无预测日前日 L2 特征
        </div>
      )}
    </div>
  );
}