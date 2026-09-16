/**
 * 体检报告统一视图（九项 + 四分类）——回测「体检结论」页签与「自助体检」上传共用。
 * 数据源均为 scripts/eval/health_check.py 同构报告（后端唯一实现）。
 */

import React from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  HelpCircle,
  Scale,
  ShieldCheck,
  XCircle,
} from 'lucide-react';
import type { HealthReport } from '../../../services/backtestService';
import { TermTooltip } from '../../../features/shared/TermTooltip';
import { useUiMode } from '../../../features/shared/useUiMode';

const VERDICT_STYLE: Record<
  HealthReport['verdict'],
  { icon: React.ComponentType<{ className?: string }>; badge: string; ring: string }
> = {
  A: {
    icon: ShieldCheck,
    badge: 'bg-red-50 text-red-700 border-red-200',
    ring: 'text-red-600',
  },
  B: {
    icon: Scale,
    badge: 'bg-blue-50 text-blue-700 border-blue-200',
    ring: 'text-blue-600',
  },
  L: {
    icon: AlertTriangle,
    badge: 'bg-amber-50 text-amber-800 border-amber-200',
    ring: 'text-amber-600',
  },
  E: {
    icon: HelpCircle,
    badge: 'bg-slate-100 text-slate-700 border-slate-200',
    ring: 'text-slate-500',
  },
};

function isPercentKey(key: string): boolean {
  return /annual|return|drawdown|uplift/.test(key) && !/bps|t$/.test(key);
}

function formatValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') {
    if (isPercentKey(key)) return `${(value * 100).toFixed(2)}%`;
    if (Number.isInteger(value)) return String(value);
    return value.toFixed(4);
  }
  if (Array.isArray(value)) return value.map((v) => formatValue(key, v)).join(' ~ ');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

interface TestRow {
  key: string;
  label: string;
  /** 术语表 key（label 的 tooltip 解释来源；与 glossary 覆盖度单测同源） */
  term: string;
  text: string;
  ok: boolean | null;
}

