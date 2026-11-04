/**
 * 系统概览（2026-10-09 机构版改版：异常优先 + 密排信息面）。
 *
 * 旧版是 13 张同构服务卡（每张一条绿色进度条）+ 4 张居中统计卡 —— 全绿时
 * 一片噪声，坏的时候要逐张找。改版后：
 * - 顶部异常横幅只在不健康时出现，点名道姓；
 * - 服务集群按平面分组（核心/数据/调度/接入生态），组内异常排前，
 *   健康行只剩一个状态点 + 一行描述，异常行整行染色；
 * - 指标条换成一条面板里的等宽数字格；事件改为左对齐的严重度流。
 * 数据逻辑（鉴权错误态 / 30s 性能轮询）保持不变；「更新系统」新增仓库更新
 * 提醒条（落后 N 个提交 + 当前→最新）与更新进度轮询。
 */
import React, { useEffect, useRef, useState } from 'react';
import { Button, Modal, Result, Spin, message } from 'antd';
import {
    CloudSyncOutlined,
    HomeOutlined,
    LoginOutlined,
    SyncOutlined,
    ThunderboltOutlined,
    AreaChartOutlined,
    ClockCircleOutlined,
} from '@ant-design/icons';
import { useNavigate, useLocation } from 'react-router-dom';
import axios from 'axios';
import { EChartsChart } from '../../../components/common/EChartsChart';
import { adminService } from '../services/adminService';
import { useSystemUpdateCheck } from '../../../hooks/useSystemUpdateCheck';
import { useAppDispatch } from '../../../store';
import { logout } from '../../auth/store/authSlice';
import { DashboardMetrics, DashboardServiceInfo } from '../types';
import { groupServicesByPlane, sortAnomalyFirst } from './servicePlanes';
import { KpiCell, Panel, StatusDot, type DotTone } from './ui/AdminPrimitives';

const SERVICE_PORT: Record<string, string> = {
    api: '8000',
    engine: '8001',
    trade: '8002',
    stream: '8003',
};

const SERVICE_DESC: Record<string, string> = {
    api: '用户认证 · 策略管理 · 社区',
    engine: 'Qlib 回测 · AI 策略 · 模型推理',
    trade: '订单管理 · 持仓 · 风控',
    stream: '实时行情 · WebSocket 推送',
};

const isServiceHealthy = (s: DashboardServiceInfo) => s.healthy && s.status === 'healthy';

const serviceTone = (s: DashboardServiceInfo): DotTone =>
    isServiceHealthy(s) ? 'ok' : s.status === 'unreachable' ? 'bad' : 'warn';

/** 更新进度（后端读 data/update.log 的状态机：idle/running/done/failed）。 */
type UpdateProgress = Awaited<ReturnType<typeof adminService.getUpdateStatus>>;

