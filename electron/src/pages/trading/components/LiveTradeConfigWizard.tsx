import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, message, Modal, Steps } from 'antd';
import { CheckCircle2 } from 'lucide-react';
import type {
  DeployMode,
  ExecutionConfig,
  LiveTradeConfig,
  StrategyLiveDefaults,
} from '../../../types/liveTrading';
import { validateLiveTradeConfig } from '../utils/liveTradeConfigValidation';
import LiveTradeConfigForm from './LiveTradeConfigForm';

type Props = {
  open: boolean;
  mode: DeployMode;
  strategyId: string;
  strategyName: string;
  strategyDefaults?: StrategyLiveDefaults | null;
  initialExecutionConfig?: ExecutionConfig | null;
  initialLiveTradeConfig?: Partial<LiveTradeConfig> | null;
  onCancel: () => void;
  onConfirm: (payload: { execution_config: ExecutionConfig; live_trade_config: LiveTradeConfig }) => Promise<void>;
};

const DEFAULT_EXECUTION_CONFIG: ExecutionConfig = {
  max_buy_drop: -0.03,
  stop_loss: -0.08,
};

const DEFAULT_LIVE_TRADE_CONFIG: LiveTradeConfig = {
  rebalance_days: 3,
  schedule_type: 'interval',
  trade_weekdays: [],
  enabled_sessions: ['AM'],
  sell_time: '09:30',
  buy_time: '09:30',
  sell_first: true,
  order_type: 'MARKET',
  max_price_deviation: 0.02,
  max_orders_per_cycle: 20,
};

function buildInitialState(
  defaults?: StrategyLiveDefaults | null,
  initialExecutionConfig?: ExecutionConfig | null,
  initialLiveTradeConfig?: Partial<LiveTradeConfig> | null,
) {
  return {
    execution_config: {
      ...DEFAULT_EXECUTION_CONFIG,
      ...(defaults?.execution_defaults || {}),
      ...(initialExecutionConfig || {}),
    },
    live_trade_config: {
      ...DEFAULT_LIVE_TRADE_CONFIG,
      ...(defaults?.live_defaults || {}),
      ...(initialLiveTradeConfig || {}),
    } as LiveTradeConfig,
  };
}

