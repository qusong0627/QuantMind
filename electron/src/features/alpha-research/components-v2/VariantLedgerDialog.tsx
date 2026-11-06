import React, { useEffect, useRef, useState } from 'react';
import { X } from 'lucide-react';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { getVariantContexts, getVariantLedger, resetVariantCooling, type VariantContextPage, type VariantLedger } from '../services-v2/variantApi';

const metric = (value: number | null | undefined) => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(4) : '—';

export const VariantLedgerDialog: React.FC<{ universe: string; initialContextId?: string; onClose: () => void }> = ({ universe, initialContextId, onClose }) => {
  const [page, setPage] = useState<VariantContextPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [contextId, setContextId] = useState(initialContextId || '');
  const [ledger, setLedger] = useState<VariantLedger | null>(null);
  const [variant, setVariant] = useState('');
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState('');
  const [refresh, setRefresh] = useState(0);
  const actionController = useRef<AbortController | null>(null);

  useEffect(() => registerSessionReset(() => {
    actionController.current?.abort(); setPage(null); setLedger(null); setReasons({}); onClose();
  }), [onClose]);
  useEffect(() => () => { actionController.current?.abort(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    const generation = getSessionGeneration();
    setPage(null); setError('');
    getVariantContexts(universe, offset, controller.signal).then((result) => {
      if (controller.signal.aborted || generation !== getSessionGeneration()) return;
      setPage(result);
      setContextId((current) => current || result.items[0]?.context_id || '');
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(reason instanceof Error ? reason.message : '台账环境加载失败');
    });
    return () => controller.abort();
  }, [universe, offset, refresh]);
  useEffect(() => {
    const controller = new AbortController();
    const generation = getSessionGeneration();
    actionController.current?.abort(); setAction(''); setLedger(null); setVariant(''); setReasons({});
    if (!contextId) { setLoading(false); return () => controller.abort(); }
    setLoading(true); setError('');
    getVariantLedger(contextId, controller.signal).then((result) => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setLedger(result);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(reason instanceof Error ? reason.message : '变体台账加载失败');
    }).finally(() => {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setLoading(false);
    });
    return () => controller.abort();
  }, [contextId, refresh]);

  const reset = async (key: string) => {
    const reason = reasons[key]?.trim();
    if (!reason || action) return;
    const controller = new AbortController();
    actionController.current?.abort(); actionController.current = controller;
    const generation = getSessionGeneration();
    setAction(key); setError('');
    try {
      await resetVariantCooling(contextId, key, reason, controller.signal);
      if (!controller.signal.aborted && generation === getSessionGeneration()) setRefresh((value) => value + 1);
    } catch (failure: unknown) {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setError(failure instanceof Error ? failure.message : '解除冷却失败');
    } finally {
      if (!controller.signal.aborted && generation === getSessionGeneration()) setAction('');
    }
  };
  const evidence = ledger?.variants.find((row) => row.key === variant);

  return <div role="dialog" aria-modal="true" aria-label="本人变体台账" className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4">
    <div className="flex max-h-[90vh] w-full max-w-6xl flex-col rounded-2xl bg-white shadow-2xl">
      <div className="flex items-center justify-between border-b border-slate-100 p-4"><h3 className="font-bold text-slate-900">本人统计变体台账 · {universe}</h3><button type="button" onClick={onClose} aria-label="关闭变体台账" className="p-2 text-slate-500"><X className="h-4 w-4" /></button></div>
      <div className="space-y-4 overflow-y-auto p-4 text-sm text-slate-600">
        <p className="rounded-lg bg-blue-50 p-3 text-xs text-blue-800">冷却只复用本人在相同股票池、参数、数值算法与冻结数据版本下的证据。数据或配置变化会重新评估；父本受保护，样本不足不会判失败。解除冷却只影响此后封存的任务，已封存任务保持原名单。</p>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 text-xs">研究环境
            <select className="max-w-md rounded border border-slate-200 p-2" value={contextId} onChange={(event) => setContextId(event.target.value)}>
              {!contextId && <option value="">暂无已封存环境</option>}
              {contextId && !page?.items.some((item) => item.context_id === contextId) && <option value={contextId}>当前环境 {contextId.slice(0, 10)}</option>}
              {page?.items.map((item) => <option key={item.context_id} value={item.context_id}>{item.window.mining_start} 至 {item.window.mining_end} · {item.context_id.slice(0, 10)}</option>)}
            </select>
          </label>
          <button type="button" disabled={!page || offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))} className="rounded border px-2 py-1 text-xs disabled:opacity-40">上一页</button>
          <button type="button" disabled={!page || offset + 20 >= page.total} onClick={() => setOffset(offset + 20)} className="rounded border px-2 py-1 text-xs disabled:opacity-40">下一页</button>
          <span className="text-xs text-slate-400">共 {page?.total ?? '—'} 个环境</span>
          <button type="button" disabled={loading || Boolean(action)} onClick={() => setRefresh((value) => value + 1)} className="rounded border px-2 py-1 text-xs disabled:opacity-40">刷新</button>
        </div>
        {error && <p role="alert" className="text-red-600">{error}</p>}
        {loading && <p role="status">正在读取本人变体证据…</p>}
        {!loading && !contextId && page && <p>尚无统计挖掘封存环境；新任务封存数据后会自动建立台账。</p>}
        {ledger && <>
          <p className="text-xs text-slate-500">{ledger.scope.window.mining_start} 至 {ledger.scope.window.mining_end} · 算法 {ledger.scope.algorithm_version} · 数据 {ledger.scope.data.panel_sha256.slice(0, 12)} · 高置信样本至少 {ledger.constants.high_conf_days} 日，残差 |ICIR| 低于 {ledger.constants.cool_icir_floor} 时冷却 {ledger.constants.cooldown_days} 日。</p>
          <div className="overflow-x-auto"><table className="w-full text-left text-xs">
            <thead><tr className="border-b"><th className="p-2">变体 / 父本</th><th className="p-2">观测</th><th className="p-2">最新残差 |ICIR|</th><th className="p-2">有效日</th><th className="p-2">冷却</th><th className="p-2">操作</th></tr></thead>
            <tbody>{ledger.variants.map((row) => <tr key={row.key} className="border-b border-slate-100 align-top">
              <td className="p-2"><p className="font-medium">{row.name}</p><p className="mt-1 font-mono text-slate-400">{row.key}<br />父本 {row.baseline_key || '—'}</p>{row.edit_type && <p className="text-slate-400">{row.edit_type}</p>}{!row.coolable && <p className="text-slate-400">{row.baseline_key ? '父本保护' : '根或无母本'}</p>}</td>
              <td className="p-2">{row.observations_count}</td><td className="p-2">{metric(row.latest_stat?.ic_ir)}<p className="text-slate-400">{row.latest_stat?.status || '暂无证据'}</p></td><td className="p-2">{row.latest_stat?.n_days ?? '—'}</td>
              <td className="p-2">{row.cooling_active ? '冷却中' : '可评估'}{row.cooling_active && <><p>{row.cooled_until?.slice(0, 10)} 到期</p><p className="text-slate-400">判据 |ICIR| {metric(row.decision_stat?.ic_ir)} · {row.decision_stat?.n_days ?? '—'} 日</p></>}{row.reset_at && <p className="text-slate-400">已人工解除</p>}</td>
              <td className="space-y-2 p-2"><button type="button" onClick={() => setVariant(row.key)} className="text-blue-600">查看证据</button>{row.cooling_active && <div className="flex flex-col gap-1"><input aria-label={`${row.name}解除原因`} value={reasons[row.key] || ''} maxLength={500} disabled={Boolean(action)} onChange={(event) => setReasons((values) => ({ ...values, [row.key]: event.target.value }))} placeholder="填写解除原因" className="rounded border p-1" /><button type="button" disabled={Boolean(action) || !reasons[row.key]?.trim()} onClick={() => void reset(row.key)} className="text-left text-blue-600 disabled:opacity-40">{action === row.key ? '正在解除…' : '解除冷却'}</button></div>}</td>
            </tr>)}</tbody>
          </table></div>
          {evidence && <section className="space-y-2 rounded-lg border p-3"><h4 className="font-semibold">{evidence.name} · 最近 {ledger.constants.max_observations} 条观测</h4>{evidence.observations.length === 0 ? <p className="text-xs text-slate-400">暂无残差观测。</p> : evidence.observations.map((item) => <article key={item.task_id} className="border-b py-2 text-xs"><p>{item.at} · 任务 {item.task_id} · 最佳周期 {item.best_horizon ?? '—'}</p><div className="mt-1 flex flex-wrap gap-3">{item.horizons.map((stat) => <span key={stat.horizon}>{stat.horizon} 日：|ICIR| {metric(stat.ic_ir)}，{stat.n_days} 日，{stat.status}</span>)}</div></article>)}</section>}
          {ledger.reset_events.length > 0 && <details className="rounded-lg border p-3 text-xs"><summary className="cursor-pointer font-semibold">最近解除记录（最多 50 条）</summary>{ledger.reset_events.map((item, index) => <p key={index} className="mt-2 break-words">{item.at} · {item.variant_key} · {item.reason}</p>)}</details>}
        </>}
      </div>
    </div>
  </div>;
};
