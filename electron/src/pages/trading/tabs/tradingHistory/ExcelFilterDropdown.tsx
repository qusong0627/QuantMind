/**
 * Excel 式表头筛选下拉（漏斗图标 → 勾选值集合 + 搜索 + 全选/清空）。
 *
 * 语义与 `columnFilters.ts` 对齐：`undefined`=全选、数组=勾选子集、`[]`=一行都不展示。
 * 勾选即生效（不设"确定"按钮），和 Excel 一致；漏斗在筛选激活时染蓝，
 * 用户扫一眼表头就知道哪几列被筛过——筛完忘了自己筛过什么，是台账页最常见的误判源。
 */
import React, { useMemo, useState } from 'react';
import { Checkbox, Popover } from 'antd';
import { Filter } from 'lucide-react';
import { isFilterActive } from './columnFilters';

export interface ExcelFilterPanelProps {
    options: readonly string[];
    /** undefined = 全选；[] = 一个都不勾 */
    selected: readonly string[] | undefined;
    onApply: (next: string[] | undefined) => void;
}

/** 下拉面板本体（与触发器分开导出：面板语义可以脱离 antd 弹层单独测） */
export function ExcelFilterPanel({ options, selected, onApply }: ExcelFilterPanelProps) {
    const [query, setQuery] = useState('');
    const allSelected = selected === undefined;
    const keyword = query.trim().toLowerCase();
    const visibleOptions = useMemo(
        () => options.filter((option) => option.toLowerCase().includes(keyword)),
        [options, keyword],
    );

    const isChecked = (option: string) => allSelected || selected.includes(option);
    const checkedCount = allSelected ? options.length : selected.length;

    const toggle = (option: string) => {
        if (allSelected) {
            // 从全选态取消一项：其余全部继续保持勾选
            const rest = options.filter((value) => value !== option);
            onApply(rest.length === options.length ? undefined : rest);
            return;
        }
        const next = new Set(selected);
        if (next.has(option)) {
            next.delete(option);
        } else {
            next.add(option);
        }
        // 恰好勾满时归一化回「全选」，避免 undefined 与全集两种表达并存
        const ordered = options.filter((value) => next.has(value));
        onApply(ordered.length === options.length ? undefined : ordered);
    };

    return (
        <div className="w-52 bg-white" data-testid="excel-filter-panel">
            <div className="p-2 border-b border-gray-100">
                <input
                    type="text"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="搜索"
                    className="w-full px-2 py-1 text-xs border border-gray-200 rounded focus:outline-none focus:border-blue-400"
                />
            </div>

            <div className="max-h-56 overflow-auto py-1">
                {options.length === 0 ? (
                    <div className="px-3 py-4 text-center text-xs text-gray-400">无可筛选的值</div>
                ) : visibleOptions.length === 0 ? (
                    <div className="px-3 py-4 text-center text-xs text-gray-400">无匹配项</div>
                ) : (
                    visibleOptions.map((option) => (
                        <label
                            key={option}
                            className="flex items-center gap-2 px-3 py-1 text-xs text-gray-700 cursor-pointer hover:bg-gray-50"
                        >
                            <Checkbox
                                checked={isChecked(option)}
                                onChange={() => toggle(option)}
                            />
                            <span className="truncate">{option}</span>
                        </label>
                    ))
                )}
            </div>

            <div className="flex items-center justify-between px-3 py-2 border-t border-gray-100">
                <span className="text-[10px] text-gray-400">
                    已选 {checkedCount}/{options.length}
                </span>
                <span className="flex items-center gap-2">
                    <button
                        type="button"
                        onClick={() => onApply(undefined)}
                        className="text-xs text-blue-600 hover:text-blue-700"
                    >
                        全选
                    </button>
                    <button
                        type="button"
                        onClick={() => onApply([])}
                        className="text-xs text-gray-500 hover:text-gray-700"
                    >
                        清空
                    </button>
                </span>
            </div>
        </div>
    );
}

export interface ExcelFilterDropdownProps {
    /** 列名（进 aria-label，如「方向筛选」） */
    title: string;
    options: readonly string[];
    selected: readonly string[] | undefined;
    onApply: (next: string[] | undefined) => void;
}

/** 表头里的漏斗触发器：点开面板，激活时染蓝 */
export default function ExcelFilterDropdown({ title, options, selected, onApply }: ExcelFilterDropdownProps) {
    const active = isFilterActive(selected);

    return (
        <Popover
            trigger="click"
            placement="bottomLeft"
            arrow={false}
            content={
                <ExcelFilterPanel options={options} selected={selected} onApply={onApply} />
            }
        >
            <button
                type="button"
                aria-label={`${title}筛选`}
                data-filter-active={active ? 'true' : 'false'}
                title={active ? `${title}（已筛选）` : `${title}筛选`}
                className={`inline-flex items-center justify-center rounded p-0.5 transition-colors ${
                    active ? 'text-blue-600 bg-blue-50' : 'text-gray-400 hover:text-gray-600 hover:bg-gray-100'
                }`}
            >
                <Filter size={11} />
                {active && <span className="ml-0.5 w-1 h-1 rounded-full bg-blue-600" />}
            </button>
        </Popover>
    );
}
