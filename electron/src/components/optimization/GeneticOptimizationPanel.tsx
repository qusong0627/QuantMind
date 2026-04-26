import React, { useState, useEffect, useRef } from 'react';
import { GeneticParamForm, GeneticConfig } from './GeneticParamForm';
import { backtestService } from '../../services/backtestService';
import { AlertCircle, TrendingUp, Activity, BarChart2 } from 'lucide-react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';
import { authService } from '../../features/auth/services/authService';
import { useBacktestCenterStore } from '../../stores/backtestCenterStore';

interface OptimizationLog {
    timestamp: string;
    message: string;
    type: 'info' | 'success' | 'warning' | 'error';
}

interface GenerationStat {
    generation: number;
    max_fitness: number;
    avg_fitness: number;
    std_fitness: number;
}

export const GeneticOptimizationPanel: React.FC = () => {
    const [isRunning, setIsRunning] = useState(false);
    const [config, setConfig] = useState<GeneticConfig | null>(null);
    const [logs, setLogs] = useState<OptimizationLog[]>([]);
    const [stats, setStats] = useState<GenerationStat[]>([]);
    const [globalError, setGlobalError] = useState<string>('');
    const [progress, setProgress] = useState(0);
    const [currentGeneration, setCurrentGeneration] = useState(0);
    const [optimizationId, setOptimizationId] = useState<string>('');
    const [bestResult, setBestResult] = useState<any>(null);
    const [currentTaskId, setCurrentTaskId] = useState<string | null>(null);
    const [applySuccess, setApplySuccess] = useState(false);

    const abortControllerRef = useRef<AbortController | null>(null);
    const isAbortedRef = useRef(false);
    const logContainerRef = useRef<HTMLDivElement>(null);
    const updateBacktestConfig = useBacktestCenterStore((state) => state.updateBacktestConfig);
    const setActiveModule = useBacktestCenterStore((state) => state.setActiveModule);

    const getCurrentUserId = () => {
        const storedUser = authService.getStoredUser() as any;
        return String(storedUser?.user_id || storedUser?.id || storedUser?.username || '').trim();
    };

    // Auto-scroll logs
    useEffect(() => {
        if (logContainerRef.current) {
            logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
        }
    }, [logs]);

    const addLog = (message: string, type: OptimizationLog['type'] = 'info') => {
        const newLog: OptimizationLog = {
            timestamp: new Date().toLocaleTimeString(),
            message,
            type
        };
        setLogs([...logs, newLog]);
    };

    const runGeneticOptimization = async (geneticConfig: GeneticConfig) => {
        setGlobalError('');
        setConfig(geneticConfig);
        setIsRunning(true);
        isAbortedRef.current = false;
        setProgress(0);
        setLogs([]);
        setStats([]);
        setBestResult(null);
        setCurrentGeneration(0);
        setApplySuccess(false);

        abortControllerRef.current = new AbortController();

        const optimizationId = crypto.randomUUID().replace(/-/g, '');
        setOptimizationId(optimizationId);

        addLog(`准备开始遗传算法优化 (Population=${geneticConfig.ga.population_size}, Gens=${geneticConfig.ga.generations})`, 'info');

        try {
            // 构造请求
            const optimizationRequest = {
                optimization_id: optimizationId,
                base_request: {
                    strategy_type: 'WeightedStrategy',
                    strategy_params: { topk: 50, min_score: 0.02, max_weight: 0.1 },
                    start_date: geneticConfig.dateRange.startDate,
                    end_date: geneticConfig.dateRange.endDate,
                    initial_capital: 10000000,
                    benchmark: 'SH000300',
                    universe: 'csi300',
                    user_id: getCurrentUserId()
                },
                param_ranges: [
                    {
                        name: 'topk',
                        min: geneticConfig.parameters.topk.min,
                        max: geneticConfig.parameters.topk.max,
                        step: geneticConfig.parameters.topk.step
                    },
                    {
                        name: 'min_score',
                        min: geneticConfig.parameters.min_score.min,
                        max: geneticConfig.parameters.min_score.max,
                        step: geneticConfig.parameters.min_score.step
                    },
                    {
                        name: 'max_weight',
                        min: geneticConfig.parameters.max_weight.min,
                        max: geneticConfig.parameters.max_weight.max,
                        step: geneticConfig.parameters.max_weight.step
                    }
                ],
                optimization_target: geneticConfig.metric,
                population_size: geneticConfig.ga.population_size,
                generations: geneticConfig.ga.generations,
                mutation_rate: geneticConfig.ga.mutation_rate,
                max_parallel: 5
            };

            // 调用API
            const response = await backtestService.optimizeQlibParameters(
                {
                    optimization_id: optimizationRequest.optimization_id,
                    symbol: optimizationRequest.base_request.universe,
                    start_date: optimizationRequest.base_request.start_date,
                    end_date: optimizationRequest.base_request.end_date,
                    initial_capital: optimizationRequest.base_request.initial_capital,
                    user_id: optimizationRequest.base_request.user_id,
                    qlib_strategy_type: 'WeightedStrategy',
                    qlib_strategy_params: optimizationRequest.base_request.strategy_params,
                    param_ranges: optimizationRequest.param_ranges,
                    optimization_target: optimizationRequest.optimization_target,
                    population_size: optimizationRequest.population_size,
                    generations: optimizationRequest.generations,
                    mutation_rate: optimizationRequest.mutation_rate,
                    max_parallel: optimizationRequest.max_parallel
                },
                {
                    onTaskCreated: (id) => {
                        setCurrentTaskId(id);
                        addLog(`任务已创建，ID: ${id}`, 'success');
                    },
                    signal: abortControllerRef.current.signal,
                    onProgress: (p, status) => {
                        if (isAbortedRef.current) return;
                        // 这里的 progress 是 0-1
                    },
                    onLog: (msg) => {
                        if (isAbortedRef.current) return;
                        addLog(msg);

                        // 尝试从日志中解析统计数据用于绘图 (Hack: 后端日志格式固定)
                        // e.g., "第 1 代统计: 最大适应度=0.1234, 平均适应度=0.1000..."
                        if (msg.includes('代统计')) {
                            try {
                                const genMatch = msg.match(/第 (\d+) 代/);
                                const maxMatch = msg.match(/最大适应度=([-\d.]+)/);
                                const avgMatch = msg.match(/平均适应度=([-\d.]+)/);

                                if (genMatch && maxMatch && avgMatch) {
                                    const gen = parseInt(genMatch[1]);
                                    const maxFit = parseFloat(maxMatch[1]);
                                    const avgFit = parseFloat(avgMatch[1]);

                                    setCurrentGeneration(gen);
                                    setProgress(gen / geneticConfig.ga.generations);

                                    // 避免重复
                                    if (!stats.some(s => s.generation === gen)) {
                                        const newStat: GenerationStat = {
                                            generation: gen,
                                            max_fitness: maxFit,
                                            avg_fitness: avgFit,
                                            std_fitness: 0
                                        };
                                        setStats([...stats, newStat].sort((a, b) => a.generation - b.generation));
                                    }
                                }
                            } catch (e) {
                                console.error('Log parsing error', e);
                            }
                        }
                    }
                }
            );

            if (isAbortedRef.current) return;

            if (response && response.best_params) {
                setBestResult(response);
                addLog('优化完成！', 'success');
                setProgress(1);

                // 确保最后的统计数据完整
                if (response.history) {
                    setStats(response.history.map((h: any) => ({
                        generation: h.generation,
                        max_fitness: h.max_fitness,
                        avg_fitness: h.avg_fitness,
                        std_fitness: h.std_fitness
                    })));
                }
            }

        } catch (error) {
            if (!isAbortedRef.current && (error as Error)?.name !== 'AbortError') {
                const msg = error instanceof Error ? error.message : '优化过程出错';
                setGlobalError(msg);
                addLog(`错误: ${msg}`, 'error');
            }
        } finally {
            setIsRunning(false);
            abortControllerRef.current = null;
            setCurrentTaskId(null);
        }
    };

    const handleStop = async () => {
        isAbortedRef.current = true;
        const taskId = currentTaskId;
        if (taskId) {
            try {
                await backtestService.stopTask(taskId);
                addLog(`已发送停止信号: ${taskId}`, 'warning');
            } catch (error) {
                console.warn('停止遗传优化任务失败', error);
            }
        }
        if (abortControllerRef.current) {
            abortControllerRef.current.abort();
        }
        setIsRunning(false);
        addLog('用户手动停止优化', 'warning');
    };

    const handleApplyBestParams = () => {
        if (!bestResult?.best_params || !config) return;
        updateBacktestConfig({
            start_date: config.dateRange.startDate,
            end_date: config.dateRange.endDate,
            initial_capital: 10000000,
            qlib_strategy_type: 'WeightedStrategy',
            qlib_strategy_params: {
                ...bestResult.best_params,
            },
            strategy_params: {
                ...bestResult.best_params,
            },
        });
        setActiveModule('quick-backtest');
        setApplySuccess(true);
    };

    return (
        <div className="space-y-6">
            <div className="bg-gradient-to-r from-indigo-50 to-purple-50 border border-indigo-200 rounded-2xl p-4">
                <h2 className="text-xl font-bold text-indigo-900 mb-2">遗传算法优化 (Genetic Algorithm)</h2>
                <p className="text-sm text-indigo-800">
                    适用于 WeightedStrategy 复杂参数空间。模拟生物进化过程（选择、交叉、变异），高效寻找 topk、min_score 和 max_weight 的最佳组合。
                </p>
            </div>

            <GeneticParamForm
                onStartOptimization={runGeneticOptimization}
                isRunning={isRunning}
            />

            {isRunning && (
                <div className="flex justify-center mb-4">
                    <button
                        onClick={handleStop}
                        className="px-6 py-2 bg-red-600 text-white rounded-2xl hover:bg-red-700 transition-colors shadow-md"
                    >
                        停止优化
                    </button>
                </div>
            )}

            {/* 实时进度与日志面板 */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                {/* 左侧：代际进化图表 */}
                <div className="bg-white rounded-2xl border border-gray-200 p-4 shadow-sm min-h-[300px]">
                    <div className="flex items-center justify-between mb-4">
                        <h3 className="font-semibold text-gray-800 flex items-center gap-2">
                            <TrendingUp className="w-5 h-5 text-indigo-600" />
                            代际进化趋势
                        </h3>
                        <span className="text-xs text-gray-500">当前代数: {currentGeneration}/{config?.ga.generations || '-'}</span>
                    </div>

                    {stats.length > 0 ? (
                        <div style={{ width: '100%', height: 250 }}>
                            <ResponsiveContainer>
                                <LineChart data={stats}>
                                    <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e5e7eb" />
                                    <XAxis
                                        dataKey="generation"
                                        label={{ value: 'Generation', position: 'insideBottom', offset: -5 }}
                                        stroke="#9ca3af"
                                        fontSize={12}
                                        tickLine={false}
                                    />
                                    <YAxis
                                        stroke="#9ca3af"
                                        fontSize={12}
                                        tickLine={false}
                                        domain={['auto', 'auto']}
                                    />
                                    <Tooltip
                                        contentStyle={{ borderRadius: '8px', border: 'none', boxShadow: '0 4px 6px -1px rgba(0, 0, 0, 0.1)' }}
                                    />
                                    <Legend verticalAlign="top" height={36} />
                                    <Line
                                        type="monotone"
                                        dataKey="max_fitness"
                                        name="Max Fitness"
                                        stroke="#6366f1"
                                        strokeWidth={2}
                                        dot={{ r: 3 }}
                                        activeDot={{ r: 5 }}
                                    />
                                    <Line
                                        type="monotone"
                                        dataKey="avg_fitness"
                                        name="Avg Fitness"
                                        stroke="#10b981"
                                        strokeWidth={2}
                                        dot={{ r: 3 }}
                                    />
                                </LineChart>
                            </ResponsiveContainer>
                        </div>
                    ) : (
                        <div className="h-[250px] flex items-center justify-center text-gray-400 bg-gray-50 rounded-xl">
                            <span className="flex items-center gap-2"><BarChart2 className="w-5 h-5" /> 暂无进化数据</span>
                        </div>
                    )}
                </div>

                {/* 右侧：实时日志 */}
                <div className="bg-gray-900 rounded-2xl p-4 shadow-sm flex flex-col h-[300px]">
                    <div className="flex items-center gap-2 mb-2 text-gray-100 border-b border-gray-700 pb-2">
                        <Activity className="w-4 h-4 text-green-400" />
                        <span className="font-mono text-sm font-bold">System Logs</span>
                    </div>
                    <div
                        ref={logContainerRef}
                        className="flex-1 overflow-y-auto font-mono text-xs space-y-1 pr-2"
                    >
                        {logs.length === 0 && <span className="text-gray-600">Waiting for tasks...</span>}
                        {logs.map((log, idx) => (
                            <div key={idx} className="flex gap-2">
                                <span className="text-gray-500">[{log.timestamp}]</span>
                                <span className={
                                    log.type === 'error' ? 'text-red-400' :
                                        log.type === 'success' ? 'text-green-400' :
                                            log.type === 'warning' ? 'text-yellow-400' :
                                                'text-gray-300'
                                }>{log.message}</span>
                            </div>
                        ))}
                    </div>
                </div>
            </div>

            {/* 最优结果展示 */}
            {bestResult && (
                <div className="bg-green-50 border border-green-200 rounded-2xl p-6 relative overflow-hidden">
                    <div className="absolute top-0 right-0 p-4 opacity-10">
                        <TrendingUp className="w-32 h-32 text-green-800" />
                    </div>
                    <h3 className="text-lg font-bold text-green-900 mb-4 flex items-center gap-2">
                        ✅ 优化完成 - 全局最优解
                    </h3>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-6 relative z-10">
                        <div>
                            <h4 className="text-sm text-green-700 font-semibold mb-2">最优参数组合</h4>
                            <div className="bg-white/60 rounded-xl p-3 space-y-2">
                                {Object.entries(bestResult.best_params).map(([key, value]) => (
                                    <div key={key} className="flex justify-between items-center text-sm">
                                        <span className="text-gray-600">{key}:</span>
                                        <span className="font-mono font-bold text-green-800">{String(value)}</span>
                                    </div>
                                ))}
                            </div>
                        </div>
                        <div>
                            <h4 className="text-sm text-green-700 font-semibold mb-2">最佳适应度 ({config?.metric})</h4>
                            <div className="flex items-baseline gap-2">
                                <span className="text-3xl font-bold text-green-700">{Number(bestResult.best_fitness).toFixed(4)}</span>
                                <span className="text-sm text-green-600">Generation {bestResult.history?.length}</span>
                            </div>
                            <p className="text-xs text-green-600 mt-2">
                                耗时: {bestResult.execution_time?.toFixed(1)} 秒
                            </p>
                        </div>
                    </div>
                    <div className="mt-5 flex items-center justify-between rounded-2xl border border-green-300 bg-white/70 p-4 relative z-10">
                        <div className="text-sm text-green-900">
                            <div className="font-semibold">一键回填参数到快速回测</div>
                            <div className="mt-1">将最优参数写入回测中心配置，并切换到快速回测模块。</div>
                        </div>
                        <button
                            type="button"
                            onClick={handleApplyBestParams}
                            className="px-4 py-2 bg-green-600 text-white rounded-xl hover:bg-green-700 transition-colors"
                        >
                            一键回填
                        </button>
                    </div>
                </div>
            )}
            {applySuccess && (
                <div className="bg-green-50 border border-green-200 rounded-2xl p-4 text-sm text-green-800">
                    最优参数已回填到“快速回测”配置，可直接开始回测。
                </div>
            )}
        </div>
    );
};
