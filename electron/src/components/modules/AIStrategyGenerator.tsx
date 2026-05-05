import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Sparkles, Settings, ChevronDown, ChevronUp, Loader2, CheckCircle, AlertCircle, Code2 } from 'lucide-react';
import { aiStrategyService } from '../../services/aiStrategyService';
import { Strategy, StrategyParams } from '../../types/strategy';

interface AIStrategyGeneratorProps {
  onStrategyGenerated?: (strategy: Strategy) => void;
  onClose?: () => void;
  initialStockPool?: string[];
}

export const AIStrategyGenerator: React.FC<AIStrategyGeneratorProps> = ({
  onStrategyGenerated,
  onClose,
  initialStockPool = []
}) => {
  // 基础状态
  const [description, setDescription] = useState('');
  const [market, setMarket] = useState<'CN' | 'US' | 'HK' | 'GLOBAL'>('CN');
  const [riskLevel, setRiskLevel] = useState<'low' | 'medium' | 'high'>('medium');
  const [timeframe, setTimeframe] = useState<'1d' | '1h' | '15m'>('1d');

  // 高级参数
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [initialCapital, setInitialCapital] = useState(100000);
  const [positionSize, setPositionSize] = useState(10);
  const [maxPositions, setMaxPositions] = useState(5);
  const [stopLoss, setStopLoss] = useState(5);
  const [takeProfit, setTakeProfit] = useState(20);
  const [maxDrawdown, setMaxDrawdown] = useState(20);

  // 生成状态
  const [isGenerating, setIsGenerating] = useState(false);
  const [generatedStrategy, setGeneratedStrategy] = useState<Strategy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showCode, setShowCode] = useState(false);

  const handleGenerate = async () => {
    if (!description.trim()) {
      setError('请输入策略描述');
      return;
    }

    setIsGenerating(true);
    setError(null);
    setGeneratedStrategy(null);

    try {
      const params: StrategyParams = {
        description,
        market,
        riskLevel,
        style: 'custom',
        symbols: initialStockPool,
        timeframe,
        strategyLength: 'unlimited',
        backtestPeriod: '1year',
        initialCapital,
        positionSize,
        maxPositions,
        stopLoss,
        takeProfit,
        maxDrawdown
      };

      const strategy = await aiStrategyService.generateStrategy(params);
      setGeneratedStrategy(strategy);

      if (onStrategyGenerated) {
        onStrategyGenerated(strategy);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '策略生成失败');
      console.error('Strategy generation error:', err);
    } finally {
      setIsGenerating(false);
    }
  };

  const handleReset = () => {
    setDescription('');
    setGeneratedStrategy(null);
    setError(null);
    setShowCode(false);
  };

  return (
    <div className="h-full flex flex-col gap-4 p-6 bg-gray-50">
      {/* 头部 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-purple-500 to-blue-500 flex items-center justify-center">
            <Sparkles className="w-5 h-5 text-white" />
          </div>
          <div>
            <h2 className="text-xl font-bold text-gray-800">AI策略生成器</h2>
            <p className="text-sm text-gray-500">使用自然语言描述您的交易策略</p>
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="max-w-4xl mx-auto space-y-6">
          {/* 策略描述 */}
          <div className="bg-white rounded-xl border border-gray-200 p-6 shadow-sm">
            <label className="block text-sm font-medium text-gray-700 mb-3">
              策略描述 <span className="text-red-500">*</span>
            </label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="请描述策略逻辑，例如: 股价突破布林带下轨且成交量放大时买入，触及上轨时卖出..."
              className="w-full h-32 px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent resize-none text-gray-800 placeholder-gray-400"
              disabled={isGenerating}
            />
            {initialStockPool.length > 0 && (
              <div className="mt-3 p-3 bg-blue-50 border border-blue-100 rounded-lg">
                <p className="text-sm text-blue-700">
                  <span className="font-medium">已选股票池:</span> {initialStockPool.length} 只股票
                </p>
              </div>
            )}
          </div>

          {/* 基础参数 */}
          <div className="bg-white rounded-xl border border-gray-200 p-6 shadow-sm">
            <h3 className="text-sm font-medium text-gray-700 mb-4">基础参数</h3>
            <div className="grid grid-cols-3 gap-4">
              <div>
                <label className="block text-sm text-gray-600 mb-2">市场</label>
                <select
                  value={market}
                  onChange={(e) => setMarket(e.target.value as any)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent text-gray-800"
                  disabled={isGenerating}
                >
                  <option value="CN">中国A股</option>
                  <option value="US">美股</option>
                  <option value="HK">港股</option>
                  <option value="GLOBAL">全球</option>
                </select>
              </div>

              <div>
                <label className="block text-sm text-gray-600 mb-2">风险等级</label>
                <select
                  value={riskLevel}
                  onChange={(e) => setRiskLevel(e.target.value as any)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent text-gray-800"
                  disabled={isGenerating}
                >
                  <option value="low">低风险</option>
                  <option value="medium">中风险</option>
                  <option value="high">高风险</option>
                </select>
              </div>

              <div>
                <label className="block text-sm text-gray-600 mb-2">时间周期</label>
                <select
                  value={timeframe}
                  onChange={(e) => setTimeframe(e.target.value as any)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent text-gray-800"
                  disabled={isGenerating}
                >
                  <option value="15m">15分钟</option>
                  <option value="1h">1小时</option>
                  <option value="1d">日线</option>
                </select>
              </div>
            </div>
          </div>

          {/* 高级参数 */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
            <button
              onClick={() => setShowAdvanced(!showAdvanced)}
              className="w-full px-6 py-4 flex items-center justify-between hover:bg-gray-50 transition-colors"
            >
              <div className="flex items-center gap-2">
                <Settings className="w-4 h-4 text-gray-600" />
                <span className="text-sm font-medium text-gray-700">高级参数配置</span>
              </div>
              {showAdvanced ? (
                <ChevronUp className="w-5 h-5 text-gray-400" />
              ) : (
                <ChevronDown className="w-5 h-5 text-gray-400" />
              )}
            </button>

            <AnimatePresence>
              {showAdvanced && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: 0.2 }}
                  className="border-t border-gray-200"
                >
                  <div className="p-6 space-y-4">
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <label className="block text-sm text-gray-600 mb-2">初始资金 (元)</label>
                        <input
                          type="number"
                          value={initialCapital}
                          onChange={(e) => setInitialCapital(Number(e.target.value))}
                          className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent text-gray-800"
                          disabled={isGenerating}
                        />
                      </div>

                      <div>
                        <label className="block text-sm text-gray-600 mb-2">单次仓位 (%)</label>
                        <input
                          type="number"
                          value={positionSize}
                          onChange={(e) => setPositionSize(Number(e.target.value))}
                          min={1}
                          max={100}
                          className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent text-gray-800"
                          disabled={isGenerating}
                        />
                      </div>

                      <div>
                        <label className="block text-sm text-gray-600 mb-2">最大持仓数</label>
                        <input
                          type="number"
                          value={maxPositions}
                          onChange={(e) => setMaxPositions(Number(e.target.value))}
                          min={1}
                          max={20}
                          className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent text-gray-800"
                          disabled={isGenerating}
                        />
                      </div>

                      <div>
                        <label className="block text-sm text-gray-600 mb-2">止损 (%)</label>
                        <input
                          type="number"
                          value={stopLoss}
                          onChange={(e) => setStopLoss(Number(e.target.value))}
                          min={1}
                          max={50}
                          className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent text-gray-800"
                          disabled={isGenerating}
                        />
                      </div>

                      <div>
                        <label className="block text-sm text-gray-600 mb-2">止盈 (%)</label>
                        <input
                          type="number"
                          value={takeProfit}
                          onChange={(e) => setTakeProfit(Number(e.target.value))}
                          min={1}
                          max={100}
                          className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent text-gray-800"
                          disabled={isGenerating}
                        />
                      </div>

                      <div>
                        <label className="block text-sm text-gray-600 mb-2">最大回撤 (%)</label>
                        <input
                          type="number"
                          value={maxDrawdown}
                          onChange={(e) => setMaxDrawdown(Number(e.target.value))}
                          min={1}
                          max={50}
                          className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent text-gray-800"
                          disabled={isGenerating}
                        />
                      </div>
                    </div>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>

          {/* 操作按钮 */}
          <div className="flex gap-3">
            <button
              onClick={handleGenerate}
              disabled={isGenerating || !description.trim()}
              className="flex-1 px-6 py-3 bg-gradient-to-r from-purple-600 to-blue-600 text-white rounded-lg font-medium hover:from-purple-700 hover:to-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-all shadow-lg shadow-purple-200 flex items-center justify-center gap-2"
            >
              {isGenerating ? (
                <>
                  <Loader2 className="w-5 h-5 animate-spin" />
                  生成中...
                </>
              ) : (
                <>
                  <Sparkles className="w-5 h-5" />
                  生成策略
                </>
              )}
            </button>

            {generatedStrategy && (
              <button
                onClick={handleReset}
                className="px-6 py-3 bg-white border border-gray-300 text-gray-700 rounded-lg font-medium hover:bg-gray-50 transition-colors"
              >
                重置
              </button>
            )}
          </div>

          {/* 错误提示 */}
          {error && (
            <motion.div
              initial={{ opacity: 0, y: -10 }}
              animate={{ opacity: 1, y: 0 }}
              className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-3"
            >
              <AlertCircle className="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" />
              <div>
                <p className="text-sm font-medium text-red-800">生成失败</p>
                <p className="text-sm text-red-600 mt-1">{error}</p>
              </div>
            </motion.div>
          )}

          {/* 生成结果 */}
          {generatedStrategy && (
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden"
            >
              <div className="bg-gradient-to-r from-green-50 to-blue-50 border-b border-gray-200 px-6 py-4">
                <div className="flex items-center gap-3">
                  <CheckCircle className="w-6 h-6 text-green-500" />
                  <div>
                    <h3 className="font-bold text-gray-800">{generatedStrategy.name}</h3>
                    <p className="text-sm text-gray-600 mt-1">{generatedStrategy.description}</p>
                  </div>
                </div>
              </div>

              <div className="p-6 space-y-4">
                {/* 策略元数据 */}
                {generatedStrategy.metadata.rationale && (
                  <div>
                    <h4 className="text-sm font-medium text-gray-700 mb-2">策略说明</h4>
                    <p className="text-sm text-gray-600 leading-relaxed">
                      {generatedStrategy.metadata.rationale}
                    </p>
                  </div>
                )}

                {/* 代码预览 */}
                <div>
                  <button
                    onClick={() => setShowCode(!showCode)}
                    className="flex items-center gap-2 text-sm font-medium text-blue-600 hover:text-blue-700"
                  >
                    <Code2 className="w-4 h-4" />
                    {showCode ? '隐藏代码' : '查看代码'}
                  </button>

                  <AnimatePresence>
                    {showCode && (
                      <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        className="mt-3"
                      >
                        <pre className="bg-gray-900 text-gray-100 p-4 rounded-lg overflow-x-auto text-xs">
                          <code>{generatedStrategy.code}</code>
                        </pre>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>

                {/* 标签 */}
                {generatedStrategy.metadata.tags && generatedStrategy.metadata.tags.length > 0 && (
                  <div className="flex flex-wrap gap-2">
                    {generatedStrategy.metadata.tags.map((tag, idx) => (
                      <span
                        key={idx}
                        className="px-3 py-1 bg-blue-50 text-blue-700 text-xs rounded-full border border-blue-200"
                      >
                        {tag}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </motion.div>
          )}
        </div>
      </div>
    </div>
  );
};
