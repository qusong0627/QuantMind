import React, { useState } from 'react';
import { Row, Col, Card, Typography, Input, Button, Space, Alert, Tabs, Tag, Divider, message } from 'antd';
import { 
  BulbOutlined, 
  EditOutlined, 
  ThunderboltOutlined, 
  CheckCircleOutlined, 
  StockOutlined,
  SendOutlined
} from '@ant-design/icons';
import { motion, AnimatePresence } from 'framer-motion';
import { useWizardV2Store } from '../store/wizardV2Store';
import { fetchWorkingPoolByDsl, syncWorkingPoolToBackend } from '../services/wizardV2Service';
// 按需加载 wizardService 的网络与解析方法，避免静态与动态导入混用导致的打包警告
import { FACTORS } from '../factors/dictionary';
import { SimpleLogicBuilder } from './SimpleLogicBuilder';
import { CustomStockSelector } from './CustomStockSelector';

const { Title, Text } = Typography;
const { TextArea } = Input;

export const NaturalTextInput: React.FC<{ onNext: () => void }> = ({ onNext }) => {
  const { workingPool, setWorkingPool, conditions, setConditions } = useWizardV2Store();
  // V2 specific: we use setWorkingPool instead of setPool/setConditions etc.
  // For simplicity during migration, we might still need some V1 states if UI depends on them
  // but let's try to stick to SSOT.
  
  const [activeTab, setActiveTab] = useState('nlp');
  const [text, setText] = useState('');
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState<{ dsl?: string; mapping?: any; suggestions?: string[] }>({});
  const [matchedCount, setMatchedCount] = useState<number | null>(null);

  const useTemplate = (t: string) => {
    setText(t);
  };

  const templates = [
    { label: '全部股票', value: '全市场股票' },
    { label: '排除ST', value: '排除ST和*ST股票' },
    { label: '沪深300', value: '沪深300成分股' },
    { label: '中证1000', value: '中证1000成分股' },
    { label: '小市值', value: '总市值在10亿到100亿之间' },
    { label: '金融股', value: '金融股' },
    { label: '低估值', value: '市盈率小于20，市净率小于2' }
  ];

  const analyze = async () => {
    if (!text.trim()) {
      message.warning('请先输入选股描述');
      return;
    }
    setLoading(true);
    setMatchedCount(null);
    try {
      const { parseText } = await import('../services/wizardService');
      const parsed = await parseText(text);
      
      // 针对数据库按“元”存储的情况，修复 DSL 中的单位换算（AI 通常输出以“亿”为单位的数字）
      let correctedDsl = parsed.dsl;
      if (correctedDsl) {
        const billionFactors = FACTORS.filter(f => f.unit === '亿').map(f => f.key);
        billionFactors.forEach(f => {
          const regex = new RegExp(`(${f}\\s*(?:>|<|>=|<=|==)\\s*)(\\d+(\\.\\d+)?)`, 'g');
          correctedDsl = correctedDsl.replace(regex, (match, p1, p2) => {
            const val = parseFloat(p2);
            // 如果数值已经很大（大于100万），说明后端可能已经做过单位换算，不再重复计算
            if (val > 1000000) return match;
            return p1 + Math.floor(val * 1e8);
          });
        });
      }
      
      setPreview({ ...parsed, dsl: correctedDsl });

      if (correctedDsl) {
        try {
          const items = await fetchWorkingPoolByDsl(correctedDsl);
          setMatchedCount(items.length);
          // fetchWorkingPoolByDsl already syncs to backend
          setWorkingPool(items, true); 
        } catch (e) {
          console.warn('Failed to pre-calculate pool size', e);
        }
      }

      if (parsed.mapping?.factors && parsed.mapping?.defaults) {
        const dummyChildren = parsed.mapping.factors
          .filter((f: string) => parsed.mapping.defaults?.[f]?.threshold !== undefined)
          .map((f: string) => {
            const factorDef = FACTORS.find(item => item.key === f);
            const isBillion = factorDef?.unit === '亿';
            const val = parsed.mapping.defaults[f].threshold;
            return {
              type: 'numeric',
              factor: f,
              operator: '>',
              threshold: isBillion ? Math.floor(val * 1e8) : val
            };
          });
        if (dummyChildren.length > 0) {
          setConditions({ type: 'composite', op: 'AND', children: dummyChildren } as any);
        }
      }
      message.success('解析成功');
    } catch (err: any) {
      console.error('parseText failed', err);
      message.error(err?.message || '智能解析失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  };

  const runStrategy = async () => {
    // In V2, workingPool should already be populated if analyze was successful
    if (workingPool.length === 0 && !preview.dsl) {
      message.warning('请先完成解析或添加自选股票');
      return;
    }
    setLoading(true);
    try {
      if (preview.dsl) {
        // If there's a DSL but maybe analyze wasn't called or we want to refresh
        const items = await fetchWorkingPoolByDsl(preview.dsl);
        setWorkingPool(items, true);
        message.success(`已生成股票池，包含 ${items.length} 只股票`);
      } else {
        message.success(`当前已选 ${workingPool.length} 只股票`);
      }
      onNext();
    } catch (err: any) {
      console.error('runStrategy failed', err);
      message.error(err?.message || '生成股票池失败');
    } finally {
      setLoading(false);
    }
  };

  const runVisualStrategy = async () => {
    if (!conditions) {
      message.warning('请先添加筛选条件');
      return;
    }
    setLoading(true);
    try {
      const { parseConditions, queryPool } = await import('../services/wizardService');
      const parsed = await parseConditions({ conditions });
      const poolRes = await queryPool({ dsl: parsed.dsl });
      if (!poolRes.items || poolRes.items.length === 0) {
        message.warning('未获取到股票池，请检查条件后重试');
        return;
      }
      const items = (poolRes.items || []).map((x: any) => ({
        symbol: String(x?.symbol || x?.code || '').trim(),
        name: String(x?.name || '').trim(),
        marketCap: Number(x?.metrics?.market_cap ?? x?.market_cap ?? 0) || 0,
        pe: Number(x?.metrics?.pe ?? x?.pe ?? 0) || 0,
        price: Number(x?.metrics?.close ?? x?.price ?? 0) || 0,
      })).filter((x: any) => x.symbol);
      
      setWorkingPool(items);
      message.success(`已生成股票池，包含 ${items.length} 只股票`);
      onNext();
    } catch (err: any) {
      console.error('runVisualStrategy failed', err);
      message.error(err?.message || '生成股票池失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="w-full p-2">
      <div className="mb-8">
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          type="line"
          className="custom-premium-tabs"
          items={[
            {
              key: 'nlp',
              label: (
                <div className="flex items-center gap-2 px-4 py-1">
                  <BulbOutlined />
                  <span className="font-medium">自然语言描述</span>
                </div>
              ),
              children: (
                <div className="mt-3">
                  <Row gutter={24}>
                    <Col span={16}>
                      <div className="bg-white rounded-2xl border border-gray-100 p-6 shadow-sm hover:shadow-md transition-all duration-300">
                        <Title level={5} className="mb-4 text-gray-800 flex items-center gap-2">
                          <SendOutlined className="text-blue-500" />
                          请输入选股逻辑
                        </Title>
                        <TextArea
                          rows={6}
                          placeholder="例如：市值在10-100亿之间且ROE小于30的股票"
                          value={text}
                          onChange={(e) => setText(e.target.value)}
                          className="text-lg p-4 rounded-2xl border-gray-200 focus:border-blue-400 focus:ring-4 focus:ring-blue-50 transition-all"
                          style={{ resize: 'none' }}
                        />
                        
                        <div className="mt-4 mb-6">
                          <div className="flex items-center gap-2 mb-3">
                            <Text type="secondary" className="text-xs font-medium uppercase tracking-wider">快速模版</Text>
                            <div className="h-px flex-1 bg-gray-100" />
                          </div>
                          <div className="flex flex-wrap gap-2">
                            {templates.map(t => (
                              <button
                                key={t.label}
                                onClick={() => useTemplate(t.value)}
                                className="px-3 py-1.5 rounded-lg text-sm font-medium transition-all duration-200
                                  bg-blue-50 text-blue-600 border border-blue-100 hover:bg-blue-600 hover:text-white hover:border-blue-600
                                  active:scale-95 shadow-sm"
                              >
                                {t.label}
                              </button>
                            ))}
                          </div>
                        </div>

                        <Divider className="my-8" />

                        <div className="flex justify-start items-center">
                          <div className="flex items-center gap-4">
                            <Button 
                              type="primary" 
                              size="large" 
                              onClick={analyze} 
                              loading={loading} 
                              icon={<ThunderboltOutlined />}
                              className="h-12 px-8 rounded-2xl bg-gradient-to-r from-blue-600 to-blue-500 border-none shadow-lg shadow-blue-200 hover:shadow-blue-300 transition-all"
                            >
                              智能解析
                            </Button>
                            <AnimatePresence>
                              {matchedCount !== null && (
                                <motion.div
                                  initial={{ opacity: 0, x: -10 }}
                                  animate={{ opacity: 1, x: 0 }}
                                  className="flex items-center gap-2 text-gray-500"
                                >
                                  <div className="w-2 h-2 rounded-full bg-green-500" />
                                  <span>
                                    匹配到 <span className="text-blue-600 font-bold text-lg">{matchedCount}</span> 只标的
                                  </span>
                                </motion.div>
                              )}
                            </AnimatePresence>
                          </div>
                        </div>
                      </div>
                    </Col>
                    <Col span={8}>
                      <Card
                        variant="borderless"
                        className="h-full rounded-3xl bg-gray-50/50 border border-gray-100 shadow-sm"
                        title={<span className="text-gray-700 font-bold">逻辑预览</span>}
                      >
                        {!preview.dsl ? (
                          <div className="space-y-6 text-gray-500 leading-relaxed text-sm">
                            <div className="p-4 bg-white rounded-2xl border border-gray-100">
                              <p className="font-medium text-gray-700 mb-2">💡 提示</p>
                              <p className="mb-2">您可以直接用自然语言描述您期望的底层股票池。</p>
                              <p className="text-orange-600 font-medium">⚠️ 请注意：建议在此构建宽泛的备选池以保证 AI 的学习空间；若过滤条件过于严格，可能会导致标的过少而影响回测效果。如需在实际交易中精准限定名单，请在后续的策略配置中设置。</p>
                            </div>
                            <p>
                              &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;QuantMind 是一款企业级 AI 量化交易平台，基于 Qlib 框架深度定制。我们依托前沿的深度学习模型与海量特征，为您提供专业的每日盘后自动决策服务。
                            </p>
                            <p>
                              &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;您无需深谙复杂算法，只需构建期望的股票池与风险偏好，AI 引擎即可随市场行情实时迭代，为您精准预测次日最具潜力的投资组合。
                            </p>
                          </div>
                        ) : (
                          <div className="space-y-6">
                            <Alert 
                              message="解析成功" 
                              type="success" 
                              showIcon 
                              className="rounded-2xl border-emerald-100 bg-emerald-50 text-emerald-800"
                            />
                            <div>
                              <Text strong className="text-xs text-gray-400 uppercase tracking-wider mb-2 block">生成的查询逻辑 (DSL)</Text>
                              <div className="bg-white border border-gray-100 p-4 rounded-2xl font-mono text-xs text-blue-600 break-all leading-relaxed">
                                {preview.dsl}
                              </div>
                            </div>
                            {preview.mapping?.factors && (
                              <div>
                                <Text strong className="text-xs text-gray-400 uppercase tracking-wider mb-2 block">识别因子</Text>
                                <div className="flex flex-wrap gap-2">
                                  {preview.mapping.factors.map((f: string) => (
                                    <Tag key={f} className="m-0 px-3 py-1 rounded-full bg-blue-50 border-blue-100 text-blue-600 font-medium">
                                      {f}
                                    </Tag>
                                  ))}
                                </div>
                              </div>
                            )}
                          </div>
                        )}
                      </Card>
                    </Col>
                  </Row>
                </div>
              )
            },
            {
              key: 'visual',
              label: (
                <div className="flex items-center gap-2 px-4 py-1">
                  <EditOutlined />
                  <span className="font-medium">简易构建器</span>
                </div>
              ),
              children: (
                <div className="mt-3 bg-white rounded-2xl border border-gray-100 p-4 shadow-sm">
                  <SimpleLogicBuilder onChange={(c) => setConditions(c)} />
                </div>
              )
            },
            {
              key: 'custom',
              label: (
                <div className="flex items-center gap-2 px-4 py-1">
                  <StockOutlined />
                  <span className="font-medium">股票池管理</span>
                </div>
              ),
              children: (
                <div className="mt-3 bg-white rounded-2xl border border-gray-100 p-4 shadow-sm">
                  <Alert
                    message="手动添加您关注的特定股票，它们将与筛选结果合并。"
                    type="info"
                    showIcon
                    className="mb-4 rounded-xl py-2 px-4"
                  />
                  <div style={{ height: '550px' }}>
                    <CustomStockSelector />
                  </div>
                </div>
              )
            }
          ]}
        />
      </div>
      
      <style>{`
        .custom-premium-tabs .ant-tabs-nav {
          margin-bottom: 0 !important;
        }
        .custom-premium-tabs .ant-tabs-nav::before {
          display: none !important;
        }
        .custom-premium-tabs .ant-tabs-tab {
          padding: 8px 0 !important;
          margin: 0 4px 0 0 !important;
          border-radius: 12px !important;
          transition: all 0.3s !important;
        }
        .custom-premium-tabs .ant-tabs-tab-active {
          background: #eff6ff !important;
        }
        .custom-premium-tabs .ant-tabs-tab-active .ant-tabs-tab-btn {
          color: #2563eb !important;
        }
        .custom-premium-tabs .ant-tabs-ink-bar {
          display: none !important;
        }
      `}</style>
    </div>
  );
};

export default NaturalTextInput;
