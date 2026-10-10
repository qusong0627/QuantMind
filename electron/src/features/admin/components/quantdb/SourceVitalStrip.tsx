/**
 * 选中来源库的「数据与质量快照」态势条（2026-10-10 机构级重排）。
 *
 * 左半：数据分区（文件数 / 覆盖区间 / 已发现字段 / 就绪态与未就绪原因）；
 * 右半：因子报告快照态势（快照日期 / 评估期 / 指标匹配 / 私域兜底 / 过期提示），
 * 来自 /fields 的 `stats_meta`——口径字符串全部以后端 training_stats.py 为准，
 * 前端只做排版，不改写。
 *
 * 纪律：一切数值缺失渲染「—」，绝不显示 0；非 A 股市场后端会给出 reason
 *（「因子报告仅覆盖 A 股（CN）市场…」），原样展示。
 */
import React from 'react';
import { Tag, Tooltip } from 'antd';

import type { QuantDBFactorStatsMeta } from '../../types';
import { Panel, StatusDot } from '../ui/AdminPrimitives';

export interface SourceVitalStripProps {
    /** 数据源显示名（已去「（默认）」等后缀） */
    label: string;
    /** /sources 里当前库的状态（ready/files/min_date/max_date/column_count…） */
    status: Record<string, any>;
    /** 当前表格里已发现的字段数（/fields 实测） */
    fieldCount: number;
    /** /fields 的 stats_meta（后端聚合元信息；旧响应可能缺失 → null） */
    statsMeta: QuantDBFactorStatsMeta | null;
}

const MISSING = '—';

const day = (value: unknown): string =>
    typeof value === 'string' && value ? value : MISSING;

/** 快照窗口一行：start ~ end · n 交易日 · horizon。 */
const WindowText: React.FC<{ window: { n_dates: number | null; start: string | null; end: string | null; horizon?: string | null } | null }> = ({ window }) => {
    if (!window) return <span className="text-slate-400">{MISSING}</span>;
    return (
        <>
            <span className="admin-num">{day(window.start)} ~ {day(window.end)}</span>
            {window.n_dates != null && (
                <> · <span className="admin-num">{window.n_dates.toLocaleString('en-US')}</span> 交易日</>
            )}
            {window.horizon && <> · <span className="admin-num">{window.horizon}</span></>}
        </>
    );
};

const StatsSection: React.FC<{ statsMeta: QuantDBFactorStatsMeta | null }> = ({ statsMeta }) => {
    if (!statsMeta) {
        return <span className="text-slate-400">质量统计：未获取</span>;
    }
    if (!statsMeta.available) {
        return (
            <span className="flex items-center gap-1.5">
                <StatusDot tone="idle" />
                <span className="text-slate-500">{statsMeta.reason || '质量统计不可用'}</span>
                {statsMeta.rebuild_hint && (
                    <span className="text-amber-700">（{statsMeta.rebuild_hint}）</span>
                )}
            </span>
        );
    }
    return (
        <>
            <span className="flex items-center gap-1.5">
                <StatusDot tone={statsMeta.stale ? 'warn' : 'info'} />
                <span>质量快照 <span className="admin-num text-slate-800">{day(statsMeta.report_date)}</span></span>
            </span>
            <span>评估期 <WindowText window={statsMeta.window} /></span>
            <span>
                指标匹配 <span className="admin-num text-slate-800">{statsMeta.matched}</span>
                <span className="admin-num text-slate-400">/{statsMeta.total}</span>
            </span>
            {statsMeta.fallback_used > 0 && (
                <Tooltip title="私域研究快照按采样日评估、方向已统一为“越大越好”，IC 与日频因子报告口径不可比——对应行以「快照」徽标标注">
                    <span className="cursor-help text-amber-700">
                        <span className="admin-num">{statsMeta.fallback_used}</span> 条为私域快照
                        {statsMeta.fallback_window?.start && (
                            <>（<span className="admin-num">{statsMeta.fallback_window.start}~{statsMeta.fallback_window.end}</span>
                            {statsMeta.fallback_window.n_dates != null && <> · <span className="admin-num">{statsMeta.fallback_window.n_dates}</span> 采样日</>}）</>
                        )}
                    </span>
                </Tooltip>
            )}
            {statsMeta.stale && (
                <span className="text-amber-700">
                    <Tag color="orange" className="!mr-1">快照超过 30 天未更新</Tag>
                    {statsMeta.rebuild_hint}
                </span>
            )}
        </>
    );
};

export const SourceVitalStrip: React.FC<SourceVitalStripProps> = ({
    label,
    status,
    fieldCount,
    statsMeta,
}) => {
    const files = Number(status.files || 0);
    const ready = Boolean(status.ready);
    const hint = !status.files
        ? `尚未同步 ${label} 数据`
        : (status.missing_required || []).length > 0
          ? '数据字段尚未满足训练条件'
          : '暂未满足直读训练条件';
    const action = !status.files
        ? '请在“数据下载”中勾选并同步，完成后点击“字段发现”。'
        : (status.missing_required || []).length > 0
          ? '请补齐行情字段后重新执行“字段发现”。'
          : '请刷新字段状态后重试。';

    return (
        <Panel bodyClassName="px-4 py-2.5">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-slate-600">
                {/* 数据分区 */}
                <span className="flex items-center gap-1.5">
                    <StatusDot tone={ready ? 'ok' : files ? 'warn' : 'bad'} />
                    <span className="font-medium text-slate-800">{label}</span>
                    {ready ? (
                        <Tag color="green" className="!mr-0 !px-1 !text-[10px] !leading-4">就绪</Tag>
                    ) : (
                        <Tag className="!mr-0 !px-1 !text-[10px] !leading-4">未就绪</Tag>
                    )}
                </span>
                <span>分区文件 <span className="admin-num text-slate-800">{files.toLocaleString('en-US')}</span></span>
                <span>覆盖 <span className="admin-num">{day(status.min_date)} ~ {day(status.max_date)}</span></span>
                <span>已发现字段 <span className="admin-num text-slate-800">{fieldCount.toLocaleString('en-US')}</span></span>
                {!ready && (
                    <span className="text-amber-700">{hint} · {action}</span>
                )}

                <div className="h-4 w-px shrink-0 bg-slate-200" />

                {/* 质量快照（stats_meta） */}
                <StatsSection statsMeta={statsMeta} />
            </div>
        </Panel>
    );
};
