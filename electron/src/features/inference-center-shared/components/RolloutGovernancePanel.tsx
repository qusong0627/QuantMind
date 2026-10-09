/**
 * 晋升流程工作区（P2 · 设计 §3.1/§5.3/§5.4）：挑战者从回放评估到晋升/回滚的
 * 全生命周期台账。
 *
 * - 名单 = 本市场 rollout 台账（replay_eval → observing → gate_passed →
 *   promoted / rejected / rolled_back），阶段徽标 + 观察进度 + 最近裁决；
 * - 行内动作按阶段收敛：**非 gate_passed 不出现「晋升」按钮**（后端也有 409
 *   兜底，前端不把不可用的入口摆出来）；「进观察期」在 G0/G1 未过时禁用并
 *   给出原因（硬闸门：三件套缺不许进流程 §4.5、复制品不许观察）；
 * - 详情抽屉 = 证据卡：G0-G7 逐闸状态与理由、准入三件套、回放 ΔIC 摘要、
 *   观察期进度与双侧 IC、审计链（谁/何时/理由/备任）；
 * - 一切决策（晋升/拒绝/回滚）走**理由必填的双确认弹窗**（OSS 单用户版口径：
 *   弹窗即确认，理由进审计）；评估是分钟级回放，按钮带进度态。
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Drawer, Input, Modal, Select, Table, Tooltip, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { clsx } from 'clsx';
import {
  CheckCircle2,
  FileSearch,
  GitBranch,
  Plus,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  XCircle,
} from 'lucide-react';
import type { UserModelRecord } from '../../../services/modelTrainingService';
import { modelDisplayName } from '../../../pages/modelRegistryUtils';
import {
  ACTIVE_ROLLOUT_STAGES,
  modelRolloutService,
  rolloutErrorMessage,
  type ModelRolloutRecord,
  type RolloutEvaluation,
  type RolloutGate,
  type RolloutStage,
} from '../../../services/modelRolloutService';

const STAGE_META: Record<RolloutStage, { label: string; cls: string }> = {
  replay_eval: { label: '回放评估', cls: 'text-slate-700 bg-slate-50 border-slate-200' },
  observing: { label: '观察期', cls: 'text-blue-700 bg-blue-50 border-blue-200' },
  gate_passed: { label: '待批准', cls: 'text-amber-700 bg-amber-50 border-amber-200' },
  promoted: { label: '已晋升', cls: 'text-emerald-700 bg-emerald-50 border-emerald-200' },
  rejected: { label: '已拒绝', cls: 'text-rose-700 bg-rose-50 border-rose-200' },
  rolled_back: { label: '已回滚', cls: 'text-purple-700 bg-purple-50 border-purple-200' },
};

const GATE_STATUS_META: Record<RolloutGate['status'], { label: string; cls: string }> = {
  pass: { label: '过', cls: 'text-emerald-700 bg-emerald-50 border-emerald-200' },
  fail: { label: '拒', cls: 'text-rose-700 bg-rose-50 border-rose-200' },
  skip: { label: '未评估', cls: 'text-slate-500 bg-slate-50 border-slate-200' },
  info: { label: '提示', cls: 'text-blue-600 bg-blue-50 border-blue-200' },
};

const VERDICT_META: Record<RolloutEvaluation['summary']['verdict'], { label: string; cls: string }> = {
  all_pass: { label: '全过', cls: 'text-emerald-700 bg-emerald-50 border-emerald-200' },
  flagged: { label: '有拒项', cls: 'text-rose-700 bg-rose-50 border-rose-200' },
  incomplete: { label: '证据缺', cls: 'text-amber-700 bg-amber-50 border-amber-200' },
};

/** G5 默认口径（后端 thresholds 缺省时兜底显示用） */
const DEFAULT_OBSERVATION_DAYS = 20;

const fmtTime = (v: string | null | undefined): string =>
  v ? String(v).replace('T', ' ').slice(0, 16) : '—';

interface RolloutGovernancePanelProps {
  market: string;
  marketLabel: string;
  /** 本市场已注册模型（换显示名 / 挑挑战者用） */
  models: UserModelRecord[];
  /** 回报「进行中」条数（壳用它挂工作区角标） */
  onActiveCountChange?: (count: number) => void;
}

