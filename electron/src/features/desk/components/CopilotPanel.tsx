/** 交易台副驾驶面板（T-P6-16）：总线事件流 + 误报标注 + 时延/预算/误报率 + 建议卡执行。
 *
 * 数据源：/api/v1/copilot/panel（事件流/时延/预算/误报率）+ /api/v1/copilot/advice（建议卡）。
 * 纪律：块级不可用如实展示（不再有 mock）；执行/拒绝/标注均落后端留痕。
 */

import React, { useCallback, useEffect, useState } from 'react';
import { Activity, AlertTriangle, BellRing, Check, Info, Loader2, RefreshCw, ShieldQuestion, X } from 'lucide-react';
import { CARD, CardHeader, StatTile } from './cardKit';
import {
  actionLine,
  adviceStatusMeta,
  alertTypeLabel,
  outcomeMeta,
  panelMetrics,
  severityMeta,
  type CopilotAdvice,
  type CopilotPanel as CopilotPanelData,
} from './copilotModel';
import {
  annotateAlert,
  executeAdvice,
  getCopilotPanel,
  listAdvice,
  rejectAdvice,
} from '../services/copilotService';

const TONE_CLASS: Record<string, string> = {
  red: 'bg-red-50 border-red-100 text-red-600',
  amber: 'bg-amber-50 border-amber-100 text-amber-700',
  green: 'bg-emerald-50 border-emerald-100 text-emerald-700',
  blue: 'bg-blue-50 border-blue-100 text-blue-600',
  slate: 'bg-slate-50 border-slate-200 text-slate-600',
};

const Chip: React.FC<{ tone: string; children: React.ReactNode }> = ({ tone, children }) => (
  <span className={`inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] font-semibold ${TONE_CLASS[tone] || TONE_CLASS.slate}`}>
    {children}
  </span>
);

