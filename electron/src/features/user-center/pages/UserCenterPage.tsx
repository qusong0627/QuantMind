import React, { useEffect, useRef, useState } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { User, UserCircle, Mail, MapPin, Calendar, LogOut, Shield, Laptop, Settings2, Server, Database } from 'lucide-react';
import { Tabs } from 'antd';
import { useAuth } from '../../auth/hooks';
import { useProfile } from '../hooks';
import { useAppDispatch } from '../../../store';
import { logout } from '../../auth/store/authSlice';

// Components
import { ProfileInfo } from '../components/ProfileInfo';
import { SecuritySettings } from '../components/SecuritySettings';
import CloudStrategyManagement from '../components/CloudStrategyManagement';
import CloudNodeSettings from '../components/CloudNodeSettings';
import QuantDBSettings from '../components/QuantDBSettings';
import OtherSettings from '../components/OtherSettings';
import defaultLogo from '../../../assets/logo.png';

const UserCenterPage: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const dispatch = useAppDispatch();
  const [activeTab, setActiveTab] = useState('profile');
  const [syncTimeout, setSyncTimeout] = useState(false);
  const [avatarLoadError, setAvatarLoadError] = useState(false);
  const contentScrollRef = useRef<HTMLDivElement>(null);

  // 支持通过 URL search 参数（如 /user-center?tab=quantdb-key）直接切换 Tab
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const tabParam = params.get('tab');
    if (tabParam) {
      if (tabParam === 'quantdb-key' || tabParam === 'data-platform') {
        setActiveTab('data-platform');
      } else {
        setActiveTab(tabParam);
      }
    }
  }, [location.search]);

  // 获取认证用户信息（已由ProtectedRoute保证认证）
  const { user, isAuthenticated, isLoading: authLoading, isInitialized } = useAuth();
  const isDev = (import.meta as any).env?.MODE === 'development';
  const disabledAuth = isDev || String((import.meta as any).env?.VITE_DISABLE_AUTH || '').toLowerCase() === 'true';

  // 未登录或无用户ID时阻断渲染并跳转登录
  useEffect(() => {
    if (!authLoading && !disabledAuth && (!isAuthenticated || (isInitialized && !user?.id && !localStorage.getItem('access_token')))) {
      console.log('[UserCenter] Auth check failed or token cleared, redirecting to login');
      navigate('/auth/login', { replace: true, state: { from: location.pathname } });
    }
  }, [authLoading, disabledAuth, isAuthenticated, user?.id, isInitialized, navigate, location.pathname]);

  // 同步超时处理：防止由于 API 失败导致 Token 被清除却卡在“同步中”状态
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    // 只有在已认证但没有用户信息时开启计时
    if (isAuthenticated && !user?.id && !authLoading && !disabledAuth) {
      timer = setTimeout(() => {
        console.warn('[UserCenter] Sync profile timeout (10s)');
        setSyncTimeout(true);
      }, 10000);
    }
    return () => {
      if (timer) clearTimeout(timer);
    };
  }, [isAuthenticated, user?.id, authLoading, disabledAuth]);

  // 处理退出登录
  const handleLogout = async () => {
    try {
      await dispatch(logout()).unwrap();
      navigate('/auth/login', { replace: true });
    } catch (error) {
      console.error('退出登录失败:', error);
      // 即使失败也跳转到登录页
      navigate('/auth/login', { replace: true });
    }
  };

  if (authLoading) {
    return (
      <div className="min-h-screen bg-[#f8fafc] p-4 flex items-center justify-center">
        <div className="bg-white rounded-2xl p-12 flex flex-col items-center gap-4 shadow-sm border border-gray-100">
          <LogOut className="w-8 h-8 text-blue-500 animate-pulse opacity-40" />
          <p className="text-sm font-bold text-gray-400 uppercase tracking-widest">Verifying Identity...</p>
        </div>
      </div>
    );
  }

  // 防御性解析本地存储的用户信息
  let storedUser = null;
  try {
    const storedUserStr = localStorage.getItem('user');
    if (storedUserStr) {
      storedUser = JSON.parse(storedUserStr);
    }
  } catch (e) {
    console.error('[UserCenter] Failed to parse stored user:', e);
  }

  const effectiveUser = user || storedUser;

  // 如果已认证但用户信息尚未同步完成，展示加载中
  if (isAuthenticated && !effectiveUser?.id && !disabledAuth) {
    if (syncTimeout) {
      return (
        <div className="min-h-screen bg-[#f8fafc] p-4 flex items-center justify-center">
          <div className="bg-white rounded-2xl p-12 flex flex-col items-center gap-6 shadow-md border border-gray-100 max-w-sm text-center">
            <div className="w-16 h-16 bg-red-50 rounded-full flex items-center justify-center">
              <LogOut className="w-8 h-8 text-red-500" />
            </div>
            <div>
              <h3 className="text-lg font-bold text-gray-800 mb-2">同步个人资料超时</h3>
              <p className="text-sm text-gray-500">无法从服务器获取您的身份信息，可能是网络连接问题或登录已失效。</p>
            </div>
            <div className="flex flex-col w-full gap-3">
              <button
                onClick={() => window.location.reload()}
                className="w-full py-3 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl font-bold transition-all shadow-lg shadow-indigo-200"
              >
                重试同步
              </button>
              <button
                onClick={handleLogout}
                className="w-full py-3 bg-gray-100 hover:bg-gray-200 text-gray-600 rounded-xl font-bold transition-all"
              >
                重新登录
              </button>
            </div>
          </div>
        </div>
      );
    }

    return (
      <div className="min-h-screen bg-[#f8fafc] p-4 flex items-center justify-center">
        <div className="bg-white rounded-2xl p-12 flex flex-col items-center gap-4 shadow-sm border border-gray-100">
          <Settings2 className="w-8 h-8 text-indigo-500 animate-spin opacity-40" />
          <p className="text-sm font-bold text-gray-400 uppercase tracking-widest">Syncing Profile...</p>
        </div>
      </div>
    );
  }

  if (!disabledAuth && (!isAuthenticated || !effectiveUser?.id)) {
    return null;
  }

  const userId = String(effectiveUser?.id || '1');
  const tenantId = String((effectiveUser as any)?.tenant_id || (import.meta as any).env?.VITE_TENANT_ID || 'default');

  // 获取用户档案数据
  const { profile, isLoading: profileLoading, lastFetchedAt } = useProfile(userId);

  const lastFetchedAtLabel = lastFetchedAt ? new Date(lastFetchedAt).toLocaleString() : '';
  const handleTabChange = (key: string) => {
    setActiveTab(key);
    requestAnimationFrame(() => {
      contentScrollRef.current?.scrollTo({ top: 0, behavior: 'auto' });
    });
  };
  const wrapTabContent = (content: React.ReactNode) => (
    <div className="w-full min-h-[420px]">
      {content}
    </div>
  );

  const tabItems = [
    {
      key: 'profile',
      label: (
        <span className="flex items-center gap-2">
          <UserCircle className="w-4 h-4" />
          个人档案
        </span>
      ),
      children: wrapTabContent(<ProfileInfo userId={userId} />),
    },
    {
      key: 'security',
      label: (
        <span className="flex items-center gap-2">
          <Shield className="w-4 h-4" />
          安全设置
        </span>
      ),
      children: wrapTabContent(<SecuritySettings userId={userId} />),
    },
    {
      key: 'strategies',
      label: (
        <span className="flex items-center gap-2">
          <Laptop className="w-4 h-4" />
          我的策略
        </span>
      ),
      children: wrapTabContent(<CloudStrategyManagement />),
    },
    ...(effectiveUser?.is_admin
      ? [
          {
            key: 'cloud-nodes',
            label: (
              <span className="flex items-center gap-2">
                <Server className="w-4 h-4" />
                云端节点
              </span>
            ),
            children: wrapTabContent(<CloudNodeSettings />),
          },
          {
            key: 'data-platform',
            label: (
              <span className="flex items-center gap-2">
                <Database className="w-4 h-4" />
                数据平台
              </span>
            ),
            children: wrapTabContent(<QuantDBSettings />),
          },
        ]
      : []),
    {
      key: 'other-settings',
      label: (
        <span className="flex items-center gap-2">
          <Settings2 className="w-4 h-4" />
          其他设置
        </span>
      ),
      children: wrapTabContent(<OtherSettings userId={userId} tenantId={tenantId} />),
    },
  ];

  // 后台管理内嵌模式（/admin/profile）：外壳（侧边菜单/头部）由后台 shell 提供，
  // 本页只渲染内容面——去掉 32px 大卡片外壳与页面内边距；独立路由 /user-center 保持原样
  const embeddedInAdmin = location.pathname.startsWith('/admin/profile');

  return (
    <div className={embeddedInAdmin ? 'w-full h-full overflow-hidden' : 'w-full h-full bg-[#f8fafc] p-6 overflow-hidden'}>
      <div
        className={`bg-white flex flex-col w-full h-full overflow-hidden ${
          embeddedInAdmin
            ? 'border border-slate-200/80 shadow-sm rounded-2xl'
            : 'border border-gray-200 shadow-sm rounded-[32px]'
        }`}
      >
        {/* 顶部标题栏 - 对齐回测中心 */}
        <div className="flex-shrink-0 bg-white border-b border-gray-200 px-6 flex items-center justify-between z-10"
          style={{ height: '60px' }}
        >
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center shadow-md">
              <User className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="text-lg font-bold text-gray-800 leading-tight">用户个人中心</h1>
              <p className="text-[10px] text-gray-400 uppercase tracking-wider font-semibold">User Dashboard & Settings</p>
            </div>
          </div>

          <div className="flex items-center gap-4">
            <div className="text-xs text-gray-400 font-medium italic">
              数据更新: {lastFetchedAtLabel || '刚刚'}
            </div>
            <div className="h-4 w-[1px] bg-gray-200 mx-1" />
            <button
              onClick={handleLogout}
              className="p-2 hover:bg-red-50 text-slate-500 hover:text-red-600 rounded-xl transition-colors group"
              title="退出登录"
            >
              <LogOut className="w-4 h-4 group-hover:scale-110 transition-transform" />
            </button>
          </div>
        </div>

        {/* 主内容区域 - 计算高度：100% - 顶部标题栏60px */}
        <div
          ref={contentScrollRef}
          className="overflow-y-auto custom-scrollbar bg-slate-50/50"
          style={{ height: 'calc(100% - 60px)', scrollbarGutter: 'stable' }}
        >
          <div className="max-w-[1320px] mx-auto px-6 py-5" style={{ paddingBottom: '20px' }}>
            {/* 用户概览卡片 */}
            <div className="bg-white rounded-[32px] border border-slate-100 p-6 mb-6">
              {profileLoading ? (
                <div className="flex flex-col items-center gap-2 py-6">
                  <LogOut className="w-6 h-6 animate-pulse text-blue-500 opacity-20" />
                  <span className="text-xs text-gray-400 font-bold uppercase tracking-widest">Loading...</span>
                </div>
              ) : (
                <div className="flex items-center gap-10">
                  <div className="relative group">
                    <div className="w-24 h-24 rounded-3xl bg-white border-4 border-white shadow-xl flex items-center justify-center overflow-hidden">
                      <img
                        src={profile?.avatar && !profile.avatar.includes('default_avatar.png') && !avatarLoadError ? profile.avatar : defaultLogo}
                        alt="Avatar"
                        className="w-full h-full object-cover"
                        onError={() => setAvatarLoadError(true)}
                      />
                    </div>
                    <div className="absolute -bottom-1 -right-1 w-6 h-6 bg-emerald-500 border-2 border-white rounded-full shadow-sm" />
                  </div>

                  <div className="flex-1">
                    <div className="flex items-center gap-3 mb-2">
                      <h2 className="text-2xl font-black text-slate-800 tracking-tight">
                        {profile?.username || user?.username || '量化交易者'}
                      </h2>
                      <span className={`px-2 py-0.5 rounded-lg text-[10px] font-black uppercase tracking-widest ${profile?.trading_experience === 'advanced' ? 'bg-amber-100 text-amber-600' : 'bg-blue-100 text-blue-600'
                        }`}>
                        {profile?.trading_experience === 'beginner'
                          ? '初级交易员'
                          : profile?.trading_experience === 'intermediate'
                            ? '中级交易员'
                            : '高级交易员'}
                      </span>
                    </div>

                    <div className="flex items-center gap-6 text-xs font-bold text-slate-400">
                      <span className="flex items-center gap-1.5"><Mail className="w-3.5 h-3.5" /> {profile?.email || user?.email || 'trader@example.com'}</span>
                      <span className="flex items-center gap-1.5"><MapPin className="w-3.5 h-3.5" /> {profile?.location || '中国'}</span>
                      <span className="flex items-center gap-1.5"><Calendar className="w-3.5 h-3.5" /> {profile?.created_at ? new Date(profile.created_at).getFullYear() + '年加入' : '2026年加入'}</span>
                    </div>
                  </div>
                </div>
              )}
            </div>

            {/* 内容选项卡 */}
            <div className="bg-white rounded-[32px] border border-slate-100 overflow-hidden min-h-[500px]">
              <div className="px-6 pt-2">
                <Tabs
                  activeKey={activeTab}
                  onChange={handleTabChange}
                  items={tabItems}
                  size="large"
                  animated={false}
                  tabBarStyle={{ marginBottom: 24 }}
                />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default UserCenterPage;
