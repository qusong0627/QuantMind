import React, { useState, useEffect } from 'react';
import { Card } from '../common/Card';
import { Loading } from '../common/Loading';

interface Alert {
  type: 'risk' | 'opportunity' | 'strategy' | 'market';
  message: string;
  time: string;
  priority: 'high' | 'medium' | 'low';
}

export const IntelligenceAlertsCard: React.FC = () => {
  const [loading, setLoading] = useState(true);
  const [alerts, setAlerts] = useState<Alert[]>([
    { type: 'risk', message: '中国平安波动率异常', time: '14:25', priority: 'high' },
    { type: 'opportunity', message: '招商银行：多因子分数回升', time: '14:20', priority: 'medium' },
    { type: 'strategy', message: 'AI策略A收益率达到预期', time: '14:15', priority: 'low' },
    { type: 'market', message: '沪深300突破阻力位', time: '14:10', priority: 'medium' }
  ]);

  useEffect(() => {
    setTimeout(() => setLoading(false), 1300);
  }, []);

  if (loading) {
    return (
      <Card title="智能提醒模块" background="alert" height="100%">
        <Loading text="加载提醒数据..." />
      </Card>
    );
  }

  const getAlertIcon = (type: string) => {
    switch (type) {
      case 'risk': return '⚠️';
      case 'opportunity': return '💡';
      case 'strategy': return '🤖';
      case 'market': return '📊';
      default: return '📢';
    }
  };

  const getAlertColor = (type: string) => {
    switch (type) {
      case 'risk': return 'border-l-rose-500 bg-slate-50';
      case 'opportunity': return 'border-l-emerald-500 bg-slate-50';
      case 'strategy': return 'border-l-blue-500 bg-slate-50';
      case 'market': return 'border-l-amber-500 bg-slate-50';
      default: return 'border-l-slate-400 bg-slate-50';
    }
  };

  const getPriorityColor = (priority: string) => {
    switch (priority) {
      case 'high': return 'text-red-600';
      case 'medium': return 'text-yellow-600';
      case 'low': return 'text-green-600';
      default: return 'text-gray-600';
    }
  };

  return (
    <Card title="智能提醒模块" background="alert" height="100%">
      <div className="space-y-3">
        {alerts.map((alert, index) => (
          <div
            key={index}
            className={`p-3 rounded-lg border-l-4 ${getAlertColor(alert.type)}`}
          >
            <div className="flex items-start justify-between">
              <div className="flex items-start gap-2 flex-1">
                <span className="text-lg">{getAlertIcon(alert.type)}</span>
                <div className="flex-1">
                  <p className="text-sm font-medium text-gray-800">{alert.message}</p>
                  <div className="flex items-center gap-2 mt-1">
                    <span className="text-xs text-gray-500">{alert.time}</span>
                    <span className={`text-xs font-medium ${getPriorityColor(alert.priority)}`}>
                      {alert.priority === 'high' ? '高' : alert.priority === 'medium' ? '中' : '低'}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
};