const LiveTradeConfigWizard: React.FC<Props> = ({
  open,
  mode,
  strategyId,
  strategyName,
  strategyDefaults,
  initialExecutionConfig,
  initialLiveTradeConfig,
  onCancel,
  onConfirm,
}) => {
  const [step, setStep] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [executionConfig, setExecutionConfig] = useState<ExecutionConfig>(DEFAULT_EXECUTION_CONFIG);
  const [liveTradeConfig, setLiveTradeConfig] = useState<LiveTradeConfig>(DEFAULT_LIVE_TRADE_CONFIG);
  const initializedKeyRef = useRef<string | null>(null);

  useEffect(() => {
    if (!open) {
      initializedKeyRef.current = null;
      return;
    }
    const nextKey = `${strategyId}:${mode}`;
    if (initializedKeyRef.current === nextKey) {
      return;
    }
    const initial = buildInitialState(strategyDefaults, initialExecutionConfig, initialLiveTradeConfig);
    setExecutionConfig(initial.execution_config);
    setLiveTradeConfig(initial.live_trade_config);
    setStep(0);
    initializedKeyRef.current = nextKey;
  }, [open, mode, strategyId, strategyDefaults, initialExecutionConfig, initialLiveTradeConfig]);

  const issues = useMemo(() => validateLiveTradeConfig(liveTradeConfig), [liveTradeConfig]);
  const tips = strategyDefaults?.live_config_tips || [];
  const modeLabel = mode === 'SIMULATION' ? '模拟盘' : (mode === 'SHADOW' ? '影子模式' : '实盘');
  const orderTypeLabel = liveTradeConfig.order_type === 'LIMIT' ? '限价' : '市价';

  const summaryRows = useMemo(
    () => [
      { label: '策略', value: strategyName || strategyId },
      { label: '模式', value: modeLabel },
      {
        label: '调仓',
        value:
          liveTradeConfig.schedule_type === 'interval'
            ? `每 ${liveTradeConfig.rebalance_days} 个交易日`
            : `每周 ${liveTradeConfig.trade_weekdays?.join(' / ') || '-'}`,
      },
      {
        label: '买卖时点',
        value: liveTradeConfig.sell_first
          ? `先卖后买，${liveTradeConfig.sell_time} / ${liveTradeConfig.buy_time}`
          : `${liveTradeConfig.buy_time}`,
      },
      {
        label: '执行方式',
        value:
          liveTradeConfig.order_type === 'LIMIT'
            ? `${orderTypeLabel}，偏离 ${((liveTradeConfig.max_price_deviation || 0) * 100).toFixed(1)}%`
            : orderTypeLabel,
      },
      {
        label: '风控',
        value: `大跌拦截 ${((executionConfig.max_buy_drop || 0) * 100).toFixed(1)}%，止损 ${((executionConfig.stop_loss || 0) * 100).toFixed(1)}%`,
      },
    ],
    [strategyId, strategyName, modeLabel, orderTypeLabel, liveTradeConfig, executionConfig],
  );

  const handleNext = async () => {
    if (step === 0) {
      if (issues.length > 0) {
        message.error(issues[0].message);
        return;
      }
      setStep(1);
      return;
    }
    if (step === 1) {
      setStep(2);
      return;
    }

    try {
      setSubmitting(true);
      await onConfirm({
        execution_config: executionConfig,
        live_trade_config: liveTradeConfig,
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="模拟执行参数"
      open={open}
      onCancel={onCancel}
      width={860}
      footer={null}
      destroyOnHidden
      centered={step > 0}
      style={step === 0 ? { top: 24 } : undefined}
      styles={{
        body: {
          paddingTop: 16,
          paddingBottom: 16,
        },
      }}
    >
      <div className={`flex flex-col ${step === 0 ? 'min-h-[560px]' : step === 1 ? 'min-h-[420px]' : 'min-h-[260px]'}`}>
        <Steps
          current={step}
          items={[
            { title: '配置参数' },
            { title: '确认摘要' },
            { title: '提交启动' },
          ]}
        />

        <div
          className="flex-1 transition-[min-height] duration-300 ease-out"
          style={{ minHeight: step === 0 ? 620 : step === 1 ? 240 : 80 }}
        >
          {step === 0 && (
            <div className="pt-3 space-y-2.5 animate-in fade-in-0 slide-in-from-bottom-1 duration-300">
              <Alert
                type="info"
                showIcon
                message={`策略 ${strategyName || strategyId} 首次模拟交易前需要确认调仓节奏与买卖时点`}
              />
              {tips.length > 0 && (
                <div className="rounded-2xl border border-blue-100 bg-blue-50 px-4 py-2.5 text-sm text-blue-900">
                  <div className="mb-1.5 font-semibold">推荐说明</div>
                  <ul className="list-disc pl-5 space-y-1">
                    {tips.map((tip, idx) => (
                      <li key={`${strategyId}-tip-${idx}`}>{tip}</li>
                    ))}
                  </ul>
                </div>
              )}
              <LiveTradeConfigForm
                executionConfig={executionConfig}
                liveTradeConfig={liveTradeConfig}
                onExecutionConfigChange={setExecutionConfig}
                onLiveTradeConfigChange={setLiveTradeConfig}
                validationIssues={issues}
              />
            </div>
          )}

          {step === 1 && (
            <div className="flex h-full min-h-[240px] items-center justify-center py-2 animate-in fade-in-0 zoom-in-95 duration-300">
              <div className="w-full max-w-[720px] space-y-3">
                <Alert type="warning" showIcon message="请确认本次启动将使用以下执行参数" />
                <div className="rounded-2xl border border-gray-200 bg-gray-50 p-3.5">
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    {summaryRows.map((item) => (
                      <div key={item.label} className="rounded-xl border border-gray-100 bg-white px-4 py-3">
                        <div className="mb-1 text-xs text-gray-500">{item.label}</div>
                        <div className="text-sm font-semibold text-gray-900">{item.value}</div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          )}

          {step === 2 && (
            <div className="flex h-full min-h-[120px] items-center justify-center py-2 animate-in fade-in-0 zoom-in-95 duration-300">
              <div className="w-full max-w-[560px]">
                <div className="rounded-2xl border border-green-300 bg-green-50 px-8 py-8 text-center">
                  <div className="mb-4 flex justify-center">
                    <div className="flex h-14 w-14 items-center justify-center rounded-full bg-green-500 text-white shadow-sm">
                      <CheckCircle2 size={28} />
                    </div>
                  </div>
                  <div className="text-[17px] font-semibold text-green-900">参数确认完成</div>
                  <div className="mx-auto mt-3 max-w-[420px] text-sm leading-7 text-green-900/85">
                    点击“确认启动”后，将继续执行既有交易准备度检测和启动流程。
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 pt-3">
          <Button onClick={onCancel}>取消</Button>
          {step > 0 && <Button onClick={() => setStep(step - 1)}>上一步</Button>}
          <Button type="primary" loading={submitting} onClick={handleNext}>
            {step === 2 ? '确认启动' : '下一步'}
          </Button>
        </div>
      </div>
    </Modal>
  );
};

export default LiveTradeConfigWizard;
