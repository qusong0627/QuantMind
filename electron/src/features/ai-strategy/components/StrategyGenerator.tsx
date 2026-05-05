/**
 * AI策略生成器组件
 * 重构后的AI策略生成界面
 */

import React, { useState, useCallback, useEffect } from 'react';
import {
  Card,
  Form,
  Input,
  Select,
  Button,
  Steps,
  Row,
  Col,
  Typography,
  Space,
  Divider,
  Alert,
  Progress,
  Tabs,
  Tag,
  Tooltip,
} from 'antd';
import {
  RobotOutlined,
  CodeOutlined,
  BarChartOutlined,
  SettingOutlined,
  PlayCircleOutlined,
  SaveOutlined,
  ShareAltOutlined,
} from '@ant-design/icons';

import { useAppDispatch, useAppSelector } from '../../../store';
import {
  generateStrategy,
  saveStrategy,
  addToHistory,
  clearError,
  AIStrategyParams,
} from '../../../store/slices/aiStrategySlice';
import {
  AIStrategyGenerationParams,
  ParameterPanelConfig,
  StrategyDisplayConfig,
} from '../types/strategy.types';
// import { ParameterPanel } from './ParameterPanel';
// import { StrategyViewer } from './StrategyViewer';
// import { StrategyPreview } from './StrategyPreview';

const { Title, Paragraph, Text } = Typography;
const { Step } = Steps;
const { TabPane } = Tabs;
const { TextArea } = Input;

interface StrategyGeneratorProps {
  onStrategyGenerated?: (strategy: any) => void;
  initialParams?: Partial<AIStrategyGenerationParams>;
}

