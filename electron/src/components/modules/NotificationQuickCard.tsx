import React, { useMemo, useState } from 'react';
import { useDispatch } from 'react-redux';
import { Bell, AlertCircle, TrendingUp, Settings, ArrowRight, RefreshCw, WifiOff, X, CheckCheck, Trash2 } from 'lucide-react';
import { motion } from 'framer-motion';
import { setCurrentTab } from '../../store/slices/aiStrategySlice';
import { useNotifications, resolveNotificationTarget, getNavigationHint } from '../../hooks/useNotifications';
import { getRelativeTime } from '../../utils/format';
import { NotificationSkeleton } from '../common/CardSkeletons';
import { useNavigate } from 'react-router-dom';
import { useBacktestCenterStore } from '../../stores/backtestCenterStore';
import type { BusinessNotification, NotificationRouteTarget } from '../../types/notification';

interface NotificationQuickCardProps {
  expanded?: boolean;
  onExpand?: () => void;
  onCloseExpand?: () => void;
}

const TYPE_CONFIG = {
  system: { color: 'text-[var(--notification-system)]', bg: 'bg-[var(--info-bg)]', Icon: Settings },
  trading: { color: 'text-[var(--notification-trading)]', bg: 'bg-[var(--error-bg)]', Icon: TrendingUp },
  market: { color: 'text-[var(--notification-market)]', bg: 'bg-[var(--warning-bg)]', Icon: AlertCircle },
  strategy: { color: 'text-[var(--notification-strategy)]', bg: 'bg-[var(--success-bg)]', Icon: Bell },
} as const;

