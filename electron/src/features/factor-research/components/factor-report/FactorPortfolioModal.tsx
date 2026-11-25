/** 组合构建（弹窗）：推荐因子集 + 权重 + 方向 + 淘汰理由；训练页勾选即来自这套规则 */

import React, { useEffect, useState } from 'react';
import { Modal, message } from 'antd';
import { Copy, RefreshCw, Target } from 'lucide-react';
import { getFactorPortfolio } from '../../services/factorReportService';
import type { FactorPortfolioResponse } from '../../types/factorReport';
import type { ResearchMarket } from '../../types/factorResearch';

interface Props {
  open: boolean;
  dataset: string;
  datasetLabel: string;
  /** 本页当前市场（推荐组合按市场快照计算） */
  market: ResearchMarket;
  onClose: () => void;
  onPick: (factor: string) => void;
}

export const FactorPortfolioModal: React.FC<Props> = ({ open, dataset, datasetLabel, market, onClose, onPick }) => {
  const [data, setData] = useState<FactorPortfolioResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [showRejected, setShowRejected] = useState(false);

  const load = (recompute = false) => {
    setLoading(true);
    getFactorPortfolio(dataset, recompute, market)
      .then((res) => setData(res))
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    if (open) load(false);
  }, [open, dataset, market]);

  const summary = data?.summary;
  const maxW = Math.max(...(data?.factors || []).map((f) => Math.abs(f.weight)), 1e-6);
  const gain = summary && summary.single_icir_avg > 0 && summary.composite_icir
    ? summary.composite_icir / summary.single_icir_avg
    : null;

  // 精选子集（T-FE-02）：旧 JSON 无该字段（undefined）→ 整节不渲染；
  // 有字段但降级（available:false）→ 显示原因，不用 0/空表冒充
  const bs = data?.best_subset;
  const best = bs && bs.available === true ? bs : null;
  const degraded = bs && bs.available === false ? bs : null;
  const sh = (v: number | null | undefined) =>
    v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}`;

  const copyJson = async () => {
    if (!data?.available) return;
    const payload = {
      dataset,
      weights: Object.fromEntries((data.factors || []).map((f) => [f.name, f.weight])),
      direction: Object.fromEntries((data.factors || []).map((f) => [f.name, f.direction])),
      composite_ic: data.summary?.composite_ic,
      composite_icir: data.summary?.composite_icir,
      rule: data.rule,
      best_subset: data.best_subset ?? undefined,
    };
    try {
      await navigator.clipboard.writeText(JSON.stringify(payload, null, 1));
      message.success('权重 JSON 已复制（训练侧可直接用作特征清单）');
    } catch {
      message.error('复制失败（浏览器未授权剪贴板）');
    }
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width="72vw"
      centered
      destroyOnHidden
      title={
        <div className="flex items-center gap-3 flex-wrap">
          <span className="flex items-center gap-1.5 text-sm font-black text-slate-800">
            <Target className="w-4 h-4 text-indigo-500" />
            组合构建 · {datasetLabel}
          </span>
          <button onClick={copyJson} className="flex items-center gap-1 rounded-full border border-slate-200 px-2.5 py-0.5 text-[11px] font-bold text-slate-500 hover:text-indigo-600 hover:border-indigo-200">
            <Copy className="w-3 h-3" /> 复制权重 JSON
          </button>
          <button onClick={() => load(true)} className="flex items-center gap-1 rounded-full border border-slate-200 px-2.5 py-0.5 text-[11px] font-bold text-slate-500 hover:text-indigo-600 hover:border-indigo-200">
            <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} /> 重算
          </button>
        </div>
      }
    >
      {!data?.available ? (
        <div className="py-10 text-center text-xs text-slate-500">{loading ? '计算中…' : data?.reason || '暂无达到证据标准的组合'}</div>
      ) : (
        <div className="flex flex-col gap-3 max-h-[74vh] overflow-y-auto custom-scrollbar pr-1">
          {summary && (
            <div className="rounded-2xl bg-indigo-50/70 border border-indigo-100 px-4 py-2.5 text-xs text-slate-700 leading-5">
              从 <b>{summary.n_universe}</b> 个因子里通过门槛 <b>{summary.n_passed}</b> 个，
              去重与择优后入选 <b className="text-indigo-700">{summary.n_selected}</b> 个
              （淘汰 {summary.n_rejected}）。
              组合 IC <b>{summary.composite_ic?.toFixed(4) ?? '—'}</b> /
              ICIR <b className="text-emerald-600">{summary.composite_icir?.toFixed(3) ?? '—'}</b>
              {gain && <>，相对单因子平均 |ICIR| {summary.single_icir_avg.toFixed(3)} 提升 <b>{gain.toFixed(2)}×</b></>}
              <div className="text-[11px] text-slate-500 mt-0.5">
                规则：|ICIR| ≥ {String(data.rule?.icir_min)}、覆盖率 ≥ {Number(data.rule?.coverage_min) * 100}%、
                T+{String(data.rule?.holding_days)} 调仓扣费后净收益 &gt; 0、|ρ| ≥ {String(data.rule?.corr_threshold)} 去重后按最大 ICIR 配权。
                <span className="text-slate-500"> 此推荐集仅在本页计算，不会自动进入训练目录——要作为训练特征请在排行榜勾选后点「注册到训练目录」。</span>
              </div>
            </div>
          )}

          <div className="flex flex-col gap-0.5">
            {(data.factors || []).map((f) => (
              <div key={f.name} className="flex items-center gap-2 px-2 py-1 rounded-lg hover:bg-slate-50">
                <span className={`w-10 shrink-0 text-center rounded text-[10px] font-black ${f.direction > 0 ? 'bg-rose-50 text-rose-600' : 'bg-emerald-50 text-emerald-600'}`}
                      title={f.direction > 0 ? '正向因子' : '反向因子'}>
                  {f.direction > 0 ? '正' : '反'}
                </span>
                <button onClick={() => { onPick(f.name); onClose(); }} className="w-[190px] shrink-0 text-left text-xs font-bold text-slate-700 hover:text-indigo-600 truncate" title="在因子报告里打开">
                  {f.name}
                  {f.display_name && <span className="ml-1 font-normal text-slate-400">{f.display_name}</span>}
                </button>
                <div className="flex-1 min-w-0 h-3 bg-slate-100 rounded-full overflow-hidden">
                  <div className={`h-full rounded-full ${f.weight > 0 ? 'bg-rose-400' : 'bg-emerald-400'}`}
                       style={{ width: `${(Math.abs(f.weight) / maxW) * 100}%` }} />
                </div>
                <span className="w-14 shrink-0 text-right text-[11px] font-mono font-bold text-slate-600">{f.weight > 0 ? '+' : ''}{f.weight.toFixed(3)}</span>
                <span className="w-24 shrink-0 text-right text-[10px] font-mono text-slate-400">
                  ICIR {f.icir?.toFixed(2) ?? '—'} · 换手 {f.turnover != null ? (f.turnover * 100).toFixed(0) : '—'}%
                </span>
              </div>
            ))}
          </div>

          {bs != null && (
            <div className="rounded-2xl border border-slate-200 px-4 py-3">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs font-black text-slate-800">精选子集 · 组合 Sharpe 口径</span>
                {best && (
                  <span className="rounded-full bg-indigo-50 text-indigo-700 px-2 py-0.5 text-[11px] font-bold">
                    K* = {best.k}（区间 {best.k_range[0]}–{best.k_range[1]}）
                  </span>
                )}
                {best?.overfit_warning && (
                  <span className="rounded-full bg-amber-50 text-amber-700 px-2 py-0.5 text-[11px] font-bold">
                    ⚠ train/holdout = {best.sharpe_ratio_train_holdout}，过拟合风险高
                  </span>
                )}
                {best?.vs_benchmarks.beats_equal_weight != null && (
                  <span className={`rounded-full px-2 py-0.5 text-[11px] font-bold ${best.vs_benchmarks.beats_equal_weight ? 'bg-emerald-50 text-emerald-600' : 'bg-slate-100 text-slate-500'}`}>
                    留出窗 {best.vs_benchmarks.beats_equal_weight ? '>' : '≤'} 等权 1/N
                  </span>
                )}
                {best?.vs_benchmarks.beats_current_rule != null && (
                  <span className={`rounded-full px-2 py-0.5 text-[11px] font-bold ${best.vs_benchmarks.beats_current_rule ? 'bg-emerald-50 text-emerald-600' : 'bg-slate-100 text-slate-500'}`}>
                    留出窗 {best.vs_benchmarks.beats_current_rule ? '>' : '≤'} 现行规则
                  </span>
                )}
              </div>
              {degraded && (
                <div className="mt-1 text-[11px] text-slate-500">精选子集不可用：{degraded.reason}</div>
              )}
              {best && (
                <>
                  <div className="mt-1 text-[11px] text-slate-500 leading-5">
                    按「对组合 Sharpe 的边际贡献」从 {best.universe.n_candidates} 个候选里选 {best.k} 个
                    （贪心前向 + 1-swap，只看训练窗前 {Math.round(best.train_frac * 100)}%）。
                    口径：T+{best.rule.holding_days} 调仓、带符号净日收益（已扣费）、年化 √{best.rule.annualization}。
                    {best.universe.n_excluded_short_history > 0 && ` 已剔除 ${best.universe.n_excluded_short_history} 个短历史因子。`}
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1">
                    {best.factors.map((f) => (
                      <button
                        key={f.name}
                        onClick={() => { onPick(f.name); onClose(); }}
                        className="rounded border border-slate-200 px-1.5 py-0.5 text-[11px] font-mono text-slate-600 hover:border-indigo-300 hover:text-indigo-600"
                        title={`方向：${f.direction > 0 ? '正向' : '反向'}；点击在因子报告里打开`}
                      >
                        {f.name}
                      </button>
                    ))}
                  </div>
                  <table className="mt-2 w-full text-[11px] font-mono">
                    <thead>
                      <tr className="text-slate-400">
                        <th className="text-left font-normal">Sharpe（年化）</th>
                        <th className="text-right font-normal">train（{best.n_days.train} 天）</th>
                        <th className="text-right font-normal">holdout（{best.n_days.holdout} 天）</th>
                        <th className="text-right font-normal">full</th>
                      </tr>
                    </thead>
                    <tbody className="text-slate-700">
                      <tr>
                        <td className="text-indigo-700 font-bold">精选子集 K*={best.k}</td>
                        <td className="text-right">{sh(best.chosen.train_sharpe)}</td>
                        <td className="text-right">{sh(best.chosen.holdout_sharpe)}</td>
                        <td className="text-right">{sh(best.chosen.full_sharpe)}</td>
                      </tr>
                      <tr>
                        <td>等权 1/N（{best.benchmarks.equal_weight_all.n_factors} 个）</td>
                        <td className="text-right">{sh(best.benchmarks.equal_weight_all.train_sharpe)}</td>
                        <td className="text-right">{sh(best.benchmarks.equal_weight_all.holdout_sharpe)}</td>
                        <td className="text-right">{sh(best.benchmarks.equal_weight_all.full_sharpe)}</td>
                      </tr>
                      {best.benchmarks.current_rule && (
                        <tr>
                          <td>现行规则（{best.benchmarks.current_rule.n_factors} 个）</td>
                          <td className="text-right">{sh(best.benchmarks.current_rule.train_sharpe)}</td>
                          <td className="text-right">{sh(best.benchmarks.current_rule.holdout_sharpe)}</td>
                          <td className="text-right">{sh(best.benchmarks.current_rule.full_sharpe)}</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                  <div className="mt-1 text-[10px] text-slate-400 font-mono">
                    K 扫描 train/holdout：{best.curve.map((c) => `${c.k} ${sh(c.train_sharpe)}/${sh(c.holdout_sharpe)}`).join(' · ')}
                  </div>
                  {best.notes.length > 0 && (
                    <div className="mt-1 text-[11px] text-amber-600 leading-5">
                      {best.notes.map((n) => <div key={n}>· {n}</div>)}
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          <div>
            <button onClick={() => setShowRejected(!showRejected)} className="text-[11px] font-bold text-slate-400 hover:text-slate-600">
              {showRejected ? '▾' : '▸'} 淘汰理由（{(data.rejected || []).length} 条）
            </button>
            {showRejected && (
              <div className="mt-1 max-h-48 overflow-y-auto custom-scrollbar text-[11px] text-slate-500 font-mono leading-5">
                {(data.rejected || []).slice(0, 120).map((r) => (
                  <div key={r.name}><span className="text-slate-600">{r.name}</span> — {r.reason}</div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </Modal>
  );
};
