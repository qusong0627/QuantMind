/**
 * 组合选股 · 证据与回放。
 *
 * 数据只来自 P1 研究产物 `/models/blends/evidence`：**前端不现算、不编数**。
 * 本轮研究结论是 17 个候选全部 `not_recommended`（K-R4），页面如实展示
 * 「暂无推荐组合」与判据，不把「基线」包装成「最优」。
 */

import React from 'react';
import { Alert, Descriptions, Empty, Spin, Table, Tag, Tooltip } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type {
  BlendEvidenceResponse,
  EvidenceCandidate,
  EvidenceWindowStats,
} from '../../../../services/blendTypes';

export interface ComboEvidencePanelProps {
  evidence: BlendEvidenceResponse | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}

const WINDOW_LABEL: Record<string, string> = {
  selection: '选择窗',
  validation: '验证窗',
  test: '检验窗',
};

const VERDICT_META: Record<string, { label: string; color: string }> = {
  // 合规：结论标签只说「是否达到预设的证据标准」，不出现「推荐」等投资判断措辞
  not_recommended: { label: '未达证据标准', color: 'default' },
  recommended: { label: '达到证据标准', color: 'blue' },
  watch: { label: '观察', color: 'gold' },
};

function pct(value: number | null | undefined, digits = 1): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

