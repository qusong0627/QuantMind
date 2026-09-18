/** 交易台「实时推理」卡（T-P6-08 收口）：状态/开关/模型切换/节拍/ONNX 状态与重建（admin）。
 *
 * 数据源：/api/v1/admin/realtime/infer/{config,models,export-onnx}（配置 + 引擎状态镜像）。
 * 非管理员：诚实降级为「需管理员权限」；镜像超 5 分钟未更新 → 提示引擎循环可能未运行。
 */

import React, { useCallback, useEffect, useState } from 'react';
import { Activity, Loader2, RefreshCw } from 'lucide-react';
import { CARD, CardHeader, StatTile } from './cardKit';
import { gateHint, inferViewState, type InferConfigView, type InferStatusView, type ModelOnnxStatus } from './realtimeInferModel';
import {
  exportOnnx,
  getInferConfig,
  listInferModels,
  setInferConfig,
  type InferModelOption,
} from '../services/realtimeInferService';

export const RealtimeInferenceCard: React.FC = () => {
  const [config, setConfig] = useState<InferConfigView | null>(null);
  const [status, setStatus] = useState<InferStatusView | null>(null);
  const [onnx, setOnnx] = useState<ModelOnnxStatus | null>(null);
  const [models, setModels] = useState<InferModelOption[]>([]);
  const [needAdmin, setNeedAdmin] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [note, setNote] = useState('');
  const [gateDraft, setGateDraft] = useState('');
  const [cadenceDraft, setCadenceDraft] = useState('');
  const [modelDraft, setModelDraft] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await getInferConfig();
      setConfig(data.config);
      setStatus(data.status);
      setOnnx(data.model_onnx ?? null);
      setNeedAdmin(false);
      setGateDraft(String(data.config?.min_live_coverage ?? ''));
      setCadenceDraft(String(data.config?.cadence_s ?? ''));
      setModelDraft(String(data.config?.model_dir ?? ''));
    } catch (e) {
      if ((e as { status?: number }).status === 403) {
        setNeedAdmin(true);
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 30000);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    // 候选模型列表只取一次（服务端 5 分钟缓存量级的轻扫描；随 load 轮询会浪费）
    void listInferModels()
      .then(setModels)
      .catch(() => setModels([]));
  }, []);

  const view = inferViewState(config, status, Date.now(), needAdmin, onnx);

  const runOp = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError('');
    setNote('');
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onToggle = () => runOp(async () => {
    await setInferConfig({ enabled: !view.enabled });
    await load();
  });

  const onSaveGate = () => {
    const value = Number(gateDraft);
    if (!Number.isFinite(value) || value < 0 || value > 1) {
      setError('覆盖率闸门需为 0~1 之间的小数');
      return;
    }
    void runOp(async () => {
      await setInferConfig({ min_live_coverage: value });
      await load();
    });
  };

  const onSaveCadence = () => {
    const value = Number(cadenceDraft);
    if (!Number.isFinite(value) || value < 3 || value > 600) {
      setError('节拍需为 3~600 秒');
      return;
    }
    void runOp(async () => {
      await setInferConfig({ cadence_s: value });
      setNote('节拍已保存（热生效）');
      await load();
    });
  };

  const onSaveModel = () => {
    const value = modelDraft.trim();
    if (!value) {
      setError('请选择模型');
      return;
    }
    if (value === view.modelDir) {
      setNote('模型未变化');
      return;
    }
    void runOp(async () => {
      await setInferConfig({ model_dir: value });
      setNote('模型已切换：覆盖白名单已清空（防旧列残留）；ONNX 缺失时首次运行自动导出');
      await load();
    });
  };

  const onExportOnnx = () => runOp(async () => {
    const result = await exportOnnx(view.modelDir || undefined);
    const ok = result.report?.ok !== false;
    setNote(
      ok
        ? 'ONNX 导出成功（含原模型对照校验）；已加载会话仍用旧产物，停/启用或重启后加载新产物'
        : `ONNX 导出失败：${result.report?.reason || '未知原因'}`,
    );
    await load();
  });

  if (needAdmin) {
    return (
      <section className={CARD} data-testid="realtime-infer-card">
        <CardHeader icon={<Activity size={15} />} title="实时推理 · T-P6-08" />
        <div className="rounded-lg border border-dashed border-slate-200 px-2 py-3 text-[11px] text-slate-400">
          需管理员权限查看与配置（热集实时推理为平台级服务）
        </div>
      </section>
    );
  }

  const modelOptions = models.some((m) => m.model_dir === view.modelDir)
    ? models
    : view.modelDir
      ? [{ model_dir: view.modelDir, name: view.modelName, dir_name: view.modelName, has_onnx: view.onnxReady === true, feature_count: 0, updated_at: '' }, ...models]
      : models;

  return (
    <section className={CARD} data-testid="realtime-infer-card">
      <CardHeader
        icon={<Activity size={15} />}
        title="实时推理 · T-P6-08"
        meta={
          <span className="inline-flex items-center gap-1.5 text-[10px]">
            <span className={`h-1.5 w-1.5 rounded-full ${view.enabled ? 'bg-emerald-500' : 'bg-slate-300'}`} />
            <span className={view.enabled ? 'text-emerald-600' : 'text-slate-400'}>
              {view.enabled ? '运行中' : '已停用'}
            </span>
          </span>
        }
        extra={
          <button
            type="button"
            onClick={() => void load()}
            className="flex items-center gap-1 rounded-lg border border-slate-200 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50"
          >
            {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            刷新
          </button>
        }
      />
      {error && (
        <div className="mb-2 rounded-lg border border-red-100 bg-red-50 px-2 py-1 text-[11px] text-red-600">{error}</div>
      )}
      {note && (
        <div className="mb-2 rounded-lg border border-emerald-100 bg-emerald-50 px-2 py-1 text-[11px] text-emerald-700">{note}</div>
      )}

      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
        <span>
          模型 <span className="font-mono text-slate-700">{view.modelName}</span>
        </span>
        <span>节拍 {view.cadenceS}s</span>
        <span>
          覆盖率 <span className="font-mono text-slate-700">{view.coverageText}</span>
        </span>
        <span>
          ONNX{' '}
          <span
            className={`font-mono ${view.onnxReady ? 'text-emerald-600' : view.onnxReady === false ? 'text-amber-600' : 'text-slate-500'}`}
          >
            {view.onnxText}
          </span>
        </span>
        {view.staleMirror && <span className="text-amber-600">状态镜像超 5 分钟未更新（引擎循环可能未运行）</span>}
      </div>
      <div className="mb-3 rounded-lg border border-slate-100 bg-slate-50/60 px-2 py-1 text-[10px] text-slate-500">
        发布闸门：{gateHint(view.minCoverage)}
      </div>

      <div className="mb-3 grid grid-cols-4 gap-2">
        <StatTile label="已发布" value={view.published} tone="green" />
        <StatTile label="分数条数" value={view.scores} tone="blue" />
        <StatTile label="闸门拦截" value={view.skippedNoLive} tone={view.skippedNoLive > 0 ? 'amber' : 'slate'} />
        <StatTile
          label="最近周期"
          value={<span className="text-[10px]">{view.lastCycleAt ? view.lastCycleAt.slice(11, 19) : '—'}</span>}
          tone="slate"
        />
      </div>

      {(view.lastSkip || view.lastError) && (
        <div className="mb-3 space-y-0.5">
          {view.lastSkip && <div className="text-[10px] text-amber-600">跳过原因：{view.lastSkip}</div>}
          {view.lastError && <div className="text-[10px] text-red-500">最近错误：{view.lastError}</div>}
        </div>
      )}

      <div className="mb-3 space-y-2 rounded-lg border border-slate-100 px-2 py-2">
        <div className="flex flex-wrap items-center gap-2 text-[10px] text-slate-500">
          <span className="shrink-0">模型</span>
          <select
            value={modelOptions.some((m) => m.model_dir === modelDraft) ? modelDraft : ''}
            onChange={(e) => setModelDraft(e.target.value)}
            className="min-w-0 flex-1 rounded-md border border-slate-200 px-1.5 py-0.5 font-mono text-[10px]"
          >
            {!modelOptions.some((m) => m.model_dir === modelDraft) && (
              <option value="">{modelDraft || '未配置'}</option>
            )}
            {modelOptions.map((m) => (
              <option key={m.model_dir} value={m.model_dir}>
                {m.dir_name}
                {m.has_onnx ? '' : '（无 ONNX）'}
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={busy}
            onClick={onSaveModel}
            className="rounded-lg border border-slate-200 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50 disabled:opacity-50"
          >
            应用模型
          </button>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-[10px] text-slate-500">
          <label className="flex items-center gap-1">
            节拍
            <input
              value={cadenceDraft}
              onChange={(e) => setCadenceDraft(e.target.value)}
              className="w-14 rounded-md border border-slate-200 px-1.5 py-0.5 font-mono text-[11px]"
              placeholder="15"
            />
            秒
          </label>
          <button
            type="button"
            disabled={busy}
            onClick={onSaveCadence}
            className="rounded-lg border border-slate-200 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50 disabled:opacity-50"
          >
            保存节拍
          </button>
          <button
            type="button"
            disabled={busy || !view.modelDir}
            onClick={onExportOnnx}
            className="rounded-lg border border-slate-200 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50 disabled:opacity-50"
          >
            重建 ONNX
          </button>
        </div>
        <div className="text-[10px] text-slate-400">
          切换模型会清空覆盖白名单；ONNX 缺失时引擎首次运行自动导出（也可手动重建，含对照校验）
        </div>
      </div>

      <div className="mt-auto flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => void onToggle()}
          className={`rounded-lg px-3 py-1 text-[11px] font-semibold text-white disabled:opacity-50 ${
            view.enabled ? 'bg-slate-500 hover:bg-slate-400' : 'bg-blue-600 hover:bg-blue-500'
          }`}
        >
          {busy ? '处理中…' : view.enabled ? '停用' : '启用'}
        </button>
        <label className="flex items-center gap-1 text-[10px] text-slate-500">
          覆盖率闸门
          <input
            value={gateDraft}
            onChange={(e) => setGateDraft(e.target.value)}
            className="w-14 rounded-md border border-slate-200 px-1.5 py-0.5 font-mono text-[11px]"
            placeholder="0.5"
          />
        </label>
        <button
          type="button"
          disabled={busy}
          onClick={() => void onSaveGate()}
          className="rounded-lg border border-slate-200 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50 disabled:opacity-50"
        >
          保存闸门
        </button>
      </div>
    </section>
  );
};