type DecisionKind = 'promote' | 'reject' | 'rollback';

const DECISION_META: Record<DecisionKind, { title: string; warning: string; confirmCls: string }> = {
  promote: {
    title: '晋升挑战者为默认模型',
    warning:
      '晋升后：本市场默认模型切换为挑战者（仅本市场，其它市场不受影响），冠军降为备任并记录在台账。可随时回滚。',
    confirmCls: '!bg-emerald-600 hover:!bg-emerald-500 !border-emerald-600',
  },
  reject: {
    title: '拒绝本次晋升',
    warning: '拒绝后：rollout 进入终态（不可再评估/晋升），挑战者的自动推理设置行会被关闭。',
    confirmCls: '!bg-rose-600 hover:!bg-rose-500 !border-rose-600',
  },
  rollback: {
    title: '回滚晋升',
    warning: '回滚后：本市场默认模型切回晋升时记录的备任（前冠军），挑战者的自动推理设置行会被关闭。',
    confirmCls: '!bg-purple-600 hover:!bg-purple-500 !border-purple-600',
  },
};

/** 进观察期的硬闸门前置（与后端 from_stages 对齐；前端不摆不可用入口） */
function gateBlocker(record: ModelRolloutRecord): string | null {
  if (record.stage !== 'replay_eval') return null;
  if (!record.gate_result) return '尚未评估：先运行「评估」（G0/G1 是进观察期的硬闸门）';
  const byId = new Map((record.gate_result.gates ?? []).map((g) => [g.gate, g]));
  for (const id of ['G0', 'G1']) {
    const gate = byId.get(id);
    if (gate?.status !== 'pass') {
      return `${id} 未过：${(gate?.reasons ?? ['未评估']).join('；')}`;
    }
  }
  return null;
}

