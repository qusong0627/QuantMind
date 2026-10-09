/**
 * 后台机构版共享视觉原语（2026-10-09 改版）。
 *
 * 改版方向「浅色统一导航轴 + 浅色密排内容」：内容面追求信息密度与克制层次 ——
 * 小圆角、发丝分隔线、状态点语义色、等宽数字。这三个原语是三个改版页面
 *（外壳 / 系统概览 / 推理监控）共用的最小集合，避免每页各画一套。
 */
import React from 'react';

export type DotTone = 'ok' | 'warn' | 'bad' | 'idle' | 'info';

const DOT_CLASS: Record<DotTone, string> = {
    ok: 'bg-emerald-500',
    warn: 'bg-amber-500',
    bad: 'bg-rose-500',
    idle: 'bg-slate-300',
    info: 'bg-sky-500',
};

/** 状态点：语义只有颜色一个通道（文字由使用方给出），pulse 仅用于「活着的进程」。 */
export const StatusDot: React.FC<{ tone: DotTone; pulse?: boolean; className?: string }> = ({
    tone,
    pulse = false,
    className = '',
}) => (
    <span
        className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${DOT_CLASS[tone]} ${pulse ? 'animate-pulse' : ''} ${className}`}
    />
);

/** 面板：白底小圆角 + 发丝边框 + 可选标题行（标题行 40px，右侧放工具）。 */
export const Panel: React.FC<{
    title?: React.ReactNode;
    sub?: React.ReactNode;
    right?: React.ReactNode;
    className?: string;
    bodyClassName?: string;
    children: React.ReactNode;
}> = ({ title, sub, right, className = '', bodyClassName = '', children }) => (
    <section
        className={`flex min-h-0 flex-col overflow-hidden rounded-lg border border-slate-200 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)] ${className}`}
    >
        {(title !== undefined || right !== undefined) && (
            <header className="flex h-10 shrink-0 items-center justify-between gap-3 border-b border-slate-100 px-4">
                <div className="flex min-w-0 items-baseline gap-2">
                    {title !== undefined && (
                        <span className="shrink-0 text-[13px] font-semibold text-slate-800">{title}</span>
                    )}
                    {sub !== undefined && <span className="truncate text-[11px] text-slate-400">{sub}</span>}
                </div>
                {right}
            </header>
        )}
        <div className={`min-h-0 ${bodyClassName}`}>{children}</div>
    </section>
);

/**
 * 指标单元：小标签 + 大等宽数字 + 一行注脚。tone 用于语义（失败率过高=bad）。
 * 注意 admin-num 带 !important —— global.css 对 .text-xs/.text-[11px] 强制了
 * 正文字体，等宽数字必须靠这个类才盖得住（见 global.css）。
 */
export const KpiCell: React.FC<{
    label: React.ReactNode;
    value: React.ReactNode;
    sub?: React.ReactNode;
    tone?: 'default' | 'ok' | 'warn' | 'bad';
    className?: string;
}> = ({ label, value, sub, tone = 'default', className = '' }) => {
    const toneClass =
        tone === 'ok'
            ? 'text-emerald-600'
            : tone === 'warn'
              ? 'text-amber-600'
              : tone === 'bad'
                ? 'text-rose-600'
                : 'text-slate-800';
    return (
        <div className={`flex min-w-0 flex-col justify-center gap-1 px-4 py-3 ${className}`}>
            <span className="text-[11px] font-medium text-slate-400">{label}</span>
            <span className={`admin-num text-[20px] font-semibold leading-none tracking-tight ${toneClass}`}>
                {value}
            </span>
            {sub !== undefined && <span className="truncate text-[11px] text-slate-400">{sub}</span>}
        </div>
    );
};
