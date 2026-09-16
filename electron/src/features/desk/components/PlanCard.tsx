/** 调仓计划卡（FE-B/T-FE-05）：引擎 dry-run 预演——每笔带理由与触发类别，绝不执行 */

import React, { useMemo, useState } from 'react';
import { AlertTriangle, ClipboardList, HelpCircle, Play } from 'lucide-react';
import { Checkbox, Modal, message } from 'antd';
import type { PlanBlock, PlanOrder } from '../types';
import {
  buildExecuteSelection,
  excludedSymbolsFromPlan,
  formatMoney,
  hasInvalidQuantityEdit,
  isValidQuantityInput,
  planKindLabel,
  planSummary,
  quantityAdjustmentSummary,
  quantityOverridesFromPlan,
} from '../deskModel';
import { executePlan } from '../services/deskService';
import { TermTooltip } from '../../shared/TermTooltip';
import { checkDiversification } from '../../../components/shared/compliance/diversification';

interface PlanCardProps {
  plan: PlanBlock | null | undefined;
  onDrillDown?: () => void;
  /** T-FE-03 v2：计划单逐层下钻（单字段 → 触发类别/当日信号 → 原始条目） */
  onOrderDrill?: (order: PlanOrder) => void;
  /** 执行成功后回调（父组件刷新交易台） */
  onExecuted?: () => void;
}

const SideBadge: React.FC<{ side: string }> = ({ side }) => {
  const isBuy = String(side).toUpperCase() === 'BUY';
  // 涨红跌绿口径：买=红（做多），卖=绿
  return (
    <span
      className={`text-[11px] px-1.5 py-0.5 rounded border font-semibold ${
        isBuy ? 'bg-red-50 text-red-700 border-red-200' : 'bg-emerald-50 text-emerald-700 border-emerald-200'
      }`}
    >
      {isBuy ? '买' : '卖'}
    </span>
  );
};