export const RolloutGovernancePanel: React.FC<RolloutGovernancePanelProps> = ({
  market,
  marketLabel,
  models,
  onActiveCountChange,
}) => {
  const [rows, setRows] = useState<ModelRolloutRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  // 抽屉开合与「当前详情」分开：开合由用户控制（关了就保持关，晚到的响应不许把它弹回来）
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [detail, setDetail] = useState<ModelRolloutRecord | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [decision, setDecision] = useState<{ kind: DecisionKind; record: ModelRolloutRecord } | null>(null);

  const nameOf = useCallback(
    (modelId: string, record?: ModelRolloutRecord | null): string => {
      const fromDetail =
        record?.champion?.model_id === modelId ? record.champion
        : record?.challenger?.model_id === modelId ? record.challenger
        : undefined;
      const hit = fromDetail ?? models.find((m) => m.model_id === modelId);
      return hit ? modelDisplayName(hit) : modelId;
    },
    [models],
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await modelRolloutService.listRollouts(market));
    } catch (err) {
      message.error(rolloutErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [market]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const openDetail = useCallback(async (rolloutId: string) => {
    setDrawerOpen(true);
    setDetailLoading(true);
    try {
      setDetail(await modelRolloutService.getRollout(rolloutId));
    } catch (err) {
      message.error(rolloutErrorMessage(err));
      setDrawerOpen(false); // 拉不到就别留个空抽屉装「加载中…」
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const closeDetail = useCallback(() => {
    setDrawerOpen(false);
    setDetail(null);
  }, []);

  /** 评估（分钟级回放）；observing 且窗满会自动转 gate_passed —— 文案如实说 */
  const runEvaluate = useCallback(
    async (record: ModelRolloutRecord) => {
      setBusyId(record.rollout_id);
      const key = 'rollout-evaluate';
      message.loading({ content: '回放评估中（vintage 回放 + 配对检验，分钟级）…', key, duration: 0 });
      try {
        const resp = await modelRolloutService.evaluateRollout(record.rollout_id);
        const verdict = resp.evaluation.summary.verdict;
        const verdictLabel =
          verdict === 'all_pass' ? '全过' : verdict === 'flagged' ? '有拒项（观察模式下由人工裁决）' : '证据不齐';
        message.success({ content: `评估完成：${verdictLabel}`, key });
        (resp.warnings ?? []).forEach((w) => message.warning(w));
        await refresh();
        if (detail?.rollout_id === record.rollout_id) await openDetail(record.rollout_id);
      } catch (err) {
        message.error({ content: rolloutErrorMessage(err), key });
      } finally {
        setBusyId(null);
      }
    },
    [refresh, detail, openDetail],
  );

  const startObserving = useCallback(
    async (record: ModelRolloutRecord) => {
      setBusyId(record.rollout_id);
      try {
        await modelRolloutService.startObservation(record.rollout_id);
        message.success('已进观察期：挑战者已加入日更推理，前向 IC 逐日积累（窗口满自动转为待批准）');
        await refresh();
        if (detail?.rollout_id === record.rollout_id) await openDetail(record.rollout_id);
      } catch (err) {
        message.error(rolloutErrorMessage(err));
      } finally {
        setBusyId(null);
      }
    },
    [refresh, detail, openDetail],
  );

  const submitDecision = useCallback(
    async (kind: DecisionKind, record: ModelRolloutRecord, notes: string) => {
      setBusyId(record.rollout_id);
      try {
        if (kind === 'promote') await modelRolloutService.promote(record.rollout_id, notes);
        else if (kind === 'reject') await modelRolloutService.reject(record.rollout_id, notes);
        else await modelRolloutService.rollback(record.rollout_id, notes);
        message.success(
          kind === 'promote' ? '已晋升：本市场默认模型已切换' : kind === 'reject' ? '已拒绝' : '已回滚：默认切回备任',
        );
        setDecision(null);
        closeDetail();
        await refresh();
      } catch (err) {
        message.error(rolloutErrorMessage(err));
      } finally {
        setBusyId(null);
      }
    },
    [refresh, closeDetail],
  );

  const observationProgress = useCallback((r: ModelRolloutRecord): { n: number; total: number } | null => {
    const obs = r.evidence?.observation;
    if (r.stage !== 'observing' && r.stage !== 'gate_passed') return null;
    const total = Number(r.gate_result?.thresholds?.g5_min_days ?? DEFAULT_OBSERVATION_DAYS);
    return { n: Number(obs?.n_days ?? 0), total };
  }, []);

  const columns: ColumnsType<ModelRolloutRecord> = useMemo(
    () => [
      {
        title: '挑战者',
        dataIndex: 'challenger_model_id',
        width: 250,
        ellipsis: true,
        render: (v: string, r) => (
          <Tooltip title={v}>
            <span className="text-xs font-bold text-slate-800 truncate">{nameOf(v, r)}</span>
          </Tooltip>
        ),
      },
      {
        title: '冠军（当时）',
        dataIndex: 'champion_model_id',
        width: 230,
        ellipsis: true,
        render: (v: string, r) => (
          <Tooltip title={v}>
            <span className="text-xs text-slate-600 truncate">{nameOf(v, r)}</span>
          </Tooltip>
        ),
      },
      {
        title: '阶段',
        dataIndex: 'stage',
        width: 96,
        render: (v: RolloutStage) => (
          <span className={clsx('inline-block px-1.5 py-0.5 rounded border text-[10px] font-black', STAGE_META[v]?.cls)}>
            {STAGE_META[v]?.label ?? v}
          </span>
        ),
      },
      {
        title: '观察进度',
        key: 'observation',
        width: 110,
        render: (_v, r) => {
          const prog = observationProgress(r);
          if (!prog) return <span className="text-[10px] text-slate-300">—</span>;
          const done = prog.n >= prog.total;
          return (
            <div className="flex items-center gap-1.5">
              <div className="flex-1 h-1.5 bg-slate-100 rounded-full overflow-hidden min-w-[36px]">
                <div
                  className={clsx('h-full rounded-full', done ? 'bg-emerald-500' : 'bg-blue-500')}
                  style={{ width: `${Math.min(100, (prog.n / prog.total) * 100)}%` }}
                />
              </div>
              <span className={clsx('font-mono text-[10px] font-bold', done ? 'text-emerald-700' : 'text-slate-600')}>
                {prog.n}/{prog.total}
              </span>
            </div>
          );
        },
      },
      {
        title: '最近裁决',
        key: 'verdict',
        width: 96,
        render: (_v, r) => {
          const verdict = r.gate_result?.summary?.verdict;
          if (!verdict) return <span className="text-[10px] text-slate-300">—</span>;
          const m = VERDICT_META[verdict];
          return (
            <span className={clsx('inline-block px-1.5 py-0.5 rounded border text-[10px] font-black', m.cls)}>
              {m.label}
            </span>
          );
        },
      },
      {
        title: '更新',
        dataIndex: 'updated_at',
        width: 128,
        sorter: (a, b) => String(a.updated_at ?? '').localeCompare(String(b.updated_at ?? '')),
        render: (v: string | null) => <span className="font-mono text-[11px] text-slate-500">{fmtTime(v)}</span>,
      },
      {
        title: '操作',
        key: 'actions',
        width: 250,
        fixed: 'right',
        render: (_v, r) => {
          const busy = busyId === r.rollout_id;
          const blocker = gateBlocker(r);
          return (
            <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
              {ACTIVE_ROLLOUT_STAGES.includes(r.stage) && (
                <Button size="small" loading={busy} onClick={() => void runEvaluate(r)}>
                  评估
                </Button>
              )}
              {r.stage === 'replay_eval' && (
                <Tooltip title={blocker ?? '把挑战者加入日更推理，开始积累前向 IC'}>
                  <Button
                    size="small"
                    type="primary"
                    ghost
                    disabled={blocker != null || busy}
                    onClick={() => void startObserving(r)}
                  >
                    进观察期
                  </Button>
                </Tooltip>
              )}
              {r.stage === 'gate_passed' && (
                <Button
                  size="small"
                  type="primary"
                  disabled={busy}
                  onClick={() => setDecision({ kind: 'promote', record: r })}
                >
                  晋升
                </Button>
              )}
              {ACTIVE_ROLLOUT_STAGES.includes(r.stage) && (
                <Button
                  size="small"
                  danger
                  ghost
                  disabled={busy}
                  onClick={() => setDecision({ kind: 'reject', record: r })}
                >
                  拒绝
                </Button>
              )}
              {r.stage === 'promoted' && (
                <Button
                  size="small"
                  disabled={busy}
                  onClick={() => setDecision({ kind: 'rollback', record: r })}
                >
                  回滚
                </Button>
              )}
              <Button size="small" type="text" onClick={() => void openDetail(r.rollout_id)}>
                详情
              </Button>
            </div>
          );
        },
      },
    ],
    [busyId, nameOf, observationProgress, openDetail, runEvaluate, startObserving],
  );

  // ── 新建：可晋升挑战者 = 本市场 ready/active 且非默认 ─────────────
  const [createChallenger, setCreateChallenger] = useState<string | null>(null);
  const [createCampaign, setCreateCampaign] = useState('');
  const [createNotes, setCreateNotes] = useState('');
  const [creating, setCreating] = useState(false);

  const challengerOptions = useMemo(
    () =>
      models
        .filter((m) => ['ready', 'active'].includes(String(m.status)) && !m.is_default)
        .map((m) => ({ value: m.model_id, label: modelDisplayName(m) })),
    [models],
  );

  const submitCreate = useCallback(async () => {
    if (!createChallenger) {
      message.warning('先选择一个挑战者模型');
      return;
    }
    setCreating(true);
    try {
      await modelRolloutService.createRollout({
        market,
        challenger_model_id: createChallenger,
        campaign_id: createCampaign.trim() || null,
        notes: createNotes.trim() || null,
      });
      message.success('已创建 rollout（回放评估阶段）');
      setCreateOpen(false);
      setCreateChallenger(null);
      setCreateCampaign('');
      setCreateNotes('');
      await refresh();
    } catch (err) {
      message.error(rolloutErrorMessage(err));
    } finally {
      setCreating(false);
    }
  }, [createCampaign, createChallenger, createNotes, market, refresh]);

  const activeCount = rows.filter((r) => ACTIVE_ROLLOUT_STAGES.includes(r.stage)).length;

  // 壳的工作区角标：每次台账刷新后回报一次（找不到比「面板自己报」更准的源）
  useEffect(() => {
    onActiveCountChange?.(activeCount);
  }, [activeCount, onActiveCountChange]);

  return (
    <div className="flex-1 min-w-0 min-h-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden">
      {/* ── 摘要 + 新建 ─────────────────────────────────────── */}
      <div className="shrink-0 px-3 py-2 border-b border-slate-200 bg-slate-50/60 flex items-center gap-3 flex-wrap">
        <span className="flex items-center gap-1.5 text-[11px] font-bold text-slate-700 whitespace-nowrap">
          <GitBranch size={13} className="text-blue-600" />
          {marketLabel}晋升台账
          <span className="font-mono font-black text-slate-900">{rows.length}</span>
        </span>
        <span className="w-px h-4 bg-slate-200" />
        <span className="text-[10px] text-slate-500 whitespace-nowrap">
          进行中 <strong className={clsx('font-mono', activeCount > 0 ? 'text-blue-700' : 'text-slate-400')}>{activeCount}</strong>
        </span>
        <span className="text-[10px] text-slate-500 whitespace-nowrap">
          已晋升 <strong className="font-mono text-emerald-700">{rows.filter((r) => r.stage === 'promoted').length}</strong>
        </span>
        <span className="text-[10px] text-slate-500 whitespace-nowrap">
          已回滚 <strong className="font-mono text-purple-700">{rows.filter((r) => r.stage === 'rolled_back').length}</strong>
        </span>
        <div className="ml-auto flex items-center gap-2">
          <Tooltip title="重算本市场台账（评估是分钟级回放，不在列表加载时跑）">
            <Button size="small" icon={<RefreshCw size={12} />} loading={loading} onClick={() => void refresh()}>
              刷新
            </Button>
          </Tooltip>
          <Button size="small" type="primary" icon={<Plus size={12} />} onClick={() => setCreateOpen(true)}>
            新建晋升
          </Button>
        </div>
      </div>

      <div className="flex-1 min-h-0">
        <Table<ModelRolloutRecord>
          rowKey="rollout_id"
          size="small"
          loading={loading}
          dataSource={rows}
          columns={columns}
          scroll={{ x: 1120, y: 'calc(100vh - 300px)' }}
          pagination={{ pageSize: 20, size: 'small' }}
          onRow={(r) => ({ onClick: () => void openDetail(r.rollout_id), className: 'cursor-pointer' })}
          locale={{
            emptyText: loading ? ' ' : (
              <div className="py-6 flex flex-col items-center gap-2 text-slate-400">
                <FileSearch size={22} className="opacity-40" />
                <span className="text-xs">还没有晋升记录——点「新建晋升」挑一个 ready 的挑战者开始</span>
              </div>
            ),
          }}
        />
      </div>

      {/* ── 详情抽屉：证据卡 + 审计链 ───────────────────────── */}
      <Drawer
        open={drawerOpen}
        onClose={closeDetail}
        loading={detailLoading}
        width={640}
        title={
          detail ? (
            <div className="flex items-center gap-2">
              <span className="text-sm font-black text-slate-800">晋升详情</span>
              <span className={clsx('px-1.5 py-0.5 rounded border text-[10px] font-black', STAGE_META[detail.stage]?.cls)}>
                {STAGE_META[detail.stage]?.label}
              </span>
              <span className="font-mono text-[10px] text-slate-400">{detail.rollout_id}</span>
            </div>
          ) : (
            '加载中…'
          )
        }
      >
        {detail && (
          <div className="flex flex-col gap-3">
            <div className="grid grid-cols-2 gap-2">
              <div className="border border-slate-200 rounded-lg p-2.5">
                <div className="text-[10px] font-bold text-slate-500 mb-1">挑战者（晋升对象）</div>
                <div className="text-xs font-bold text-slate-800 truncate" title={detail.challenger_model_id}>
                  {nameOf(detail.challenger_model_id, detail)}
                </div>
                <div className="font-mono text-[10px] text-slate-400 truncate">{detail.challenger_model_id}</div>
              </div>
              <div className="border border-slate-200 rounded-lg p-2.5">
                <div className="text-[10px] font-bold text-slate-500 mb-1">冠军（当时默认）</div>
                <div className="text-xs font-bold text-slate-800 truncate" title={detail.champion_model_id}>
                  {nameOf(detail.champion_model_id, detail)}
                </div>
                <div className="font-mono text-[10px] text-slate-400 truncate">{detail.champion_model_id}</div>
              </div>
            </div>

            <GateCard evaluation={detail.gate_result} />
            <EvidenceCard record={detail} />
            <AuditCard record={detail} nameOf={nameOf} />

            {/* 抽屉底动作（与行内一致，阶段前置同样收敛） */}
            <div className="flex items-center gap-2 pt-1 border-t border-slate-100">
              {ACTIVE_ROLLOUT_STAGES.includes(detail.stage) && (
                <Button loading={busyId === detail.rollout_id} onClick={() => void runEvaluate(detail)}>
                  重新评估
                </Button>
              )}
              {detail.stage === 'replay_eval' && (
                <Tooltip title={gateBlocker(detail) ?? '开始积累前向 IC'}>
                  <Button
                    type="primary"
                    ghost
                    disabled={gateBlocker(detail) != null || busyId === detail.rollout_id}
                    onClick={() => void startObserving(detail)}
                  >
                    进观察期
                  </Button>
                </Tooltip>
              )}
              {detail.stage === 'gate_passed' && (
                <Button
                  type="primary"
                  disabled={busyId === detail.rollout_id}
                  onClick={() => setDecision({ kind: 'promote', record: detail })}
                >
                  晋升
                </Button>
              )}
              {ACTIVE_ROLLOUT_STAGES.includes(detail.stage) && (
                <Button danger ghost onClick={() => setDecision({ kind: 'reject', record: detail })}>
                  拒绝
                </Button>
              )}
              {detail.stage === 'promoted' && (
                <Button onClick={() => setDecision({ kind: 'rollback', record: detail })}>回滚</Button>
              )}
            </div>
          </div>
        )}
      </Drawer>

      {/* ── 新建弹窗 ───────────────────────────────────────── */}
      <Modal
        title={`新建晋升（${marketLabel}）`}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => void submitCreate()}
        okText="创建"
        confirmLoading={creating}
        destroyOnHidden
      >
        <div className="flex flex-col gap-3 py-1">
          <div>
            <div className="text-xs font-bold text-slate-600 mb-1">挑战者模型（本市场 ready 且非默认）</div>
            <Select
              showSearch
              className="w-full"
              placeholder="选择要晋升的挑战者"
              value={createChallenger}
              onChange={setCreateChallenger}
              options={challengerOptions}
              filterOption={(input, option) =>
                String(option?.label ?? '').toLowerCase().includes(input.trim().toLowerCase()) ||
                String(option?.value ?? '').toLowerCase().includes(input.trim().toLowerCase())
              }
              notFoundContent="没有可晋升的候选（需 ready/active 且非当前默认）"
            />
          </div>
          <div>
            <div className="text-xs font-bold text-slate-600 mb-1">
              锚定 campaign（可选——填了则回放拼接该 campaign 各 vintage 的 OOS 段）
            </div>
            <Input
              placeholder="rc_cn_..."
              value={createCampaign}
              onChange={(e) => setCreateCampaign(e.target.value)}
            />
          </div>
          <div>
            <div className="text-xs font-bold text-slate-600 mb-1">备注（可选）</div>
            <Input.TextArea
              rows={2}
              value={createNotes}
              onChange={(e) => setCreateNotes(e.target.value)}
              placeholder="例：2026-10 月度重训产物"
            />
          </div>
        </div>
      </Modal>

      {/* ── 决策双确认弹窗（理由必填）───────────────────────── */}
      <DecisionModal
        decision={decision}
        busy={decision != null && busyId === decision.record.rollout_id}
        onCancel={() => setDecision(null)}
        onSubmit={(notes) => {
          if (decision) void submitDecision(decision.kind, decision.record, notes);
        }}
      />
    </div>
  );
};

/** G0-G7 决策卡：逐闸状态 + 理由；verdict 汇总徽标 */
const GateCard: React.FC<{ evaluation: RolloutEvaluation | null }> = ({ evaluation }) => {
  if (!evaluation) {
    return (
      <div className="border border-dashed border-slate-200 rounded-lg p-3 text-xs text-slate-400">
        尚未评估——点「评估」运行 vintage 回放（分钟级）后出 G0-G7 决策卡
      </div>
    );
  }
  const verdict = evaluation.summary.verdict;
  const vm = VERDICT_META[verdict];
  return (
    <div className="border border-slate-200 rounded-lg overflow-hidden">
      <div className="px-2.5 py-1.5 bg-slate-50 border-b border-slate-200 flex items-center gap-2">
        <span className="text-[11px] font-black text-slate-700">决策卡（G0-G7）</span>
        <span className={clsx('px-1.5 py-0.5 rounded border text-[10px] font-black', vm.cls)}>{vm.label}</span>
        <span className="ml-auto text-[10px] text-slate-400">
          拒 {evaluation.summary.counts?.fail ?? 0} · 未评估 {evaluation.summary.counts?.skip ?? 0}
        </span>
      </div>
      <div className="divide-y divide-slate-100">
        {(evaluation.gates ?? []).map((g) => {
          const m = GATE_STATUS_META[g.status];
          return (
            <div key={g.gate} className="px-2.5 py-1.5 flex items-start gap-2">
              <span className="font-mono text-[11px] font-black text-slate-700 w-7 shrink-0 pt-0.5">{g.gate}</span>
              <span className={clsx('px-1.5 py-0.5 rounded border text-[10px] font-black shrink-0', m.cls)}>
                {m.label}
              </span>
              <span className="text-[11px] text-slate-600 leading-5 min-w-0 break-all">
                {(g.reasons ?? []).join('；') || '—'}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
};

/** 证据卡：准入三件套 + 回放 ΔIC + 观察期进度 */
const EvidenceCard: React.FC<{ record: ModelRolloutRecord }> = ({ record }) => {
  const ev = record.evidence ?? {};
  const admission = ev.admission;
  const delta = ev.delta_summary;
  const obs = ev.observation;
  const total = Number(record.gate_result?.thresholds?.g5_min_days ?? DEFAULT_OBSERVATION_DAYS);
  const n = Number(obs?.n_days ?? 0);
  const repro = admission?.reproducibility;

  const softLabel =
    admission?.registration_soft_gate_passed === true ? '通过'
    : admission?.registration_soft_gate_passed === false ? '未过（人工激活）'
    : '未评估';

  return (
    <div className="border border-slate-200 rounded-lg overflow-hidden">
      <div className="px-2.5 py-1.5 bg-slate-50 border-b border-slate-200 text-[11px] font-black text-slate-700">
        证据
      </div>
      <div className="px-2.5 py-2 flex flex-col gap-2 text-[11px] text-slate-600">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-bold text-slate-700">准入</span>
          <span>软闸门：{softLabel}</span>
          <span>状态：{admission?.status ?? '—'}</span>
          <span className="font-mono">
            三件套：seed {repro?.seed == null ? '缺' : '✓'} · config {repro?.config_yaml ? '✓' : '缺'} · 指纹{' '}
            {repro?.data_fingerprint ? '✓' : '缺'}
          </span>
        </div>
        {delta && (
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-bold text-slate-700">回放 ΔIC</span>
            {delta.sufficient === false ? (
              <span className="text-amber-700">{delta.reason ?? '样本不足'}</span>
            ) : (
              <span className="font-mono">
                mean {delta.mean ?? '—'} · t {delta.t ?? '—'} · n {delta.n_days ?? '—'}
              </span>
            )}
          </div>
        )}
        {(record.stage === 'observing' || record.stage === 'gate_passed' || obs) && (
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <span className="font-bold text-slate-700">观察期</span>
              <span className="font-mono">
                {n}/{total} 交易日
              </span>
              {ev.observation_since && (
                <span className="text-slate-400">自 {String(ev.observation_since).slice(0, 10)} 起</span>
              )}
            </div>
            <div className="flex items-center gap-3 flex-wrap font-mono">
              <span>
                挑战者 IC {obs?.challenger_mean_ic ?? '—'} / 覆盖 {obs?.challenger_coverage ?? '—'}
              </span>
              <span className="text-slate-400">
                冠军 IC {obs?.champion_mean_ic ?? '—'} / 覆盖 {obs?.champion_coverage ?? '—'}
              </span>
            </div>
          </div>
        )}
        {ev.trials?.trial_count != null && (
          <div>
            <span className="font-bold text-slate-700">同配方试次</span>{' '}
            <span className="font-mono">{ev.trials.trial_count}</span>
            <span className="text-slate-400">（试次越多，选中「碰巧好」的概率越高——仅提示）</span>
          </div>
        )}
      </div>
    </div>
  );
};

/** 审计链：谁/何时/理由/备任/锚定 campaign */
const AuditCard: React.FC<{
  record: ModelRolloutRecord;
  nameOf: (id: string, record?: ModelRolloutRecord | null) => string;
}> = ({ record, nameOf }) => (
  <div className="border border-slate-200 rounded-lg overflow-hidden">
    <div className="px-2.5 py-1.5 bg-slate-50 border-b border-slate-200 text-[11px] font-black text-slate-700">
      审计链
    </div>
    <div className="px-2.5 py-2 grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-slate-600">
      <span>创建：<span className="font-mono">{fmtTime(record.created_at)}</span></span>
      <span>最近更新：<span className="font-mono">{fmtTime(record.updated_at)}</span></span>
      <span>裁决人：{record.decided_by ?? '—'}</span>
      <span>裁决时间：<span className="font-mono">{fmtTime(record.decided_at)}</span></span>
      <span className="col-span-2">
        备任（回滚目标）：{record.prior_default_model_id ? (
          <span title={record.prior_default_model_id}>{nameOf(record.prior_default_model_id, record)}</span>
        ) : '—'}
      </span>
      <span className="col-span-2">锚定 campaign：<span className="font-mono">{record.campaign_id ?? '—'}</span></span>
      <span className="col-span-2 whitespace-pre-wrap break-words">理由：{record.notes ?? '—'}</span>
    </div>
  </div>
);

/** 决策双确认（理由必填；空理由不提交——后端同样 400 拦截） */
const DecisionModal: React.FC<{
  decision: { kind: DecisionKind; record: ModelRolloutRecord } | null;
  busy: boolean;
  onCancel: () => void;
  onSubmit: (notes: string) => void;
}> = ({ decision, busy, onCancel, onSubmit }) => {
  const [notes, setNotes] = useState('');
  useEffect(() => {
    if (decision) setNotes('');
  }, [decision]);

  const meta = decision ? DECISION_META[decision.kind] : null;
  return (
    <Modal
      title={
        meta ? (
          <span className="flex items-center gap-2">
            {decision?.kind === 'promote' ? <CheckCircle2 size={16} className="text-emerald-600" /> :
             decision?.kind === 'reject' ? <XCircle size={16} className="text-rose-600" /> :
             <RotateCcw size={16} className="text-purple-600" />}
            {meta.title}
          </span>
        ) : ''
      }
      open={decision != null}
      onCancel={onCancel}
      okText="确认"
      cancelText="取消"
      confirmLoading={busy}
      okButtonProps={{
        disabled: notes.trim().length === 0,
        className: meta?.confirmCls,
      }}
      onOk={() => onSubmit(notes.trim())}
      destroyOnHidden
    >
      {meta && decision && (
        <div className="flex flex-col gap-3 py-1">
          <div className="flex items-start gap-2 text-xs text-slate-600 bg-slate-50 border border-slate-200 rounded-lg p-2.5">
            <ShieldAlert size={14} className="text-amber-500 mt-0.5 shrink-0" />
            <span>{meta.warning}</span>
          </div>
          <div className="text-[11px] text-slate-500 font-mono truncate" title={decision.record.challenger_model_id}>
            {decision.record.rollout_id} · {decision.record.challenger_model_id}
          </div>
          <div>
            <div className="text-xs font-bold text-slate-600 mb-1">
              理由（必填，进审计链）
            </div>
            <Input.TextArea
              rows={3}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder={
                decision.kind === 'promote' ? '例：观察期 20 日 IC 稳定高于冠军，回放配对显著'
                : decision.kind === 'reject' ? '例：回放 ΔIC 不显著，证据不足以晋升'
                : '例：晋升后前向 IC 转负，回退冠军'
              }
            />
          </div>
        </div>
      )}
    </Modal>
  );
};

export default RolloutGovernancePanel;
