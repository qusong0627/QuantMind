/**
 * 活动历史页面
 * Activities History Page
 *
 * 展示用户的所有活动记录
 *
 * @author QuantMind Team
 * @date 2025-12-02
 */

import React, { useState, useEffect } from 'react';
import { Timeline, Empty, Spin, Select, Tag, Space } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { Filter, Calendar, Activity as ActivityIcon, Clock, RefreshCw } from 'lucide-react';
import {
  Activity,
  ActivityType,
  ActivitySource,
  ActivityGroup,
  ActivityStats,
  groupActivitiesByDate,
  getActivityIcon,
  getActivityColor,
  getActivityText,
  formatActivityTime,
} from '../../../shared/types/activity';
import { userCenterService } from '../services/userCenterService';

const { Option } = Select;

interface ActivitiesPageProps {
  userId: string;
}

/**
 * 活动历史页面组件
 */
export const ActivitiesPage: React.FC<ActivitiesPageProps> = ({ userId }) => {
  const [activities, setActivities] = useState<Activity[]>([]);
  const [groupedActivities, setGroupedActivities] = useState<ActivityGroup[]>([]);
  const [stats, setStats] = useState<ActivityStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [filterType, setFilterType] = useState<ActivityType | 'all'>('all');
  const [filterSource, setFilterSource] = useState<ActivitySource | 'all'>('all');

  // 加载活动数据
  useEffect(() => {
    loadActivities();
  }, [userId]);

  // 过滤和分组
  useEffect(() => {
    let filtered = activities;

    // 按类型过滤
    if (filterType !== 'all') {
      filtered = filtered.filter(act => act.type === filterType);
    }

    // 按来源过滤
    if (filterSource !== 'all') {
      filtered = filtered.filter(act => act.source === filterSource);
    }

    // 按日期分组
    const grouped = groupActivitiesByDate(filtered);
    setGroupedActivities(grouped);
  }, [activities, filterType, filterSource]);

  const loadActivities = async () => {
    setLoading(true);
    try {
      const userActivities = await loadUserCenterActivities(userId);
      setActivities(userActivities);
      calculateStats(userActivities);
    } catch (error) {
      console.error('加载活动失败:', error);
    } finally {
      setLoading(false);
    }
  };

  // 加载个人中心活动（模拟数据）
  const loadUserCenterActivities = async (userId: string): Promise<Activity[]> => {
    try {
      // TODO: 调用真实API
      // const response = await userCenterService.getActivities(userId);
      // return response.data;

      // 模拟数据
      return [
        {
          id: '1',
          user_id: userId,
          type: ActivityType.STRATEGY_CREATE,
          source: ActivitySource.USER_CENTER,
          title: '创建了新策略',
          description: '双均线交叉策略',
          created_at: new Date().toISOString(),
          metadata: {
            strategy_id: 'strategy_001',
            strategy_name: '双均线交叉策略',
            strategy_type: 'CTA',
          },
        },
        {
          id: '2',
          user_id: userId,
          type: ActivityType.STRATEGY_BACKTEST,
          source: ActivitySource.BACKTEST,
          title: '回测了策略',
          description: '回测结果：年化收益15.6%',
          created_at: new Date(Date.now() - 3600000).toISOString(),
          metadata: {
            strategy_id: 'strategy_001',
            strategy_name: '双均线交叉策略',
            performance: {
              return_pct: 15.6,
              sharpe_ratio: 1.38,
            },
          },
        },
      ];
    } catch (error) {
      console.error('加载个人中心活动失败:', error);
      return [];
    }
  };

  // 计算统计数据
  const calculateStats = (activities: Activity[]) => {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const weekAgo = new Date(today.getTime() - 7 * 86400000);
    const monthAgo = new Date(today.getTime() - 30 * 86400000);

    const stats: ActivityStats = {
      total_count: activities.length,
      today_count: activities.filter(a => new Date(a.created_at) >= today).length,
      week_count: activities.filter(a => new Date(a.created_at) >= weekAgo).length,
      month_count: activities.filter(a => new Date(a.created_at) >= monthAgo).length,
      by_type: {} as Record<ActivityType, number>,
      by_source: {} as Record<ActivitySource, number>,
    };

    // 按类型统计
    activities.forEach(activity => {
      stats.by_type[activity.type] = (stats.by_type[activity.type] || 0) + 1;
      stats.by_source[activity.source] = (stats.by_source[activity.source] || 0) + 1;
    });

    setStats(stats);
  };

  // 渲染活动项
  const renderActivityItem = (activity: Activity) => {
    interface ActivityMetadata {
      strategy_name?: string;
      strategy_id?: string;
      portfolio_name?: string;
      post_title?: string;
      post_id?: number;
      performance?: {
        return_pct?: number;
        sharpe_ratio?: number;
      };
      [key: string]: any;
    }
    const icon = getActivityIcon(activity.type);
    const color = getActivityColor(activity.type);
    const text = getActivityText(activity.type);

    return (
      <div className="bg-white rounded-xl border border-gray-100 p-4 mb-3 transition-all hover:border-blue-100 hover:shadow-sm">
        <div className="flex items-start gap-4">
          <div className="w-10 h-10 rounded-xl bg-slate-50 flex items-center justify-center text-xl shadow-inner border border-white">
            {icon}
          </div>

          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1 flex-wrap">
              <span className="text-sm font-black uppercase tracking-tight" style={{ color }}>{text}</span>
              {activity.description && (
                <span className="text-sm text-slate-500 font-medium">
                  {activity.description}
                </span>
              )}
            </div>

            {/* 元数据 */}
            {activity.metadata && (() => {
              const meta = activity.metadata as ActivityMetadata;
              return (
                <div className="flex gap-2 flex-wrap mt-2">
                  {meta.strategy_name && (
                    <span className="px-2 py-0.5 bg-blue-50 text-blue-600 rounded-lg text-[10px] font-bold border border-blue-100">
                      {meta.strategy_name}
                    </span>
                  )}
                  {meta.portfolio_name && (
                    <span className="px-2 py-0.5 bg-emerald-50 text-emerald-600 rounded-lg text-[10px] font-bold border border-emerald-100">
                      {meta.portfolio_name}
                    </span>
                  )}
                  {meta.post_title && (
                    <span className="px-2 py-0.5 bg-purple-50 text-purple-600 rounded-lg text-[10px] font-bold border border-purple-100">
                      {meta.post_title}
                    </span>
                  )}
                  {meta.performance && (
                    <span className="px-2 py-0.5 bg-amber-50 text-amber-600 rounded-lg text-[10px] font-bold border border-amber-100">
                      收益 {meta.performance.return_pct?.toFixed(2)}%
                    </span>
                  )}
                </div>
              );
            })()}

            {/* 时间和来源 */}
            <div className="flex items-center justify-between mt-4">
              <div className="flex items-center gap-3 text-[10px] font-bold text-slate-300 uppercase tracking-widest">
                <span className="flex items-center gap-1.5"><Clock className="w-3 h-3" /> {formatActivityTime(activity.created_at)}</span>
              </div>
              <span className={`px-2 py-0.5 rounded-lg text-[10px] font-black uppercase tracking-tighter ${
                activity.source === ActivitySource.COMMUNITY ? 'bg-indigo-50 text-indigo-500' : 'bg-slate-50 text-slate-400'
              }`}>
                {activity.source === ActivitySource.COMMUNITY ? 'Community' : 'System'}
              </span>
            </div>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="activities-page">
      {/* 统计卡片 */}
      {stats && (
        <div className="bg-white rounded-2xl border border-gray-200 p-6 mb-8 grid grid-cols-4 gap-6 shadow-sm">
          <div className="text-center p-4 bg-slate-50/50 rounded-xl border border-slate-100">
            <div className="text-2xl font-black text-slate-800">{stats.total_count}</div>
            <div className="text-[10px] text-gray-400 font-black uppercase tracking-widest mt-1">Total Activities</div>
          </div>
          <div className="text-center p-4 bg-emerald-50/30 rounded-xl border border-emerald-100/50">
            <div className="text-2xl font-black text-emerald-600">{stats.today_count}</div>
            <div className="text-[10px] text-gray-400 font-black uppercase tracking-widest mt-1">Activities Today</div>
          </div>
          <div className="text-center p-4 bg-blue-50/30 rounded-xl border border-blue-100/50">
            <div className="text-2xl font-black text-blue-600">{stats.week_count}</div>
            <div className="text-[10px] text-gray-400 font-black uppercase tracking-widest mt-1">Weekly Volume</div>
          </div>
          <div className="text-center p-4 bg-purple-50/30 rounded-xl border border-purple-100/50">
            <div className="text-2xl font-black text-purple-600">{stats.month_count}</div>
            <div className="text-[10px] text-gray-400 font-black uppercase tracking-widest mt-1">Monthly Engagement</div>
          </div>
        </div>
      )}

      {/* 过滤器 */}
      <div className="bg-white rounded-2xl border border-gray-200 p-4 mb-8 flex items-center justify-between shadow-sm">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2 px-3 py-1.5 bg-slate-50 text-slate-400 rounded-xl">
            <Filter className="w-4 h-4" />
            <span className="text-xs font-bold uppercase tracking-wider">Filters</span>
          </div>

          <Select
            value={filterSource}
            onChange={setFilterSource}
            className="w-40 custom-select-xl"
            variant="borderless"
          >
            <Option value="all">所有来源</Option>
            <Option value={ActivitySource.USER_CENTER}>个人中心</Option>
            <Option value={ActivitySource.BACKTEST}>回测系统</Option>
          </Select>

          <Select
            value={filterType}
            onChange={setFilterType}
            className="w-40 custom-select-xl"
            variant="borderless"
          >
            <Option value="all">所有类型</Option>
            <Option value={ActivityType.STRATEGY_CREATE}>创建策略</Option>
            <Option value={ActivityType.STRATEGY_SHARE}>分享策略</Option>
          </Select>
        </div>

        <button
          onClick={loadActivities}
          className={`flex items-center gap-2 px-6 py-2 bg-slate-900 text-white rounded-xl font-bold text-sm hover:bg-slate-800 transition-all ${loading ? 'opacity-50 cursor-not-allowed' : ''}`}
        >
          <ReloadOutlined className={loading ? 'animate-spin' : ''} />
          <span>刷新记录</span>
        </button>
      </div>

      {/* 活动时间线 */}
      <Spin spinning={loading} indicator={<RefreshCw className="animate-spin text-blue-500" />}>
        {groupedActivities.length === 0 ? (
          <div className="py-24 text-center">
            <Empty description={<span className="text-slate-400 font-medium">暂无活动记录</span>} image={Empty.PRESENTED_IMAGE_SIMPLE} />
          </div>
        ) : (
          <div className="space-y-10">
            {groupedActivities.map(group => (
              <div key={group.date}>
                {/* 日期标签 */}
                <div className="flex items-center gap-3 mb-6 px-2">
                  <div className="w-10 h-10 rounded-2xl bg-white border border-gray-200 shadow-sm flex items-center justify-center">
                    <Calendar className="w-5 h-5 text-blue-500" />
                  </div>
                  <div>
                    <div className="text-base font-black text-slate-800">{group.dateLabel}</div>
                    <div className="text-[10px] text-gray-400 font-black uppercase tracking-widest">{group.activities.length} Events Logged</div>
                  </div>
                </div>

                {/* 活动列表 */}
                <div className="pl-5 border-l-2 border-slate-100 ml-5 space-y-2">
                  {group.activities.map(activity => (
                    <div key={activity.id} className="relative">
                      <div className="absolute -left-[31px] top-5 w-2.5 h-2.5 rounded-full bg-white border-2 border-blue-500 z-10 shadow-sm" />
                      {renderActivityItem(activity)}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </Spin>
    </div>
  );
};

export default ActivitiesPage;
