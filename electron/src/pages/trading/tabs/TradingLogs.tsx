import React, { useEffect, useState, useRef } from 'react';
import { Terminal, RefreshCw, Filter, AlertCircle } from 'lucide-react';
// 按需加载 realTradingService

interface TradingLogsProps {
    tenantId: string;
    userId: string;
    isActive: boolean;
}

const TradingLogs: React.FC<TradingLogsProps> = ({ tenantId, userId, isActive }) => {
    const [logs, setLogs] = useState<string>('');
    const [loading, setLoading] = useState(false);
    const [autoRefresh, setAutoRefresh] = useState(true);
    const logEndRef = useRef<HTMLDivElement>(null);

    const fetchLogs = async () => {
        setLoading(true);
        try {
            const { realTradingService } = await import('../../../services/realTradingService');
            const data = await realTradingService.getLogs(userId, 200, tenantId);
            setLogs(data.logs);
        } catch (e) {
            // ignore
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        if (isActive && autoRefresh) {
            fetchLogs();
            const interval = setInterval(fetchLogs, 3000);
            return () => clearInterval(interval);
        }
    }, [isActive, userId, autoRefresh]);

    useEffect(() => {
        if (logEndRef.current) {
            logEndRef.current.scrollIntoView({ behavior: 'smooth' });
        }
    }, [logs]);

    if (!isActive) return null;

    return (
        <div className="h-full flex flex-col p-6 space-y-4">
            {/* Toolbar */}
            <div className="flex justify-between items-center bg-gray-50 p-3 rounded-lg border border-gray-100 shadow-sm">
                <h3 className="text-sm font-bold text-gray-700 flex items-center">
                    <Terminal className="mr-2 text-gray-500" size={18} />
                    Pod 实时控制台
                </h3>
                <div className="flex space-x-2">
                    <button
                        onClick={() => setAutoRefresh(!autoRefresh)}
                        className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-md text-xs font-medium border transition-all ${autoRefresh
                                ? 'bg-blue-50 border-blue-200 text-blue-600 shadow-sm'
                                : 'bg-white border-gray-200 text-gray-500 hover:bg-gray-50'
                            }`}
                    >
                        <RefreshCw size={12} className={autoRefresh ? "animate-spin" : ""} />
                        <span>{autoRefresh ? 'Auto Refresh: ON' : 'Paused'}</span>
                    </button>
                    <button className="flex items-center space-x-1.5 px-3 py-1.5 rounded-md text-xs font-medium bg-white border border-gray-200 text-gray-600 hover:bg-gray-50 hover:text-gray-800 transition-colors shadow-sm">
                        <Filter size={12} />
                        <span>Filter</span>
                    </button>
                </div>
            </div>

            {/* Log Console */}
            <div className="flex-1 bg-gray-900 rounded-xl border border-gray-800 p-4 font-mono text-xs overflow-y-auto shadow-inner relative">
                <div className="absolute top-2 right-4 text-gray-600 text-[10px] uppercase font-bold tracking-wider pointer-events-none">
                    Console Output
                </div>
                {logs ? (
                    <pre className="whitespace-pre-wrap text-gray-300 leading-relaxed font-menu">
                        {logs}
                    </pre>
                ) : (
                    <div className="h-full flex flex-col items-center justify-center text-gray-600">
                        <AlertCircle size={32} className="mb-3 opacity-30" />
                        <p>暂无日志数据</p>
                        <p className="text-[10px] text-gray-700 mt-1">请确认策略是否已启动</p>
                    </div>
                )}
                <div ref={logEndRef} />
            </div>
        </div>
    );
};

export default TradingLogs;