export const PlanCard: React.FC<PlanCardProps> = ({ plan, onDrillDown, onOrderDrill, onExecuted }) => {
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [excludedIndexes, setExcludedIndexes] = useState<Set<number>>(new Set());
  // T-FE-05 v2：人工改量（下标 → 数量）；退出规则单不出现在该表（风控不绕过）
  const [quantityEdits, setQuantityEdits] = useState<Map<number, number>>(new Map());
  const [executing, setExecuting] = useState(false);
  const [execResult, setExecResult] = useState<string>('');
  const [execAdjustments, setExecAdjustments] = useState<{ applied: string[]; ignored: string[] }>({
    applied: [],
    ignored: [],
  });

  const selection = useMemo(
    () => buildExecuteSelection(plan, excludedIndexes),
    [plan, excludedIndexes]
  );

  const toggleExclude = (index: number, checked: boolean) => {
    // 传值更新（tsc 下函数式 setter 的类型坑，repo 约定）
    const next = new Set(excludedIndexes);
    if (checked) next.delete(index);
    else next.add(index);
    setExcludedIndexes(next);
  };

  const setQuantityEdit = (index: number, quantity: number) => {
    const next = new Map(quantityEdits);
    next.set(index, quantity);
    setQuantityEdits(next);
  };

  const resetQuantityEdit = (index: number) => {
    const next = new Map(quantityEdits);
    next.delete(index);
    setQuantityEdits(next);
  };

  const invalidEdit = useMemo(
    () => hasInvalidQuantityEdit(plan, quantityEdits),
    [plan, quantityEdits]
  );
  const changedCount = useMemo(
    () => quantityOverridesFromPlan(plan, quantityEdits).length,
    [plan, quantityEdits]
  );

  const handleExecute = async () => {
    setExecuting(true);
    setExecResult('');
    setExecAdjustments({ applied: [], ignored: [] });
    try {
      const resp = await executePlan(
        excludedSymbolsFromPlan(plan, excludedIndexes),
        quantityOverridesFromPlan(plan, quantityEdits)
      );
      const report = resp?.data?.report || {};
      const filled = Number((report as Record<string, unknown>).filled_count ?? 0);
      const rejected = Number((report as Record<string, unknown>).rejected_count ?? 0);
      const orders = Number((report as Record<string, unknown>).order_count ?? 0);
      setExecResult(`执行完成：委托 ${orders} 笔，成交 ${filled}，拒单 ${rejected}`);
      setExecAdjustments(quantityAdjustmentSummary(report));
      message.success('调仓执行完成');
      onExecuted?.();
    } catch (err: unknown) {
      const text = err instanceof Error ? err.message : '执行失败';
      setExecResult(text);
      message.error(text.slice(0, 80));
    } finally {
      setExecuting(false);
    }
  };
  const summary = planSummary(plan);
  const orders = plan?.orders || [];
  // T-FE-18：强制分散提示（按本次计划买入金额估算，非全账户口径）
  const diversification = useMemo(() => checkDiversification(orders), [orders]);

  return (
    <section className="bg-white rounded-2xl border border-gray-200 p-4 flex flex-col">
      <header className="flex items-center justify-between gap-2 mb-2">
        <div className="flex items-center gap-2">
          <ClipboardList className="w-4 h-4 text-blue-600" />
          <h3 className="text-sm font-semibold text-slate-800">调仓计划（预演）</h3>
          {plan?.available && plan?.dry_run && (
            <TermTooltip term="dry_run" className="!border-0">
              <span className="text-[11px] px-1.5 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-200">
                未执行
              </span>
            </TermTooltip>
          )}
        </div>
        {plan?.strategy_name && (
          <span className="text-[11px] text-slate-500 truncate max-w-[160px]">
            {plan.strategy_name}（{plan.mode || 'SIMULATION'}）
          </span>
        )}
        {plan?.available && orders.length > 0 && (
          <button
            type="button"
            onClick={() => {
              setExcludedIndexes(new Set());
              setQuantityEdits(new Map());
              setExecResult('');
              setExecAdjustments({ applied: [], ignored: [] });
              setConfirmOpen(true);
            }}
            className="inline-flex items-center gap-1 rounded-xl bg-blue-600 px-3 py-1.5 text-[11px] font-bold text-white hover:bg-blue-500 shrink-0"
          >
            <Play className="w-3 h-3" />
            一键执行
          </button>
        )}
      </header>

      {!plan?.available ? (
        <div className="flex-1 flex flex-col items-center justify-center text-center py-6">
          <HelpCircle className="w-6 h-6 text-slate-300 mb-1.5" />
          <p className="text-xs text-slate-500">{plan?.reason || '计划预演不可用'}</p>
        </div>
      ) : orders.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center text-center py-6">
          <p className="text-xs text-slate-500">
            {plan?.error ? `预演未产出计划：${plan.error}` : '今日无需调仓（已符合目标权重）'}
          </p>
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-3 text-[11px] text-slate-600 mb-2">
            <span>
              目标调仓 <b className="text-slate-800">{orders.length}</b> 笔
              （买 {summary.buys.length} / 卖 {summary.sells.length}
              {summary.exits > 0 ? (
                <>
                  {' '}
                  · 含<TermTooltip term="exit_rule">退出规则</TermTooltip> {summary.exits}
                </>
              ) : (
                ''
              )}）
            </span>
            <span className="text-red-600">买入约 {formatMoney(summary.buyAmount)}</span>
            <span className="text-emerald-600">卖出约 {formatMoney(summary.sellAmount)}</span>
          </div>

          {diversification.warnings.length > 0 ? (
            <div className="mb-2 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1.5 space-y-0.5">
              {diversification.warnings.map((w, i) => (
                <div key={i} className="text-[11px] text-amber-800 flex items-center gap-1">
                  <AlertTriangle className="w-3 h-3 shrink-0" />
                  分散提示：{w}
                </div>
              ))}
            </div>
          ) : diversification.maxWeight !== null ? (
            <div className="mb-2 text-[11px] text-emerald-700">
              分散检查 ✓ 单票最大 {(diversification.maxWeight * 100).toFixed(1)}% · {diversification.buyCount} 只
            </div>
          ) : null}

          <div className="flex-1 overflow-y-auto max-h-[320px] pr-1 space-y-1.5">
            {orders.map((order, index) => (
              <div
                key={`${order.symbol}-${order.side}-${index}`}
                className={`flex items-center gap-2 text-xs border-b border-gray-100 pb-1.5 last:border-0 rounded-lg px-1 ${onOrderDrill ? 'cursor-pointer hover:bg-blue-50/40' : ''}`}
                onClick={onOrderDrill ? () => onOrderDrill(order) : undefined}
                title={onOrderDrill ? '点击下钻：计划单 → 触发类别/当日信号 → 原始条目' : undefined}
              >
                <SideBadge side={order.side} />
                <span className="font-medium text-slate-800 w-24">{order.symbol}</span>
                <span className="text-slate-600 w-16 text-right">{order.quantity}</span>
                <span className="text-slate-400 w-14 text-right">@{order.price}</span>
                <span className="text-slate-400 w-20 text-right">{formatMoney(order.estimated_amount)}</span>
                <span className="text-[11px] text-slate-500 truncate flex-1" title={order.reason}>
                  {planKindLabel(order.kind)}：{order.reason}
                </span>
                {(order.is_limit_up || order.is_limit_down || order.is_suspended) && (
                  <span className="text-[11px] text-amber-600 inline-flex items-center gap-0.5 shrink-0">
                    <AlertTriangle className="w-3 h-3" />
                    {order.is_suspended ? '停牌' : order.is_limit_up ? '涨停' : '跌停'}
                  </span>
                )}
              </div>
            ))}
          </div>
        </>
      )}

      <Modal
        open={confirmOpen}
        title="一键执行调仓（模拟盘）"
        okText="确认执行"
        cancelText="取消"
        confirmLoading={executing}
        okButtonProps={{ disabled: invalidEdit }}
        onOk={() => void handleExecute()}
        onCancel={() => setConfirmOpen(false)}
        width={620}
      >
        <div className="space-y-3 text-xs">
          <div className="rounded-xl bg-slate-50 border border-slate-100 p-3 text-slate-600 space-y-1">
            <div>· 与预演共用同一引擎（RebalanceCalculator + 撮合）；执行时行情若有变化，成交与预演可能有出入。</div>
            <div>· <b className="text-amber-700">退出规则单不可排除、不可改量</b>（止损/止盈属风控动作，人工不可绕过）。</div>
            <div>· 数量会按市场申报规则归一（整手/科创板 200 起）；同一策略 60 秒内仅允许触发一次（服务端防重）。</div>
          </div>

          <div className="max-h-[320px] overflow-y-auto space-y-1.5 pr-1">
            {selection.locked.map(({ index, order }) => (
              <div key={`locked-${index}`} className="flex items-center gap-2 text-xs">
                <Checkbox checked disabled />
                <span className="text-[10px] px-1 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200">
                  退出规则 · 不可排除/改量
                </span>
                <span className={`font-medium ${order.side === 'BUY' ? 'text-red-600' : 'text-emerald-600'}`}>
                  {order.side === 'BUY' ? '买' : '卖'} {order.symbol}
                </span>
                <span className="text-slate-500">{order.quantity} 股</span>
                <span className="text-slate-400 truncate">{order.reason}</span>
              </div>
            ))}
            {selection.selectable.map(({ index, order }) => {
              const edited = quantityEdits.get(index);
              const effectiveQty = edited !== undefined ? edited : order.quantity;
              const editedValid = !quantityEdits.has(index) || isValidQuantityInput(effectiveQty);
              const isChanged = edited !== undefined && isValidQuantityInput(effectiveQty) && Math.floor(effectiveQty) !== order.quantity;
              const excluded = excludedIndexes.has(index);
              return (
                <div
                  key={`sel-${index}`}
                  className={`flex items-center gap-2 text-xs rounded-lg px-1 py-0.5 ${excluded ? 'opacity-50' : ''}`}
                >
                  <Checkbox
                    checked={!excludedIndexes.has(index)}
                    onChange={(e) => toggleExclude(index, e.target.checked)}
                  />
                  <span className={`font-medium shrink-0 ${order.side === 'BUY' ? 'text-red-600' : 'text-emerald-600'}`}>
                    {order.side === 'BUY' ? '买' : '卖'} {order.symbol}
                  </span>
                  <input
                    type="number"
                    min={1}
                    step={100}
                    value={effectiveQty}
                    disabled={excluded}
                    onChange={(e) => setQuantityEdit(index, Number(e.target.value))}
                    aria-label={`${order.symbol} 数量`}
                    className={`w-[84px] shrink-0 px-1.5 py-0.5 text-xs border rounded-lg outline-none focus:ring-1 ${
                      editedValid ? 'border-gray-200 focus:ring-blue-400' : 'border-rose-300 bg-rose-50 focus:ring-rose-400'
                    }`}
                  />
                  {isChanged ? (
                    <span className="text-[10px] text-blue-600 shrink-0 inline-flex items-center gap-1">
                      原 {order.quantity}
                      <button
                        type="button"
                        onClick={() => resetQuantityEdit(index)}
                        className="text-slate-400 hover:text-slate-600 underline decoration-dotted"
                        title="恢复计划数量"
                      >
                        恢复
                      </button>
                    </span>
                  ) : (
                    <span className="text-[10px] text-slate-400 shrink-0">≈ {formatMoney(order.price * order.quantity)}</span>
                  )}
                  <span className="text-slate-400 truncate">{order.reason}</span>
                  {!editedValid && <span className="text-[10px] text-rose-600 shrink-0">数量须为正整数</span>}
                </div>
              );
            })}
          </div>

          <div className="text-slate-600">
            本次将执行 <b className="text-slate-800">{selection.executableCount}</b> 笔
            {excludedIndexes.size > 0 && (
              <span className="text-amber-700">（勾除 {excludedIndexes.size} 笔）</span>
            )}
            {changedCount > 0 && <span className="text-blue-700">（改量 {changedCount} 笔）</span>}
          </div>
          {invalidEdit && (
            <div className="rounded-xl p-2.5 border bg-rose-50 border-rose-200 text-rose-700">
              存在不合法的改量输入（须为正整数）——修正后才能确认执行。
            </div>
          )}
          {execResult && (
            <div className={`rounded-xl p-3 border ${execResult.includes('完成') ? 'bg-emerald-50 border-emerald-200 text-emerald-800' : 'bg-rose-50 border-rose-200 text-rose-700'}`}>
              {execResult}
              {execAdjustments.applied.length > 0 && (
                <div className="mt-1.5 text-[11px] text-emerald-700">
                  改量应用：{execAdjustments.applied.join('；')}
                </div>
              )}
              {execAdjustments.ignored.length > 0 && (
                <div className="mt-1.5 text-[11px] text-amber-700">
                  改量未应用：{execAdjustments.ignored.join('；')}
                </div>
              )}
            </div>
          )}
        </div>
      </Modal>

      <footer className="text-[10px] text-slate-400 mt-2">
        {onDrillDown ? (
          <button
            type="button"
            onClick={onDrillDown}
            className="hover:text-blue-600 underline decoration-dotted underline-offset-2"
            title="下钻：来源链与原始载荷"
          >
            来源：{plan?.source || '—'}（点击下钻）
          </button>
        ) : (
          <>来源：{plan?.source || '—'}（与执行同一 RebalanceCalculator；执行前人工可审）</>
        )}
      </footer>
    </section>
  );
};
