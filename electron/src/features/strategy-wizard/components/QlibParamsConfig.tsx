import React from 'react';
import { Form, Slider, Select, Space, Typography, Divider, Card, Badge, Tooltip } from 'antd';
import { 
  Settings, 
  Target, 
  RefreshCcw, 
  Trash2, 
  Info,
  Layers,
  Zap,
  TrendingUp
} from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import { useWizardV2Store } from '../store/wizardV2Store';
import { QLIB_REBALANCE_DAY_OPTIONS, resolveRebalanceDays, RebalanceDays } from '../../../shared/qlib/rebalance';

const { Title, Text, Paragraph } = Typography;

interface Props {
  onNext?: () => void;
  onBack?: () => void;
}

const StrategyTypeCard: React.FC<{
  type: 'TopkDropout' | 'TopkWeight';
  selected: boolean;
  onClick: () => void;
  title: string;
  description: string;
  tag?: string;
}> = ({ selected, onClick, title, description, tag }) => (
  <motion.div
    whileHover={{ y: -2 }}
    onClick={onClick}
    className={`relative cursor-pointer rounded-2xl border-2 p-6 transition-all duration-300 ${
      selected 
        ? 'border-blue-500 bg-blue-50/50 shadow-lg shadow-blue-100' 
        : 'border-gray-100 bg-white hover:border-blue-200 hover:shadow-md'
    }`}
  >
    <div className="flex items-center justify-between">
      <div className={`text-lg font-bold ${selected ? 'text-blue-900' : 'text-gray-900'}`}>{title}</div>
      {tag && (
        <Badge 
          count={tag} 
          style={{ 
            backgroundColor: selected ? '#3b82f6' : '#f1f5f9', 
            color: selected ? '#fff' : '#64748b',
            fontWeight: 600
          }} 
        />
      )}
    </div>
    <div className="mt-3">
      <div className="text-sm leading-relaxed text-gray-700 font-medium">{description}</div>
    </div>
    {selected && (
      <motion.div
        layoutId="selected-border"
        className="absolute inset-0 rounded-2xl ring-2 ring-blue-500 ring-offset-2"
        initial={false}
        transition={{ type: "spring", stiffness: 300, damping: 30 }}
      />
    )}
  </motion.div>
);

