/**
 * 后台管理外壳（2026-10-09 机构版改版：浅色统一导航轴 + 浅色密排内容）。
 *
 * 改版要点：
 * - 导航从 antd Menu 换成自绘导航轴（白底 + slate 边框 + indigo 选中）：
 *   分组标题、左缘选中指示条、折叠态图标 + 右侧 Tooltip；分组与路由键**不变**，
 *   其余 23 个面板只换外壳。
 * - 顶栏压到 48px，标题跟随当前路由（分组名做弱化的面包屑尾）；右侧健康告示
 *   与左下负载读数**同源**（AdminPage 统一轮询 getSystemLoad，20s）。
 * - 内容区包装类沿用旧逻辑（orders/risk/inference/profile 的滚动与宽度特例），
 *   避免动到 23 个子面板的既有布局。
 */
import React, { useEffect, useState } from 'react';
import { Avatar, Badge, Button, Divider, Tooltip } from 'antd';
import {
    DashboardOutlined,
    UserOutlined,
    RocketOutlined,
    SettingOutlined,
    ThunderboltOutlined,
    BellOutlined,
    DatabaseOutlined,
    FolderOpenOutlined,
    StockOutlined,
    LineChartOutlined,
    ReadOutlined,
    TeamOutlined,
    BarChartOutlined,
    ExperimentOutlined,
    SlidersOutlined,
    HddOutlined,
    CloudServerOutlined,
    OrderedListOutlined,
    SafetyCertificateOutlined,
    MenuFoldOutlined,
    MenuUnfoldOutlined,
} from '@ant-design/icons';
import { useNavigate, useLocation, Outlet } from 'react-router-dom';
import { AdminSystemLoadWidget } from './components/AdminSystemLoadWidget';
import { StatusDot } from './components/ui/AdminPrimitives';
import { adminService } from './services/adminService';
import { systemService, type SystemVersion } from '../../services/systemService';
import type { SystemLoadSummary } from './types';

// ── 导航注册表（分组 + 路由键）。新增栏目只改这里 ──────────────────────

interface NavItemDef {
    key: string;
    label: string;
    icon: React.ReactNode;
}

interface NavGroupDef {
    /** 空串 = 不显示分组标题（总览独立置顶） */
    label: string;
    items: NavItemDef[];
}

const NAV_GROUPS: NavGroupDef[] = [
    {
        label: '',
        items: [{ key: 'overview', label: '系统概览', icon: <DashboardOutlined /> }],
    },
    {
        label: '数据管理',
        items: [
            { key: 'data', label: '数据集目录', icon: <FolderOpenOutlined /> },
            { key: 'qlib', label: 'Qlib 数据管理', icon: <DatabaseOutlined /> },
            { key: 'stock-pools', label: '全局股票池', icon: <StockOutlined /> },
            { key: 'quotes', label: '数据源监控', icon: <LineChartOutlined /> },
            { key: 'news', label: '新闻情感', icon: <ReadOutlined /> },
        ],
    },
    {
        label: 'API 服务',
        items: [
            { key: 'users', label: '用户管理', icon: <TeamOutlined /> },
            { key: 'strategies', label: '策略仓库', icon: <BarChartOutlined /> },
        ],
    },
    {
        label: '推理引擎',
        items: [
            { key: 'models', label: '模型管理', icon: <ExperimentOutlined /> },
            { key: 'inference', label: '推理监控', icon: <ThunderboltOutlined /> },
        ],
    },
    {
        label: '训练服务',
        items: [
            { key: 'feature-catalog', label: '特征字典', icon: <SlidersOutlined /> },
            { key: 'training-datasets', label: '模型训练数据集', icon: <HddOutlined /> },
            { key: 'autodl-nodes', label: 'AutoDL 节点', icon: <CloudServerOutlined /> },
        ],
    },
    {
        label: '交易核心',
        items: [
            { key: 'orders', label: '订单管理', icon: <OrderedListOutlined /> },
            { key: 'risk', label: '风险控制', icon: <SafetyCertificateOutlined /> },
        ],
    },
    {
        label: '系统',
        items: [
            { key: 'profile', label: '个人中心', icon: <UserOutlined /> },
            { key: 'settings', label: '系统设置', icon: <SettingOutlined /> },
        ],
    },
];

const NAV_INDEX: Record<string, { label: string; group: string }> = Object.fromEntries(
    NAV_GROUPS.flatMap((g) => g.items.map((it) => [it.key, { label: it.label, group: g.label }])),
);

// ── 顶栏更新徽章（留在原处；版本接口无 update 字段时整体不渲染） ──────────

