import React from 'react';
import { Card, Divider, Input, Button, Row, Col, InputNumber, Select, Alert, Typography, Tag, Checkbox, Switch, Tooltip } from 'antd';
import { Settings2, MonitorPlay, TreePine, Cpu, Ruler, AlertTriangle } from 'lucide-react';
import {
  TrainingParams,
  TrainingContext,
  TrainingTarget,
  WfaConfig,
  DealPrice,
  ModelType,
  EnsembleMethod,
  MODEL_TYPE_OPTIONS,
  MODEL_DL_DEFAULTS,
} from './trainingUtils';
import type { AppMarket } from '../../store/slices/uiSlice';

const MARKET_BENCHMARKS: Record<string, { label: string; value: string }[]> = {
  CN: [
    { label: '沪深300', value: 'SH000300' },
    { label: '中证500', value: 'SH000905' },
    { label: '中证1000', value: 'SH000852' },
  ],
  HK: [
    { label: '恒生指数', value: 'HSI' },
    { label: '恒生国企', value: 'HSCEI' },
    { label: '恒生科技', value: 'HSTECH' },
  ],
  US: [
    { label: '标普500', value: 'SPX' },
    { label: '纳斯达克100', value: 'NDX' },
    { label: '道琼斯30', value: 'DJI' },
  ],
  CRYPTO: [
    { label: '比特币', value: 'BTC' },
    { label: '以太坊', value: 'ETH' },
  ],
  FUTURES: [
    { label: '原油', value: 'CL.FUT' },
    { label: '沪铜', value: 'CU.FUT' },
    { label: '螺纹钢', value: 'RB.FUT' },
    { label: '黄金', value: 'AU.FUT' },
  ],
};

interface ParameterConfigProps {
  params: TrainingParams;
  context: TrainingContext;
  onParamsChange: (params: TrainingParams) => void;
  onContextChange: (context: TrainingContext) => void;
  displayName: string;
  onDisplayNameChange: (name: string, mode: 'auto' | 'manual') => void;
  autoDisplayName: string;
  /** 训练市场：全局五市场，或训练页专有的自定义数据市场（CUSTOM）。 */
  market?: AppMarket | 'CUSTOM';
  target: TrainingTarget;
  onTargetChange: (target: TrainingTarget) => void;
  wfa?: WfaConfig;
  onWfaChange?: (wfa: WfaConfig) => void;
}

const SectionHeader: React.FC<{ title: string; desc: string; icon?: React.ReactNode }> = ({ title, desc, icon }) => (
  <div className="flex items-start justify-between gap-4">
    <div>
      <div className="flex items-center gap-2">
        {icon}
        <Typography.Title level={4} className="!mb-0 !text-slate-900">
          {title}
        </Typography.Title>
      </div>
      <Typography.Paragraph className="!mb-0 !mt-2 !text-xs !text-slate-500 leading-relaxed">
        {desc}
      </Typography.Paragraph>
    </div>
  </div>
);

