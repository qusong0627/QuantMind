import React, { useState } from 'react';
import { ChevronDown, ChevronUp, Stethoscope } from 'lucide-react';
import type { RegimeResponse } from '../services/regimeService';
import { isNum } from './panorama/format';

type Verdict = '有效' | '弱有效' | '仅状态' | '无效' | '数据缺口';

const VERDICT_TONE: Record<Verdict, string> = {
  有效: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  弱有效: 'border-amber-200 bg-amber-50 text-amber-700',
  仅状态: 'border-amber-200 bg-amber-50 text-amber-700',
  无效: 'border-slate-200 bg-slate-50 text-slate-500',
  数据缺口: 'border-rose-200 bg-rose-50 text-rose-600',
};

function layerMetric(data: RegimeResponse, key: string, label: string): string | number | null {
  const layer = data.layers.find((l) => l.key === key);
  const metric = layer?.metrics.find((m) => m.label === label);
  return metric?.value ?? null;
}

function fmtPct(v: number | null | undefined, digits = 1): string {
  return isNum(v) ? `${v > 0 ? '+' : ''}${v.toFixed(digits)}%` : '—';
}

function fmtNum(v: number | null | undefined, digits = 2): string {
  return isNum(v) ? v.toFixed(digits) : '—';
}

/** 指标体检：把“能信的 / 只能描述 / 已证伪 / 没数据”分开，防止把描述性指标当预测。 */
export function IndicatorHealthCard({ data }: { data: RegimeResponse }): React.ReactElement {
  const e = data.events;
  const shortUp = layerMetric(data, 'short', '收盘涨家');
  const cheap = layerMetric(data, 'long', 'PB便宜分位');
  const rows: Array<{
    name: string;
    current: string;
    verdict: Verdict;
    how: string;
  }> = [
    {
      name: '10:10 广度',
      current: isNum(e?.breadth_1010) ? `${e.breadth_1010.toFixed(1)}%` : '—',
      verdict: '有效',
      how: '当日定基调：<30% 当日 −0.20%（t−2.6），>60% +0.25%（t4.6）；只做执行，不做隔夜多头。',
    },
    {
      name: '10:10 Q1–Q5',
      current: data.stock_breadth?.available && isNum(data.stock_breadth.q1_minus_q5_after)
        ? `Q1-Q5 ${data.stock_breadth.q1_minus_q5_after > 0 ? '+' : ''}${data.stock_breadth.q1_minus_q5_after.toFixed(2)}pp`
        : '—',
      verdict: '弱有效',
      how: '个股执行层：强广度日 Q1（10:10 最弱）收盘跑赢 Q5（+0.41% vs +0.20%）；弱广度日不接飞刀。热集样本只看形状。',
    },
    {
      name: 'MCL 5日变化',
      current: fmtNum(e?.mcl_chg5),
      verdict: '弱有效',
      how: '当日动能确认：扩张 +0.16%（t4.2），收缩 −0.08%（t−2.0）；是确认不是提前预言。',
    },
    {
      name: 'TRIN_量',
      current: fmtNum(e?.trin_vol),
      verdict: '有效',
      how: '只做投降确认：>2 时 20 日 +1.4~+4.0%（t2.7~4.1）；TRIN 低不等于买盘强。',
    },
    {
      name: '额价差',
      current: isNum(e?.amount_gap_pp)
        ? `${e.amount_gap_pp > 0 ? '+' : ''}${e.amount_gap_pp.toFixed(1)}pp`
        : '—',
      verdict: '有效',
      how: '40 日风险闸：>20pp 时 40 日 −1.6~−2.5pp（t−2.4~−3.1）；是广度与 TRIN 的函数，不当独立投票。',
    },
    {
      name: 'ADL 背离',
      current: e?.adl_divergence ? '有' : '无',
      verdict: '有效',
      how: '顶部风险：指数 250 日新高且 ADL 未新高，20 日 −2.01%（t−2.18）；后半衰减，只做减仓提示。',
    },
    {
      name: '收盘涨家',
      current: isNum(shortUp) ? `${shortUp.toFixed(1)}%` : '—',
      verdict: '弱有效',
      how: '短期反抽/延续：<15% 次日 +0.13~0.22%，>80% 20 日 +0.88~1.58%；隔夜腿长期为负。',
    },
    {
      name: 'PB 便宜分位',
      current: isNum(cheap) ? cheap.toFixed(3) : '—',
      verdict: '有效',
      how: '长期赔率：便宜前 20% → 250 日 +8~10%；底部组合只有 7 段，样本少。',
    },
    {
      name: '成交额 HHI',
      current: '—',
      verdict: '仅状态',
      how: '40 日风险提示：前 10% 时 40 日 −3.84%（t−3.60），但后半衰减；不单独用。',
    },
    {
      name: 'NH-NL / 板块扩散 / 两融',
      current: '—',
      verdict: '无效',
      how: '全样本 20/40 日不显著；只做描述，不进入评分。',
    },
    {
      name: '北向 / ERP / 期权 IV',
      current: '—',
      verdict: '数据缺口',
      how: '北向 2024-08 起停更；本地无 10Y 历史与 A 股期权链，不臆测。',
    },
  ];
  const [open, setOpen] = useState(false);
  const counts = rows.reduce<Record<string, number>>((acc, r) => {
    acc[r.verdict] = (acc[r.verdict] ?? 0) + 1;
    return acc;
  }, {});
  const summary = `有效 ${counts['有效'] ?? 0} · 弱有效 ${counts['弱有效'] ?? 0} · 仅状态 ${counts['仅状态'] ?? 0} · 无效 ${counts['无效'] ?? 0} · 缺口 ${counts['数据缺口'] ?? 0}`;

  return (
    <div className="rounded-2xl border border-slate-200/80 bg-white px-4 py-3 shadow-sm">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full flex-wrap items-center justify-between gap-2 text-left"
      >
        <div className="flex items-center gap-1.5">
          <Stethoscope className="h-3.5 w-3.5 text-purple-600" />
          <h3 className="text-xs font-extrabold text-slate-800">指标体检 · 哪些能信</h3>
          <span className="rounded-full border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[9px] font-bold text-slate-500">
            2016–2026 实测
          </span>
        </div>
        <span className="flex items-center gap-2 text-[9px] text-slate-400">
          <span className="font-mono">{summary}</span>
          <span className="hidden sm:inline">有效 ≠ 扣费后可用</span>
          {open ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        </span>
      </button>
      {open && (
        <div className="mt-2 overflow-x-auto">
          <table className="w-full min-w-[720px] border-collapse text-left">
            <thead>
              <tr className="text-[9px] font-bold uppercase tracking-wide text-slate-400">
                <th className="border-b border-slate-100 py-1.5 pr-3">指标</th>
                <th className="border-b border-slate-100 py-1.5 pr-3">当前</th>
                <th className="border-b border-slate-100 py-1.5 pr-3">判决</th>
                <th className="border-b border-slate-100 py-1.5">怎么用 / 证据</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.name} className="align-top text-[10px]">
                  <td className="border-b border-slate-50 py-1.5 pr-3 font-bold text-slate-700">{r.name}</td>
                  <td className="border-b border-slate-50 py-1.5 pr-3 font-mono font-bold text-slate-800">
                    {r.current}
                  </td>
                  <td className="border-b border-slate-50 py-1.5 pr-3">
                    <span className={`inline-flex rounded-md border px-1.5 py-0.5 text-[9px] font-bold ${VERDICT_TONE[r.verdict]}`}>
                      {r.verdict}
                    </span>
                  </td>
                  <td className="border-b border-slate-50 py-1.5 leading-relaxed text-slate-500">{r.how}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1.5 text-[9px] text-slate-400">有效 ≠ 扣费后可用；负结果也照实标注。</p>
        </div>
      )}
    </div>
  );
}
