import React from 'react';
import { Form, Radio, Slider, Select, Space, Button, Typography, Divider } from 'antd';
import { SettingOutlined } from '@ant-design/icons';
import { useWizardStore } from '../store/wizardStore';
import { QLIB_REBALANCE_DAY_OPTIONS, resolveRebalanceDays, RebalanceDays } from '../../../shared/qlib/rebalance';

const { Title, Text } = Typography;

interface Props {
  onNext?: () => void;
  onBack?: () => void;
}

const QlibParamsConfig: React.FC<Props> = ({ onNext, onBack }) => {
  const { qlibParams, setQlibParams } = useWizardStore();
  const params = qlibParams ?? { strategy_type: 'TopkDropout', topk: 10, n_drop: 2, rebalance_days: 5 };
  const normalizedRebalanceDays = resolveRebalanceDays(params);

  const update = (patch: Partial<typeof params>) => {
    const nextParams = { ...params, ...patch };

    if (nextParams.strategy_type === 'TopkWeight') {
      delete nextParams.n_drop;
    } else if (typeof nextParams.n_drop !== 'number') {
      nextParams.n_drop = 2;
    }

    setQlibParams(nextParams);
  };

  return (
    <div style={{ maxWidth: 800, margin: '0 auto', padding: '0' }}>
      <Space>
        <SettingOutlined />
        <span style={{ fontSize: 16, fontWeight: 500 }}>Qlib 策略参数</span>
      </Space>
      <Form layout="vertical" style={{ marginTop: 8 }}>
        <Form.Item label={<Text strong>策略类型</Text>}>
          <Radio.Group
            value={params.strategy_type}
            onChange={(e) => update({ strategy_type: e.target.value })}
            optionType="button"
            buttonStyle="solid"
          >
            <Radio.Button value="TopkDropout">TopkDropoutStrategy（推荐）</Radio.Button>
            <Radio.Button value="TopkWeight">TopkWeightStrategy</Radio.Button>
          </Radio.Group>
          <div style={{ marginTop: 6 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {params.strategy_type === 'TopkDropout'
                ? 'TopK 选股 + 每期剔除最差 n_drop 只，持续轮换，适合动量策略'
                : '按因子得分加权持仓，适合多因子复合策略'}
            </Text>
          </div>
        </Form.Item>

        <Divider style={{ margin: '12px 0' }} />

        <Form.Item label={<Text strong>选股数量（TopK = {params.topk}）</Text>}>
          <Slider
            min={3} max={100} step={1}
            value={params.topk}
            onChange={(v) => update({ topk: v })}
            marks={{ 3: '3', 10: '10', 30: '30', 50: '50', 100: '100' }}
          />
        </Form.Item>

        <Form.Item label={<Text strong>调仓周期</Text>}>
          <Select
            value={normalizedRebalanceDays}
            onChange={(v: RebalanceDays) => update({ rebalance_days: v })}
            options={QLIB_REBALANCE_DAY_OPTIONS.map((item) => ({
              value: item.value,
              label: item.value === 5 ? `${item.label}（推荐）` : item.label,
            }))}
            style={{ width: 200 }}
          />
        </Form.Item>

        {params.strategy_type === 'TopkDropout' && (
          <Form.Item label={<Text strong>每期剔除数（n_drop = {params.n_drop}）</Text>}>
            <Slider
              min={0} max={50} step={1}
              value={params.n_drop}
              onChange={(v) => update({ n_drop: v })}
              marks={{ 0: '0(不限制)', 10: '10', 30: '30', 50: '50' }}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>每次调仓剔除因子得分最低的 n_drop 只股票。0表示不限制剔除，仅根据因子值和持仓配置进行动态资金分配</Text>
          </Form.Item>
        )}
      </Form>

      <Space style={{ marginTop: 16 }}>
        {onBack && <Button onClick={onBack}>上一步</Button>}
        {onNext && <Button type="primary" onClick={onNext}>下一步</Button>}
      </Space>
    </div>
  );
};

export default QlibParamsConfig;