const QlibParamsConfig: React.FC<Props> = () => {
  const { qlibParams, setQlibParams } = useWizardV2Store();
  const params = qlibParams ?? { strategy_type: 'TopkDropout', topk: 10, n_drop: 2, rebalance_days: 5 };
  const normalizedRebalanceDays = resolveRebalanceDays(params);

  const update = (patch: Partial<typeof params>) => {
    const newParams = { ...params, ...patch };
    if (patch.strategy_type === 'TopkWeight') {
      delete newParams.n_drop;
    }
    setQlibParams(newParams);
  };

  return (
    <div className="mx-auto max-w-4xl">
      <div className="mb-8">
        <Title level={4} style={{ margin: 0, fontWeight: 800, color: '#0f172a' }}>Qlib 策略参数</Title>
        <Text style={{ color: '#475569', fontWeight: 500 }}>配置 Alpha 引擎的选股逻辑与执行参数</Text>
      </div>

      <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
        <StrategyTypeCard
          type="TopkDropout"
          selected={params.strategy_type === 'TopkDropout'}
          onClick={() => update({ strategy_type: 'TopkDropout', n_drop: 2 })}
          title="TopK 轮换策略"
          description="基于 Qlib 的 TopkDropout 原理。每期根据模型得分对股票池进行排名，选取前 K 名进入组合。若原有持仓在排名中掉出前 K 名 or 满足剔除数(n_drop)条件，系统将自动卖出并调入新的高分标的。该模式能保持组合的极高灵敏度，是动量和短中线策略的首选。"
          tag="推荐"
        />
        <StrategyTypeCard
          type="TopkWeight"
          selected={params.strategy_type === 'TopkWeight'}
          onClick={() => update({ strategy_type: 'TopkWeight' })}
          title="因子加权策略"
          description="基于 Qlib 的 TopkWeight 原理。系统不仅关注排名，更会根据模型预测的具体分值计算权重。这种方式会平滑地分配资金，不会因为排名的微小变动而产生频繁换仓，更适合价值投资、指数增强等追求低换手率、稳健超额收益的场景。"
        />
      </div>

      <div className="mt-8 space-y-6">
        <Card variant="borderless" className="rounded-3xl border border-gray-100 shadow-sm">
          <div className="grid grid-cols-1 gap-8 md:grid-cols-2">
            {/* 核心执行参数 */}
            <div className="space-y-6">
              <div>
                <div className="mb-4 flex items-center justify-between">
                  <div className="flex items-center gap-2 font-bold text-slate-800">
                    <Target size={18} className="text-blue-600" />
                    <span>选股数量 (TopK)</span>
                    <Tooltip title="策略每期在池子中选出的标的数量">
                      <Info size={14} className="cursor-help text-slate-500" />
                    </Tooltip>
                  </div>
                  <div className="rounded-lg bg-blue-50 px-3 py-1 font-mono text-lg font-bold text-blue-600">
                    {params.topk}
                  </div>
                </div>
                <Slider
                  min={3}
                  max={100}
                  step={1}
                  value={params.topk}
                  onChange={(v) => update({ topk: v })}
                  tooltip={{ open: false }}
                  className="custom-premium-slider"
                />
                <div className="mt-2 flex justify-between text-[10px] font-black uppercase tracking-widest text-slate-500">
                  <span>3 只 (精选)</span>
                  <span>100 只 (宽量)</span>
                </div>
              </div>

              <div>
                <div className="mb-4 flex items-center gap-2 font-bold text-slate-800">
                  <RefreshCcw size={18} className="text-blue-600" />
                  <span>调仓周期</span>
                  <Tooltip title="策略执行重新审视持仓并换仓的频率">
                    <Info size={14} className="cursor-help text-slate-500" />
                  </Tooltip>
                </div>
                <Select
                  value={normalizedRebalanceDays}
                  onChange={(v: RebalanceDays) => update({ rebalance_days: v })}
                  options={QLIB_REBALANCE_DAY_OPTIONS.map((item) => ({
                    value: item.value,
                    label: item.value === 5 ? `${item.label} (平衡型)` : item.label,
                  }))}
                  className="w-full premium-select"
                  dropdownClassName="premium-dropdown"
                />
              </div>
            </div>

            {/* 高级控制项 */}
            <div className="rounded-2xl bg-gray-50/50 p-6">
              <AnimatePresence mode="wait">
                {params.strategy_type === 'TopkDropout' ? (
                  <motion.div
                    key="dropout-params"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    className="space-y-4"
                  >
                    <div className="flex items-center gap-2 text-sm font-bold text-slate-800">
                      <Trash2 size={16} className="text-orange-600" />
                      <span>强制剔除数 (n_drop)</span>
                    </div>
                    <div className="flex items-center gap-4">
                      <div className="flex-1">
                        <Slider
                          min={0}
                          max={params.topk}
                          step={1}
                          value={params.n_drop}
                          onChange={(v) => update({ n_drop: v })}
                          className="custom-orange-slider"
                        />
                      </div>
                      <div className="w-12 text-center font-mono text-lg font-bold text-orange-600">
                        {params.n_drop}
                      </div>
                    </div>
                    <Paragraph className="text-xs leading-relaxed text-slate-600 font-medium">
                      每次调仓将强制卖出排名最低的 <Text strong>{params.n_drop}</Text> 只标的。
                      {params.n_drop === 0 ? '当前设为 0，即仅根据得分变化触发换仓。' : '强制剔除能有效防止持仓僵化，提升组合灵敏度。'}
                    </Paragraph>
                  </motion.div>
                ) : (
                  <motion.div
                    key="weight-params"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    className="flex h-full flex-col items-center justify-center text-center py-4"
                  >
                    <div className="mb-3 rounded-full bg-blue-100 p-3 text-blue-600">
                      <TrendingUp size={24} />
                    </div>
                    <div className="text-sm font-bold text-slate-800">因子均衡模式已激活</div>
                    <Text className="mt-2 text-xs text-slate-600 font-medium">
                      该模式下持仓权重将根据模型预测得分动态分配，无需手动设置剔除阈值。
                    </Text>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </div>
        </Card>
      </div>

      <style>{`
        .custom-premium-slider .ant-slider-track {
          background: linear-gradient(90deg, #3b82f6, #8b5cf6) !important;
          height: 6px !important;
        }
        .custom-premium-slider .ant-slider-rail {
          height: 6px !important;
          background-color: #f1f5f9 !important;
        }
        .custom-premium-slider .ant-slider-handle::after {
          width: 14px !important;
          height: 14px !important;
          background: #fff !important;
          box-shadow: 0 4px 10px rgba(59,130,246,0.3) !important;
          border: 3px solid #3b82f6 !important;
        }
        
        .custom-orange-slider .ant-slider-track {
          background: #f97316 !important;
        }
        .custom-orange-slider .ant-slider-handle::after {
          border-color: #f97316 !important;
        }

        .premium-select .ant-select-selector {
          height: 44px !important;
          padding: 6px 12px !important;
          border-radius: 12px !important;
          border-color: #e2e8f0 !important;
          box-shadow: none !important;
        }
        .premium-select.ant-select-focused .ant-select-selector {
          border-color: #3b82f6 !important;
        }
      `}</style>
    </div>
  );
};

export default QlibParamsConfig;