/** 九项检验 → 展示行（不足项如实显示原因，不隐藏） */
export function buildTestRows(tests: HealthReport['tests']): TestRow[] {
  const t = tests || {};
  const rows: TestRow[] = [];
  const fr = (t.factor_regression || {}) as Record<string, unknown>;
  const dsr = (t.dsr || {}) as Record<string, unknown>;
  const psr = (t.psr || {}) as Record<string, unknown>;
  const mtrl = (t.min_trl || {}) as Record<string, unknown>;
  const boot = (t.bootstrap || {}) as Record<string, unknown>;
  const conc = (t.concentration || {}) as Record<string, unknown>;
  const regime = (t.regime || {}) as Record<string, unknown>;
  const cost = (t.cost || {}) as Record<string, unknown>;
  const pbo = (t.pbo || {}) as Record<string, unknown>;

  rows.push(
    fr.sufficient
      ? {
          key: 'factor_regression',
          term: 'factor_regression',
          label: '因子回归（alpha/beta）',
          text: `年化 alpha ${formatValue('alpha_annual', fr.alpha_annual)}，t=${formatValue('alpha_t', fr.alpha_t)} ${
            fr.alpha_significant ? '✓ 显著' : '✗ 不显著'
          }；R²=${formatValue('r2', fr.r2)}，beta=${formatValue('beta', fr.beta)}`,
          ok: Boolean(fr.alpha_significant),
        }
      : {
          key: 'factor_regression',
          term: 'factor_regression',
          label: '因子回归（alpha/beta）',
          text: String(fr.reason || '未提供基准序列'),
          ok: null,
        }
  );
  rows.push(
    dsr.sufficient
      ? {
          key: 'dsr',
          term: 'dsr',
          label: 'DSR 紧缩夏普',
          text: `${formatValue('dsr', dsr.dsr)} ${dsr.passes_095 ? '✓' : '✗'} ≥0.95（试验 N=${formatValue('n_trials', dsr.n_trials)}）`,
          ok: Boolean(dsr.passes_095),
        }
      : { key: 'dsr', term: 'dsr', label: 'DSR 紧缩夏普', text: String(dsr.reason || '样本不足'), ok: null }
  );
  rows.push(
    psr.sufficient
      ? {
          key: 'psr',
          term: 'psr',
          label: 'PSR 概率夏普',
          text: `${formatValue('psr', psr.psr)}（Sharpe>0 概率，偏度/峰度校正）`,
          ok: null,
        }
      : { key: 'psr', term: 'psr', label: 'PSR 概率夏普', text: String(psr.reason || '样本不足'), ok: null }
  );
  rows.push(
    mtrl.sufficient
      ? {
          key: 'min_trl',
          term: 'min_trl',
          label: '最短样本 MinTRL',
          text: `样本 ${formatValue('observed_years', mtrl.observed_years)} 年 vs 需 ${formatValue('min_trl_years', mtrl.min_trl_years)} 年 ${mtrl.adequate ? '✓' : '✗'}`,
          ok: Boolean(mtrl.adequate),
        }
      : { key: 'min_trl', term: 'min_trl', label: '最短样本 MinTRL', text: String(mtrl.reason || '样本不足'), ok: null }
  );
  rows.push(
    boot.sufficient
      ? {
          key: 'bootstrap',
          term: 'bootstrap',
          label: 'Block Bootstrap',
          text: `年化收益 CI ${formatValue('return_ci', boot.return_ci)}（${
            boot.return_ci_crosses_zero ? '跨 0 ⚠' : '不跨 0'
          }）`,
          ok: !boot.return_ci_crosses_zero,
        }
      : { key: 'bootstrap', term: 'bootstrap', label: 'Block Bootstrap', text: String(boot.reason || '样本不足'), ok: null }
  );
  rows.push(
    conc.sufficient
      ? {
          key: 'concentration',
          term: 'concentration',
          label: '收益集中度',
          text: `剔 Top${formatValue('top_k', conc.top_k)} 日 ${formatValue('full_total_return', conc.full_total_return)} → ${formatValue(
            'ex_top_total_return',
            conc.ex_top_total_return
          )} ${conc.kills_alpha ? '⚠ 集中' : '✓ 分散'}`,
          ok: !conc.kills_alpha,
        }
      : { key: 'concentration', term: 'concentration', label: '收益集中度', text: String(conc.reason || '样本不足'), ok: null }
  );
  rows.push(
    regime.sufficient
      ? {
          key: 'regime',
          term: 'regime',
          label: '跨 regime 分段',
          text: `覆盖 ${formatValue('regimes_covered', regime.regimes_covered)}；${
            regime.all_alive ? '各段存活 ✓' : '存在失效段 ✗'
          }`,
          ok: Boolean(regime.all_alive),
        }
      : { key: 'regime', term: 'regime', label: '跨 regime 分段', text: String(regime.reason || '未提供指数序列'), ok: null }
  );
  rows.push(
    cost.sufficient
      ? {
          key: 'cost',
          term: 'cost_sensitivity',
          label: '成本敏感性',
          text: `费率上浮 ${formatValue('uplift_bps', cost.uplift_bps)}bps → 年化 ${formatValue('adjusted_annual', cost.adjusted_annual)} ${
            cost.still_positive_after_cost ? '✓ 仍正' : '✗ 转负'
          }`,
          ok: Boolean(cost.still_positive_after_cost),
        }
      : { key: 'cost', term: 'cost_sensitivity', label: '成本敏感性', text: String(cost.reason || '未提供换手序列'), ok: null }
  );
  rows.push(
    pbo.sufficient
      ? {
          key: 'pbo',
          term: 'pbo',
          label: 'PBO 过拟合概率',
          text: `${formatValue('pbo', pbo.pbo)}（${formatValue('overfit_risk', pbo.overfit_risk)}，${formatValue('n_params', pbo.n_params)} 组）`,
          ok: null,
        }
      : { key: 'pbo', term: 'pbo', label: 'PBO 过拟合概率', text: String(pbo.reason || '未提供参数扫描矩阵'), ok: null }
  );
  return rows;
}

