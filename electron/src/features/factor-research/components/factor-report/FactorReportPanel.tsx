/**
 * 因子报告（Alphalens 式）——因子研究「因子报告」页签（2026-09-17 由技能中心迁入）
 *
 * 三个问题一屏回答：
 *  1) 分位收益：把股票按因子值分 10 组，Q10−Q1 是不是单调、价差多大（区分真因子与噪声）
 *  2) 换手：这组因子每天换掉多少仓位（决定交易成本能否吃得住）
 *  3) 相关性：它是不是别人的复制品（|ρ|>0.9 的因子只该留一个）
 *
 * 数据集可切换：Alpha 库（Alpha101/GTJA191/Alpha158）与 QuantDB 的 L1 / L2 / L1+L2 因子。
 * 数据：backend/services/engine/factor_report（快照 + 预计算序列），
 * 快照由 backend/scripts/build_factor_report.py --dataset <名> 生成。
 */

import React, { useEffect, useMemo, useState } from 'react';
import { AlertCircle, Layers, RefreshCw, Sparkles, Target } from 'lucide-react';
import { FactorRankList } from './FactorRankList';
import { FactorClusterModal } from './FactorClusterModal';
import { HorizonDecayChart } from './HorizonDecayChart';
import { FactorPortfolioModal } from './FactorPortfolioModal';
import { FactorDetailCharts } from './FactorDetailCharts';
import { FactorCorrelationHeatmap } from './FactorCorrelationHeatmap';
import {
  getFactorCorrelation,
  getFactorDatasets,
  getFactorDetail,
  getFactorRelated,
  getFactorSummary,
} from '../../services/factorReportService';
import type {
  FactorCorrelation,
  FactorDatasetInfo,
  FactorDetail,
  FactorRelated,
  FactorReportMeta,
  FactorSummary,
} from '../../types/factorReport';

function MetricTile({ label, value, tone = 'slate', title }: {
  label: string;
  value: string;
  tone?: 'slate' | 'up' | 'down';
  title?: string;
}) {
  const color = tone === 'up' ? 'text-rose-600' : tone === 'down' ? 'text-emerald-600' : 'text-slate-800';
  return (
    <div className="bg-white rounded-xl border border-slate-200/80 px-3 py-2 min-w-[86px]" title={title}>
      <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wide">{label}</div>
      <div className={`text-sm font-black font-mono leading-tight mt-0.5 ${color}`}>{value}</div>
    </div>
  );
}

