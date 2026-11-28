/**
 * 新建 / 保存组合（三步：选成员 → 参数 → 预览保存）。
 *
 * 「同特征」是硬约束：后端只接受同一签名组（factor_schema_hash + data_source +
 * feature_count + horizon）的模型；这里在勾选层就阻止跨组，并在保存前再校验一次。
 * 预览走 `/preview`（不落库），保存走 `POST /models/blends`。
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Input,
  InputNumber,
  Modal,
  Select,
  Steps,
  Table,
  Tag,
  Tooltip,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { blendService, blendErrorMessage } from '../../../../services/blendService';
import type {
  BlendCreateRequest,
  BlendMethod,
  BlendRankingResponse,
  BlendRankingRow,
  BlendRecord,
  ComboEligibleGroup,
  ComboTemplate,
} from '../../../../services/blendTypes';
import { formatResearchScore } from '../../../shared/researchScore';
import { positionTagClass } from '../../positionLabels';
import { dispersionLabel, positionLabel, templateRollingDefaults } from './comboRankingModel';

export interface ComboBuilderModalProps {
  open: boolean;
  market: string;
  marketLabel: string;
  /** 治理面板「组合排名」带入的模型（可跨组，需用户自行收敛） */
  initialModelIds?: string[];
  /** 从模板保存为组合：预填成员与参数 */
  initialTemplate?: ComboTemplate | null;
  onClose: () => void;
  onCreated: (blend: BlendRecord) => void;
}

interface BuilderParams {
  name: string;
  method: BlendMethod;
  trim_count: number;
  min_coverage: number;
  consensus_min_count: number;
  entry_per_day: number;
  rolling_window: number;
  target_positions: number;
}

const DEFAULT_PARAMS: BuilderParams = {
  name: '',
  method: 'mean',
  trim_count: 2,
  min_coverage: 2,
  consensus_min_count: 0,
  entry_per_day: 5,
  rolling_window: 10,
  target_positions: 50,
};

function signatureLabel(sig: Record<string, unknown> | null | undefined): string {
  const horizon = sig?.target_horizon_days;
  const features = sig?.feature_count;
  const source = sig?.data_source;
  return [
    typeof horizon === 'number' ? `T+${horizon}` : 'T+?',
    typeof features === 'number' ? `${features} 维` : '? 维',
    source ? String(source) : '未知源',
  ].join(' · ');
}

