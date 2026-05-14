import React, { useState } from 'react';
import { Settings, Play, TrendingUp, AlertCircle } from 'lucide-react';
import ReactECharts from 'echarts-for-react';
// backtestService will be loaded dynamically when needed
import { QlibBacktestConfig, QlibStrategyParams } from '../../types/backtest/qlib';
import { MultiStockCodeInput } from './MultiStockCodeInput';
import { BACKTEST_CONFIG } from '../../config/backtest';

interface OptimizationResult {
  optimization_id: string;
  status: string;
  best_params: any;
  best_fitness: number;
  history: Array<{
    generation: number;
    max_fitness: number;
    avg_fitness: number;
    std_fitness: number;
  }>;
}

export const QlibParameterOptimizer: React.FC = () => {
  // 基础配置
  const [symbols, setSymbols] = useState<string[]>([]);  // 改为数组，支持多股票
  const [startDate, setStartDate] = useState<string>(BACKTEST_CONFIG.QLIB.DEFAULT_START);
  const [endDate, setEndDate] = useState<string>(BACKTEST_CONFIG.QLIB.DEFAULT_END);
  const [initialCapital, setInitialCapital] = useState<number>(1000000);

  // 费率配置 (A股标准)
  const [commission, setCommission] = useState<number>(0.00025); // 万2.5
  const [minCommission, setMinCommission] = useState<number>(5.0); // 5元
  const [stampDuty, setStampDuty] = useState<number>(0.0005); // 万5
  const [transferFee, setTransferFee] = useState<number>(0.00001); // 万0.1

  // 优化配置
  const [populationSize, setPopulationSize] = useState(20);
  const [generations, setGenerations] = useState(10);

  // 参数范围配置
  const [paramRanges, setParamRanges] = useState([
    { name: 'topk', min: 10, max: 100, step: 10 },
    { name: 'min_score', min: 0, max: 0.1, step: 0.01 },
    { name: 'max_weight', min: 0.05, max: 0.3, step: 0.05 },
  ]);

  const [isRunning, setIsRunning] = useState(false);
  const [result, setResult] = useState<OptimizationResult | null>(null);
  const [progress, setProgress] = useState(0);
  const [status, setStatus] = useState('');
  const [taskId, setTaskId] = useState<string | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);

  const appendLog = (message: string) => {
    // console.log('[UI] appendLog:', message.substring(0, 50));
    // 简单的日志处理：如果消息包含换行符，拆分处理
    if (message.includes('\n')) {
        message.split('\n').forEach(line => {
            if (line.trim()) appendLog(line);
        });
        return;
    }

    const stamp = new Date().toLocaleTimeString();
    // 如果消息本身已经包含时间戳（后端Redis日志通常包含），就不再添加
    const displayMsg = message.startsWith('[') ? message : `${stamp} ${message}`;

    // 限制日志条数，防止内存溢出
    setLogLines([...logLines, displayMsg].slice(-200));
  };

  const handleRunOptimization = async () => {
    setIsRunning(true);
    setResult(null);
    setProgress(0);
    setStatus('pending');
    setTaskId(null);
    setLogLines([]);
    appendLog('已提交优化任务，等待执行');

    try {
      const config = {
        symbol: symbols.join(','), // 多股票用逗号分隔
        start_date: startDate,
        end_date: endDate,
        initial_capital: initialCapital,
        user_id: 'current_user',
        qlib_strategy_type: 'WeightStrategy',
        param_ranges: paramRanges,
        optimization_target: 'sharpe_ratio',
        population_size: populationSize,
        generations: generations,
        // 传递详细费率参数
        commission: commission,
        min_commission: minCommission,
        stamp_duty: stampDuty,
        transfer_fee: transferFee,
        qlib_strategy_params: {
             min_score: 0.0,
             max_weight: 1.0,
        }
      };

      // 启动日志轮询
      const logIndex = 0;
      const logInterval = setInterval(async () => {
          if (!result && isRunning) { // 简单检查，实际应该用更健壮的停止标志
             // 只有当获得了 optimization_id 后才开始轮询 (在 optimizeQlibParameters 内部很难拿到 task ID 对应的 optimization_id 除非它返回了)
             // 实际上 optimizeQlibParameters 是异步等待直到完成，所以我们只能在它返回后或者通过 onProgress 回调拿到 ID 吗？
             // 不，optimizeQlibParameters 现在是阻塞直到完成。
             // 我们需要一种机制在任务开始时就获得 ID。
             // optimizeQlibParameters 内部不仅轮询，而且它返回最终结果。
             // 现有的架构下，optimizeQlibParameters 封装了提交和轮询。
             // 我们无法在它返回前从外部轮询日志，除非我们修改 optimizeQlibParameters 返回 task info 而不是 promise result。
             // 或者，我们在 onProgress 回调里做日志轮询？
          }
      }, 2000);

      // 修正：由于 optimizeQlibParameters 内部封装了所有轮询逻辑，外部很难插入。
      // 最好的办法是利用 optimizeQlibParameters 的 onProgress 回调，或者修改 optimizeQlibParameters 支持 log 轮询。
      // 鉴于我们刚刚给 BacktestService 加了 getOptimizationLogs，我们应该在 BacktestService 内部的 pollOptimizationTask 中调用它。

      // 这里我们恢复使用 onLog 接口，但在 Service 层实现改为调用 getOptimizationLogs。
      // Wait, user asked to read from Redis directly from Frontend?
      // "前端直接从redis数据库读取日志" - well, frontend connects to Backend API which reads Redis. Frontend cannot connect to Redis directly usually.
      // My implementation: Backend API reads Redis, Frontend polls Backend API.

      // Let's rely on the service to handle log polling.
      const { backtestService } = await import('../../services/backtestService');
      const res = await backtestService.optimizeQlibParameters(
        {
          symbol: config.symbol,
          start_date: config.start_date,
          end_date: config.end_date,
          initial_capital: config.initial_capital,
          user_id: config.user_id,
          qlib_strategy_type: config.qlib_strategy_type,
          qlib_strategy_params: config.qlib_strategy_params,
          param_ranges: config.param_ranges,
          optimization_target: config.optimization_target,
          population_size: config.population_size,
          generations: config.generations,
          // 费率参数
          commission: config.commission,
          min_commission: config.min_commission,
          stamp_duty: config.stamp_duty,
          transfer_fee: config.transfer_fee,
        },
        {
          onTaskCreated: (id) => {
              setTaskId(id);
              appendLog(`任务ID: ${id}`);
          },
          onProgress: (nextProgress, nextStatus) => {
            setProgress(nextProgress);
            if (nextStatus) {
              setStatus(nextStatus);
              appendLog(nextStatus); // Status message
            } else {
              appendLog(`进度 ${Math.round(nextProgress * 100)}%`);
            }
          },
          onLog: (message) => {
              appendLog(message);
          }
        }
      );
      setResult(res);
      appendLog('优化完成');
    } catch (error: any) {
      console.error(error);
      appendLog(`优化失败: ${error.message || '未知错误'}`);
      alert('优化失败: ' + error.message);
    } finally {
      setIsRunning(false);
      setTaskId(null);
    }
  };

  // 渲染优化历史图表
  const getHistoryOption = () => {
    if (!result || !result.history) return {};

    const gens = result.history.map(h => h.generation);
    const maxFit = result.history.map(h => h.max_fitness);
    const avgFit = result.history.map(h => h.avg_fitness);

    return {
      title: { text: '优化收敛曲线' },
      tooltip: { trigger: 'axis' },
      legend: { data: ['最大适应度', '平均适应度'] },
      xAxis: { type: 'category', data: gens, name: '代数' },
      yAxis: { type: 'value', name: 'Fitness (Sharpe)' },
      series: [
        { name: '最大适应度', type: 'line', data: maxFit, smooth: true },
        { name: '平均适应度', type: 'line', data: avgFit, smooth: true, lineStyle: { type: 'dashed' } }
      ]
    };
  };

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="pb-4 border-b border-gray-200 flex justify-between items-center">
        <h2 className="text-lg font-semibold text-gray-800 flex items-center gap-2">
          <Settings className="w-5 h-5 text-purple-600" />
          策略参数优化（WeightedStrategy 遗传算法）
        </h2>
      </div>

      <div className="flex flex-1 overflow-hidden">
        {/* 左侧配置 */}
        <div className="w-[360px] border-r border-gray-200 p-4 overflow-y-auto flex flex-col gap-6 bg-gray-50/50">

          <div className="space-y-4">
            <h3 className="text-sm font-medium text-gray-700 uppercase tracking-wider">基础设置</h3>
            <div className="p-3 rounded-2xl border bg-gradient-to-r from-purple-50 to-indigo-50 border-purple-100 text-xs text-gray-600 leading-relaxed">
              口径：前端显式传入的参数优先；初始资金、成交价格与回测日期建议在当前入口中显式确认，避免和专家模式或 AI-IDE 的默认值混淆。
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">
                股票代码
                <span className="ml-2 text-xs text-blue-600">(支持多只，可留空)</span>
              </label>
              <MultiStockCodeInput
                value={symbols}
                onChange={setSymbols}
                allowEmpty={true}
                maxStocks={20}
              />
            </div>
            <div className="grid grid-cols-2 gap-2">
               <div>
                  <label className="block text-xs text-gray-500 mb-1">开始日期</label>
                  <input
                    type="date"
                    value={startDate}
                    onChange={e => setStartDate(e.target.value)}
                    min={BACKTEST_CONFIG.QLIB.DATA_START}
                    max={BACKTEST_CONFIG.QLIB.DATA_END}
                    className="w-full px-2 py-2 border rounded-2xl text-sm"
                  />
               </div>
               <div>
                  <label className="block text-xs text-gray-500 mb-1">结束日期</label>
                  <input
                    type="date"
                    value={endDate}
                    onChange={e => setEndDate(e.target.value)}
                    min={BACKTEST_CONFIG.QLIB.DATA_START}
                    max={BACKTEST_CONFIG.QLIB.DATA_END}
                    className="w-full px-2 py-2 border rounded-2xl text-sm"
                  />
               </div>
            </div>
            <div>
               <label className="block text-xs text-gray-500 mb-1">初始资金 (元)</label>
               <input
                 type="number"
                 value={initialCapital}
                 onChange={e => setInitialCapital(Number(e.target.value))}
                 className="w-full px-2 py-2 border rounded-2xl text-sm"
               />
            </div>
            <div className="grid grid-cols-2 gap-2">
               <div>
                  <label className="block text-xs text-gray-500 mb-1">佣金费率</label>
                  <input
                    type="number"
                    step="0.0001"
                    value={commission}
                    onChange={e => setCommission(Number(e.target.value))}
                    className="w-full px-2 py-2 border rounded-2xl text-sm"
                  />
               </div>
               <div>
                  <label className="block text-xs text-gray-500 mb-1">最低佣金 (元)</label>
                  <input
                    type="number"
                    value={minCommission}
                    onChange={e => setMinCommission(Number(e.target.value))}
                    className="w-full px-2 py-2 border rounded-2xl text-sm"
                  />
               </div>
               <div>
                  <label className="block text-xs text-gray-500 mb-1">印花税率 (卖出)</label>
                  <input
                    type="number"
                    step="0.0001"
                    value={stampDuty}
                    onChange={e => setStampDuty(Number(e.target.value))}
                    className="w-full px-2 py-2 border rounded-2xl text-sm"
                  />
               </div>
               <div>
                  <label className="block text-xs text-gray-500 mb-1">过户费率</label>
                  <input
                    type="number"
                    step="0.00001"
                    value={transferFee}
                    onChange={e => setTransferFee(Number(e.target.value))}
                    className="w-full px-2 py-2 border rounded-2xl text-sm"
                  />
               </div>
            </div>
          </div>

          <div className="space-y-4">
            <h3 className="text-sm font-medium text-gray-700 uppercase tracking-wider">算法参数</h3>
            <div className="grid grid-cols-2 gap-2">
               <div>
                  <label className="block text-xs text-gray-500 mb-1">种群大小</label>
                  <input type="number" value={populationSize} onChange={e => setPopulationSize(Number(e.target.value))} className="w-full px-2 py-2 border rounded-2xl text-sm" />
               </div>
               <div>
                  <label className="block text-xs text-gray-500 mb-1">迭代代数</label>
                  <input type="number" value={generations} onChange={e => setGenerations(Number(e.target.value))} className="w-full px-2 py-2 border rounded-2xl text-sm" />
               </div>
            </div>
          </div>

          <div className="flex-1">
             <h3 className="text-sm font-medium text-gray-700 uppercase tracking-wider mb-2">参数范围</h3>
             {paramRanges.map((range, idx) => (
               <div key={idx} className="bg-white p-3 rounded-2xl border mb-2 shadow-sm">
                 <div className="font-medium text-sm text-gray-800 mb-2">{range.name}</div>
                 <div className="grid grid-cols-3 gap-2">
                   <input
                     type="number"
                     placeholder="Min"
                     value={range.min}
                     onChange={e => {
                       const newRanges = [...paramRanges];
                       newRanges[idx].min = Number(e.target.value);
                       setParamRanges(newRanges);
                     }}
                     className="px-2 py-1 border rounded-2xl text-xs"
                   />
                   <input
                     type="number"
                     placeholder="Max"
                     value={range.max}
                     onChange={e => {
                       const newRanges = [...paramRanges];
                       newRanges[idx].max = Number(e.target.value);
                       setParamRanges(newRanges);
                     }}
                     className="px-2 py-1 border rounded-2xl text-xs"
                   />
                   <input
                     type="number"
                     placeholder="Step"
                     value={range.step}
                     onChange={e => {
                       const newRanges = [...paramRanges];
                       newRanges[idx].step = Number(e.target.value);
                       setParamRanges(newRanges);
                     }}
                     className="px-2 py-1 border rounded-2xl text-xs"
                   />
                 </div>
               </div>
             ))}
          </div>

          <div className="flex gap-3">
            <button
              onClick={handleRunOptimization}
              disabled={isRunning}
              className="flex-1 py-3 bg-purple-600 hover:bg-purple-700 text-white rounded-2xl shadow font-medium disabled:opacity-50 flex items-center justify-center gap-2"
            >
              {isRunning ? (
                <>
                  <div className="animate-spin rounded-full h-4 w-4 border-2 border-white border-t-transparent"></div>
                  优化中...
                </>
              ) : (
                <>
                  <Play className="w-4 h-4" />
                  开始优化
                </>
              )}
            </button>

            <button
              onClick={async () => {
                  if (taskId) {
                      try {
                          await backtestService.stopTask(taskId);
                          appendLog('正在停止任务...');
                      } catch (e: any) {
                          appendLog(`停止失败: ${e.message}`);
                      }
                  } else {
                      setIsRunning(false);
                      setStatus('cancelled');
                      appendLog('用户手动停止优化 (本地)');
                  }
              }}
              disabled={!isRunning}
              className="flex-1 py-3 bg-red-500 hover:bg-red-600 text-white rounded-2xl shadow font-medium disabled:opacity-50 flex items-center justify-center gap-2"
            >
              <AlertCircle className="w-4 h-4" />
              停止优化
            </button>
          </div>

          {isRunning && (
            <div className="bg-white border border-purple-100 rounded-2xl p-3 text-sm">
              <div className="flex items-center justify-between text-gray-700 mb-2">
                <span>优化进度</span>
                <span>{Math.round(progress * 100)}%</span>
              </div>
              <div className="w-full h-2 bg-purple-100 rounded-full overflow-hidden">
                <div
                  className="h-full bg-purple-500 rounded-full transition-all"
                  style={{ width: `${Math.round(progress * 100)}%` }}
                />
              </div>
              {status && (
                <div className="text-xs text-gray-500 mt-2">状态: {status}</div>
              )}
            </div>
          )}
        </div>

        {/* 右侧结果 */}
        <div className="flex-1 bg-white p-6 overflow-y-auto">
          <div className="flex flex-col h-full gap-6">
            <div className="border border-gray-200 rounded-2xl p-4 bg-slate-50">
              <div className="flex items-center justify-between text-sm text-gray-700">
                <span className="font-medium">优化进度</span>
                <span>{Math.round(progress * 100)}%</span>
              </div>
              <div className="mt-2 w-full h-2 bg-gray-200 rounded-full overflow-hidden">
                <div
                  className="h-full bg-purple-500 rounded-full transition-all"
                  style={{ width: `${Math.round(progress * 100)}%` }}
                />
              </div>
              <div className="mt-2 text-xs text-gray-600">
                当前状态: {status || 'pending'}
              </div>
              <div className="mt-3">
                <div className="text-xs font-medium text-gray-700 mb-2">
                  运行日志
                </div>
                <div className="h-64 md:h-80 overflow-y-auto bg-white border border-gray-200 rounded-xl p-2 font-mono text-xs text-gray-700">
                  {logLines.length ? (
                    logLines.map((line, idx) => (
                      <div key={`${line}-${idx}`}>{line}</div>
                    ))
                  ) : (
                    <div className="text-gray-400">暂无日志</div>
                  )}
                </div>
              </div>
            </div>
            {result ? (
              <div className="space-y-6">
                {/* 最佳结果卡片 */}
                <div className="bg-gradient-to-r from-purple-50 to-indigo-50 p-6 rounded-2xl border border-purple-100">
                  <div className="flex items-center gap-2 mb-4">
                    <TrendingUp className="w-6 h-6 text-purple-600" />
                    <h3 className="text-xl font-bold text-gray-800">优化完成</h3>
                  </div>
                  <div className="grid grid-cols-2 gap-8">
                    <div>
                      <div className="text-sm text-gray-500 mb-1">
                        最佳适应度 (Sharpe)
                      </div>
                      <div className="text-3xl font-bold text-purple-700">
                        {result.best_fitness.toFixed(4)}
                      </div>
                    </div>
                    <div>
                      <div className="text-sm text-gray-500 mb-2">
                        最佳参数组合
                      </div>
                      <div className="bg-white/60 rounded-2xl p-2 font-mono text-sm border border-purple-100">
                        {Object.entries(result.best_params).map(([k, v]) => (
                          <div key={k} className="flex justify-between">
                            <span className="text-gray-600">{k}:</span>
                            <span className="font-bold text-gray-800">
                              {String(v)}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>

                {/* 收敛曲线 */}
                <div className="h-[400px] border border-gray-200 rounded-2xl p-4">
                  <ReactECharts
                    option={getHistoryOption()}
                    style={{ height: '100%' }}
                  />
                </div>
              </div>
            ) : (
              <div className="flex-1 flex items-center justify-center text-gray-400">
                <div className="flex flex-col items-center gap-2">
                  <Settings className="w-16 h-16 opacity-20" />
                  <p>配置参数范围并点击开始优化</p>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
