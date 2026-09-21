/**
 * 策略对比页面
 * Strategy Comparison Page
 *
 * 完整的策略对比功能页面
 *
 * @author QuantMind Team
 * @date 2025-12-02
 */

import React, { useState, useMemo } from 'react';
import { Card, Button, Space, Alert, Empty, Divider, Tag } from 'antd';
import { ArrowLeftOutlined, DownloadOutlined, ReloadOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { StrategySelector } from '../components/StrategySelector';
import { ComparisonTable } from '../components/ComparisonTable';
import { ComparisonCharts } from '../components/ComparisonCharts';
import type { StrategyComparisonItem, BestPerformers, StrategyRanking } from '../../../shared/types/strategyComparison';
import { useAuth } from '../../auth/hooks';
import { useAppSelector } from '../../../store';
import { selectCurrentMarket } from '../../../store/slices/uiSlice';
import { getMarketConfig } from '../../../config/marketConfig';

/**
 * 策略对比页面组件
 */
export const StrategyComparisonPage: React.FC = () => {
  const navigate = useNavigate();
  const { user } = useAuth();
  const currentMarket = useAppSelector(selectCurrentMarket);
  const marketCfg = getMarketConfig(currentMarket);

  const [selectedStrategies, setSelectedStrategies] = useState<StrategyComparisonItem[]>([]);

  // 计算最佳表现者
  const bestPerformers = useMemo((): BestPerformers | null => {
    if (selectedStrategies.length < 2) return null;

    return {
      highest_return: selectedStrategies.reduce((best, s) =>
        s.performance.annual_return > (best?.performance.annual_return || -Infinity) ? s : best
      ).strategy_id,

      lowest_drawdown: selectedStrategies.reduce((best, s) =>
        Math.abs(s.performance.max_drawdown) < Math.abs(best?.performance.max_drawdown || Infinity) ? s : best
      ).strategy_id,

      best_sharpe: selectedStrategies.reduce((best, s) =>
        s.performance.sharpe_ratio > (best?.performance.sharpe_ratio || -Infinity) ? s : best
      ).strategy_id,

      best_win_rate: selectedStrategies.reduce((best, s) =>
        s.trading_stats.win_rate > (best?.trading_stats.win_rate || -Infinity) ? s : best
      ).strategy_id,

      lowest_volatility: selectedStrategies.reduce((best, s) =>
        s.performance.volatility < (best?.performance.volatility || Infinity) ? s : best
      ).strategy_id,

      best_calmar: selectedStrategies.reduce((best, s) =>
        s.performance.calmar_ratio > (best?.performance.calmar_ratio || -Infinity) ? s : best
      ).strategy_id,
    };
  }, [selectedStrategies]);

  // 计算策略排名
  const rankings = useMemo((): StrategyRanking[] => {
    if (selectedStrategies.length < 2) return [];

    // 简化的评分算法
    const scored = selectedStrategies.map(strategy => {
      const score = (
        strategy.performance.annual_return * 0.3 +
        (100 + strategy.performance.max_drawdown) * 0.2 +
        strategy.performance.sharpe_ratio * 20 * 0.3 +
        strategy.trading_stats.win_rate * 0.2
      );

      return {
        strategy_id: strategy.strategy_id,
        strategy_name: strategy.strategy_name,
        rank: 0, // 稍后填充
        score: Math.max(0, Math.min(100, score)),
        strengths: [],
        weaknesses: [],
        recommendation: '',
      };
    });

    // 按分数排序并分配排名
    scored.sort((a, b) => b.score - a.score);
    scored.forEach((item, index) => {
      item.rank = index + 1;
    });

    return scored;
  }, [selectedStrategies]);

  // 生成比较结论（**不是投资建议**：只陈述本页口径下的比较结果，不给适当性结论、
  // 不说「适合稳健型投资者」—— 我们不是持牌投顾，无权对投资者做适当性判断）
  const recommendations = useMemo((): string[] => {
    if (selectedStrategies.length < 2 || rankings.length === 0) return [];

    const recs: string[] = [];
    const topStrategy = rankings[0];

    recs.push(`${topStrategy.strategy_name} 综合评分最高（${topStrategy.score.toFixed(1)} 分，按本页比较口径计算）。`);

    // 高收益低风险特征的策略（仅陈述筛选口径，不做适当性判断）
    const balanced = selectedStrategies.filter(s =>
      s.performance.annual_return > 10 && Math.abs(s.performance.max_drawdown) < 15
    );

    if (balanced.length > 0) {
      recs.push(`${balanced.map(s => s.strategy_name).join('、')} 在本页口径下年化收益高于 10% 且最大回撤小于 15%（历史样本区间，不代表未来）。`);
    }

    // 风险警告
    const highRisk = selectedStrategies.filter(s =>
      Math.abs(s.performance.max_drawdown) > 20
    );

    if (highRisk.length > 0) {
      recs.push(`⚠️ ${highRisk.map(s => s.strategy_name).join('、')} 最大回撤超过20%，风险较高，请谨慎使用。`);
    }

    return recs;
  }, [selectedStrategies, rankings]);

  // 处理导出PDF
  const handleExportPDF = () => {
    // TODO: 实现PDF导出
    console.log('导出PDF功能待实现');
  };

  // 重置选择
  const handleReset = () => {
    setSelectedStrategies([]);
  };

  return (
    <div className="w-full h-full bg-[#f8fafc] p-6 overflow-hidden">
      <div className="w-full h-full bg-white border border-gray-200 shadow-md rounded-2xl overflow-hidden flex flex-col">
        <div className="flex-1 overflow-y-auto p-6">
      {/* 页面头部 */}
      <Card style={{ marginBottom: 24 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Space>
            <Button
              icon={<ArrowLeftOutlined />}
              onClick={() => navigate(-1)}
            >
              返回
            </Button>
            <h2 style={{ margin: 0 }}>策略对比分析</h2>
            <Tag color="blue" style={{ marginLeft: 8 }}>{marketCfg.label}</Tag>
          </Space>

          <Space>
            {selectedStrategies.length >= 2 && (
              <>
                <Button
                  icon={<DownloadOutlined />}
                  onClick={handleExportPDF}
                >
                  导出PDF
                </Button>
                <Button
                  icon={<ReloadOutlined />}
                  onClick={handleReset}
                >
                  重新选择
                </Button>
              </>
            )}
          </Space>
        </div>
      </Card>

      {/* 策略选择器 */}
      <Card title="选择对比策略" style={{ marginBottom: 24 }}>
        <StrategySelector
          selectedStrategies={selectedStrategies}
          onSelectionChange={setSelectedStrategies}
          maxCount={5}
          userId={String(user?.id || '')}
        />

        {selectedStrategies.length === 1 && (
          <Alert
            message="请再选择至少1个策略进行对比"
            type="info"
            showIcon
            style={{ marginTop: 16 }}
          />
        )}
      </Card>

      {/* 对比结果 */}
      {selectedStrategies.length >= 2 ? (
        <>
          {/* 比较结论（措辞避开「建议」：不构成投资建议，也不做投资者适当性判断） */}
          {recommendations.length > 0 && (
            <Card title="💡 比较结论（非投资建议）" style={{ marginBottom: 24 }}>
              <Space direction="vertical" style={{ width: '100%' }}>
                {recommendations.map((rec, index) => (
                  <Alert
                    key={index}
                    message={rec}
                    type={rec.includes('⚠️') ? 'warning' : 'success'}
                    showIcon
                  />
                ))}
              </Space>
            </Card>
          )}

          {/* 综合排名 */}
          <Card title="📊 综合排名" style={{ marginBottom: 24 }}>
            <Space direction="vertical" style={{ width: '100%' }} size="large">
              {rankings.map((ranking) => {
                const strategy = selectedStrategies.find(s => s.strategy_id === ranking.strategy_id);
                if (!strategy) return null;

                return (
                  <div
                    key={ranking.strategy_id}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      padding: 16,
                      backgroundColor: ranking.rank === 1 ? '#f6ffed' : '#fafafa',
                      borderRadius: 8,
                      border: ranking.rank === 1 ? '2px solid #52c41a' : '1px solid #d9d9d9',
                    }}
                  >
                    <div
                      style={{
                        fontSize: 24,
                        fontWeight: 'bold',
                        marginRight: 16,
                        color: ranking.rank === 1 ? '#52c41a' : '#595959',
                      }}
                    >
                      {ranking.rank === 1 ? '🏆' : `#${ranking.rank}`}
                    </div>

                    <div style={{ flex: 1 }}>
                      <div style={{ fontWeight: 500, fontSize: 16, marginBottom: 4 }}>
                        {ranking.strategy_name}
                      </div>
                      <Space size="small">
                        <span>年化收益：
                          <span style={{ color: '#52c41a', fontWeight: 500 }}>
                            {strategy.performance.annual_return.toFixed(2)}%
                          </span>
                        </span>
                        <Divider type="vertical" />
                        <span>夏普比率：
                          <span style={{ color: '#1890ff', fontWeight: 500 }}>
                            {strategy.performance.sharpe_ratio.toFixed(2)}
                          </span>
                        </span>
                        <Divider type="vertical" />
                        <span>最大回撤：
                          <span style={{ color: '#f5222d', fontWeight: 500 }}>
                            {strategy.performance.max_drawdown.toFixed(2)}%
                          </span>
                        </span>
                      </Space>
                    </div>

                    <div
                      style={{
                        fontSize: 20,
                        fontWeight: 'bold',
                        color: ranking.rank === 1 ? '#52c41a' : '#595959',
                      }}
                    >
                      {ranking.score.toFixed(1)}分
                    </div>
                  </div>
                );
              })}
            </Space>
          </Card>

          {/* 详细对比表格 */}
          <Card title="📋 详细对比" style={{ marginBottom: 24 }}>
            <ComparisonTable
              strategies={selectedStrategies}
              showBasicInfo={true}
            />
          </Card>

          {/* 图表对比 */}
          <Card title="📈 图表对比" style={{ marginBottom: 24 }}>
            <ComparisonCharts strategies={selectedStrategies} />
          </Card>
        </>
      ) : (
        <Card>
          <Empty
            description="请选择至少2个策略进行对比"
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          />
        </Card>
      )}
        </div>
      </div>
    </div>
  );
};

export default StrategyComparisonPage;
