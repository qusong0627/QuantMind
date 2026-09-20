import React, { useCallback, useEffect, useState } from 'react';
import { Lock, ShieldAlert, ShieldCheck, ShieldQuestion, TrendingDown } from 'lucide-react';
import type { RealTradingStatus } from '../../../../../services/realTradingService';

/** `/risk-status` 响应（T-RC-22）——L4 风控层的数据源。 */
interface RiskStatusPayload {
    effective_execution_config?: Record<string, unknown> | null;
    strategy_execution_config?: Record<string, unknown> | null;
    execution_config_divergence?: {
        diverged: boolean;
        message?: string;
    } | null;
    running?: boolean;
    locks?: {
        available: boolean;
        trade_date?: string;
        account_frozen?: boolean;
        symbols?: string[];
        reason?: string | null;
    } | null;
}

interface RiskLayerProps {
    /** 用于在轮询时重新拉取（随 status 更新而刷新） */
    status: RealTradingStatus | null;
    enabled: boolean;
    refreshKey?: number;
}

const pct = (value: unknown): string => {
    const n = Number(value);
    return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '未设置';
};

const Metric: React.FC<{
    label: string;
    value: string;
    hint: string;
    tone: 'ok' | 'off' | 'warn';
}> = ({ label, value, hint, tone }) => (
    <div className={`rounded-xl border p-3 ${tone === 'warn' ? 'border-amber-200 bg-amber-50/40' : 'border-slate-100 bg-slate-50/70'}`}>
        <div className="text-[11px] font-bold text-slate-500 mb-0.5" title={hint}>{label}</div>
        <div className={`text-lg font-black ${tone === 'warn' ? 'text-amber-700' : value === '未设置' ? 'text-slate-400' : 'text-slate-800'}`}>
            {value}
        </div>
    </div>
);

/**
 * L4 风控层（T-RC-22）：一屏回答「现在的止损到底是多少、有没有被锁」。
 *
 * 此前这三件事分散在运行快照、策略参数、下单逻辑里，用户无法自证哪个是真的。
 * 本层直接读 `/risk-status`（与下单侧同源），并把「口径分裂」放在最显眼处：
 * **止损看着配了却不触发**是最隐蔽的一类事故，必须主动报。
 */
const RiskLayer: React.FC<RiskLayerProps> = ({ status, enabled, refreshKey }) => {
    const [data, setData] = useState<RiskStatusPayload | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const { realTradingService } = await import('../../../../../services/realTradingService');
            const result = await realTradingService.getRiskStatus();
            setData(result);
            setError(null);
        } catch (e) {
            setError(e instanceof Error ? e.message : '风控状态读取失败');
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        if (!enabled) return;
        void load();
    }, [enabled, load, refreshKey]);

    const exec = (data?.effective_execution_config || {}) as Record<string, unknown>;
    const locks = data?.locks;
    const divergence = data?.execution_config_divergence;
    const frozen = !!locks?.account_frozen;
    const lockSymbols = Array.isArray(locks?.symbols) ? locks!.symbols! : [];

    return (
        <section className="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-4">
            <div className="flex items-center gap-2 mb-3">
                <span className="text-[10px] font-black px-1.5 py-0.5 rounded bg-rose-50 text-rose-500 tracking-widest">RISK</span>
                <h3 className="font-bold text-slate-800 text-sm">风控与风险锁</h3>
                <span
                    className={`ml-auto flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-black border ${
                        frozen
                            ? 'bg-rose-50 text-rose-700 border-rose-200'
                            : 'bg-emerald-50 text-emerald-700 border-emerald-200'
                    }`}
                >
                    {frozen ? <Lock size={11} /> : <ShieldCheck size={11} />}
                    {frozen ? '账户已冻结买入' : '风控正常'}
                </span>
            </div>

            {/* 口径分裂：最隐蔽的事故，放最上面 */}
            {divergence?.diverged && (
                <div className="mb-3 rounded-xl border border-amber-300 bg-amber-50 px-3 py-2.5 flex items-start gap-2">
                    <ShieldAlert size={15} className="text-amber-600 mt-0.5 shrink-0" />
                    <div className="text-[11px] leading-5 text-amber-800">
                        <div className="font-black">风控口径不一致 —— 止损可能看着配了却不触发</div>
                        <div className="font-medium text-amber-700 mt-0.5">
                            {divergence.message || '策略退出规则读的是策略参数，与这里显示的生效值不同。'}
                        </div>
                    </div>
                </div>
            )}

            {error ? (
                <div className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-[11px] font-bold text-rose-700">
                    {error}
                    <button type="button" onClick={() => void load()} className="ml-2 underline">重试</button>
                </div>
            ) : (
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
                    <Metric
                        label="止损线"
                        value={pct(exec.stop_loss)}
                        hint="持仓亏损达到该比例即自动卖出（隐式止损风控读运行快照）"
                        tone={exec.stop_loss === undefined || exec.stop_loss === null ? 'off' : 'ok'}
                    />
                    <Metric
                        label="大跌拦截"
                        value={pct(exec.max_buy_drop)}
                        hint="标的当日跌幅超过该值时不下买单，避免接飞刀"
                        tone={exec.max_buy_drop === undefined || exec.max_buy_drop === null ? 'off' : 'ok'}
                    />
                    <Metric
                        label="未平仓笔数上限"
                        value={Number.isFinite(Number(exec.max_positions)) && Number(exec.max_positions) > 0
                            ? String(exec.max_positions)
                            : '不限制'}
                        hint="同时持有的标的上限"
                        tone="ok"
                    />
                    <Metric
                        label="当日风险锁"
                        value={locks?.available === false ? '读不到' : (lockSymbols.length > 0 ? `${lockSymbols.length} 只` : '无锁')}
                        hint={locks?.available === false ? (locks?.reason || '风险锁读取失败') : '被锁标的当日不再买入（风控触发后自动写入）'}
                        tone={locks?.available === false ? 'warn' : 'ok'}
                    />
                </div>
            )}

            {/* 锁定明细：读不到与确实没锁必须分辨 */}
            {locks?.available === false && (
                <div className="mt-2.5 flex items-center gap-2 text-[11px] font-bold text-amber-700">
                    <ShieldQuestion size={12} />
                    风险锁状态读取失败（{locks.reason || '未知原因'}）——<span className="font-black">这不等于「没有锁」</span>，请勿据此判断可买额度
                </div>
            )}
            {lockSymbols.length > 0 && (
                <div className="mt-2.5 rounded-xl border border-rose-100 bg-rose-50/50 px-3 py-2">
                    <div className="text-[11px] font-black text-rose-700 mb-1 flex items-center gap-1.5">
                        <TrendingDown size={12} />
                        当日锁定标的（{locks?.trade_date || '-'}）
                    </div>
                    <div className="flex flex-wrap gap-1">
                        {lockSymbols.map((s) => (
                            <span key={s} className="px-1.5 py-0.5 rounded bg-white border border-rose-200 text-[10px] font-mono font-bold text-rose-700">
                                {s}
                            </span>
                        ))}
                    </div>
                </div>
            )}

            <div className="mt-2.5 text-[11px] font-bold text-slate-400">
                {loading && !data
                    ? '读取中…'
                    : data?.running
                        ? '生效值来源：运行快照（托管调度器每周期重读）'
                        : '当前无运行策略：显示的是系统默认风控口径'}
                {status?.execution_config_divergence === null && ' · 无法比对策略参数（未声明）'}
            </div>
        </section>
    );
};

export default RiskLayer;
