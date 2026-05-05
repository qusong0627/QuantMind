/**
 * 增强版AI策略生成器 - 集成股票池功能
 * 支持完整的参数配置和股票池选择
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
  InputNumber,
  Switch,
  Slider,
  message,
  Collapse,
} from 'antd';
import {
  RobotOutlined,
  CodeOutlined,
  BarChartOutlined,
  SettingOutlined,
  PlayCircleOutlined,
  SaveOutlined,
  ShareAltOutlined,
  ThunderboltOutlined,
  StarOutlined,
  ExperimentOutlined,
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
} from '../types/strategy.types';
import { StockPoolSelector } from './StockPoolSelector';

const { Title, Paragraph, Text } = Typography;
const { Step } = Steps;
const { TabPane } = Tabs;
const { TextArea } = Input;
const { Panel } = Collapse;

interface EnhancedStrategyGeneratorProps {
  onStrategyGenerated?: (strategy: any) => void;
  initialParams?: Partial<AIStrategyGenerationParams>;
}

export const EnhancedStrategyGenerator: React.FC<EnhancedStrategyGeneratorProps> = ({
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
  const [activeTab, setActiveTab] = useState('basic');
  const [generationProgress, setGenerationProgress] = useState(0);

  // 表单值状态
  const [formValues, setFormValues] = useState<AIStrategyGenerationParams>({
    description: '',
    marketType: 'stock',
    riskPreference: 'moderate',
    investmentStyle: 'balanced',
    timeframe: 'daily',
    style: 'custom',
    symbols: [],
    initialCapital: 100000,
    positionSize: 0.1,
    maxPositions: 5,
    stopLoss: 5,
    takeProfit: 20,
    strategyLength: 'unlimited',
    backtestPeriod: '1year',
    ...initialParams,
  });

  // 监听生成状态，更新进度条
  useEffect(() => {
    if (isGenerating) {
      // 模拟进度增长
      const interval = setInterval(() => {
        (setGenerationProgress as any)((prev) => {
          if (prev >= 90) return prev;
          return prev + 10;
        });
      }, 500);
      return () => clearInterval(interval);
    } else {
      setGenerationProgress(0);
    }
  }, [isGenerating]);

  // 处理表单值变化
  const handleFormChange = useCallback((changedValues: any, allValues: any) => {
    setFormValues({ ...formValues, ...allValues });
  }, [formValues]);

  // 股票池变化处理
  const handleSymbolsChange = useCallback((symbols: string[]) => {
    (setFormValues as any)(prev => ({
      ...prev,
      symbols,
      symbolsCount: symbols.length,
    }));
    form.setFieldsValue({ symbols });
  }, [form]);

  // 生成策略
  const handleGenerate = useCallback(async () => {
    try {
      await form.validateFields();

      // 验证股票池
      if (formValues.symbols && formValues.symbols.length === 0) {
        message.warning('请先添加股票到股票池');
        setActiveTab('stockPool');
        return;
      }

      // 构建完整参数
      const params: AIStrategyGenerationParams = {
        ...formValues,
        symbols: formValues.symbols || [],
      };

      console.log('生成策略参数：', params);

      // 转换为AIStrategyParams格式
      const aiStrategyParams: AIStrategyParams = {
        description: params.description,
        market: 'CN', // 默认市场
        riskLevel: params.riskPreference === 'conservative' ? 'low' :
                  params.riskPreference === 'aggressive' ? 'high' : 'medium',
        style: params.style,
        symbols: params.symbols || [],
        timeframe: '1d', // 默认时间框架
        strategyLength: params.strategyLength || 'unlimited',
        backtestPeriod: params.backtestPeriod || '1year',
        initialCapital: params.initialCapital || 100000,
        positionSize: params.positionSize || 0.1,
        maxPositions: params.maxPositions || 5,
        stopLoss: params.stopLoss || 5,
        takeProfit: params.takeProfit || 20,
        stockPoolConfig: params.symbols ? { symbols: params.symbols } : undefined,
        framework: params.framework,
        outputFormat: params.outputFormat,
      };

      // 调用Redux action生成策略
      const result = await dispatch(generateStrategy(aiStrategyParams)).unwrap();

      setGenerationProgress(100);
      message.success('策略生成成功！');

      if (onStrategyGenerated) {
        onStrategyGenerated(result);
      }

      // 切换到下一步
      setCurrentStep(1);
    } catch (error: any) {
      console.error('生成策略失败：', error);
      message.error(error.message || '策略生成失败，请检查参数');
      setGenerationProgress(0);
    }
  }, [form, formValues, dispatch, onStrategyGenerated]);

  // 保存策略
  const handleSave = useCallback(async () => {
    if (!currentStrategy) {
      message.error('没有可保存的策略');
      return;
    }

    try {
      await dispatch(saveStrategy(currentStrategy)).unwrap();
      message.success('策略保存成功！');
    } catch (error: any) {
      console.error('保存策略失败：', error);
      message.error(error.message || '策略保存失败');
    }
  }, [currentStrategy, dispatch]);

  // 重新生成
  const handleRegenerate = useCallback(() => {
    setCurrentStep(0);
    setGenerationProgress(0);
    dispatch(clearError());
  }, [dispatch]);

  return (
    <div style={{ padding: '24px', maxWidth: 1400, margin: '0 auto' }}>
      {/* 页面标题 */}
      <div style={{ marginBottom: 24 }}>
        <Title level={2}>
          <RobotOutlined /> AI策略生成器
        </Title>
        <Paragraph type="secondary">
          通过简单配置，让AI帮你生成专业的量化交易策略
        </Paragraph>
      </div>

      {/* 进度步骤 */}
      <Steps current={currentStep} style={{ marginBottom: 32 }}>
        <Step title="配置参数" icon={<SettingOutlined />} />
        <Step title="生成策略" icon={<RobotOutlined />} />
        <Step title="查看结果" icon={<CodeOutlined />} />
      </Steps>

      {/* 错误提示 */}
      {error && (
        <Alert
          message="生成失败"
          description={error}
          type="error"
          closable
          onClose={() => dispatch(clearError())}
          style={{ marginBottom: 16 }}
        />
      )}

      {/* 主要内容区域 */}
      {currentStep === 0 && (
        <Card>
          <Form
            form={form}
            layout="vertical"
            initialValues={formValues}
            onValuesChange={handleFormChange}
          >
            <Tabs activeKey={activeTab} onChange={setActiveTab}>
              {/* 基础配置 */}
              <TabPane
                tab={
                  <span>
                    <SettingOutlined />
                    基础配置
                  </span>
                }
                key="basic"
              >
                <Row gutter={16}>
                  <Col span={24}>
                    <Form.Item
                      name="description"
                      label="策略描述"
                      rules={[
                        { required: true, message: '请输入策略描述' },
                        { min: 10, message: '策略描述至少需要10个字符' },
                      ]}
                      extra="详细描述您想要的策略逻辑、指标和交易规则"
                    >
                      <TextArea
                        rows={4}
                        placeholder="请描述你想要的策略，例如：股价突破20日均线且成交量放大时买入，跌破10日均线卖出..."
                        showCount
                        maxLength={1000}
                      />
                    </Form.Item>
                  </Col>

                  <Col span={8}>
                    <Form.Item
                      name="marketType"
                      label="市场类型"
                      rules={[{ required: true }]}
                    >
                      <Select>
                        <Select.Option value="stock">股票市场</Select.Option>
                        <Select.Option value="futures">期货市场</Select.Option>
                        <Select.Option value="forex">外汇市场</Select.Option>
                        <Select.Option value="crypto">加密货币</Select.Option>
                      </Select>
                    </Form.Item>
                  </Col>

                  <Col span={8}>
                    <Form.Item
                      name="riskPreference"
                      label="风险偏好"
                      rules={[{ required: true }]}
                    >
                      <Select>
                        <Select.Option value="conservative">保守型</Select.Option>
                        <Select.Option value="moderate">稳健型</Select.Option>
                        <Select.Option value="aggressive">激进型</Select.Option>
                      </Select>
                    </Form.Item>
                  </Col>

                  <Col span={8}>
                    <Form.Item
                      name="investmentStyle"
                      label="投资风格"
                      rules={[{ required: true }]}
                    >
                      <Select>
                        <Select.Option value="value">价值投资</Select.Option>
                        <Select.Option value="growth">成长投资</Select.Option>
                        <Select.Option value="balanced">均衡配置</Select.Option>
                        <Select.Option value="technical">技术分析</Select.Option>
                      </Select>
                    </Form.Item>
                  </Col>

                  <Col span={8}>
                    <Form.Item
                      name="timeframe"
                      label="时间周期"
                      rules={[{ required: true }]}
                    >
                      <Select>
                        <Select.Option value="intraday">日内交易</Select.Option>
                        <Select.Option value="daily">日线交易</Select.Option>
                        <Select.Option value="weekly">周线交易</Select.Option>
                        <Select.Option value="monthly">月线交易</Select.Option>
                      </Select>
                    </Form.Item>
                  </Col>

                  <Col span={8}>
                    <Form.Item
                      name="strategyType"
                      label="策略类型"
                    >
                      <Select placeholder="请选择策略类型（可选）">
                        <Select.Option value="trend_following">趋势跟踪</Select.Option>
                        <Select.Option value="mean_reversion">均值回归</Select.Option>
                        <Select.Option value="arbitrage">套利策略</Select.Option>
                        <Select.Option value="market_making">做市策略</Select.Option>
                      </Select>
                    </Form.Item>
                  </Col>

                  <Col span={8}>
                    <Form.Item
                      name="strategyLength"
                      label="策略周期"
                    >
                      <Select>
                        <Select.Option value="short_term">短期（&lt;3个月）</Select.Option>
                        <Select.Option value="medium_term">中期（3-12个月）</Select.Option>
                        <Select.Option value="long_term">长期（&gt;1年）</Select.Option>
                        <Select.Option value="unlimited">不限</Select.Option>
                      </Select>
                    </Form.Item>
                  </Col>
                </Row>
              </TabPane>

              {/* 股票池配置 */}
              <TabPane
                tab={
                  <span>
                    <StarOutlined />
                    股票池
                    {formValues.symbols && formValues.symbols.length > 0 && (
                      <Tag color="blue" style={{ marginLeft: 8 }}>
                        {formValues.symbols.length}
                      </Tag>
                    )}
                  </span>
                }
                key="stockPool"
              >
                <Form.Item
                  name="symbols"
                  label={null}
                  extra="选择要进行策略交易的股票池，支持手动添加、批量导入和预设板块"
                >
                  <StockPoolSelector
                    value={formValues.symbols}
                    onChange={handleSymbolsChange}
                    maxSymbols={50}
                    market={formValues.marketType === 'stock' ? 'CN' : 'US'}
                    showRecommendations={true}
                  />
                </Form.Item>
              </TabPane>

              {/* 高级参数 */}
              <TabPane
                tab={
                  <span>
                    <ExperimentOutlined />
                    高级参数
                  </span>
                }
                key="advanced"
              >
                <Collapse
                  defaultActiveKey={['capital']}
                  items={[
                    {
                      key: 'capital',
                      label: <Space><SettingOutlined /><span>资金与仓位控制</span></Space>,
                      children: (
                        <Row gutter={24}>
                          <Col span={12}>
                            <Form.Item label="初始资金 (CNY)" name="initialCapital" tooltip="策略回测及模拟运行的初始虚拟本金">
                              <InputNumber
                                style={{ width: '100%' }}
                                min={1000}
                                step={10000}
                                formatter={(value) => `${value}`.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}
                              />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item label="单仓比例" name="positionSize" tooltip="每只股票买入时占总资产的上限比例">
                              <Slider
                                min={0.01}
                                max={1}
                                step={0.01}
                                marks={{ 0.1: '10%', 0.2: '20%', 0.5: '50%', 1: '100%' }}
                                tooltip={{ formatter: (v) => `${(v || 0) * 100}%` }}
                              />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item label="最大持仓数" name="maxPositions">
                              <InputNumber min={1} max={100} style={{ width: '100%' }} />
                            </Form.Item>
                          </Col>
                        </Row>
                      ),
                    },
                    {
                      key: 'risk',
                      label: <Space><ThunderboltOutlined /><span>止盈止损 (风控)</span></Space>,
                      children: (
                        <Row gutter={24}>
                          <Col span={12}>
                            <Form.Item label="止损比例 (%)" name="stopLoss">
                              <Slider
                                min={1}
                                max={20}
                                marks={{ 5: '5%', 10: '10%', 15: '15%' }}
                                tooltip={{ formatter: (v) => `${v}%` }}
                              />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item label="止盈比例 (%)" name="takeProfit">
                              <Slider
                                min={5}
                                max={100}
                                marks={{ 10: '10%', 20: '20%', 50: '50%' }}
                                tooltip={{ formatter: (v) => `${v}%` }}
                              />
                            </Form.Item>
                          </Col>
                        </Row>
                      ),
                    },
                    {
                      key: 'cost',
                      label: <span>交易成本</span>,
                      children: (
                        <Row gutter={16}>
                          <Col span={12}>
                            <Form.Item
                              name="commissionRate"
                              label="手续费率"
                              extra="交易手续费率（0-1之间的小数）"
                            >
                              <InputNumber
                                style={{ width: '100%' }}
                                min={0}
                                max={0.01}
                                step={0.0001}
                                formatter={value => `${(Number(value) * 100).toFixed(4)}%`}
                                parser={value => parseFloat(value!.replace('%', '')) / 100 as any}
                              />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item
                              name="slippage"
                              label="滑点"
                              extra="交易滑点（0-1之间的小数）"
                            >
                              <InputNumber
                                style={{ width: '100%' }}
                                min={0}
                                max={0.01}
                                step={0.0001}
                                formatter={value => `${(Number(value) * 100).toFixed(4)}%`}
                                parser={value => parseFloat(value!.replace('%', '')) / 100 as any}
                              />
                            </Form.Item>
                          </Col>
                        </Row>
                      ),
                    },
                    {
                      key: 'backtest',
                      label: <span>回测设置</span>,
                      children: (
                        <Row gutter={16}>
                          <Col span={12}>
                            <Form.Item
                              name="backtestPeriod"
                              label="回测周期"
                              extra="策略回测的时间范围"
                            >
                              <Select>
                                <Select.Option value="3months">近3个月</Select.Option>
                                <Select.Option value="6months">近6个月</Select.Option>
                                <Select.Option value="1year">近1年</Select.Option>
                                <Select.Option value="2years">近2年</Select.Option>
                                <Select.Option value="5years">近5年</Select.Option>
                              </Select>
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item
                              name="benchmark"
                              label="基准指数"
                              extra="用于对比的基准指数代码"
                            >
                              <Input placeholder="如：000300.SH（沪深300）" />
                            </Form.Item>
                          </Col>
                        </Row>
                      ),
                    },
                  ]}
                />
              </TabPane>
            </Tabs>

            {/* 操作按钮 */}
            <Divider />
            <Space size="large" style={{ width: '100%', justifyContent: 'center' }}>
              <Button
                type="primary"
                size="large"
                icon={<ThunderboltOutlined />}
                loading={isGenerating}
                onClick={handleGenerate}
              >
                {isGenerating ? '生成中...' : '开始生成策略'}
              </Button>
              <Button size="large" onClick={() => form.resetFields()}>
                重置参数
              </Button>
            </Space>

            {/* 生成进度 */}
            {isGenerating && (
              <div style={{ marginTop: 24 }}>
                <Progress
                  percent={generationProgress}
                  status="active"
                  strokeColor={{
                    from: '#108ee9',
                    to: '#87d068',
                  }}
                />
                <Text type="secondary" style={{ display: 'block', textAlign: 'center', marginTop: 8 }}>
                  AI正在分析您的需求并生成策略代码...
                </Text>
              </div>
            )}
          </Form>
        </Card>
      )}

      {/* 策略生成结果展示（步骤2和3） */}
      {currentStep > 0 && currentStrategy && (
        <Card
          title={
            <Space>
              <CodeOutlined />
              <span>生成的策略</span>
              <Tag color="green">成功</Tag>
            </Space>
          }
          extra={
            <Space>
              <Button icon={<SaveOutlined />} onClick={handleSave} loading={isSaving}>
                保存策略
              </Button>
              <Button icon={<PlayCircleOutlined />} onClick={handleRegenerate}>
                重新生成
              </Button>
            </Space>
          }
        >
          <Alert
            message="策略生成成功"
            description={`策略名称：${currentStrategy.name || currentStrategy.strategy_name || '未命名策略'}`}
            type="success"
            showIcon
            style={{ marginBottom: 16 }}
          />

          {/* 这里可以添加策略详细信息展示组件 */}
          <Paragraph>
            <Text strong>策略说明：</Text>
            <br />
            {currentStrategy.rationale || currentStrategy.description || '暂无说明'}
          </Paragraph>

          {currentStrategy.artifacts && Array.isArray(currentStrategy.artifacts) && currentStrategy.artifacts.length > 0 && (
            <div>
              <Text strong>策略代码：</Text>
              <pre style={{
                background: '#f5f5f5',
                padding: 16,
                borderRadius: 4,
                overflow: 'auto',
                maxHeight: 500,
              }}>
                {currentStrategy.artifacts[0].code}
              </pre>
            </div>
          )}
        </Card>
      )}
    </div>
  );
};

export default EnhancedStrategyGenerator;