export const FactorReportPanel: React.FC = () => {
  const [datasets, setDatasets] = useState<FactorDatasetInfo[]>([]);
  const [dataset, setDataset] = useState<string>('alpha_library');
  const [factors, setFactors] = useState<FactorSummary[]>([]);
  const [meta, setMeta] = useState<FactorReportMeta | null>(null);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<FactorDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [related, setRelated] = useState<FactorRelated | null>(null);
  const [correlation, setCorrelation] = useState<FactorCorrelation | null>(null);
  const [corrLoading, setCorrLoading] = useState(false);
  const [clusterOpen, setClusterOpen] = useState(false);
  const [portfolioOpen, setPortfolioOpen] = useState(false);

  // 数据集清单（含快照状态；未生成的数据集置灰）
  useEffect(() => {
    getFactorDatasets()
      .then((res) => {
        setDatasets(res.items || []);
        const first = (res.items || []).find((d) => d.available);
        if (first) setDataset(first.dataset);
      })
      .catch(() => undefined);
  }, []);

  // 快照摘要（一次拉全量，前端做筛选/搜索）
  const loadSummary = async (ds: string, pickFirst = false) => {
    setListLoading(true);
    try {
      const res = await getFactorSummary({ dataset: ds, sort: 'abs_ic' });
      if (!res.available) {
        setUnavailable(res.reason || '快照尚未生成');
        setFactors([]);
        setMeta(null);
        return;
      }
      setUnavailable(null);
      setFactors(res.factors || []);
      setMeta(res.meta || null);
      if (pickFirst && (res.factors || []).length > 0) {
        // 函数式更新在 tsc 下会报类型错（历史坑），改用传值
        setSelected(res.factors[0].name);
      }
    } catch (e) {
      setUnavailable(e instanceof Error ? e.message : '因子报告加载失败');
    } finally {
      setListLoading(false);
    }
  };

  useEffect(() => {
    if (!dataset) return;
    setSelected(null);
    setDetail(null);
    setRelated(null);
    setCorrelation(null);
    void loadSummary(dataset, true);
  }, [dataset]);

  // 选中因子变化 → 明细 + 相关因子 → 相关性矩阵
  useEffect(() => {
    if (!selected || !dataset) return;
    let cancelled = false;
    setDetailLoading(true);
    setDetail(null);
    getFactorDetail(selected, dataset)
      .then((d) => !cancelled && setDetail(d))
      .catch(() => !cancelled && setDetail(null))
      .finally(() => !cancelled && setDetailLoading(false));

    setCorrLoading(true);
    getFactorRelated(selected, dataset, 7)
      .then(async (r) => {
        if (cancelled) return;
        setRelated(r);
        const names = [selected, ...r.related.map((x) => x.name)];
        const corr = await getFactorCorrelation(names, dataset);
        if (!cancelled) setCorrelation(corr);
      })
      .catch(() => {
        if (!cancelled) {
          setRelated(null);
          setCorrelation(null);
        }
      })
      .finally(() => !cancelled && setCorrLoading(false));

    return () => {
      cancelled = true;
    };
  }, [selected, dataset]);

  const current = useMemo(() => factors.find((f) => f.name === selected) || null, [factors, selected]);

  const monotoneText = current?.monotonicity == null
    ? '—'
    : `${current.monotonicity > 0 ? '+' : ''}${current.monotonicity.toFixed(2)}`;

  return (
    <div className="flex h-full min-h-0 bg-gray-50/40">
      {/* 左：因子榜 */}
      <aside className="w-[280px] shrink-0 border-r border-gray-200 bg-white flex flex-col min-h-0">
        <FactorRankList
          factors={factors}
          selected={selected}
          onSelect={setSelected}
          loading={listLoading}
        />
      </aside>

      {/* 右：详情 */}
      <main className="flex-1 min-w-0 flex flex-col gap-3 p-3 min-h-0">
        {/* 顶：数据集切换 + 快照信息 */}
        <div className="flex items-center gap-3 flex-wrap shrink-0">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-indigo-500 to-violet-500 flex items-center justify-center shadow-sm">
              <Sparkles className="w-4 h-4 text-white" />
            </div>
            <div className="flex items-center gap-1 rounded-full bg-slate-100 border border-slate-200 p-0.5">
              {(datasets.length ? datasets : [{ dataset: 'alpha_library', label: 'Alpha 库', available: true } as FactorDatasetInfo]).map((d) => (
                <button
                  key={d.dataset}
                  onClick={() => d.available && setDataset(d.dataset)}
                  disabled={!d.available}
                  title={d.available
                    ? `${d.label}${d.n_factors ? ` · ${d.n_factors} 个因子` : ''}`
                    : `${d.label}：快照尚未生成（build_factor_report.py --dataset ${d.dataset}）`}
                  className={`rounded-full px-3 py-1 text-[11px] font-bold transition-colors ${
                    dataset === d.dataset
                      ? 'bg-white text-indigo-700 shadow-sm'
                      : d.available
                        ? 'text-slate-500 hover:text-slate-700'
                        : 'text-slate-300 cursor-not-allowed'
                  }`}
                >
                  {d.label}
                </button>
              ))}
            </div>
          </div>

          <span className="text-[10px] text-slate-400 font-mono">
            {meta
              ? `${meta.universe} · ${meta.horizon.replace('fwd_ret_', 'T+')} 前瞻 · ${meta.start}~${meta.end} · ${meta.n_dates} 个交易日 · 快照 ${meta.generated_at}`
              : '加载中…'}
          </span>

          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={() => setPortfolioOpen(true)}
              className="flex items-center gap-1.5 rounded-full bg-emerald-600 px-3 py-1 text-[11px] font-bold text-white shadow-sm hover:bg-emerald-500 active:scale-95"
              title="推荐因子集与权重（已写入训练目录，训练页默认勾选）"
            >
              <Target className="w-3 h-3" />
              组合构建
            </button>
            <button
              onClick={() => setClusterOpen(true)}
              className="flex items-center gap-1.5 rounded-full bg-indigo-600 px-3 py-1 text-[11px] font-bold text-white shadow-sm hover:bg-indigo-500 active:scale-95"
              title="按相关性找同源因子簇，每簇只留一个代表（含 PDF 报告）"
            >
              <Layers className="w-3 h-3" />
              因子去重
            </button>
          </div>
          <button
            onClick={() => void loadSummary(dataset, false)}
            className="flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-bold text-slate-500 hover:text-indigo-600 hover:border-indigo-200"
            title="重新读取快照（快照由服务器脚本生成）"
          >
            <RefreshCw className={`w-3 h-3 ${listLoading ? 'animate-spin' : ''}`} />
            刷新
          </button>
        </div>

        {/* 当前因子 + 指标条 */}
        {!unavailable && selected && (
          <div className="flex items-center gap-3 flex-wrap shrink-0">
            <div className="min-w-[190px]">
              <div className="flex items-center gap-2">
                <span className="text-sm font-black text-slate-800">{current?.display_name || selected}</span>
                {current && (
                  <span className="rounded-full bg-indigo-50 border border-indigo-100 px-2 py-[1px] text-[10px] font-bold text-indigo-600">
                    {current.library}
                  </span>
                )}
              </div>
              <span className="text-[10px] text-slate-400 font-mono">
                {current?.display_name ? `${selected} · ${current.category_name || ''}` : current?.category_name || ''}
              </span>
            </div>
            <MetricTile
              label="IC 均值"
              value={current ? `${current.ic_mean >= 0 ? '+' : ''}${current.ic_mean.toFixed(4)}` : '—'}
              tone={current && current.ic_mean >= 0 ? 'up' : 'down'}
              title="横截面秩相关（Spearman）的日均值，|IC|>0.03 通常算有效"
            />
            <MetricTile
              label="ICIR"
              value={current ? current.icir.toFixed(3) : '—'}
              tone={current && current.icir >= 0 ? 'up' : 'down'}
              title="IC 均值 / IC 标准差，衡量稳定性；|ICIR|>0.3 较好"
            />
            <MetricTile label="t 值" value={current ? current.t_value.toFixed(1) : '—'} title="ICIR × √样本数；|t|>2 显著" />
            <MetricTile label="IC 胜率" value={current ? `${(current.win_rate * 100).toFixed(1)}%` : '—'} title="IC>0 的交易日占比" />
            <MetricTile
              label="多空价差"
              value={current ? `${current.ls_mean >= 0 ? '+' : ''}${(current.ls_mean * 100).toFixed(3)}%` : '—'}
              tone={current && current.ls_mean >= 0 ? 'up' : 'down'}
              title="Q10 组 − Q1 组的平均前瞻收益（每个调仓周期）"
            />
            <MetricTile
              label="单边换手"
              value={current ? `${(current.turnover * 100).toFixed(0)}%` : '—'}
              title="十分位组合每日成员变动比例，直接决定交易成本"
            />
            <MetricTile label="单调性" value={monotoneText} title="分位序号与分位收益的秩相关，±1 = 完美单调" />
            <HorizonDecayChart icByHorizon={current?.ic_by_horizon} />
          </div>
        )}

        {unavailable ? (
          <div className="flex-1 min-h-0 flex flex-col items-center justify-center gap-2 rounded-2xl border border-dashed border-amber-200 bg-amber-50/50 text-center px-6">
            <AlertCircle className="w-5 h-5 text-amber-500" />
            <span className="text-xs font-bold text-amber-700">因子报告快照不可用</span>
            <span className="text-[11px] text-amber-600/90 leading-5 max-w-xl">{unavailable}</span>
          </div>
        ) : (
          <>
            <FactorDetailCharts detail={detail} loading={detailLoading} />
            <div className="h-[240px] shrink-0 flex">
              <div className="flex-1 min-w-0">
                <FactorCorrelationHeatmap
                  correlation={correlation}
                  related={related}
                  loading={corrLoading}
                  onPick={setSelected}
                />
              </div>
            </div>
          </>
        )}
      </main>

      <FactorPortfolioModal
        open={portfolioOpen}
        dataset={dataset}
        datasetLabel={(datasets.find((d) => d.dataset === dataset)?.label) || dataset}
        onClose={() => setPortfolioOpen(false)}
        onPick={(f) => setSelected(f)}
      />

      <FactorClusterModal
        open={clusterOpen}
        dataset={dataset}
        datasetLabel={(datasets.find((d) => d.dataset === dataset)?.label) || dataset}
        onClose={() => setClusterOpen(false)}
        onPick={(f) => setSelected(f)}
      />
    </div>
  );
};
