import React from 'react';
import { Card, Divider, Alert, Tag, Space, Typography, Empty, Button, Table, Tooltip as AntTooltip, Modal, Input } from 'antd';
import { BarChart, MonitorPlay, Activity, Download, Filter } from 'lucide-react';
import {
  BarChart as ReBarChart,
  LineChart as ReLineChart,
  Line,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  ReferenceLine
} from 'recharts';
import dayjs from 'dayjs';
import { clsx } from 'clsx';
import {
  TrainingResult,
  TrainingRequestPayload,
  EvalReport,
  getObjectiveMetricDescription,
  getTargetModeDescription,
  resolveEvalIcHeadline,
} from './trainingUtils';
import {
  modelTrainingService,
  type StrictEvalCostBlock,
  type StrictEvalReportRow,
  type TrainingAuditRow,
  type TrainingRunCompareResult,
} from '../../services/modelTrainingService';

const { Text } = Typography;

// 结果元数据里的 market 是训练时的市场词表全集（TRAINING_MARKET_OPTIONS），
// 词条必须六市场全覆盖——缺 CUSTOM 时结果卡会退化成显示原始码。单测以
// TRAINING_MARKET_OPTIONS 为全集断言（防再漏）。
export const MARKET_LABELS: Record<string, string> = {
  CN: 'A股',
  HK: '港股',
  US: '美股',
  CRYPTO: '区块链',
  FUTURES: '期货',
  CUSTOM: '自定义市场',
};

interface TrainingResultViewProps {
  result: TrainingResult | null;
  resultError: string;
  settingDefaultModel: boolean;
  onSetDefaultModel: () => void;
  onExportConfig: () => void;
  trainingStatus: string;
  /** 训练 run id：用于拉取 WP2 严格评估报告 */
  runId?: string;
}

const MetricCard: React.FC<{
  label: string;
  value: string;
  hint?: string;
  centered?: boolean;
  valueClassName?: string;
  hintClassName?: string;
}> = ({ label, value, hint, centered = false, valueClassName, hintClassName }) => (
  <div className={clsx('rounded-2xl border border-slate-200 bg-white p-4 shadow-sm', centered && 'text-center')}>
    <div className={clsx('text-[10px] font-black uppercase tracking-[0.18em] text-slate-400', centered && 'text-center')}>{label}</div>
    <div className={clsx('mt-2 text-lg font-semibold text-slate-900', centered && 'text-center', valueClassName)}>{value}</div>
    {hint ? <div className={clsx('mt-1 text-xs text-slate-500', centered && 'text-center', hintClassName)}>{hint}</div> : null}
  </div>
);

const SectionHeader: React.FC<{ title: string; desc: string; icon?: React.ReactNode }> = ({ title, desc, icon }) => (
  <div className="flex items-start justify-between gap-4">
    <div>
      <div className="flex items-center gap-2">
        {icon}
        <Typography.Title level={4} className="!mb-0 !text-slate-900">
          {title}
        </Typography.Title>
      </div>
      <Typography.Paragraph className="!mb-0 !mt-2 !text-xs !text-slate-500 leading-relaxed">
        {desc}
      </Typography.Paragraph>
    </div>
  </div>
);

interface FactorReportRow {
  name: string;
  ic: number | null;
  icir: number | null;
  ic_positive_rate: number | null;
  n_days: number;
  coverage: number;
  status: string;
  reason: string;
  pfs?: number | null;
  pfs_backfilled?: boolean;
}

