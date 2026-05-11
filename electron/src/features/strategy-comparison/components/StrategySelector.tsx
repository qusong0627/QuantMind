/**
 * 策略选择器组件
 * Strategy Selector for Comparison
 *
 * 支持选择个人策略进行对比
 *
 * @author QuantMind Team
 * @date 2025-12-02
 */

import React, { useState, useEffect } from 'react';
import { Select, Tag, Space, Button, List, Card, Empty, Input, Spin } from 'antd';
import { PlusOutlined, CloseCircleOutlined, SearchOutlined } from '@ant-design/icons';
import type { StrategyComparisonItem } from '../../../shared/types/strategyComparison';

const { Search } = Input;

export interface StrategySelectorProps {
  /** 已选策略列表 */
  selectedStrategies: StrategyComparisonItem[];
  /** 选择变更回调 */
  onSelectionChange: (strategies: StrategyComparisonItem[]) => void;
  /** 最大选择数量 */
  maxCount?: number;
  /** 用户ID */
  userId: string;
}

interface StrategyListItem {
  id: string;
  name: string;
  type: string;
  annual_return?: number;
  sharpe_ratio?: number;
  created_at: string;
}

/**
 * 策略选择器组件
 */
export const StrategySelector: React.FC<StrategySelectorProps> = ({
  selectedStrategies,
  onSelectionChange,
  maxCount = 5,
  userId,
}) => {
  const [visible, setVisible] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [loading, setLoading] = useState(false);

  const [personalStrategies, setPersonalStrategies] = useState<StrategyListItem[]>([]);

  // 加载个人策略列表
  useEffect(() => {
    if (visible) {
      loadPersonalStrategies();
    }
  }, [visible]);

  const loadPersonalStrategies = async () => {
    setLoading(true);
    try {
      // TODO: 调用真实API
      // const response = await userCenterService.getStrategies(userId);

      // 模拟数据
      const mockStrategies: StrategyListItem[] = [
        {
          id: 'ps_001',
          name: '双均线交叉策略',
          type: 'CTA',
          annual_return: 15.6,
          sharpe_ratio: 1.38,
          created_at: '2025-01-15',
        },
        {
          id: 'ps_002',
          name: '动量策略',
          type: '趋势跟踪',
          annual_return: 12.3,
          sharpe_ratio: 1.15,
          created_at: '2025-02-01',
        },
        {
          id: 'ps_003',
          name: '均值回归策略',
          type: '均值回归',
          annual_return: 8.9,
          sharpe_ratio: 0.85,
          created_at: '2024-12-20',
        },
      ];

      setPersonalStrategies(mockStrategies);
    } catch (error) {
      console.error('加载个人策略失败:', error);
    } finally {
      setLoading(false);
    }
  };

  // 判断策略是否已选
  const isSelected = (strategyId: string): boolean => {
    return selectedStrategies.some(s => s.strategy_id === strategyId);
  };

  // 判断是否已达最大选择数
  const isMaxReached = (): boolean => {
    return selectedStrategies.length >= maxCount;
  };

  // 添加策略到对比列表
  const handleAddStrategy = async (strategy: StrategyListItem) => {
    if (isSelected(strategy.id) || isMaxReached()) {
      return;
    }

    // TODO: 调用API获取完整策略数据
    // const fullStrategy = await loadFullStrategyData(strategy.id);

    // 模拟完整数据
    const fullStrategy: StrategyComparisonItem = {
      strategy_id: strategy.id,
      strategy_name: strategy.name,
      strategy_type: strategy.type,
      source: 'personal',
      source_label: '个人策略',
      created_at: strategy.created_at,
      basic_info: {
        market: ['期货'],
        style: '日内',
        tags: [strategy.type],
      },
      performance: {
        total_return: (strategy.annual_return || 0) * 2.5,
        annual_return: strategy.annual_return || 0,
        monthly_avg_return: (strategy.annual_return || 0) / 12,
        max_drawdown: -12.3,
        volatility: 18.5,
        sharpe_ratio: strategy.sharpe_ratio || 0,
        sortino_ratio: (strategy.sharpe_ratio || 0) * 1.3,
        calmar_ratio: (strategy.annual_return || 0) / 12.3,
      },
      trading_stats: {
        total_trades: 156,
        win_rate: 58.2,
        profit_loss_ratio: 2.1,
        avg_holding_period: 3.5,
        max_consecutive_wins: 8,
        max_consecutive_losses: 4,
      },
      equity_curve: {
        dates: ['2024-01', '2024-06', '2024-12'],
        returns: [0, 15, (strategy.annual_return || 0) * 2.5],
      },
    };

    onSelectionChange([...selectedStrategies, fullStrategy]);
  };

  // 移除策略
  const handleRemoveStrategy = (strategyId: string) => {
    onSelectionChange(selectedStrategies.filter(s => s.strategy_id !== strategyId));
  };

  // 过滤策略列表
  const filterStrategies = (strategies: StrategyListItem[]): StrategyListItem[] => {
    if (!searchQuery) return strategies;

    const query = searchQuery.toLowerCase();
    return strategies.filter(s =>
      s.name.toLowerCase().includes(query) ||
      s.type.toLowerCase().includes(query)
    );
  };

  // 渲染策略列表项
  const renderStrategyItem = (strategy: StrategyListItem) => {
    const selected = isSelected(strategy.id);
    const disabled = !selected && isMaxReached();

    return (
      <List.Item
        key={strategy.id}
        style={{
          opacity: disabled ? 0.5 : 1,
          cursor: disabled ? 'not-allowed' : 'pointer',
        }}
      >
        <Card
          size="small"
          hoverable={!disabled}
          style={{ width: '100%' }}
          onClick={() => !selected && !disabled && handleAddStrategy(strategy)}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 500, marginBottom: 4 }}>
                {strategy.name}
                {selected && (
                  <Tag color="blue" style={{ marginLeft: 8 }}>已选</Tag>
                )}
              </div>
              <Space size="small">
                <Tag>{strategy.type}</Tag>
                {strategy.annual_return !== undefined && (
                  <span style={{ fontSize: 12, color: '#52c41a' }}>
                    年化 {strategy.annual_return.toFixed(2)}%
                  </span>
                )}
                {strategy.sharpe_ratio !== undefined && (
                  <span style={{ fontSize: 12, color: '#1890ff' }}>
                    夏普 {strategy.sharpe_ratio.toFixed(2)}
                  </span>
                )}
              </Space>
            </div>

            {selected && (
              <Button
                type="text"
                danger
                size="small"
                icon={<CloseCircleOutlined />}
                onClick={(e) => {
                  e.stopPropagation();
                  handleRemoveStrategy(strategy.id);
                }}
              >
                移除
              </Button>
            )}
          </div>
        </Card>
      </List.Item>
    );
  };

  return (
    <div className="strategy-selector">
      {/* 已选策略展示 */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ marginBottom: 8, fontSize: 14, color: '#595959' }}>
          已选策略 ({selectedStrategies.length}/{maxCount})：
        </div>
        <Space wrap>
          {selectedStrategies.map(strategy => (
            <Tag
              key={strategy.strategy_id}
              color="blue"
              closable
              onClose={() => handleRemoveStrategy(strategy.strategy_id)}
            >
              {strategy.strategy_name}
            </Tag>
          ))}

          {selectedStrategies.length < maxCount && (
            <Button
              type="dashed"
              size="small"
              icon={<PlusOutlined />}
              onClick={() => setVisible(!visible)}
            >
              添加策略
            </Button>
          )}
        </Space>

        {isMaxReached() && (
          <div style={{ marginTop: 8, fontSize: 12, color: '#faad14' }}>
            已达到最大选择数量 ({maxCount})
          </div>
        )}
      </div>

      {/* 策略选择面板 */}
      {visible && (
        <Card
          style={{ marginTop: 16 }}
          title="选择对比策略"
          extra={
            <Button size="small" onClick={() => setVisible(false)}>
              收起
            </Button>
          }
        >
          <Search
            placeholder="搜索策略..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{ marginBottom: 16 }}
            prefix={<SearchOutlined />}
            allowClear
          />

          <Spin spinning={loading}>
            {filterStrategies(personalStrategies).length === 0 ? (
              <Empty description="暂无个人策略" />
            ) : (
              <List
                dataSource={filterStrategies(personalStrategies)}
                renderItem={renderStrategyItem}
                style={{ maxHeight: 400, overflow: 'auto' }}
              />
            )}
          </Spin>
        </Card>
      )}
    </div>
  );
};

export default StrategySelector;
