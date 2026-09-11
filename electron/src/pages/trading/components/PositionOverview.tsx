import React, { useEffect, useMemo, useState } from 'react';
import { ChevronLeft, ChevronRight, Minus, PieChart as PieChartIcon, TrendingDown, TrendingUp } from 'lucide-react';
import { PieChart as RechartsPieChart, Pie, Cell, ResponsiveContainer, Tooltip, Legend } from 'recharts';

import { NormalizedHolding, PositionSummary } from '../utils/positionMetrics';

interface PositionOverviewProps {
    holdings: NormalizedHolding[];
    summary: PositionSummary;
    variant?: 'full' | 'compact';
    className?: string;
}

const FULL_LEFT_RATIO = 'w-[30%]';
const FULL_RIGHT_RATIO = 'w-[70%]';
const COMPACT_LEFT_RATIO = 'w-1/2';
const COMPACT_RIGHT_RATIO = 'w-1/2';

const formatAmount = (val: number) => {
    if (Math.abs(val) >= 10000) {
        return `¥${(val / 10000).toFixed(2)}万`;
    }
    return `¥${val.toFixed(2)}`;
};

const PositionOverview: React.FC<PositionOverviewProps> = ({ holdings, summary, variant = 'full', className }) => {
    const [currentPage, setCurrentPage] = useState(1);
    const itemsPerPage = 10;
    const compact = variant === 'compact';

    const totalAsset = summary.totalAsset;
    const cashValue = summary.cashValue;
    const positionValue = summary.positionValue;
    const positionRatio = summary.positionRatio.toFixed(2);
    const cashRatio = summary.cashRatio.toFixed(2);

    const positionData = useMemo(() => {
        const data = [
            { name: '持仓', value: positionValue, color: '#3B82F6' },
            { name: '现金', value: cashValue, color: '#10B981' },
        ];
        return data.filter(item => item.value >= 0);
    }, [cashValue, positionValue]);

    const totalPages = compact ? 1 : Math.max(1, Math.ceil(holdings.length / itemsPerPage));
    const startIndex = (currentPage - 1) * itemsPerPage;
    const currentHoldings = compact ? holdings : holdings.slice(startIndex, startIndex + itemsPerPage);

    useEffect(() => {
        if (!compact && currentPage > totalPages) {
            setCurrentPage(totalPages);
        }
    }, [compact, currentPage, totalPages]);

    const hasChartData = positionData.some(item => item.value > 0);

    return (
        <div className={`h-full flex flex-row gap-4 ${className || ''}`}>
            <div className={`${compact ? COMPACT_LEFT_RATIO : FULL_LEFT_RATIO} bg-white rounded-xl p-6 border border-gray-200 flex flex-col`}>
                <h3 className="text-base font-bold text-gray-800 mb-6 flex items-center">
                    <PieChartIcon className="mr-2 text-blue-600" size={18} />
                    持仓分布
                </h3>

                <div className="flex-1 relative min-h-0">
                    {hasChartData ? (
                        <ResponsiveContainer width="100%" height="100%">
                            <RechartsPieChart>
                                <Pie
                                    data={positionData}
                                    cx="50%"
                                    cy="50%"
                                    innerRadius={compact ? 70 : 80}
                                    outerRadius={compact ? 90 : 120}
                                    paddingAngle={5}
                                    dataKey="value"
                                >
                                    {positionData.map((entry, index) => (
                                        <Cell key={`cell-${index}`} fill={entry.color} strokeWidth={0} />
                                    ))}
                                </Pie>
                                <Tooltip
                                    formatter={(value: number) => `¥${(value / 10000).toFixed(2)}万`}
                                    contentStyle={{
                                        backgroundColor: '#fff',
                                        borderColor: '#e5e7eb',
                                        borderRadius: '8px',
                                        boxShadow: '0 4px 6px -1px rgba(0, 0, 0, 0.1)',
                                    }}
                                />
                                <Legend
                                    verticalAlign="bottom"
                                    height={36}
                                    formatter={(value, entry: any) => {
                                        const percent = entry.payload.name === '持仓' ? positionRatio : cashRatio;
                                        return `${value} ${percent}%`;
                                    }}
                                />
                            </RechartsPieChart>
                        </ResponsiveContainer>
                    ) : (
                        <div className="h-full flex items-center justify-center text-gray-400 text-sm">
                            暂无持仓数据
                        </div>
                    )}
                    {hasChartData && (
                        <div className="absolute inset-0 flex items-center justify-center pointer-events-none pb-8">
                            <div className="text-center">
                                <div className="text-xs text-gray-400 font-medium">总资产</div>
                                <div className={`${compact ? 'text-lg' : 'text-xl'} font-bold text-gray-800`}>
                                    ¥{((totalAsset || positionValue + cashValue) / 10000).toFixed(2)}万
                                </div>
                            </div>
                        </div>
                    )}
                </div>
            </div>

            <div className={`${compact ? COMPACT_RIGHT_RATIO : FULL_RIGHT_RATIO} bg-white rounded-xl border border-gray-200 overflow-hidden flex flex-col`}>
                <div className="px-6 py-2 border-b border-gray-200">
                    <h3 className="text-base font-bold text-gray-800">{compact ? '持仓明细' : '持仓明细（按市值排序）'}</h3>
                </div>

                <div className="flex-1 overflow-y-auto overflow-x-auto custom-scrollbar pb-2">
                    {currentHoldings.length > 0 ? (
                        <table className={`w-full min-w-[1160px] table-fixed ${compact ? '' : '[&_td]:!py-[9.5px] [&_th]:!py-[8.5px]'}`}>
                            <thead className="sticky top-0 bg-gray-100 border-b border-gray-200">
                                <tr>
                                    {!compact && <th className="px-3 py-1.5 text-center text-sm font-semibold text-gray-700 w-[6%] whitespace-nowrap">序号</th>}
                                    <th className={`px-3 py-1.5 text-center text-sm font-semibold text-gray-700 whitespace-nowrap ${compact ? 'w-1/4' : 'w-[11%]'}`}>股票代码</th>
                                    {!compact && <th className="px-3 py-1.5 text-center text-sm font-semibold text-gray-700 w-[12%] whitespace-nowrap">股票名称</th>}
                                    <th className={`px-3 py-1.5 text-center text-sm font-semibold text-gray-700 whitespace-nowrap ${compact ? 'w-1/4' : 'w-[11%]'}`}>持仓数量</th>
                                    {!compact && <th className="px-3 py-1.5 text-center text-sm font-semibold text-gray-700 w-[11%] whitespace-nowrap">成本价</th>}
                                    {!compact && <th className="px-3 py-1.5 text-center text-sm font-semibold text-gray-700 w-[11%] whitespace-nowrap">现价</th>}
                                    <th className={`px-3 py-1.5 text-center text-sm font-semibold text-gray-700 whitespace-nowrap ${compact ? 'w-1/4' : 'w-[12%]'}`}>持仓市值</th>
                                    {!compact && <th className="px-3 py-1.5 text-center text-sm font-semibold text-gray-700 w-[14%] whitespace-nowrap">盈亏金额</th>}
                                    <th className={`px-3 py-1.5 text-center text-sm font-semibold text-gray-700 whitespace-nowrap ${compact ? 'w-1/4' : 'w-[12%]'}`}>盈亏比例</th>
                                </tr>
                            </thead>
                            <tbody>
                                {currentHoldings.map((holding, index) => (
                                    <tr
                                        key={holding.code}
                                        className={`border-b border-gray-100 transition-colors hover:bg-blue-50/50 ${
                                            index % 2 === 0 ? 'bg-white' : 'bg-blue-50/30'
                                        }`}
                                    >
                                        {!compact && (
                                            <td className="px-3 py-1.5 text-center text-sm text-gray-900">
                                                {startIndex + index + 1}
                                            </td>
                                        )}
                                        <td className="px-3 py-1.5 text-center text-sm font-mono text-gray-900 overflow-hidden text-ellipsis whitespace-nowrap">
                                            {holding.code}
                                        </td>
                                        {!compact && (
                                            <td className="px-3 py-1.5 text-center text-sm font-medium text-gray-900 overflow-hidden text-ellipsis whitespace-nowrap">
                                                {holding.name}
                                            </td>
                                        )}
                                        <td className="px-3 py-1.5 text-center text-sm text-gray-900">
                                            {holding.shares.toLocaleString()}
                                        </td>
                                        {!compact && (
                                            <td className="px-3 py-1.5 text-center text-sm text-gray-700">
                                                ¥{holding.cost.toFixed(2)}
                                            </td>
                                        )}
                                        {!compact && (
                                            <td className="px-3 py-1.5 text-center text-sm font-semibold text-gray-900">
                                                ¥{holding.current.toFixed(2)}
                                            </td>
                                        )}
                                        <td className="px-3 py-1.5 text-center text-sm font-bold text-gray-900">
                                            {formatAmount(holding.value)}
                                        </td>
                                        {!compact && (
                                            <td className={`px-3 py-1.5 text-center text-sm font-bold flex items-center justify-center gap-1 ${
                                                holding.profit > 0 ? 'text-red-500' : holding.profit < 0 ? 'text-emerald-500' : 'text-black'
                                            }`}>
                                                {holding.profit > 0 ? <TrendingUp size={14} /> : holding.profit < 0 ? <TrendingDown size={14} /> : <Minus size={14} />}
                                                {holding.profit > 0 ? '+' : ''}{formatAmount(holding.profit)}
                                            </td>
                                        )}
                                        <td className={`px-3 py-1.5 text-center text-sm font-semibold ${
                                            holding.profitPercent > 0 ? 'text-red-500' : holding.profitPercent < 0 ? 'text-emerald-500' : 'text-black'
                                        }`}>
                                            {holding.profitPercent > 0 ? '+' : ''}{holding.profitPercent.toFixed(2)}%
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    ) : (
                        <div className="flex flex-col items-center justify-center py-16 text-gray-400">
                            <div className="text-4xl mb-2">📊</div>
                            <p className="text-sm">暂无持仓数据</p>
                        </div>
                    )}
                </div>

                {!compact && holdings.length > 0 && (
                    <div className="border-t border-gray-200 bg-gray-50 px-6 py-3 flex items-center justify-between">
                        <div className="text-sm text-gray-600">
                            共 {holdings.length} 只股票，当前第 {currentPage}/{totalPages} 页
                        </div>
                        <div className="flex items-center gap-2">
                            <button
                                onClick={() => setCurrentPage(Math.max(1, currentPage - 1))}
                                disabled={currentPage === 1}
                                className={`inline-flex items-center justify-center w-8 h-8 rounded-lg transition-colors ${
                                    currentPage === 1
                                        ? 'bg-gray-100 text-gray-400 cursor-not-allowed'
                                        : 'bg-white border border-gray-300 text-gray-700 hover:bg-gray-50'
                                }`}
                            >
                                <ChevronLeft size={16} />
                            </button>
                            <span className="text-sm text-gray-700 min-w-[60px] text-center">
                                {currentPage} / {totalPages}
                            </span>
                            <button
                                onClick={() => setCurrentPage(Math.min(totalPages, currentPage + 1))}
                                disabled={currentPage === totalPages}
                                className={`inline-flex items-center justify-center w-8 h-8 rounded-lg transition-colors ${
                                    currentPage === totalPages
                                        ? 'bg-gray-100 text-gray-400 cursor-not-allowed'
                                        : 'bg-white border border-gray-300 text-gray-700 hover:bg-gray-50'
                                }`}
                            >
                                <ChevronRight size={16} />
                            </button>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
};

export default PositionOverview;