export const NotificationQuickCard: React.FC<NotificationQuickCardProps> = ({
  expanded = false,
  onExpand,
  onCloseExpand,
}) => {
  const [isHovered, setIsHovered] = useState(false);
  const dispatch = useDispatch();
  const navigate = useNavigate();
  const setBacktestModule = useBacktestCenterStore((state) => state.setActiveModule);

  const {
    notifications,
    total,
    unreadCount,
    typeCounts,
    hasMore,
    loading,
    loadingMore,
    error,
    degraded,
    realtimeStatus,
    refresh,
    loadMore,
    clearNotifications,
    markAsRead,
    markAllAsRead,
  } = useNotifications({ limit: expanded ? 20 : 10, days: 7, autoRefresh: true });

  const handleNavigation = (target: NotificationRouteTarget) => {
    if (target === 'backtest-history') {
      setBacktestModule('backtest-history');
      dispatch(setCurrentTab('backtest'));
      return;
    }
    
    const internalTargets = ['dashboard', 'strategy', 'trading', 'community', 'profile', 'ai-ide'] as const;
    if (internalTargets.includes(target as typeof internalTargets[number])) {
      dispatch(setCurrentTab(target as typeof internalTargets[number]));
      return;
    }
    
    if (typeof target === 'object' && 'external' in target) {
      window.open(target.external, '_blank', 'noopener,noreferrer');
      return;
    }
    
    if (typeof target === 'object' && 'route' in target) {
      navigate(target.route);
    }
  };

  const openAction = async (notification: BusinessNotification) => {
    const target = resolveNotificationTarget(notification);
    
    if (!notification.is_read) {
      await markAsRead(notification.id);
    }
    
    if (target) {
      handleNavigation(target);
    }
  };

  const stats = useMemo(() => ({
    total,
    unread: unreadCount,
    system: typeCounts['system'] || 0,
    trading: typeCounts['trading'] || 0,
    market: typeCounts['market'] || 0,
    strategy: typeCounts['strategy'] || 0,
  }), [total, unreadCount, typeCounts]);

  const visibleNotifications = useMemo(
    () => expanded ? notifications : notifications.slice(0, 3),
    [expanded, notifications]
  );

  const handleListScroll = (event: React.UIEvent<HTMLDivElement>) => {
    if (!expanded || loadingMore || !hasMore) return;
    const el = event.currentTarget;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 24) {
      void loadMore();
    }
  };

  const getTypeConfig = (type: string) => TYPE_CONFIG[type as keyof typeof TYPE_CONFIG] || TYPE_CONFIG.system;

  if (loading && notifications.length === 0) {
    return <NotificationSkeleton />;
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
      <style>{`.notification-modal-surface { border-radius: 12px; }`}</style>

      <div className="relative mb-3.5 z-10">
        <div className="text-center px-8">
          <h3 className="text-base font-black text-slate-800 inline-flex items-center gap-2">
            信息通知
          </h3>
          <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mt-0.5">
            {realtimeStatus === 'connected' ? 'REAL-TIME UPDATES ENABLED' : 'POLLING MODE ACTIVE'}
          </p>
        </div>
        
        <div className="absolute top-[-4px] right-[-4px] flex gap-1">
          {expanded && (
            <>
              <motion.button
                onClick={() => void markAllAsRead()}
                className="p-2 hover:bg-white/60 rounded-xl transition-colors shadow-sm bg-white/30 border border-white/60"
                whileHover={{ scale: 1.06 }}
                whileTap={{ scale: 0.95 }}
                title="全部标记已读"
              >
                <CheckCheck className="w-4 h-4 text-slate-500" />
              </motion.button>
              <motion.button
                onClick={() => {
                  if (window.confirm('确定要清除最近7天的所有通知吗？')) {
                    void clearNotifications();
                  }
                }}
                className="p-2 hover:bg-white/60 rounded-xl transition-colors shadow-sm bg-white/30 border border-white/60"
                whileHover={{ scale: 1.06 }}
                whileTap={{ scale: 0.95 }}
                title="一键清除通知"
              >
                <Trash2 className="w-4 h-4 text-slate-500" />
              </motion.button>
              <motion.button
                onClick={onCloseExpand}
                className="p-2 hover:bg-white/60 rounded-xl transition-colors shadow-sm bg-white/30 border border-white/60"
                whileHover={{ scale: 1.06 }}
                whileTap={{ scale: 0.95 }}
                title="关闭"
              >
                <X className="w-4 h-4 text-slate-500" />
              </motion.button>
            </>
          )}
          <motion.button
            onClick={refresh}
            className="p-2 hover:bg-white/60 rounded-xl transition-colors shadow-sm bg-white/30 border border-white/60"
            whileHover={{ scale: 1.1, rotate: 180 }}
            whileTap={{ scale: 0.95 }}
            title="刷新通知"
          >
            <RefreshCw className="w-4 h-4 text-slate-500" />
          </motion.button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-1.5 mb-2.5 relative z-10">
        <div className="bg-amber-50 rounded-xl p-1.5 border border-amber-100">
          <div className="flex items-center justify-center space-x-1.5">
            <Bell className="w-3.5 h-3.5 text-amber-600" />
            <span className="text-xs font-medium text-amber-900">近7天</span>
            <span className="text-lg font-bold text-amber-900">{stats.total}</span>
          </div>
        </div>
        <div className="bg-rose-50 rounded-xl p-1.5 border border-rose-100">
          <div className="flex items-center justify-center space-x-1.5">
            <AlertCircle className="w-3.5 h-3.5 text-rose-600" />
            <span className="text-xs font-medium text-rose-900">未读</span>
            <span className="text-lg font-bold text-rose-900">{stats.unread}</span>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-4 gap-1 mb-2.5 relative z-10 bg-slate-50 border border-slate-100/80 rounded-xl p-1.5">
        <div className="text-center">
          <div className="w-6 h-6 bg-blue-50 rounded-full flex items-center justify-center mx-auto mb-1">
            <Settings className="w-3 h-3 text-blue-500" />
          </div>
          <p className="text-[10px] font-bold text-slate-400 uppercase">系统</p>
          <p className="text-xs font-bold text-slate-800">{stats.system}</p>
        </div>
        <div className="text-center">
          <div className="w-6 h-6 bg-rose-50 rounded-full flex items-center justify-center mx-auto mb-1">
            <TrendingUp className="w-3 h-3 text-rose-500" />
          </div>
          <p className="text-[10px] font-bold text-slate-400 uppercase">交易</p>
          <p className="text-xs font-bold text-slate-800">{stats.trading}</p>
        </div>
        <div className="text-center">
          <div className="w-6 h-6 bg-amber-50 rounded-full flex items-center justify-center mx-auto mb-1">
            <AlertCircle className="w-3 h-3 text-amber-500" />
          </div>
          <p className="text-[10px] font-bold text-slate-400 uppercase">市场</p>
          <p className="text-xs font-bold text-slate-800">{stats.market}</p>
        </div>
        <div className="text-center">
          <div className="w-6 h-6 bg-emerald-50 rounded-full flex items-center justify-center mx-auto mb-1">
            <Bell className="w-3 h-3 text-emerald-500" />
          </div>
          <p className="text-[10px] font-bold text-slate-400 uppercase">策略</p>
          <p className="text-xs font-bold text-slate-800">{stats.strategy}</p>
        </div>
      </div>

      <div className="flex-1 min-h-0 relative z-10 flex flex-col bg-slate-50/50 border border-slate-100 rounded-xl p-2">
        <h4 className="text-[10px] font-black text-slate-400 uppercase tracking-widest mb-2 px-2 text-center">
          {expanded ? 'NOTIFICATION LIST' : 'RECENT NOTIFICATIONS'}
        </h4>
        {(degraded || error) && (
          <div className="mb-2 text-xs text-[var(--warning-dark)] bg-[var(--warning-bg)] rounded-lg px-2 py-1.5">
            通知服务当前处于降级模式，已保留现有数据并继续刷新。
          </div>
        )}
        <div className="flex-1 overflow-y-auto space-y-1.5 custom-scrollbar pr-1" onScroll={handleListScroll}>
          {visibleNotifications.length > 0 ? (
            visibleNotifications.map((notification) => {
              const { color, bg, Icon } = getTypeConfig(notification.type);
              const relativeTime = getRelativeTime(new Date(notification.created_at).getTime());
              const target = resolveNotificationTarget(notification);
              const navigationHint = getNavigationHint(target);

              return (
                <motion.div
                  key={notification.id}
                  onClick={() => openAction(notification)}
                  className={`flex items-center justify-between py-2 px-3 rounded-xl transition-colors cursor-pointer bg-white border border-slate-100 shadow-sm ${!notification.is_read ? 'border-l-4 border-l-blue-500' : ''}`}
                  title={navigationHint}
                  whileHover={{ backgroundColor: '#f1f5f9', borderColor: '#e2e8f0' }}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                >
                  <div className="flex items-center space-x-2.5 flex-1">
                    <div className={`w-5 h-5 rounded-full flex items-center justify-center ${bg}`}>
                      <Icon className={`w-2.5 h-2.5 ${color}`} />
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className={`text-sm truncate ${!notification.is_read ? 'font-semibold text-[var(--text-primary)]' : 'font-medium text-[var(--text-secondary)]'}`}>
                        {notification.title}
                      </p>
                      <p className="text-xs text-[var(--text-tertiary)] flex justify-between">
                        <span>{relativeTime}</span>
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 ml-2">
                    {target && (
                      <ArrowRight className="w-3.5 h-3.5 text-[var(--warning)] flex-shrink-0" />
                    )}
                    {!notification.is_read && (
                      <div className="w-2 h-2 bg-[var(--info)] rounded-full flex-shrink-0" />
                    )}
                  </div>
                </motion.div>
              );
            })
          ) : (
            <div className="text-center py-4 text-xs text-[var(--text-tertiary)]">
              暂无通知
            </div>
          )}
          {expanded && hasMore && (
            <div className="pt-1 text-center">
              <button
                onClick={() => void loadMore()}
                disabled={loadingMore}
                className="text-xs px-2 py-1 rounded border border-[var(--border-primary)] text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] disabled:opacity-60"
              >
                {loadingMore ? '加载中...' : '加载更多'}
              </button>
            </div>
          )}
        </div>
      </div>

      {!expanded && (
        <motion.button
          onClick={onExpand}
          className="w-full mt-2.5 py-2 px-4 bg-amber-500 text-white rounded-xl font-bold text-sm hover:bg-amber-600 transition-colors relative z-10 shadow-lg shadow-amber-100"
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          查看所有通知
        </motion.button>
      )}
    </motion.div>
  );
};