export const StrategyGenerator: React.FC<StrategyGeneratorProps> = ({
  onStrategyGenerated,
  initialParams,
}) => {
  const dispatch = useAppDispatch();
  const {
    isGenerating,
    isSaving,
    currentStrategy,
    error,
    generationHistory,
  } = useAppSelector((state) => state.aiStrategy);

  const [form] = Form.useForm();
  const [currentStep, setCurrentStep] = useState(0);
  const [params, setParams] = useState<AIStrategyGenerationParams>({
    description: '',
    marketType: 'stock',
    riskPreference: 'moderate',
    investmentStyle: 'balanced',
    timeframe: 'daily',
    style: 'custom',
    symbols: initialParams?.symbols || [],
    positionSize: 0.1,
    ...initialParams,
  });
  const [displayConfig, setDisplayConfig] = useState<StrategyDisplayConfig>({
    showCode: true,
    showParameters: true,
    showPerformance: true,
    showChart: false,
    layout: 'vertical',
  });

  // 参数面板配置
  const parameterConfig: ParameterPanelConfig = {
    title: '策略参数配置',
    description: '请填写策略生成的相关参数',
    fields: [
      {
        name: 'description',
        label: '策略描述',
        type: 'textarea',
        required: true,
        placeholder: '请详细描述您想要的策略，例如：股价突破20日均线且放量的趋势跟踪策略...',
        validation: [
          { type: 'required', message: '请输入策略描述' },
          { type: 'minLength', value: 10, message: '策略描述至少需要10个字符' },
        ],
      },
      {
        name: 'marketType',
        label: '市场类型',
        type: 'select',
        required: true,
        defaultValue: 'stock',
        options: [
          { label: '股票市场', value: 'stock' },
          { label: '期货市场', value: 'futures' },
          { label: '外汇市场', value: 'forex' },
          { label: '加密货币', value: 'crypto' },
        ],
      },
      {
        name: 'riskPreference',
        label: '风险偏好',
        type: 'select',
        required: true,
        defaultValue: 'moderate',
        options: [
          { label: '保守型', value: 'conservative' },
          { label: '稳健型', value: 'moderate' },
          { label: '激进型', value: 'aggressive' },
        ],
      },
      {
        name: 'investmentStyle',
        label: '投资风格',
        type: 'select',
        required: true,
        defaultValue: 'balanced',
        options: [
          { label: '价值投资', value: 'value' },
          { label: '成长投资', value: 'growth' },
          { label: '均衡配置', value: 'balanced' },
          { label: '技术分析', value: 'technical' },
        ],
      },
      {
        name: 'timeframe',
        label: '时间周期',
        type: 'select',
        required: true,
        defaultValue: 'daily',
        options: [
          { label: '日内交易', value: 'intraday' },
          { label: '日线', value: 'daily' },
          { label: '周线', value: 'weekly' },
          { label: '月线', value: 'monthly' },
        ],
      },
      {
        name: 'initialCapital',
        label: '初始资金',
        type: 'number',
        defaultValue: 100000,
        min: 1000,
        step: 10000,
      },
      {
        name: 'maxPositions',
        label: '最大持仓数',
        type: 'number',
        defaultValue: 5,
        min: 1,
        max: 20,
      },
      {
        name: 'stopLoss',
        label: '止损比例 (%)',
        type: 'range',
        defaultValue: 5,
        min: 1,
        max: 20,
        step: 0.5,
      },
      {
        name: 'takeProfit',
        label: '止盈比例 (%)',
        type: 'range',
        defaultValue: 15,
        min: 5,
        max: 50,
        step: 1,
      },
    ],
  };

  // 处理参数变化
  const handleParamsChange = useCallback((newParams: Partial<AIStrategyGenerationParams>) => {
    (setParams as any)(prev => ({ ...prev, ...newParams }));
  }, []);

  // 生成策略
  const handleGenerate = useCallback(async () => {
    try {
      dispatch(clearError());
      dispatch(addToHistory(params as any));

      const result = await dispatch(generateStrategy(params as any)).unwrap();

      if (onStrategyGenerated) {
        onStrategyGenerated(result);
      }

      setCurrentStep(2); // 跳转到结果页
    } catch (error) {
      console.error('策略生成失败:', error);
    }
  }, [dispatch, params, onStrategyGenerated]);

  // 保存策略
  const handleSave = useCallback(async () => {
    if (!currentStrategy) return;

    try {
      await dispatch(saveStrategy(currentStrategy)).unwrap();
      // 可以显示保存成功的提示
    } catch (error) {
      console.error('策略保存失败:', error);
    }
  }, [dispatch, currentStrategy]);

  // 重新生成
  const handleRegenerate = useCallback(() => {
    setCurrentStep(0);
    dispatch(clearError());
  }, [dispatch]);

  // 使用历史参数
  const useHistoryParams = useCallback((historyParams: AIStrategyGenerationParams | AIStrategyParams) => {
    // 如果是 AIStrategyParams，转换为 AIStrategyGenerationParams
    if ('market' in historyParams) {
      const convertedParams: AIStrategyGenerationParams = {
        description: historyParams.description || '',
        marketType: 'stock',
        riskPreference: historyParams.riskLevel === 'low' ? 'conservative' :
                      historyParams.riskLevel === 'high' ? 'aggressive' : 'moderate',
        investmentStyle: 'balanced',
        timeframe: 'daily',
        style: historyParams.style || 'custom',
        symbols: historyParams.symbols || [],
        initialCapital: 100000,
        positionSize: 0.1,
        maxPositions: 5,
        stopLoss: 5,
        takeProfit: 20,
      };
      setParams(convertedParams);
      form.setFieldsValue(convertedParams);
    } else {
      setParams(historyParams as AIStrategyGenerationParams);
      form.setFieldsValue(historyParams);
    }
  }, [form]);

  // 步骤配置
  const steps = [
    {
      title: '参数配置',
      description: '配置策略生成参数',
      icon: <SettingOutlined />,
    },
    {
      title: '生成中',
      description: 'AI正在生成策略',
      icon: <RobotOutlined />,
    },
    {
      title: '生成结果',
      description: '查看和编辑生成的策略',
      icon: <CodeOutlined />,
    },
  ];

  return (
    <div className="strategy-generator">
      <Card title="AI策略生成器" className="generator-card">
        <Steps current={currentStep} items={steps} className="generator-steps" />

        <Divider />

        {error && (
          <Alert
            message="生成失败"
            description={error}
            type="error"
            closable
            onClose={() => dispatch(clearError())}
            className="error-alert"
          />
        )}

        {currentStep === 0 && (
          <div className="parameter-step">
            <Row gutter={24}>
              <Col span={16}>
                {/* <ParameterPanel
                  config={parameterConfig}
                  values={params}
                  onChange={handleParamsChange}
                /> */}

                <div className="step-actions">
                  <Space>
                    <Button
                      type="primary"
                      size="large"
                      icon={<RobotOutlined />}
                      onClick={handleGenerate}
                      loading={isGenerating}
                      disabled={!params.description || params.description.length < 10}
                    >
                      开始生成策略
                    </Button>

                    {generationHistory.length > 0 && (
                      <Button
                        icon={<BarChartOutlined />}
                        onClick={() => setCurrentStep(3)}
                      >
                        查看历史记录
                      </Button>
                    )}
                  </Space>
                </div>
              </Col>

              <Col span={8}>
                <Card title="参数说明" size="small" className="help-card">
                  <Paragraph type="secondary">
                    <Text strong>策略描述：</Text>
                    详细说明您想要的策略逻辑、交易信号、风险管理等。
                  </Paragraph>
                  <Paragraph type="secondary">
                    <Text strong>风险偏好：</Text>
                    保守型注重本金安全，激进型追求高收益。
                  </Paragraph>
                  <Paragraph type="secondary">
                    <Text strong>投资风格：</Text>
                    影响策略的交易频率和分析方法。
                  </Paragraph>
                </Card>

                {generationHistory.length > 0 && (
                  <Card title="历史记录" size="small" className="history-card">
                    {generationHistory.slice(0, 3).map((history, index) => (
                      <div key={index} className="history-item">
                        <Text ellipsis style={{ width: '100%' }}>
                          {history.description}
                        </Text>
                        <Button
                          type="link"
                          size="small"
                          onClick={() => useHistoryParams(history)}
                        >
                          使用
                        </Button>
                      </div>
                    ))}
                  </Card>
                )}
              </Col>
            </Row>
          </div>
        )}

        {currentStep === 1 && (
          <div className="generating-step">
            <div className="generating-content">
              <div className="generating-icon">
                <RobotOutlined style={{ fontSize: 64, color: '#1890ff' }} />
              </div>

              <Title level={3}>AI正在生成策略</Title>
              <Paragraph type="secondary">
                根据您的参数，AI正在分析市场特征并生成最优策略...
              </Paragraph>

              <Progress
                percent={isGenerating ? 60 : 100}
                status={isGenerating ? 'active' : 'success'}
                className="generating-progress"
              />

              <div className="generating-tips">
                <Tag color="blue">分析市场特征</Tag>
                <Tag color="green">优化策略参数</Tag>
                <Tag color="orange">生成交易代码</Tag>
              </div>

              <div className="step-actions">
                <Button onClick={handleRegenerate} disabled={isGenerating}>
                  重新配置
                </Button>
              </div>
            </div>
          </div>
        )}

        {currentStep === 2 && currentStrategy && (
          <div className="result-step">
            <div className="result-header">
              <Space>
                <Title level={3}>策略生成完成</Title>
                <Tag color="success">就绪</Tag>
              </Space>

              <Space>
                <Button
                  type="primary"
                  icon={<SaveOutlined />}
                  onClick={handleSave}
                  loading={isSaving}
                >
                  保存策略
                </Button>

                <Button icon={<ShareAltOutlined />}>
                  分享策略
                </Button>

                <Button icon={<PlayCircleOutlined />}>
                  运行回测
                </Button>

                <Button onClick={handleRegenerate}>
                  重新生成
                </Button>
              </Space>
            </div>

            <Tabs defaultActiveKey="code" className="result-tabs">
              <TabPane tab="策略代码" key="code">
                {/* <StrategyViewer
                  strategy={currentStrategy}
                  displayConfig={displayConfig}
                  onDisplayConfigChange={setDisplayConfig}
                /> */}
              </TabPane>

              <TabPane tab="策略预览" key="preview">
                {/* <StrategyPreview strategy={currentStrategy} /> */}
              </TabPane>

              <TabPane tab="参数详情" key="parameters">
                <Card title="策略参数">
                  <pre>{JSON.stringify(params, null, 2)}</pre>
                </Card>
              </TabPane>
            </Tabs>
          </div>
        )}

        {currentStep === 3 && (
          <div className="history-step">
            <Title level={3}>历史生成记录</Title>

            <div className="history-list">
              {generationHistory.map((history, index) => (
                <Card
                  key={index}
                  size="small"
                  className="history-item-card"
                  extra={
                    <Button
                      type="link"
                      onClick={() => {
                        useHistoryParams(history as any);
                        setCurrentStep(0);
                      }}
                    >
                      使用此配置
                    </Button>
                  }
                >
                  <div className="history-content">
                    <Text strong>策略描述：</Text>
                    <Paragraph ellipsis={{ rows: 2 }}>
                      {history.description}
                    </Paragraph>

                    <Space>
                      <Tag>{(history as any).marketType}</Tag>
                      <Tag>{(history as any).riskPreference}</Tag>
                      <Tag>{(history as any).investmentStyle}</Tag>
                    </Space>
                  </div>
                </Card>
              ))}
            </div>

            <div className="step-actions">
              <Button type="primary" onClick={() => setCurrentStep(0)}>
                返回配置
              </Button>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
};

export default StrategyGenerator;
