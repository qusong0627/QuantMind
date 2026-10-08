/**
 * PageHeader — 统一页头（icon 芯片 h-7 + text-base 标题 + text-[10px] 副标题 + 右侧动作）。
 *
 * 尺寸口径来自因子池页既有惯例（原先 h-9 chip / h1 text-3xl 各页不一，
 * 「顶部图标太大」即此）；新页面一律用本组件，不要再手写页头。
 */

import React from 'react';
import type { LucideIcon } from 'lucide-react';

export interface PageHeaderProps {
  icon: LucideIcon;
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
}

export const PageHeader: React.FC<PageHeaderProps> = ({ icon: Icon, title, subtitle, actions }) => (
  <div className="flex flex-wrap items-center gap-3">
    <div className="mr-auto flex min-w-0 items-center gap-2">
      <div className="relative flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-violet-600 via-purple-600 to-fuchsia-600 text-white shadow-xs">
        <Icon className="h-3.5 w-3.5" />
      </div>
      <div className="min-w-0">
        <h2 className="m-0 truncate text-base font-black tracking-tight leading-none text-slate-800">
          {title}
        </h2>
        {subtitle && (
          <p className="m-0 mt-1 text-[10px] font-bold leading-none text-slate-400">{subtitle}</p>
        )}
      </div>
    </div>
    {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
  </div>
);

export default PageHeader;
