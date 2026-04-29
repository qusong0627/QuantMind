/**
 * 回测历史组件
 *
 * 功能：
 * - 展示回测历史列表
 * - 搜索和过滤
 * - 分页
 * - 批量操作
 * - 查看详情
 */

import React, { useState, useMemo } from 'react';
import { motion } from 'framer-motion';
import { FixedSizeList } from 'react-window';
import pLimit from 'p-limit';
import {
  History,
  Search,
  Trash2,
  Eye,
  CheckSquare,
  Square,
  ChevronLeft,
  ChevronRight,
  Calendar,
  TrendingUp,
  TrendingDown,
  RefreshCw,
  AlertCircle,
  FileText,
  Table,
} from 'lucide-react';
import { useBacktestHistory, useDeleteBacktest, useBatchDeleteBacktests, useExportCSV, useExportJSON } from '../../hooks/useBacktestQueries';
import { BacktestResult, HistoryFilter } from '../../services/backtestService';
import { format } from 'date-fns';
import { zhCN } from 'date-fns/locale';

interface BacktestHistoryProps {
  userId: string;
  onViewDetail?: (backtest: BacktestResult) => void;
}

type BacktestRecordExt = BacktestResult & {
  strategy_display_name?: string;
  strategy_type?: string;
  qlib_strategy_type?: string;
  config?: {
    strategy_display_name?: string;
    strategy_name?: string;
    strategy_type?: string;
    qlib_strategy_type?: string;
    start_date?: string;
    end_date?: string;
    initial_capital?: number | string;
  };
};

type HistoryApiPayload = BacktestResult[] | {
  backtests?: BacktestResult[];
  total?: number;
};