const AdminUpdateBadge: React.FC = () => {
    const [versionInfo, setVersionInfo] = useState<SystemVersion | null>(null);
    const [checkingUpdate, setCheckingUpdate] = useState(false);

    const loadVersion = (force = false) => {
        setCheckingUpdate(true);
        systemService
            .getVersion(force)
            .then((info) => setVersionInfo(info))
            .catch(() => setVersionInfo(null))
            .finally(() => setCheckingUpdate(false));
    };

    useEffect(() => {
        loadVersion();
    }, []);

    const update = versionInfo?.update;
    if (checkingUpdate && !update) {
        return <span className="text-[10px] text-slate-400">检查更新…</span>;
    }
    if (!update) {
        return null;
    }
    if (update.behind > 0) {
        return (
            <Tooltip title="在服务器项目目录执行：sudo bash deploy/update.sh">
                <button
                    type="button"
                    onClick={() => loadVersion(true)}
                    disabled={checkingUpdate}
                    className="inline-flex items-center gap-1.5 rounded-full border border-amber-200 bg-amber-50 px-3 py-1 text-xs font-medium text-amber-700 disabled:opacity-60"
                >
                    <span className="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
                    {`落后 ${update.behind}${update.behind_capped ? '+' : ''} 个提交`}
                </button>
            </Tooltip>
        );
    }
    if (update.is_up_to_date) {
        return (
            <button
                type="button"
                onClick={() => loadVersion(true)}
                className="text-[10px] font-medium text-slate-400 hover:text-emerald-600"
            >
                已最新
            </button>
        );
    }
    return null;
};