function RowIcon({ ok }: { ok: boolean | null }) {
  if (ok === true) return <CheckCircle2 className="w-4 h-4 text-red-500 shrink-0 mt-0.5" />;
  if (ok === false) return <XCircle className="w-4 h-4 text-amber-500 shrink-0 mt-0.5" />;
  return <HelpCircle className="w-4 h-4 text-gray-300 shrink-0 mt-0.5" />;
}

export const HealthReportView: React.FC<{ report: HealthReport }> = ({ report }) => {
  const { isSimple } = useUiMode();
  const style = VERDICT_STYLE[report.verdict] || VERDICT_STYLE.E;
  const VerdictIcon = style.icon;
  const rows = buildTestRows(report.tests);
  const confidence = Math.max(0, Math.min(100, Math.round(report.confidence ?? 0)));
  const verdictTerm = `verdict_${String(report.verdict || 'e').toLowerCase()}`;

  return (
    <div className="space-y-4">
      <div className={`rounded-2xl border p-5 ${style.badge}`}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <VerdictIcon className={`w-7 h-7 ${style.ring}`} />
            <div>
              <div className="text-lg font-bold">
                结论：
                <TermTooltip term={verdictTerm} className="!border-white/40">
                  {report.verdict_label || `判定 ${report.verdict}`}
                </TermTooltip>
              </div>
              <div className="text-xs opacity-80 mt-0.5">
                样本 {report.n_days} 个交易日 · 试验 N={report.n_trials}
                {report.inputs?.window ? ` · 窗口 ${report.inputs.window[0]} → ${report.inputs.window[1]}` : ''}
                {report.inputs?.benchmark ? ` · 基准 ${report.inputs.benchmark}` : ''}
                {report.generated_at ? ` · 生成于 ${report.generated_at}` : ''}
              </div>
            </div>
          </div>
          <div className="text-right">
            <div className="text-xs opacity-80">可信度分</div>
            <div className="text-2xl font-bold">{confidence}/100</div>
            <div className="w-36 h-1.5 bg-white/60 rounded-full mt-1 overflow-hidden">
              <div
                className="h-full bg-current opacity-70"
                style={{ width: `${confidence}%` }}
              />
            </div>
          </div>
        </div>

        {(report.reasons?.length > 0 || report.suggestions?.length > 0) && (
          <div className="mt-4 text-sm space-y-1">
            {report.reasons?.map((reason, index) => (
              <div key={`r-${index}`}>· {reason}</div>
            ))}
            {report.suggestions?.map((suggestion, index) => (
              <div key={`s-${index}`} className="opacity-90">
                建议 {index + 1}: {suggestion}
              </div>
            ))}
          </div>
        )}
      </div>

      {isSimple ? (
        <div className="bg-white rounded-2xl border border-gray-200 p-4 text-xs text-slate-500">
          已是简单模式精炼视图（结论 + 理由 + 建议）；右上角「专业」可展开
          <TermTooltip term="dsr">九项检验</TermTooltip>明细与原始口径。
        </div>
      ) : (
      <div className="bg-white rounded-2xl border border-gray-200 p-5">
        <h4 className="text-sm font-semibold text-gray-800 mb-3">九项检验明细</h4>
        <div className="space-y-2.5">
          {rows.map((row) => (
            <div key={row.key} className="flex items-start gap-2 text-sm">
              <RowIcon ok={row.ok} />
              <div>
                <TermTooltip term={row.term} className="text-gray-500 mr-2">
                  {row.label}
                </TermTooltip>
                <span className="text-gray-800">{row.text}</span>
              </div>
            </div>
          ))}
        </div>
        <p className="text-xs text-gray-400 mt-4">
          判定优先级：证据不足（E）→ 运气嫌疑（L）→ Beta 主导（B）→ 真 alpha（A）；L/E 类策略不得晋级模拟/实盘。
        </p>
      </div>
      )}
    </div>
  );
};
