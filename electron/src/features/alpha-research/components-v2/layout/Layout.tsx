import React from 'react';
import { Sparkles, Database, BarChart3, Settings as SettingsIcon, Network, History } from 'lucide-react';

export type PageId =
  | 'home'
  | 'library'
  | 'pool'
  | 'backtest'
  | 'history'
  | 'settings'
  | 'mining_dashboard';

interface LayoutProps {
  children: React.ReactNode;
  currentPage: PageId;
  onNavigate: (page: PageId) => void;
  showNavigation?: boolean;
}

export const Layout: React.FC<LayoutProps> = ({
  children,
  currentPage,
  onNavigate,
  showNavigation = true,
}) => {
  // 导航一律直达目标页：进度台由「开始挖掘后的自动跳转」或首页「查看演化台」进入，
  // 不能因为存在历史/运行中任务就把「因子挖掘」强制带到进度页。
  const handleNavClick = (itemId: PageId) => {
    onNavigate(itemId);
  };

  const navItems = [
    { id: 'home' as const, label: '因子挖掘', icon: Sparkles },
    { id: 'library' as const, label: '因子库', icon: Database },
    { id: 'pool' as const, label: '因子池', icon: Network },
    { id: 'backtest' as const, label: '回测', icon: BarChart3 },
    { id: 'history' as const, label: '挖掘历史', icon: History },
    { id: 'settings' as const, label: '设置', icon: SettingsIcon },
  ];

  return (
    <div className="min-h-screen bg-transparent gradient-mesh p-4 pt-2 select-none">
      {/* 顶部悬浮圆角磨砂胶囊顶栏 (Preserves window rounded corners and window controls) */}
      <header className="sticky top-[38px] z-40 max-w-6xl mx-auto w-full px-5 py-2 rounded-2xl bg-white/80 backdrop-blur-xl border border-white/90 shadow-xs flex items-center justify-between transition-all">
        {/* Left: Brand Logo & Title */}
        <div 
          className="flex items-center gap-3 cursor-pointer hover:opacity-85 transition-opacity"
          onClick={() => onNavigate('home')}
        >
          <div className="relative flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-blue-600 via-indigo-600 to-purple-600 text-white shadow-xs">
            <Sparkles className="h-3.5 w-3.5" />
          </div>
          <div>
            <h1 className="text-sm font-black text-slate-800 m-0 tracking-tight leading-none">QuantaAlpha</h1>
            <p className="text-[10px] font-bold text-slate-400 m-0 leading-none mt-1">智能因子挖掘平台</p>
          </div>
        </div>

        {/* Right: Navigation Tabs (with anti-collision spacing for window controls) */}
        {showNavigation && (
          <nav className="flex items-center gap-1.5 pr-20">
            {navItems.map((item) => {
              const Icon = item.icon;
              const isActive = currentPage === item.id || (item.id === 'home' && currentPage === 'mining_dashboard');
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => handleNavClick(item.id)}
                  className={`flex items-center gap-1.5 px-3.5 py-1.5 rounded-xl text-xs font-bold transition-all cursor-pointer ${
                    isActive
                      ? 'bg-gradient-to-r from-blue-600 to-indigo-600 text-white shadow-xs'
                      : 'text-slate-500 hover:text-slate-800 hover:bg-slate-100/70'
                  }`}
                >
                  <Icon className="h-3.5 w-3.5" />
                  <span>{item.label}</span>
                </button>
              );
            })}
          </nav>
        )}
      </header>

      {/* Main Content（pt 预留顶部悬浮顶栏高度，避免首屏内容被遮挡） */}
      <main className="pt-[46px] pb-20">
        <div className="max-w-6xl mx-auto">{children}</div>
      </main>
    </div>
  );
};

export default Layout;
