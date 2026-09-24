import React, { useEffect, useState } from 'react';
import { Typography, Tooltip, Progress } from 'antd';
import {
  Cpu, HardDrive, Server, Activity, AlertCircle, CheckCircle2
} from 'lucide-react';
import { adminService } from '../services/adminService';
import { SystemLoadSummary } from '../types';

const { Text } = Typography;

interface AdminSystemLoadWidgetProps {
  collapsed?: boolean;
}

export const AdminSystemLoadWidget: React.FC<AdminSystemLoadWidgetProps> = ({ collapsed = false }) => {
  const [load, setLoad] = useState<SystemLoadSummary | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let isMounted = true;

    const fetchLoad = async () => {
      try {
        const data = await adminService.getSystemLoad();
        if (isMounted && data?.workload) {
          setLoad(data);
        }
      } catch (e) {
        // 静默处理轮询错误
      } finally {
        if (isMounted) setLoading(false);
      }
    };

    fetchLoad();
    const timer = setInterval(fetchLoad, 15000); // 15秒静默轮询

    return () => {
      isMounted = false;
      clearInterval(timer);
    };
  }, []);

  const cpuPercent = load?.workload?.cpu_percent ?? 0;
  const memPercent = load?.workload?.memory_percent ?? 0;
  const diskPercent = load?.workload?.disk_percent ?? 0;
  const cpuCount = load?.workload?.cpu_count ?? 1;
  const memUsed = load?.workload?.memory_used_gb ?? 0;
  const memTotal = load?.workload?.memory_total_gb ?? 0;
  const healthScore = load?.health_score ?? 100;
  const healthyServices = load?.services_summary?.healthy ?? 0;
  const totalServices = load?.services_summary?.total ?? 0;
  const uptimeDays = load?.uptime_days ?? 0;

  const getStatusColor = (percent: number) => {
    if (percent < 60) return '#10b981'; // 正常 绿色
    if (percent < 85) return '#f59e0b'; // 警告 黄色
    return '#f43f5e'; // 高负荷 红色
  };

  const getHealthBadgeColor = (score: number) => {
    if (score >= 80) return 'bg-emerald-500';
    if (score >= 60) return 'bg-amber-500';
    return 'bg-rose-500';
  };

  const tooltipContent = (
    <div className="space-y-1.5 p-1 text-xs font-mono">
      <div className="font-bold border-b border-slate-700 pb-1 text-slate-200 flex items-center justify-between gap-4">
        <span>宿主机系统负载详情</span>
        <span className="text-[10px] text-emerald-400">运行 {uptimeDays} 天</span>
      </div>
      <div className="flex justify-between gap-4 text-slate-300">
        <span>CPU 负载:</span>
        <span className="font-bold text-white">{cpuPercent}% ({cpuCount} 逻辑核心)</span>
      </div>
      <div className="flex justify-between gap-4 text-slate-300">
        <span>内存占用:</span>
        <span className="font-bold text-white">{memPercent}% ({memUsed}G / {memTotal}G)</span>
      </div>
      <div className="flex justify-between gap-4 text-slate-300">
        <span>数据盘空间:</span>
        <span className="font-bold text-white">{diskPercent}%</span>
      </div>
      <div className="flex justify-between gap-4 text-slate-300">
        <span>微服务集群:</span>
        <span className="font-bold text-emerald-400">{healthyServices}/{totalServices} 正常</span>
      </div>
    </div>
  );

  // 折叠侧边栏状态下的紧凑微型卡片
  if (collapsed) {
    return (
      <Tooltip title={tooltipContent} placement="right">
        <div className="p-2 border-t border-slate-100 flex flex-col items-center gap-2 cursor-pointer hover:bg-slate-50 transition-colors">
          <div className="relative">
            <Activity className="w-5 h-5 text-slate-600" />
            <span className={`absolute -top-0.5 -right-0.5 w-2 h-2 rounded-full ${getHealthBadgeColor(healthScore)} ring-2 ring-white`} />
          </div>
          <span className="text-[9px] font-mono font-bold text-slate-600">
            {cpuPercent}%
          </span>
        </div>
      </Tooltip>
    );
  }

  // 展开侧边栏状态下的系统负载仪表卡片
  return (
    <Tooltip title={tooltipContent} placement="right" mouseEnterDelay={0.5}>
      <div className="p-3.5 border-t border-slate-100 bg-white">
        <div className="bg-slate-50/90 rounded-2xl p-3 border border-slate-100 hover:border-slate-200 transition-all shadow-2xs space-y-2.5">
          {/* 标题行 */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-1.5">
              <span className={`w-2 h-2 rounded-full ${getHealthBadgeColor(healthScore)} animate-pulse`} />
              <Text className="text-xs font-black text-slate-500 uppercase tracking-wider">系统真实负载</Text>
            </div>
            {totalServices > 0 && (
              <span className="text-[10px] font-mono font-bold text-slate-400 bg-white px-1.5 py-0.2 rounded border border-slate-100">
                {healthyServices}/{totalServices} 在线
              </span>
            )}
          </div>

          {/* CPU 进度 */}
          <div className="space-y-1">
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-slate-500 flex items-center gap-1 font-medium">
                <Cpu className="w-3 h-3 text-slate-400" /> CPU
              </span>
              <span className="font-mono font-bold text-slate-700">
                {cpuPercent}% <span className="text-[9px] text-slate-400 font-normal">({cpuCount}核)</span>
              </span>
            </div>
            <div className="h-1.5 w-full bg-slate-200/80 rounded-full overflow-hidden">
              <div
                className="h-full rounded-full transition-all duration-500"
                style={{
                  width: `${Math.min(100, Math.max(2, cpuPercent))}%`,
                  backgroundColor: getStatusColor(cpuPercent),
                }}
              />
            </div>
          </div>

          {/* 内存 进度 */}
          <div className="space-y-1">
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-slate-500 flex items-center gap-1 font-medium">
                <HardDrive className="w-3 h-3 text-slate-400" /> 内存
              </span>
              <span className="font-mono font-bold text-slate-700">
                {memPercent}% <span className="text-[9px] text-slate-400 font-normal">({memUsed}G)</span>
              </span>
            </div>
            <div className="h-1.5 w-full bg-slate-200/80 rounded-full overflow-hidden">
              <div
                className="h-full rounded-full transition-all duration-500"
                style={{
                  width: `${Math.min(100, Math.max(2, memPercent))}%`,
                  backgroundColor: getStatusColor(memPercent),
                }}
              />
            </div>
          </div>
        </div>
      </div>
    </Tooltip>
  );
};

export default AdminSystemLoadWidget;
