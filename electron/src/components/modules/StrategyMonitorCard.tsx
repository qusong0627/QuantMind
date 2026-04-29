import React, { useState, useMemo } from 'react';
import { useDispatch } from 'react-redux';
import { TrendingUp, TrendingDown, Activity, AlertTriangle, CheckCircle, Clock, ArrowRight, RefreshCw, WifiOff, X, Wifi } from 'lucide-react';
import { motion } from 'framer-motion';
import { setCurrentTab } from '../../store/slices/aiStrategySlice';
import { useStrategies } from '../../hooks/useStrategies';
import { StrategyMonitorSkeleton } from '../common/CardSkeletons';
import { formatBackendTime } from '../../utils/format';

interface StrategyMonitorCardProps {
  expanded?: boolean;
  onExpand?: () => void;
  onCloseExpand?: () => void;
}

const STATUS_CONFIG = {
  running: { color: 'text-[var(--success)]', bg: 'bg-[var(--success-bg)]', Icon: CheckCircle, text: '运行中' },
  starting: { color: 'text-[var(--primary-blue)]', bg: 'bg-[var(--primary-blue-bg)]', Icon: Clock, text: '启动中' },
  paused: { color: 'text-[var(--neutral)]', bg: 'bg-[var(--neutral-bg)]', Icon: Activity, text: '已暂停' },
  error: { color: 'text-[var(--error)]', bg: 'bg-[var(--error-bg)]', Icon: AlertTriangle, text: '异常' },
  stopped: { color: 'text-[var(--neutral)]', bg: 'bg-[var(--neutral-bg)]', Icon: Activity, text: '已停止' },
} as const;

const RISK_CONFIG = {
  low: { color: 'text-[var(--success)]', text: '低风险' },
  medium: { color: 'text-[var(--warning)]', text: '中风险' },
  high: { color: 'text-[var(--error)]', text: '高风险' },
} as const;

