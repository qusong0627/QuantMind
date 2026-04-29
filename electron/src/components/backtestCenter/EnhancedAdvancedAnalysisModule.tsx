/**
 * 高级分析模块（简化版）
 * 仅保留后端已实现且稳定的能力：
 * - 基础风险
 * - 交易统计
 * - 基准对比
 * - 持仓分析
 */

import React, { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  BarChart3,
  PieChart,
  RefreshCw,
  Shield,
} from 'lucide-react';
import { Select } from 'antd';
import { useBacktestCenterStore } from '../../stores/backtestCenterStore';
import { backtestService, type BacktestResult } from '../../services/backtestService';
import { BasicRiskPanel } from './analysis/BasicRiskPanel';
import { TradeStatsPanel } from './analysis/TradeStatsPanel';
import { BenchmarkPanel } from './analysis/BenchmarkPanel';
import { authService } from '../../features/auth/services/authService';
import { resolveStrategyName, resolveBacktestPeriod } from '../backtest/BacktestHistory';

type AnalysisType = 'risk' | 'trade' | 'benchmark';

interface AnalysisModule {
  id: AnalysisType;
  name: string;
  icon: React.ComponentType<{ className?: string }>;
  description: string;
}

const ANALYSIS_MODULES: AnalysisModule[] = [
  {
    id: 'risk',
    name: '基础风险',
    icon: AlertTriangle,
    description: '收益、波动、回撤等核心风险指标',
  },
  {
    id: 'trade',
    name: '交易统计',
    icon: BarChart3,
    description: '胜率、盈亏比、交易频率',
  },
  {
    id: 'benchmark',
    name: '基准对比',
    icon: PieChart,
    description: '超额收益、Beta、Alpha、相关性',
  },
];

