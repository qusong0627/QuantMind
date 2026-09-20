/**
 * 候选信号一键推送 · 确认面板（T-FE-09）。
 *
 * 面板的内容**不是前端猜的**：整块由 `/push-orders/preflight` 的逐笔结果渲染，
 * 与真正下单共用同一份组装逻辑（`push_orders._build_legs`），所以「看到的」
 * 就是「发出去的」。用户的原始要求是「我推送前，都需要系统帮我排除风险的，
 * 包括能显示如果有新闻利好、或者利空的都要给我标注」—— 逐笔的风险列即那句话的实现。
 *
 * 三处刻意的设计：
 * 1. **`batch_id` 在面板打开时生成一次**，不是点确认时生成：服务端用它拼幂等键，
 *    重复点确认不会重复下单（回执里如实标 `duplicate`）。
 * 2. **实盘要硬确认**（输入确认词），且白名单/急停/配额三态**并列**展示 ——
 *    真钱路径上「点了两下就过」的确认等于没有确认。
 * 3. **回执逐笔如实**：未提交/重复/失败各占一个词，`skipped` 与 `failed` 绝不渲染成成功。
 */

import { useCallback, useEffect, useMemo, useState, type ReactElement } from 'react';
import { AlertTriangle, RefreshCw, Send, ShieldCheck } from 'lucide-react';
import { Checkbox, Input, Modal, Segmented, Spin, Tooltip, message } from 'antd';
import { stockTerminalService } from '../services/stockTerminalService';
import { riskChips } from '../riskModel';
import { newUuid } from '../../../utils/uuid';
import {
  CHANNEL_OPTIONS,
  MAX_PICK,
  REAL_CONFIRM_WORD,
  blockedReason,
  blockedTag,
  budgetBanner,
  channelLabel,
  effectiveQuantity,
  executeHeadline,
  isRealDirect,
  legAmount,
  legResultView,
  mirrorPlanIssues,
  mirrorPrecheckText,
  mirrorReceiptView,
  panelSummary,
  parseQuantity,
  pushGate,
  quotaLine,
  realDirectText,
  riskVerdictView,
} from '../pushModel';
import type {
  PushChannel,
  PushExecute,
  PushLeg,
  PushPreflight,
  PushSide,
} from '../../stock-terminal-shared/types';

interface Props {
  open: boolean;
  /** 勾选的股票（后缀式 600036.SH） */
  symbols: string[];
  side: PushSide;
  channels: PushChannel[];
  onChannelsChange: (c: PushChannel[]) => void;
  onClose: () => void;
  /** 推送完成（用于刷新持仓/清空勾选） */
  onDone?: (data: PushExecute) => void;
}

function fmtMoney(v: number): string {
  return `¥${Number(v || 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`;
}

/** 服务端错误 → 一句人话（`detail` 可能带逐笔阻断清单） */
function errText(err: unknown): string {
  const e = err as { response?: { data?: { detail?: unknown } }; message?: string };
  const detail = e?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object') {
    const d = detail as { message?: string; blocked?: { symbol: string; why?: string }[] };
    const head = d.message || '推送被拒绝';
    const legs = (d.blocked ?? []).slice(0, 3).map(b => `${b.symbol}：${b.why || ''}`).join('；');
    return legs ? `${head}（${legs}）` : head;
  }
  return e?.message || '推送失败';
}

const TONE_CLS: Record<string, string> = {
  ok: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  queued: 'bg-sky-50 text-sky-700 border-sky-200',
  dup: 'bg-amber-50 text-amber-700 border-amber-200',
  skipped: 'bg-slate-100 text-slate-500 border-slate-200',
  fail: 'bg-rose-50 text-rose-600 border-rose-200',
};