export const StrategyMonitorCard: React.FC<StrategyMonitorCardProps> = ({
  expanded = false,
  onExpand,
  onCloseExpand,
}) => {
  const [isHovered, setIsHovered] = useState(false);
  const dispatch = useDispatch();

  const {
    strategies,
    stats,
    loading,
    error,
    isSimulated,
    isStale,
    lastUpdatedAt,
    realtimeStatus,
    refresh,
  } = useStrategies({ autoRefresh: true, refreshInterval: 10000, enableRealtime: true });

  const formatAmount = (value: number) => {
    const formatter = new Intl.NumberFormat('zh-CN', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    const sign = value > 0 ? '+' : '';
    return `${sign}${formatter.format(value)}`;
  };

  const handleGoToStrategy = () => {
    dispatch(setCurrentTab('strategy'));
  };

  const handleGoToStrategyDetail = (strategyId: string) => {
    try {
      sessionStorage.setItem('dashboard_strategy_focus_id', strategyId);
    } catch {
      // ignore storage errors
    }
    dispatch(setCurrentTab('strategy'));
  };

  const getStatusConfig = (status: string) => STATUS_CONFIG[status as keyof typeof STATUS_CONFIG] || STATUS_CONFIG.stopped;
  const getRiskConfig = (risk: string) => RISK_CONFIG[risk as keyof typeof RISK_CONFIG] || { color: 'text-[var(--neutral)]', text: '未知' };

  const formatTimestamp = (timeStr: string | null) => {
    return formatBackendTime(timeStr, { withSeconds: true });
  };

  const priorityWeight: Record<string, number> = {
    error: 0,
    starting: 1,
    running: 2,
    stopped: 3,
    paused: 3,
  };

  const prioritizedStrategies = useMemo(() => {
    return [...strategies].sort((a, b) => {
      const pa = priorityWeight[a.status] ?? 9;
      const pb = priorityWeight[b.status] ?? 9;
      if (pa !== pb) return pa - pb;

      const ta = new Date(a.updated_at).getTime();
      const tb = new Date(b.updated_at).getTime();
      if (Number.isNaN(ta) && Number.isNaN(tb)) return 0;
      if (Number.isNaN(ta)) return 1;
      if (Number.isNaN(tb)) return -1;
      return tb - ta;
    });
  }, [strategies]);

  const getExceptionText = (strategy: { status: string; description?: string; error_message?: string; error_code?: string }) => {
    if (strategy.status !== 'error') return null;
    if (strategy.error_message && strategy.error_message.trim()) {
      return strategy.error_message.trim().slice(0, 18);
    }
    if (strategy.error_code && strategy.error_code.trim()) {
      return `代码:${strategy.error_code.trim().slice(0, 10)}`;
    }
    if (strategy.description && strategy.description.trim()) {
      return strategy.description.trim().slice(0, 18);
    }
    return '待检查';
  };

  const visibleStrategies = useMemo(() => {
    const filtered = expanded ? prioritizedStrategies : prioritizedStrategies.filter(s => s.status === 'running');
    return filtered.slice(0, expanded ? 14 : 3);
  }, [expanded, prioritizedStrategies]);

  if (loading && strategies.length === 0) {
    return <StrategyMonitorSkeleton />;
  }

  return (
    <motion.div
      className={`panel-card bg-white border border-slate-200 shadow-sm relative ${expanded ? 'panel-card-expanded bg-white' : ''}`}
      onHoverStart={() => setIsHovered(true)}
      onHoverEnd={() => setIsHovered(false)}
      whileHover={expanded ? undefined : { scale: 1.02 }}
      transition={{ duration: 0.2 }}
      style={expanded ? { height: '100%', overflow: 'hidden' } : undefined}
    >
      <style>{`.strategy-modal-surface { border-radius: 12px; }`}</style>

      <div className="relative mb-3.5 z-10">
        <div className="text-center px-8">
          <h3 className="text-base font-black text-slate-800 inline-flex items-center gap-2">
            策略监控
          </h3>
          <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mt-0.5">
            {realtimeStatus === 'connected' ? 'REAL-TIME UPDATES ENABLED' : 'POLLING MODE ACTIVE'}
          </p>
        </div>
        
        <div className="absolute top-[-4px] right-[-4px] flex gap-1">
          {expanded && (
            <motion.button
              onClick={onCloseExpand}
              className="p-2 hover:bg-white/60 rounded-xl transition-colors shadow-sm bg-white/30 border border-white/60"
              whileHover={{ scale: 1.06 }}
              whileTap={{ scale: 0.95 }}
              title="关闭"
            >
              <X className="w-4 h-4 text-slate-500" />
            </motion.button>
          )}
          <motion.button
            onClick={refresh}
            className="p-2 hover:bg-white/60 rounded-xl transition-colors shadow-sm bg-white/30 border border-white/60"
            whileHover={{ scale: 1.1, rotate: 180 }}
            whileTap={{ scale: 0.95 }}
            title="刷新策略数据"
          >
            <RefreshCw className="w-4 h-4 text-slate-500" />
          </motion.button>
        </div>
      </div>

      <div className="grid grid-cols-4 gap-1.5 mb-2.5 relative z-10 strategy-modal-surface">
        <div className="bg-slate-50 rounded-xl p-1 border border-slate-100/80 text-center">
          <div className="w-5.5 h-5.5 bg-indigo-50 rounded-lg flex items-center justify-center mx-auto mb-1">
            <Activity className="w-3 h-3 text-indigo-500" />
          </div>
          <p className="text-[9px] text-slate-400 font-bold uppercase leading-tight">总策略</p>
          <p className="text-xs font-black text-slate-800">{stats.totalStrategies}</p>
        </div>
        <div className="bg-slate-50 rounded-xl p-1 border border-slate-100/80 text-center">
          <div className="w-5.5 h-5.5 bg-emerald-50 rounded-lg flex items-center justify-center mx-auto mb-1">
            <CheckCircle className="w-3 h-3 text-emerald-500" />
          </div>
          <p className="text-[9px] text-slate-400 font-bold uppercase leading-tight">运行中</p>
          <p className="text-xs font-black text-emerald-600">{stats.activeStrategies}</p>
        </div>
        <div className="bg-slate-50 rounded-xl p-1 border border-slate-100/80 text-center">
          <div className="w-5.5 h-5.5 bg-slate-200/50 rounded-lg flex items-center justify-center mx-auto mb-1">
            <Clock className="w-3 h-3 text-slate-400" />
          </div>
          <p className="text-[9px] text-slate-400 font-bold uppercase leading-tight">已停止</p>
          <p className="text-xs font-black text-slate-500">{stats.stoppedStrategies}</p>
        </div>
        <div className="bg-slate-50 rounded-xl p-1 border border-slate-100/80 text-center">
          <div className="w-5.5 h-5.5 bg-rose-50 rounded-lg flex items-center justify-center mx-auto mb-1">
            <AlertTriangle className="w-3 h-3 text-rose-500" />
          </div>
          <p className="text-[9px] text-slate-400 font-bold uppercase leading-tight">异常</p>
          <p className="text-xs font-black text-rose-600">{stats.errorStrategies}</p>
        </div>
      </div>

      <div className="bg-slate-50 border border-slate-100 rounded-xl p-2.5 mb-2.5 relative z-10 strategy-modal-surface shadow-sm text-center">
        <div className="grid grid-cols-3 gap-2 items-center">
          <div>
            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider leading-tight">总收益率</p>
            <p className={`text-xl font-black leading-tight ${stats.totalReturn >= 0 ? 'text-[var(--profit-primary)]' : 'text-[var(--loss-primary)]'}`}>
              {stats.totalReturn >= 0 ? '+' : ''}{stats.totalReturn}%
            </p>
          </div>
          <div>
            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider leading-tight">今日收益</p>
            <p className={`text-xl font-black leading-tight ${stats.todayReturn >= 0 ? 'text-[var(--profit-primary)]' : 'text-[var(--loss-primary)]'}`}>
              {stats.todayReturn >= 0 ? '+' : ''}{stats.todayReturn}%
            </p>
          </div>
          <div>
            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider leading-tight">今日盈亏</p>
            <p className={`text-xl font-black leading-tight ${stats.todayPnL >= 0 ? 'text-[var(--profit-primary)]' : 'text-[var(--loss-primary)]'}`}>
              ¥{formatAmount(stats.todayPnL).replace(/^\+/, '')}
            </p>
          </div>
        </div>
      </div>

      <div className="flex-1 min-h-0 relative z-10 flex flex-col bg-slate-50/50 border border-slate-100 rounded-xl p-2 strategy-modal-surface">
        <h4 className="text-[10px] font-black text-slate-400 uppercase tracking-widest mb-2 px-2 text-center">
          {expanded ? 'STRATEGY DETAILS (Full List)' : 'RUNNING STRATEGIES'}
        </h4>
        <div className={`flex-1 overflow-y-auto space-y-2 custom-scrollbar pr-1 ${expanded ? 'max-h-[50vh]' : ''}`}>
          {visibleStrategies.length > 0 ? (
            visibleStrategies.map((strategy) => {
              const { color, bg, Icon, text } = getStatusConfig(strategy.status);
              const riskConfig = getRiskConfig(strategy.risk_level);

              return (
                <motion.div
                  key={strategy.id}
                  onClick={() => handleGoToStrategyDetail(strategy.id)}
                  className="flex items-center justify-between py-1.5 px-2 rounded-xl transition-colors bg-white border border-slate-100 shadow-sm cursor-pointer"
                  whileHover={{ backgroundColor: '#f1f5f9', borderColor: '#e2e8f0' }}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                >
                  <div className="flex items-center space-x-2 flex-1 min-w-0">
                    <div className={`w-6 h-6 rounded-lg flex items-center justify-center flex-shrink-0 ${bg}`}>
                      <Icon className={`w-3.5 h-3.5 ${color}`} />
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-bold text-slate-800 truncate leading-snug">{strategy.name}</p>
                      <div className="flex items-center space-x-1.5 mt-1">
                        <span className={`text-[9px] px-1.5 py-0.5 rounded-md font-bold uppercase leading-none ${bg} ${color}`}>
                          {text}
                        </span>
                        <span className={`text-[9px] font-bold uppercase leading-none ${riskConfig.color}`}>
                          {riskConfig.text}
                        </span>
                      </div>
                    </div>
                  </div>
                  <div className="text-right flex-shrink-0 ml-2">
                    <p className={`text-sm font-black leading-tight ${strategy.total_return >= 0 ? 'text-[var(--profit-primary)]' : 'text-[var(--loss-primary)]'}`}>
                      {strategy.total_return >= 0 ? '+' : ''}{strategy.total_return}%
                    </p>
                    <p className="text-[10px] font-bold text-slate-400 leading-tight mt-0.5">
                      {strategy.today_return >= 0 ? '+' : ''}{strategy.today_return}%
                    </p>
                  </div>
                </motion.div>
              );
            })
          ) : (
            <div className="flex flex-col items-center justify-center py-8 text-center">
              <Activity className="w-8 h-8 text-slate-200 mb-2" />
              <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">
                {expanded ? 'No Strategies' : 'No Running Strategies'}
              </p>
            </div>
          )}
        </div>
      </div>

      {!expanded && (
        <motion.button
          onClick={onExpand || handleGoToStrategy}
          className="w-full mt-2.5 py-2 px-4 bg-slate-800 text-white rounded-xl font-bold text-sm hover:bg-slate-900 transition-colors relative z-10 shadow-lg shadow-slate-200"
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          查看所有策略
        </motion.button>
      )}
    </motion.div>
  );
};