export const EnhancedAdvancedAnalysisModule: React.FC = () => {
  const { backtestConfig } = useBacktestCenterStore();
  const [backtestId, setBacktestId] = useState('');
  const [backtestList, setBacktestList] = useState<BacktestResult[]>([]);
  const [activeAnalysis, setActiveAnalysis] = useState<AnalysisType>('risk');
  const [loadingList, setLoadingList] = useState(false);

  useEffect(() => {
    void loadBacktestList();
  }, []);

  const selectedBacktest = useMemo(
    () => backtestList.find((item) => item.backtest_id === backtestId),
    [backtestList, backtestId]
  );

  const backtestOptions = useMemo(
    () => [
      ...backtestList.map((item) => {
        const modelName = (item as any).model_name || '-';
        return {
          value: item.backtest_id,
          label: `${resolveStrategyName(item)} | ${modelName} | ${resolveBacktestPeriod(item)}`,
        };
      }),
    ],
    [backtestList]
  );

  const loadBacktestList = async () => {
    setLoadingList(true);
    try {
      const storedUser = authService.getStoredUser() as
        | { id?: string | number; user_id?: string | number }
        | null;
      const fallbackUserId = backtestConfig.user_id && backtestConfig.user_id !== 'default_user'
        ? backtestConfig.user_id
        : undefined;
      const resolvedUserId = storedUser?.id ?? storedUser?.user_id ?? fallbackUserId;
      if (!resolvedUserId) {
        setBacktestList([]);
        setBacktestId('');
        return;
      }

      const list = await backtestService.getHistory(String(resolvedUserId), {
        page: 1,
        page_size: 20,
      });

      setBacktestList(Array.isArray(list) ? list : []);
      if (Array.isArray(list) && list.length > 0) {
        setBacktestId(backtestId || list[0].backtest_id);
      }
    } catch (error) {
      console.error('Failed to load backtest list:', error);
      setBacktestList([]);
      setBacktestId('');
    } finally {
      setLoadingList(false);
    }
  };

  return (
    <div className="h-full">
      <div className="bg-white rounded-2xl border border-gray-200 shadow-sm p-6 space-y-6">
        <div>
          <h2 className="text-2xl font-bold text-gray-800">高级分析</h2>
          <p className="text-sm text-gray-600 mt-1">
            仅展示后端已实现能力，避免无效模块和空占位
          </p>
        </div>

        <div className="bg-amber-50 border border-amber-200 rounded-2xl p-4">
          <div className="flex items-start gap-2 text-amber-800">
            <Shield className="w-4 h-4 mt-0.5" />
            <div className="text-xs leading-5">
              分析结果基于已完成回测的落库数据；若当前回测刚提交，请先在回测历史确认状态为“完成”。
            </div>
          </div>
        </div>

        <div className="bg-gray-50 rounded-2xl border border-gray-200 p-5">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-base font-semibold text-gray-800">选择回测结果</h3>
            <button
              type="button"
              onClick={() => void loadBacktestList()}
              disabled={loadingList}
              className="px-3 py-1.5 text-xs rounded-xl border border-gray-200 bg-white hover:bg-gray-100 text-gray-700 disabled:opacity-50"
            >
              <span className="inline-flex items-center gap-1">
                <RefreshCw className={`w-3.5 h-3.5 ${loadingList ? 'animate-spin' : ''}`} />
                刷新
              </span>
            </button>
          </div>

          <Select
            value={backtestId || undefined}
            onChange={(value) => setBacktestId(String(value))}
            options={backtestOptions}
            placeholder="请选择回测结果"
            className="w-full"
            size="large"
            variant="outlined"
            popupMatchSelectWidth
            getPopupContainer={(triggerNode) => triggerNode.parentElement ?? document.body}
            styles={{
              root: {
                borderRadius: 16,
              },
              popup: {
                root: {
                  borderRadius: 16,
                  overflow: 'hidden',
                },
              },
            }}
          />
        </div>

        {selectedBacktest && (
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-3">
            <SummaryCard label="总收益率" value={formatPercent(selectedBacktest.total_return ?? 0)} color="red" />
            <SummaryCard label="夏普比率" value={(selectedBacktest.sharpe_ratio ?? 0).toFixed(2)} color="orange" />
            <SummaryCard label="最大回撤" value={formatPercent(selectedBacktest.max_drawdown ?? 0)} color="emerald" />
            <SummaryCard label="投资胜率" value={formatPercent(selectedBacktest.win_rate ?? 0)} color="red" />
            <SummaryCard label="交易次数" value={Math.floor(selectedBacktest.total_trades ?? 0).toLocaleString()} color="gray" />
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          {ANALYSIS_MODULES.map((module) => (
            <button
              key={module.id}
              type="button"
              onClick={() => setActiveAnalysis(module.id)}
              className={`flex flex-col items-center text-center p-3 rounded-2xl border transition-colors ${activeAnalysis === module.id
                ? 'border-blue-500 bg-blue-50'
                : 'border-gray-200 bg-white hover:bg-gray-50'
                }`}
            >
              <div className="flex items-center justify-center gap-2 mb-1.5">
                <module.icon className="w-4 h-4 text-blue-600" />
                <span className="text-sm font-medium text-gray-800">{module.name}</span>
              </div>
              <p className="text-xs text-gray-500">{module.description}</p>
            </button>
          ))}
        </div>

        {backtestId ? (
          <AnalysisResults type={activeAnalysis} backtestId={backtestId} />
        ) : (
          <div className="bg-gray-50 rounded-2xl border border-gray-200 p-10 text-center text-gray-500">
            请先选择一个已完成的回测结果
          </div>
        )}
      </div>
    </div>
  );
};

const AnalysisResults: React.FC<{ type: AnalysisType; backtestId: string }> = ({ type, backtestId }) => {
  switch (type) {
    case 'risk':
      return <BasicRiskPanel backtestId={backtestId} />;
    case 'trade':
      return <TradeStatsPanel backtestId={backtestId} />;
    case 'benchmark':
      return <BenchmarkPanel backtestId={backtestId} />;
    default:
      return null;
  }
};

const SummaryCard: React.FC<{
  label: string;
  value: string;
  color?: 'red' | 'green' | 'blue' | 'rose' | 'emerald' | 'orange' | 'purple' | 'gray';
}> = ({ label, value, color }) => {
  const colorClasses: Record<string, string> = {
    red: 'text-red-600',
    rose: 'text-rose-600',
    green: 'text-green-600',
    emerald: 'text-emerald-600',
    blue: 'text-blue-600',
    orange: 'text-orange-600',
    purple: 'text-purple-600',
    gray: 'text-gray-600',
  };

  const valueColor = color ? colorClasses[color] || 'text-gray-900' : 'text-gray-900';

  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-4 flex flex-col items-center text-center">
      <div className="text-xs text-gray-500 mb-1">{label}</div>
      <div className={`text-xl font-bold ${valueColor}`}>{value}</div>
    </div>
  );
};

function formatPercent(value: number): string {
  return `${(value * 100).toFixed(2)}%`;
}
