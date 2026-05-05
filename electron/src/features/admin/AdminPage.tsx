import React, { useState } from 'react';
import { Layout, Menu, Button, Badge, Avatar, Typography, Divider, Tag } from 'antd';
import { 
    DashboardOutlined, 
    UserOutlined, 
    RobotOutlined, 
    DatabaseOutlined, 
    FileTextOutlined,
    ArrowLeftOutlined,
    RocketOutlined,
    SafetyCertificateOutlined,
    BellOutlined,
    SettingOutlined,
    ThunderboltOutlined,
    ApiOutlined,
    SwapOutlined,
    GlobalOutlined
} from '@ant-design/icons';
import { useNavigate, useLocation, Outlet } from 'react-router-dom';

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
            key: 'trade-service', 
            icon: <SwapOutlined />, 
            label: '交易核心',
            children: [
                { key: 'orders', label: '订单管理' },
                { key: 'risk', label: '风险控制' },
            ]
        },
        { 
            key: 'stream-service', 
            icon: <GlobalOutlined />, 
            label: '实时数据流',
            children: [
                { key: 'data', label: '数据管理' },
                { key: 'quotes', label: '行情源监控' },
            ]
        },
        { type: 'divider' as const },
        { key: 'settings', icon: <SettingOutlined />, label: '系统设置' },
    ];

    const currentKey = location.pathname.split('/').pop() || 'overview';

    return (
        <div className="flex h-screen w-full bg-slate-50 overflow-hidden font-sans">
            {/* Sidebar */}
            <div className={`flex flex-col h-full bg-white border-r border-slate-200 transition-all duration-300 ${collapsed ? 'w-20' : 'w-64'}`}>
                <div className="p-6 flex items-center gap-3">
                    <div className="w-9 h-9 bg-slate-900 rounded-lg flex items-center justify-center shrink-0 shadow-sm">
                        <RocketOutlined className="text-white text-lg" />
                    </div>
                    {!collapsed && (
                        <div className="min-w-0">
                            <Title level={5} className="!m-0 !font-black !tracking-tight !text-slate-800 uppercase text-sm truncate">QuantMind</Title>
                            <Text className="text-slate-400 text-[10px] font-bold tracking-widest uppercase">Admin</Text>
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

                <div className="p-4 border-t border-slate-100">
                    <div className="bg-slate-50 rounded-xl p-4">
                        <div className="flex items-center justify-between mb-2">
                            <Text className="text-[10px] font-black text-slate-400 uppercase tracking-wider">System Score</Text>
                            <Text className="text-[10px] font-black text-emerald-500">94%</Text>
                        </div>
                        <div className="h-1 w-full bg-slate-200 rounded-full overflow-hidden">
                            <div className="h-full bg-emerald-500 w-[94%]" />
                        </div>
                    </div>
                </div>
            </div>

            {/* Main Content Area */}
            <div className="flex-1 flex flex-col h-full overflow-hidden relative">
                {/* HeaderBar */}
                <header className="h-16 bg-white border-b border-slate-200 px-8 flex items-center justify-between shrink-0">
                    <div className="flex items-center gap-6">
                        <Button 
                            type="text"
                            icon={<ArrowLeftOutlined />} 
                            onClick={() => navigate('/dashboard')}
                            className="text-slate-500 font-bold text-xs flex items-center hover:bg-slate-50 rounded-lg h-9 px-3"
                        >
                            RETURN TO PLATFORM
                        </Button>
                        <Divider type="vertical" className="h-4 border-slate-200" />
                        <div className="flex items-center gap-2">
                            <Tag color="success" className="m-0 border-none rounded-full px-3 text-[10px] font-black uppercase bg-emerald-50 text-emerald-600">Infrastructure Healthy</Tag>
                        </div>
                    </div>
                    
                    <div className="flex items-center gap-5">
                        <Badge dot color="#10b981" offset={[-2, 2]}>
                            <Button type="text" icon={<BellOutlined />} className="text-slate-400 hover:text-slate-800" />
                        </Badge>
                        <Divider type="vertical" className="h-4 border-slate-200" />
                        <div className="flex items-center gap-3 pl-2">
                            <div className="text-right hidden sm:block">
                                <div className="text-[9px] font-black text-slate-400 uppercase tracking-widest leading-none mb-0.5">Superuser</div>
                                <div className="text-xs font-bold text-slate-800">Administrator</div>
                            </div>
                            <Avatar shape="circle" className="bg-slate-100 text-slate-400 border border-slate-200" icon={<UserOutlined />} />
                        </div>
                    </div>
                </header>

                {/* Content Container */}
                <main className="flex-1 overflow-y-auto p-8 bg-slate-50/50">
                    <div className="max-w-[1200px] mx-auto animate-in fade-in slide-in-from-bottom-4 duration-500">
                        <Outlet />
                    </div>
                </main>
            </div>
        </div>
    );
};

export default AdminPage;

