/**
 * 手动任务第 4/5 步共用的右侧栏：风控裁定 → 资金台账 → 操作。
 *
 * 改版前这块是「执行摘要」（只列金额，风控藏去第 5 步）+「执行确认」（再列一遍金额），
 * 两步各写一套、信息还不一样；预案算完后主按钮变成不可点的「预案计算完成」，
 * 用户得回到顶部找「下一步」。现在两步共用一条栏，主操作永远唯一且可点：
 * 算预案 → 进入确认 → 推送执行，风控裁定始终在最上面。
 */

import React from 'react';
import { Activity, AlertTriangle, CheckCircle2, Loader2, Play, RotateCw, ShieldAlert, ShieldCheck, Zap } from 'lucide-react';
import type { ManualExecutionPreview } from '../../../../services/realTradingService';
import {
    cashVerdict,
    formatMoney,
    railPrimaryAction,
    summarizeSkipped,
    type RailStep,
    type TradeSide,
} from './manualTaskModel';

/** 栏内最多列几类拦截原因，其余提示去左侧清单看 */
const RAIL_RISK_GROUP_LIMIT = 4;

const FUND_TONE: Record<TradeSide, string> = {
    buy: 'text-rose-600',
    sell: 'text-emerald-600',
};

const CASH_TONE: Record<string, string> = {
    ok: 'text-gray-400',
    tight: 'text-amber-600',
    short: 'text-rose-600',
};

interface ManualTaskRailProps {
    step: RailStep;
    preview: ManualExecutionPreview | null;
    previewLoading: boolean;
    submitting: boolean;
    isRealMode: boolean;
    taskId: string;
    taskCompleted: boolean;
    onGenerate: () => void;
    onAdvance: () => void;
    onSubmit: () => void;
    onReview: () => void;
    onViewResult: () => void;
}

