/**
 * Qlib策略参数配置器
 * 根据选定的策略类型动态渲染参数表单
 */
import React from 'react';
import { QlibStrategyParams } from '../../types/backtest/qlib';
import { HelpCircle } from 'lucide-react';
import { shouldShowNDrop } from '../../shared/qlib/strategyParams';

interface Props {
  strategyType: string;
  params: QlibStrategyParams;
  onChange: (params: QlibStrategyParams) => void;
  strategyCode?: string;
}

export const QlibStrategyConfigurator: React.FC<Props> = ({ strategyType, params, onChange, strategyCode }) => {
  const isLongShortTopk = strategyType === 'long_short_topk';
  const paramMap = params as Record<string, unknown>;
  const hasParam = (key: string) => paramMap[key] !== undefined && paramMap[key] !== null;
  const getNumberParam = (key: string, fallback: number) =>
    typeof paramMap[key] === 'number' ? (paramMap[key] as number) : fallback;

  // 判断是否应该显示 n_drop 参数
  const showNDrop = shouldShowNDrop(strategyCode, strategyType);

  const KNOWN_PARAM_KEYS = new Set([
    'topk',
    'short_topk',
    'n_drop',
    'min_score',
    'max_weight',
    'momentum_period',
    'lookback_days',
    'vol_lookback',
    'stop_loss',
    'take_profit',
    'long_exposure',
    'short_exposure',
    'market',
    'buy_cost',
    'sell_cost',
    'signal',
    'rebalance_days',
    'dynamic_position',
    'market_state_symbol',
    'enable_short_selling',
  ]);

  // 渲染通用的 TopK 滑块
  const renderTopK = (label = "持仓股票总数", min = 5, max = 100) => {
    const topk = params.topk !== undefined && params.topk !== null ? params.topk : 0;
    return (
      <div>
        <div className="flex items-center justify-between mb-2.5">
          <label className="flex items-center gap-2 text-sm text-gray-700">
            {label}
            <HelpCircle className="h-4 w-4 text-gray-400 cursor-help" />
          </label>
          <span className="text-sm font-mono text-gray-800 px-2 py-0.5 bg-gray-100 rounded-lg">
            {topk === 0 ? "不设限制 (0)" : topk}
          </span>
        </div>
        <input
          type="range"
          value={topk}
          onChange={(e) => onChange({ ...params, topk: Number(e.target.value) })}
          min={min}
          max={max}
          step={1}
          className="w-full h-2.5 bg-gray-200 rounded-2xl appearance-none cursor-pointer accent-blue-500"
        />
      </div>
    );
  };

  const renderShortTopK = (label = "空头持仓股票数", min = 0, max = 100) => {
    const shortTopk =
      params.short_topk !== undefined && params.short_topk !== null
        ? params.short_topk
        : 0;
    return (
      <div>
        <div className="flex items-center justify-between mb-2.5">
          <label className="flex items-center gap-2 text-sm text-gray-700">
            {label}
            <HelpCircle className="h-4 w-4 text-gray-400 cursor-help" />
          </label>
          <span className="text-sm font-mono text-gray-800 px-2 py-0.5 bg-gray-100 rounded-lg">
            {shortTopk}
          </span>
        </div>
        <input
          type="range"
          value={shortTopk}
          onChange={(e) => onChange({ ...params, short_topk: Number(e.target.value) })}
          min={min}
          max={max}
          step={1}
          className="w-full h-2.5 bg-gray-200 rounded-2xl appearance-none cursor-pointer accent-rose-500"
        />
      </div>
    );
  };


  // 渲染调仓数量滑块
  const renderNDrop = () => {
    const nDrop = params.n_drop !== undefined && params.n_drop !== null ? params.n_drop : 5;
    return (
      <div>
        <div className="flex items-center justify-between mb-2.5">
          <label className="flex items-center gap-2 text-sm text-gray-700">
            每日最大调仓数 (n_drop)
            <HelpCircle className="h-4 w-4 text-gray-400 cursor-help" />
          </label>
          <span className="text-sm font-mono text-gray-800 px-2 py-0.5 bg-gray-100 rounded-lg">
            {nDrop === 0 ? "不设限制 (0 => TOPK)" : nDrop}
          </span>
        </div>
        <input
          type="range"
          value={nDrop}
          onChange={(e) => onChange({ ...params, n_drop: Number(e.target.value) })}
          min={0}
          max={100}
          step={1}
          className="w-full h-2.5 bg-gray-200 rounded-2xl appearance-none cursor-pointer accent-blue-500"
        />
        {nDrop === 0 && (
          <div className="mt-1 text-[10px] text-blue-500 italic">
            * 设置为 0 表示只要股票跌出前 N 名即立即卖出，不设对冲限制。
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="bg-white rounded-2xl border border-gray-200 shadow-sm p-5 space-y-7">
      <div className="flex items-center gap-2 pb-2 border-b border-gray-50">
        <h3 className="font-bold text-gray-800 text-base tracking-tight">策略动态参数</h3>
        <div className="px-2 py-0.5 bg-blue-50 text-blue-600 rounded text-[11px] font-bold uppercase">{strategyType}</div>
      </div>

      <div className="space-y-7">
        {isLongShortTopk && (
          <>
            <div className="p-3 bg-gradient-to-r from-red-50 to-blue-50 rounded-xl border border-red-100 text-[12px] text-gray-700 leading-relaxed">
              当前模板按“两融多空”口径运行：做多最高分股票，同时做空最低分股票。空头侧默认复用固定融资融券股票池，前端显式参数优先，后端会再做兼容修复。
            </div>
            {renderTopK("多头持仓股票数", 5, 200)}
            {renderShortTopK("空头持仓股票数", 0, 200)}
            <div className="space-y-6">
              <div className="flex items-center justify-between px-1">
                <div className="flex flex-col">
                  <span className="text-[10px] text-gray-400 font-bold uppercase mb-1">空头比例</span>
                  <span className={`text-sm font-mono px-2 py-0.5 rounded-lg ${params.short_exposure && params.short_exposure > 0.01 ? 'bg-red-50 text-red-600 border border-red-100' : 'bg-gray-50 text-gray-400'}`}>
                    {params.short_exposure ? params.short_exposure.toFixed(2) : '0.00'}x
                  </span>
                </div>
                <div className="flex flex-col items-center">
                  <span className="text-[11px] font-extrabold text-gray-500 tracking-[0.2em]">
                    {params.long_exposure === params.short_exposure ? 'MARKET NEUTRAL' : 'EXPOSURE BIAS'}
                  </span>
                  <div className="h-6 w-[2px] bg-gradient-to-b from-transparent via-gray-300 to-transparent my-1"></div>
                  <span className="text-[10px] text-gray-400 font-mono">Net: {((params.long_exposure || 0) - (params.short_exposure || 0)).toFixed(2)}x</span>
                </div>
                <div className="flex flex-col items-end">
                  <span className="text-[10px] text-gray-400 font-bold uppercase mb-1">多头比例</span>
                  <span className={`text-sm font-mono px-2 py-0.5 rounded-lg ${params.long_exposure && params.long_exposure > 0.01 ? 'bg-blue-50 text-blue-600 border border-blue-100' : 'bg-gray-50 text-gray-400'}`}>
                    {params.long_exposure ? params.long_exposure.toFixed(2) : '0.00'}x
                  </span>
                </div>
              </div>
              <div className="relative pt-2 pb-2">
                <input
                  type="range"
                  value={((params.long_exposure || 0) - (params.short_exposure || 0)) * 100}
                  onChange={(e) => {
                    const net = Number(e.target.value) / 100;
                    // 核心逻辑：总敞口固定为 2.0 (1:1 杠杆上限)，通过净敞口 net (L-S) 计算 L 和 S
                    // long - short = net
                    // long + short = 2.0
                    // => 2*long = net + 2.0 => long = (net + 2) / 2
                    // => 2*short = 2.0 - net => short = (2 - net) / 2
                    const l = (net + 2) / 2;
                    const s = (2 - net) / 2;
                    onChange({ 
                      ...params, 
                      long_exposure: Number(l.toFixed(2)), 
                      short_exposure: Number(s.toFixed(2)) 
                    });
                  }}
                  min={-200}
                  max={200}
                  step={5}
                  className="w-full h-3 bg-gradient-to-r from-red-400 via-gray-200 to-blue-400 rounded-2xl appearance-none cursor-pointer accent-gray-900 border border-gray-100"
                />
                <div className="absolute left-1/2 -translate-x-1/2 -top-4 text-[10px] text-gray-400 font-bold bg-white px-1">NEUTRAL (0.00)</div>
                <div className="flex justify-between mt-2 px-1">
                  <span className="text-[9px] text-gray-400 font-bold">MAX SHORT (-2.0)</span>
                  <span className="text-[9px] text-gray-400 font-bold">MAX LONG (+2.0)</span>
                </div>
              </div>
              <div className="p-3 bg-gray-50 rounded-xl border border-gray-100">
                <p className="text-[11px] text-gray-500 leading-relaxed">
                  <strong className="text-gray-700">设计提示：</strong> 
                  滑块中间位置 (0) 代表 <span className="text-gray-900 font-bold">市场中性</span>（多空各 1.0x）。
                  系统将根据你的账户盈亏比例，在 1:1 杠杆配资范围内动态计算实际可用的最大授信额度。
                </p>
              </div>
            </div>
            <div>
              <div className="flex items-center justify-between mb-2">
                <label className="text-sm text-gray-700">单票绝对权重上限</label>
                <span className="text-sm font-mono text-gray-800 px-2 py-0.5 bg-gray-100 rounded-lg">
                  {((params.max_weight || 0.05) * 100).toFixed(1)}%
                </span>
              </div>
              <input
                type="range"
                value={(params.max_weight || 0.05) * 100}
                onChange={(e) => onChange({ ...params, max_weight: Number(e.target.value) / 100 })}
                min={1}
                max={20}
                step={0.5}
                className="w-full h-2.5 bg-gray-200 rounded-2xl appearance-none cursor-pointer accent-purple-500"
              />
            </div>
          </>
        )}

        {/* 1. TopK 系列 */}
        {!isLongShortTopk && hasParam('topk') && (
          <>
            {renderTopK()}
            {showNDrop && hasParam('n_drop') && renderNDrop()}
          </>
        )}

        {/* 2. 权重系列（含波动率加权） */}
        {!isLongShortTopk && hasParam('max_weight') && (
          <>
            {renderTopK("参与分配股票数", 10, 200)}
            {hasParam('min_score') && (
              <div>
                <div className="flex items-center justify-between mb-2">
                  <label className="text-sm text-gray-700">最低分阈值 (min_score)</label>
                  <span className="text-sm font-mono text-gray-800 px-2 py-0.5 bg-gray-100 rounded-lg">
                    {getNumberParam('min_score', 0).toFixed(4)}
                  </span>
                </div>
                <input
                  type="range"
                  value={getNumberParam('min_score', 0) * 1000}
                  onChange={(e) => onChange({ ...params, min_score: Number(e.target.value) / 1000 })}
                  min={-50}
                  max={200}
                  step={1}
                  className="w-full h-2.5 bg-gray-200 rounded-2xl appearance-none cursor-pointer accent-indigo-500"
                />
              </div>
            )}
            <div>
              <div className="flex items-center justify-between mb-2">
                <label className="text-sm text-gray-700">单只股票最大权重</label>
                <span className="text-sm font-mono text-gray-800 px-2 py-0.5 bg-gray-100 rounded-lg">{(getNumberParam('max_weight', 0.05) * 100).toFixed(1)}%</span>
              </div>
              <input
                type="range"
                value={getNumberParam('max_weight', 0.05) * 100}
                onChange={(e) => onChange({ ...params, max_weight: Number(e.target.value) / 100 })}
                min={1}
                max={20}
                step={0.5}
                className="w-full h-2.5 bg-gray-200 rounded-2xl appearance-none cursor-pointer accent-blue-500"
              />
            </div>
          </>
        )}

        {/* 3. 动量特有 */}
        {hasParam('momentum_period') && (
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-sm text-gray-700">动量回看周期 (天, momentum_period)</label>
              <span className="text-sm font-mono text-gray-800 px-2 py-0.5 bg-gray-100 rounded-lg">{getNumberParam('momentum_period', 20)}</span>
            </div>
            <input
              type="range"
              value={getNumberParam('momentum_period', 20)}
              onChange={(e) => onChange({ ...params, momentum_period: Number(e.target.value) })}
              min={5}
              max={60}
              step={1}
              className="w-full h-2.5 bg-gray-200 rounded-2xl appearance-none cursor-pointer accent-blue-500"
            />
          </div>
        )}

        {/* 4. 自适应提示 */}
        {strategyType === 'adaptive_drift' && (
          <div className="p-3 bg-indigo-50 rounded-xl text-[10px] text-indigo-700 leading-relaxed border border-indigo-100 italic">
            提示：该策略将根据基准指数波动率自动调整选股宽度。
          </div>
        )}

        {/* 5. 增强指数 */}
        {!isLongShortTopk && hasParam('market') && (
          <>
            {renderTopK("超配选股数量", 10, 100)}
            <div>
              <label className="text-sm text-gray-700 block mb-2">跟踪基准市场</label>
              <select
                value={params.market || 'csi300'}
                onChange={(e) => onChange({ ...params, market: e.target.value })}
                className="w-full px-3 py-2 bg-gray-50 border border-gray-200 rounded-xl text-sm focus:outline-none focus:border-blue-500"
              >
                <option value="csi300">沪深300 (csi300)</option>
                <option value="csi500">中证500 (csi500)</option>
              </select>
            </div>
          </>
        )}

        {/* 6. 风险平价 */}
        {!isLongShortTopk && hasParam('lookback_days') && (
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-sm text-gray-700">波动率估计回看周期 (天)</label>
              <span className="text-sm font-mono text-gray-800 px-2 py-0.5 bg-gray-100 rounded-lg">{getNumberParam('lookback_days', 60)}</span>
            </div>
            <input
              type="range"
              value={getNumberParam('lookback_days', 60)}
              onChange={(e) => onChange({ ...params, lookback_days: Number(e.target.value) })}
              min={20}
              max={120}
              step={5}
              className="w-full h-2.5 bg-gray-200 rounded-2xl appearance-none cursor-pointer accent-blue-500"
            />
          </div>
        )}

        {/* 8. 止损止盈 */}
        {(hasParam('stop_loss') || hasParam('take_profit')) && (
          <>
            <div>
              <div className="flex items-center justify-between mb-2">
                <label className="text-sm text-gray-700">止损阈值 (%)</label>
                <span className="text-sm font-mono text-gray-800 px-2 py-0.5 bg-gray-100 rounded-lg text-red-600">{(getNumberParam('stop_loss', -0.08) * 100).toFixed(0)}%</span>
              </div>
              <input
                type="range"
                value={Math.abs(getNumberParam('stop_loss', -0.08) * 100)}
                onChange={(e) => onChange({ ...params, stop_loss: -Number(e.target.value) / 100 })}
                min={1}
                max={30}
                step={1}
                className="w-full h-2.5 bg-gray-200 rounded-2xl appearance-none cursor-pointer accent-red-500"
              />
            </div>
            <div>
              <div className="flex items-center justify-between mb-2">
                <label className="text-sm text-gray-700">止盈阈值 (%)</label>
                <span className="text-sm font-mono text-gray-800 px-2 py-0.5 bg-gray-100 rounded-lg text-green-600">+{(getNumberParam('take_profit', 0.15) * 100).toFixed(0)}%</span>
              </div>
              <input
                type="range"
                value={getNumberParam('take_profit', 0.15) * 100}
                onChange={(e) => onChange({ ...params, take_profit: Number(e.target.value) / 100 })}
                min={5}
                max={50}
                step={1}
                className="w-full h-2.5 bg-gray-200 rounded-2xl appearance-none cursor-pointer accent-green-500"
              />
            </div>
          </>
        )}

        {/* 波动率加权专属控件 */}
        {hasParam('vol_lookback') && (
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-sm text-gray-700">波动率回看周期 (天)</label>
              <span className="text-sm font-mono text-gray-800 px-2 py-0.5 bg-gray-100 rounded-lg">{getNumberParam('vol_lookback', 20)}</span>
            </div>
            <input
              type="range"
              value={getNumberParam('vol_lookback', 20)}
              onChange={(e) => onChange({ ...params, vol_lookback: Number(e.target.value) })}
              min={5}
              max={60}
              step={1}
              className="w-full h-2.5 bg-gray-200 rounded-2xl appearance-none cursor-pointer accent-teal-500"
            />
            <div className="mt-1 text-[10px] text-gray-400 italic">
              基于近 N 天日度收益率标准差估算波动率，低波动标的获得更高权重
            </div>
          </div>
        )}

        {/* 9. 通用动态参数兜底（模板定义了数值/布尔参数即展示） */}
        {(() => {
          const extraNumberEntries = Object.entries(paramMap).filter(
            ([key, value]) => typeof value === 'number' && !KNOWN_PARAM_KEYS.has(key)
          );
          const extraBoolEntries = Object.entries(paramMap).filter(
            ([key, value]) => typeof value === 'boolean' && !KNOWN_PARAM_KEYS.has(key)
          );
          if (extraNumberEntries.length === 0 && extraBoolEntries.length === 0) return null;

          return (
            <div className="pt-4 border-t border-gray-100 space-y-3">
              <div className="text-xs font-bold text-gray-400 uppercase tracking-wider">模板扩展参数</div>
              {extraNumberEntries.map(([key, value]) => (
                <div key={key} className="flex items-center gap-3">
                  <label className="w-44 text-xs text-gray-600">{key}</label>
                  <input
                    type="number"
                    value={Number(value)}
                    step={0.01}
                    onChange={(e) => onChange({ ...params, [key]: Number(e.target.value) })}
                    className="flex-1 px-2 py-1 bg-gray-50 border border-gray-200 rounded text-xs font-mono text-gray-700 focus:outline-none"
                  />
                </div>
              ))}
              {extraBoolEntries.map(([key, value]) => (
                <label key={key} className="flex items-center gap-2 text-xs text-gray-700">
                  <input
                    type="checkbox"
                    checked={Boolean(value)}
                    onChange={(e) => onChange({ ...params, [key]: e.target.checked })}
                    className="h-3.5 w-3.5"
                  />
                  {key}
                </label>
              ))}
            </div>
          );
        })()}

        {/* 5. 费率统一显示 */}
        <div className="pt-4 border-t border-gray-100">
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-bold text-gray-400 uppercase tracking-wider">交易费率 (万分之)</span>
            <span className="text-xs text-gray-400 italic">已匹配 A 股标准</span>
          </div>
          <div className="space-y-3">
            <div className="flex items-center gap-3">
              <input
                type="number"
                value={((params.buy_cost || 0.00026) * 10000 - 0.1).toFixed(1)}
                onChange={(e) => {
                  const c = Number(e.target.value);
                  onChange({ ...params, buy_cost: (c + 0.1) / 10000, sell_cost: (c + 0.1 + 5.0) / 10000 });
                }}
                className="w-20 px-2 py-1 bg-gray-50 border border-gray-200 rounded text-xs font-mono text-gray-700 focus:outline-none"
              />
              <span className="text-[10px] text-gray-500">双向基础佣金</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
