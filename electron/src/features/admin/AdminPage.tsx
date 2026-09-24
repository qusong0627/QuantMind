import React, { useEffect, useState } from 'react';
import { Menu, Avatar, Typography, Divider, Tag, Tooltip } from 'antd';
import { 
    DashboardOutlined, 
    UserOutlined, 
    RocketOutlined, 
    SettingOutlined,
    ThunderboltOutlined,
    ApiOutlined,
    SwapOutlined,
    GlobalOutlined,
    QuestionCircleOutlined,
} from '@ant-design/icons';
import { useNavigate, useLocation, Outlet } from 'react-router-dom';
import { AdminSystemLoadWidget } from './components/AdminSystemLoadWidget';
import { systemService, type SystemVersion } from '../../services/systemService';

const { Title, Text } = Typography;

const FRONTEND_VERSION =
  typeof __APP_VERSION__ !== 'undefined' && __APP_VERSION__ ? __APP_VERSION__ : 'dev';

/** 管理后台版本落后提示：进入页面立即检查，之后每 10 分钟自动检查一次。 */
const UPDATE_CHECK_INTERVAL_MS = 10 * 60 * 1000;

const AdminUpdateBadge: React.FC = () => {
    const [versionInfo, setVersionInfo] = useState<SystemVersion | null>(null);

    useEffect(() => {
        let cancelled = false;
        const run = () => {
            systemService
                .getVersion(true)
                .then((info) => {
                    if (!cancelled) setVersionInfo(info);
                })
                .catch(() => {
                    if (!cancelled) setVersionInfo(null);
                });
        };
        run();
        const timer = window.setInterval(run, UPDATE_CHECK_INTERVAL_MS);
        return () => {
            cancelled = true;
            window.clearInterval(timer);
        };
    }, []);

    const update = versionInfo?.update;
    if (!update) return null;
    if (update.status === 'diverged') {
        return (
            <Tooltip title="本地提交不在上游 master 历史中，无法计算落后个数">
                <span className="text-[10px] font-medium text-slate-400">无法与上游对齐</span>
            </Tooltip>
        );
    }
    if (update.behind != null && update.behind > 0) {
        return (
            <Tooltip
                title={
                    <div className="text-xs leading-relaxed">
                        概览页可点击「更新系统」一键执行；或用 SSH 工具登录服务器执行
                        <div className="mt-1 rounded bg-white/15 px-1.5 py-0.5 font-mono break-all">
                            cd /opt/quantmind && sudo bash deploy/update.sh --force
                        </div>
                    </div>
                }
            >
                <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200 px-3 py-1 text-xs font-medium">
                    <span className="w-2 h-2 rounded-full bg-amber-500 animate-pulse" />
                    {`落后 ${update.behind} 个提交`}
                </span>
            </Tooltip>
        );
    }
    return null;
};

