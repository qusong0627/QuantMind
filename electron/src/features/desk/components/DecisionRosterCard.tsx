/** 交易台「决策模型名册」卡：`QM_DECISION_LLM_ROSTER` 的现状 / 保存 / 清空。
 *
 * 数据源：`/api/v1/decision/roster`（GET/PUT/DELETE，admin 门）。这一页回答四件事：
 * 现在跑的是名册还是单家三件套、每家端点/key 配没配、每家上一轮跑成什么样、
 * 以及**这家为什么跑不动**（逐家解析错误原文，点名变量）。
 *
 * 三处刻意的克制：
 * 1. **决策轮开关只展示不代写**：worker 只在启动时判一次（`decision_round_runner`
 *    判否即 return），代写就是撒谎——写了不重启是假生效，写了重启是真钱开跑。
 * 2. **key 只写不回**：输入框永远空着，留空=沿用原 key；要删除得显式点「清除 key」。
 * 3. **清空按钮在单家三件套不可用时禁用**并说明原因：后端会拒绝并回滚，前端先说清楚，
 *    省得用户点出一个 400 再猜。
 */

import React, { useCallback, useEffect, useState } from 'react';
import { BrainCircuit, Loader2, Plus, RefreshCw, Trash2, X } from 'lucide-react';
import { CARD, CardHeader } from './cardKit';
import {
  blankDraft,
  buildSavePayload,
  clearGuard,
  draftFromEntry,
  entryEndpointText,
  entryKeyText,
  entryStatusLine,
  lastRoundLine,
  overLimitHint,
  rosterHeadline,
  roundStatusLabel,
  roundStatusTone,
  sourceLabel,
  sourceTone,
  tuningText,
  type RosterDraft,
  type RosterEntryView,
  type RosterState,
  type Tone,
} from './decisionRosterModel';
import { clearRoster, getRoster, saveRoster } from '../services/decisionRosterService';

const TONE_CLS: Record<Tone, string> = {
  ok: 'text-emerald-600',
  warn: 'text-amber-600',
  bad: 'text-red-600',
  off: 'text-slate-400',
};

const INPUT =
  'w-full rounded-lg border border-slate-200 px-2 py-1 text-[11px] text-slate-700 placeholder:text-slate-300 focus:border-blue-300 focus:outline-none';

const RunRow: React.FC<{ label: string; full?: boolean; children: React.ReactNode }> = ({
  label,
  full = false,
  children,
}) => (
  <>
    <span className="text-slate-400">{label}</span>
    <span className={`min-w-0 truncate text-slate-600 ${full ? 'md:col-span-3' : ''}`}>{children}</span>
  </>
);

