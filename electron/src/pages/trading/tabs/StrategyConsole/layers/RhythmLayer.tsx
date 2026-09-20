import React from 'react';
import { Gauge, Clock, Radio } from 'lucide-react';
import type { RealTradingStatus } from '../../../../../services/realTradingService';
import { deriveFrequencyTier, describeRhythm, FREQUENCY_TIERS, INTRADAY_TIER } from '../../../utils/frequencyTier';

interface RhythmLayerProps {
    status: RealTradingStatus | null;
    loading: boolean;
}

const WEEKDAY_LABELS: Record<string, string> = {
    MON: '周一', TUE: '周二', WED: '周三', THU: '周四', FRI: '周五', SAT: '周六', SUN: '周日',
};

const SESSION_LABELS: Record<string, string> = {
    AM: '上午盘', PM: '下午盘', AFTER_HOURS: '盘后', NIGHT: '夜盘',
};

const Cell: React.FC<{ label: string; value: React.ReactNode; title?: string; hint?: string }> = ({
    label, value, title, hint,
}) => (
    <div className="rounded-xl bg-slate-50/70 p-2.5 border border-slate-100/50 min-w-0">
        <div className="text-[11px] font-bold text-slate-500 mb-0.5" title={hint}>{label}</div>
        <div className="font-bold text-slate-800 text-xs truncate" title={title}>{value}</div>
    </div>
);

/**
 * L3 节奏层（T-RC-18）：把 `rebalance_days` 这类配置翻译成操盘手语言。
 *
 * 本层只描述**配置里确实写了**的东西——缺失项显示「-」而不是补一个后端默认值，
 * 否则界面会把系统默认值显示成「用户的选择」，用户按假象做判断。
 */
