import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert, Button, Input, InputNumber, Modal, Radio, Spin, Table, Tag, Tooltip, message,
} from 'antd';
import type { TableColumnsType } from 'antd';
import { AlertTriangle, CheckCircle2, Layers, TrendingUp } from 'lucide-react';
import {
  modelTrainingService,
  type FusionMemberRow,
  type FusionPreviewResponse,
  type UserModelRecord,
} from '../services/modelTrainingService';
import { modelDisplayName } from './modelRegistryUtils';
import {
  classifyVerdict,
  describeDiagnosticReason,
  describeFusionWarning,
  diagnosticFor,
  formatMetric,
  formatWeightPct,
} from './fusionUtils';

type WeightStrategy = 'icir_shrunk' | 'equal' | 'manual';

interface FusionModalProps {
  open: boolean;
  /** 已勾选的成员模型（≥2，同市场由入口保证、后端复验）。 */
  models: UserModelRecord[];
  onCancel: () => void;
  onCreated: (modelId: string) => void;
}

function errText(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (err instanceof Error && err.message) return err.message;
  return '未知错误';
}

export const FusionModal: React.FC<FusionModalProps> = ({ open, models, onCancel, onCreated }) => {
  const [strategy, setStrategy] = useState<WeightStrategy>('icir_shrunk');
  const [manualWeights, setManualWeights] = useState<Record<string, number>>({});
  const [displayName, setDisplayName] = useState('');
  const [preview, setPreview] = useState<FusionPreviewResponse | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState('');
  const [creating, setCreating] = useState(false);

  const sourceIds = models.map((m) => m.model_id);

  const runPreview = useCallback(
    async (ws: WeightStrategy, manual?: Record<string, number>) => {
      if (sourceIds.length < 2) return;
      setPreviewing(true);
      setPreviewError('');
      try {
        const resp = await modelTrainingService.previewEnsemble({
          source_model_ids: sourceIds,
          weight_strategy: ws,
          manual_weights: ws === 'manual' ? manual ?? manualWeights : undefined,
        });
        setPreview(resp);
      } catch (err) {
        setPreview(null);
        setPreviewError(errText(err));
      } finally {
        setPreviewing(false);
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    },
    [sourceIds.join(','), manualWeights],
  );

  useEffect(() => {
    if (!open) return;
    setStrategy('icir_shrunk');
    setPreview(null);
    setPreviewError('');
    setDisplayName('');
    setManualWeights(
      Object.fromEntries(models.map((m) => [m.model_id, Math.round(100 / Math.max(models.length, 1))])),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (open && strategy !== 'manual') void runPreview(strategy);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, strategy]);

  const handleCreate = async () => {
    setCreating(true);
    try {
      const created = await modelTrainingService.createEnsemble({
        source_model_ids: sourceIds,
        display_name: displayName.trim() || undefined,
        weight_strategy: strategy,
        manual_weights: strategy === 'manual' ? { ...manualWeights } : undefined,
        fusion_strategy: 'linear',
      });
      message.success(`融合模型已创建并加入日更推理：${created.model_id}`);
      onCreated(created.model_id);
    } catch (err) {
      message.error(`创建融合模型失败：${errText(err)}`);
    } finally {
      setCreating(false);
    }
  };

  const columns: TableColumnsType<FusionMemberRow> = [
    {
      title: '成员',
      dataIndex: 'model_id',
      render: (_v, row) => (
        <div className="min-w-0">
          <div className="font-bold text-[11px] text-slate-800 truncate">{row.display_name || row.model_id}</div>
          <Tooltip title={row.model_id}>
            <div className="text-[9px] text-slate-400 font-mono truncate">{row.model_id}</div>
          </Tooltip>
        </div>
      ),
    },
    { title: '市场', dataIndex: 'market', width: 58, render: (v: string) => <Tag className="m-0 text-[9px]">{v || 'CN'}</Tag> },
    { title: '周期', dataIndex: 'horizon_days', width: 52, render: (v: number) => <span className="text-[10px]">{v ? `${v}日` : '—'}</span> },
    { title: 'IC天数', dataIndex: 'ic_days', width: 62, render: (v: number) => <span className="text-[10px]">{v || 0}</span> },
    { title: 'IC均值', dataIndex: 'ic_mean', width: 68, render: (v: number | null) => <span className="text-[10px] font-mono">{formatMetric(v)}</span> },
    { title: 'ICIR', dataIndex: 'icir', width: 62, render: (v: number | null) => <span className="text-[10px] font-mono">{formatMetric(v)}</span> },
    { title: '回放ICIR', dataIndex: 'replay_icir', width: 72, render: (v: number | null) => <span className="text-[10px] font-mono text-slate-500">{formatMetric(v)}</span> },
    {
      title: '权重',
      dataIndex: 'weight',
      width: 64,
      render: (v: number | null, row) => {
        const diag = preview ? diagnosticFor(preview.diagnostics, row.model_id) : null;
        const reason = diag ? describeDiagnosticReason(diag.reason) : '';
        return (
          <div className="flex items-center gap-1">
            <span className="text-[10px] font-black font-mono text-blue-700">{formatWeightPct(v)}</span>
            {diag?.dropped && <Tag color="red" className="m-0 text-[8px] leading-3">剔除</Tag>}
            {!diag?.dropped && reason && <Tag color="orange" className="m-0 text-[8px] leading-3">{reason}</Tag>}
          </div>
        );
      },
    },
  ];

  const verdict = classifyVerdict(preview?.replay?.verdict);
  const warningTexts = Array.from(
    new Set((preview?.warnings ?? []).map(describeFusionWarning).filter(Boolean)),
  );

  return (
    <Modal
      open={open}
      onCancel={onCancel}
      width={920}
      destroyOnClose
      footer={null}
      title={
        <span className="flex items-center gap-2 font-bold">
          <Layers size={15} className="text-purple-600" />
          模型融合（机构级）
          <Tag color="purple" className="ml-1 font-mono">{models.length} 个成员</Tag>
        </span>
      }
    >
      <div className="max-h-[72vh] overflow-y-auto custom-scrollbar pr-1 space-y-4 pt-1">
        {/* 权重策略 */}
        <div className="flex items-center gap-3 flex-wrap">
          <span className="text-[10px] font-black text-slate-500 tracking-wider">权重策略</span>
          <Radio.Group
            size="small"
            value={strategy}
            onChange={(e) => setStrategy(e.target.value as WeightStrategy)}
          >
            <Radio.Button value="icir_shrunk">机构级 ICIR 收缩</Radio.Button>
            <Radio.Button value="equal">等权</Radio.Button>
            <Radio.Button value="manual">手工</Radio.Button>
          </Radio.Group>
          {strategy === 'manual' && (
            <Button size="small" className="rounded-lg" onClick={() => void runPreview('manual')} loading={previewing}>
              按手工权重预览
            </Button>
          )}
          <span className="text-[9px] text-slate-400">
            滚动 ICIR × 多样性惩罚 × 样本量收缩；仅用已兑现收益，无前视
          </span>
        </div>

        {strategy === 'manual' && (
          <div className="grid grid-cols-2 gap-2">
            {models.map((m) => (
              <div key={m.model_id} className="flex items-center gap-2 bg-slate-50 rounded-xl px-3 py-1.5">
                <span className="text-[10px] font-bold text-slate-600 truncate flex-1">{modelDisplayName(m)}</span>
                <InputNumber
                  size="small"
                  min={0}
                  max={100}
                  className="w-20"
                  value={manualWeights[m.model_id] ?? 0}
                  onChange={(v) =>
                    setManualWeights((prev) => ({ ...prev, [m.model_id]: Number(v ?? 0) }))
                  }
                />
                <span className="text-[9px] text-slate-400">份</span>
              </div>
            ))}
          </div>
        )}

        {/* 警示面 */}
        {previewError && (
          <Alert type="error" showIcon message="预览失败" description={previewError} className="rounded-xl" />
        )}
        {warningTexts.length > 0 && (
          <Alert
            type="warning"
            showIcon
            icon={<AlertTriangle size={14} />}
            message={<span className="text-[10px] font-black">样本与口径警示</span>}
            description={
              <ul className="pl-4 m-0 list-disc text-[10px] text-slate-600 space-y-0.5">
                {warningTexts.map((w) => <li key={w}>{w}</li>)}
              </ul>
            }
            className="rounded-xl"
          />
        )}
        {(preview?.corr_warnings?.length ?? 0) > 0 && (
          <Alert
            type="warning"
            showIcon
            message={<span className="text-[10px] font-black">成员相关性偏高</span>}
            description={
              <ul className="pl-4 m-0 list-disc text-[10px] text-slate-600 space-y-0.5">
                {preview!.corr_warnings.map((c) => (
                  <li key={`${c.a}-${c.b}`}>
                    {c.a} ↔ {c.b}：截面相关 {c.corr.toFixed(2)}（&gt;0.95）——权重已受多样性惩罚，极高者将被近似去重
                  </li>
                ))}
              </ul>
            }
            className="rounded-xl"
          />
        )}

        {/* 成员证据与权重 */}
        <div className="rounded-2xl border border-slate-100 overflow-hidden">
          <div className="px-3 py-2 bg-slate-50/70 flex items-center justify-between">
            <span className="text-[10px] font-black text-slate-600 tracking-wider">成员证据与权重预览</span>
            {previewing && <Spin size="small" />}
          </div>
          <Table<FusionMemberRow>
            size="small"
            rowKey="model_id"
            columns={columns}
            dataSource={preview?.members ?? []}
            pagination={false}
            loading={previewing && !preview}
          />
        </div>

        {/* OOS 回放结论 */}
        <div className="rounded-2xl border border-slate-100 p-3">
          <div className="flex items-center gap-2 mb-1.5">
            <TrendingUp size={12} className="text-blue-500" />
            <span className="text-[10px] font-black text-slate-600 tracking-wider">OOS 回放（融合 vs 成员）</span>
            {preview?.replay && (
              <span className="text-[9px] text-slate-400">回放 {preview.replay.dates} 个交易日</span>
            )}
          </div>
          {!preview?.replay ? (
            <div className="text-[10px] text-slate-400">OOS 证据不足（成员分数桶/已兑现收益尚未积累），融合结论待样本积累。</div>
          ) : verdict === 'beats_best' ? (
            <div className="flex items-center gap-2 text-[11px] font-bold text-emerald-600">
              <CheckCircle2 size={14} />
              优于全部成员：融合 ICIR {formatMetric(preview.replay.verdict?.fused_icir)} ＞ 最优成员 {formatMetric(preview.replay.verdict?.best_member_icir)}
            </div>
          ) : verdict === 'beats_median' ? (
            <div className="flex items-center gap-2 text-[11px] font-bold text-amber-600">
              <AlertTriangle size={14} />
              优于成员中位（{formatMetric(preview.replay.verdict?.fused_icir)} vs {formatMetric(preview.replay.verdict?.median_member_icir)}），未超最优成员 {formatMetric(preview.replay.verdict?.best_member_icir)}
            </div>
          ) : verdict === 'below_median' ? (
            <div className="flex items-center gap-2 text-[11px] font-bold text-red-600">
              <AlertTriangle size={14} />
              低于成员中位（{formatMetric(preview.replay.verdict?.fused_icir)} vs {formatMetric(preview.replay.verdict?.median_member_icir)}）——该组合暂无融合增益，建议更换成员或降级为单模型
            </div>
          ) : (
            <div className="text-[10px] text-slate-400">回放样本不足，无法裁决。</div>
          )}
        </div>

        {/* 命名与创建 */}
        <div className="flex items-center gap-3">
          <Input
            size="middle"
            placeholder="展示名（留空自动命名并追加市场后缀）"
            value={displayName}
            maxLength={80}
            onChange={(e) => setDisplayName(e.target.value)}
            className="rounded-xl flex-1"
          />
          <Button
            type="primary"
            className="rounded-xl bg-purple-600 hover:bg-purple-500 border-none font-bold px-6"
            loading={creating}
            disabled={!preview || previewing}
            onClick={() => void handleCreate()}
          >
            创建融合模型
          </Button>
        </div>
        <div className="text-[9px] text-slate-400 -mt-2">
          创建即写入日更推理名单（可随时在「自动推理」中关闭），权重快照与回放结论随模型落盘，全程可审计。
        </div>
      </div>
    </Modal>
  );
};
