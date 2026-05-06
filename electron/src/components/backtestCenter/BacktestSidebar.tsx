/**
 * 回测中心左侧导航栏
 *
 * 功能：
 * - 6个功能模块按钮
 * - 激活状态指示
 * - 悬停动画效果
 */

import React from 'react';
import { motion } from 'framer-motion';
import {
  Zap,
  Code2,
  History,
  GitCompare,
  Settings,
  FolderKanban,
  TrendingUp,
  HelpCircle,
} from 'lucide-react';
import { ModuleId } from '../../stores/backtestCenterStore';

interface Module {
  id: ModuleId;
  name: string;
  icon: React.ComponentType<{ className?: string }>;
  color: string;
  description: string;
}

const modules: Module[] = [
  {
    id: 'quick-backtest',
    name: '快速回测',
    icon: Zap,
    color: 'text-blue-400',
    description: '快速运行单次 Qlib 回测'
  },
  {
    id: 'expert-mode',
    name: '专家模式',
    icon: Code2,
    color: 'text-indigo-400',
    description: '云端策略开发与回测'
  },
  {
    id: 'backtest-history',
    name: '回测历史',
    icon: History,
    color: 'text-purple-400',
    description: '查看和管理历史记录'
  },
  {
    id: 'strategy-compare',
    name: '策略对比',
    icon: GitCompare,
    color: 'text-green-400',
    description: '对比多个回测结果'
  },
  {
    id: 'parameter-optimize',
    name: '参数优化',
    icon: Settings,
    color: 'text-orange-400',
    description: '遗传算法优化 Qlib 参数'
  },
  {
    id: 'strategy-management',
    name: '策略管理',
    icon: FolderKanban,
    color: 'text-cyan-400',
    description: '管理策略生命周期与云端保存'
  },
  {
    id: 'advanced-analysis',
    name: '高级分析',
    icon: TrendingUp,
    color: 'text-pink-400',
    description: '深度性能分析'
  }
];

interface BacktestSidebarProps {
  width: number;
  activeModule: ModuleId;
  onModuleChange: (id: ModuleId) => void;
}

export const BacktestSidebar: React.FC<BacktestSidebarProps> = ({
  width,
  activeModule,
  onModuleChange,
}) => {

  return (
    <aside
      className="bg-white border-r border-gray-200 flex flex-col shadow-sm"
      style={{ width: `${width}px` }}
    >
      {/* 模块列表 */}
      <div className="flex-1 py-4 overflow-y-auto custom-scrollbar">
        <div className="px-6 mb-2">
          <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
            功能模块
          </p>
        </div>

        <div className="space-y-1">
          {modules.map((module) => (
            <ModuleButton
              key={module.id}
              module={module}
              isActive={activeModule === module.id}
              onClick={() => onModuleChange(module.id)}
            />
          ))}
        </div>
      </div>

      {/* 底部帮助链接 */}
      <div className="border-t border-gray-200 p-4">
        <a
          href="https://api.quantmind.cloud/help"
          target="_blank"
          rel="noopener noreferrer"
          className="w-full flex items-center gap-3 px-4 py-3 rounded-2xl text-gray-600 hover:text-blue-600 hover:bg-blue-50 transition-colors"
        >
          <HelpCircle className="w-5 h-5" />
          <span className="text-sm">帮助中心</span>
        </a>
      </div>

    </aside>
  );
};

// ============================================================================
// 模块按钮组件
// ============================================================================

interface ModuleButtonProps {
  module: Module;
  isActive: boolean;
  onClick: () => void;
}

const ModuleButton: React.FC<ModuleButtonProps> = ({
  module,
  isActive,
  onClick,
}) => {
  const Icon = module.icon;

  return (
    <motion.button
      onClick={onClick}
      whileHover={{ x: 4 }}
      whileTap={{ scale: 0.98 }}
      className={`
        relative w-full px-6 text-left transition-colors
        ${isActive ? 'bg-blue-50' : 'hover:bg-gray-50'}
      `}
    >
      {/* 激活指示器 */}
      {isActive && (
        <motion.div
          layoutId="activeIndicator"
          className="absolute left-0 top-0 bottom-0 w-1 bg-blue-500 rounded-r-full"
          transition={{ type: 'spring', stiffness: 300, damping: 30 }}
        />
      )}

      <div className="flex items-center gap-3 py-3 px-0">
        <div className={`
          w-10 h-10 rounded-2xl flex items-center justify-center transition-colors
          ${isActive ? 'bg-blue-500/10 shadow-sm' : 'bg-gray-100'}
        `}>
          <Icon className={`w-5 h-5 ${isActive ? 'text-blue-600' : 'text-gray-600'}`} />
        </div>

        <div className="flex-1 min-w-0">
          <div className={`font-medium text-sm ${isActive ? 'text-gray-900' : 'text-gray-700'}`}>
            {module.name}
          </div>
          <div className="text-xs text-gray-500 truncate">
            {module.description}
          </div>
        </div>
      </div>
    </motion.button>
  );
};