export const DecisionRosterCard: React.FC = () => {
  const [state, setState] = useState<RosterState | null>(null);
  const [needAdmin, setNeedAdmin] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [errors, setErrors] = useState<string[]>([]);
  const [note, setNote] = useState('');
  const [editOpen, setEditOpen] = useState(false);
  const [drafts, setDrafts] = useState<RosterDraft[]>([]);
  const [confirmClear, setConfirmClear] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { state: next } = await getRoster();
      setState(next);
      setNeedAdmin(false);
      setError('');
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

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError('');
    setErrors([]);
    setNote('');
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setErrors((e as { errors?: string[] }).errors || []);
    } finally {
      setBusy(false);
    }
  };

  const openEdit = () => {
    setDrafts(state?.entries.length ? state.entries.map(draftFromEntry) : [blankDraft()]);
    setErrors([]);
    setNote('');
    setEditOpen(true);
  };

  // 传值更新，不用函数式 setter：electron/ 的 tsc 把 setter 类型简化成了
  // `(value: S) => void`（丢掉 SetStateAction 的函数分支），函数式写法一律 TS2345。
  const patchDraft = (index: number, patch: Partial<RosterDraft>) => {
    setDrafts(drafts.map((d, i) => (i === index ? { ...d, ...patch } : d)));
  };

  const onSave = () =>
    run(async () => {
      const next = await saveRoster(buildSavePayload(drafts).entries);
      setState(next);
      setEditOpen(false);
      setNote('已生效：trade 进程下一轮就按新名册跑，无需重启。');
    });

  const onClear = () =>
    run(async () => {
      const next = await clearRoster();
      setState(next);
      setConfirmClear(false);
      setEditOpen(false);
      setNote('已回到单家三件套。');
    });

  if (needAdmin) {
    return (
      <section className={CARD} data-testid="decision-roster-card">
        <CardHeader icon={<BrainCircuit size={15} />} title="决策模型名册" />
        <div className="rounded-lg border border-dashed border-slate-200 px-2 py-3 text-[11px] text-slate-400">
          需管理员权限查看与配置（多模型名册决定真实下单的决策来源）
        </div>
      </section>
    );
  }

  const tone = sourceTone(state?.source || 'none');
  const guard = state ? clearGuard(state) : '';
  const limitHint = state ? overLimitHint(state, drafts.length) : '';

  return (
    <section className={CARD} data-testid="decision-roster-card">
      <CardHeader
        icon={<BrainCircuit size={15} />}
        title="决策模型名册"
        meta={
          <span className={`flex items-center gap-1 text-[11px] font-semibold ${TONE_CLS[tone]}`}>
            <span className="h-1.5 w-1.5 rounded-full bg-current" />
            {sourceLabel(state?.source || 'none')}
          </span>
        }
        extra={
          <div className="flex items-center gap-1.5">
            {!editOpen && (
              <button
                type="button"
                onClick={openEdit}
                disabled={loading || !state}
                className="rounded-lg border border-slate-200 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50 disabled:opacity-40"
              >
                编辑名册
              </button>
            )}
            <button
              type="button"
              onClick={() => void load()}
              className="flex items-center gap-1 rounded-lg border border-slate-200 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50"
            >
              {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
              刷新
            </button>
          </div>
        }
      />

      {error && (
        <div className="mb-2 rounded-lg border border-red-100 bg-red-50 px-2 py-1 text-[11px] text-red-600">
          {error}
          {errors.length > 1 && (
            <ul className="mt-1 list-disc pl-4">
              {errors.slice(1).map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {note && (
        <div className="mb-2 rounded-lg border border-emerald-100 bg-emerald-50 px-2 py-1 text-[11px] text-emerald-700">
          {note}
        </div>
      )}

      <div className="mb-3 rounded-xl border border-slate-200/80 bg-slate-50/50 px-3 py-2">
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] md:grid-cols-[auto_1fr_auto_1fr]">
          <RunRow label="名册">
            <span className="text-slate-700">{state ? rosterHeadline(state) : '—'}</span>
          </RunRow>
          <RunRow label="决策轮">
            <span className={state?.round.enabled ? TONE_CLS.ok : TONE_CLS.off}>
              {state?.round.enabled ? '开' : '关'}
            </span>
            <span className="ml-1.5 text-slate-400">（开关只在启动时判一次）</span>
          </RunRow>
          <RunRow label="变量">
            <span className="font-mono">{state?.env || 'QM_DECISION_LLM_ROSTER'}</span>
          </RunRow>
          <RunRow label="落盘">
            <span className="font-mono" title={state?.runtime_env_path}>
              {state?.runtime_env_path || '—'}
            </span>
          </RunRow>
          <RunRow label="最后一轮" full>
            {lastRoundLine(state)}
          </RunRow>
        </div>
      </div>

      {state?.error && (
        <div className="mb-2 rounded-lg border border-amber-100 bg-amber-50 px-2 py-1 text-[11px] text-amber-700">
          名册解析失败（这一轮一家都跑不了）：{state.error}
        </div>
      )}
      {state && !state.roster_configured && (
        <div className="mb-2 rounded-lg border border-slate-200 bg-slate-50/60 px-2 py-1 text-[11px] text-slate-600">
          未启名册，走单家三件套：{state.single.ok ? '可用' : state.single.error || '未配置'}
        </div>
      )}
      {state?.status_error && (
        <div className="mb-2 text-[11px] text-slate-400">状态镜像读不到：{state.status_error}</div>
      )}

      {/* ── 逐家 ───────────────────────────────────────────────────────── */}
      {!editOpen && (
        <div className="flex flex-col gap-1.5">
          {(state?.entries || []).map((entry) => (
            <EntryRow key={`${entry.index}-${entry.agent}`} entry={entry} />
          ))}
          {state && state.entries.length === 0 && (
            <div className="rounded-lg border border-dashed border-slate-200 px-2 py-2 text-[11px] text-slate-400">
              名册为空——点「编辑名册」加第一家常驻模型。
            </div>
          )}
        </div>
      )}

      {/* ── 编辑 ───────────────────────────────────────────────────────── */}
      {editOpen && (
        <div className="flex flex-col gap-2">
          {limitHint && <div className="text-[11px] text-amber-600">{limitHint}</div>}
          {drafts.map((draft, index) => (
            <div key={index} className="rounded-xl border border-slate-200 px-2.5 py-2">
              <div className="mb-1.5 flex items-center gap-2">
                <span className="text-[10px] font-bold tracking-wide text-slate-400">
                  第 {index + 1} 家
                </span>
                <span className="h-px flex-1 bg-slate-200/70" />
                <button
                  type="button"
                  onClick={() => setDrafts(drafts.filter((_, i) => i !== index))}
                  className="text-slate-300 hover:text-red-500"
                  aria-label={`移除第 ${index + 1} 家`}
                >
                  <X size={12} />
                </button>
              </div>
              <div className="grid grid-cols-1 gap-1.5 md:grid-cols-2">
                <label className="flex flex-col gap-0.5">
                  <span className="text-[10px] text-slate-400">模型名（同时是账本身份，必须唯一）</span>
                  <input
                    className={INPUT}
                    value={draft.model}
                    aria-label={`第 ${index + 1} 家模型名`}
                    placeholder="deepseek-v4-pro"
                    onChange={(e) => patchDraft(index, { model: e.target.value })}
                  />
                </label>
                <label className="flex flex-col gap-0.5">
                  <span className="text-[10px] text-slate-400">端点（留空=沿用，回落全局）</span>
                  <input
                    className={INPUT}
                    value={draft.baseUrl}
                    aria-label={`第 ${index + 1} 家端点`}
                    placeholder="https://api.example.com/v1"
                    onChange={(e) => patchDraft(index, { baseUrl: e.target.value })}
                  />
                </label>
                <label className="flex flex-col gap-0.5">
                  <span className="text-[10px] text-slate-400">
                    API Key（留空=沿用原来那把，永不回显）
                  </span>
                  <input
                    className={INPUT}
                    type="password"
                    autoComplete="off"
                    value={draft.apiKey}
                    aria-label={`第 ${index + 1} 家 API Key`}
                    placeholder={draft.clearKey ? '将清除已存 key' : '••••••••'}
                    onChange={(e) => patchDraft(index, { apiKey: e.target.value })}
                  />
                </label>
                <div className="flex items-end gap-2 pb-1 text-[10px] text-slate-500">
                  <label className="flex items-center gap-1">
                    <input
                      type="checkbox"
                      checked={draft.clearKey}
                      onChange={(e) => patchDraft(index, { clearKey: e.target.checked, apiKey: '' })}
                    />
                    清除已存 key
                  </label>
                </div>
                <div className="grid grid-cols-3 gap-1.5 md:col-span-2">
                  <label className="flex flex-col gap-0.5">
                    <span className="text-[10px] text-slate-400">超时(秒)</span>
                    <input
                      className={INPUT}
                      value={draft.timeout}
                      aria-label={`第 ${index + 1} 家超时`}
                      placeholder="120"
                      onChange={(e) => patchDraft(index, { timeout: e.target.value })}
                    />
                  </label>
                  <label className="flex flex-col gap-0.5">
                    <span className="text-[10px] text-slate-400">token 上限</span>
                    <input
                      className={INPUT}
                      value={draft.maxTokens}
                      aria-label={`第 ${index + 1} 家 token 上限`}
                      placeholder="4000"
                      onChange={(e) => patchDraft(index, { maxTokens: e.target.value })}
                    />
                  </label>
                  <label className="flex flex-col gap-0.5">
                    <span className="text-[10px] text-slate-400">温度</span>
                    <input
                      className={INPUT}
                      value={draft.temperature}
                      aria-label={`第 ${index + 1} 家温度`}
                      placeholder="0.3"
                      onChange={(e) => patchDraft(index, { temperature: e.target.value })}
                    />
                  </label>
                </div>
              </div>
            </div>
          ))}

          <div className="flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              onClick={() => setDrafts([...drafts, blankDraft()])}
              className="flex items-center gap-1 rounded-lg border border-slate-200 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50"
            >
              <Plus size={12} /> 加一家
            </button>
            <span className="flex-1" />
            <button
              type="button"
              onClick={() => setEditOpen(false)}
              className="rounded-lg border border-slate-200 px-2.5 py-1 text-[11px] text-slate-600 hover:bg-slate-50"
            >
              取消
            </button>
            <button
              type="button"
              disabled={busy || drafts.length === 0}
              onClick={() => void onSave()}
              className="rounded-lg bg-blue-600 px-2.5 py-1 text-[11px] font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {busy ? '保存中…' : '保存名册'}
            </button>
          </div>
        </div>
      )}

      {/* ── 脚注：口径与纪律 ───────────────────────────────────────────── */}
      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-slate-100 pt-2 text-[10px] text-slate-400">
        <span>保存后立即生效（下一轮 tick 读新名册），无需重启</span>
        <span className="flex-1" />
        {!editOpen && state?.roster_configured && (
          <>
            {guard && <span className="text-amber-600">清空已禁用：{guard}</span>}
            <button
              type="button"
              disabled={busy || Boolean(guard)}
              title={guard || undefined}
              onClick={() => (confirmClear ? void onClear() : setConfirmClear(true))}
              onBlur={() => setConfirmClear(false)}
              className={`flex items-center gap-1 rounded-lg border px-2 py-0.5 disabled:opacity-40 ${
                confirmClear
                  ? 'border-red-200 bg-red-50 text-red-600'
                  : 'border-slate-200 text-slate-500 hover:bg-slate-50'
              }`}
            >
              <Trash2 size={11} />
              {confirmClear ? '再点一次确认清空' : '清空名册'}
            </button>
          </>
        )}
      </div>
    </section>
  );
};

const EntryRow: React.FC<{ entry: RosterEntryView }> = ({ entry }) => (
  <div
    className={`rounded-xl border px-2.5 py-1.5 ${
      entry.ok ? 'border-slate-200/80' : 'border-red-100 bg-red-50/40'
    }`}
    data-testid={`roster-entry-${entry.index}`}
  >
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
      <span className="text-[12px] font-semibold text-slate-800">{entry.model || '（缺模型名）'}</span>
      <span className="font-mono text-[10px] text-slate-400">{entry.agent}</span>
      <span className="flex-1" />
      <span className={`text-[11px] font-semibold ${entry.ok ? TONE_CLS.ok : TONE_CLS.bad}`}>
        {entry.ok ? '可用' : '不可用'}
      </span>
    </div>
    <div className="mt-0.5 grid grid-cols-1 gap-x-4 text-[11px] text-slate-500 md:grid-cols-2">
      <div className="min-w-0 truncate" title={entry.base_url || entry.base_url_env}>
        端点 <span className="text-slate-700">{entryEndpointText(entry)}</span>
      </div>
      <div className="min-w-0 truncate">Key <span className="text-slate-700">{entryKeyText(entry)}</span></div>
      <div className="min-w-0 truncate">调参 <span className="text-slate-700">{tuningText(entry)}</span></div>
      <div className="min-w-0 truncate" title={entry.status?.round_id || undefined}>
        上一轮{' '}
        <span className={TONE_CLS[roundStatusTone(entry.status?.status)]}>
          {entryStatusLine(entry.status)}
        </span>
      </div>
    </div>
    {!entry.ok && entry.error && (
      <div className="mt-0.5 text-[11px] text-red-600">{entry.error}</div>
    )}
    {entry.ok && entry.status?.errors?.length ? (
      <div className="mt-0.5 text-[11px] text-amber-600">
        本轮告警：{entry.status.errors.slice(0, 2).join('；')}
      </div>
    ) : null}
    {/* 未登记的轮次状态原样回显（不编标签）*/}
    {entry.status?.status && roundStatusLabel(entry.status.status) === entry.status.status ? (
      <div className="mt-0.5 text-[10px] text-slate-400">
        未登记的轮次状态：{entry.status.status}
      </div>
    ) : null}
  </div>
);