export const AdminDashboard: React.FC = () => {
    const dispatch = useAppDispatch();
    const navigate = useNavigate();
    const location = useLocation();
    const [metrics, setMetrics] = useState<DashboardMetrics | null>(null);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [loadedAt, setLoadedAt] = useState('');
    const [authError, setAuthError] = useState<{ status: number; message: string } | null>(null);
    const [updating, setUpdating] = useState(false);
    const [perfHistory, setPerfHistory] = useState<Array<{ ts: number; cpu: number; mem: number; disk: number }>>([]);
    const [perfLoading, setPerfLoading] = useState(true);
    // 更新检查（与顶栏徽章/全局提醒共享同一份 query 缓存，不会重复请求上游）
    const { version, update, isFetching: checkingUpdate, refresh: refreshUpdate } = useSystemUpdateCheck();
    const [updateProgress, setUpdateProgress] = useState<UpdateProgress | null>(null);
    const progressTimer = useRef<number | null>(null);

    useEffect(() => {
        loadMetrics();
    }, []);

    // 卸载时停掉更新进度轮询（组件切换不应留下后台 setInterval）
    useEffect(
        () => () => {
            if (progressTimer.current !== null) {
                window.clearInterval(progressTimer.current);
                progressTimer.current = null;
            }
        },
        [],
    );

    const loadMetrics = async () => {
        try {
            adminService.clearMetricsUnauthorized();
            setAuthError(null);
            const data = await adminService.getMetrics();
            setMetrics(data);
            setLoadedAt(
                new Date().toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' }),
            );
        } catch (err: any) {
            const status = err?.response?.status;
            const isLocked = String(err?.message || '').includes('ADMIN_METRICS_UNAUTHORIZED_LOCKED');
            const isAuthError =
                isLocked ||
                status === 401 ||
                status === 403 ||
                (axios.isAxiosError(err) && (err.response?.status === 401 || err.response?.status === 403));

            if (isAuthError) {
                adminService.markMetricsUnauthorized();
                setAuthError({
                    status: status || 401,
                    message: status === 403 ? '您没有访问管理面板的权限。' : '您的登录会话已过期，请重新登录。',
                });
                return;
            }
            message.error('加载系统指标失败');
        } finally {
            setLoading(false);
        }
    };

    const handleRefresh = async () => {
        setRefreshing(true);
        try {
            await loadMetrics();
        } finally {
            setRefreshing(false);
        }
    };

    /**
     * 轮询更新进度（后端读 data/update.log）。更新会重建 api 容器，期间请求失败
     * 是预期内的，不能因此判定更新失败——继续等到接口回来说明状态。
     */
    const watchUpdateProgress = () => {
        if (progressTimer.current !== null) {
            window.clearInterval(progressTimer.current);
        }
        setUpdateProgress({ state: 'running', message: '更新任务已启动' });

        let attempts = 0;
        let busy = false;
        const timer = window.setInterval(async () => {
            // 更新期间 api 容器重建，请求可能挂到 15s 超时；避免轮询叠加
            if (busy) return;
            busy = true;
            attempts += 1;
            try {
                const status = await adminService.getUpdateStatus();
                setUpdateProgress(status);
                if (status.state === 'done') {
                    window.clearInterval(timer);
                    if (progressTimer.current === timer) progressTimer.current = null;
                    message.success('系统更新完成，页面即将刷新…');
                    window.setTimeout(() => window.location.reload(), 3000);
                    return;
                }
                if (status.state === 'failed') {
                    window.clearInterval(timer);
                    if (progressTimer.current === timer) progressTimer.current = null;
                    message.error(status.message || '系统更新失败，请查看更新日志');
                    return;
                }
            } catch {
                /* api 容器重建窗口：继续等 */
            } finally {
                busy = false;
            }
            if (attempts >= 200) {
                // ~10 分钟仍未终态：停止轮询，保留最后一次状态，避免无限 setInterval
                window.clearInterval(timer);
                if (progressTimer.current === timer) progressTimer.current = null;
            }
        }, 3000);
        progressTimer.current = timer;
    };

    /**
     * 「更新系统」：确认弹窗 → 触发宿主机 deploy/update.sh。
     * 更新会重建并重启所有核心服务，当前会话可能短暂中断；故二次确认。
     * 目标分支取更新检查算出的分支（当前部署的 next 分支），保证「提示算哪个
     * 分支、更新就拉哪个分支」。
     */
    const handleUpdateSystem = () => {
        const branch = update?.upstream_branch || version?.branch || 'next';
        const behind = update?.behind ?? 0;
        const current = update?.current_short || update?.current_commit?.slice(0, 8);
        const latest = update?.latest_short || update?.latest_commit?.slice(0, 8);

        Modal.confirm({
            title: '确认更新系统？',
            icon: <CloudSyncOutlined className="text-blue-500" />,
            content: (
                <div className="space-y-2 text-sm">
                    <p className="m-0">
                        将从 git 拉取 <b>{branch}</b> 分支最新代码，执行宿主机{' '}
                        <b>deploy/update.sh</b>：重建镜像并重启服务。
                    </p>
                    {behind > 0 ? (
                        <p className="m-0 rounded-lg bg-amber-50 px-3 py-2 text-[12px] text-amber-700">
                            当前落后 <b>{behind}{update?.behind_capped ? '+' : ''}</b> 个提交
                            {current ? <>（<span className="font-mono">{current}</span></> : null}
                            {latest ? <> → <span className="font-mono">{latest}</span></> : null}
                            {update?.latest_subject ? <>：{update.latest_subject}</> : null}
                        </p>
                    ) : null}
                    <p className="m-0 text-amber-600">⚠️ 重启过程中当前连接可能中断，请勿在交易时段执行，并确保已保存数据。</p>
                    <p className="m-0 text-xs text-slate-400">
                        若服务器工作树有未提交改动，更新会被安全中止（不会覆盖现场改动），日志会给出指引。
                    </p>
                </div>
            ),
            okText: '开始更新',
            cancelText: '取消',
            okButtonProps: { type: 'primary', danger: true, disabled: updating, loading: updating },
            onOk: async () => {
                setUpdating(true);
                try {
                    const res = await adminService.updateSystem(branch);
                    message.success(
                        res?.started
                            ? `已提交系统更新（${res.ref || branch} 分支），后台构建中…`
                            : '更新任务已提交',
                    );
                    watchUpdateProgress();
                } catch (err: any) {
                    const status = err?.response?.status;
                    if (status === 403) {
                        message.warning('更新功能未开启：宿主 docker socket 未挂载到 quantmind 容器');
                    } else if (status === 409) {
                        message.warning('已有更新任务在执行中，请稍候');
                    } else {
                        message.error(err?.response?.data?.detail || '系统更新失败');
                    }
                } finally {
                    setUpdating(false);
                }
            },
        });
    };

    // 节点性能历史：挂载时拉取一次，此后每 30s 轮询（采样器 1min 一个点）
    useEffect(() => {
        let cancelled = false;
        const loadPerf = async () => {
            try {
                const pts = await adminService.getNodeHistory(180);
                if (!cancelled) setPerfHistory(pts);
            } catch {
                /* 静默，保留上次数据 */
            } finally {
                if (!cancelled) setPerfLoading(false);
            }
        };
        loadPerf();
        const timer = setInterval(loadPerf, 30000);
        return () => {
            cancelled = true;
            clearInterval(timer);
        };
    }, []);

    if (authError) {
        return (
            <div className="flex items-center justify-center rounded-lg border border-slate-200 bg-white py-20 shadow-sm">
                <Result
                    status="403"
                    title={<span className="text-xl font-bold text-slate-800">访问受限</span>}
                    subTitle={<span className="text-slate-500">{authError.message}</span>}
                    extra={[
                        <Button
                            type="primary"
                            key="login"
                            icon={<LoginOutlined />}
                            size="large"
                            className="h-11 rounded-xl border-none bg-slate-900 px-8 shadow-sm"
                            onClick={async () => {
                                await dispatch(logout());
                                navigate('/auth/login', { state: { from: location } });
                            }}
                        >
                            重新登录
                        </Button>,
                        <Button
                            key="home"
                            icon={<HomeOutlined />}
                            size="large"
                            className="h-11 rounded-xl border-slate-200 px-8 font-bold text-slate-600 transition-all hover:bg-slate-50"
                            onClick={() => navigate('/')}
                        >
                            返回首页
                        </Button>,
                    ]}
                />
            </div>
        );
    }

    if (loading || !metrics) {
        return (
            <div className="flex w-full flex-col items-center justify-center space-y-4 py-32">
                <Spin size="large" />
                <span className="text-xs font-bold text-slate-400">正在加载指标数据...</span>
            </div>
        );
    }

    const services: DashboardServiceInfo[] = metrics.system?.services || [];
    const unhealthy = sortAnomalyFirst(services.filter((s) => !isServiceHealthy(s)));
    const planes = groupServicesByPlane(services);
    const healthyCount = services.filter(isServiceHealthy).length;
    const { users, strategies, models, system } = metrics;

    const perfOption = {
        backgroundColor: 'transparent',
        grid: { left: 34, right: 12, top: 36, bottom: 24 },
        tooltip: {
            trigger: 'axis',
            formatter: (params: any) => {
                const axisValue = params?.[0]?.axisValue;
                const head = axisValue ?? '';
                const rows = (params || []).map((p: any) => `${p.marker}${p.seriesName}: <b>${p.value}%</b>`).join('<br/>');
                return `<div class="text-xs"><b>${head}</b><br/>${rows}</div>`;
            },
        },
        legend: { top: 4, right: 8, itemWidth: 12, itemHeight: 8, textStyle: { fontSize: 10, color: '#94a3b8' } },
        xAxis: {
            type: 'category',
            data: perfHistory.map((p) =>
                new Date(p.ts * 1000).toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' }),
            ),
            axisLabel: { fontSize: 9, color: '#94a3b8', interval: perfHistory.length > 40 ? Math.ceil(perfHistory.length / 10) : 0 },
            axisLine: { lineStyle: { color: '#e2e8f0' } },
            axisTick: { show: false },
        },
        yAxis: {
            type: 'value',
            min: 0,
            max: 100,
            axisLabel: { fontSize: 9, color: '#94a3b8', formatter: '{value}%' },
            splitLine: { lineStyle: { type: 'dashed', color: '#f1f5f9' } },
        },
        series: [
            {
                name: 'CPU',
                type: 'line',
                smooth: true,
                showSymbol: false,
                data: perfHistory.map((p) => p.cpu),
                lineStyle: { width: 1.5, color: '#6366f1' },
                areaStyle: { color: 'rgba(99,102,241,0.12)' },
                itemStyle: { color: '#6366f1' },
            },
            {
                name: '内存',
                type: 'line',
                smooth: true,
                showSymbol: false,
                data: perfHistory.map((p) => p.mem),
                lineStyle: { width: 1.5, color: '#10b981' },
                areaStyle: { color: 'rgba(16,185,129,0.12)' },
                itemStyle: { color: '#10b981' },
            },
            {
                name: '磁盘',
                type: 'line',
                smooth: true,
                showSymbol: false,
                data: perfHistory.map((p) => p.disk),
                lineStyle: { width: 1.5, color: '#f59e0b' },
                areaStyle: { color: 'rgba(245,158,11,0.10)' },
                itemStyle: { color: '#f59e0b' },
            },
        ],
    };

    return (
        <div className="flex animate-in flex-col gap-3 fade-in duration-500">
            {/* 页头 */}
            <div className="flex items-center justify-between">
                <div>
                    <h2 className="text-[16px] font-semibold text-slate-800">系统控制台</h2>
                    <p className="mt-0.5 text-[12px] text-slate-400">
                        基础设施节点监控与管理{loadedAt ? ` · 更新于 ${loadedAt}` : ''}
                    </p>
                </div>
                <div className="flex items-center gap-2">
                    <Button
                        size="small"
                        danger
                        icon={<SyncOutlined spin={updating} />}
                        loading={updating}
                        onClick={handleUpdateSystem}
                        className="rounded-md"
                    >
                        更新系统
                    </Button>
                    <Button
                        size="small"
                        icon={<ThunderboltOutlined />}
                        loading={refreshing}
                        onClick={() => void handleRefresh()}
                        className="rounded-md"
                    >
                        刷新数据
                    </Button>
                </div>
            </div>

            {/* 仓库更新提醒：更新检查算出的分支就是本次更新会拉取的分支 */}
            {update && update.behind > 0 ? (
                <div className="rounded-xl border border-amber-200/80 bg-gradient-to-r from-amber-50 via-orange-50/70 to-white px-4 py-3">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                        <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                                <span className="relative flex h-2.5 w-2.5">
                                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-70" />
                                    <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-amber-500" />
                                </span>
                                <span className="text-[13px] font-semibold text-amber-900">
                                    仓库 {update.upstream_branch || 'next'} 分支有更新
                                </span>
                                <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-700">
                                    落后 {update.behind}{update.behind_capped ? '+' : ''} 个提交
                                </span>
                            </div>
                            <p className="m-0 mt-1 truncate text-[11px] text-amber-800/70">
                                当前{' '}
                                <span className="font-mono">
                                    {update.current_short || update.current_commit?.slice(0, 8) || '未知'}
                                </span>
                                {update.latest_short || update.latest_commit ? (
                                    <>
                                        {' → '}最新{' '}
                                        <span className="font-mono">
                                            {update.latest_short || update.latest_commit?.slice(0, 8)}
                                        </span>
                                    </>
                                ) : null}
                                {update.latest_subject ? ` · ${update.latest_subject}` : ''}
                            </p>
                        </div>
                        <div className="flex shrink-0 items-center gap-2">
                            <Button
                                size="small"
                                icon={<SyncOutlined spin={checkingUpdate} />}
                                loading={checkingUpdate}
                                onClick={() => void refreshUpdate(true)}
                                className="rounded-md"
                            >
                                重新检查
                            </Button>
                            <Button
                                size="small"
                                type="primary"
                                danger
                                icon={<CloudSyncOutlined />}
                                loading={updating}
                                onClick={handleUpdateSystem}
                                className="rounded-md"
                            >
                                立即更新
                            </Button>
                        </div>
                    </div>
                    {updateProgress ? (
                        <div className="mt-2 border-t border-amber-200/60 pt-2 text-[11px]">
                            {updateProgress.state === 'running' ? (
                                <span className="text-amber-700">
                                    更新进行中…（构建/重启期间接口可能短暂不可达，完成后页面会自动刷新）
                                </span>
                            ) : null}
                            {updateProgress.state === 'failed' ? (
                                <span className="text-rose-600">更新失败：{updateProgress.message}</span>
                            ) : null}
                            {updateProgress.log_tail ? (
                                <details className="mt-1">
                                    <summary className="cursor-pointer select-none text-slate-400 hover:text-slate-600">
                                        查看更新日志
                                    </summary>
                                    <pre className="mt-1 max-h-32 overflow-auto rounded-lg bg-slate-900/90 p-2 text-[10px] leading-4 text-slate-100">
                                        {updateProgress.log_tail}
                                    </pre>
                                </details>
                            ) : null}
                        </div>
                    ) : null}
                </div>
            ) : null}

            {/* 异常横幅：只在有服务不健康时出现 */}
            {unhealthy.length > 0 && (
                <div className="flex items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-4 py-2.5 text-[12px] text-rose-700">
                    <StatusDot tone="bad" pulse />
                    <span className="font-semibold">{unhealthy.length} 项服务异常：</span>
                    <span className="min-w-0 truncate">
                        {unhealthy.map((s) => `${s.service.toUpperCase()}（${s.status}）`).join('、')}
                    </span>
                </div>
            )}

            {/* 指标条 */}
            <Panel bodyClassName="grid grid-cols-2 divide-y divide-slate-100 lg:grid-cols-5 lg:divide-x lg:divide-y-0">
                <KpiCell label="总用户数" value={users.total} sub={`今日新增 ${users.new_today} 人`} />
                <KpiCell label="模拟策略" value={strategies.live} sub={`共 ${strategies.total} 个策略`} />
                <KpiCell label="模型数量" value={models.total} sub="累计训练产出模型" />
                <KpiCell label="系统运行" value={`${system.uptime_days} 天`} sub={`健康度 ${system.health_score}%`} />
                <KpiCell
                    label="服务健康"
                    value={`${healthyCount}/${services.length}`}
                    tone={unhealthy.length > 0 ? 'bad' : 'ok'}
                    sub="服务集群探测"
                />
            </Panel>

            {/* 服务集群（按平面分组，异常优先） */}
            <Panel title="服务集群" sub={`${healthyCount}/${services.length} 健康`}>
                <div className="grid grid-cols-1 gap-x-8 px-4 py-2 lg:grid-cols-2">
                    {planes.map((plane) => {
                        const okCount = plane.services.filter(isServiceHealthy).length;
                        return (
                            <div key={plane.key} className="py-1.5">
                                <div className="flex items-center justify-between border-b border-slate-100 px-2 pb-1">
                                    <span className="text-[11px] font-semibold tracking-wide text-slate-400">{plane.label}</span>
                                    <span className="admin-num text-[10px] text-slate-300">
                                        {okCount}/{plane.services.length}
                                    </span>
                                </div>
                                {plane.services.map((s) => {
                                    const ok = isServiceHealthy(s);
                                    const portText = s.port
                                        ? String(s.port)
                                        : SERVICE_PORT[s.service] ||
                                          (s.service === 'celery' ? '异步' : s.service === 'celery_beat' ? '定时' : '—');
                                    const desc =
                                        s.desc || SERVICE_DESC[s.service] || s.url?.replace(/^https?:\/\//, '') || '—';
                                    return (
                                        <div
                                            key={s.service}
                                            className={`flex h-9 items-center gap-2.5 rounded-sm px-2 ${ok ? '' : 'bg-rose-50/70'}`}
                                        >
                                            <StatusDot tone={serviceTone(s)} pulse={!ok} />
                                            <span className="w-[110px] shrink-0 truncate text-[12px] font-semibold text-slate-700">
                                                {s.service.toUpperCase()}
                                            </span>
                                            <span className="admin-num w-[44px] shrink-0 text-[10px] text-slate-400">
                                                {portText}
                                            </span>
                                            <span className="min-w-0 flex-1 truncate text-[11px] text-slate-400" title={desc}>
                                                {desc}
                                            </span>
                                            <span
                                                className={`admin-num shrink-0 text-[11px] ${
                                                    ok ? 'text-slate-400' : 'font-semibold text-rose-600'
                                                }`}
                                            >
                                                {s.score}%
                                            </span>
                                        </div>
                                    );
                                })}
                            </div>
                        );
                    })}
                </div>
            </Panel>

            {/* 性能历史 + 最近事件 */}
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-12">
                <Panel className="lg:col-span-8" title="节点性能历史" sub="CPU / 内存 / 磁盘 · 采样 1 分钟">
                    {perfLoading && perfHistory.length === 0 ? (
                        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-slate-200 bg-slate-50 py-16 mx-4 my-3">
                            <AreaChartOutlined className="mb-3 text-3xl text-slate-300" />
                            <span className="text-xs font-bold text-slate-400">数据采集中，稍后展示曲线…</span>
                        </div>
                    ) : perfHistory.length >= 2 ? (
                        <div className="h-64 w-full px-1 py-2">
                            <EChartsChart option={perfOption} />
                        </div>
                    ) : (
                        <div className="mx-4 my-3 flex flex-col items-center justify-center rounded-lg border border-dashed border-slate-200 bg-slate-50 py-16">
                            <AreaChartOutlined className="mb-3 text-3xl text-slate-300" />
                            <span className="text-xs font-bold text-slate-400">数据采集中，稍后展示曲线…</span>
                        </div>
                    )}
                </Panel>

                <Panel className="lg:col-span-4" title="最近事件" sub={`${metrics.recent_events?.length || 0} 条`}>
                    {metrics.recent_events && metrics.recent_events.length > 0 ? (
                        <div className="admin-dark-scrollbar max-h-[276px] divide-y divide-slate-50 overflow-y-auto">
                            {metrics.recent_events.map((item, idx) => (
                                <div key={idx} className="flex items-center gap-2.5 px-4 py-2">
                                    <StatusDot
                                        tone={item.type === 'warning' ? 'warn' : item.type === 'success' ? 'ok' : 'info'}
                                    />
                                    <span className="min-w-0 flex-1 truncate text-[12px] text-slate-600" title={item.title}>
                                        {item.title}
                                    </span>
                                    <span className="admin-num shrink-0 text-[10px] text-slate-400">{item.time}</span>
                                </div>
                            ))}
                        </div>
                    ) : (
                        <div className="flex flex-col items-center justify-center py-12">
                            <ClockCircleOutlined className="mb-3 text-3xl text-slate-300" />
                            <span className="text-xs font-bold text-slate-400">暂无事件记录</span>
                        </div>
                    )}
                </Panel>
            </div>
        </div>
    );
};