const RhythmLayer: React.FC<RhythmLayerProps> = ({ status, loading }) => {
    const live = status?.live_trade_config;
    const tier = deriveFrequencyTier(live);
    const rhythm = describeRhythm(live);
    const window_ = status?.signal_source_status;
    const cycle = status?.latest_cycle;

    const weekly = live?.schedule_type === 'weekly';
    const weekdayText = weekly && Array.isArray(live?.trade_weekdays) && live!.trade_weekdays!.length > 0
        ? live!.trade_weekdays!.map((d) => WEEKDAY_LABELS[String(d).toUpperCase()] ?? d).join(' / ')
        : '';
    const sessions = Array.isArray(live?.enabled_sessions) && live!.enabled_sessions!.length > 0
        ? live!.enabled_sessions!.map((s) => SESSION_LABELS[String(s).toUpperCase()] ?? s).join(' + ')
        : '';

    const tierTone = tier.supported
        ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
        : 'bg-slate-100 text-slate-500 border-slate-200';

    return (
        <section className="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-4">
            <div className="flex items-center gap-2 mb-3">
                <span className="text-[10px] font-black px-1.5 py-0.5 rounded bg-violet-50 text-violet-500 tracking-widest">RHYTHM</span>
                <h3 className="font-bold text-slate-800 text-sm">调仓节奏与执行响应</h3>
                <span className={`ml-auto px-2.5 py-0.5 rounded-full text-[11px] font-black border ${tierTone}`}>
                    {tier.label}
                </span>
            </div>

            {/* 频率档位带：把「未开放」的档位也列出来，但视觉上明确不可选 */}
            <div className="flex flex-wrap items-center gap-1.5 mb-3">
                {FREQUENCY_TIERS.map((t) => (
                    <span
                        key={t.key}
                        title={`${t.detail} · ${t.note}`}
                        className={`px-2 py-0.5 rounded-lg text-[10px] font-bold border ${
                            t.key === tier.key
                                ? 'bg-violet-50 text-violet-700 border-violet-300'
                                : 'bg-white text-slate-400 border-slate-200'
                        }`}
                    >
                        {t.label} · {t.detail}
                    </span>
                ))}
                <span
                    title={INTRADAY_TIER.note}
                    className="px-2 py-0.5 rounded-lg text-[10px] font-bold border border-dashed border-slate-300 text-slate-400 line-through"
                >
                    {INTRADAY_TIER.label} · 未开放
                </span>
            </div>

            {!live ? (
                <div className="border border-dashed border-slate-200 rounded-xl py-8 text-center text-xs text-slate-400">
                    {loading ? '加载中…' : '未启动策略：启动后可在此查看调仓节奏与执行响应'}
                </div>
            ) : (
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
                    <Cell
                        label="调仓周期"
                        value={weekly ? (weekdayText || '每周执行') : (live.rebalance_days ? `每 ${live.rebalance_days} 个交易日` : '-')}
                        title={weekly ? `每周 ${weekdayText || '（未指定周内日）'}` : `每 ${live.rebalance_days ?? '-'} 个交易日`}
                        hint="策略代码里的节奏优先，没写才用启动器选择"
                    />
                    <Cell
                        label="执行时段"
                        value={sessions || '-'}
                        title={sessions}
                        hint="启用的交易时段；多时段即一天跑多轮"
                    />
                    <Cell
                        label="买卖时点"
                        value={live.sell_time && live.buy_time ? `${live.sell_time} / ${live.buy_time}` : '-'}
                        title={`卖 ${live.sell_time || '-'} / 买 ${live.buy_time || '-'}`}
                        hint="先卖后买可释放资金，避免买入时余额不足"
                    />
                    <Cell
                        label="单轮最大委托"
                        value={typeof live.max_orders_per_cycle === 'number' ? `${live.max_orders_per_cycle} 笔` : '-'}
                        hint="单轮最多下多少笔，防止一次性冲击市场"
                    />
                    <Cell
                        label="委托方式"
                        value={live.order_type ? (live.order_type === 'MARKET' ? '市价' : '限价') : '-'}
                        title={live.order_type === 'LIMIT' && typeof live.max_price_deviation === 'number'
                            ? `限价 · 最大偏离 ${(live.max_price_deviation * 100).toFixed(1)}%`
                            : undefined}
                    />
                    <Cell label="节奏解读" value={rhythm || '-'} title={rhythm} />
                    <Cell
                        label="信号窗口"
                        value={window_?.available && window_.execution_window_start
                            ? `${window_.execution_window_start} ~ ${window_.execution_window_end || '-'}`
                            : '无可用窗口'}
                        title={window_?.message}
                        hint="本批信号可执行的时间范围，过期需等新一批推理"
                    />
                    <Cell
                        label="最近周期"
                        value={cycle?.at ? new Date(cycle.at).toLocaleString() : '尚无记录'}
                        title={cycle?.last_line}
                        hint="托管调度器最近一次实际执行的时间"
                    />
                </div>
            )}

            {/* 响应状态条：信号源 + 时延口径（如实标注来源，不编造一个没测的时延） */}
            <div className="mt-2.5 flex flex-wrap items-center gap-3 text-[11px] font-bold text-slate-500">
                <span className="flex items-center gap-1.5">
                    <Radio size={12} className={window_?.available ? 'text-emerald-500' : 'text-slate-400'} />
                    信号源：{window_?.available
                        ? ({ inference: '推理产物', fallback: '回退产物', missing: '缺失', window_pending: '窗口未到', expired: '已过期', mismatch: '不匹配' } as Record<string, string>)[String(window_?.source || '')] || window_?.source || '可用'
                        : '不可用'}
                </span>
                <span className="text-slate-200">|</span>
                <span className="flex items-center gap-1.5">
                    <Clock size={12} className="text-slate-400" />
                    轮询节拍：运行中 10s / 空闲 30s
                </span>
                <span className="text-slate-200">|</span>
                <span className="flex items-center gap-1.5" title="行情到达时延见「情报副驾驶」面板的时延卡">
                    <Gauge size={12} className="text-slate-400" />
                    下单→回报时延：见副驾驶面板
                </span>
            </div>

            {/* 风控口径分裂的告警归属 L4 风控层（那里有完整的比对面），此处不重复报 */}
        </section>
    );
};

export default RhythmLayer;