export const ComboBuilderModal: React.FC<ComboBuilderModalProps> = ({
  open,
  market,
  marketLabel,
  initialModelIds,
  initialTemplate,
  onClose,
  onCreated,
}) => {
  const [step, setStep] = useState(0);
  const [groups, setGroups] = useState<ComboEligibleGroup[]>([]);
  const [excluded, setExcluded] = useState<{ model_id: string; reason: string }[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [params, setParams] = useState<BuilderParams>(DEFAULT_PARAMS);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<BlendRankingResponse | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [saving, setSaving] = useState(false);
  // 读取失败后的手动重试序号：只用于重跑资格请求，不动已填参数
  const [reloadNonce, setReloadNonce] = useState(0);
  // 用户手改过滚动参数后，不再随所选组周期自动覆盖
  const rollingTouched = useRef(false);
  // 打开瞬间捕获预填参数：拉取过程中父级重渲染不应改变本次预填
  const initialRef = useRef({ template: initialTemplate, modelIds: initialModelIds });
  initialRef.current = { template: initialTemplate, modelIds: initialModelIds };

  const reset = useCallback(() => {
    setStep(0);
    setGroups([]);
    setExcluded([]);
    setSelected([]);
    setParams(DEFAULT_PARAMS);
    rollingTouched.current = false;
    setError(null);
    setPreview(null);
  }, []);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    reset();
    setLoading(true);
    blendService
      .getEligible(market)
      .then((resp) => {
        if (!alive) return;
        const nextGroups = resp.groups ?? [];
        setGroups(nextGroups);
        setExcluded(resp.excluded ?? []);

        // 预填：模板优先，其次是治理面板带入的模型；只在拿到**本次**分组后做一次
        const { template, modelIds } = initialRef.current;
        const allIds = new Set(nextGroups.flatMap((g) => g.models.map((m) => m.model_id)));
        if (template) {
          const rolling = templateRollingDefaults(template.horizon_days);
          setSelected(template.members.map((m) => m.model_id).filter((id) => allIds.has(id)));
          setParams({
            name: template.name,
            method: template.method,
            trim_count: template.trim_count,
            min_coverage: template.min_coverage,
            consensus_min_count: template.consensus_min_count,
            entry_per_day: template.entry_per_day ?? rolling.entry_per_day,
            rolling_window: template.rolling_window ?? rolling.rolling_window,
            target_positions: template.target_positions ?? rolling.target_positions,
          });
        } else if (modelIds?.length) {
          setSelected(modelIds.filter((id) => allIds.has(id)));
        }
      })
      .catch((err) => alive && setError(blendErrorMessage(err)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [open, market, reset, reloadNonce]);

  const groupOf = useCallback(
    (modelId: string) => groups.find((g) => g.models.some((m) => m.model_id === modelId)),
    [groups],
  );
  const selectedGroupKeys = useMemo(
    () => Array.from(new Set(selected.map((id) => groupOf(id)?.key).filter(Boolean))),
    [selected, groupOf],
  );
  const singleGroup = selectedGroupKeys.length === 1 ? selectedGroupKeys[0] : null;
  const selectedSignature = useMemo(() => {
    if (!singleGroup) return null;
    return groupOf(selected[0])?.signature ?? null;
  }, [singleGroup, selected, groupOf]);
  const selectedHorizon = useMemo(() => {
    const raw = selectedSignature?.target_horizon_days;
    return typeof raw === 'number' && Number.isFinite(raw) && raw > 0 ? raw : null;
  }, [selectedSignature]);

  // 手工勾选成员时：滚动默认值随该组周期派生（模板预填优先，手改后不再覆盖）
  useEffect(() => {
    if (initialRef.current.template || rollingTouched.current || selectedHorizon === null) return;
    const rolling = templateRollingDefaults(selectedHorizon);
    setParams((prev) => {
      if (
        prev.entry_per_day === rolling.entry_per_day &&
        prev.rolling_window === rolling.rolling_window &&
        prev.target_positions === rolling.target_positions
      ) {
        return prev;
      }
      return { ...prev, ...rolling };
    });
  }, [selectedHorizon]);

  const toggle = (groupId: string, modelId: string, checked: boolean) => {
    setSelected((prev) => {
      if (!checked) return prev.filter((id) => id !== modelId);
      // 新组：清掉跨组选择，保证签名一致
      const group = groups.find((g) => g.key === groupId);
      const sameGroup = group?.models.some((m) => m.model_id === prev[0]);
      const base = prev.length > 0 && !sameGroup ? [] : prev;
      return Array.from(new Set([...base, modelId]));
    });
  };

  const canAdvance = selected.length >= 2 && singleGroup !== null;
  const canPreview = canAdvance && params.name.trim().length > 0;

  const runPreview = useCallback(async () => {
    setStep(2);
    setPreviewing(true);
    setError(null);
    try {
      const resp = await blendService.preview({
        market,
        model_ids: selected,
        purpose: 'rebalance',
        method: params.method,
        trim_count: params.trim_count,
        min_coverage: params.min_coverage,
        consensus_min_count: params.consensus_min_count,
        top: 50,
      });
      setPreview(resp);
    } catch (err) {
      setError(blendErrorMessage(err));
    } finally {
      setPreviewing(false);
    }
  }, [market, selected, params]);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const sameAsTemplate =
        Boolean(initialTemplate) &&
        initialTemplate!.members.length === selected.length &&
        initialTemplate!.members.every((m) => selected.includes(m.model_id)) &&
        initialTemplate!.method === params.method;
      const body: BlendCreateRequest = {
        name: params.name.trim(),
        market,
        model_ids: selected,
        purpose: 'rebalance',
        method: params.method,
        trim_count: params.trim_count,
        min_coverage: params.min_coverage,
        consensus_min_count: params.consensus_min_count,
        entry_per_day: params.entry_per_day,
        rolling_window: params.rolling_window,
        target_positions: params.target_positions,
        weights: null,
        source: sameAsTemplate ? 'system' : 'manual',
        template_id: sameAsTemplate ? initialTemplate!.template_id : null,
      };
      const created = await blendService.createBlend(body);
      message.success(`组合已保存：${created.name}`);
      onCreated(created);
      onClose();
    } catch (err) {
      setError(blendErrorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const previewColumns: ColumnsType<BlendRankingRow> = [
    { title: '排名', dataIndex: 'rank', width: 60, align: 'right' },
    { title: '代码', dataIndex: 'code', width: 105, render: (v: string) => <span className="font-mono text-[11px]">{v}</span> },
    { title: '名称', dataIndex: 'name', width: 110, ellipsis: true },
    {
      title: '综合分',
      dataIndex: 'research_score',
      width: 80,
      align: 'right',
      render: (v: number | null) => <span className="font-mono text-[11px] font-bold">{formatResearchScore(v)}</span>,
    },
    {
      title: '位置',
      dataIndex: 'position',
      width: 70,
      align: 'center',
      render: (_v, row) => {
        const label = positionLabel(row);
        return <span className={`rounded border px-1 py-0.5 text-[10px] font-bold ${positionTagClass(label)}`}>{label}</span>;
      },
    },
    {
      title: '覆盖',
      dataIndex: 'coverage',
      width: 60,
      align: 'center',
      render: (v: number) => <span className="font-mono text-[11px]">{v}/{selected.length}</span>,
    },
    {
      title: '分歧',
      dataIndex: 'dispersion_band',
      width: 60,
      align: 'center',
      render: (v: string) => <span className="text-[11px]">{dispersionLabel(v)}</span>,
    },
  ];

  const footer = (
    <div className="flex items-center justify-between gap-2">
      <span className="text-[11px] text-slate-400">
        {selected.length} 只成员 · {selectedSignature ? signatureLabel(selectedSignature) : '未选择'}
      </span>
      <span className="flex items-center gap-2">
        <Button onClick={onClose} disabled={saving}>取消</Button>
        {step > 0 && <Button onClick={() => setStep(step - 1)} disabled={saving || previewing}>上一步</Button>}
        {step === 0 && (
          <Button type="primary" disabled={!canAdvance || loading} onClick={() => setStep(1)}>
            下一步
          </Button>
        )}
        {step === 1 && (
          <Button type="primary" disabled={!canPreview || loading} onClick={() => void runPreview()}>
            预览榜单
          </Button>
        )}
        {step === 2 && (
          <Button type="primary" loading={saving} disabled={previewing || !preview} onClick={() => void save()}>
            保存组合
          </Button>
        )}
      </span>
    </div>
  );

  return (
    <Modal
      open={open}
      title={<span className="text-sm font-black text-slate-800">新建组合 · {marketLabel}</span>}
      width={960}
      footer={footer}
      onCancel={onClose}
      maskClosable={false}
      destroyOnHidden
    >
      <div className="space-y-3" data-testid="combo-builder">
        <Steps
          size="small"
          current={step}
          items={[{ title: '选成员' }, { title: '参数' }, { title: '预览保存' }]}
        />
        {error && (
          <Alert
            type="error"
            showIcon
            message="可参与模型读取失败"
            description={error}
            action={
              <Button size="small" onClick={() => setReloadNonce((n) => n + 1)}>
                重试
              </Button>
            }
          />
        )}
        {selectedGroupKeys.length > 1 && (
          <Alert
            type="warning"
            showIcon
            message="所选模型不属于同一特征签名组，请只保留同一组内的模型"
            description="组合要求成员使用同一因子集、同一数据源与同一预测周期。切换组别会自动清空上一组选择。"
          />
        )}

        {step === 0 && (
          <div className="max-h-[420px] overflow-auto space-y-2" data-testid="combo-builder-members">
            {loading && <div className="text-xs text-slate-500">读取可参与模型…</div>}
            {!loading && !error && groups.length === 0 && (
              <Alert type="info" showIcon message="没有可参与组合的模型" description="模型需就绪、非融合模型，且具备完整特征签名。" />
            )}
            {groups.map((group) => {
              const disabledGroup = singleGroup !== null && group.key !== singleGroup;
              return (
                <div key={group.key} className="rounded-lg border border-slate-200 p-2">
                  <div className="flex items-center justify-between px-1 pb-1">
                    <span className="text-[11px] font-bold text-slate-700">{signatureLabel(group.signature)}</span>
                    <span className="text-[10px] text-slate-400">{group.models.length} 只</span>
                  </div>
                  <div className="grid grid-cols-2 gap-x-3 gap-y-1">
                    {group.models.map((model) => {
                      const checked = selected.includes(model.model_id);
                      return (
                        <Tooltip key={model.model_id} title={disabledGroup ? '与其他已选模型不同组；勾选会切换组别' : model.model_id}>
                          <div className="flex items-start gap-1.5">
                            <Checkbox
                              checked={checked}
                              onChange={(e) => toggle(group.key, model.model_id, e.target.checked)}
                            />
                            <div className="min-w-0 leading-tight">
                              <div className="text-[11px] text-slate-800 truncate">{model.display_name || model.model_id}</div>
                              <div className="text-[10px] text-slate-400">
                                {model.model_type} · {model.horizon_days == null ? '—' : `T+${model.horizon_days}`}
                              </div>
                            </div>
                          </div>
                        </Tooltip>
                      );
                    })}
                  </div>
                </div>
              );
            })}
            {excluded.length > 0 && (
              <div className="flex items-center gap-1 flex-wrap text-[10px] text-slate-400">
                <span>不可参与：</span>
                {excluded.slice(0, 10).map((row) => (
                  <Tag key={row.model_id} className="text-[9px]">{row.model_id} · {row.reason}</Tag>
                ))}
              </div>
            )}
          </div>
        )}

        {step === 1 && (
          <div className="grid grid-cols-2 gap-3 max-w-[720px]" data-testid="combo-builder-params">
            <label className="col-span-2 space-y-1 text-[11px] text-slate-600">
              <span className="font-bold">组合名称</span>
              <Input
                value={params.name}
                maxLength={64}
                placeholder="如：13 模型 T+10 等权"
                onChange={(e) => setParams({ ...params, name: e.target.value })}
              />
            </label>
            <label className="space-y-1 text-[11px] text-slate-600">
              <span className="font-bold">合成方法</span>
              <Select
                className="w-full"
                value={params.method}
                onChange={(method: BlendMethod) => setParams({ ...params, method })}
                options={[
                  { value: 'mean', label: '等权均值' },
                  { value: 'median', label: '中位数' },
                  { value: 'trimmed', label: '截尾均值' },
                ]}
              />
            </label>
            <label className="space-y-1 text-[11px] text-slate-600">
              <span className="font-bold">截尾数量（仅截尾均值）</span>
              <InputNumber
                className="w-full"
                min={0}
                max={10}
                disabled={params.method !== 'trimmed'}
                value={params.trim_count}
                onChange={(v) => setParams({ ...params, trim_count: Number(v ?? 0) })}
              />
            </label>
            <label className="space-y-1 text-[11px] text-slate-600">
              <span className="font-bold">最小覆盖成员数</span>
              <InputNumber className="w-full" min={1} max={selected.length} value={params.min_coverage}
                onChange={(v) => setParams({ ...params, min_coverage: Number(v ?? 2) })} />
            </label>
            <label className="space-y-1 text-[11px] text-slate-600">
              <span className="font-bold">共识最小成员数</span>
              <InputNumber className="w-full" min={0} max={selected.length} value={params.consensus_min_count}
                onChange={(v) => setParams({ ...params, consensus_min_count: Number(v ?? 0) })} />
            </label>
            <label className="space-y-1 text-[11px] text-slate-600">
              <span className="font-bold">每日新进上限</span>
              <InputNumber className="w-full" min={1} max={50} value={params.entry_per_day}
                onChange={(v) => {
                  rollingTouched.current = true;
                  setParams({ ...params, entry_per_day: Number(v ?? 5) });
                }} />
            </label>
            <label className="space-y-1 text-[11px] text-slate-600">
              <span className="font-bold">滚动窗口（交易日）</span>
              <InputNumber className="w-full" min={1} max={60} value={params.rolling_window}
                onChange={(v) => {
                  rollingTouched.current = true;
                  setParams({ ...params, rolling_window: Number(v ?? 10) });
                }} />
            </label>
            <label className="space-y-1 text-[11px] text-slate-600">
              <span className="font-bold">目标持仓数</span>
              <InputNumber className="w-full" min={1} max={500} value={params.target_positions}
                onChange={(v) => {
                  rollingTouched.current = true;
                  setParams({ ...params, target_positions: Number(v ?? 50) });
                }} />
            </label>
            <div className="col-span-2 text-[10px] text-slate-400">
              默认随预测周期派生：{selectedHorizon === null ? 'T+10' : `T+${selectedHorizon}`} → 每日 5 只 × {params.rolling_window} 个交易日
              ≈ {params.entry_per_day * params.rolling_window} 只（目标持仓 {params.target_positions}）。
              参数只影响滚动展示与纸面计划，不改变榜单排序口径。
            </div>
          </div>
        )}

        {step === 2 && (
          <div className="space-y-2" data-testid="combo-builder-preview">
            {preview?.data_trade_date && (
              <div className="text-[11px] text-slate-500">
                数据日 <span className="font-mono text-slate-800">{preview.data_trade_date}</span> · 展示前 50 名
                {preview.warnings?.length ? ` · ${preview.warnings.slice(0, 2).join('；')}` : ''}
              </div>
            )}
            <Table<BlendRankingRow>
              rowKey={(row) => row.symbol || row.code}
              size="small"
              loading={previewing}
              dataSource={preview?.ranking?.slice(0, 50) ?? []}
              columns={previewColumns}
              pagination={false}
              scroll={{ y: 320 }}
              locale={{ emptyText: previewing ? ' ' : '没有可预览的标的' }}
            />
          </div>
        )}
      </div>
    </Modal>
  );
};

export default ComboBuilderModal;