export const ParameterConfig: React.FC<ParameterConfigProps> = ({
  params,
  context,
  onParamsChange,
  onContextChange,
  displayName,
  onDisplayNameChange,
  autoDisplayName,
  market = 'CN',
  target,
  onTargetChange,
  wfa,
  onWfaChange,
}) => {
  const benchmarkOptions = MARKET_BENCHMARKS[market] || MARKET_BENCHMARKS.CN;
  const isSingleLgb = params.model_types.length === 1 && params.model_type === 'lightgbm';
  const isReturnTarget = target.mode === 'return';
  // 分位推理：后端仅支持 A 股单 LightGBM 回归模型（train.py _validate_quantile_config）
  const quantileDisabled = market !== 'CN' || !isSingleLgb || !isReturnTarget;
  // WFA 诊断：后端仅支持树模型 + 线性，其余直接 skip（train.py _train_wfa_single）。
  // 集合训练时 WFA 跑在主模型（model_type，即选中列表第一个）上。
  const wfaSupported = ['lightgbm', 'xgboost', 'catboost', 'linear'].includes(params.model_type);
  // 行业编码：仅 CatBoost 声明 cat_features 做类别处理；多模型混跑一律不给
  const isCatboost = params.model_types.length === 1 && params.model_type === 'catboost';
  // 集合训练（Stacking）：≥2 个模型才可选；「树/线性 + 深度学习」混合后端不支持
  // 集成（train.py 多模型分支），混合选型将各自独立训练。
  const hasDLSelected = params.model_types.some((mt) => MODEL_TYPE_OPTIONS.find((m) => m.value === mt)?.category === 'deep_learning');
  const hasClassicSelected = params.model_types.some((mt) => {
    const category = MODEL_TYPE_OPTIONS.find((m) => m.value === mt)?.category;
    return category === 'tree' || category === 'linear';
  });
  const isEnsembleMixed = params.model_types.length > 1 && hasDLSelected && hasClassicSelected;
  const showEnsembleSelector = params.model_types.length > 1 && !isEnsembleMixed;
  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_0.9fr]">
      <Card className="rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20 } }}>
        <SectionHeader
          title="第三步：参数配置"
          desc="把模型超参与训练上下文拆开，避免配置语义混在一起。"
          icon={<Settings2 size={18} className="text-indigo-500" />}
        />
        <Divider className="my-4" />
        <div className="space-y-4">
          {/* 模型类型选择 */}
          <Card className="rounded-2xl border-slate-200" size="small" title="模型类型">
            <div className="space-y-3">
              <div className="text-xs text-slate-500">
                可多选模型：选 2 个及以上树模型 / 线性模型可做 Stacking 集合训练；与深度学习模型混合选型时各自独立训练（不集成）。
              </div>
              <Checkbox.Group
                value={params.model_types}
                className="w-full"
                onChange={(checkedValues) => {
                  const selected = checkedValues as ModelType[];
                  if (selected.length === 0) return; // 至少保留一个模型
                  const primary = selected[0];
                  // 混合（树/线性 + 深度学习）后端不支持集成，将各自独立训练
                  const selectedHasDL = selected.some((mt) => MODEL_TYPE_OPTIONS.find((m) => m.value === mt)?.category === 'deep_learning');
                  const selectedHasClassic = selected.some((mt) => {
                    const category = MODEL_TYPE_OPTIONS.find((m) => m.value === mt)?.category;
                    return category === 'tree' || category === 'linear';
                  });
                  const mixed = selected.length > 1 && selectedHasDL && selectedHasClassic;
                  // 切换模型类型时，自动填充主模型的推荐 DL 默认参数
                  const dlDefaults = MODEL_DL_DEFAULTS[primary] || {};
                  // 对于非 DL 模型，不覆盖已设置的 DL 参数
                  const updated: TrainingParams = {
                    ...params,
                    model_type: primary,
                    model_types: selected,
                    // 多选且非混合时保留集成方式；单选/混合一律回落 none
                    ensemble_method: selected.length > 1 && !mixed ? (params.ensemble_method || 'none') : 'none',
                    prediction_mode: selected.length === 1 && primary === 'lightgbm' ? params.prediction_mode : 'point',
                  };
                  if (dlDefaults.dl_hidden_size !== undefined) {
                    Object.assign(updated, dlDefaults);
                  }
                  onParamsChange(updated);
                  // 行业编码仅单选 CatBoost 可用；其余组合自动关闭，避免提交无效组合
                  if (!(selected.length === 1 && primary === 'catboost') && context.industry_as_feature) {
                    onContextChange({ ...context, industry_as_feature: false });
                  }
                  // WFA 诊断跑在主模型上：主模型不支持时自动关闭
                  if (!['lightgbm', 'xgboost', 'catboost', 'linear'].includes(primary) && wfa?.enabled) {
                    onWfaChange?.({ ...wfa, enabled: false });
                  }
                }}
              >
                <div className="space-y-3">
                  <div className="text-xs font-medium text-slate-600 flex items-center gap-1">
                    <TreePine size={12} /> 树模型
                  </div>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-2 pl-1">
                    {MODEL_TYPE_OPTIONS.filter(m => m.category === 'tree').map(m => (
                      <Checkbox
                        key={m.value}
                        value={m.value}
                        className="!inline-flex !items-start [&_.ant-checkbox]:top-1 [&_.ant-checkbox]:self-start"
                      >
                        <span className="inline-flex flex-col gap-0.5 leading-normal">
                          <span className="inline-flex items-center gap-1.5">
                            <Tooltip title={m.tooltip} placement="topLeft" styles={{ root: { maxWidth: 360 } }}>
                              <span className="text-sm cursor-help border-b border-dashed border-slate-300 font-medium text-slate-700">{m.label}</span>
                            </Tooltip>
                            <Tag color={m.framework === 'pytorch' ? 'orange' : 'blue'} className="!m-0 rounded-md px-1 text-[10px] leading-4">
                              {m.framework === 'pytorch' ? 'GPU' : 'CPU'}
                            </Tag>
                          </span>
                          <span className="text-xs text-slate-400">{m.description}</span>
                        </span>
                      </Checkbox>
                    ))}
                  </div>
                  <div className="text-xs font-medium text-slate-600 flex items-center gap-1">
                    <Ruler size={12} /> 线性基线
                  </div>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-2 pl-1">
                    {MODEL_TYPE_OPTIONS.filter(m => m.category === 'linear').map(m => (
                      <Checkbox
                        key={m.value}
                        value={m.value}
                        className="!inline-flex !items-start [&_.ant-checkbox]:top-1 [&_.ant-checkbox]:self-start"
                      >
                        <span className="inline-flex flex-col gap-0.5 leading-normal">
                          <span className="inline-flex items-center gap-1.5">
                            <Tooltip title={m.tooltip} placement="topLeft" styles={{ root: { maxWidth: 360 } }}>
                              <span className="text-sm cursor-help border-b border-dashed border-slate-300 font-medium text-slate-700">{m.label}</span>
                            </Tooltip>
                            <Tag color={m.framework === 'pytorch' ? 'orange' : 'blue'} className="!m-0 rounded-md px-1 text-[10px] leading-4">
                              {m.framework === 'pytorch' ? 'GPU' : 'CPU'}
                            </Tag>
                          </span>
                          <span className="text-xs text-slate-400">{m.description}</span>
                        </span>
                      </Checkbox>
                    ))}
                  </div>
                  <div className="text-xs font-medium text-slate-600 flex items-center gap-1">
                    <Cpu size={12} /> 深度学习
                  </div>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-2 pl-1">
                    {MODEL_TYPE_OPTIONS.filter(m => m.category === 'deep_learning').map(m => (
                      <Checkbox
                        key={m.value}
                        value={m.value}
                        className="!inline-flex !items-start [&_.ant-checkbox]:top-1 [&_.ant-checkbox]:self-start"
                      >
                        <span className="inline-flex flex-col gap-0.5 leading-normal">
                          <span className="inline-flex items-center gap-1.5">
                            <Tooltip title={m.tooltip} placement="topLeft" styles={{ root: { maxWidth: 360 } }}>
                              <span className="text-sm cursor-help border-b border-dashed border-slate-300 font-medium text-slate-700">{m.label}</span>
                            </Tooltip>
                            <Tag color={m.framework === 'pytorch' ? 'orange' : 'blue'} className="!m-0 rounded-md px-1 text-[10px] leading-4">
                              {m.framework === 'pytorch' ? 'GPU' : 'CPU'}
                            </Tag>
                          </span>
                          <span className="text-xs text-slate-400">{m.description}</span>
                        </span>
                      </Checkbox>
                    ))}
                  </div>
                </div>
              </Checkbox.Group>
              {params.model_types.length > 1 && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-xs text-slate-500">已选模型：</span>
                  {params.model_types.map(mt => {
                    const opt = MODEL_TYPE_OPTIONS.find(m => m.value === mt);
                    return (
                      <Tag key={mt} color="blue" className="!m-0 rounded-lg">
                        {opt?.label ?? mt}
                      </Tag>
                    );
                  })}
                </div>
              )}
              {isEnsembleMixed && (
                <Alert
                  type="warning"
                  showIcon
                  icon={<AlertTriangle size={14} />}
                  message="树模型与深度学习模型混合训练时，集成方法暂不支持，将分别独立训练"
                  className="rounded-xl"
                />
              )}
              {showEnsembleSelector && (
                <div className="space-y-1">
                  <div className="text-xs text-slate-500">集成方法</div>
                  <Select
                    value={params.ensemble_method}
                    className="w-full"
                    onChange={(value) => onParamsChange({ ...params, ensemble_method: value as EnsembleMethod })}
                    options={[
                      { label: '无集成 (各自独立训练)', value: 'none' },
                      { label: 'Stacking 集成', value: 'stacking' },
                    ]}
                  />
                  <div className="text-[10px] text-slate-400">Stacking：各基模型先产 OOF 预测，再由 Ridge 元学习器加权融合（评估与产物口径均为集成预测）</div>
                </div>
              )}
              {showEnsembleSelector && params.ensemble_method === 'stacking' && (
                <Row gutter={[12, 12]}>
                  <Col span={12}>
                    <div className="space-y-1">
                      <div className="text-xs text-slate-500">OOF 折数 (n_folds)</div>
                      <InputNumber
                        value={params.n_folds ?? 3}
                        min={2}
                        max={10}
                        step={1}
                        className="w-full"
                        onChange={(v) => onParamsChange({ ...params, n_folds: Number(v ?? 3) })}
                      />
                      <div className="text-[10px] text-slate-400">时序扩展窗口折数，越多越稳但越慢</div>
                    </div>
                  </Col>
                  <Col span={12}>
                    <div className="space-y-1">
                      <div className="text-xs text-slate-500">元学习器正则 (alpha)</div>
                      <InputNumber
                        value={params.meta_alpha ?? 1.0}
                        min={0.01}
                        max={100}
                        step={0.5}
                        className="w-full"
                        onChange={(v) => onParamsChange({ ...params, meta_alpha: Number(v ?? 1.0) })}
                      />
                      <div className="text-[10px] text-slate-400">Ridge 元学习器 L2 系数，越大越保守</div>
                    </div>
                  </Col>
                </Row>
              )}
              {params.model_types.some(mt => {
                const opt = MODEL_TYPE_OPTIONS.find(m => m.value === mt);
                return opt?.framework === 'pytorch';
              }) && (
                <Alert
                  type="info"
                  showIcon
                  message="深度学习模型需要 GPU 和 PyTorch 环境，训练时间较长"
                  className="rounded-xl"
                />
              )}
            </div>
          </Card>

          <Card className="rounded-2xl border-slate-200" size="small" title="模型命名">
            <div className="space-y-2">
              <div className="text-xs text-slate-500">
                display_name 用于模型管理页展示和训练结果命名，自动规则为“模型_日期_T+N_模型维度_版本”。
              </div>
              <div className="flex items-center gap-2">
                <Input
                  value={displayName}
                  onChange={(event) => onDisplayNameChange(event.target.value, 'manual')}
                  placeholder={autoDisplayName}
                  className="rounded-xl flex-1"
                  maxLength={128}
                />
                <Button
                  className="rounded-xl flex-shrink-0"
                  onClick={() => onDisplayNameChange(autoDisplayName, 'auto')}
                >
                  恢复自动
                </Button>
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-400">
                <span>当前自动示例：{autoDisplayName}</span>
                <span>{displayName.trim().length}/128</span>
              </div>
            </div>
          </Card>

          <Card className="rounded-2xl border-slate-200" size="small" title="训练超参">
            <div className="space-y-4">
              {/* Objective & Metric - 共享 */}
              <Row gutter={[12, 12]}>
                <Col span={12}>
                  <div className="space-y-1">
                    <div className="text-xs text-slate-500">Objective</div>
                    <Select
                      value={params.objective}
                      className="w-full"
                      onChange={(value) => onParamsChange({ ...params, objective: value as TrainingParams['objective'] })}
                      options={[
                        { label: '回归 (regression)', value: 'regression' },
                        { label: '二分类 (binary)', value: 'binary' },
                      ]}
                    />
                  </div>
                </Col>
                <Col span={12}>
                  <div className="space-y-1">
                    <div className="text-xs text-slate-500">Metric</div>
                    <Select
                      value={params.metric}
                      className="w-full"
                      onChange={(value) => onParamsChange({ ...params, metric: value as TrainingParams['metric'] })}
                      options={[
                        { label: 'L2', value: 'l2' },
                        { label: 'RMSE', value: 'rmse' },
                        { label: 'MAE', value: 'mae' },
                        { label: 'AUC', value: 'auc' },
                        { label: 'Binary Logloss', value: 'binary_logloss' },
                      ]}
                    />
                  </div>
                </Col>
              </Row>

              {/* LightGBM 专属参数 */}
              {params.model_types.includes('lightgbm') && (
                <>
                  <div className="text-xs font-medium text-slate-500 border-t pt-3">LightGBM 超参</div>
                  <Row gutter={[12, 12]}>
                    {[
                      ['num_leaves', '叶子数', { min: 1, max: 1024, step: 1 }],
                      ['min_data_in_leaf', '叶子最小样本', { min: 1, max: 10000, step: 1 }],
                      ['path_smooth', '路径平滑', { min: 0, max: 10, step: 0.1 }],
                      ['bagging_freq', 'Bagging 频率', { min: 0, max: 100, step: 1 }],
                      ['lambda_l1', 'L1 正则', { min: 0, max: 10, step: 0.1 }],
                      ['lambda_l2', 'L2 正则', { min: 0, max: 10, step: 0.1 }],
                      ['feature_fraction', '特征采样', { min: 0.1, max: 1, step: 0.01 }],
                      ['bagging_fraction', '行采样', { min: 0.1, max: 1, step: 0.01 }],
                      ['num_boost_round', '最大迭代轮数', { min: 1, max: 10000, step: 10 }],
                      ['early_stopping_rounds', '早停轮数', { min: 1, max: 1000, step: 5 }],
                    ].map(([key, label, lim]) => (
                      <Col span={12} key={key as string}>
                        <div className="space-y-1">
                          <div className="text-xs text-slate-500">{label as string}</div>
                          <InputNumber
                            value={params[key as keyof TrainingParams] as number}
                            min={(lim as any)?.min}
                            max={(lim as any)?.max}
                            step={(lim as any)?.step}
                            className="w-full"
                            onChange={(v) => onParamsChange({ ...params, [key as string]: Number(v ?? params[key as keyof TrainingParams]) })}
                          />
                        </div>
                      </Col>
                    ))}
                    <Col span={12}>
                      <div className="space-y-1">
                        <div className="text-xs text-slate-500">学习率 (lgb_learning_rate)</div>
                        <InputNumber
                          value={params.lgb_learning_rate ?? params.learning_rate}
                          min={0.0001}
                          max={1}
                          step={0.001}
                          className="w-full"
                          onChange={(v) => onParamsChange({ ...params, lgb_learning_rate: Number(v ?? params.learning_rate) })}
                        />
                      </div>
                    </Col>
                    <Col span={12}>
                      <div className="space-y-1">
                        <div className="text-xs text-slate-500">最大深度 (lgb_max_depth)</div>
                        <InputNumber
                          value={params.lgb_max_depth ?? params.max_depth}
                          min={-1}
                          max={64}
                          step={1}
                          className="w-full"
                          onChange={(v) => onParamsChange({ ...params, lgb_max_depth: Number(v ?? params.max_depth) })}
                        />
                      </div>
                    </Col>
                  </Row>
                </>
              )}

              {/* XGBoost 专属参数 */}
              {params.model_types.includes('xgboost') && (
                <>
                  <div className="text-xs font-medium text-slate-500 border-t pt-3">XGBoost 超参</div>
                  <Row gutter={[12, 12]}>
                    {[
                      ['xgb_subsample', '行采样 (subsample)', { min: 0.1, max: 1, step: 0.05 }],
                      ['xgb_colsample_bytree', '列采样', { min: 0.1, max: 1, step: 0.05 }],
                      ['xgb_reg_alpha', 'L1 正则', { min: 0, max: 10, step: 0.1 }],
                      ['xgb_reg_lambda', 'L2 正则', { min: 0, max: 10, step: 0.1 }],
                      ['xgb_min_child_weight', '最小叶子权重', { min: 1, max: 1000, step: 10 }],
                      ['num_boost_round', '最大迭代轮数', { min: 1, max: 10000, step: 10 }],
                      ['early_stopping_rounds', '早停轮数', { min: 1, max: 1000, step: 5 }],
                    ].map(([key, label, lim]) => (
                      <Col span={12} key={key as string}>
                        <div className="space-y-1">
                          <div className="text-xs text-slate-500">{label as string}</div>
                          <InputNumber
                            value={params[key as keyof TrainingParams] as number}
                            min={(lim as any)?.min}
                            max={(lim as any)?.max}
                            step={(lim as any)?.step}
                            className="w-full"
                            onChange={(v) => onParamsChange({ ...params, [key as string]: Number(v ?? params[key as keyof TrainingParams]) })}
                          />
                        </div>
                      </Col>
                    ))}
                    <Col span={12}>
                      <div className="space-y-1">
                        <div className="text-xs text-slate-500">学习率 (xgb_learning_rate)</div>
                        <InputNumber
                          value={params.xgb_learning_rate ?? params.learning_rate}
                          min={0.0001}
                          max={1}
                          step={0.001}
                          className="w-full"
                          onChange={(v) => onParamsChange({ ...params, xgb_learning_rate: Number(v ?? params.learning_rate) })}
                        />
                      </div>
                    </Col>
                    <Col span={12}>
                      <div className="space-y-1">
                        <div className="text-xs text-slate-500">最大深度 (xgb_max_depth)</div>
                        <InputNumber
                          value={params.xgb_max_depth ?? params.max_depth}
                          min={1}
                          max={16}
                          step={1}
                          className="w-full"
                          onChange={(v) => onParamsChange({ ...params, xgb_max_depth: Number(v ?? params.max_depth) })}
                        />
                      </div>
                    </Col>
                  </Row>
                </>
              )}

              {/* CatBoost 专属参数 */}
              {params.model_types.includes('catboost') && (
                <>
                  <div className="text-xs font-medium text-slate-500 border-t pt-3">CatBoost 超参</div>
                  <Row gutter={[12, 12]}>
                    {[
                      ['cb_l2_leaf_reg', 'L2 正则', { min: 0, max: 10, step: 0.5 }],
                      ['cb_random_strength', '随机扰动', { min: 0, max: 10, step: 0.5 }],
                      ['cb_bagging_temperature', 'Bagging 温度', { min: 0, max: 10, step: 0.5 }],
                      ['cb_od_wait', '早停等待轮数', { min: 1, max: 1000, step: 10 }],
                      ['num_boost_round', '最大迭代轮数', { min: 1, max: 10000, step: 10 }],
                      ['early_stopping_rounds', '早停轮数', { min: 1, max: 1000, step: 5 }],
                    ].map(([key, label, lim]) => (
                      <Col span={12} key={key as string}>
                        <div className="space-y-1">
                          <div className="text-xs text-slate-500">{label as string}</div>
                          <InputNumber
                            value={params[key as keyof TrainingParams] as number}
                            min={(lim as any)?.min}
                            max={(lim as any)?.max}
                            step={(lim as any)?.step}
                            className="w-full"
                            onChange={(v) => onParamsChange({ ...params, [key as string]: Number(v ?? params[key as keyof TrainingParams]) })}
                          />
                        </div>
                      </Col>
                    ))}
                    <Col span={12}>
                      <div className="space-y-1">
                        <div className="text-xs text-slate-500">学习率 (cb_learning_rate)</div>
                        <InputNumber
                          value={params.cb_learning_rate ?? params.learning_rate}
                          min={0.001}
                          max={1}
                          step={0.01}
                          className="w-full"
                          onChange={(v) => onParamsChange({ ...params, cb_learning_rate: Number(v ?? params.learning_rate) })}
                        />
                      </div>
                    </Col>
                    <Col span={12}>
                      <div className="space-y-1">
                        <div className="text-xs text-slate-500">树深度 (cb_depth)</div>
                        <InputNumber
                          value={params.cb_depth ?? params.max_depth}
                          min={1}
                          max={16}
                          step={1}
                          className="w-full"
                          onChange={(v) => onParamsChange({ ...params, cb_depth: Number(v ?? params.max_depth) })}
                        />
                      </div>
                    </Col>
                  </Row>
                </>
              )}

              {/* Linear 专属参数 */}
              {params.model_types.includes('linear') && (
                <>
                  <div className="text-xs font-medium text-slate-500 border-t pt-3">Ridge 回归超参</div>
                  <Row gutter={[12, 12]}>
                    <Col span={12}>
                      <div className="space-y-1">
                        <div className="text-xs text-slate-500">正则化系数 (alpha)</div>
                        <InputNumber
                          value={params.linear_alpha ?? 1.0}
                          min={0.0001}
                          max={1000}
                          step={0.1}
                          className="w-full"
                          onChange={(v) => onParamsChange({ ...params, linear_alpha: Number(v ?? 1.0) })}
                        />
                      </div>
                    </Col>
                  </Row>
                </>
              )}

              {/* RandomForest 专属参数 */}
              {params.model_types.includes('random_forest') && (
                <>
                  <div className="text-xs font-medium text-slate-500 border-t pt-3">随机森林超参</div>
                  <Row gutter={[12, 12]}>
                    <Col span={12}>
                      <div className="space-y-1">
                        <div className="text-xs text-slate-500">树的数量 (n_estimators)</div>
                        <InputNumber
                          value={params.rf_n_estimators ?? 300}
                          min={10}
                          max={2000}
                          step={10}
                          className="w-full"
                          onChange={(v) => onParamsChange({ ...params, rf_n_estimators: Number(v ?? 300) })}
                        />
                      </div>
                    </Col>
                    <Col span={12}>
                      <div className="space-y-1">
                        <div className="text-xs text-slate-500">最大深度 (max_depth)</div>
                        <InputNumber
                          value={params.rf_max_depth ?? 12}
                          min={1}
                          max={64}
                          step={1}
                          className="w-full"
                          onChange={(v) => onParamsChange({ ...params, rf_max_depth: Number(v ?? 12) })}
                        />
                      </div>
                    </Col>
                    <Col span={12}>
                      <div className="space-y-1">
                        <div className="text-xs text-slate-500">分裂特征数 (max_features)</div>
                        <Select
                          value={params.rf_max_features ?? 'sqrt'}
                          className="w-full"
                          onChange={(value) => onParamsChange({ ...params, rf_max_features: value })}
                          options={[
                            { label: 'sqrt（默认）', value: 'sqrt' },
                            { label: 'log2', value: 'log2' },
                          ]}
                        />
                      </div>
                    </Col>
                  </Row>
                </>
              )}

              {/* 深度学习模型参数 */}
              {params.model_types.some(mt => {
                const opt = MODEL_TYPE_OPTIONS.find(m => m.value === mt);
                return opt?.category === 'deep_learning';
              }) && (
                <>
                  <div className="text-xs font-medium text-slate-500 border-t pt-3">
                    深度学习超参 (主模型: {params.model_type ?
                      MODEL_TYPE_OPTIONS.find(m => m.value === params.model_type)?.label :
                      params.model_types.filter(mt => {
                        const opt = MODEL_TYPE_OPTIONS.find(m => m.value === mt);
                        return opt?.category === 'deep_learning';
                      }).map(mt => MODEL_TYPE_OPTIONS.find(m => m.value === mt)?.label).join(', ')}
                    )
                    <span className="ml-2 text-slate-400">— 切换模型时自动填充推荐默认值</span>
                  </div>
                  <Row gutter={[12, 12]}>
                    {[
                      ['dl_hidden_size', '隐藏维度', { min: 16, max: 512, step: 16 }],
                      ['dl_num_layers', '网络层数', { min: 1, max: 8, step: 1 }],
                      ['dl_dropout', 'Dropout', { min: 0, max: 0.9, step: 0.05 }],
                      ['dl_n_epochs', '训练轮数', { min: 10, max: 1000, step: 10 }],
                      ['dl_batch_size', 'Batch Size', { min: 64, max: 10000, step: 64 }],
                      ['dl_lr', '学习率', { min: 0.00001, max: 0.1, step: 0.0001 }],
                      ['dl_step_len', '序列长度', { min: 5, max: 120, step: 5 }],
                    ].map(([key, label, lim]) => (
                      <Col span={12} key={key as string}>
                        <div className="space-y-1">
                          <div className="text-xs text-slate-500">{label as string}</div>
                          <InputNumber
                            value={params[key as keyof TrainingParams] as number}
                            min={(lim as any)?.min}
                            max={(lim as any)?.max}
                            step={(lim as any)?.step}
                            className="w-full"
                            onChange={(v) => onParamsChange({ ...params, [key as string]: Number(v ?? params[key as keyof TrainingParams]) })}
                          />
                        </div>
                      </Col>
                    ))}
                    {/* DL 模型专属参数：并进主网格，与序列长度凑成一行 */}
                    {params.model_type === 'tcn' && (
                      <Col span={12}>
                        <div className="space-y-1">
                          <div className="text-xs text-slate-500">卷积核大小 (kernel_size)</div>
                          <InputNumber
                            value={params.tcn_kernel_size ?? 5}
                            min={3}
                            max={15}
                            step={2}
                            className="w-full"
                            onChange={(v) => onParamsChange({ ...params, tcn_kernel_size: Number(v ?? 5) })}
                          />
                          <div className="text-[10px] text-slate-400">增大到 7+ 捕捉更长期依赖</div>
                        </div>
                      </Col>
                    )}
                    {params.model_type === 'nativetft' && (
                      <Col span={12}>
                        <div className="space-y-1">
                          <div className="text-xs text-slate-500">注意力头数 (num_heads)</div>
                          <InputNumber
                            value={params.tft_num_heads ?? 4}
                            min={1}
                            max={16}
                            step={1}
                            className="w-full"
                            onChange={(v) => onParamsChange({ ...params, tft_num_heads: Number(v ?? 4) })}
                          />
                          <div className="text-[10px] text-slate-400">需能被隐藏维度整除</div>
                        </div>
                      </Col>
                    )}
                  </Row>
                </>
              )}
            </div>
          </Card>

        </div>
      </Card>

      <Card className="rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20 } }}>
        <SectionHeader
          title="训练上下文"
          desc="记录训练时的资产、基准与交易成本，方便后续回放与模型管理页对齐。"
          icon={<MonitorPlay size={18} className="text-indigo-500" />}
        />
        <Divider className="my-4" />
        <div className="space-y-4">
          <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <div>
                <div className="mb-1 text-xs text-slate-500">初始资金</div>
                <InputNumber
                  value={context.initialCapital}
                  min={1000}
                  step={10000}
                  className="w-full"
                  onChange={(value) => onContextChange({ ...context, initialCapital: Number(value ?? context.initialCapital) })}
                />
              </div>
              <div>
                <div className="mb-1 text-xs text-slate-500">基准指数</div>
                <Select
                  value={context.benchmark}
                  className="w-full"
                  onChange={(value) => onContextChange({ ...context, benchmark: value })}
                  options={benchmarkOptions}
                />
              </div>
              <div>
                <div className="mb-1 text-xs text-slate-500">手续费率</div>
                <InputNumber
                  value={context.commissionRate}
                  min={0}
                  max={1}
                  step={0.0001}
                  className="w-full"
                  onChange={(value) => onContextChange({ ...context, commissionRate: Number(value ?? context.commissionRate) })}
                />
              </div>
              <div>
                <div className="mb-1 text-xs text-slate-500">滑点</div>
                <InputNumber
                  value={context.slippage}
                  min={0}
                  max={1}
                  step={0.0001}
                  className="w-full"
                  onChange={(value) => onContextChange({ ...context, slippage: Number(value ?? context.slippage) })}
                />
              </div>
              <div className="md:col-span-2">
                <div className="mb-1 text-xs text-slate-500">成交价格</div>
                <Select
                  value={context.dealPrice}
                  className="w-full"
                  onChange={(value) => onContextChange({ ...context, dealPrice: value as DealPrice })}
                  options={[
                    { label: '开盘价 (open)', value: 'open' },
                    { label: '收盘价 (close)', value: 'close' },
                  ]}
                />
              </div>
            </div>
          </div>

          {/* ── 行业编码作为特征 ── */}
          <div className="rounded-2xl border border-indigo-100 bg-white px-3 py-2">
            <div className="flex items-center justify-between gap-3">
              <div className="space-y-0.5">
                <div className="text-xs font-semibold text-slate-700">行业编码作为特征</div>
                <div className="text-[11px] text-slate-400 leading-relaxed">
                  将行业编码作为特征加入模型，CatBoost 原生支持类别特征
                </div>
              </div>
              <Tooltip title="将行业编码作为特征加入模型，仅 CatBoost 会按类别特征原生处理">
                <Switch
                  checked={!!context.industry_as_feature}
                  disabled={!isCatboost}
                  onChange={(checked) => onContextChange({ ...context, industry_as_feature: checked })}
                />
              </Tooltip>
            </div>
            {!isCatboost && (
              <div className="mt-2 text-[11px] text-amber-600">行业类别特征仅 CatBoost 原生支持，当前模型不可用。</div>
            )}
          </div>

          {/* ── 特征截面预处理 ── */}
          <div className="rounded-2xl border border-indigo-100 bg-white px-3 py-2">
            <div className="flex items-center justify-between gap-3">
              <div className="space-y-0.5">
                <div className="text-xs font-semibold text-slate-700">特征截面预处理</div>
                <div className="text-[11px] text-slate-400 leading-relaxed">
                  按交易日截面：缺失填充 + 分位缩尾 + 标准化。缩尾分位 / 标准化口径 / 填充方式可展开细调
                </div>
              </div>
              <Tooltip title="对特征做截面预处理：每交易日按特征填充缺失（停牌）、分位缩尾、标准化。开启后模型输入分布更规范，但会改变特征量纲（与旧模型不可直接对比）">
                <Switch
                  checked={!!params.preprocessingEnabled}
                  onChange={(checked) => onParamsChange({ ...params, preprocessingEnabled: checked })}
                />
              </Tooltip>
            </div>
            {!!params.preprocessingEnabled && (
              <div className="mt-2.5 grid grid-cols-3 gap-2 border-t border-indigo-50 pt-2.5">
                <div className="space-y-1">
                  <div className="text-[11px] text-slate-500">缩尾分位</div>
                  <Select
                    size="small"
                    className="w-full"
                    value={(params.preprocessingWinsorQ || [0.01, 0.99]).join('/')}
                    onChange={(v) => {
                      const [lo, hi] = v.split('/').map(Number);
                      onParamsChange({ ...params, preprocessingWinsorQ: [lo, hi] });
                    }}
                    options={[
                      { value: '0.005/0.995', label: '0.5% / 99.5%' },
                      { value: '0.01/0.99', label: '1% / 99%（默认）' },
                      { value: '0.025/0.975', label: '2.5% / 97.5%' },
                      { value: '0.05/0.95', label: '5% / 95%' },
                    ]}
                  />
                </div>
                <div className="space-y-1">
                  <div className="text-[11px] text-slate-500">标准化</div>
                  <Select
                    size="small"
                    className="w-full"
                    value={params.preprocessingStandardize || 'zscore'}
                    onChange={(v) =>
                      onParamsChange({ ...params, preprocessingStandardize: v as 'zscore' | 'rank' })
                    }
                    options={[
                      { value: 'zscore', label: 'Z-score（默认）' },
                      { value: 'rank', label: '截面百分位秩' },
                    ]}
                  />
                </div>
                <div className="space-y-1">
                  <div className="text-[11px] text-slate-500">缺失填充</div>
                  <Select
                    size="small"
                    className="w-full"
                    value={params.preprocessingFill || 'median'}
                    onChange={(v) =>
                      onParamsChange({ ...params, preprocessingFill: v as 'median' | 'zero' })
                    }
                    options={[
                      { value: 'median', label: '截面中位数（默认）' },
                      { value: 'zero', label: '填 0' },
                    ]}
                  />
                </div>
              </div>
            )}
          </div>

          {/* ── 收益率分位推理 ── */}
          <div className="rounded-2xl border border-indigo-100 bg-white px-3 py-2">
            <div className="flex items-center justify-between gap-3">
              <div className="space-y-0.5">
                <div className="text-xs font-semibold text-slate-700">收益率分位推理（P10 / P50 / P90）</div>
                <div className="text-[11px] text-slate-400 leading-relaxed">
                  训练三个 LightGBM 分位模型并做验证集校准。P50 保持作为交易信号；区间仅用于个股推理展示。
                </div>
              </div>
              <Tooltip title="训练三个 LightGBM 分位模型并做验证集校准，用于输出收益率的 P10/P50/P90 区间（仅支持 A 股单模型）">
                <Switch
                  checked={params.prediction_mode === 'quantile'}
                  disabled={quantileDisabled}
                  onChange={(checked) => onParamsChange({ ...params, prediction_mode: checked ? 'quantile' : 'point' })}
                />
              </Tooltip>
            </div>
            {(market !== 'CN' || !isSingleLgb || !isReturnTarget) ? (
              <div className="mt-2 text-[11px] text-amber-600">首版仅支持 A 股单 LightGBM；目标类型还需选择“回归目标（未来收益率）”。</div>
            ) : null}
          </div>

          {/* ── Walk-Forward 稳定性诊断 ── */}
          {onWfaChange && (
            <div className="rounded-2xl border border-indigo-100 bg-white px-3 py-2">
              <div className="flex items-start justify-between gap-3">
                <div className="space-y-0.5">
                  <div className="text-xs font-semibold text-slate-700">Walk-Forward 稳定性诊断</div>
                  <div className="text-[11px] text-slate-400 leading-relaxed">
                    滚动窗口训练并输出每个窗口的 IC，评估模型在不同历史区间上的稳定性与参数漂移。诊断在正式训练前执行，不产生正式模型。
                  </div>
                </div>
                <Switch
                  checked={!!wfa?.enabled}
                  disabled={!wfaSupported}
                  onChange={(checked) => onWfaChange({ ...(wfa || { enabled: false, strategy: 'rolling', nWindows: 4, trainYears: 3, valMonths: 12, stepMonths: 12 }), enabled: checked })}
                />
              </div>
              {!wfaSupported && (
                <div className="mt-2 text-[11px] text-amber-600">WFA 诊断仅支持树模型与线性模型（LightGBM / XGBoost / CatBoost / Ridge），当前模型后端会直接跳过。</div>
              )}

              {wfa?.enabled && (
                <>
                  <div className="mt-3 grid gap-3 md:grid-cols-2">
                    <div className="md:col-span-2">
                      <div className="mb-1 text-xs text-slate-500">窗口策略</div>
                      <Select
                        value={wfa.strategy}
                        onChange={(v) => onWfaChange({ ...wfa, strategy: v })}
                        className="w-full"
                        options={[
                          { label: '滚动窗口（固定训练长度）', value: 'rolling' },
                          { label: '扩张窗口（数据累积）', value: 'expanding' },
                        ]}
                      />
                      <div className="mt-1 text-[10px] text-slate-400">
                        {wfa.strategy === 'rolling' ? '每窗训练长度固定，避免老数据影响' : '训练集从起点累积，贴近实盘迭代'}
                      </div>
                    </div>
                    <div>
                      <div className="mb-1 text-xs text-slate-500">窗口数</div>
                      <InputNumber
                        min={1}
                        max={12}
                        value={wfa.nWindows}
                        onChange={(v) => onWfaChange({ ...wfa, nWindows: Number(v ?? 4) })}
                        className="w-full"
                      />
                      <div className="mt-1 text-[10px] text-slate-400">验证段数量（个）</div>
                    </div>
                    <div>
                      <div className="mb-1 text-xs text-slate-500">训练长度（年）</div>
                      <InputNumber
                        min={1}
                        max={8}
                        value={wfa.trainYears}
                        onChange={(v) => onWfaChange({ ...wfa, trainYears: Number(v ?? 3) })}
                        className="w-full"
                        disabled={wfa.strategy === 'expanding'}
                      />
                      <div className="mt-1 text-[10px] text-slate-400">每窗训练长度</div>
                    </div>
                    <div>
                      <div className="mb-1 text-xs text-slate-500">验证长度（月）</div>
                      <InputNumber
                        min={1}
                        max={36}
                        value={wfa.valMonths}
                        onChange={(v) => onWfaChange({ ...wfa, valMonths: Number(v ?? 12) })}
                        className="w-full"
                      />
                      <div className="mt-1 text-[10px] text-slate-400">每窗验证长度</div>
                    </div>
                    <div>
                      <div className="mb-1 text-xs text-slate-500">步长（月）</div>
                      <InputNumber
                        min={1}
                        max={36}
                        value={wfa.stepMonths}
                        onChange={(v) => onWfaChange({ ...wfa, stepMonths: Number(v ?? 12) })}
                        className="w-full"
                      />
                      <div className="mt-1 text-[10px] text-slate-400">窗口推进步长</div>
                    </div>
                  </div>
                  <Alert
                    className="mt-3 rounded-xl border-violet-100 bg-white/60"
                    type="info"
                    showIcon
                    message="诊断说明"
                    description="WFA 会额外运行多个窗口的训练，耗时约为基础训练的 2-3 倍。支持树模型（LightGBM/XGBoost/CatBoost）和线性模型，深度学习模型因耗时过长不参与诊断。"
                  />
                </>
              )}
            </div>
          )}

          <Alert
            type="warning"
            showIcon
            message="口径提醒"
            description="训练上下文会写入请求预览和模型元数据，保证模型管理页、回测中心和训练页使用同一套参数口径。"
            className="rounded-2xl border-amber-100 bg-amber-50/70"
          />
        </div>
      </Card>
    </div>
  );
};
