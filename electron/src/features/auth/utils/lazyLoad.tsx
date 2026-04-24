/**
 * 认证模块懒加载工具
 * 用于代码分割和性能优化
 */

import { lazy, ComponentType, Suspense } from 'react';
import { Spin } from 'antd';

const LoadingSpinner = () => (
  <div style={{ display: 'flex', justifyContent: 'center', padding: '50px' }}>
    <Spin size="large" />
  </div>
);

/**
 * 懒加载组件包装器
 */
export const lazyLoad = <T extends ComponentType<any>>(
  importFunc: () => Promise<{ default: T }>,
  fallback?: React.ReactNode
): React.ComponentType<any> => {
  const LazyComponent = lazy(importFunc);

  const WrappedComponent = (props: any) => (
    <Suspense fallback={fallback || <LoadingSpinner />}>
      <LazyComponent {...props} />
    </Suspense>
  );

  return WrappedComponent;
};

/**
 * 认证页面懒加载配置
 */
export const LazyLoginPage = lazyLoad(() => import('../components/LoginPage'));
export const LazyRegisterPage = lazyLoad(() => import('../components/RegisterPage'));
export const LazyForgotPasswordPage = lazyLoad(() => import('../components/ForgotPasswordPage'));
export const LazyResetPasswordPage = lazyLoad(() => import('../components/ResetPasswordPage'));
export const LazyProtectedRoute = lazyLoad(() => import('../components/ProtectedRoute'));

/**
 * 预加载函数
 */
export const preloadAuthPages = async () => {
  try {
    // 并行预加载所有认证页面
    await Promise.all([
      import('../components/LoginPage'),
      import('../components/RegisterPage'),
      import('../components/ForgotPasswordPage'),
      import('../components/ResetPasswordPage'),
      import('../components/ProtectedRoute'),
    ]);

    console.log('✅ 认证页面预加载完成');
  } catch (error) {
    console.warn('⚠️ 认证页面预加载失败:', error);
  }
};

let aiIdePreloadPromise: Promise<void> | null = null;

const warmMonacoStaticAssets = () => {
  if (typeof window === 'undefined' || typeof fetch !== 'function') return;
  try {
    const baseUrl = new URL('monaco/vs/', window.location.href);
    const warmPaths = [
      'loader.js',
      'editor/editor.main.js',
      'editor/editor.main.css',
    ];
    warmPaths.forEach((p) => {
      const u = new URL(p, baseUrl).toString();
      void fetch(u, { cache: 'force-cache' }).catch(() => undefined);
    });
  } catch {
    // ignore
  }
};

/**
 * 预加载 AI-IDE 关键资源（开发/生产统一生效）
 * - 预取 AI-IDE 页面 chunk
 * - 预热 Electron 主进程 AI-IDE IPC（若可用）
 */
export const preloadAiIdeResources = async () => {
  if (aiIdePreloadPromise) {
    return aiIdePreloadPromise;
  }

  aiIdePreloadPromise = (async () => {
    try {
      // Prism 语言包依赖全局 Prism；必须先加载 core 并挂到全局，避免 "Prism is not defined"。
      const prismCore = await import('prismjs');
      const prismInstance = (prismCore as any).default || prismCore;
      if (typeof globalThis !== 'undefined' && !(globalThis as any).Prism) {
        (globalThis as any).Prism = prismInstance;
      }
      if (typeof window !== 'undefined' && !(window as any).Prism) {
        try {
          (window as any).Prism = prismInstance;
        } catch {
          // window.Prism 可能已被设为只读，跳过
        }
      }

      await Promise.all([
        import('../../../pages/AIIDEPage'),
        import('@monaco-editor/react'),
        import('@monaco-editor/loader').then((mod) => mod.default.init()).catch(() => undefined),
        import('prismjs/components/prism-python'),
        import('prismjs/components/prism-bash'),
        import('prismjs/components/prism-json'),
      ]);

      warmMonacoStaticAssets();

      // 非阻塞预热：仅在 Electron 环境下调用
      if (typeof window !== 'undefined' && window.electronAPI) {
        void window.electronAPI.ensureDefaultAIIDEWorkspace?.().catch(() => undefined);
        void window.electronAPI.getAIIDERuntimeStatus?.().catch(() => undefined);
      }

      console.log('✅ AI-IDE 资源预加载完成');
    } catch (error) {
      console.warn('⚠️ AI-IDE 资源预加载失败:', error);
      // 允许后续重试
      aiIdePreloadPromise = null;
    }
  })();

  return aiIdePreloadPromise;
};

export default {
  lazyLoad,
  LazyLoginPage,
  LazyRegisterPage,
  LazyForgotPasswordPage,
  LazyResetPasswordPage,
  LazyProtectedRoute,
  preloadAuthPages,
  preloadAiIdeResources,
};