/** 顶栏健康告示：与左下负载卡片同源（AdminPage 轮询下发的 load）。 */
const HealthPill: React.FC<{ load: SystemLoadSummary | null }> = ({ load }) => {
    const total = load?.services_summary?.total ?? 0;
    if (!load || total === 0) {
        return (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] font-medium text-slate-400">
                <StatusDot tone="idle" /> 状态检测中
            </span>
        );
    }
    const healthy = load.services_summary.healthy;
    const allOk = healthy === total;
    return (
        <span
            className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium ${
                allOk ? 'border-emerald-200 bg-emerald-50 text-emerald-700' : 'border-rose-200 bg-rose-50 text-rose-700'
            }`}
        >
            <StatusDot tone={allOk ? 'ok' : 'bad'} pulse />
            {allOk ? `基础设施正常 ${healthy}/${total}` : `${total - healthy} 项异常 · ${healthy}/${total} 在线`}
        </span>
    );
};

// ── 外壳 ─────────────────────────────────────────────────────────────

const AdminPage: React.FC = () => {
    const navigate = useNavigate();
    const location = useLocation();
    const [collapsed, setCollapsed] = useState(false);
    const [load, setLoad] = useState<SystemLoadSummary | null>(null);

    // 系统负载/健康：顶栏告示与左下读数共用一个轮询源，20s 一拍
    useEffect(() => {
        let cancelled = false;
        const fetchLoad = async () => {
            try {
                const data = await adminService.getSystemLoad();
                if (!cancelled && data?.workload) setLoad(data);
            } catch {
                /* 静默：负载轮询失败不该打扰管理操作 */
            }
        };
        void fetchLoad();
        const timer = setInterval(fetchLoad, 20000);
        return () => {
            cancelled = true;
            clearInterval(timer);
        };
    }, []);

    const currentKey = location.pathname.split('/').pop() || 'overview';
    const current = NAV_INDEX[currentKey] || { label: '管理后台', group: '' };

    return (
        <div className="admin-page flex h-screen w-full overflow-hidden bg-[#F4F6F8] font-sans">
            {/* 浅色导航轴（与内容区同色系；选中态用 indigo 口音） */}
            <aside
                className={`flex h-full shrink-0 flex-col border-r border-slate-200 bg-white transition-[width] duration-200 ${
                    collapsed ? 'w-16' : 'w-[236px]'
                }`}
            >
                <div
                    className={`flex h-14 shrink-0 items-center gap-2.5 border-b border-slate-100 ${
                        collapsed ? 'justify-center' : 'px-4'
                    }`}
                >
                    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-indigo-500">
                        <RocketOutlined className="text-[15px] text-white" />
                    </div>
                    {!collapsed && (
                        <div className="min-w-0">
                            <div className="truncate text-[13px] font-bold leading-tight tracking-wide text-slate-800">
                                QuantMind
                            </div>
                            <div className="text-[9px] font-semibold uppercase tracking-[0.18em] text-slate-400">
                                管理后台
                            </div>
                        </div>
                    )}
                </div>

                <nav className="admin-dark-scrollbar flex-1 space-y-4 overflow-y-auto px-2 py-3">
                    {NAV_GROUPS.map((group, gi) => (
                        <div key={group.label || `g${gi}`}>
                            {group.label && !collapsed && (
                                <div className="mb-1 px-2 text-[9px] font-semibold uppercase tracking-[0.16em] text-slate-400">
                                    {group.label}
                                </div>
                            )}
                            <div className="space-y-0.5">
                                {group.items.map((item) => {
                                    const active = currentKey === item.key;
                                    const button = (
                                        <button
                                            type="button"
                                            onClick={() => navigate(`/admin/${item.key}`)}
                                            className={
                                                collapsed
                                                    ? `relative mx-auto flex h-9 w-9 items-center justify-center rounded-md transition-colors ${
                                                          active
                                                              ? 'bg-indigo-50 text-indigo-600'
                                                              : 'text-slate-500 hover:bg-slate-100 hover:text-slate-700'
                                                      }`
                                                    : `relative flex h-[34px] w-full items-center gap-2.5 rounded-md pl-3 pr-2 text-[13px] transition-colors ${
                                                          active
                                                              ? 'bg-indigo-50 font-semibold text-indigo-700'
                                                              : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
                                                      }`
                                            }
                                        >
                                            {active && !collapsed && (
                                                <span className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r bg-indigo-500" />
                                            )}
                                            <span
                                                className={`flex h-4 w-4 shrink-0 items-center justify-center text-[14px] ${
                                                    active ? 'text-indigo-600' : 'text-slate-400'
                                                }`}
                                            >
                                                {item.icon}
                                            </span>
                                            {!collapsed && <span className="truncate">{item.label}</span>}
                                        </button>
                                    );
                                    return collapsed ? (
                                        <Tooltip key={item.key} title={item.label} placement="right">
                                            {button}
                                        </Tooltip>
                                    ) : (
                                        <React.Fragment key={item.key}>{button}</React.Fragment>
                                    );
                                })}
                            </div>
                        </div>
                    ))}
                </nav>

                <div className="shrink-0">
                    <AdminSystemLoadWidget collapsed={collapsed} load={load} />
                    <button
                        type="button"
                        onClick={() => setCollapsed((v) => !v)}
                        className="flex h-9 w-full items-center justify-center gap-2 border-t border-slate-200 text-[11px] text-slate-400 transition-colors hover:bg-slate-50 hover:text-slate-600"
                    >
                        {collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                        {!collapsed && <span>收起导航</span>}
                    </button>
                </div>
            </aside>

            {/* 浅色内容区 */}
            <div className="relative flex h-full flex-1 flex-col overflow-hidden">
                <header className="flex h-12 shrink-0 items-center justify-between border-b border-slate-200 bg-white pl-5 pr-6">
                    <div className="flex min-w-0 items-baseline gap-2">
                        <span className="truncate text-[13px] font-semibold text-slate-800">{current.label}</span>
                        {current.group && <span className="shrink-0 text-[11px] text-slate-400">/ {current.group}</span>}
                    </div>

                    <div className="flex items-center gap-4">
                        <HealthPill load={load} />
                        <AdminUpdateBadge />
                        <Badge dot color="#10b981" offset={[-2, 2]}>
                            <Button type="text" size="small" icon={<BellOutlined />} className="text-slate-400 hover:text-slate-800" />
                        </Badge>
                        <Divider type="vertical" className="mx-0 h-4 border-slate-200" />
                        <div className="flex items-center gap-2.5">
                            <div className="hidden text-right sm:block">
                                <div className="mb-0.5 text-[9px] font-semibold uppercase leading-none tracking-widest text-slate-400">
                                    超级用户
                                </div>
                                <div className="text-[12px] font-semibold leading-none text-slate-800">管理员</div>
                            </div>
                            <Avatar size={28} shape="circle" className="border border-slate-200 bg-slate-100 text-slate-400" icon={<UserOutlined />} />
                        </div>
                    </div>
                </header>

                <main
                    className={`flex-1 px-6 pt-6 pb-[60px] ${
                        ['orders', 'risk', 'inference', 'profile'].includes(currentKey)
                            ? 'flex min-h-0 flex-col overflow-hidden'
                            : 'overflow-y-auto'
                    }`}
                >
                    {/* 大屏页面用全宽，其余保留 1400px 阅读宽度（沿用旧规则） */}
                    <div
                        className={
                            ['news', 'inference', 'tags', 'settings', 'stock-pools', 'orders', 'risk', 'profile'].includes(currentKey)
                                ? `min-h-0 animate-in fade-in slide-in-from-bottom-4 duration-500 ${
                                      ['orders', 'risk', 'profile'].includes(currentKey)
                                          ? 'flex w-full flex-1 flex-col'
                                          : currentKey === 'inference'
                                            ? 'mx-auto flex w-full max-w-[1400px] flex-1 flex-col'
                                            : 'h-full w-full'
                                  }`
                                : 'mx-auto max-w-[1400px] animate-in fade-in slide-in-from-bottom-4 duration-500'
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