function deltaPct(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)} pp`;
}

function num(value: number | null | undefined, digits = 3): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return value.toFixed(digits);
}

function splitLabel(split: unknown): string {
  if (Array.isArray(split) && split.length >= 2) return `${split[0]} ~ ${split[1]}`;
  if (split && typeof split === 'object') {
    const obj = split as { start?: string; end?: string };
    if (obj.start || obj.end) return `${obj.start ?? '?'} ~ ${obj.end ?? '?'}`;
  }
  return '—';
}

function WindowDetail({ candidate }: { candidate: EvidenceCandidate }) {
  const rows = Object.entries(candidate.windows ?? {}).map(([key, stats]) => ({
    key,
    ...(stats as EvidenceWindowStats),
  }));
  return (
    <div className="px-4 py-2 space-y-2" data-testid="combo-evidence-window-detail">
      <table className="text-[11px]">
        <thead>
          <tr className="text-slate-400">
            {['窗口', '交易日', '命中(理论)', '命中(可执行)', 'Top20命中', '次日收益(理论)', '次日收益(可执行净)', '日均评分标的', '日均入选'].map((h) => (
              <th key={h} className="pr-5 text-left font-semibold whitespace-nowrap">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className="text-slate-700">
              <td className="pr-5 font-bold">{WINDOW_LABEL[row.key] ?? row.key}</td>
              <td className="pr-5 font-mono">{row.days ?? '—'}</td>
              <td className="pr-5 font-mono">{pct(row.hit_theory_mean)}</td>
              <td className="pr-5 font-mono">{pct(row.hit_exec_mean)}</td>
              <td className="pr-5 font-mono">{pct(row.hit_exec_top20)}</td>
              <td className="pr-5 font-mono">{pct(row.ret_theory_mean, 3)}</td>
              <td className="pr-5 font-mono">{pct(row.ret_exec_net_mean, 3)}</td>
              <td className="pr-5 font-mono">{num(row.n_scored_mean, 0)}</td>
              <td className="pr-5 font-mono">{num(row.n_entry_mean, 0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {candidate.failed_checks?.length ? (
        <div className="flex items-center gap-1 flex-wrap">
          <span className="text-[10px] text-slate-400">未通过判据：</span>
          {candidate.failed_checks.map((check) => (
            <Tag key={check} color="default" className="text-[9px]">{check}</Tag>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export const ComboEvidencePanel: React.FC<ComboEvidencePanelProps> = ({
  evidence,
  loading,
  error,
  onRetry,
}) => {
  if (loading && !evidence) {
    return (
      <div className="flex-1 min-h-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl">
        <Spin tip="读取证据包…" />
      </div>
    );
  }

  if (error && !evidence) {
    return (
      <div className="p-3" data-testid="combo-evidence-error">
        <Alert type="error" showIcon message={error} action={<a onClick={onRetry}>重试</a>} />
      </div>
    );
  }

  if (!evidence?.available) {
    return (
      <div className="flex-1 min-h-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl" data-testid="combo-evidence-empty">
        <Empty description="证据未生成">
          <div className="max-w-md text-center text-[11px] text-slate-400 leading-relaxed">
            尚未读取到 P1 研究产物（evidence.json）。页面不会现算任何命中率或收益指标；
            研究脚本产出后刷新即可看到候选、置信区间与 10 日闸门。
            {evidence?.path ? <div className="mt-1 font-mono break-all">{evidence.path}</div> : null}
          </div>
        </Empty>
      </div>
    );
  }

  const candidates = evidence.candidates ?? [];
  const recommendation = evidence.recommendation;
  const hasRecommendation = Boolean(recommendation?.ids?.length);

  const columns: ColumnsType<EvidenceCandidate> = [
    {
      title: '候选',
      dataIndex: 'name',
      width: 180,
      render: (name: string, row) => (
        <Tooltip title={row.id}>
          <div className="leading-tight">
            <div className="text-[11px] font-bold text-slate-800 truncate">{name || row.id}</div>
            <div className="text-[10px] text-slate-400">{row.members?.length ?? 0} 只成员 · {row.method}</div>
          </div>
        </Tooltip>
      ),
    },
    {
      title: '周期',
      dataIndex: 'horizon',
      width: 60,
      align: 'center',
      render: (v: number | null | undefined) => <span className="font-mono text-[11px]">{v == null ? '—' : `T+${v}`}</span>,
    },
    {
      title: '判定',
      dataIndex: 'verdict',
      width: 84,
      align: 'center',
      render: (v: string) => {
        const meta = VERDICT_META[v] ?? { label: v || '—', color: 'default' };
        return <Tag color={meta.color} className="!m-0 text-[10px]">{meta.label}</Tag>;
      },
    },
    {
      title: '检验窗命中(可执行)',
      key: 'hit_exec',
      width: 120,
      align: 'right',
      render: (_v, row) => (
        <span className="font-mono text-[11px] text-slate-700">{pct(row.windows?.test?.hit_exec_mean)}</span>
      ),
    },
    {
      title: 'Δ vs C0(检验)',
      key: 'delta_test',
      width: 100,
      align: 'right',
      render: (_v, row) => (
        <span className="font-mono text-[11px] font-bold text-slate-700">{deltaPct(row.criteria?.delta_test)}</span>
      ),
    },
    {
      title: '95% CI',
      key: 'ci',
      width: 150,
      align: 'right',
      render: (_v, row) => (
        <span className="font-mono text-[10px] text-slate-600">
          [{deltaPct(row.bootstrap?.ci_low)}, {deltaPct(row.bootstrap?.ci_high)}]
        </span>
      ),
    },
    {
      title: 'BH q',
      dataIndex: 'bh_q',
      width: 70,
      align: 'right',
      render: (v: number | null | undefined) => <span className="font-mono text-[11px]">{num(v, 3)}</span>,
    },
    {
      title: '月度胜率',
      key: 'monthly',
      width: 80,
      align: 'right',
      render: (_v, row) => <span className="font-mono text-[11px]">{pct(row.monthly?.win_ratio)}</span>,
    },
    {
      title: '闸门年化/回撤',
      key: 'gate',
      width: 128,
      align: 'right',
      render: (_v, row) => (
        <span className="font-mono text-[10px] text-slate-600">
          {pct(row.gate?.performance?.annual_return)} / {pct(row.gate?.performance?.max_drawdown)}
        </span>
      ),
    },
    {
      title: '留一最大Δ',
      key: 'loo',
      width: 88,
      align: 'right',
      render: (_v, row) => (
        <Tooltip title={row.leave_one_out?.member ? `最敏感成员：${row.leave_one_out.member}` : ''}>
          <span className="font-mono text-[11px]">{deltaPct(row.leave_one_out?.max_delta)}</span>
        </Tooltip>
      ),
    },
  ];

  return (
    <div className="flex-1 min-h-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden" data-testid="combo-evidence">
      <div className="shrink-0 p-3 space-y-2 border-b border-slate-100">
        <Alert
          type={hasRecommendation ? 'info' : 'warning'}
          showIcon
          message={
            hasRecommendation
              ? `达到证据标准：${recommendation!.ids.join('、')}`
              : '研究结论：暂无达到证据标准的组合'
          }
          description={
            recommendation?.reason ||
            '证据包未给出判定理由；在判定出来之前，页面不声称任何组合「命中率最高」。'
          }
        />
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] text-slate-400">研究判据：</span>
          {Object.entries(evidence.kill_criteria ?? {}).map(([name, item]) => {
            const passed = Boolean((item as { passed?: boolean }).passed);
            const enforced = Boolean((item as { enforced?: boolean }).enforced);
            return (
              <Tooltip key={name} title={JSON.stringify(item)}>
                <Tag color={passed ? 'blue' : 'gold'} className="!m-0 text-[9px]">
                  {name} {enforced ? '已执行' : passed ? '通过' : '未过'}
                </Tag>
              </Tooltip>
            );
          })}
          {evidence.generated_at ? (
            <span className="ml-auto font-mono text-[10px] text-slate-400">生成于 {evidence.generated_at}</span>
          ) : null}
        </div>
      </div>

      <div className="shrink-0 px-3 py-2">
        <Descriptions size="small" column={3} colon={false} items={[
          ...Object.entries(evidence.splits ?? {}).map(([key, value]) => ({
            key,
            label: WINDOW_LABEL[key] ?? key,
            children: <span className="font-mono text-[11px]">{splitLabel(value)}</span>,
          })),
          {
            key: 'candidates',
            label: '候选数',
            children: <span className="font-mono text-[11px]">{candidates.length}</span>,
          },
        ]} />
      </div>

      <div className="flex-1 min-h-0" data-testid="combo-evidence-table">
        <Table<EvidenceCandidate>
          rowKey="id"
          size="small"
          dataSource={candidates}
          columns={columns}
          pagination={false}
          scroll={{ x: 1100, y: 380 }}
          expandable={{ expandedRowRender: (row) => <WindowDetail candidate={row} /> }}
          locale={{ emptyText: '证据包没有候选' }}
        />
      </div>

      {evidence.caveats?.length ? (
        <div className="shrink-0 px-3 py-2 border-t border-slate-100 text-[10px] text-slate-400 space-y-0.5" data-testid="combo-evidence-caveats">
          {evidence.caveats.map((caveat) => (
            <div key={caveat}>· {caveat}</div>
          ))}
        </div>
      ) : null}
    </div>
  );
};

export default ComboEvidencePanel;