const AdminPage: React.FC = () => {
    const navigate = useNavigate();
    const location = useLocation();
    const [collapsed, setCollapsed] = useState(false);

    const menuItems = [
        { 
            key: 'overview', 
            icon: <DashboardOutlined />, 
            label: '系统概览' 
        },
        { type: 'divider' as const },
        {
            key: 'stream-service',
            icon: <GlobalOutlined />,
            label: '数据管理',
            children: [
                { key: 'data', label: '数据管理' },
                { key: 'qlib', label: 'Qlib 引擎' },
                { key: 'stock-pools', label: '全局股票池' },
                { key: 'news', label: '新闻情感' },
            ]
        },
        { 
            key: 'api-service', 
            icon: <ApiOutlined />, 
            label: 'API 服务',
            children: [
                { key: 'users', label: '用户管理' },
                { key: 'strategies', label: '策略仓库' },
            ]
        },
        {
            key: 'engine-service',
            icon: <ThunderboltOutlined />,
            label: '推理引擎',
            children: [
                { key: 'models', label: '模型管理' },
                { key: 'inference', label: '推理监控' },
            ]
        },
        {
            key: 'training-service',
            icon: <RocketOutlined />,
            label: '训练服务',
            children: [
                { key: 'feature-catalog', label: '特征字典' },
                { key: 'training-datasets', label: '数据发布' },
                { key: 'autodl-nodes', label: 'AutoDL 节点' },
            ]
        },
        { type: 'divider' as const },
        { 
            key: 'trade-service', 
            icon: <SwapOutlined />, 
            label: '交易核心', 
            children: [
                { key: 'orders', label: '订单管理' },
                { key: 'risk', label: '风险控制' },
            ]
        },
        { key: 'settings', icon: <SettingOutlined />, label: '系统设置' },
        { key: 'help', icon: <QuestionCircleOutlined />, label: '帮助中心' },
    ];

    const currentKey = location.pathname.split('/').pop() || 'overview';

    return (
        <div className="admin-page flex h-screen w-full bg-slate-50 overflow-hidden font-sans">
            {/* Sidebar */}
            <div className={`flex flex-col h-full bg-white border-r border-slate-200 transition-all duration-300 ${collapsed ? 'w-20' : 'w-64'}`}>
                <div className="p-6 flex items-center gap-3">
                    <Tooltip
                        title={`前端版本 v${FRONTEND_VERSION}（electron/package.json）`}
                        placement="right"
                    >
                        <div className="w-9 h-9 bg-slate-900 rounded-lg flex items-center justify-center shrink-0 shadow-sm cursor-default">
                            <RocketOutlined className="text-white text-lg" />
                        </div>
                    </Tooltip>
                    {!collapsed && (
                        <div className="min-w-0">
                            <Title level={5} className="!m-0 !font-black !tracking-tight !text-slate-800 uppercase text-sm truncate">QuantMind</Title>
                            <div className="flex items-center gap-1.5 mt-0.5">
                                <Text className="text-slate-400 text-[10px] font-bold tracking-widest uppercase">管理后台</Text>
                                <Tooltip title={`当前前端构建版本，来自 electron/package.json`}>
                                    <Tag className="!m-0 !px-1.5 !py-0 !text-[10px] !leading-4 !border-slate-200 !bg-slate-50 !text-slate-500 !rounded font-mono cursor-default">
                                        v{FRONTEND_VERSION}
                                    </Tag>
                                </Tooltip>
                            </div>
                        </div>
                    )}
                </div>

                <div className="flex-1 px-3 py-2 overflow-y-auto custom-scrollbar">
                    <Menu
                        mode="inline"
                        selectedKeys={[currentKey]}
                        onClick={({ key }) => navigate(`/admin/${key}`)}
                        className="border-none admin-menu-modern"
                        items={menuItems}
                        inlineCollapsed={collapsed}
                    />
                </div>

                {/* 侧边栏左下角：真实系统负载监控卡片 */}
                <AdminSystemLoadWidget collapsed={collapsed} />
            </div>

            {/* Main Content Area */}
            <div className="flex-1 flex flex-col h-full overflow-hidden relative">
                {/* HeaderBar */}
                <header className="h-16 bg-white border-b border-slate-200 px-8 flex items-center justify-between shrink-0">
                    <div className="flex items-center gap-6">
                        <div className="flex items-center gap-2">
                            <Tag color="success" className="m-0 border-none rounded-full px-3 text-[10px] font-black uppercase bg-emerald-50 text-emerald-600">基础设施正常</Tag>
                        </div>
                    </div>
                    
                    <div className="flex items-center gap-5">
                        <AdminUpdateBadge />
                        <Divider type="vertical" className="h-4 border-slate-200" />
                        <div className="flex items-center gap-3 pl-2">
                            <div className="text-right hidden sm:block">
                                <div className="text-[9px] font-black text-slate-400 uppercase tracking-widest leading-none mb-0.5">超级用户</div>
                                <div className="text-xs font-bold text-slate-800">管理员</div>
                            </div>
                            <Avatar shape="circle" className="bg-slate-100 text-slate-400 border border-slate-200" icon={<UserOutlined />} />
                        </div>
                    </div>
                </header>

                {/* Content Container */}
                <main
                    className={`flex-1 px-6 pt-6 pb-[60px] bg-slate-50/50 ${
                        ['orders', 'risk', 'inference'].includes(currentKey)
                            ? 'overflow-hidden flex flex-col min-h-0'
                            : 'overflow-y-auto'
                    }`}
                >
                    {/* 资讯监控 / 订单 / 风控等大屏页面用全宽，其余保留 1400px 阅读宽度 */}
                    <div
                        className={
                            ['news', 'inference', 'tags', 'settings', 'help', 'stock-pools', 'orders', 'risk'].includes(currentKey)
                                ? `min-h-0 animate-in fade-in slide-in-from-bottom-4 duration-500 ${
                                      ['orders', 'risk'].includes(currentKey)
                                          ? 'flex w-full flex-1 flex-col'
                                          : currentKey === 'inference'
                                            ? 'mx-auto flex w-full max-w-[1400px] flex-1 flex-col'
                                            : 'h-full w-full'
                                  }`
                                : 'max-w-[1400px] mx-auto animate-in fade-in slide-in-from-bottom-4 duration-500'
                        }
                    >
                        <Outlet />
                    </div>
                </main>
            </div>
        </div>
    );
};

export default AdminPage;
