import React, { useState, useCallback, lazy, Suspense } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import { RecoilRoot } from 'recoil';
import { Routes, Route, Navigate, useNavigate, useLocation } from 'react-router-dom';
import { motion } from 'framer-motion';
import { Spin, notification, Button, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';
import { DashboardSkeleton } from './components/common/DashboardSkeleton';
import { DashboardLayout } from './components/layout/DashboardLayout';
import { FloatingNavBar } from './components/navigation/FloatingNavBar';
import { TitleBar } from './components/layout/TitleBar';
import { ErrorBoundary } from './components/common/ErrorBoundary';
import { useMenuExport } from './hooks/useMenuExport';
import { WebSocketProvider } from './contexts/WebSocketContext';
import { QueryProvider } from './providers/QueryProvider';
import { selectCurrentTab, setCurrentTab } from './store/slices/aiStrategySlice';
import type { DashboardTab } from './store/slices/aiStrategySlice';
import logger from './utils/safeLogger';
import { refreshOrchestrator } from './services/refreshOrchestrator';
import { useTradingModeInitialization } from './hooks/useTradingModeInitialization';
import { authService } from './features/auth/services/authService';
import { initDynamicServerUrl } from './config/services';

// 认证相关组件
import AppRoutes from './features/auth/AppRoutes';
import { useAuth } from './features/auth/hooks/useAuth';
import { LazyProtectedRoute as ProtectedRoute } from './features/auth/utils/lazyLoad';
import { preloadAiIdeResources } from './features/auth/utils/lazyLoad';

import './styles/global.css';
import './styles/mac-theme.css';
import './styles/ai-strategy-theme.css';

const UserCenterPage = lazy(() => import('./features/user-center/pages/UserCenterPage'));
const StrategyComparisonPage = lazy(() => import('./features/strategy-comparison/pages/StrategyComparisonPage'));
const StrategyWizardPage = lazy(() => import('./features/strategy-wizard/components/SmartStrategyStudioV2'));
const QuantBotPage = lazy(() => import('./features/quantbot/pages/QuantBotPage'));
const AIIDEPage = lazy(() => import('./pages/AIIDEPage'));
const ModelTrainingPage = lazy(() => import('./pages/ModelTrainingPage'));
const ModelRegistryPage = lazy(() => import('./pages/ModelRegistryPage'));
const ResearchPlatformPage = lazy(() => import('./pages/ResearchPlatformPage').then(m => ({ default: m.default || m.ResearchPlatformPage })));
const RealTradingPage = lazy(() => import('./pages/trading/RealTradingPage'));
const AdminPage = lazy(() => import('./features/admin/AdminPage'));
const AdminDashboard = lazy(() => import('./features/admin/components/AdminDashboard').then(m => ({ default: m.AdminDashboard })));
const AdminUserTable = lazy(() => import('./features/admin/components/AdminUserTable').then(m => ({ default: m.AdminUserTable })));
const AdminModelManagement = lazy(() => import('./features/admin/components/AdminModelManagement').then(m => ({ default: m.AdminModelManagement })));
const AdminDataManagement = lazy(() => import('./features/admin/components/AdminDataManagement').then(m => ({ default: m.AdminDataManagement })));
const AdminStrategyTemplates = lazy(() => import('./features/admin/components/AdminStrategyTemplates').then(m => ({ default: m.AdminStrategyTemplates })));

// 主题切换hook
// 主题管理已移除 - 应用统一使用浅色主题
const useTheme = () => {
  // 不再监听系统主题变化，强制使用浅色主题
};

// 模块配置接口
interface DashboardModule {
  id: string;
  title: string;
  component: string;
  size: 'small' | 'medium' | 'large';
  position: { x: number; y: number };
  isVisible: boolean;
}

// 默认模块配置
const defaultModules: DashboardModule[] = [
  { id: 'market', title: '市场概览', component: 'market', size: 'medium', position: { x: 0, y: 0 }, isVisible: true },
  { id: 'fund', title: '资金概览', component: 'fund', size: 'medium', position: { x: 1, y: 0 }, isVisible: true },
  { id: 'trade', title: '交易记录', component: 'trade', size: 'medium', position: { x: 2, y: 0 }, isVisible: true },
  { id: 'strategy', title: '策略监控', component: 'strategy', size: 'medium', position: { x: 0, y: 1 }, isVisible: true },
  { id: 'charts', title: '智能图表', component: 'charts', size: 'medium', position: { x: 1, y: 1 }, isVisible: true },
  { id: 'ai-quick', title: '信息通知', component: 'ai-quick', size: 'medium', position: { x: 2, y: 1 }, isVisible: true }
];

export default function App() {
  const dispatch = useDispatch();
  const tab = useSelector(selectCurrentTab);
  const navigate = useNavigate();
  const location = useLocation();
  const [modules, setModules] = useState<DashboardModule[]>(defaultModules);
  const [serverConfigReady, setServerConfigReady] = useState(false);
  const { ExportModal } = useMenuExport();
  const { isAuthenticated, isLoading } = useAuth();
  useTheme();
  useTradingModeInitialization();
  
  dayjs.locale('zh-cn');

  // 定义公开路由列表
  const publicRoutes = [
    '/auth/login',
    '/auth/register',
    '/auth/forgot-password',
    '/auth/reset-password',
    '/auth/mfa/verify',
    '/auth/mfa/setup'
  ];

  // 检查是否为公开路由
  const isPublicRoute = publicRoutes.some(route =>
    location.pathname.startsWith(route)
  );

  // 处理导航栏切换
  const handleNavChange = (newTab: string) => {
    // 映射 ID 到路由路径
    const routeMap: Record<string, string> = {
      'agent': '/quantbot',
      'strategy': '/strategy-wizard',
      'ai-ide': '/ai-ide',
      'model-training': '/model-training',
      'model-registry': '/model-registry',
      'research': '/research',
      'trading': '/trading',
      'profile': '/user-center',
      'admin': '/admin',
    };
    if (routeMap[newTab]) {
      navigate(routeMap[newTab]);
      // 同时更新 tab 状态以保持高亮
      dispatch(setCurrentTab(newTab as any));
    } else {
      // 如果不在主控制台路径，先回跳
      if (location.pathname !== '/') {
        navigate('/');
      }
      dispatch(setCurrentTab(newTab as any));
    }
  };

  // 注意：不再强制根据路由回写 tab=profile，避免在 /user-center 下点击其它栏目首次无效
  // 由 handleNavChange 主动执行路由跳转与 tab 切换，确保一次点击即可生效

  // 根据路由同步当前tab，保证导航高亮且避免闪屏
  React.useEffect(() => {
    if (location.pathname.startsWith('/user-center')) {
      dispatch(setCurrentTab('profile' as DashboardTab));
    } else if (location.pathname.startsWith('/strategy-wizard')) {
      dispatch(setCurrentTab('strategy' as DashboardTab));
    } else if (location.pathname.startsWith('/quantbot')) {
      dispatch(setCurrentTab('agent' as DashboardTab));
    } else if (location.pathname.startsWith('/ai-ide')) {
      dispatch(setCurrentTab('ai-ide' as DashboardTab));
    } else if (location.pathname.startsWith('/model-training')) {
      dispatch(setCurrentTab('model-training' as DashboardTab));
    } else if (location.pathname.startsWith('/model-registry')) {
      dispatch(setCurrentTab('model-registry' as DashboardTab));
    } else if (location.pathname.startsWith('/research')) {
      dispatch(setCurrentTab('research' as DashboardTab));
    } else if (location.pathname.startsWith('/trading')) {
      dispatch(setCurrentTab('trading' as DashboardTab));
    } else if (location.pathname.startsWith('/admin')) {
      dispatch(setCurrentTab('admin' as DashboardTab));
    } else if (location.pathname === '/') {
      // 保留根路由下的当前 tab（避免从策略页返回时被强制重置为仪表盘）
      return;
    }
  }, [location.pathname, dispatch]);

  const handleLayoutChange = (newLayout: DashboardModule[]) => {
    setModules(newLayout);
  };

  // 初始化时加载保存的布局
  React.useEffect(() => {
    const savedLayout = localStorage.getItem('dashboardLayout');
    if (savedLayout) {
      try {
        const parsedLayout = JSON.parse(savedLayout);
        setModules(parsedLayout);
      } catch (error) {
        logger.error('加载保存的布局失败:', error);
      }
    }
  }, []);

  // 应用启动时优先恢复用户保存的服务器地址，避免业务页先回落到 localhost。
  React.useEffect(() => {
    let cancelled = false;

    const bootstrapServerConfig = async () => {
      try {
        await initDynamicServerUrl();
      } catch (error) {
        logger.warn('初始化服务器地址失败，将继续使用默认配置:', error);
      } finally {
        if (!cancelled) {
          setServerConfigReady(true);
        }
      }
    };

    void bootstrapServerConfig();

    return () => {
      cancelled = true;
    };
  }, []);

  // 全局预加载 AI-IDE 资源：应用启动/刷新后统一触发（开发/生产均生效）
  React.useEffect(() => {
    const trigger = () => {
      void preloadAiIdeResources();
    };

    trigger(); // 立即预热一轮，减少首次进入 AI-IDE 时的资源加载等待。

    const idleCallback = (window as any).requestIdleCallback as
      | ((cb: () => void, opts?: { timeout: number }) => number)
      | undefined;

    if (typeof idleCallback === 'function') {
      const id = idleCallback(trigger, { timeout: 1200 });
      return () => {
        if (typeof (window as any).cancelIdleCallback === 'function') {
          (window as any).cancelIdleCallback(id);
        }
      };
    }

    const timer = window.setTimeout(trigger, 300);
    return () => {
      window.clearTimeout(timer);
    };
  }, []);

  // AI-IDE 保活：应用启动后即触发一次，随后定时维持后端活跃状态（Electron 环境）
  React.useEffect(() => {
    if (!(window as any).electronAPI?.keepAliveAIIDEBackend) return;

    const keepAlive = async () => {
      try {
        await (window as any).electronAPI.keepAliveAIIDEBackend();
      } catch (error) {
        logger.warn('AI-IDE keepalive failed:', error);
      }
    };

    void keepAlive();
    const timer = window.setInterval(() => {
      void keepAlive();
    }, 30000);

    return () => {
      window.clearInterval(timer);
    };
  }, []);

  // 🔐 认证守卫：初始化加载中（添加超时保护）
  React.useEffect(() => {
    // 5秒后如果还在加载，强制结束加载状态
    const timeout = setTimeout(() => {
      if (isLoading) {
        console.warn('认证初始化超时，强制结束加载状态');
      }
    }, 5000);

    return () => clearTimeout(timeout);
  }, [isLoading]);

  // 页面可见后统一静默刷新，避免模块各自抢占式轮询
  React.useEffect(() => {
    const handleVisibilityChange = () => {
      if (!document.hidden) {
        refreshOrchestrator.requestAll('visibility-visible', true);
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, []);

  // 回到主仪表盘时刷新一次
  React.useEffect(() => {
    if (location.pathname === '/') {
      refreshOrchestrator.requestAll('dashboard-enter', true);
    }
  }, [location.pathname]);

  // 低频兜底刷新：仅在已登录且非公开页生效
  React.useEffect(() => {
    if (!isAuthenticated || isPublicRoute) {
      return;
    }

    const timer = window.setInterval(() => {
      refreshOrchestrator.requestAll('fallback-poll', true);
    }, 120000);

    return () => {
      window.clearInterval(timer);
    };
  }, [isAuthenticated, isPublicRoute]);

  const hasLocalToken = !!authService.getAccessToken();

  if (!serverConfigReady) {
    if (!hasLocalToken || isPublicRoute) {
      return (
        <div
          style={{
            minHeight: '100vh',
            background:
              'linear-gradient(135deg, #667eea 0%, #764ba2 25%, #f093fb 50%, #f5576c 75%, #4facfe 100%)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <div style={{ textAlign: 'center', color: 'white' }}>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
              <div style={{ fontSize: 18, fontWeight: 700, color: 'white', letterSpacing: '-0.02em' }}>
                QuantMind
              </div>
              <div style={{ fontSize: 12, color: 'rgba(255,255,255,0.7)' }}>
                正在恢复服务器配置
              </div>
            </div>
            <div style={{ height: 20 }} />
            <Spin size="large" />
          </div>
        </div>
      );
    }
    return <DashboardSkeleton />;
  }

  if (isLoading) {
    if (!hasLocalToken || isPublicRoute) {
      return (
        <div
          style={{
            minHeight: '100vh',
            background:
              'linear-gradient(135deg, #667eea 0%, #764ba2 25%, #f093fb 50%, #f5576c 75%, #4facfe 100%)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <div style={{ textAlign: 'center', color: 'white' }}>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
              <div style={{ fontSize: 18, fontWeight: 700, color: 'white', letterSpacing: '-0.02em' }}>
                QuantMind
              </div>
              <div style={{ fontSize: 12, color: 'rgba(255,255,255,0.7)' }}>
                企业级量化交易平台
              </div>
            </div>
            <div style={{ height: 20 }} />
            <Spin size="large" />
          </div>
        </div>
      );
    }
    return <DashboardSkeleton />;
  }

  // 🔐 认证守卫：未登录且访问受保护路由，重定向到登录页
  if (!isAuthenticated && !isPublicRoute) {
    logger.info('未登录用户访问受保护路由，重定向到登录页');
    return (
      <div className="app-root">
        <Navigate to="/auth/login" state={{ from: location }} replace />
      </div>
    );
  }

  // 🔐 认证守卫：已登录访问登录页，重定向到仪表盘
  if (isAuthenticated && location.pathname === '/auth/login') {
    logger.info('已登录用户访问登录页，重定向到仪表盘');
    return <Navigate to="/" replace />;
  }

  const shouldShowNavigation = isAuthenticated && !isPublicRoute;

  return (
    <RecoilRoot>
      <QueryProvider>
        <WebSocketProvider>
          <ConfigProvider locale={zhCN}>
            <div className="app-root">
            <TitleBar />
            <ErrorBoundary>
              <Suspense
                fallback={<DashboardSkeleton />}
              >
                {/* 使用路由系统，包含认证守卫 */}
                <Routes>
                  {/* 认证相关路由 */}
                  <Route path="/auth/*" element={<AppRoutes />} />

                  {/* 个人中心受保护路由 */}
                  <Route
                    path="/user-center/*"
                    element={
                      <ProtectedRoute>
                        <UserCenterPage />
                      </ProtectedRoute>
                    }
                  />

                  {/* 策略对比受保护路由 */}
                  <Route
                    path="/strategy-comparison"
                    element={
                      <ProtectedRoute>
                        <StrategyComparisonPage />
                      </ProtectedRoute>
                    }
                  />

                  {/* 智能策略向导受保护路由 */}
                  <Route
                    path="/strategy-wizard"
                    element={
                      <ProtectedRoute>
                        <StrategyWizardPage />
                      </ProtectedRoute>
                    }
                  />
                  {/* QuantBot受保护路由 */}
                  <Route
                    path="/quantbot"
                    element={
                      <ProtectedRoute>
                        <QuantBotPage />
                      </ProtectedRoute>
                    }
                  />
                  <Route path="/openclaw" element={<Navigate to="/quantbot" replace />} />
                  <Route
                    path="/ai-ide"
                    element={
                      <ProtectedRoute>
                        <AIIDEPage />
                      </ProtectedRoute>
                    }
                  />
                  <Route
                    path="/model-training"
                    element={
                      <ProtectedRoute>
                        <ModelTrainingPage />
                      </ProtectedRoute>
                    }
                  />
                  <Route
                    path="/model-registry"
                    element={
                      <ProtectedRoute>
                        <ModelRegistryPage />
                      </ProtectedRoute>
                    }
                  />
                  <Route
                    path="/research"
                    element={
                      <ProtectedRoute>
                        <ResearchPlatformPage />
                      </ProtectedRoute>
                    }
                  />
                  <Route
                    path="/trading"
                    element={
                      <ProtectedRoute>
                        <RealTradingPage />
                      </ProtectedRoute>
                    }
                  />
                  <Route
                    path="/admin"
                    element={
                      <ProtectedRoute requiredRole="admin">
                        <AdminPage />
                      </ProtectedRoute>
                    }
                  >
                    <Route index element={<Navigate to="overview" replace />} />
                    <Route path="overview" element={<Suspense fallback={<Spin size="large" />}><AdminDashboard /></Suspense>} />
                    <Route path="users" element={<Suspense fallback={<Spin size="large" />}><AdminUserTable /></Suspense>} />
                    <Route path="models" element={<Suspense fallback={<Spin size="large" />}><AdminModelManagement /></Suspense>} />
                    <Route path="data" element={<Suspense fallback={<Spin size="large" />}><AdminDataManagement /></Suspense>} />
                    <Route path="strategies" element={<Suspense fallback={<Spin size="large" />}><AdminStrategyTemplates /></Suspense>} />
                    {/* 待开发页面占位 */}
                    <Route path="inference" element={<div className="p-8 text-center text-slate-400">推理监控页面开发中...</div>} />
                    <Route path="orders" element={<div className="p-8 text-center text-slate-400">订单管理页面开发中...</div>} />
                    <Route path="risk" element={<div className="p-8 text-center text-slate-400">风险控制页面开发中...</div>} />
                    <Route path="quotes" element={<div className="p-8 text-center text-slate-400">行情源监控页面开发中...</div>} />
                    <Route path="settings" element={<div className="p-8 text-center text-slate-400">系统设置页面开发中...</div>} />
                  </Route>

                  {/* 主应用路由 - 仪表盘等 */}
                  <Route
                    path="/*"
                    element={
                      <motion.div
                        key={tab}
                        initial={false}
                        animate={{ opacity: 1 }}
                        className="w-full h-full"
                      >
                        <DashboardLayout modules={modules} onLayoutChange={handleLayoutChange} />
                      </motion.div>
                    }
                  />
                </Routes>
              </Suspense>

              {/* macOS 风格毛玻璃悬浮导航栏 */}
              {shouldShowNavigation && (
                <FloatingNavBar
                  current={tab}
                  onChange={handleNavChange}
                />
              )}

              {/* 菜单触发的导出模态框 */}
              {shouldShowNavigation && ExportModal}
            </ErrorBoundary>
          </div>
          </ConfigProvider>
        </WebSocketProvider>
      </QueryProvider>
    </RecoilRoot>
  );
}
