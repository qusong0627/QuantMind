import React, { useState, useEffect } from 'react';
import { Card, Button, Select, InputNumber, Space, Typography, Row, Col, Tag, Empty, message } from 'antd';
import { PlusOutlined, DeleteOutlined, InfoCircleOutlined } from '@ant-design/icons';
import { useWizardV2Store } from '../store/wizardV2Store';
import type { Condition } from '../types';
import { FACTORS } from '../factors/dictionary';

const { Text } = Typography;

// 扁平化的条件类型，对用户隐藏嵌套逻辑
interface FlatCondition {
  id: string;
  factor: string;
  operator: string;
  value: number;
}

const operators = [
  { label: '大于', value: '>' },
  { label: '大于等于', value: '>=' },
  { label: '小于', value: '<' },
  { label: '小于等于', value: '<=' },
  { label: '等于', value: '==' },
];

// 限制仅显示的5个因子
const ALLOWED_FACTOR_KEYS = ['market_cap', 'float_mv', 'pe', 'pb', 'roe'];

const factorOptions = FACTORS.filter(f => ALLOWED_FACTOR_KEYS.includes(f.key)).map(f => ({
  label: f.label,
  value: f.key,
  unit: f.unit
}));

// 获取因子的单位
const getFactorUnit = (factorKey: string): string | undefined => {
  const factor = factorOptions.find(f => f.value === factorKey);
  return factor?.unit;
};

export const SimpleLogicBuilder: React.FC<{
  onChange?: (c: Condition) => void;
}> = ({ onChange }) => {
  const { conditions, setConditions } = useWizardV2Store();
  
  // 内部维护一个扁平数组，提交时转换为 AND 逻辑的 CompositeCondition
  const [flatConditions, setFlatConditions] = useState<FlatCondition[]>([]);

  // 从 Store 同步初始化 (仅支持顶层是 AND 且只有一层的简单结构回显)
  useEffect(() => {
    if (conditions && conditions.type === 'composite' && conditions.op === 'AND' && Array.isArray(conditions.children)) {
      const simple = conditions.children.map((c: any) => {
        if (c.type === 'numeric') {
          if (!ALLOWED_FACTOR_KEYS.includes(c.factor)) {
            return null;
          }
          return {
            id: Math.random().toString(36).slice(2),
            factor: c.factor,
            operator: c.operator,
            value: c.threshold
          };
        }
        return null;
      }).filter(Boolean) as FlatCondition[];
      if (simple.length > 0) {
        setFlatConditions(simple);
      }
    }
  }, []); // 仅挂载时同步一次

  const updateStore = (list: FlatCondition[]) => {
    setFlatConditions(list);

    // 转换为标准 Condition 结构 (默认为 AND 关系)
    const composite: Condition = {
      type: 'composite',
      op: 'AND',
      children: list.map(item => ({
        type: 'numeric',
        factor: item.factor,
        operator: item.operator as any,
        threshold: item.value
      }))
    };

    setConditions(composite);
    if (onChange) onChange(composite);
  };

  const addCondition = () => {
    if (flatConditions.length >= 5) {
      message.warning('最多只能添加 5 个筛选条件');
      return;
    }
    const newItem: FlatCondition = {
      id: Math.random().toString(36).slice(2),
      factor: 'market_cap',
      operator: '>',
      value: 0
    };
    updateStore([...flatConditions, newItem]);
  };

  const removeCondition = (id: string) => {
    updateStore(flatConditions.filter(c => c.id !== id));
  };

  const updateCondition = (id: string, field: keyof FlatCondition, val: any) => {
    const newList = flatConditions.map(c => c.id === id ? { ...c, [field]: val } : c);
    updateStore(newList);
  };

  return (
    <div style={{ padding: 0 }}>
      {flatConditions.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={<Text type="secondary">暂无筛选条件</Text>}
        >
          <Button type="primary" icon={<PlusOutlined />} onClick={addCondition}>添加第一个条件</Button>
        </Empty>
      ) : (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          {flatConditions.map((item, index) => {
            const unit = getFactorUnit(item.factor);
            return (
              <Card
                key={item.id}
                size="small"
                styles={{ body: { padding: 12 } }}
                variant="borderless"
                style={{ background: '#f9f9f9', border: '1px solid #f0f0f0' }}
              >
                <Row gutter={12} align="middle">
                  <Col flex="30px">
                    <Tag color="blue">{index + 1}</Tag>
                  </Col>
                  <Col flex="auto">
                    <Space wrap>
                      <Select
                        value={item.factor}
                        style={{ width: 180 }}
                        onChange={(v) => updateCondition(item.id, 'factor', v)}
                        options={factorOptions}
                        showSearch
                        optionFilterProp="label"
                        placeholder="选择因子"
                      />
                      <Select
                        value={item.operator}
                        style={{ width: 100 }}
                        onChange={(v) => updateCondition(item.id, 'operator', v)}
                        options={operators}
                      />
                      <InputNumber
                        value={item.value}
                        style={{ width: 120 }}
                        onChange={(v) => updateCondition(item.id, 'value', v)}
                        placeholder="数值"
                      />
                      {unit && <Text type="secondary" style={{ fontSize: 12 }}>{unit}</Text>}
                    </Space>
                  </Col>
                  <Col flex="40px" style={{ textAlign: 'right' }}>
                    <Button type="text" danger icon={<DeleteOutlined />} onClick={() => removeCondition(item.id)} />
                  </Col>
                </Row>
              </Card>
            );
          })}

          <Button 
            type="dashed" 
            block 
            icon={<PlusOutlined />} 
            onClick={addCondition} 
            style={{ height: 48 }}
            disabled={flatConditions.length >= 5}
          >
            {flatConditions.length >= 5 ? '已达到最大条件数量 (5)' : '添加筛选条件'}
          </Button>

          <div style={{ marginTop: 16, padding: 12, background: '#e6f7ff', borderRadius: 4, border: '1px solid #91d5ff' }}>
            <Space align="start">
              <InfoCircleOutlined style={{ color: '#1890ff', marginTop: 4 }} />
              <Text type="secondary" style={{ fontSize: 12 }}>
                当前所有条件均为 <Text strong>且 (AND)</Text> 关系，即股票必须同时满足上述所有条件才会被选中。
              </Text>
            </Space>
          </div>
        </Space>
      )}
    </div>
  );
};
