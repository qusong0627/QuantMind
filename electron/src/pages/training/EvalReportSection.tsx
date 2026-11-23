/**
 * 模型评估报告：预测强弱诊断（RankIC 稳健性 / 十分位分层收益 / 多空组合）。
 *
 * 训练结果页（训练溯源）与模型详情页共用同一实现，避免两处口径漂移。
 */
import React from 'react';
import { Card } from 'antd';
import {
  BarChart as ReBarChart,
  LineChart as ReLineChart,
  ComposedChart,
  Line,
  Bar,
  Legend,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts';
import { clsx } from 'clsx';
import { resolveEvalIcHeadline, type EvalReport } from './trainingUtils';

/** 多空净值倍数格式化：曲线是复利累计倍数，直接按 % 展示会放大成 1e10% 这类荒谬数字。 */
const fmtNavMultiple = (value: number): string => {
  if (!Number.isFinite(value)) return '—';
  if (value >= 1e8) return `${(value / 1e8).toFixed(1)}亿×`;
  if (value >= 1e4) return `${(value / 1e4).toFixed(1)}万×`;
  if (value >= 100) return `${value.toFixed(0)}×`;
  return `${value.toFixed(2)}×`;
};

export const EvalReportSection: React.FC<{ report?: EvalReport }> = ({ report }) => {
  if (!report || report.error) return null;
  const ls: NonNullable<EvalReport['long_short']> = report.long_short || {};
  const groups: NonNullable<EvalReport['groups']> = report.groups || {};
  const groupData = (groups.mean_returns || []).map((v, i) => ({
    group: `G${i + 1}`,
    ret: v == null ? null : Number((v * 100).toFixed(3)),
  }));
  const isRealReturn = report.return_basis === 'label_return';
  const tradability = report.tradability;
  // 多空曲线：后端存累计收益比率（0.05 = +5%）→ 展示净值倍数 + 对数轴。
  // 秩标签口径的曲线是秩空间 cumprod，没有收益/净值语义（实测能到 1e10 量级），
  // 不画图、只给出说明——画出来就是「多空赚了 100 亿倍」的假象。
  const lsCurve = (ls.curve || [])
    .map(([d, v]) => ({ date: String(d).slice(2, 10), nav: Number(v) + 1 }))
    .filter((point) => Number.isFinite(point.nav) && point.nav > 0);
  // 多空净值只在真实收益口径、且有 ≥2 个点时才展示；秩口径没有收益语义，
  // 缺曲线时也直接不渲染整块（不留空图/占位框）。
  const showLongShort = isRealReturn && lsCurve.length >= 2;
  const fmt = (v: number | null | undefined, digits = 2, scale = 1) =>
    v == null || Number.isNaN(v) ? '—' : `${(v * scale).toFixed(digits)}${scale === 100 ? '%' : ''}`;
  const lvl = (v: number | null | undefined, good: number, ok: number) =>
    v == null ? 'text-slate-400' : v >= good ? 'text-emerald-600' : v >= ok ? 'text-amber-600' : 'text-rose-500';
  const basisText = isRealReturn ? '真实未来收益口径' : '秩标签口径（非真实收益）';
  const bySplit = report.by_split || {} as NonNullable<EvalReport['by_split']>;
  const splitLabel: Record<string, string> = { train: '训练', valid: '验证', test: '测试' };
  // 分段固定按 训练→验证→测试 排序（后端 groupby 顺序不稳定，原文直接铺开无法对比）
  const SPLIT_ORDER: Record<string, number> = { train: 0, valid: 1, test: 2 };
  const splitRows = Object.entries(bySplit)
    .sort(([a], [b]) => (SPLIT_ORDER[a] ?? 9) - (SPLIT_ORDER[b] ?? 9));
  const yearRows = report.yearly || [];
  // 分年 IC 用百分点画柱（0.099 → 9.9），ICIR 用右轴折线；阈值线落在 5 / 10
  const yearData = yearRows.map((y) => ({
    year: String(y.year),
    IC: y.mean == null ? null : Number((y.mean * 100).toFixed(3)),
    ICIR: y.icir == null ? null : Number(Number(y.icir).toFixed(3)),
  }));
  // 主展示口径：优先测试段（样本外）——headline 是全窗合计（含训练段，约虚高 1.5 倍）。
  // 无 test 段（老报告）时回落全窗，并在标签上如实标注口径，不让两种口径看起来一样。
  const icHead = resolveEvalIcHeadline(report);
  return (
    <Card
      className="rounded-2xl border-emerald-100"
      size="small"
      title={
        <span className="text-sm font-black text-slate-800">
          模型评估报告
          <span className="ml-2 text-[10px] font-normal text-slate-400">
            {basisText} · {report.n_days ?? '—'} 个交易日 · {Number(report.n_rows || 0).toLocaleString()} 行
          </span>
        </span>
      }
    >
      {!isRealReturn && (
        <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50/60 px-3 py-2 text-[10px] leading-relaxed text-amber-700">
          该报告为<b>秩标签口径</b>：分层/多空是秩空间统计，<b>不是可交易收益</b>，涨跌停可交易性也不适用；
          IC / 胜率仍可用于模型比较。
        </div>
      )}
      {isRealReturn && tradability && tradability.applied === false && (
        <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50/60 px-3 py-2 text-[10px] leading-relaxed text-amber-700">
          该报告<b>未剔除涨跌停</b>（生成早于 2026-11-23 可交易性口径），多空/分层收益可能偏乐观。
        </div>
      )}
      <div className="grid grid-cols-3 gap-2 md:grid-cols-6">
        {/* 染色断点对齐样本外阶梯（backend/scripts/eval/model_card.py 的 score_oos：
            IC 0.04/0.08、ICIR 0.3/0.5）。沿用旧的全窗尺度断点（0.03/0.015）会因
            测试段普遍低于全窗而全部染绿（49 份 CN 报告 test 段 IC 全部 ≥0.05），失去分辨力。 */}
        {[
          { label: `RankIC ${icHead.tag}`, value: fmt(icHead.mean, 4), cls: lvl(icHead.mean, 0.08, 0.04) },
          { label: `ICIR ${icHead.tag}`, value: fmt(icHead.icir, 3), cls: lvl(icHead.icir, 0.5, 0.3) },
          { label: `IC 胜率 ${icHead.tag}`, value: fmt(icHead.winRate, 1, 100), cls: lvl(icHead.winRate, 0.6, 0.5) },
          { label: '分层单调性', value: fmt(groups.monotonicity, 3), cls: lvl(groups.monotonicity, 0.9, 0.6) },
          {
            label: isRealReturn ? '多空夏普' : '多空夏普（秩口径）',
            value: isRealReturn ? fmt(ls.sharpe, 2) : '—',
            cls: isRealReturn ? lvl(ls.sharpe, 1.0, 0.5) : 'text-slate-300',
          },
          {
            label: isRealReturn ? '多空年化' : '多空年化（秩口径）',
            value: isRealReturn ? fmt(ls.ann_return, 1, 100) : '—',
            cls: isRealReturn ? 'text-slate-800' : 'text-slate-300',
          },
        ].map((it) => (
          <div key={it.label} className="rounded-xl border border-emerald-100 bg-white px-2 py-1.5 text-center">
            <div className="text-[9px] font-bold tracking-wider text-slate-400">{it.label}</div>
            <div className={clsx('mt-0.5 text-sm font-black', it.cls)}>{it.value}</div>
          </div>
        ))}
      </div>
      <div className={clsx('mt-3 grid gap-3', showLongShort && 'md:grid-cols-2')}>
        <div className="rounded-xl border border-slate-100 bg-white p-2">
          <div className="mb-1 text-[10px] font-bold text-slate-500">
            {isRealReturn ? '十分位分层平均收益' : '十分位分层平均秩（秩口径）'}
            （G1=预测最弱，G{groups.n_groups || 10}=最强）
          </div>
          <ResponsiveContainer width="100%" height={150}>
            <ReBarChart data={groupData} margin={{ top: 4, right: 4, bottom: 0, left: -18 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
              <XAxis dataKey="group" tick={{ fontSize: 9 }} />
              <YAxis tick={{ fontSize: 9 }} />
              <Tooltip formatter={(v) => [v == null ? '—' : `${v}${isRealReturn ? '%' : ' 秩分'}`, isRealReturn ? '平均收益' : '平均秩']} />
              <Bar dataKey="ret" fill="#10b981" radius={[3, 3, 0, 0]} />
            </ReBarChart>
          </ResponsiveContainer>
        </div>
        {showLongShort && (
          <div className="rounded-xl border border-slate-100 bg-white p-2">
            <div className="mb-1 flex items-center justify-between text-[10px] font-bold text-slate-500">
              <span>多空组合累计净值（Top − Bottom，对数轴 · ×）</span>
              <span className="font-mono text-[9px] font-normal text-slate-400">首点 = 1×</span>
            </div>
            <ResponsiveContainer width="100%" height={150}>
              <ReLineChart data={lsCurve} margin={{ top: 4, right: 4, bottom: 0, left: -14 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                <XAxis dataKey="date" tick={{ fontSize: 9 }} minTickGap={48} />
                <YAxis
                  tick={{ fontSize: 9 }}
                  scale="log"
                  domain={['auto', 'auto']}
                  tickFormatter={(value: number) => fmtNavMultiple(value)}
                />
                <Tooltip formatter={(v) => [fmtNavMultiple(Number(v)), '累计净值']} />
                <ReferenceLine y={1} stroke="#94a3b8" strokeDasharray="2 2" />
                <Line type="monotone" dataKey="nav" stroke="#6366f1" dot={false} strokeWidth={1.6} />
              </ReLineChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>
      {(splitRows.length > 0 || yearRows.length > 0) && (
        <div className="mt-3 grid gap-3 lg:grid-cols-12">
          {/* 分段对比：训练→验证→测试 固定顺序、IC 条形可横比、测试段高亮 */}
          {splitRows.length > 0 && (
            <div className={clsx('rounded-xl border border-slate-100 bg-white p-2', yearRows.length > 0 ? 'lg:col-span-4' : 'lg:col-span-12')}>
              <div className="mb-1 text-[10px] font-bold text-slate-500">分段对比（IC / ICIR / 胜率）</div>
              <div className="space-y-1">
                {splitRows.map(([name, s]) => {
                  const isTest = name === 'test';
                  const icPct = s.mean == null ? null : Math.max(0, Math.min(100, (s.mean / 0.15) * 100));
                  return (
                    <div
                      key={name}
                      className={clsx(
                        'rounded-lg border px-2 py-1.5',
                        isTest ? 'border-emerald-200 bg-emerald-50/60' : 'border-slate-100 bg-slate-50/50',
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className={clsx('flex items-center gap-1 text-[10px] font-black', isTest ? 'text-emerald-700' : 'text-slate-600')}>
                          {splitLabel[name] || name}
                          {isTest && <span className="rounded-full bg-emerald-600 px-1 py-px text-[7px] font-black text-white">样本外</span>}
                        </span>
                        <span className={clsx('font-mono text-[14px] font-black tabular-nums', lvl(s.mean, 0.08, 0.04))}>
                          IC {fmt(s.mean, 4)}
                        </span>
                      </div>
                      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-white/90">
                        <div
                          className={clsx('h-full rounded-full', isTest ? 'bg-emerald-500' : 'bg-indigo-400')}
                          style={{ width: `${icPct ?? 0}%` }}
                        />
                      </div>
                      <div className="mt-1 flex items-center justify-between font-mono text-[9px] font-bold text-slate-500">
                        <span>ICIR {fmt(s.icir, 2)}</span>
                        <span>胜率 {fmt(s.win_rate, 0, 100)}</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
          {/* 分年走势：IC 柱（%）+ ICIR 折线（右轴），阈值线 5%/10% */}
          {yearRows.length > 0 && (
            <div className={clsx('rounded-xl border border-slate-100 bg-white p-2', splitRows.length > 0 ? 'lg:col-span-8' : 'lg:col-span-12')}>
              <div className="mb-1 flex items-center justify-between text-[10px] font-bold text-slate-500">
                <span>分年 IC（柱，%）/ ICIR（线，右轴）</span>
                <span className="text-[9px] font-normal text-slate-400">虚线 5% / 10%</span>
              </div>
              <ResponsiveContainer width="100%" height={170}>
                <ComposedChart data={yearData} margin={{ top: 4, right: 6, bottom: 0, left: -18 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                  <XAxis dataKey="year" tick={{ fontSize: 9 }} />
                  <YAxis yAxisId="ic" tick={{ fontSize: 9 }} tickFormatter={(v: number) => `${v}%`} />
                  <YAxis yAxisId="icir" orientation="right" tick={{ fontSize: 9 }} width={30} />
                  <Tooltip
                    contentStyle={{ borderRadius: 10, fontSize: 11 }}
                    formatter={(value, key) => [
                      key === 'IC' ? `${Number(value).toFixed(2)}%` : Number(value).toFixed(2),
                      key === 'IC' ? 'IC' : 'ICIR',
                    ]}
                  />
                  <Legend wrapperStyle={{ fontSize: 9 }} />
                  <ReferenceLine yAxisId="ic" y={5} stroke="#f59e0b" strokeDasharray="4 3" />
                  <ReferenceLine yAxisId="ic" y={10} stroke="#10b981" strokeDasharray="4 3" />
                  <Bar yAxisId="ic" dataKey="IC" fill="#6366f1" radius={[3, 3, 0, 0]} />
                  <Line yAxisId="icir" type="monotone" dataKey="ICIR" stroke="#f59e0b" dot={{ r: 2 }} strokeWidth={1.6} />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>
      )}
      <div className="mt-2 text-[10px] text-slate-400">
        磁贴 RankIC/ICIR/胜率口径：{icHead.isTest ? '测试段（样本外）' : '全窗合计（该报告无测试段）'}；
        分层{showLongShort ? '与多空' : ''}为全窗口径（含训练段）。
        {isRealReturn
          ? (
            <>
              分层收益为未来收益口径（T+1 执行、持有 N 日）；
              {tradability?.applied
                ? `已剔除次日涨跌停 ${tradability.excluded_rows ?? 0} 行（涨停买不进/跌停卖不出）；`
                : '未剔除涨跌停（报告生成早于可交易性口径）；'}
              {showLongShort ? '多空净值为复利累计倍数（对数轴），未扣交易成本。' : ''}
            </>
          )
          : '该报告为秩标签口径：分层在秩空间，不是可交易收益，多空净值与年化/夏普不适用。'}
        IC 胜率 = 日 RankIC 为正的比例。
      </div>
    </Card>
  );
};

export default EvalReportSection;
