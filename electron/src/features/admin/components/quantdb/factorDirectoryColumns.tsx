/**
 * 训练数据集页「因子目录」列定义（2026-10-10 机构级重排）。
 *
 * 结构：标识列（编号/因子，左固定）→ 归属组 → 质量指标组 → 数据量组 →
 * 状态/训练配置（右固定）。固定列刻意放在**组外**的单列上——rc-table 的组头
 * 单元格只有在子树全部固定时才跟着固定，组里混固定/滚动列会把表头撕开。
 *
 * 数据纪律：每个统计单元格走 statFormat 的 formatter，缺失一律「—」；
 * 私域快照来源（source="research_snapshot"）的行在 IC 列带「快照」徽标——
 * 该口径是 82 采样日、方向统一，与日频报告 IC **不可比**，没有徽标就是误导。
 */
import React from 'react';
import { Button, Space, Switch, Tag, Tooltip, Typography } from 'antd';
import { EditOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';

import type { QuantDBFactorField, QuantDBFactorStat } from '../../types';
import {
    MISSING,
    fmtInt,
    fmtNum,
    fmtPct,
    fmtSigned,
    fmtWindowCoverage,
} from './statFormat';

const { Text } = Typography;

/** 草稿映射行（/catalog 的 categories[].features[] 摊平后的形状）。 */
export type TrainingMapping = {
    mapping_id: string;
    source_dataset: string;
    source_column: string;
    key: string;
    feature_name: string;
    enabled: boolean;
    default_selected: boolean;
    required: boolean;
    category_id?: string;
    category_name?: string;
    order_no?: number;
    /** 长描述：B 特征字典用户编辑优先，缺省回退代码字典精确条目 */
    explanation?: string;
};

export interface FactorDirectoryRow {
    row_no: number;
    source_column: string;
    factor: string;
    style: string;
    explanation: string;
    is_present: boolean;
    /** /fields 原始登记行（展开行展示数据类型与库级登记覆盖） */
    field: QuantDBFactorField | Record<string, any>;
    mapping?: TrainingMapping;
    /** 行级质量统计（/fields 的 stats，缺失为 null） */
    stat: QuantDBFactorStat | null;
}

/** 可开关的列（编号/因子/训练配置恒显，不在此列）。 */
export type FactorColumnKey =
    | 'sub_library'
    | 'category'
    | 'explanation'
    | 'ic'
    | 'icir'
    | 'turnover'
    | 'monotonicity'
    | 'win_rate'
    | 'n_valid'
    | 'ic_days'
    | 'coverage'
    | 'status';

/** 列设置 Popover 的分组清单（页面据此渲染 Checkbox）。 */
export const FACTOR_COLUMN_SETTINGS: ReadonlyArray<{
    group: string;
    columns: ReadonlyArray<{ key: FactorColumnKey; label: string }>;
}> = [
    {
        group: '归属',
        columns: [
            { key: 'sub_library', label: '子库' },
            { key: 'category', label: '分类' },
            { key: 'explanation', label: '中文释义' },
        ],
    },
    {
        group: '质量指标',
        columns: [
            { key: 'ic', label: 'IC' },
            { key: 'icir', label: 'ICIR' },
            { key: 'turnover', label: '换手' },
            { key: 'monotonicity', label: '单调性' },
            { key: 'win_rate', label: '胜率' },
        ],
    },
    {
        group: '数据量',
        columns: [
            { key: 'n_valid', label: '样本量' },
            { key: 'ic_days', label: '有效天数' },
            { key: 'coverage', label: '窗口覆盖' },
        ],
    },
    {
        group: '目录与训练',
        columns: [{ key: 'status', label: '状态' }],
    },
];

/** 统计数值列可排序的键（与 QuantDBFactorStat 的标量字段对齐）。 */
type StatNumericKey =
    | 'ic_mean'
    | 'icir'
    | 'turnover'
    | 'monotonicity'
    | 'win_rate'
    | 'n_valid_mean'
    | 'ic_neutral_days';

/** 缺失值渲染「—」用灰、有值用等宽数字——一眼区分「没有」与「很小」。 */
const statCell = (text: string): React.ReactNode =>
    text === MISSING ? (
        <span className="text-slate-300">{MISSING}</span>
    ) : (
        <span className="admin-num text-[12px] text-slate-700">{text}</span>
    );

const plainCell = (text: string | null | undefined, className = 'text-[11px] text-slate-500'): React.ReactNode =>
    text ? <span className={className}>{text}</span> : <span className="text-slate-300">{MISSING}</span>;

/** 带下划虚线 + Tooltip 的表头（口径说明随列走）。 */
const head = (label: React.ReactNode, tip?: string): React.ReactNode =>
    tip ? (
        <Tooltip title={tip}>
            <span className="cursor-help border-b border-dotted border-slate-300">{label}</span>
        </Tooltip>
    ) : (
        label
    );

/** 数值排序：缺失（null）永远排最后，不参与大小比较。 */
const statSorter =
    (key: StatNumericKey) =>
    (a: FactorDirectoryRow, b: FactorDirectoryRow): number => {
        const av = a.stat?.[key] ?? null;
        const bv = b.stat?.[key] ?? null;
        if (av === null && bv === null) return 0;
        if (av === null) return 1;
        if (bv === null) return -1;
        return av - bv;
    };

export interface FactorColumnsOptions {
    /** stats_meta.window.n_dates：窗口覆盖列的分母（报告评估期总天数） */
    statsNDates: number | null;
    onToggleEnabled: (row: FactorDirectoryRow, checked: boolean) => void;
    onToggleDefault: (row: FactorDirectoryRow, checked: boolean) => void;
    onEdit: (row: FactorDirectoryRow) => void;
}

export function buildFactorColumns(opts: FactorColumnsOptions): ColumnsType<FactorDirectoryRow> {
    const { statsNDates, onToggleEnabled, onToggleDefault, onEdit } = opts;

    const switchCell = (row: FactorDirectoryRow): React.ReactNode =>
        row.mapping ? (
            <Space size={8} wrap>
                <Tooltip title="启用：该因子参与训练（左开关）">
                    <Space size={2}>
                        启用
                        <Switch
                            size="small"
                            checked={row.mapping.enabled}
                            onChange={(checked) => onToggleEnabled(row, checked)}
                        />
                    </Space>
                </Tooltip>
                <Tooltip title="默认：训练时默认勾选该因子（右开关）">
                    <Space size={2}>
                        默认
                        <Switch
                            size="small"
                            checked={row.mapping.default_selected}
                            disabled={!row.mapping.enabled}
                            onChange={(checked) => onToggleDefault(row, checked)}
                        />
                    </Space>
                </Tooltip>
                <Button
                    type="text"
                    size="small"
                    icon={<EditOutlined />}
                    onClick={() => onEdit(row)}
                />
            </Space>
        ) : (
            <Tooltip title="创建或认领草稿后，在此配置启用 / 默认与分类释义">
                <Text type="secondary" className="text-xs">未纳入草稿</Text>
            </Tooltip>
        );

    const statColumn = (
        key: FactorColumnKey,
        label: React.ReactNode,
        width: number,
        sorterKey: StatNumericKey,
        format: (stat: QuantDBFactorStat) => string,
        tip: string,
    ): any => ({
        key,
        title: head(label, tip),
        width,
        align: 'right',
        sorter: statSorter(sorterKey),
        render: (_: unknown, row: FactorDirectoryRow) =>
            statCell(row.stat ? format(row.stat) : MISSING),
    });

    return [
        {
            title: '编号',
            dataIndex: 'row_no',
            key: 'row_no',
            width: 56,
            align: 'center',
            fixed: 'left',
            render: (value: number) => (
                <span className="admin-num text-[11px] text-slate-400">{value}</span>
            ),
        },
        {
            title: '因子',
            dataIndex: 'factor',
            key: 'factor',
            width: 190,
            fixed: 'left',
            render: (value: string) => <Text code>{value}</Text>,
        },
        {
            title: '归属',
            key: 'group_identity',
            children: [
                {
                    title: head('子库', '因子报告快照里的来源子库（library）'),
                    key: 'sub_library',
                    width: 96,
                    render: (_: unknown, row: FactorDirectoryRow) =>
                        plainCell(row.stat?.library ?? null),
                },
                {
                    title: '分类',
                    dataIndex: 'style',
                    key: 'category',
                    width: 112,
                    render: (value: string) => (
                        <Tag color={value === '待分类' ? 'default' : 'blue'}>{value}</Tag>
                    ),
                },
                {
                    title: '中文释义',
                    dataIndex: 'explanation',
                    key: 'explanation',
                    ellipsis: true,
                    render: (value: string) => (
                        <Tooltip title={value} placement="topLeft">
                            <Text ellipsis className="text-xs">{value}</Text>
                        </Tooltip>
                    ),
                },
            ],
        },
        {
            title: '质量指标',
            key: 'group_quality',
            children: [
                {
                    key: 'ic',
                    title: head('IC', 'Rank IC 均值（因子报告口径 fwd_ret_5）；缺失显示“—”而不是 0'),
                    width: 100,
                    align: 'right',
                    sorter: statSorter('ic_mean'),
                    render: (_: unknown, row: FactorDirectoryRow) => (
                        <span className="inline-flex items-center justify-end gap-1">
                            {statCell(row.stat ? fmtSigned(row.stat.ic_mean) : MISSING)}
                            {row.stat?.source === 'research_snapshot' && (
                                <Tooltip title="私域研究快照口径：82 采样日、方向已统一为“越大越好”，与日频因子报告 IC 不可比">
                                    <Tag color="orange" className="!mr-0 !px-1 !text-[10px] !leading-4">快照</Tag>
                                </Tooltip>
                            )}
                        </span>
                    ),
                },
                statColumn('icir', 'ICIR', 76, 'icir', (s) => fmtNum(s.icir), 'IC 均值 ÷ IC 标准差（信息比）'),
                statColumn('turnover', '换手', 80, 'turnover', (s) => fmtPct(s.turnover), '因子值日换手率（0~1）'),
                statColumn('monotonicity', '单调性', 84, 'monotonicity', (s) => fmtNum(s.monotonicity), '分位组合收益单调性（越大越单调）'),
                statColumn('win_rate', '胜率', 80, 'win_rate', (s) => fmtPct(s.win_rate), 'IC 为正的交易日占比'),
            ],
        },
        {
            title: '数据量',
            key: 'group_volume',
            children: [
                statColumn('n_valid', '样本量', 100, 'n_valid_mean', (s) => fmtInt(s.n_valid_mean), '日均非空样本数（n_valid_mean）'),
                statColumn('ic_days', '有效天数', 96, 'ic_neutral_days', (s) => fmtInt(s.ic_neutral_days), '可计算中性化 IC 的交易日数（ic_neutral_days）'),
                {
                    key: 'coverage',
                    title: head('窗口覆盖', '有效天数 ÷ 该库报告的评估期总天数（stats_meta.window.n_dates）；不是全历史覆盖率'),
                    width: 92,
                    align: 'right',
                    sorter: statSorter('ic_neutral_days'),
                    render: (_: unknown, row: FactorDirectoryRow) =>
                        statCell(fmtWindowCoverage(row.stat?.ic_neutral_days ?? null, statsNDates)),
                },
            ],
        },
        {
            title: '状态',
            key: 'status',
            width: 84,
            render: (_: unknown, row: FactorDirectoryRow) =>
                row.is_present ? <Tag color="green">已发现</Tag> : <Tag>已删除</Tag>,
        },
        {
            title: '训练配置',
            key: 'config',
            width: 230,
            fixed: 'right',
            render: (_: unknown, row: FactorDirectoryRow) => switchCell(row),
        },
    ] as ColumnsType<FactorDirectoryRow>;
}

/**
 * 按列设置过滤（组内子列全隐藏时连组头一起摘掉）。
 *
 * 纯函数：把「哪些列可见」这件事从 antd 树里剥出来，单测与页面共用一份。
 */
export function filterFactorColumns(
    columns: ColumnsType<FactorDirectoryRow>,
    hidden: ReadonlySet<string>,
): ColumnsType<FactorDirectoryRow> {
    if (hidden.size === 0) return columns;
    const kept = columns
        .map((column): ColumnsType<FactorDirectoryRow>[number] | null => {
            const anyColumn = column as any;
            if (Array.isArray(anyColumn.children)) {
                const children = filterFactorColumns(anyColumn.children, hidden);
                return children.length > 0 ? ({ ...column, children } as any) : null;
            }
            return anyColumn.key !== undefined && hidden.has(String(anyColumn.key)) ? null : column;
        })
        .filter(Boolean);
    return kept as ColumnsType<FactorDirectoryRow>;
}