/** 因子筛选报告：漏斗 + 每特征 IC/ICIR/覆盖/PFS/淘汰原因（train.py select_top_factors 产出）。 */
const FactorSelectionReport: React.FC<{ report: any }> = ({ report }) => {
  const sc: Record<string, number> = report?.stage_counts || {};
  const thr: Record<string, number | null> = report?.thresholds || {};
  const features: FactorReportRow[] = Array.isArray(report?.features) ? report.features : [];
  const diversity = report?.diversity as { n_factors: number; n_eff: number; entropy: number } | null;
  const pfsOn = thr.pfs_threshold != null;
  const maxVal = Math.max(1, Number(sc.input) || 1);

  const funnel = [
    { label: '输入特征', value: Number(sc.input) || 0, color: 'bg-slate-400' },
    { label: '|IC|/|ICIR| 通过', value: Number(sc.ic_pass) || 0, color: 'bg-indigo-400' },
    { label: '相关性+多样性后', value: Number(sc.corr_pass) || 0, color: 'bg-violet-400' },
    { label: '稳定性通过', value: Number(sc.stable) || 0, color: 'bg-emerald-400' },
    ...(pfsOn
      ? [{ label: 'PFS 扰动保真度', value: Number(sc.pfs_pass) || 0, color: 'bg-teal-400' }]
      : []),
    { label: '最终入选', value: Number(sc.selected) || 0, color: 'bg-emerald-500' },
  ];

  const columns = [
    {
      title: '特征',
      dataIndex: 'name',
      key: 'name',
      width: 220,
      align: 'center' as const,
      ellipsis: true,
      render: (v: string, row: FactorReportRow) => (
        <div className="flex items-center justify-center gap-1.5">
          <span className="font-mono text-[11px]">{v}</span>
          {row.status === 'selected' && <Tag className="m-0" color="green">入选</Tag>}
        </div>
      ),
    },
    {
      title: 'IC',
      dataIndex: 'ic',
      key: 'ic',
      width: 70,
      align: 'center' as const,
      render: (v: number | null) => (v == null ? '—' : v.toFixed(4)),
    },
    {
      title: 'ICIR',
      dataIndex: 'icir',
      key: 'icir',
      width: 70,
      align: 'center' as const,
      render: (v: number | null) => (v == null ? '—' : v.toFixed(3)),
    },
    {
      title: 'IC>0占比',
      dataIndex: 'ic_positive_rate',
      key: 'ic_positive_rate',
      width: 80,
      align: 'center' as const,
      render: (v: number | null) => (v == null ? '—' : `${(v * 100).toFixed(0)}%`),
    },
    {
      title: '有效天数',
      dataIndex: 'n_days',
      key: 'n_days',
      width: 70,
      align: 'center' as const,
    },
    ...(pfsOn
      ? [{
          title: 'PFS',
          dataIndex: 'pfs',
          key: 'pfs',
          width: 64,
          align: 'center' as const,
          render: (v: number | null | undefined) =>
            v == null ? (
              <span className="text-slate-300">—</span>
            ) : (
              <span className={v < 0.9 ? 'font-semibold text-red-500' : 'text-slate-700'}>{v.toFixed(3)}</span>
            ),
        }]
      : []),
    {
      title: '训练段覆盖',
      dataIndex: 'coverage',
      key: 'coverage',
      width: 96,
      align: 'center' as const,
      render: (v: number) => {
        const pct = v * 100;
        const cls = v >= 0.99 ? 'text-emerald-600' : v >= 0.9 ? 'text-amber-600' : 'text-red-500 font-semibold';
        return <span className={cls}>{pct.toFixed(1)}%</span>;
      },
    },
    {
      title: '淘汰原因',
      dataIndex: 'reason',
      key: 'reason',
      width: 240,
      align: 'center' as const,
      ellipsis: true,
      render: (v: string, row: FactorReportRow) => (
        <span className={row.status === 'selected' ? 'text-emerald-600' : 'text-slate-500'}>{v}</span>
      ),
    },
  ];

  return (
    <Card className="rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20 } }}>
      <SectionHeader
        title="因子筛选报告"
        desc="为什么最终是这些特征进入训练：IC/ICIR 初筛 → 相关性 + 多样性剪枝 → 稳定性检验 → PFS 扰动保真度的完整漏斗与逐特征依据。"
        icon={<Filter size={18} className="text-emerald-500" />}
      />
      <Divider className="my-4" />

      {/* 漏斗 */}
      <div className="flex items-end gap-2">
        {funnel.map((s) => (
          <div key={s.label} className="flex flex-1 flex-col items-center gap-1">
            <div className="text-sm font-bold text-slate-800">{s.value}</div>
            <div className={`w-full rounded-t-md ${s.color}`} style={{ height: `${Math.max(10, (s.value / maxVal) * 72)}px` }} />
            <div className="h-8 text-center text-[10px] leading-tight text-slate-500">{s.label}</div>
          </div>
        ))}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-500">
        <span>阈值：top-N ≤ {thr.n_top ?? '—'} · |IC| ≥ {thr.ic_threshold ?? '—'} · |ICIR| ≥ {thr.icir_threshold ?? '—'} · 相关性 &lt; {thr.correlation_threshold ?? '—'}</span>
        {pfsOn && (
          <span>
            质量闸门：PFS ≥ {thr.pfs_threshold}（扰动保真度，σ={thr.pfs_sigma ?? '—'}）
            {thr.dh_min_gain != null ? ` · 多样性增益 ≥ ${thr.dh_min_gain}` : ''}
          </span>
        )}
        {report?.train_rows != null && <span>筛选基于训练段 {Number(report.train_rows).toLocaleString()} 行（不含验证/测试，防泄漏）</span>}
        {report?.method ? <span className="font-mono">{report.method}</span> : null}
      </div>

      {(diversity || report?.pfs_fallback || Number(sc.dh_rejected) > 0 || Number(sc.pfs_backfilled) > 0) && (
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
          {diversity && (
            <span className="text-slate-600">
              多样性：入选 {diversity.n_factors} 个特征 ≈ <span className="font-semibold text-slate-800">{diversity.n_eff}</span> 个独立因子（多样性熵 {diversity.entropy}）
            </span>
          )}
          {Number(sc.dh_rejected) > 0 && (
            <span className="text-violet-500">多样性增益淘汰 {Number(sc.dh_rejected)} 个（与已选多重共线）</span>
          )}
          {Number(sc.pfs_backfilled) > 0 && (
            <span className="text-teal-600">PFS 回填 {Number(sc.pfs_backfilled)} 个（补足被扰动检验淘汰的名额）</span>
          )}
          {report?.pfs_fallback ? (
            <span className="text-amber-600">PFS 淘汰过多（&lt; 30），已回退原名单，PFS 仅作标注</span>
          ) : null}
        </div>
      )}

      {/* 逐特征明细：各列定宽不吞剩余空间，整体居中包裹避免超宽拉伸 */}
      {features.length > 0 ? (
        <div className="mx-auto mt-3 w-full" style={{ maxWidth: 980 }}>
          <Table<FactorReportRow>
            size="small"
            rowKey="name"
            columns={columns}
            dataSource={features}
            pagination={{ pageSize: 20, showSizeChanger: false, size: 'small' }}
            scroll={{ y: 380 }}
            title={() => (
              <div className="flex items-center justify-between">
                <span className="text-[11px] text-slate-500">共 {features.length} 个特征 · 入选 {features.filter((f) => f.status === 'selected').length} 个</span>
                <AntTooltip title="训练段覆盖 = 训练窗口内特征非空比例。低于 90% 说明上游数据存在缺失（如 L2 vpin 系 2024Q4-2025Q1 空窗），红色需排查。">
                  <span className="cursor-help text-[11px] text-slate-400">覆盖度说明</span>
                </AntTooltip>
              </div>
            )}
          />
        </div>
      ) : (
        <Empty description="本次训练未产出因子筛选报告" className="mt-4" />
      )}
    </Card>
  );
};

/** WFA 诊断解读：基于 IC 均值/标准差/正窗占比/ICIR 组合判断，输出可读结论 */
const WfaInterpretation: React.FC<{ wfa: any }> = ({ wfa }) => {
  if (!wfa || !wfa.enabled) return null;
  const icMean = Number(wfa.ic_mean);
  const icStd = Number(wfa.ic_std);
  const positiveRate = Number(wfa.positive_rate);
  const icir = Number(wfa.overall_icir);
  const hasIcir = Number.isFinite(icir) && !Number.isNaN(icir);

  const checks: Array<{ label: string; ok: boolean; text: string }> = [];
  if (icMean >= 0.05) checks.push({ label: 'IC 强度', ok: true, text: `IC均值 ${icMean.toFixed(4)} ≥ 0.05，信号强度良好` });
  else if (icMean >= 0) checks.push({ label: 'IC 强度', ok: true, text: `IC均值 ${icMean.toFixed(4)} 为正，信号有效但偏弱（<0.05）` });
  else checks.push({ label: 'IC 强度', ok: false, text: `IC均值 ${icMean.toFixed(4)} 为负，信号方向可能反了` });

  if (icStd <= 0.02) checks.push({ label: 'IC 稳定性', ok: true, text: `标准差 ${icStd.toFixed(4)} ≤ 0.02，各窗口波动小` });
  else checks.push({ label: 'IC 稳定性', ok: false, text: `标准差 ${icStd.toFixed(4)} > 0.02，各窗口波动偏大` });

  if (positiveRate >= 0.75) checks.push({ label: '正窗占比', ok: true, text: `${Math.round(positiveRate * 100)}% 窗口 IC 为正，跨期一致性好` });
  else if (positiveRate >= 0.5) checks.push({ label: '正窗占比', ok: true, text: `${Math.round(positiveRate * 100)}% 窗口为正，存在少数走弱窗口` });
  else checks.push({ label: '正窗占比', ok: false, text: `仅 ${Math.round(positiveRate * 100)}% 窗口为正，多数窗口失效` });

  if (hasIcir) {
    if (Math.abs(icir) >= 0.3) checks.push({ label: 'ICIR', ok: true, text: `ICIR ${icir.toFixed(3)}，收益/波动比合理` });
    else checks.push({ label: 'ICIR', ok: false, text: `ICIR ${icir.toFixed(3)} < 0.3，信号相对波动偏弱` });
  }

  const okCount = checks.filter(c => c.ok).length;

  return (
    <div className="mt-3 rounded-xl bg-slate-50/70 border border-slate-100 p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[9px] font-black uppercase tracking-wider text-slate-500">判断解读</div>
        <Text className={clsx('text-[9px] font-black', okCount === checks.length ? 'text-emerald-600' : okCount >= 2 ? 'text-amber-600' : 'text-rose-500')}>
          {okCount}/{checks.length} 项达标
        </Text>
      </div>
      <div className="space-y-1">
        {checks.map((c, i) => (
          <div key={i} className="flex items-start gap-1.5">
            <span className={clsx('mt-0.5 text-[8px] font-black', c.ok ? 'text-emerald-500' : 'text-rose-400')}>{c.ok ? '✓' : '✗'}</span>
            <Text className={clsx('text-[10px] leading-snug', c.ok ? 'text-slate-600' : 'text-rose-500/80')}>
              <span className="font-bold text-slate-500">{c.label}：</span>{c.text}
            </Text>
          </div>
        ))}
      </div>
      <Text className="block mt-2 text-[10px] text-slate-400 leading-relaxed">
        {okCount === checks.length
          ? '整体稳定可用，适合作为选股模型。'
          : okCount >= 2
            ? '多数维度达标，个别窗口波动可接受，建议关注 IC 表现最弱的区间。'
            : '多个维度未达标，建议调整特征/参数后重新训练，或考虑融合其他模型。'}
      </Text>
    </div>
  );
};

