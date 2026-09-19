/**
 * 推理中心工作区切换（三工作区顶栏切换 IA）。
 *
 * 机构级推理平台的三类使用场景在**操作节奏**上完全不同，混在一屏里会让每一类都别扭：
 *   - 单票研判：慢，反复调参看同一只票（改基准日 / 换模型 / 换周期）
 *   - 截面选股：快，扫全市场找标的（排序 / 过滤 / 导出）
 *   - 模型治理：离线，审模型资产（哪个模型能出区间、哪个归因不可用、哪个批次失败）
 *
 * 因此拆成三个并列工作区，共享同一份市场上下文与顶栏状态带（切换不丢上下文）。
 */

import React from 'react';
import { clsx } from 'clsx';
import { Target, Table2, ShieldCheck } from 'lucide-react';

export type WorkspaceKey = 'single' | 'cross' | 'governance';

interface WorkspaceMeta {
  key: WorkspaceKey;
  label: string;
  hint: string;
  icon: React.ComponentType<{ size?: number | string; className?: string }>;
}

export const WORKSPACES: WorkspaceMeta[] = [
  {
    key: 'single',
    label: '单票研判',
    hint: '逐只标的做 T+N 预测、多模型对比与因子归因',
    icon: Target,
  },
  {
    key: 'cross',
    label: '截面选股',
    hint: '全市场截面打分排序、信号过滤与名单导出',
    icon: Table2,
  },
  {
    key: 'governance',
    label: '模型治理',
    hint: '模型资产盘点：周期口径、区间能力、归因可用性、批次健康度',
    icon: ShieldCheck,
  },
];

interface WorkspaceTabsProps {
  active: WorkspaceKey;
  onChange: (key: WorkspaceKey) => void;
  /** 每个工作区右上角的小角标（如截面行数、模型数）；缺省不显示 */
  badges?: Partial<Record<WorkspaceKey, string>>;
}

export const WorkspaceTabs: React.FC<WorkspaceTabsProps> = ({ active, onChange, badges }) => (
  <nav
    aria-label="推理中心工作区"
    className="flex items-center gap-1 bg-slate-100/80 border border-slate-200 rounded-lg p-0.5 shrink-0"
  >
    {WORKSPACES.map((w) => {
      const Icon = w.icon;
      const isActive = active === w.key;
      return (
        <button
          key={w.key}
          type="button"
          onClick={() => onChange(w.key)}
          title={w.hint}
          aria-current={isActive ? 'page' : undefined}
          className={clsx(
            'flex items-center gap-1.5 px-3 h-8 rounded-md text-xs font-bold transition-all whitespace-nowrap',
            isActive
              ? 'bg-white text-blue-700 shadow-sm ring-1 ring-blue-100'
              : 'text-slate-500 hover:text-slate-800 hover:bg-white/60',
          )}
        >
          <Icon size={13} className={isActive ? 'text-blue-600' : 'text-slate-400'} />
          {w.label}
          {badges?.[w.key] && (
            <span
              className={clsx(
                'font-mono text-[10px] font-black px-1 rounded',
                isActive ? 'bg-blue-50 text-blue-600' : 'bg-slate-200 text-slate-500',
              )}
            >
              {badges[w.key]}
            </span>
          )}
        </button>
      );
    })}
  </nav>
);
