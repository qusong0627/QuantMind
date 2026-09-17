/** 交易台「实时推理」卡（T-P6-08 收口）：状态/覆盖率闸门/开关（admin）。
 *
 * 数据源：/api/v1/admin/realtime/infer/config（配置 + 引擎状态镜像）。
 * 非管理员：诚实降级为「需管理员权限」；镜像超 5 分钟未更新 → 提示引擎循环可能未运行。
 */

import React, { useCallback, useEffect, useState } from 'react';
import { Activity, Loader2, RefreshCw } from 'lucide-react';
import { CARD, CardHeader, StatTile } from './cardKit';
import { gateHint, inferViewState, type InferConfigView, type InferStatusView } from './realtimeInferModel';
import { getInferConfig, setInferConfig } from '../services/realtimeInferService';

export const RealtimeInferenceCard: React.FC = () => {
  const [config, setConfig] = useState<InferConfigView | null>(null);
  const [status, setStatus] = useState<InferStatusView | null>(null);
  const [needAdmin, setNeedAdmin] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [gateDraft, setGateDraft] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await getInferConfig();
      setConfig(data.config);
      setStatus(data.status);
      setNeedAdmin(false);
      setGateDraft(String(data.config?.min_live_coverage ?? ''));
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

  const view = inferViewState(config, status, Date.now(), needAdmin);

  const onToggle = async () => {
    setBusy(true);
    setError('');
    try {
      await setInferConfig({ enabled: !view.enabled });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onSaveGate = async () => {
    const value = Number(gateDraft);
    if (!Number.isFinite(value) || value < 0 || value > 1) {
      setError('覆盖率闸门需为 0~1 之间的小数');
      return;
    }
    setBusy(true);
    setError('');
    try {
      await setInferConfig({ min_live_coverage: value });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

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

      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
        <span>
          模型 <span className="font-mono text-slate-700">{view.modelName}</span>
        </span>
        <span>节拍 {view.cadenceS}s</span>
        <span>
          覆盖率 <span className="font-mono text-slate-700">{view.coverageText}</span>
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