export const ManualTaskRail: React.FC<ManualTaskRailProps> = ({
    step,
    preview,
    previewLoading,
    submitting,
    isRealMode,
    taskId,
    taskCompleted,
    onGenerate,
    onAdvance,
    onSubmit,
    onReview,
    onViewResult,
}) => {
    const summary = preview?.summary;
    const risk = summarizeSkipped(preview?.skipped_items);
    const cash = cashVerdict(summary?.estimated_remaining_cash, summary?.estimated_buy_amount);
    const primary = railPrimaryAction(step, {
        hasPreview: !!preview,
        previewLoading,
        submitting,
        hasTask: !!taskId,
    });

    const orderCount = (summary?.buy_order_count || 0) + (summary?.sell_order_count || 0);
    const hasRisk = risk.total > 0;
    const busy = previewLoading || submitting;

    const handlePrimary = () => {
        if (!primary) return;
        if (primary.kind === 'generate') onGenerate();
        else if (primary.kind === 'advance') onAdvance();
        else onSubmit();
    };

    return (
        <div className="sticky top-4 overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-sm">
            {/* 通道带：这一步到底往哪个账户发单，必须在按钮正上方说清楚 */}
            <div
                className={`flex items-center justify-between px-4 py-2 text-[10px] font-black uppercase tracking-widest text-white ${
                    isRealMode ? 'bg-rose-600' : 'bg-sky-600'
                }`}
            >
                <span className="flex items-center gap-1.5">
                    {isRealMode ? <AlertTriangle size={12} /> : <Activity size={12} />}
                    {isRealMode ? '实盘通道 · REAL' : '模拟通道 · SIMULATION'}
                </span>
                <span className="opacity-80">{isRealMode ? '真实资金' : '虚拟资金'}</span>
            </div>

            <div className="space-y-4 p-4">
                <div className="flex items-center gap-2">
                    <ShieldAlert size={15} className="text-gray-400" />
                    <h3 className="text-sm font-bold text-gray-900">风控 · 操作</h3>
                </div>

                {/* 风控裁定：这一栏存在的理由 */}
                <section
                    className={`rounded-xl border p-3.5 ${
                        !preview
                            ? 'border-gray-100 bg-gray-50/60'
                            : hasRisk
                              ? 'border-amber-200 bg-amber-50/50'
                              : 'border-emerald-100 bg-emerald-50/50'
                    }`}
                >
                    <div
                        className={`flex items-center gap-1.5 text-[10px] font-black uppercase tracking-widest ${
                            !preview ? 'text-gray-400' : hasRisk ? 'text-amber-700' : 'text-emerald-700'
                        }`}
                    >
                        {preview && !hasRisk ? <ShieldCheck size={13} /> : <ShieldAlert size={13} />}
                        风控裁定
                    </div>

                    {preview ? (
                        <>
                            <div className="mt-1.5 flex items-baseline gap-1.5">
                                <span
                                    className={`font-mono text-2xl font-black ${hasRisk ? 'text-amber-700' : 'text-emerald-700'}`}
                                >
                                    {risk.total}
                                </span>
                                <span className="text-[11px] font-bold text-gray-500">
                                    {hasRisk ? '笔被拦截 / 过滤' : '笔拦截，全部放行'}
                                </span>
                            </div>

                            {risk.groups.slice(0, RAIL_RISK_GROUP_LIMIT).map((group) => (
                                <div key={`${group.reason}|${group.action}`} className="mt-2 flex items-center gap-2 text-[11px]">
                                    <span
                                        className={`shrink-0 rounded px-1 py-px text-[9px] font-black ${
                                            group.action === 'SELL'
                                                ? 'bg-emerald-100 text-emerald-700'
                                                : group.action === 'BUY'
                                                  ? 'bg-rose-100 text-rose-700'
                                                  : 'bg-gray-200 text-gray-600'
                                        }`}
                                    >
                                        {group.action === 'SELL' ? '卖' : group.action === 'BUY' ? '买' : '过滤'}
                                    </span>
                                    <span className="min-w-0 flex-1 truncate text-gray-600" title={group.reason}>
                                        {group.reason}
                                    </span>
                                    <span className="shrink-0 font-mono font-bold text-gray-900">{group.count}</span>
                                </div>
                            ))}

                            {risk.groups.length > RAIL_RISK_GROUP_LIMIT && (
                                <div className="mt-1.5 text-[10px] text-gray-400">
                                    其余 {risk.groups.length - RAIL_RISK_GROUP_LIMIT} 类原因见左侧「风控 / 过滤」清单
                                </div>
                            )}
                            {hasRisk && (
                                <div className="mt-2 border-t border-amber-100 pt-2 text-[10px] leading-relaxed text-amber-700/80">
                                    被拦截的标的不会出现在委托里；如需放行，请调整策略参数或持仓后重新计算。
                                </div>
                            )}
                        </>
                    ) : (
                        <div className="mt-1.5 text-[11px] font-medium text-gray-400">
                            生成调仓预案后，这里给出拦截统计与原因分类。
                        </div>
                    )}
                </section>

                {/* 资金台账 */}
                <section className="divide-y divide-gray-100 text-[11px]">
                    <div className="flex items-center justify-between py-1.5">
                        <span className="text-gray-400">卖出回款</span>
                        <span className={`font-mono font-bold ${FUND_TONE.sell}`}>
                            {formatMoney(summary?.estimated_sell_proceeds)}
                        </span>
                    </div>
                    <div className="flex items-center justify-between py-1.5">
                        <span className="text-gray-400">买入支出</span>
                        <span className={`font-mono font-bold ${FUND_TONE.buy}`}>
                            {formatMoney(summary?.estimated_buy_amount)}
                        </span>
                    </div>
                    <div className="flex items-center justify-between py-1.5">
                        <span className="text-gray-400">预估剩余</span>
                        <span className="flex items-center gap-2">
                            <span className={`font-mono font-bold ${CASH_TONE[cash.tone]}`}>
                                {formatMoney(summary?.estimated_remaining_cash)}
                            </span>
                            <span className={`rounded px-1.5 py-px text-[9px] font-bold ${CASH_TONE[cash.tone]}`}>
                                {cash.label}
                            </span>
                        </span>
                    </div>
                    {preview && <div className="pt-1.5 text-[10px] leading-relaxed text-gray-400">{cash.hint}</div>}
                </section>

                {/* 计数条 */}
                <div className="grid grid-cols-3 divide-x divide-gray-100 rounded-xl border border-gray-100 bg-gray-50/60 text-center">
                    <div className="px-1 py-2">
                        <div className="text-[9px] font-bold uppercase tracking-widest text-gray-400">信号</div>
                        <div className="mt-0.5 font-mono text-[12px] font-bold text-gray-900">
                            {summary?.signal_count ?? 0}
                        </div>
                    </div>
                    <div className="px-1 py-2">
                        <div className="text-[9px] font-bold uppercase tracking-widest text-gray-400">委托</div>
                        <div className="mt-0.5 font-mono text-[12px] font-bold text-gray-900">{orderCount}</div>
                    </div>
                    <div className="px-1 py-2">
                        <div className="text-[9px] font-bold uppercase tracking-widest text-gray-400">指纹</div>
                        <div className="mt-0.5 truncate font-mono text-[10px] font-bold text-gray-500" title={preview?.preview_hash}>
                            {preview ? preview.preview_hash.slice(0, 10) : '—'}
                        </div>
                    </div>
                </div>

                {/* 操作区 */}
                <div className="space-y-2 pt-0.5">
                    {primary ? (
                        <button
                            type="button"
                            onClick={handlePrimary}
                            disabled={primary.disabled}
                            className={`flex w-full items-center justify-center gap-2 rounded-xl py-3.5 text-[13px] font-black transition-all active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 ${
                                primary.kind === 'submit'
                                    ? 'bg-red-600 text-white shadow-lg shadow-red-100 hover:bg-red-700'
                                    : 'bg-blue-600 text-white shadow-lg shadow-blue-100 hover:bg-blue-700'
                            }`}
                        >
                            {primary.kind === 'submit' && submitting ? (
                                <Loader2 size={16} className="animate-spin" />
                            ) : primary.kind === 'generate' && previewLoading ? (
                                <Loader2 size={16} className="animate-spin" />
                            ) : primary.kind === 'submit' ? (
                                <Zap size={14} fill="currentColor" />
                            ) : (
                                <Play size={14} fill="currentColor" />
                            )}
                            {primary.label}
                        </button>
                    ) : (
                        <div className="flex items-center gap-3 rounded-xl border border-emerald-100 bg-emerald-50 p-3">
                            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-white text-emerald-600 shadow-sm">
                                <CheckCircle2 size={16} />
                            </span>
                            <div className="min-w-0">
                                <div className="text-[11px] font-bold text-emerald-900">任务已进入执行队列</div>
                                <div className="truncate font-mono text-[9px] uppercase tracking-widest text-emerald-600/70">
                                    Executing · {taskId.slice(0, 12)}
                                </div>
                            </div>
                        </div>
                    )}

                    {step === 'preview' && preview && (
                        <button
                            type="button"
                            onClick={onGenerate}
                            disabled={busy}
                            className="flex w-full items-center justify-center gap-1.5 rounded-xl border border-gray-200 bg-white py-2 text-[11px] font-bold text-gray-600 transition-colors hover:bg-gray-50 disabled:opacity-40"
                        >
                            <RotateCw size={12} className={previewLoading ? 'animate-spin' : ''} />
                            重新计算预案
                        </button>
                    )}

                    {step === 'submit' && !taskId && (
                        <button
                            type="button"
                            onClick={onReview}
                            disabled={busy}
                            className="w-full py-2 text-center text-[10px] font-bold uppercase tracking-widest text-gray-400 transition-colors hover:text-gray-900 disabled:opacity-40"
                        >
                            返回核对预案
                        </button>
                    )}

                    {taskCompleted && (
                        <button
                            type="button"
                            onClick={onViewResult}
                            className="w-full rounded-xl border border-gray-200 bg-white py-2.5 text-[11px] font-bold text-gray-900 shadow-sm transition-colors hover:bg-gray-50"
                        >
                            查看成交结果
                        </button>
                    )}
                </div>

                <p className="border-t border-gray-50 pt-3 text-[9px] leading-relaxed text-gray-400">
                    <span className="font-bold text-gray-500">原子级幂等：</span>
                    提交时按预案指纹校验，重复提交不会产生重复报单。
                    {isRealMode
                        ? '本次为实盘通道，指令将报送真实账户，请确认资金与持仓已就绪。'
                        : '本次为模拟通道，不产生真实委托。'}
                </p>
            </div>
        </div>
    );
};
