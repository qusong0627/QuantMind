import React, { useState } from 'react';
import { Layout, Menu, Button, Badge, Avatar, Typography, Divider, Tag } from 'antd';
import { 
    DashboardOutlined, 
    UserOutlined, 
    RocketOutlined, 
    SettingOutlined,
    ThunderboltOutlined,
    ApiOutlined,
    SwapOutlined,
    GlobalOutlined,
    BellOutlined
} from '@ant-design/icons';
import { useNavigate, useLocation, Outlet } from 'react-router-dom';
import { AdminSystemLoadWidget } from './components/AdminSystemLoadWidget';

const { Title, Text } = Typography;

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
                { key: 'data', label: '数据集目录' },
                { key: 'qlib', label: 'Qlib 数据管理' },
                { key: 'quotes', label: '数据源监控' },
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
                { key: 'feature-catalog', label: '特征字典' },
                { key: 'rd-agent', label: 'AlphaAgent因子' },
                { key: 'inference', label: '推理监控（开发中）' },
            ]
        },
        {
            key: 'training-service',
            icon: <RocketOutlined />,
            label: '训练服务',
            children: [
                { key: 'autodl-nodes', label: 'AutoDL 节点' },
                { key: 'training-datasets', label: '模型训练数据集' },
            ]
        },
        { type: 'divider' as const },
        { 
            key: 'trade-service', 
            icon: <SwapOutlined />, 
            label: '交易核心', 
            children: [
                { key: 'orders', label: '订单管理（开发中）' },
                { key: 'risk', label: '风险控制（开发中）' },
            ]
        },
        { key: 'settings', icon: <SettingOutlined />, label: '系统设置（开发中）' },
    ];

    const currentKey = location.pathname.split('/').pop() || 'overview';

    return (
        <div className="admin-page flex h-screen w-full bg-slate-50 overflow-hidden font-sans">
            {/* Sidebar */}
            <div className={`flex flex-col h-full bg-white border-r border-slate-200 transition-all duration-300 ${collapsed ? 'w-20' : 'w-64'}`}>
                <div className="p-6 flex items-center gap-3">
                    <div className="w-9 h-9 bg-slate-900 rounded-lg flex items-center justify-center shrink-0 shadow-sm">
                        <RocketOutlined className="text-white text-lg" />
                    </div>
                    {!collapsed && (
                        <div className="min-w-0">
                            <Title level={5} className="!m-0 !font-black !tracking-tight !text-slate-800 uppercase text-sm truncate">QuantMind</Title>
                            <Text className="text-slate-400 text-[10px] font-bold tracking-widest uppercase">管理后台</Text>
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
                        <Badge dot color="#10b981" offset={[-2, 2]}>
                            <Button type="text" icon={<BellOutlined />} className="text-slate-400 hover:text-slate-800" />
                        </Badge>
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
                <main className="flex-1 overflow-y-auto px-6 pt-6 pb-[60px] bg-slate-50/50">
                    {/* 资讯监控 / RD 因子挖掘等大屏页面用全宽，其余保留 1400px 阅读宽度 */}
                    <div
                        className={
                            ['news', 'rd-agent', 'inference', 'tags'].includes(currentKey)
                                ? 'w-full animate-in fade-in slide-in-from-bottom-4 duration-500'
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