const LineageItem: React.FC<{ label: string; value?: string }> = ({ label, value }) => (
  <div className="flex items-center justify-between gap-2 rounded-xl bg-slate-50 px-3 py-2">
    <span className="shrink-0 text-slate-500">{label}</span>
    <span className="break-all font-mono text-[11px] text-slate-800">{value || '—'}</span>
  </div>
);

const SEGMENT_LABELS: Record<string, string> = { valid: '验证集', test: '测试集', custom: '自定义窗口' };

/** WP2 严格评估：净 Top-N（可成交 + 三档成本 + HAC/PSR + 换手）。 */
const StrictEvalSection: React.FC<{ runId?: string }> = ({ runId }) => {
  const [loading, setLoading] = React.useState(false);
  const [row, setRow] = React.useState<StrictEvalReportRow | null>(null);

  React.useEffect(() => {
    if (!runId) {
      setRow(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    modelTrainingService
      .getTrainingStrictReport(runId)
      .then((data) => {
        if (!cancelled) setRow(data);
      })
      .catch(() => {
        if (!cancelled) setRow(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  if (!runId) return null;

  const pct = (v?: number) => (v == null || Number.isNaN(v) ? '—' : `${(v * 100).toFixed(2)}%`);
  const num = (v?: number, digits = 2) =>
    v == null || Number.isNaN(v) ? '—' : v.toFixed(digits);
  const tone = (v?: number) =>
    v == null || Number.isNaN(v) ? 'text-slate-400' : v >= 0 ? 'text-emerald-600' : 'text-rose-500';

  return (
    <Card
      className="rounded-2xl border-indigo-100"
      size="small"
      title={
        <span className="text-sm font-black text-slate-800">
          严格评估
          <span className="ml-2 text-[10px] font-normal text-slate-400">
            净 Top-N · T+1 {row?.entry_price === 'close' ? '收盘' : '开盘'}买入 · 可成交 · 三档成本
          </span>
        </span>
      }
    >
      {loading ? (
        <div className="py-6 text-center text-xs text-slate-500">正在加载严格评估…</div>
      ) : null}
      {!loading && row?.status === 'running' ? (
        <div className="py-6 text-center text-xs text-slate-500">严格评估计算中…</div>
      ) : null}
      {!loading && (!row || row.status === 'failed') ? (
        <Alert
          type={row?.status === 'failed' ? 'error' : 'info'}
          showIcon
          className="rounded-2xl"
          message={row?.status === 'failed' ? '严格评估失败' : '严格评估尚未生成'}
          description={
            row?.error ||
            '训练完成后会自动计算净 Top-N；老模型可在后台点「重算严格评估」。'
          }
        />
      ) : null}
      {!loading && row?.status === 'ready' && row.report ? (
        <div className="space-y-4">
          {Object.entries(row.report.results || {}).map(([seg, tops]) => {
            const top20 = (tops['top20'] || {}) as Record<string, unknown>;
            const c15 = top20['cost_15bp'] as StrictEvalCostBlock | undefined;
            const c25 = top20['cost_25bp'] as StrictEvalCostBlock | undefined;
            const c40 = top20['cost_40bp'] as StrictEvalCostBlock | undefined;
            return (
              <div key={seg}>
                <div className="mb-2 flex items-center gap-2">
                  <Tag className="m-0 rounded-full border-0 bg-indigo-50 px-3 py-0.5 text-indigo-600">
                    {SEGMENT_LABELS[seg] || seg}
                  </Tag>
                  <span className="text-[10px] text-slate-400">
                    {row.report.segments?.[seg]?.start} ~ {row.report.segments?.[seg]?.end}
                  </span>
                </div>
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-slate-400">
                      <th className="py-1 text-left font-normal">Top-N</th>
                      <th className="py-1 text-right font-normal">批次</th>
                      <th className="py-1 text-right font-normal">25bp 收益</th>
                      <th className="py-1 text-right font-normal">MDD</th>
                      <th className="py-1 text-right font-normal">超额 HS300</th>
                      <th className="py-1 text-right font-normal">HAC t</th>
                      <th className="py-1 text-right font-normal">PSR</th>
                      <th className="py-1 text-right font-normal">换手/批</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(tops).map(([top, raw]) => {
                      const block = raw as Record<string, unknown>;
                      const c = block['cost_25bp'] as StrictEvalCostBlock | undefined;
                      if (!c) return null;
                      return (
                        <tr key={top} className="border-t border-slate-100">
                          <td className="py-1 font-semibold text-slate-700">
                            {top.replace('top', 'Top ')}
                          </td>
                          <td className="py-1 text-right text-slate-500">
                            {String(block.batches ?? '—')}
                          </td>
                          <td className={clsx('py-1 text-right font-semibold', tone(c.total))}>
                            {pct(c.total)}
                          </td>
                          <td className="py-1 text-right text-slate-600">{pct(c.mdd)}</td>
                          <td className={clsx('py-1 text-right', tone(c.excess_hs300))}>
                            {pct(c.excess_hs300)}
                          </td>
                          <td className="py-1 text-right text-slate-600">{num(c.hac_t)}</td>
                          <td className="py-1 text-right text-slate-600">{num(c.psr)}</td>
                          <td className="py-1 text-right text-slate-500">
                            {typeof block.turnover_per_batch === 'number'
                              ? `${(block.turnover_per_batch * 100).toFixed(0)}%`
                              : '—'}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                {c15 && c25 && c40 ? (
                  <div className="mt-1 text-[10px] text-slate-500">
                    Top20 成本敏感性：15bp {pct(c15.total)} · 25bp {pct(c25.total)} · 40bp{' '}
                    {pct(c40.total)}；95% bootstrap CI{' '}
                    [{pct(c25.bootstrap_ci?.[0])}, {pct(c25.bootstrap_ci?.[1])}]
                  </div>
                ) : null}
              </div>
            );
          })}
          <div className="text-[10px] leading-relaxed text-slate-400">
            口径：信号日收盘出分，T+1 {row.entry_price === 'close' ? '收盘' : '开盘'}买入，T+1+
            {row.horizon_days} 收盘卖出；ST/停牌/涨停买不进按 trade_mask 剔除；成本为往返；每只固定名义本金，
            参与率按当日成交额计算。
          </div>
        </div>
      ) : null}
    </Card>
  );
};

/** WP6 run 对比：贴 run_id（2~5 个），同口径严格评估 + 配置 diff + 单轴判定。 */
const CompareRunsModal: React.FC<{
  open: boolean;
  onClose: () => void;
  defaultRunId?: string;
}> = ({ open, onClose, defaultRunId }) => {
  const [idsText, setIdsText] = React.useState('');
  const [loading, setLoading] = React.useState(false);
  const [data, setData] = React.useState<TrainingRunCompareResult | null>(null);

  React.useEffect(() => {
    if (open) {
      setIdsText(defaultRunId ? `${defaultRunId},` : '');
      setData(null);
    }
  }, [open, defaultRunId]);

  const runCompare = async () => {
    const ids = idsText
      .split(/[\s,]+/)
      .map((x) => x.trim())
      .filter(Boolean);
    if (ids.length < 2) {
      setData(null);
      return;
    }
    setLoading(true);
    const result = await modelTrainingService.compareTrainingRuns(ids);
    setData(result);
    setLoading(false);
  };

  const pct = (v?: number) => (v == null || Number.isNaN(v) ? '—' : `${(v * 100).toFixed(2)}%`);
  const num = (v?: number, d = 2) => (v == null || Number.isNaN(v) ? '—' : v.toFixed(d));

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width={920}
      title="训练 run 对比（严格评估口径）"
    >
      <div className="space-y-3">
        <div className="text-xs text-slate-500">
          第一行作为基线。配置 diff 只比较实验字段（特征/参数/标签/切分/样本权重）；
          只差一个轴才算严格 A/B。
        </div>
        <Input.TextArea
          rows={2}
          value={idsText}
          onChange={(e) => setIdsText(e.target.value)}
          placeholder="逗号或换行分隔 run_id，2~5 个，例如 train_xxx,train_yyy"
        />
        <div className="flex items-center gap-2">
          <Button type="primary" size="small" loading={loading} onClick={runCompare}>
            对比
          </Button>
          <span className="text-[11px] text-slate-400">run_id 取自训练任务详情</span>
        </div>
        {data ? (
          <div className="space-y-3">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-slate-400">
                  <th className="py-1 text-left font-normal">run</th>
                  <th className="py-1 text-left font-normal">模型</th>
                  <th className="py-1 text-right font-normal">特征</th>
                  <th className="py-1 text-right font-normal">seed</th>
                  <th className="py-1 text-right font-normal">test RankIC</th>
                  <th className="py-1 text-right font-normal">Top20 25bp</th>
                  <th className="py-1 text-right font-normal">MDD</th>
                  <th className="py-1 text-right font-normal">HAC t</th>
                  <th className="py-1 text-center font-normal">单轴</th>
                </tr>
              </thead>
              <tbody>
                {data.runs.map((row) => (
                  <tr key={row.run_id} className="border-t border-slate-100">
                    <td className="py-1 pr-2 font-mono text-[10px] text-slate-600 break-all">
                      {row.run_id}
                      {row.run_id === data.baseline_run_id ? (
                        <Tag className="ml-1 rounded-full border-0 bg-slate-100 px-2 py-0 text-slate-500">
                          基线
                        </Tag>
                      ) : null}
                    </td>
                    <td className="py-1 text-slate-600">{row.model_type || '—'}</td>
                    <td className="py-1 text-right text-slate-600">{row.feature_count ?? '—'}</td>
                    <td className="py-1 text-right text-slate-600">{row.seed ?? '—'}</td>
                    <td className="py-1 text-right text-slate-600">{num(row.test_rank_ic, 4)}</td>
                    <td className="py-1 text-right font-semibold text-slate-700">
                      {pct(row.strict?.total_25bp)}
                    </td>
                    <td className="py-1 text-right text-slate-600">{pct(row.strict?.mdd)}</td>
                    <td className="py-1 text-right text-slate-600">{num(row.strict?.hac_t)}</td>
                    <td className="py-1 text-center">
                      {row.run_id === data.baseline_run_id ? (
                        '—'
                      ) : row.single_axis ? (
                        <Tag className="m-0 rounded-full border-0 bg-emerald-50 px-2 text-emerald-600">
                          单轴
                        </Tag>
                      ) : (
                        <Tag className="m-0 rounded-full border-0 bg-rose-50 px-2 text-rose-500">
                          多轴
                        </Tag>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.runs
              .filter((row) => row.run_id !== data.baseline_run_id)
              .map((row) => (
                <div key={`diff-${row.run_id}`} className="rounded-xl bg-slate-50 p-2">
                  <div className="text-[11px] font-semibold text-slate-600">
                    {row.run_id} 相对基线差异（{row.diff_axes?.join(' / ') || '无'}）
                  </div>
                  <div className="mt-1 space-y-0.5 text-[11px] text-slate-600">
                    {(row.diff_vs_baseline || []).map((d) => (
                      <div key={d.path} className="font-mono break-all">
                        <span className="text-slate-400">{d.path}</span>：{String(d.baseline)} →{' '}
                        {String(d.candidate)}
                      </div>
                    ))}
                    {!row.diff_vs_baseline?.length ? <div>配置一致</div> : null}
                  </div>
                </div>
              ))}
          </div>
        ) : null}
      </div>
    </Modal>
  );
};

/** WP7 数据审计：未来列/标签契约/trade_mask/ST/退市/指纹/季度 PIT 探针。 */
const AuditSection: React.FC<{ runId?: string }> = ({ runId }) => {
  const [loading, setLoading] = React.useState(false);
  const [row, setRow] = React.useState<TrainingAuditRow | null>(null);

  React.useEffect(() => {
    if (!runId) {
      setRow(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    modelTrainingService
      .getTrainingAuditReport(runId)
      .then((data) => {
        if (!cancelled) setRow(data);
      })
      .catch(() => {
        if (!cancelled) setRow(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  if (!runId) return null;

  const levelTone: Record<string, string> = {
    pass: 'bg-emerald-50 text-emerald-600',
    fail: 'bg-rose-50 text-rose-500',
    warn: 'bg-amber-50 text-amber-600',
    info: 'bg-slate-100 text-slate-500',
  };

  return (
    <Card
      className="rounded-2xl border-slate-200"
      size="small"
      title={
        <span className="text-sm font-black text-slate-800">
          数据审计
          <span className="ml-2 text-[10px] font-normal text-slate-400">
            PIT / 泄漏 / 可成交 / 复现
          </span>
        </span>
      }
    >
      {loading ? <div className="py-4 text-center text-xs text-slate-500">正在加载数据审计…</div> : null}
      {!loading && (!row || row.status === 'failed') ? (
        <Alert
          type={row?.status === 'failed' ? 'error' : 'info'}
          showIcon
          className="rounded-2xl"
          message={row?.status === 'failed' ? '数据审计失败' : '数据审计尚未生成'}
          description={row?.error || '训练完成后会自动运行；老模型可在后台点「重算数据审计」。'}
        />
      ) : null}
      {!loading && row?.status === 'ready' && row.report ? (
        <div className="space-y-2">
          {row.report.blocked ? (
            <Alert
              type="error"
              showIcon
              className="rounded-2xl"
              message="审计未通过：该模型不可晋升"
              description="存在 fail 级检查（见下方明细）。"
            />
          ) : null}
          {(row.report.checks || []).map((check) => (
            <div key={check.id} className="flex items-start gap-2 text-xs">
              <Tag
                className={clsx(
                  'm-0 shrink-0 rounded-full border-0 px-2 py-0 text-[10px]',
                  levelTone[check.status === 'fail' ? 'fail' : check.level] || levelTone.info,
                )}
              >
                {check.status === 'fail' ? '未通过' : check.level === 'info' ? '信息' : '通过'}
              </Tag>
              <div className="min-w-0">
                <span className="font-mono text-[11px] text-slate-600">{check.id}</span>
                <span className="ml-2 text-slate-500">{check.detail}</span>
              </div>
            </div>
          ))}
          {row.report.warnings?.length ? (
            <div className="rounded-xl bg-amber-50/60 p-2 text-[11px] text-amber-700">
              {row.report.warnings.map((w, i) => (
                <div key={i}>• {w}</div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
};

export const TrainingResultView: React.FC<TrainingResultViewProps> = ({
  result,
  resultError,
  settingDefaultModel,
  onSetDefaultModel,
  onExportConfig,
  trainingStatus,
  runId,
}) => {
  const [compareOpen, setCompareOpen] = React.useState(false);
  if (!result && !resultError) {
    return (
      <Card className="rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20 } }}>
         <SectionHeader
          title="第五步：结果入库"
          desc="展示训练完成后会进入模型管理页的元数据与产物预览。"
          icon={<BarChart size={18} className="text-indigo-500" />}
        />
        <Divider className="my-4" />
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="先执行训练，再查看结果摘要" />
      </Card>
    );
  }

  // 入模因子：因子筛选报告中 status=selected 的特征
  const selectedFactors: FactorReportRow[] = (
    (result?.metadata?.factor_selection?.features ?? []) as FactorReportRow[]
  ).filter((f) => f.status === 'selected');

  // WP1 输入漏斗：请求原文 → 目录解析 → 实际入模（老模型没有新字段时退化为原值）
  const originalFeatureCount =
    result?.metadata.requested_feature_count_original
    ?? result?.metadata.requested_features_original?.length
    ?? result?.metadata.requested_feature_count
    ?? 0;
  const resolvedFeatureCount =
    result?.metadata.resolved_feature_count
    ?? result?.metadata.resolved_features?.length
    ?? result?.metadata.requested_feature_count
    ?? 0;
  const trainedFeatureCount = result?.metadata.feature_count ?? 0;
  const missingFeatures = result?.metadata.missing_features ?? [];
  const lineage = result?.metadata.lineage;
  const catalogVersions = Object.entries(lineage?.factor_catalog_versions || {})
    .map(([lib, vid]) => `${lib}:${String(vid).slice(-8)}`)
    .join(' · ');

  return (
    <div className="space-y-4">
      {/* 第一排：结果入库概览全宽横条（状态 + 注册 + 关键指标 + 产物） */}
      <Card className="rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20 } }}>
        <SectionHeader
          title="第五步：结果入库"
          desc="展示训练完成后会进入模型管理页的元数据与产物预览。"
          icon={<BarChart size={18} className="text-indigo-500" />}
        />
        <Divider className="my-4" />
        {resultError ? (
          <Alert type="error" showIcon className="mb-4 rounded-2xl" message="训练结果异常" description={resultError} />
        ) : null}
        {result ? (
          <div className="space-y-4">
            {result.metrics?.score_direction === 'reversed' && (
              <Alert
                type="warning"
                showIcon
                message="检测到反向模型"
                description="验证集 IC < 0，模型预测方向与实际收益相反（高分=跌，低分=涨）。推理时已自动翻转分数，但建议检查特征选择是否合理或重新训练。"
                className="rounded-2xl border-amber-100 bg-amber-50/70"
              />
            )}
            <Alert
              type="success"
              showIcon
              message={result.summary.status}
              description={result.summary.notes}
              className="rounded-2xl border-emerald-100 bg-emerald-50/70"
            />

            <Card className="rounded-2xl border-slate-200" size="small" title="模型注册与同步状态">
              <div className="flex flex-wrap items-center gap-3">
                <Tag
                  className={clsx(
                    'm-0 rounded-full border-0 px-3 py-1',
                    result.modelRegistration?.status === 'ready'
                      ? 'bg-emerald-50 text-emerald-600'
                      : result.modelRegistration?.status === 'failed'
                        ? 'bg-rose-50 text-rose-600'
                        : 'bg-amber-50 text-amber-600',
                  )}
                >
                  {result.modelRegistration?.status || 'unknown'}
                </Tag>
                {result.metadata.market ? (
                  <Tag className="m-0 rounded-full border-0 bg-blue-50 px-3 py-1 text-blue-600">
                    {MARKET_LABELS[result.metadata.market.toUpperCase()] || result.metadata.market}
                  </Tag>
                ) : null}
                <Text className="text-xs text-slate-600">
                  model_id: {result.modelRegistration?.modelId || result.modelId}
                </Text>
                <Button
                  size="small"
                  type="primary"
                  className="rounded-xl bg-blue-600"
                  loading={settingDefaultModel}
                  disabled={result.modelRegistration?.status !== 'ready'}
                  onClick={onSetDefaultModel}
                >
                  设为默认模型
                </Button>
                <Button
                  size="small"
                  icon={<Download size={14} />}
                  className="rounded-xl"
                  onClick={onExportConfig}
                >
                  导出训练配置
                </Button>
                <Button
                  size="small"
                  className="rounded-xl"
                  disabled={!runId}
                  onClick={() => setCompareOpen(true)}
                >
                  对比其他 run
                </Button>
              </div>
            </Card>

            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
              <MetricCard
                label="模型标识"
                value={result.modelId}
                hint={result.modelName}
                centered
                valueClassName="text-sm leading-tight break-all"
                hintClassName="text-[10px] leading-tight break-all"
              />
              <MetricCard
                label="T+N"
                value={`T+${result.metadata.target_horizon_days}`}
                hint={result.metadata.target_mode === 'classification' ? '分类目标' : '回归目标'}
                centered
              />
              <MetricCard
                label="请求特征数"
                value={`${originalFeatureCount}`}
                hint={`目录解析 ${resolvedFeatureCount} 个`}
                centered
              />
              <MetricCard
                label="实际入模特征数"
                value={`${trainedFeatureCount}`}
                hint={
                  missingFeatures.length
                    ? `缺失 ${missingFeatures.length} 个`
                    : result.metadata.feature_categories.join(' / ') || '—'
                }
                centered
              />
            </div>

            {missingFeatures.length > 0 ? (
              <Alert
                type="error"
                showIcon
                className="rounded-2xl"
                message={`${missingFeatures.length} 个请求特征未进入训练`}
                description={
                  <span className="text-xs">
                    目录解析后物理列缺失：{missingFeatures.slice(0, 20).join('、')}
                    {missingFeatures.length > 20 ? ` …(+${missingFeatures.length - 20})` : ''}
                  </span>
                }
              />
            ) : null}

            {lineage ? (
              <Card className="rounded-2xl border-slate-200" size="small" title="训练血缘（可复现）">
                <div className="grid gap-2 text-xs sm:grid-cols-2 xl:grid-cols-3">
                  <LineageItem label="config hash" value={lineage.config_hash} />
                  <LineageItem
                    label="git commit"
                    value={lineage.git_commit ? lineage.git_commit.slice(0, 10) : ''}
                  />
                  <LineageItem label="镜像" value={lineage.app_image} />
                  <LineageItem
                    label="seed"
                    value={lineage.seed !== undefined ? String(lineage.seed) : ''}
                  />
                  <LineageItem label="因子目录" value={catalogVersions} />
                  <LineageItem
                    label="schema hash"
                    value={lineage.factor_schema_hash ? lineage.factor_schema_hash.slice(0, 12) : ''}
                  />
                </div>
              </Card>
            ) : null}

            {/* 模型评估报告（预测强弱：IC 时序 / 分层收益 / 多空组合） */}
            <EvalReportSection report={result.eval_report || result.metadata.eval_report} />

            {/* WP2 严格评估：净 Top-N / 可成交 / 三档成本 / HAC / PSR */}
            <StrictEvalSection runId={runId} />

            {/* WP7 数据审计：PIT / 泄漏 / 可成交 / 复现 */}
            <AuditSection runId={runId} />

            <Card className="rounded-2xl border-slate-200" size="small" title="建议落盘文件">
              <div className="flex flex-wrap gap-2">
                {result.artifacts.map((artifact) => (
                  <Tag key={artifact} className="m-0 rounded-full border-0 bg-indigo-50 px-3 py-1 text-indigo-600">
                    {artifact}
                  </Tag>
                ))}
              </div>
            </Card>
          </div>
        ) : null}
      </Card>

      {/* 第二排：元数据预览 + 结果摘要平铺全宽 */}
      {result ? (
      <div className="grid gap-4 md:grid-cols-2">
      <Card className="h-full rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20, height: '100%', display: 'flex', flexDirection: 'column' } }}>
        <div className="mb-3 flex items-center gap-2">
          <BarChart size={15} className="text-indigo-500" />
          <span className="text-sm font-black text-slate-800">模型元数据预览</span>
        </div>
        <div className="space-y-2">
          {([
            { zh: '展示名称', en: 'display_name', value: <Text code className="text-[11px] break-all">{result.metadata.display_name}</Text> },
            { zh: '预测类型', en: 'target_mode', value: <span>{getTargetModeDescription(result.metadata.target_mode)}</span> },
            { zh: '标签公式', en: 'label_formula', value: <Text code className="text-[11px] break-all">{result.metadata.label_formula}</Text> },
            { zh: '时间窗口', en: 'training_window', value: <Text code className="text-[11px] break-all">{result.metadata.training_window}</Text> },
            { zh: '目标函数', en: 'objective_metric', value: <span>{getObjectiveMetricDescription(result.metadata.objective, result.metadata.metric)}</span> },
          ] as { zh: string; en: string; value: React.ReactNode }[]).map((it) => (
            <div key={it.en} className="rounded-xl border border-slate-100 bg-slate-50/70 px-3 py-2">
              <div className="text-[9px] font-bold tracking-wider text-slate-400">{it.zh} · <span className="font-mono">{it.en}</span></div>
              <div className="mt-1 text-[11px] font-semibold leading-relaxed text-slate-800 break-all">{it.value}</div>
            </div>
          ))}
        </div>

        {/* 入模因子：只显示名称，标签流一行多个，填充等高后的剩余区域 */}
        {selectedFactors.length > 0 && (
          <div className="mt-4 flex flex-col">
            <div className="mb-2 flex items-center justify-between text-[9px] font-bold uppercase tracking-wider text-slate-400">
              <span>入模因子</span>
              <span>{selectedFactors.length} 个</span>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {selectedFactors.map((f) => (
                <span key={f.name} className="rounded-md border border-indigo-100 bg-indigo-50/70 px-2 py-0.5 font-mono text-[10px] font-semibold text-indigo-600">
                  {f.name}
                </span>
              ))}
            </div>
          </div>
        )}
      </Card>

      <Card className="h-full rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20 } }}>
        <div className="mb-3 flex items-center gap-2">
          <MonitorPlay size={15} className="text-indigo-500" />
          <span className="text-sm font-black text-slate-800">结果摘要</span>
          <span className="text-[10px] text-slate-400">给模型管理页与后续回放使用的最小信息集合</span>
        </div>
        {result ? (
          <div className="space-y-4">
            <MetricCard label="结果状态" value={trainingStatus === 'completed' ? '已生成' : '等待完成'} hint={result.completedAt ? dayjs(result.completedAt).format('YYYY-MM-DD HH:mm:ss') : ''} />
            
            {result.metrics && (
              <div className="rounded-2xl border border-slate-200 bg-white p-4">
                <div className="mb-3 flex items-center justify-between">
                  <div className="text-[10px] font-black uppercase tracking-[0.18em] text-slate-400">IC 评估图表</div>
                </div>
                <ResponsiveContainer width="100%" height={200}>
                  <ReBarChart
                    data={[
                      { name: '训练集', IC: result.metrics.train.ic, RankIC: result.metrics.train.rank_ic },
                      { name: '验证集', IC: result.metrics.val.ic, RankIC: result.metrics.val.rank_ic },
                      { name: '测试集', IC: result.metrics.test.ic, RankIC: result.metrics.test.rank_ic },
                    ]}
                    barCategoryGap="30%"
                    margin={{ top: 10, right: 10, left: -10, bottom: 0 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                    <XAxis dataKey="name" tick={{ fontSize: 11, fill: '#64748b' }} axisLine={false} tickLine={false} />
                    <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} axisLine={false} tickLine={false} tickFormatter={(v: number) => v.toFixed(2)} />
                    <Tooltip
                      contentStyle={{ borderRadius: 12, fontSize: 11 }}
                      formatter={(value: number) => Number(value).toFixed(4)}
                    />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    <ReferenceLine y={0.05} stroke="#f59e0b" strokeDasharray="5 3" />
                    <ReferenceLine y={0.10} stroke="#10b981" strokeDasharray="5 3" />
                    <Bar dataKey="IC" fill="#6366f1" radius={[4, 4, 0, 0]} />
                    <Bar dataKey="RankIC" fill="#06b6d4" radius={[4, 4, 0, 0]} />
                  </ReBarChart>
                </ResponsiveContainer>
                
                <div className="mt-3 grid grid-cols-3 gap-2">
                  {(['train', 'val', 'test'] as const).map((split, i) => {
                    const labels = ['训练集', '验证集', '测试集'];
                    const seg = result.metrics![split];
                    const icVal = seg.ic;
                    const color = icVal >= 0.10 ? 'text-emerald-600' : icVal >= 0.05 ? 'text-amber-600' : 'text-rose-500';
                    return (
                      <div key={split} className="rounded-xl bg-slate-50 px-3 py-2 text-center">
                        <div className="text-[9px] font-semibold uppercase tracking-wider text-slate-400">{labels[i]}</div>
                        <div className={`mt-0.5 text-sm font-bold ${color}`}>{icVal.toFixed(4)}</div>
                        <div className="text-[9px] text-slate-400">RankIC {seg.rank_ic.toFixed(4)}</div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {result.wfa?.enabled && result.wfa.windows?.length > 0 && (
              <div className="rounded-2xl border border-violet-200 bg-white p-4">
                <div className="mb-3 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Activity size={14} className="text-violet-500" />
                    <div className="text-[10px] font-black uppercase tracking-[0.18em] text-slate-400">
                      WFA 稳定性诊断
                    </div>
                  </div>
                  <Tag
                    className={clsx('m-0 rounded-full border-0 px-2.5 py-0.5', result.wfa.stability === 'stable' ? 'bg-emerald-50 text-emerald-600' : 'bg-amber-50 text-amber-600')}
                  >
                    {result.wfa.stability === 'stable' ? '稳定' : '不稳定'}
                  </Tag>
                </div>

                <div className="mb-4 grid grid-cols-2 gap-2">
                  <div className="rounded-xl bg-slate-50 px-3 py-2 text-center">
                    <div className="text-[9px] font-semibold uppercase tracking-wider text-slate-400">IC 均值</div>
                    <div className={`mt-0.5 text-sm font-bold ${result.wfa.ic_mean >= 0.05 ? 'text-emerald-600' : result.wfa.ic_mean >= 0 ? 'text-amber-600' : 'text-rose-500'}`}>
                      {Number(result.wfa.ic_mean).toFixed(4)}
                    </div>
                  </div>
                  <div className="rounded-xl bg-slate-50 px-3 py-2 text-center">
                    <div className="text-[9px] font-semibold uppercase tracking-wider text-slate-400">IC 标准差</div>
                    <div className={`mt-0.5 text-sm font-bold ${result.wfa.ic_std <= 0.02 ? 'text-emerald-600' : 'text-amber-600'}`}>
                      {Number(result.wfa.ic_std).toFixed(4)}
                    </div>
                  </div>
                  <div className="rounded-xl bg-slate-50 px-3 py-2 text-center">
                    <div className="text-[9px] font-semibold uppercase tracking-wider text-slate-400">ICIR</div>
                    <div className={`mt-0.5 text-sm font-bold ${Number(result.wfa.overall_icir) >= 0.3 ? 'text-emerald-600' : 'text-slate-700'}`}>
                      {Number.isFinite(Number(result.wfa.overall_icir)) ? Number(result.wfa.overall_icir).toFixed(3) : '—'}
                    </div>
                  </div>
                  <div className="rounded-xl bg-slate-50 px-3 py-2 text-center">
                    <div className="text-[9px] font-semibold uppercase tracking-wider text-slate-400">正窗占比</div>
                    <div className="mt-0.5 text-sm font-bold text-slate-700">{Math.round(Number(result.wfa.positive_rate) * 100)}%</div>
                  </div>
                </div>

                <ResponsiveContainer width="100%" height={180}>
                  <ReBarChart
                    data={result.wfa.windows.map(w => ({
                      name: `W${w.window_idx + 1}`,
                      IC: Number(w.ic),
                      RankIC: Number(w.rank_ic),
                    }))}
                    barCategoryGap="25%"
                    margin={{ top: 10, right: 10, left: -10, bottom: 0 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                    <XAxis dataKey="name" tick={{ fontSize: 10, fill: '#64748b' }} axisLine={false} tickLine={false} />
                    <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} axisLine={false} tickLine={false} tickFormatter={(v: number) => v.toFixed(2)} />
                    <Tooltip
                      contentStyle={{ borderRadius: 12, fontSize: 11 }}
                      formatter={(value: any, name: string, props: any) => {
                        const formatted = Number(value).toFixed(4);
                        const w = result.wfa?.windows?.[props?.payload?.payloadIndex ?? 0];
                        if (w && name === 'IC') {
                          return [formatted, `${w.val_start} ~ ${w.val_end}`];
                        }
                        return [formatted, name];
                      }}
                    />
                    <Legend wrapperStyle={{ fontSize: 10 }} />
                    <ReferenceLine y={0} stroke="#cbd5e1" />
                    <Bar dataKey="IC" fill="#8b5cf6" radius={[4, 4, 0, 0]} />
                    <Bar dataKey="RankIC" fill="#a78bfa" radius={[4, 4, 0, 0]} />
                  </ReBarChart>
                </ResponsiveContainer>

                <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px] text-slate-400">
                  <span className="font-mono">{result.wfa.windows.length} 个窗口</span>
                  <span>·</span>
                  <span>{result.wfa.strategy === 'rolling' ? '滚动窗口' : '扩张窗口'}</span>
                  <span>·</span>
                  <span>模型: {result.wfa.model_type}</span>
                  <span>·</span>
                  <span>IC 区间 [{Number(result.wfa.ic_min).toFixed(4)}, {Number(result.wfa.ic_max).toFixed(4)}]</span>
                </div>

                {/* 判断解读 */}
                <WfaInterpretation wfa={result.wfa} />
              </div>
            )}

            <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
              <div className="text-[10px] font-black uppercase tracking-[0.22em] text-slate-400">后续动作</div>
              <div className="mt-2 text-sm text-slate-700">
                1. 将 metadata.json 写入模型目录<br/>
                2. 在模型管理页展示 T+N / label_formula<br/>
                3. 将相同口径带入回测中心复用
              </div>
            </div>
          </div>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="训练完成后，这里会展示元数据摘要" />
        )}
      </Card>
      </div>
      ) : null}

      {result?.metadata?.factor_selection ? (
        <FactorSelectionReport report={result.metadata.factor_selection} />
      ) : null}

      <CompareRunsModal
        open={compareOpen}
        onClose={() => setCompareOpen(false)}
        defaultRunId={runId}
      />
    </div>
  );
};

/** 模型评估报告：预测强弱诊断（RankIC 稳健性 / 十分位分层收益 / 多空组合） */
const EvalReportSection: React.FC<{ report?: EvalReport }> = ({ report }) => {
  if (!report || report.error) return null;
  const ls: NonNullable<EvalReport['long_short']> = report.long_short || {};
  const groups: NonNullable<EvalReport['groups']> = report.groups || {};
  const groupData = (groups.mean_returns || []).map((v, i) => ({
    group: `G${i + 1}`,
    ret: v == null ? null : Number((v * 100).toFixed(3)),
  }));
  const lsCurve = (ls.curve || []).map(([d, v]) => ({
    date: String(d).slice(2, 10),
    v: Number((v * 100).toFixed(2)),
  }));
  const fmt = (v: number | null | undefined, digits = 2, scale = 1) =>
    v == null || Number.isNaN(v) ? '—' : `${(v * scale).toFixed(digits)}${scale === 100 ? '%' : ''}`;
  const lvl = (v: number | null | undefined, good: number, ok: number) =>
    v == null ? 'text-slate-400' : v >= good ? 'text-emerald-600' : v >= ok ? 'text-amber-600' : 'text-rose-500';
  const basisText = report.return_basis === 'label_return' ? '真实收益口径' : '秩标签口径';
  const bySplit = report.by_split || {} as NonNullable<EvalReport['by_split']>;
  const splitLabel: Record<string, string> = { train: '训练', valid: '验证', test: '测试' };
  // 主展示口径：优先测试段（样本外）——headline 是全窗合计（含训练段，约虚高 1.5 倍）。
  // 无 test 段（老报告）时回落全窗，并在标签上如实标注口径，不让两种口径看起来一样。
  // 口径解析钉在 resolveEvalIcHeadline（trainingUtils），单测锁定「不静默冒充样本外」。
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
      <div className="grid grid-cols-3 gap-2 md:grid-cols-6">
        {/* 染色断点对齐样本外阶梯（backend/scripts/eval/model_card.py 的 score_oos：
            IC 0.04/0.08、ICIR 0.3/0.5）。沿用旧的全窗尺度断点（0.03/0.015）会因
            测试段普遍低于全窗而全部染绿（49 份 CN 报告 test 段 IC 全部 ≥0.05），失去分辨力。 */}
        {[
          { label: `RankIC ${icHead.tag}`, value: fmt(icHead.mean, 4), cls: lvl(icHead.mean, 0.08, 0.04) },
          { label: `ICIR ${icHead.tag}`, value: fmt(icHead.icir, 3), cls: lvl(icHead.icir, 0.5, 0.3) },
          { label: `IC 胜率 ${icHead.tag}`, value: fmt(icHead.winRate, 1, 100), cls: lvl(icHead.winRate, 0.6, 0.5) },
          { label: '分层单调性', value: fmt(groups.monotonicity, 3), cls: lvl(groups.monotonicity, 0.9, 0.6) },
          { label: '多空夏普', value: fmt(ls.sharpe, 2), cls: lvl(ls.sharpe, 1.0, 0.5) },
          { label: '多空年化', value: fmt(ls.ann_return, 1, 100), cls: 'text-slate-800' },
        ].map((it) => (
          <div key={it.label} className="rounded-xl border border-emerald-100 bg-white px-2 py-1.5 text-center">
            <div className="text-[9px] font-bold tracking-wider text-slate-400">{it.label}</div>
            <div className={clsx('mt-0.5 text-sm font-black', it.cls)}>{it.value}</div>
          </div>
        ))}
      </div>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <div className="rounded-xl border border-slate-100 bg-white p-2">
          <div className="mb-1 text-[10px] font-bold text-slate-500">
            十分位分层平均收益（G1=预测最弱，G{groups.n_groups || 10}=最强）
          </div>
          <ResponsiveContainer width="100%" height={150}>
            <ReBarChart data={groupData} margin={{ top: 4, right: 4, bottom: 0, left: -18 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
              <XAxis dataKey="group" tick={{ fontSize: 9 }} />
              <YAxis tick={{ fontSize: 9 }} />
              <Tooltip formatter={(v) => [v == null ? '—' : `${v}%`, '平均收益']} />
              <Bar dataKey="ret" fill="#10b981" radius={[3, 3, 0, 0]} />
            </ReBarChart>
          </ResponsiveContainer>
        </div>
        <div className="rounded-xl border border-slate-100 bg-white p-2">
          <div className="mb-1 text-[10px] font-bold text-slate-500">多空组合累计净值（Top − Bottom，%）</div>
          <ResponsiveContainer width="100%" height={150}>
            <ReLineChart data={lsCurve} margin={{ top: 4, right: 4, bottom: 0, left: -18 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
              <XAxis dataKey="date" tick={{ fontSize: 9 }} minTickGap={48} />
              <YAxis tick={{ fontSize: 9 }} />
              <Tooltip formatter={(v) => [`${v}%`, '累计净值']} />
              <ReferenceLine y={0} stroke="#94a3b8" strokeDasharray="2 2" />
              <Line type="monotone" dataKey="v" stroke="#6366f1" dot={false} strokeWidth={1.6} />
            </ReLineChart>
          </ResponsiveContainer>
        </div>
      </div>
      {(Object.keys(bySplit).length > 0 || (report.yearly || []).length > 0) && (
        <div className="mt-3 flex flex-wrap gap-2">
          {Object.entries(bySplit).map(([name, s]) => (
            <span key={name} className="rounded-lg border border-slate-100 bg-slate-50/70 px-2 py-1 text-[10px] text-slate-600">
              <b className="mr-1 text-slate-700">{splitLabel[name] || name}</b>
              IC {fmt(s.mean, 4)} · ICIR {fmt(s.icir, 2)} · 胜率 {fmt(s.win_rate, 0, 100)}
            </span>
          ))}
          {(report.yearly || []).map((y) => (
            <span key={y.year} className="rounded-lg border border-indigo-50 bg-indigo-50/50 px-2 py-1 text-[10px] text-indigo-700">
              <b className="mr-1">{y.year}</b>IC {fmt(y.mean, 3)} · ICIR {fmt(y.icir, 2)}
            </span>
          ))}
        </div>
      )}
      <div className="mt-2 text-[10px] text-slate-400">
        磁贴 RankIC/ICIR/胜率口径：{icHead.isTest ? '测试段（样本外）' : '全窗合计（该报告无测试段）'}；
        分层与多空为全窗口径（含训练段）。分层收益为未来收益口径（T+1 执行、持有 N 日）；
        多空净值按每日 Top−Bottom 平均收益复利累计，未扣交易成本；IC 胜率 = 日 RankIC 为正的比例。
      </div>
    </Card>
  );
};
