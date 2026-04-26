import React, { useMemo } from 'react';
import { Card, Typography, List, Tag, Collapse, Empty, Space, Divider } from 'antd';
import { BulbOutlined, InfoCircleOutlined, ReadOutlined } from '@ant-design/icons';
import { useWizardStore } from '../store/wizardStore';
import { FACTORS } from '../factors/dictionary';

const { Text, Paragraph, Title } = Typography;

export const ContextAwareAssistant: React.FC<{ step: number }> = ({ step }) => {
  const { conditions, generated, pool, qlibParams } = useWizardStore();

  // 提取当前相关的因子
  const activeFactors = useMemo(() => {
    if (step !== 0) return [];

    // 简单的深度优先遍历提取条件中的因子
    const factors = new Set<string>();
    const traverse = (c: any) => {
      if (!c) return;
      if (c.factor) factors.add(c.factor);
      if (c.children) c.children.forEach(traverse);
    };
    traverse(conditions);

    return Array.from(factors).map(k => FACTORS.find(f => f.key === k)).filter(Boolean);
  }, [conditions, step]);

  const renderContent = () => {
    switch (step) {
      case 0: // 选股条件
        return (
          <Space direction="vertical" style={{ width: '100%' }}>
            <div style={{ background: '#e6f7ff', padding: 12, borderRadius: 6, border: '1px solid #91d5ff' }}>
              <Space align="start">
                <BulbOutlined style={{ color: '#1890ff', marginTop: 4 }} />
                <Text style={{ fontSize: 13 }}>
                  尝试输入组合条件，例如："低估值且高增长"，或具体指标 "PE &lt; 20 且 营收增长 &gt; 30%"。
                </Text>
              </Space>
            </div>

            {activeFactors.length > 0 ? (
              <Collapse
                ghost
                defaultActiveKey={['0']}
                size="small"
                items={activeFactors.map((f, i) => ({
                  key: String(i),
                  label: <Space><Tag color="blue">{f?.category}</Tag>{f?.label}</Space>,
                  children: (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {getFactorDescription(f?.key)}
                    </Text>
                  ),
                }))}
              />
            ) : (
              <div style={{ marginTop: 16 }}>
                 <Divider plain orientation="left" style={{ margin: '12px 0', fontSize: 12 }}>常用因子速查</Divider>
                 <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                   {FACTORS.slice(0, 8).map(f => (
                     <Tag key={f.key} style={{ cursor: 'pointer' }}>{f.label}</Tag>
                   ))}
                 </div>
              </div>
            )}
          </Space>
        );

      case 1: // 股票池
        return (
          <Space direction="vertical" style={{ width: '100%' }}>
            <div style={{ background: '#f6ffed', padding: 12, borderRadius: 6, border: '1px solid #b7eb8f' }}>
              <Space align="start">
                <InfoCircleOutlined style={{ color: '#52c41a', marginTop: 4 }} />
                <Text style={{ fontSize: 13 }}>
                  当前选出 <Text strong>{pool?.items?.length || 0}</Text> 只股票。
                  建议检查是否存在行业过度集中风险。
                </Text>
              </Space>
            </div>
          </Space>
        );

      case 2: // 交易规则
        const isTopkDropout = (qlibParams?.strategy_type ?? 'TopkDropout') === 'TopkDropout';
        return (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Card size="small" title="Qlib 参数提示" variant="borderless" styles={{ body: { padding: 12 } }}>
               <List size="small" dataSource={[
                 isTopkDropout
                   ? { title: 'TopK / n_drop', desc: '建议先用 TopK=20~50、n_drop=3~10 作为稳定起点。' }
                   : { title: 'TopK / 权重上限', desc: '权重策略建议先用 TopK=20~50，并结合 max_weight、min_score 控制集中度。' },
                 { title: '调仓与风控', desc: '调仓周期可先用 5 日；结合止损/止盈与仓位上限控制回撤。' }
               ]} renderItem={item => (
                 <List.Item>
                   <List.Item.Meta title={<Text style={{ fontSize: 12 }}>{item.title}</Text>} description={<Text type="secondary" style={{ fontSize: 11 }}>{item.desc}</Text>} />
                 </List.Item>
               )} />
            </Card>
          </Space>
        );

      case 3: // 生成
        return (
           <div style={{ background: '#fff7e6', padding: 12, borderRadius: 6, border: '1px solid #ffd591' }}>
              <Space align="start">
                <ReadOutlined style={{ color: '#fa8c16', marginTop: 4 }} />
                <Text style={{ fontSize: 13 }}>
                  生成代码后，您可以：
                  1. 点击“一键回测”验证效果
                  2. 复制简报到剪贴板
                  3. 导出为 Python 文件
                </Text>
              </Space>
            </div>
        );

      default:
        return null;
    }
  };

  return (
    <Card
      title={<Space><BulbOutlined style={{ color: '#faad14' }} /><span>智能助手</span></Space>}
      size="small"
      style={{ height: '100%', overflow: 'auto' }}
      variant="borderless"
    >
      {renderContent()}
    </Card>
  );
};

// 模拟因子描述库
function getFactorDescription(key?: string) {
  const map: Record<string, string> = {
    market_cap: '市值是指一家上市公司的发行股份按市场价格计算出来的股票总价值，是衡量公司规模的重要指标。',
    pe: '市盈率 (Price-to-Earnings Ratio) 是股票价格除以每股收益的比率，用于衡量估值水平。低PE通常代表价值股。',
    roe: '净资产收益率 (Return on Equity) 衡量公司运用自有资本的效率。巴菲特非常看重的指标，通常 >15% 为优秀。',
    volume: '成交量反映了市场的活跃程度。量价配合是技术分析的核心。',
    // ... 其他可以继续补充
  };
  return map[key || ''] || '暂无详细描述，请参考通用金融定义。';
}