/** 风险/新闻徽章（判据在 riskModel.riskChips，这里只管画） */
function RiskChips({ risk }: { risk: PushLeg['risk'] }): ReactElement | null {
  const chips = riskChips(risk);
  if (!chips.length) return null;
  return (
    <span className="flex flex-wrap items-center gap-0.5">
      {chips.map(c => (
        <Tooltip key={c.key} title={<span className="whitespace-pre-line text-[11px]">{c.title}</span>}>
          <span className={`text-[9px] rounded px-1 py-0.5 shrink-0 ${c.cls}`}>{c.label}</span>
        </Tooltip>
      ))}
    </span>
  );
}

const LEG_GRID = 'grid grid-cols-[20px_minmax(120px,1fr)_56px_92px_84px_minmax(90px,1fr)_68px_86px] gap-1.5 items-center';

export function PushConfirmPanel({ open, symbols, side, channels, onChannelsChange, onClose, onDone }: Props): ReactElement {
  const [batchId, setBatchId] = useState('');
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');
  const [data, setData] = useState<PushPreflight | null>(null);
  const [deselected, setDeselected] = useState<string[]>([]);
  const [qtyText, setQtyText] = useState<Record<string, string>>({});
  const [ackRisk, setAckRisk] = useState(false);
  const [realAckText, setRealAckText] = useState('');
  const [executing, setExecuting] = useState(false);
  const [result, setResult] = useState<PushExecute | null>(null);

  const real = channels.includes('real');

  // batch_id 与预检同生共死于面板的这一次打开：关掉再开会换新 id（新的一批），
  // 而同一批里反复点确认始终是同一个 id → 服务端幂等键命中，不会重复下单。
  useEffect(() => {
    if (!open) return;
    // 必须走 newUuid()：`crypto.randomUUID` 只在安全上下文存在，局域网 http 打开时
    // 裸调会 TypeError 把整页崩掉（预检面板一点「卖出」就崩，2026-09-20 实测）
    setBatchId(newUuid());
    setData(null);
    setErr('');
    setDeselected([]);
    setQtyText({});
    setAckRisk(false);
    setRealAckText('');
    setResult(null);
  }, [open]);

  const runPreflight = useCallback(async (id: string) => {
    if (!id || !symbols.length) return;
    setLoading(true);
    setErr('');
    try {
      const resp = await stockTerminalService.pushOrdersPreflight({
        symbols, side, channels, batch_id: id, ack_risk: ackRisk,
      });
      setData(resp);
    } catch (e) {
      setErr(errText(e));
    } finally {
      setLoading(false);
    }
  }, [symbols, side, channels, ackRisk]);

  useEffect(() => {
    if (!open || !batchId || result) return;
    void runPreflight(batchId);
  }, [open, batchId, result, runPreflight]);

  // useMemo 而非裸表达式：下面的 useMemo/useCallback 都依赖 `legs`，
  // 每次渲染新建数组会让它们全部失效（lint 的 exhaustive-deps 也据此报警）
  const legs: PushLeg[] = useMemo(() => data?.legs ?? [], [data]);

  // 手填量：只把**合法**的收进 edits（非法的走 invalid 集合，把确认按钮按住）
  const { edits, invalidSymbols } = useMemo(() => {
    const e = new Map<string, number>();
    const bad = new Set<string>();
    for (const [symbol, text] of Object.entries(qtyText)) {
      const parsed = parseQuantity(text);
      if (parsed.error) bad.add(symbol);
      else if (parsed.value != null) e.set(symbol, parsed.value);
    }
    return { edits: e, invalidSymbols: bad };
  }, [qtyText]);

  const deselectSet = useMemo(() => new Set(deselected), [deselected]);
  const summary = useMemo(
    () => panelSummary(legs, deselectSet, edits, invalidSymbols),
    [legs, deselectSet, edits, invalidSymbols],
  );
  const gate = pushGate(summary, { channels, realAck: realAckText.trim() === REAL_CONFIRM_WORD, loading });
  const issues = mirrorPlanIssues(data?.mirror);
  const quota = quotaLine(data?.mirror);
  const budgetText = budgetBanner(data?.meta?.budget);

  const toggleLeg = useCallback((symbol: string) => {
    setDeselected(deselected.includes(symbol) ? deselected.filter(s => s !== symbol) : [...deselected, symbol]);
  }, [deselected]);

  const submit = useCallback(async () => {
    const runSymbols = legs
      .filter(l => l.executable && !deselectSet.has(l.symbol))
      .map(l => l.symbol);
    setExecuting(true);
    try {
      // 只发改量过的键：没改的让服务端按自己的口径算（前端重算一遍必然与它漂开）
      const quantities: Record<string, number> = {};
      for (const [symbol, qty] of edits) if (runSymbols.includes(symbol)) quantities[symbol] = qty;
      const resp = await stockTerminalService.pushOrders({
        symbols: runSymbols,
        side,
        channels,
        batch_id: batchId,
        ack_risk: ackRisk,
        ...(Object.keys(quantities).length ? { quantities } : {}),
      });
      setResult(resp);
      onDone?.(resp);
    } catch (e) {
      message.error(errText(e));
    } finally {
      setExecuting(false);
    }
  }, [legs, deselectSet, edits, side, channels, batchId, ackRisk, onDone]);

  const headline = result ? executeHeadline(result) : null;

  return (
    <Modal
      open={open}
      title={
        <span className="flex items-center gap-2">
          <Send className="w-4 h-4 text-blue-600" />
          一键推送 · {side === 'buy' ? '买入' : '卖出'} · 已选 {symbols.length} 只
          <span className="text-[11px] font-normal text-slate-400">{channelLabel(channels)}</span>
        </span>
      }
      onCancel={onClose}
      width={1080}
      centered
      zIndex={1200}
      footer={null}
      styles={{ body: { maxHeight: '76vh', overflowY: 'auto' } }}
    >
      <div className="space-y-3 text-xs">
        {/* 通道选择：中途换通道要重跑预检（实盘配额与风控账户上下文都变） */}
        <div className="flex flex-wrap items-center gap-2">
          <Segmented
            size="small"
            value={real ? 'real' : 'sim'}
            onChange={(v) => onChannelsChange(v === 'real' ? ['sim', 'real'] : ['sim'])}
            options={CHANNEL_OPTIONS.map(o => ({ value: o.value, label: o.label, title: o.hint }))}
          />
          <span className="text-[11px] text-slate-400">
            {CHANNEL_OPTIONS.find(o => o.value === (real ? 'real' : 'sim'))?.hint}
          </span>
          <button
            type="button"
            onClick={() => void runPreflight(batchId)}
            disabled={loading}
            className="ml-auto inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2 py-1 text-[11px] font-bold text-slate-600 hover:bg-slate-50 disabled:opacity-50"
          >
            <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} /> 重新预检
          </button>
        </div>

        {/* 实盘闸门：白名单/急停/就绪/配额——并列展示，不吞并 */}
        {real && (
          <div className={`rounded-xl border p-2.5 space-y-1 ${issues.length ? 'bg-amber-50 border-amber-200 text-amber-800' : 'bg-emerald-50 border-emerald-200 text-emerald-800'}`}>
            <div className="flex items-center gap-1.5 font-bold">
              <ShieldCheck className="w-3.5 h-3.5" /> 实盘闸门
              {issues.length === 0 && <span>· 就绪</span>}
            </div>
            {issues.map(t => (
              <div key={t} className="flex items-start gap-1">
                <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />
                <span>{t.replace(/\*\*/g, '')}</span>
              </div>
            ))}
            {quota && <div className="font-mono tabular-nums">{quota}</div>}
            {data?.mirror.whitelist?.length ? (
              <div className="text-[10px] opacity-80">账户白名单：{data.mirror.whitelist.join('、')}</div>
            ) : null}
          </div>
        )}

        {err && (
          <div className="rounded-xl border border-rose-200 bg-rose-50 p-2.5 text-rose-700">{err}</div>
        )}

        {loading && !data && (
          <div className="flex items-center justify-center gap-2 py-8 text-slate-400">
            <Spin size="small" /> 正在逐笔预检（价格/仓位/名单/新闻/风控/配额）…
          </div>
        )}

        {data && (
          <>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
              <span>信号日 <b className="font-mono">{data.meta?.signal_date ?? '—'}</b></span>
              <span>可用资金 <b className="font-mono tabular-nums">{fmtMoney(Number(data.meta?.available_cash ?? 0))}</b></span>
              <span className="text-slate-400">数量由「可用资金 × 仓位信号 ÷ 现价」整手归一；可逐笔改</span>
            </div>

            {/* 批次缩量：不缩就不显示（平时挂一句「未超资金」只会稀释真正的警告） */}
            {budgetText && (
              <div className="rounded-xl border border-amber-200 bg-amber-50 px-2.5 py-2 text-[11px] text-amber-800 flex items-start gap-1.5">
                <AlertTriangle className="w-3.5 h-3.5 mt-px shrink-0" />
                <span>
                  {budgetText}
                  <span className="opacity-80">（手填数量不参与缩量）</span>
                </span>
              </div>
            )}

            {/* 逐笔表：一行 = 服务端算出来的这一笔 */}
            <div className="rounded-xl border border-slate-200 overflow-hidden">
              <div className={`${LEG_GRID} bg-slate-50 px-2 py-1.5 text-[10px] font-bold text-slate-500`}>
                <span className="text-center">选</span>
                <span>股票</span>
                <span className="text-right">现价</span>
                <span className="text-right">数量</span>
                <span className="text-right">金额</span>
                <span>名单 / 新闻</span>
                <span className="text-center">风控</span>
                {/* 有实盘直发腿时改口径：那一列不再全是「镜像」（直发的真单没有模拟腿） */}
                <span className="text-center">
                  {legs.some(isRealDirect) ? '真单路径' : real ? '真单镜像' : '通道'}
                </span>
              </div>
              {legs.map(leg => {
                const blocked = !leg.executable;
                const checked = !deselectSet.has(leg.symbol) && !blocked;
                const qty = effectiveQuantity(leg, edits);
                const qtyErr = invalidSymbols.has(leg.symbol) ? parseQuantity(qtyText[leg.symbol]).error : '';
                const verdict = riskVerdictView(leg);
                return (
                  <div
                    key={leg.symbol}
                    className={`${LEG_GRID} px-2 py-1.5 border-t border-slate-100 ${blocked ? 'bg-rose-50/40' : 'hover:bg-slate-50/60'}`}
                  >
                    <span className="flex justify-center">
                      <Checkbox
                        checked={checked}
                        disabled={blocked}
                        onChange={() => toggleLeg(leg.symbol)}
                        aria-label={`${leg.symbol} 是否推送`}
                      />
                    </span>
                    <span className="flex flex-col min-w-0">
                      <span className="flex items-center gap-1 min-w-0">
                        <span className="font-bold text-slate-700 truncate">{leg.name || leg.symbol}</span>
                        <span className="text-[9px] font-mono text-slate-400 shrink-0">{leg.symbol}</span>
                        {leg.source === 'manual' && <span className="text-[9px] rounded bg-blue-50 text-blue-600 px-1 shrink-0">手填</span>}
                      </span>
                      <span className="text-[9px] text-slate-400 truncate" title={leg.note}>
                        {blocked ? '' : qtyText[leg.symbol] !== undefined ? (
                          <>
                            原自动 <span className="font-mono">{leg.quantity}</span>
                            <button
                              type="button"
                              onClick={() => {
                                const next = { ...qtyText };
                                delete next[leg.symbol];
                                setQtyText(next);
                              }}
                              className="ml-1 text-slate-400 underline decoration-dotted hover:text-slate-600"
                            >
                              恢复
                            </button>
                          </>
                        ) : (
                          leg.note || `仓位信号 ${leg.position_score == null ? '—' : `${Math.round(leg.position_score * 100)}%`}`
                        )}
                      </span>
                    </span>
                    <span className="text-right font-mono tabular-nums text-slate-600">{leg.price != null ? leg.price.toFixed(2) : '--'}</span>
                    <span className="flex justify-end">
                      <input
                        type="number"
                        min={1}
                        step={100}
                        value={qtyText[leg.symbol] ?? String(qty)}
                        disabled={blocked}
                        onChange={(e) => setQtyText({ ...qtyText, [leg.symbol]: e.target.value })}
                        aria-label={`${leg.symbol} 数量`}
                        className={`w-[86px] px-1.5 py-0.5 text-right text-xs font-mono tabular-nums border rounded-lg outline-none focus:ring-1 disabled:bg-slate-100 disabled:text-slate-400 ${
                          qtyErr ? 'border-rose-300 bg-rose-50 focus:ring-rose-400' : 'border-slate-200 focus:ring-blue-400'
                        }`}
                      />
                    </span>
                    <span className="text-right font-mono tabular-nums text-slate-700">{blocked ? '—' : fmtMoney(legAmount(leg, edits))}</span>
                    <span className="flex flex-col gap-0.5 min-w-0">
                      <RiskChips risk={leg.risk} />
                      {blocked && (
                        <span className="text-[10px] text-rose-600 leading-snug">
                          <b className="mr-0.5">{blockedTag(leg)}</b>{blockedReason(leg)}
                        </span>
                      )}
                      {qtyErr && <span className="text-[10px] text-rose-600">{qtyErr}</span>}
                    </span>
                    <span className="flex justify-center">
                      <Tooltip title={<span className="whitespace-pre-line text-[11px]">{verdict.title || '未判定'}</span>}>
                        <span className={`text-[9px] rounded px-1 py-0.5 border shrink-0 ${verdict.cls}`}>{verdict.txt}</span>
                      </Tooltip>
                    </span>
                    <span className="text-center text-[10px] leading-snug">
                      {isRealDirect(leg) ? (
                        <span className={leg.mirror_precheck?.will_skip ? 'text-amber-700 font-bold' : 'text-rose-600 font-bold'}>
                          {realDirectText(leg)}
                        </span>
                      ) : real ? (
                        <span className={leg.mirror_precheck?.will_skip ? 'text-amber-700 font-bold' : 'text-slate-500'}>
                          {mirrorPrecheckText(leg)}
                        </span>
                      ) : (
                        <span className="text-slate-400">模拟盘</span>
                      )}
                    </span>
                  </div>
                );
              })}
            </div>

            {/* 名单/新闻命中：默认阻断；显式勾选才放行（批量语义，与后端 ack_risk 同口径） */}
            {legs.some(l => l.blocked_by === 'list') && (
              <label className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 p-2.5 text-amber-800 cursor-pointer">
                <Checkbox checked={ackRisk} onChange={(e) => setAckRisk(e.target.checked)} />
                <span>
                  我已知悉并接受这些票的**名单/新闻命中**，仍要推送
                  <span className="block text-[10px] opacity-80">勾选后重新预检；风控裁定（时段/资金/整手…）不受此开关影响，仍会拦单。</span>
                </span>
              </label>
            )}

            {/* 汇总：只数会发出去的那几笔 */}
            <div className="rounded-xl bg-slate-50 border border-slate-100 p-2.5 text-slate-600">
              本次将执行 <b className="font-mono tabular-nums text-slate-800">{summary.willRun}</b> 笔
              {summary.deselected > 0 && <span className="text-amber-700">（勾除 {summary.deselected} 笔）</span>}
              {summary.blocked > 0 && <span className="text-rose-600">（阻断 {summary.blocked} 笔）</span>}
              {summary.mirrorSkips > 0 && <span className="text-amber-700">（其中 {summary.mirrorSkips} 笔不会下发真单）</span>}
              <span className="mx-1">·</span>预计金额 <b className="font-mono tabular-nums text-slate-800">{fmtMoney(summary.estAmount)}</b>
              <div className="mt-1 text-[10px] text-slate-400">
                预检之后改数量或勾除，提交时服务端会按最终数量重新校验（可能被风控拦下，回执逐笔如实给出）。
              </div>
            </div>

            {/* 实盘硬确认 */}
            {real && (
              <div className="rounded-xl border border-rose-200 bg-rose-50/60 p-2.5 space-y-1.5">
                <div className="font-bold text-rose-700">实盘通道：确认后将产生真实委托</div>
                <Input
                  size="small"
                  value={realAckText}
                  onChange={(e) => setRealAckText(e.target.value)}
                  placeholder={`输入「${REAL_CONFIRM_WORD}」以启用推送`}
                  status={realAckText && realAckText.trim() !== REAL_CONFIRM_WORD ? 'error' : undefined}
                />
              </div>
            )}

            {!gate.ok && !result && (
              <div className="rounded-xl border border-slate-200 bg-slate-50 p-2 text-[11px] text-slate-500">{gate.why}</div>
            )}

            {!result && (
              <div className="flex items-center justify-end gap-2">
                <button type="button" onClick={onClose} className="rounded-xl border border-slate-200 px-3 py-1.5 text-[11px] font-bold text-slate-600 hover:bg-slate-50">
                  取消
                </button>
                <button
                  type="button"
                  disabled={!gate.ok || executing}
                  onClick={() => void submit()}
                  className="inline-flex items-center gap-1.5 rounded-xl bg-blue-600 px-4 py-1.5 text-[11px] font-bold text-white shadow-sm hover:bg-blue-700 disabled:opacity-40"
                >
                  {executing ? <Spin size="small" /> : <Send className="w-3.5 h-3.5" />}
                  {executing ? '推送中…' : `确认推送 ${summary.willRun} 笔`}
                </button>
              </div>
            )}
          </>
        )}

        {/* 逐笔回执：未提交/重复/失败各占一个词 */}
        {result && headline && (
          <>
            <div className={`rounded-xl border p-2.5 font-bold ${TONE_CLS[headline.tone]}`}>{headline.text}</div>
            <div className="rounded-xl border border-slate-200 divide-y divide-slate-100">
              {result.results.map(r => {
                const view = legResultView(r);
                const m = mirrorReceiptView(r);
                return (
                  <div key={r.symbol} className="flex items-center gap-2 px-2 py-1.5">
                    <span className="font-mono text-[11px] text-slate-600 w-[96px] shrink-0">{r.symbol}</span>
                    <span className={`text-[10px] rounded px-1.5 py-0.5 border shrink-0 ${TONE_CLS[view.tone]}`}>{view.label}</span>
                    <span className="min-w-0 flex-1 text-[11px] text-slate-500 truncate" title={view.detail}>{view.detail}</span>
                    {m && <span className={`text-[10px] rounded px-1.5 py-0.5 border shrink-0 ${TONE_CLS[m.tone]}`}>{m.label}</span>}
                    {r.mirror?.reason && <span className="text-[10px] text-slate-400 shrink-0">{r.mirror.reason}</span>}
                  </div>
                );
              })}
              {result.results.length === 0 && <div className="px-2 py-3 text-center text-[11px] text-slate-400">没有需要执行的腿</div>}
            </div>
            <div className="flex justify-end">
              <button type="button" onClick={onClose} className="rounded-xl bg-slate-800 px-4 py-1.5 text-[11px] font-bold text-white hover:bg-slate-700">
                完成
              </button>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}

/** 面板最多允许勾选的只数（与后端单批上限同口径，供侧栏提示） */
export const PUSH_MAX_PICK = MAX_PICK;