export const BacktestHistory: React.FC<BacktestHistoryProps> = ({
  userId,
  onViewDetail,
}) => {
  // ========== 状态管理 ==========
  const [searchTerm, setSearchTerm] = useState('');
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [filter, setFilter] = useState<HistoryFilter>({
    sort_by: 'created_at',
    sort_order: 'desc',
    page: 1,
    page_size: 10,
  });

  // ========== React Query ==========
  const { data: response, isLoading, isFetching, error, refetch } = useBacktestHistory(userId, filter);
  const deleteBacktest = useDeleteBacktest(userId);
  const batchDelete = useBatchDeleteBacktests(userId);
  const exportCSV = useExportCSV();
  const exportJSON = useExportJSON();

  // 解析后端响应 (支持分页和非分页格式)
  const history = useMemo(() => {
    if (!response) return [];
    const res = response as unknown as HistoryApiPayload;
    if (Array.isArray(res)) return res;
    if (res.backtests && Array.isArray(res.backtests)) return res.backtests;
    return [];
  }, [response]);

  const totalCount = useMemo(() => {
    if (!response) return 0;
    const res = response as unknown as HistoryApiPayload;
    if (Array.isArray(res)) return res.length;
    return res.total || res.backtests?.length || 0;
  }, [response]);

  const totalPages = Math.ceil(totalCount / (filter.page_size || 10));

  // ========== 前端搜索过滤 (仅在当前页) ==========
  const filteredHistory = useMemo(() => {
    if (!searchTerm) return history;

    const term = searchTerm.toLowerCase();
    return history.filter((bt) =>
      bt.symbol?.toLowerCase().includes(term) ||
      bt.strategy_name?.toLowerCase().includes(term) ||
      String((bt as BacktestRecordExt).strategy_type || '').toLowerCase().includes(term) ||
      String((bt as BacktestRecordExt).qlib_strategy_type || '').toLowerCase().includes(term) ||
      String((bt as BacktestRecordExt).config?.strategy_name || '').toLowerCase().includes(term) ||
      String((bt as BacktestRecordExt).config?.qlib_strategy_type || '').toLowerCase().includes(term) ||
      bt.backtest_id.toLowerCase().includes(term)
    );
  }, [history, searchTerm]);

  // ========== 选择操作 ==========
  const isAllSelected = filteredHistory.length > 0 &&
    filteredHistory.every((bt) => selectedIds.includes(bt.backtest_id));

  const toggleSelectAll = () => {
    if (isAllSelected) {
      setSelectedIds([]);
    } else {
      setSelectedIds(filteredHistory.map((bt) => bt.backtest_id));
    }
  };

  const toggleSelect = (id: string) => {
    setSelectedIds(
      selectedIds.includes(id) ? selectedIds.filter((x) => x !== id) : [...selectedIds, id]
    );
  };

  // ========== 删除操作 ==========
  const handleDelete = async (id: string) => {
    if (confirm('确定要删除这个回测记录吗？')) {
      try {
        // 查找对应记录以获取其真实的 user_id
        const record = history.find(bt => bt.backtest_id === id);
        await deleteBacktest.mutateAsync({ id, userId: record?.user_id });
        setSelectedIds(selectedIds.filter((x) => x !== id));
        // 删除成功后刷新列表
        refetch();
      } catch (err: any) {
        console.error('删除回测失败:', err);
        alert(`删除失败: ${err.message || '未知错误'}`);
      }
    }
  };

  // P1: 批量删除限流 - 并发控制防止服务器过载
  const handleBatchDelete = async () => {
    if (selectedIds.length === 0) return;

    if (confirm(`确定要删除选中的 ${selectedIds.length} 个回测记录吗？`)) {
      const limit = pLimit(3); // 限制并发数为3
      const deletePromises = selectedIds.map((id) => {
        const record = history.find(bt => bt.backtest_id === id);
        return limit(() => deleteBacktest.mutateAsync({ id, userId: record?.user_id }));
      });

      try {
        await Promise.all(deletePromises);
        setSelectedIds([]);
        refetch(); // 刷新列表
      } catch (error) {
        console.error('批量删除失败:', error);
        alert('部分删除失败，请重试');
      }
    }
  };

  // ========== 导出操作 ==========
  const handleExportCSV = async (id: string, symbol: string) => {
    try {
      await exportCSV.mutateAsync({
        backtestId: id,
        filename: `回测报告_${symbol}_${format(new Date(), 'yyyyMMdd')}.csv`,
      });
    } catch (err: any) {
      alert(`导出CSV失败: ${err?.message || '未知错误'}`);
    }
  };

  const handleExportJSON = async (id: string, symbol: string) => {
    try {
      await exportJSON.mutateAsync({
        backtestId: id,
        filename: `回测原始数据_${symbol}_${format(new Date(), 'yyyyMMdd')}.json`,
      });
    } catch (err: any) {
      alert(`导出JSON失败: ${err?.message || '未知错误'}`);
    }
  };

  // ========== 排序操作 ==========
  const handleSort = (field: 'created_at' | 'total_return' | 'sharpe_ratio' | 'max_drawdown') => {
    setFilter({
      ...filter,
      sort_by: field,
      sort_order: filter.sort_by === field && filter.sort_order === 'desc' ? 'asc' : 'desc',
    });
  };

  // P2: 虚拟滚动 - 当数据量大时使用
  const useVirtualScroll = filteredHistory.length > 100;
  const rowHeight = 72; // 每行高度（px）
  const listHeight = Math.min(600, filteredHistory.length * rowHeight); // 最大高度600px

  // ========== 渲染 ==========
  return (
    <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
      {/* 头部 */}
      <div className="p-6 border-b border-gray-200">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <History className="w-6 h-6 text-blue-500" />
            <h2 className="text-xl font-bold text-gray-800">回测历史</h2>
            <span className="px-2 py-1 text-xs bg-blue-100 text-blue-600 rounded-2xl">
              {filteredHistory.length} 条记录
            </span>
          </div>

          <button
            type="button"
            onClick={() => refetch()}
            disabled={isFetching}
            aria-label="刷新列表"
            title="刷新"
            className="p-2 hover:bg-gray-50 rounded-2xl transition-colors"
          >
            <RefreshCw className={`w-5 h-5 text-gray-600 ${isFetching ? 'animate-spin' : ''}`} />
          </button>
        </div>

        {/* 搜索和操作栏 */}
        <div className="flex gap-3">
          {/* 搜索框 */}
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
            <input
              type="text"
              placeholder="搜索策略名称、股票代码或回测ID..."
              value={searchTerm}
              onChange={(e) => {
                setSearchTerm(e.target.value);
                // 搜索时重置到第1页
                if (filter.page !== 1) {
                  setFilter({ ...filter, page: 1 });
                }
              }}
              className="w-full pl-10 pr-4 py-2 bg-gray-50 border border-gray-200 rounded-2xl text-gray-800 placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"
            />
          </div>

          {/* 批量删除按钮 */}
          {selectedIds.length > 0 && (
            <motion.button
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              onClick={handleBatchDelete}
              disabled={batchDelete.isPending}
              className="px-4 py-2 bg-red-100 text-red-600 rounded-2xl hover:bg-red-200 transition-colors flex items-center gap-2"
            >
              <Trash2 className="w-4 h-4" />
              删除选中 ({selectedIds.length})
            </motion.button>
          )}
        </div>
      </div>

      {/* 列表内容 */}
      <div className="overflow-auto max-h-[600px]">
        {isLoading ? (
          <div className="flex items-center justify-center py-20">
            <RefreshCw className="w-8 h-8 text-blue-400 animate-spin" />
          </div>
        ) : error ? (
          <div className="flex items-center justify-center py-20 text-red-400">
            <AlertCircle className="w-6 h-6 mr-2" />
            加载失败: {error.message}
          </div>
        ) : filteredHistory.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-gray-600">
            <History className="w-12 h-12 mb-4 opacity-50" />
            <p className="text-lg">暂无回测历史</p>
            {searchTerm && (
              <p className="text-sm mt-2">
                未找到包含 "{searchTerm}" 的记录
              </p>
            )}
          </div>
        ) : useVirtualScroll ? (
          // P2: 虚拟滚动模式（数据量>100）
          <div>
            <table className="w-full min-w-[1500px]">
              <thead className="bg-gray-50 sticky top-0 z-10">
                <tr>
                  <th className="px-6 py-3 text-left">
                    <button
                      type="button"
                      onClick={toggleSelectAll}
                      aria-label="全选或取消全选"
                      title="全选/取消全选"
                      className="text-gray-600 hover:text-gray-800 transition-colors"
                    >
                      {isAllSelected ? (
                        <CheckSquare className="w-5 h-5" />
                      ) : (
                        <Square className="w-5 h-5" />
                      )}
                    </button>
                  </th>
                  <th
                    className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-800"
                    onClick={() => handleSort('created_at')}
                  >
                    时间
                  </th>
                  <th
                    className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-800"
                    onClick={() => handleSort('created_at')}
                  >
                    策略
                  </th>
                  <th className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider">
                    模型
                  </th>
                  <th className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider">
                    回测期间
                  </th>
                  <th className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider">
                    初始资金
                  </th>
                  <th className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider">
                    基准指数
                  </th>
                  <th
                    className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-800"
                    onClick={() => handleSort('total_return')}
                  >
                    收益率
                  </th>
                  <th
                    className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-800"
                    onClick={() => handleSort('sharpe_ratio')}
                  >
                    夏普比率
                  </th>
                  <th className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider">
                    基准收益
                  </th>
                  <th className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider">
                    操作
                  </th>
                </tr>
              </thead>
            </table>
            <FixedSizeList
              height={listHeight}
              itemCount={filteredHistory.length}
              itemSize={rowHeight}
              width="100%"
              className="divide-y divide-gray-200"
            >
              {({ index, style }) => {
                const backtest = filteredHistory[index];
                return (
                  <div style={style}>
                    <table className="w-full min-w-[1500px]">
                      <tbody>
                        <BacktestHistoryRow
                          key={backtest.backtest_id}
                          backtest={backtest}
                          isSelected={selectedIds.includes(backtest.backtest_id)}
                          onToggleSelect={() => toggleSelect(backtest.backtest_id)}
                          onDelete={() => handleDelete(backtest.backtest_id)}
                          onView={() => onViewDetail?.(backtest)}
                          onExportCSV={() => handleExportCSV(backtest.backtest_id, backtest.symbol || '未知')}
                          onExportJSON={() => handleExportJSON(backtest.backtest_id, backtest.symbol || '未知')}
                        />
                      </tbody>
                    </table>
                  </div>
                );
              }}
            </FixedSizeList>
          </div>
        ) : (
          // 普通模式（数据量<=100）
          <table className="w-full min-w-[1500px]">
            <thead className="bg-gray-50 sticky top-0 z-10">
              <tr>
                <th className="px-6 py-3 text-left">
                  <button
                    type="button"
                    onClick={toggleSelectAll}
                    aria-label="全选或取消全选"
                    title="全选/取消全选"
                    className="text-gray-600 hover:text-gray-800 transition-colors"
                  >
                    {isAllSelected ? (
                      <CheckSquare className="w-5 h-5" />
                    ) : (
                      <Square className="w-5 h-5" />
                    )}
                  </button>
                </th>
                <th
                  className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-800"
                  onClick={() => handleSort('created_at')}
                >
                  时间
                </th>
                <th
                  className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-800"
                  onClick={() => handleSort('created_at')}
                >
                  策略
                </th>
                <th className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider">
                  模型
                </th>
                <th className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider">
                  回测期间
                </th>
                <th className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider">
                  初始资金
                </th>
                <th className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider">
                  基准指数
                </th>
                <th
                  className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-800"
                  onClick={() => handleSort('total_return')}
                >
                  收益率
                </th>
                <th
                  className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider cursor-pointer hover:text-gray-800"
                  onClick={() => handleSort('sharpe_ratio')}
                >
                  夏普比率
                </th>
                <th className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider">
                  基准收益
                </th>
                <th className="px-6 py-3 text-center text-xs font-medium text-gray-600 uppercase tracking-wider">
                  操作
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {filteredHistory.map((backtest) => (
                <BacktestHistoryRow
                  key={backtest.backtest_id}
                  backtest={backtest}
                  isSelected={selectedIds.includes(backtest.backtest_id)}
                  onToggleSelect={() => toggleSelect(backtest.backtest_id)}
                  onDelete={() => handleDelete(backtest.backtest_id)}
                  onView={() => onViewDetail?.(backtest)}
                  onExportCSV={() => handleExportCSV(backtest.backtest_id, backtest.symbol || '未知')}
                  onExportJSON={() => handleExportJSON(backtest.backtest_id, backtest.symbol || '未知')}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* 分页 */}
      {totalPages > 1 && (
        <div className="px-6 py-4 border-t border-gray-200 flex items-center justify-between">
          <div className="text-sm text-gray-600">
            第 {filter.page} / {totalPages} 页，共 {totalCount} 条记录
          </div>

          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => setFilter({ ...filter, page: Math.max(1, (filter.page || 1) - 1) })}
              disabled={filter.page === 1}
              aria-label="上一页"
              title="上一页"
              className="p-2 hover:bg-gray-50 rounded-2xl disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <ChevronLeft className="w-5 h-5 text-gray-600" />
            </button>

            <button
              type="button"
              onClick={() => setFilter({ ...filter, page: Math.min(totalPages, (filter.page || 1) + 1) })}
              disabled={filter.page === totalPages}
              aria-label="下一页"
              title="下一页"
              className="p-2 hover:bg-gray-50 rounded-2xl disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <ChevronRight className="w-5 h-5 text-gray-600" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export const STRATEGY_NAME_MAP: Record<string, string> = {
  // 核心内置 ID
  TopkDropout: '默认 Top-K 选股策略',
  WeightStrategy: '截面 Alpha 预测策略',
  standard_topk: '默认 Top-K 选股策略',
  alpha_cross_section: '截面 Alpha 预测策略',
  adaptive_drift: '自适应动态调仓策略 (Concept Drift)',
  score_weighted: '得分加权组合策略',
  StopLoss: '止损止盈策略',
  LimitFilter: '涨停过滤 Top-K 策略',
  VolatilityWeighted: '波动率加权 TopK 策略',
  momentum: '趋势动量策略 (Momentum)',
  Momentum: '趋势动量策略 (Momentum)',
  deep_time_series: '深度学习时序策略 (GRU/LSTM)',
  aggressive_topk_strategy: '激进版截面TopK策略',
  long_short_topk: '多空 TopK 策略',

  // 扩展分析/兼容旧版
  EnhancedIndex: '增强指数策略',
  RiskParity: '风险平价策略',

  // 经典算法模板
  dual_ma_crossover: '双均线交叉策略',
  macd_strategy: 'MACD趋势策略',
  bollinger_bands: '布林带均值回归策略',
  rsi_strategy: 'RSI超买超卖策略',
  turtle_trading: '海龟交易策略',
  pairs_trading: '统计套利配对交易策略',
  grid_trading: '网格交易策略',
  momentum_strategy: '价格动量策略',
  multi_factor: '多因子选股策略',
  volatility_breakout: '波动率突破策略',
  mean_reversion_portfolio: '均值回归组合策略',
};

export const formatStrategyName = (raw?: string | null) => {
  if (!raw) return '';
  const normalizedRaw = String(raw).trim().replace(/^['"]|['"]$/g, '').replace(/\.(py|json)$/i, '');
  if (!normalizedRaw) return '';

  // 1. 直接匹配
  if (STRATEGY_NAME_MAP[normalizedRaw]) return STRATEGY_NAME_MAP[normalizedRaw];

  // 2. 不区分大小写匹配
  const lowerRaw = normalizedRaw.toLowerCase();
  const foundKey = Object.keys(STRATEGY_NAME_MAP).find(
    k => k.toLowerCase() === lowerRaw
  );

  if (foundKey) return STRATEGY_NAME_MAP[foundKey];

  // 3. 子串兜底（兼容后端返回带前后缀的模板标识）
  const fuzzyKey = Object.keys(STRATEGY_NAME_MAP).find(
    k => lowerRaw.includes(k.toLowerCase())
  );
  if (fuzzyKey) return STRATEGY_NAME_MAP[fuzzyKey];

  // 3. 处理带下划线的命名风格
  return normalizedRaw;
};

export const resolveStrategyName = (backtest: BacktestResult): string => {
  const ext = backtest as BacktestRecordExt;
  const config = ext.config ?? {};
  const candidates = [
    ext.strategy_display_name,
    backtest.strategy_name,
    config.strategy_display_name,
    config.strategy_name,
    config.qlib_strategy_type,
    ext.qlib_strategy_type,
    config.strategy_type,
    ext.strategy_type,
  ];
  const rawName = candidates.find((item) => String(item || '').trim()) as string | undefined;
  return formatStrategyName(rawName) || '默认 Top-K 选股策略';
};

export const resolveBacktestPeriod = (backtest: BacktestResult): string => {
  const config = (backtest as BacktestRecordExt).config ?? {};
  const startDate = String(backtest.start_date || config.start_date || '').trim();
  const endDate = String(backtest.end_date || config.end_date || '').trim();
  if (!startDate && !endDate) return '-';
  return `${startDate || '-'} ~ ${endDate || '-'}`;
};

const resolveInitialCapital = (backtest: BacktestResult): number | null => {
  const config = (backtest as BacktestRecordExt).config ?? {};
  const raw = backtest.initial_capital ?? config.initial_capital;
  const value = Number(raw);
  return Number.isFinite(value) && value > 0 ? value : null;
};

// ============================================================================
// 历史记录行组件
// ============================================================================

interface BacktestHistoryRowProps {
  backtest: BacktestResult;
  isSelected: boolean;
  onToggleSelect: () => void;
  onDelete: () => void;
  onView: () => void;
  onExportCSV: () => void;
  onExportJSON: () => void;
}

const BacktestHistoryRow: React.FC<BacktestHistoryRowProps> = ({
  backtest,
  isSelected,
  onToggleSelect,
  onDelete,
  onView,
  onExportCSV,
  onExportJSON,
}) => {
  const totalReturn = backtest.total_return || 0;
  const sharpeRatio = backtest.sharpe_ratio || 0;
  const initialCapital = resolveInitialCapital(backtest);
  const isProfit = totalReturn > 0;
  const isLoss = totalReturn < 0;

  return (
    <tr className="hover:bg-gray-50 transition-colors">
      {/* 选择框 */}
      <td className="px-6 py-4">
        <button
          type="button"
          onClick={onToggleSelect}
          aria-label="选择记录"
          title="选择"
          className="text-gray-600 hover:text-gray-800 transition-colors"
        >
          {isSelected ? (
            <CheckSquare className="w-5 h-5 text-blue-500" />
          ) : (
            <Square className="w-5 h-5" />
          )}
        </button>
      </td>

      {/* 时间 */}
      <td className="px-6 py-4 whitespace-nowrap text-center">
        <div className="flex items-center justify-center gap-2">
          <Calendar className="w-4 h-4 text-gray-500" />
          <span className="text-sm text-gray-700">
            {format(new Date(backtest.created_at || ''), 'yyyy-MM-dd HH:mm', { locale: zhCN })}
          </span>
        </div>
      </td>

      {/* 策略 */}
      <td className="px-6 py-4 whitespace-nowrap max-w-[320px]">
        <span className="block truncate text-sm font-medium text-gray-800" title={resolveStrategyName(backtest)}>
          {resolveStrategyName(backtest)}
        </span>
      </td>

      {/* 模型 */}
      <td className="px-6 py-4 whitespace-nowrap text-center">
        <span className="text-sm text-gray-700" title={(backtest as any).model_name || ''}>
          {(backtest as any).model_name || '-'}
        </span>
      </td>

      {/* 期间 */}
      <td className="px-6 py-4 whitespace-nowrap text-center">
        <span className="text-sm text-gray-700">
          {resolveBacktestPeriod(backtest)}
        </span>
      </td>

      {/* 初始资金 */}
      <td className="px-6 py-4 whitespace-nowrap text-center">
        <span className="text-sm text-gray-700">
          {initialCapital != null ? `¥${initialCapital.toLocaleString()}` : '-'}
        </span>
      </td>

      {/* 基准指数 */}
      <td className="px-6 py-4 whitespace-nowrap text-center">
        <span className="text-sm text-gray-700">
          {backtest.benchmark_symbol || '-'}
        </span>
      </td>

      {/* 收益率 */}
      <td className="px-6 py-4 whitespace-nowrap text-center">
        <div className="flex items-center justify-center gap-1">
          {isProfit ? (
            <TrendingUp className="w-4 h-4 text-red-500" />
          ) : isLoss ? (
            <TrendingDown className="w-4 h-4 text-green-500" />
          ) : (
            <TrendingUp className="w-4 h-4 text-gray-400" />
          )}
          <span className={`text-sm font-medium ${isProfit ? 'text-red-500' : isLoss ? 'text-green-500' : 'text-gray-500'}`}>
            {(totalReturn * 100).toFixed(2)}%
          </span>
        </div>
      </td>

      {/* 夏普比率 */}
      <td className="px-6 py-4 whitespace-nowrap text-center">
        <span className="text-sm font-medium text-red-500">
          {sharpeRatio.toFixed(2)}
        </span>
      </td>

      {/* 基准收益 */}
      <td className="px-6 py-4 whitespace-nowrap text-center">
        <span className="text-sm text-gray-700">
          {backtest.benchmark_return != null ? `${(backtest.benchmark_return * 100).toFixed(2)}%` : '-'}
        </span>
      </td>

      {/* 操作 */}
      <td className="px-6 py-4 whitespace-nowrap text-right">
        <div className="flex items-center justify-end gap-2">
          <button
            onClick={onView}
            className="p-1.5 hover:bg-blue-500/20 rounded-2xl text-blue-400"
            title="查看详情"
          >
            <Eye className="w-4 h-4" />
          </button>

          <button
            onClick={onExportCSV}
            className="p-1.5 hover:bg-purple-500/20 rounded-2xl text-purple-400"
            title="导出CSV"
          >
            <FileText className="w-4 h-4" />
          </button>

          <button
            onClick={onExportJSON}
            className="p-1.5 hover:bg-green-500/20 rounded-2xl text-green-400"
            title="导出JSON"
          >
            <Table className="w-4 h-4" />
          </button>

          <button
            onClick={onDelete}
            className="p-1.5 hover:bg-red-500/20 rounded-2xl text-red-400"
            title="删除"
          >
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      </td>
    </tr>
  );
};

// (AlertCircle imported at top)
