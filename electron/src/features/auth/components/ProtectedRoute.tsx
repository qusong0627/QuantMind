/**
 * 受保护的路由组件
 */

import React, { ReactNode } from 'react';
import { useNavigate, useLocation, Navigate } from 'react-router-dom';
import { Spin, Result, Button } from 'antd';
import { LockOutlined, CloudServerOutlined } from '@ant-design/icons';
import { useRequireAuth, useRequireRole } from '../hooks/useAuth';
import { useAppDispatch, useAppSelector } from '../../../store';
import { logout, retryAuthInit } from '../store/authSlice';
import { getDynamicServerUrl, initDynamicServerUrl } from '../../../config/services';

interface ProtectedRouteProps {
  children: ReactNode;
  requiredRole?: string;
  fallback?: ReactNode;
  redirectTo?: string;
}

const ProtectedRoute: React.FC<ProtectedRouteProps> = ({
  children,
  requiredRole,
  fallback,
  redirectTo = '/auth/login',
}) => {
  const location = useLocation();
  const navigate = useNavigate();
  const dispatch = useAppDispatch();
  const { isAuthenticated, isLoading, user } = useRequireAuth();
  const { hasRole } = useRequireRole(requiredRole);
  const serverUnreachable = useAppSelector((state) => state.auth.serverUnreachable);

  // 重试连接：先重新探测服务器地址（换 IP 后旧地址会失效），再重置认证状态重试
  const handleRetry = async () => {
    await initDynamicServerUrl();
    dispatch(retryAuthInit());
  };

  // 认证唯一依据是 Redux 已校验状态。localStorage 里有旧 token 不代表已登录——
  // 服务器不可达/初始化失败时不得放行，避免后端关闭后“登录成功看缓存数据”。
  const effectiveAuth = isAuthenticated;

  // 加载中
  if (isLoading) {
    return (
      <div
        style={{
          height: '100%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#f0f2f5',
        }}
      >
        <Spin size="large" tip="验证身份中...">
          <div style={{ height: 100 }} />
        </Spin>
      </div>
    );
  }

  // 服务器不可达：整页提示 + 重试，不进入缓存数据
  if (serverUnreachable && !effectiveAuth) {
    const serverUrl = getDynamicServerUrl() || '未配置';
    return (
      <div
        style={{
          height: '100vh',
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'var(--bg-main, #f8fafc)',
          padding: '24px',
          position: 'fixed',
          top: 0,
          left: 0,
          zIndex: 1000,
        }}
      >
        <Result
          icon={<CloudServerOutlined style={{ fontSize: 64, color: '#faad14' }} />}
          title="服务器不可达"
          subTitle={
            <span>
              无法连接到后端服务器（{serverUrl}），请确认服务器已启动或网络正常。
              <br />
              已保留你的登录令牌与服务器配置，恢复后可直接重试，无需重新配置。
            </span>
          }
          extra={
            <div style={{ display: 'flex', gap: 12, justifyContent: 'center' }}>
              <Button
                type="primary"
                size="large"
                onClick={() => void handleRetry()}
              >
                重试连接
              </Button>
              <Button
                size="large"
                onClick={() => navigate('/auth/login', { replace: true })}
              >
                返回登录页
              </Button>
            </div>
          }
          style={{
            background: 'white',
            padding: '72px 48px',
            borderRadius: '48px',
            boxShadow: '0 25px 60px -15px rgba(15, 23, 42, 0.12)',
            maxWidth: '640px',
            width: '100%',
          }}
        />
      </div>
    );
  }

  // ✅ 未登录且无Token，显示自定义fallback或跳转到登录页
  if (!effectiveAuth) {
    if (fallback) {
      return <>{fallback}</>;
    }

    return (
      <Navigate
        to={redirectTo}
        state={{ from: location }}
        replace
      />
    );
  }

  // 角色权限检查
  if (requiredRole && !hasRole) {
    return (
      <div
        style={{
          height: '100vh',
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'var(--bg-main, #f8fafc)',
          padding: '24px',
          position: 'fixed',
          top: 0,
          left: 0,
          zIndex: 1000,
        }}
      >
        <Result
          status="403"
          icon={
            <div className="flex justify-center mb-0">
              <div 
                className="w-56 h-56 rounded-full flex items-center justify-center relative"
                style={{ 
                  background: 'rgba(99, 102, 241, 0.03)',
                }}
              >
                <div 
                  className="absolute inset-0 rounded-full animate-pulse"
                  style={{ background: 'radial-gradient(circle, rgba(99, 102, 241, 0.08) 0%, transparent 70%)' }}
                ></div>
                <div 
                  className="w-20 h-20 bg-gradient-to-br from-indigo-500 to-purple-600 rounded-3xl flex items-center justify-center shadow-2xl transform shadow-indigo-200/50 z-10"
                >
                  <LockOutlined style={{ fontSize: 36, color: 'white' }} />
                </div>
              </div>
            </div>
          }
          title={
            <h1 className="text-3xl font-black text-slate-800 tracking-tight m-0">
              访问被拒绝
            </h1>
          }
          subTitle={
            <p className="text-slate-500 text-base mt-4 max-w-sm mx-auto leading-relaxed">
              您当前没有权限访问管理控制台。<br />
              请确保您已获得管理员授权后重试。
            </p>
          }
          extra={
            <div className="flex gap-4 justify-center items-center mt-10">
              <Button 
                size="large"
                className="h-12 rounded-xl px-10 border-slate-200 text-slate-600 font-bold hover:bg-slate-50 hover:text-indigo-600 transition-all"
                onClick={() => window.history.back()}
              >
                返回上一页
              </Button>
              <Button
                type="primary"
                danger
                size="large"
                className="h-12 rounded-xl px-10 font-bold shadow-lg shadow-red-200/50 hover:scale-105 active:scale-95 transition-all"
                onClick={() => {
                  dispatch(logout());
                  navigate('/auth/login', { replace: true });
                }}
              >
                退出并重新登录
              </Button>
            </div>
          }
          style={{
            background: 'white',
            padding: '72px 48px',
            borderRadius: '48px',
            boxShadow: '0 25px 60px -15px rgba(15, 23, 42, 0.12)',
            maxWidth: '640px',
            width: '100%',
          }}
        />
      </div>
    );
  }

  // 通过所有检查，渲染子组件
  return <>{children}</>;
};

export default ProtectedRoute;
