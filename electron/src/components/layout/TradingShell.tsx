import React from 'react';

interface TradingShellProps {
  children: React.ReactNode;
  className?: string;
}

/**
 * 交易类页面统一外壳（设计对齐企业版模拟盘 SimulationTradingPage）：
 * - 外层径向渐变背景 + p-6 全页内边距
 * - 主容器 rounded-[32px] / 半透明白边框 / 浅蓝灰底色 / 大扩散阴影
 * - 侧边栏与主内容区拆分组件，方便回测、持仓、交易记录等页复用
 */
export function TradingShell({ children, className }: TradingShellProps) {
  return (
    <div
      className={`flex h-full w-full flex-col overflow-hidden p-6 ${className || ''}`}
      style={{
        background:
          'radial-gradient(circle at 12% 0%, #f7f8ff 0%, #eef2f6 52%, #eaf0f4 100%)',
      }}
    >
      {/* 主容器 */}
      <div className="flex h-full min-h-0 w-full flex-col overflow-hidden rounded-[32px] border border-white/90 bg-[#f6f8fa] shadow-[0_24px_70px_-32px_rgba(30,41,59,0.35)]">
        {children}
      </div>
    </div>
  );
}

/**
 * 交易页左侧边栏（同款）
 */
export function TradingSidebar({
  children,
  width = 216,
}: {
  children: React.ReactNode;
  width?: number | string;
}) {
  return (
    <aside
      className="flex shrink-0 flex-col border-r border-slate-200/70 bg-[#fafbfc]"
      style={{ width: typeof width === 'number' ? `${width}px` : width }}
    >
      {children}
    </aside>
  );
}

/**
 * 交易页右侧内容区（同款，带细点网格背景）
 */
export function TradingMain({ children }: { children: React.ReactNode }) {
  return (
    <main className="relative min-w-0 flex-1 overflow-hidden bg-[#f4f6f8]">
      <div
        className="pointer-events-none absolute inset-0 opacity-40"
        style={{
          backgroundImage: 'radial-gradient(#cbd5e1 0.6px, transparent 0.6px)',
          backgroundSize: '22px 22px',
        }}
      />
      <div className="relative h-full">{children}</div>
    </main>
  );
}

/**
 * 交易页通用卡片（同款圆角、边框、阴影）
 * variant:
 *  - default: 浅蓝灰底，半透明白边框
 *  - elevated: 白底，灰边框，较重阴影
 *  - dark: 深色底色，白文字
 */
export function TradingCard({
  children,
  variant = 'default',
  className,
}: {
  children: React.ReactNode;
  variant?: 'default' | 'elevated' | 'dark';
  className?: string;
}) {
  const base = 'overflow-hidden transition-all duration-200';
  const variants: Record<string, string> = {
    default:
      'rounded-[22px] border border-white/80 bg-[#f6f8fa] shadow-sm',
    elevated:
      'rounded-[22px] border-slate-200/80 bg-white shadow-[0_8px_28px_-20px_rgba(15,23,42,0.25)]',
    dark:
      'rounded-[22px] bg-[#131d31] text-white shadow-[0_16px_36px_-18px_rgba(15,23,42,0.65)]',
  };
  return (
    <div className={`${base} ${variants[variant]} ${className || ''}`}>
      {children}
    </div>
  );
}