export const CopilotPanel: React.FC = () => {
  const [panel, setPanel] = useState<CopilotPanelData | null>(null);
  const [advice, setAdvice] = useState<CopilotAdvice[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string>('');
  const [error, setError] = useState<string>('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [p, a] = await Promise.all([getCopilotPanel(24), listAdvice('', 10)]);
      setPanel(p);
      setAdvice(a);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 60000);
    return () => window.clearInterval(timer);
  }, [load]);

  const metrics = panelMetrics(panel);
  const events = panel?.events?.items ?? [];

  const onAnnotate = async (alertId: string, annotation: 'true_positive' | 'false_positive') => {
    setBusy(alertId);
    try {
      await annotateAlert(alertId, annotation);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  };

  const onExecute = async (adviceId: string) => {
    setBusy(adviceId);
    try {
      await executeAdvice(adviceId);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  };

  const onReject = async (adviceId: string) => {
    setBusy(adviceId);
    try {
      await rejectAdvice(adviceId, '交易台手动拒绝');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  };

  return (
    <section className={CARD} data-testid="copilot-panel">
      <CardHeader
        icon={<BellRing size={15} />}
        title="副驾驶 · 实时情报"
        meta={<span className="text-[10px] text-slate-400">{panel?.as_of ? `截至 ${panel.as_of.slice(11, 19)}` : ''}</span>}
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
        <div className="mb-2 rounded-lg border border-red-100 bg-red-50 px-2 py-1 text-[11px] text-red-600">
          {error}
        </div>
      )}

      <div className="mb-3 grid grid-cols-4 gap-2">
        <StatTile label="时延 P95" value={metrics.latencyP95} tone={metrics.latencyP95Ms !== null && metrics.latencyP95Ms >= 60000 ? 'amber' : 'blue'} />
        <StatTile label="误报率(30d)" value={metrics.missRate} tone={metrics.missRate === '—' ? 'slate' : 'amber'} />
        <StatTile label="24h 事件" value={metrics.events} tone="slate" />
        <StatTile label="P6 资源" value={<span className="text-[11px]">{metrics.budgetText}</span>} tone="slate" />
      </div>

      {/* 事件流（卡片） */}
      <div className="mb-3 overflow-hidden rounded-xl border border-slate-200/80">
        <div className="flex items-center gap-2 border-b border-slate-100 bg-slate-50/70 px-3 py-2">
          <Activity size={13} className="shrink-0 text-blue-500" />
          <span className="text-[12px] font-bold text-slate-700">情报事件流（24h）</span>
          <span className="ml-auto rounded-full border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] font-semibold text-slate-500">
            {events.length} 条
          </span>
        </div>
        <div className="p-2">
      {panel?.events?.available === false ? (
        <div className="rounded-lg border border-dashed border-slate-200 px-2 py-3 text-[11px] text-slate-400">
          事件流不可用：{panel.events.reason || '未知原因'}
        </div>
      ) : events.length === 0 ? (
        <div className="rounded-lg border border-dashed border-slate-200 px-2 py-3 text-[11px] text-slate-400">
          近 24h 无情报事件
        </div>
      ) : (
        <ul className="space-y-1.5 max-h-56 overflow-y-auto pr-1">
          {events.slice(0, 12).map((event) => {
            const sev = severityMeta(event.severity);
            const outcome = outcomeMeta(event);
            return (
              <li key={event.alert_id} className={`group relative overflow-hidden rounded-xl border bg-white pl-3.5 pr-2 py-2 transition-all hover:shadow-sm ${
                sev.tone === 'red' ? 'border-red-100' : sev.tone === 'amber' ? 'border-amber-100' : 'border-slate-200/80'
              }`}>
                {/* 左侧严重度色条 */}
                <span className={`absolute left-0 top-0 h-full w-1 ${sev.tone === 'red' ? 'bg-red-400' : sev.tone === 'amber' ? 'bg-amber-400' : 'bg-slate-300'}`} />
                <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 text-[11px] flex-wrap">
                    <span className={`inline-flex items-center gap-1 font-bold ${
                      sev.tone === 'red' ? 'text-red-600' : sev.tone === 'amber' ? 'text-amber-600' : 'text-slate-500'
                    }`}>
                      {sev.tone === 'red' ? <AlertTriangle className="w-3 h-3" /> : sev.tone === 'amber' ? <BellRing className="w-3 h-3" /> : <Info className="w-3 h-3" />}
                      {alertTypeLabel(event.alert_type)}
                    </span>
                    <span className="font-mono text-slate-500">{event.symbol}</span>
                    <Chip tone={outcome.tone}>{outcome.label}</Chip>
                    {event.pushed && <Chip tone="blue">已推送</Chip>}
                  </div>
                  <div className="mt-0.5 line-clamp-2 text-[11px] leading-4 text-slate-600" title={event.title}>{event.title}</div>
                </div>
                {event.annotation ? (
                  <span className="shrink-0 rounded-full border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[10px] text-slate-400">已标注</span>
                ) : (
                  <div className="flex shrink-0 gap-1 opacity-70 transition-opacity group-hover:opacity-100">
                    <button
                      type="button"
                      title="标注为真实告警"
                      disabled={busy === event.alert_id}
                      onClick={() => void onAnnotate(event.alert_id, 'true_positive')}
                      className="rounded-md border border-emerald-100 bg-emerald-50 p-1 text-emerald-600 hover:bg-emerald-100 disabled:opacity-50"
                    >
                      <Check size={11} />
                    </button>
                    <button
                      type="button"
                      title="标注为误报"
                      disabled={busy === event.alert_id}
                      onClick={() => void onAnnotate(event.alert_id, 'false_positive')}
                      className="rounded-md border border-red-100 bg-red-50 p-1 text-red-500 hover:bg-red-100 disabled:opacity-50"
                    >
                      <X size={11} />
                    </button>
                  </div>
                )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

        </div>
      </div>

      {/* 建议卡（卡片） */}
      <div className="overflow-hidden rounded-xl border border-slate-200/80">
        <div className="flex items-center gap-2 border-b border-slate-100 bg-slate-50/70 px-3 py-2">
          <ShieldQuestion size={13} className="shrink-0 text-indigo-500" />
          <span className="text-[12px] font-bold text-slate-700">建议卡</span>
          <span className="ml-auto rounded-full border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] font-semibold text-slate-500">
            {advice.length} 条
          </span>
        </div>
        <div className="p-2">
      {advice.length === 0 ? (
        <div className="rounded-lg border border-dashed border-slate-200 px-2 py-3 text-[11px] text-slate-400">
          暂无建议卡（QuantBot 生成后在此决策）
        </div>
      ) : (
        <ul className="space-y-2">
          {advice.map((item) => {
            const meta = adviceStatusMeta(item.status);
            const isPending = item.status === 'pending';
            return (
              <li key={item.advice_id} className="rounded-xl border border-slate-200 px-3 py-2">
                <div className="flex items-center gap-2">
                  <ShieldQuestion size={13} className="shrink-0 text-blue-500" />
                  <span className="text-[12px] font-semibold text-slate-800">{item.title}</span>
                  <Chip tone={meta.tone}>{meta.label}</Chip>
                  <span className="ml-auto text-[10px] text-slate-400">{item.source}</span>
                </div>
                {item.rationale && (
                  <div className="mt-1 text-[11px] text-slate-500">{item.rationale}</div>
                )}
                <ul className="mt-1 space-y-0.5">
                  {item.actions.map((action, idx) => (
                    <li key={`${item.advice_id}-${idx}`} className="font-mono text-[11px] text-slate-600">
                      · {actionLine(action)}
                    </li>
                  ))}
                </ul>
                {item.execution && item.execution.length > 0 && (
                  <div className="mt-1 space-y-0.5">
                    {item.execution.map((exec, idx) => (
                      <div key={`${item.advice_id}-ex-${idx}`} className={`text-[10px] ${exec.success ? 'text-emerald-600' : 'text-red-500'}`}>
                        {exec.symbol} {exec.side}：{exec.success ? '已受理' : exec.message || '失败'}
                        {exec.duplicate ? '（幂等命中）' : ''}
                      </div>
                    ))}
                  </div>
                )}
                {isPending && (
                  <div className="mt-2 flex gap-2">
                    <button
                      type="button"
                      disabled={busy === item.advice_id}
                      onClick={() => void onExecute(item.advice_id)}
                      className="rounded-lg bg-blue-600 px-3 py-1 text-[11px] font-semibold text-white hover:bg-blue-500 disabled:opacity-50"
                    >
                      {busy === item.advice_id ? '执行中…' : '一键执行（OrderRouter）'}
                    </button>
                    <button
                      type="button"
                      disabled={busy === item.advice_id}
                      onClick={() => void onReject(item.advice_id)}
                      className="rounded-lg border border-slate-200 px-3 py-1 text-[11px] text-slate-600 hover:bg-slate-50 disabled:opacity-50"
                    >
                      拒绝
                    </button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
        </div>
      </div>
    </section>
  );
};
