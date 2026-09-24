import React, { useEffect, useState } from 'react';
import { Switch, Tag, Typography, message, Alert, Modal } from 'antd';
import {
    RobotOutlined,
    SettingOutlined,
    CheckCircleFilled,
    CloseCircleFilled,
    LoadingOutlined,
    HddOutlined,
    ThunderboltOutlined,
    ApiOutlined,
    ClockCircleOutlined,
    CloudSyncOutlined,
} from '@ant-design/icons';
import { adminService } from '../services/adminService';
import { SectionLoading } from '../../../components/common/UnifiedLoading';

const { Title, Text } = Typography;

type AutoUpdateInfo = {
    enabled: boolean;
    run_at: string;
    next_run_at: string | null;
    available: boolean;
    last_decision: { at: string; action: string; reason: string } | null;
};

type FinbertDetail = {
    enabled?: boolean;
    device?: number;
    model?: string;
    installed?: boolean;
    framework_ok?: boolean;
    model_ready?: boolean;
    model_failed?: boolean;
    override?: boolean | null;
    toggle_path?: string;
};

function StatusPill({
    ok,
    pending,
    label,
    value,
    icon,
}: {
    ok?: boolean;
    pending?: boolean;
    label: string;
    value: string;
    icon: React.ReactNode;
}) {
    const tone = pending
        ? 'border-amber-100 bg-amber-50/80 text-amber-800'
        : ok
          ? 'border-emerald-100 bg-emerald-50/70 text-emerald-800'
          : 'border-slate-100 bg-slate-50 text-slate-600';

    return (
        <div className={`rounded-xl border px-3 py-2.5 text-center ${tone}`}>
            <div className="flex items-center justify-center gap-1.5 text-[11px] opacity-70">
                {icon}
                <span>{label}</span>
            </div>
            <div className="mt-1 text-sm font-semibold tracking-tight truncate">{value}</div>
        </div>
    );
}

const RISK_ITEMS = [
    '执行 deploy/update.sh --force：服务器上的未提交改动会被 git reset --hard 覆盖',
    '全部后端服务重启，进行中的回测 / 推理 / 页面连接会中断',
    '无自动回滚：更新后健康检查失败不会退回旧版本，需人工介入恢复',
];

function AutoUpdateCard() {
    const [info, setInfo] = useState<AutoUpdateInfo | null>(null);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [saving, setSaving] = useState(false);
    const [updateState, setUpdateState] = useState<{
        state: string;
        message?: string;
        log_tail?: string;
    } | null>(null);

    const load = async () => {
        try {
            const cfg = await adminService.getAutoUpdate();
            setInfo(cfg);
            setLoadError(null);
        } catch (e: any) {
            const status = e?.response?.status;
            setLoadError(
                status === 404
                    ? '后端未部署该接口（404），需同步代码并重启 quantmind 容器'
                    : status === 401 || status === 403
                      ? '无权限访问（需管理员登录）'
                      : e?.message || '网络错误',
            );
            setInfo(null);
        }
    };

    useEffect(() => {
        load();
        adminService
            .getUpdateStatus()
            .then((st) => setUpdateState(st as any))
            .catch(() => undefined);
    }, []);

    const apply = async (next: boolean) => {
        setSaving(true);
        try {
            await adminService.setAutoUpdate(next);
            await load();
            message.success(next ? '每日自动更新已开启' : '每日自动更新已关闭');
        } catch (e: any) {
            message.error(e?.response?.data?.detail || '保存失败，请检查管理员权限');
        } finally {
            setSaving(false);
        }
    };

    const handleChange = (next: boolean) => {
        if (!next) {
            apply(false);
            return;
        }
        Modal.confirm({
            title: '开启每日自动强制更新？',
            width: 560,
            okText: '我已了解风险，开启',
            okButtonProps: { danger: true },
            cancelText: '取消',
            content: (
                <div className="text-xs leading-relaxed text-slate-600 space-y-1.5 pt-1">
                    <p className="!mb-1">
                        开启后<b>每天 {info?.run_at || '00:00'}</b>
                        自动执行一次强制更新，请确认以下风险：
                    </p>
                    <ul className="list-disc pl-4 space-y-1 !mb-1">
                        {RISK_ITEMS.map((t) => (
                            <li key={t}>{t}</li>
                        ))}
                    </ul>
                    <p className="!mb-0 text-slate-400">
                        已做保护：无新版本时不重启；服务器工作区有未提交改动时中止；
                        版本索引不可达时跳过。每次决策都记入「最近事件」。
                    </p>
                </div>
            ),
            onOk: () => apply(true),
        });
    };

    if (!info) {
        return (
            <section className="rounded-2xl border border-slate-200 bg-white shadow-sm px-5 py-4 sm:px-6">
                <div className="flex flex-wrap items-center gap-2">
                    <span className="inline-flex h-8 w-8 items-center justify-center rounded-xl bg-slate-100 text-slate-400">
                        <CloudSyncOutlined />
                    </span>
                    <h3 className="m-0 text-sm font-bold text-slate-700">每日自动强制更新</h3>
                    <Tag className="m-0 border-none text-[11px]" color="default">
                        不可用
                    </Tag>
                </div>
                <Text className="mt-2 block text-xs text-slate-400">
                    配置读取失败：{loadError || '未知原因'}
                </Text>
            </section>
        );
    }

    const disabled = !info.available;
    const last = info.last_decision;
    const lastFailed = last?.action === '失败';
    const nextRun = info.next_run_at
        ? new Date(info.next_run_at).toLocaleString('zh-CN', { hour12: false })
        : '—';

    return (
        <section className="rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden">
            <div className="px-5 pt-5 pb-4 sm:px-6">
                <div className="flex items-start justify-between gap-6">
                    <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                            <span className="inline-flex h-8 w-8 items-center justify-center rounded-xl bg-slate-900 text-white">
                                <CloudSyncOutlined />
                            </span>
                            <h3 className="m-0 text-base font-bold text-slate-800">
                                每日自动强制更新
                            </h3>
                            <Tag
                                color={disabled ? 'warning' : info.enabled ? 'success' : 'default'}
                                className="m-0 border-none text-[11px]"
                            >
                                {disabled ? '不可用' : info.enabled ? '已开启' : '已关闭'}
                            </Tag>
                        </div>
                        <p className="mt-2 mb-0 text-xs leading-relaxed text-slate-500">
                            开启后每天 {info.run_at} 自动执行一次
                            <code className="mx-1 text-[11px]">deploy/update.sh --force</code>
                            拉取最新代码并重启全部服务。
                        </p>
                    </div>

                    <div className="shrink-0 flex flex-col items-end gap-1.5 pt-0.5">
                        <Switch
                            checked={info.enabled}
                            loading={saving}
                            onChange={handleChange}
                            disabled={disabled}
                            checkedChildren="开"
                            unCheckedChildren="关"
                        />
                        <span className="text-[11px] text-slate-400">
                            {disabled ? '需挂载 docker socket' : `下次 ${nextRun}`}
                        </span>
                    </div>
                </div>
            </div>

            {info.enabled && (
                <div className="px-5 pb-4 sm:px-6">
                    <Alert
                        type="warning"
                        showIcon
                        className="rounded-xl text-xs !py-2 !px-3"
                        message="自动更新会覆盖服务器上的未提交改动并重启服务"
                        description={
                            <ul className="list-disc pl-4 space-y-0.5 !mb-0">
                                {RISK_ITEMS.map((t) => (
                                    <li key={t}>{t}</li>
                                ))}
                            </ul>
                        }
                    />
                </div>
            )}

            <div className="border-t border-slate-100 bg-slate-50/60 px-5 py-3 sm:px-6">
                <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
                    <Text className="text-[11px] text-slate-400 flex items-center gap-1.5">
                        <ClockCircleOutlined />
                        上次决策：
                        <span className={lastFailed ? 'text-red-500' : 'text-slate-500'}>
                            {last ? `${last.at.replace('T', ' ')} · ${last.action} · ${last.reason}` : '尚未自动执行过'}
                        </span>
                    </Text>
                    {updateState?.state === 'running' ? (
                        <Text className="text-[11px] text-amber-500">更新正在执行中</Text>
                    ) : updateState?.state === 'failed' ? (
                        <Text className="text-[11px] text-red-500">
                            最近一次更新失败，请查看 data/update.log
                        </Text>
                    ) : null}
                </div>
            </div>
        </section>
    );
}

export const AdminSystemSettings: React.FC = () => {
    const [enabled, setEnabled] = useState<boolean | null>(null);
    const [loading, setLoading] = useState(false);
    const [detail, setDetail] = useState<FinbertDetail | null>(null);
    const [initialLoading, setInitialLoading] = useState(true);

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const st: any = await adminService.getFinbertStatus();
                if (!cancelled) {
                    const en = !!(st?.enabled ?? st?.data?.enabled);
                    setEnabled(en);
                    setDetail(st?.data ?? st);
                }
            } catch {
                if (!cancelled) setEnabled(null);
            } finally {
                if (!cancelled) setInitialLoading(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, []);

    const handleToggle = async (checked: boolean) => {
        setLoading(true);
        const prev = enabled;
        setEnabled(checked);
        try {
            const res: any = await adminService.setFinbertEnabled(checked);
            const st = res?.data ?? res;
            const en = !!(st?.enabled ?? checked);
            setEnabled(en);
            setDetail(st);
            message.success(
                `FinBERT 已${en ? '开启' : '关闭'}${
                    st?.model_ready
                        ? '（模型就绪）'
                        : checked
                          ? '（后台加载中，约 10-20s 后生效）'
                          : ''
                }`,
            );
        } catch (e: any) {
            setEnabled(prev);
            message.error(e?.response?.data?.detail || '切换失败，请检查管理员权限');
        } finally {
            setLoading(false);
        }
    };

    if (initialLoading) {
        return <SectionLoading tip="加载系统设置..." minHeight={240} />;
    }

    const modelName = detail?.model || 'bardsai/finance-sentiment-zh-base';
    const notInstalled = !!detail && detail.installed === false;
    const noFramework = !!detail && detail.framework_ok === false;
    const cannotEnable = notInstalled || noFramework;
    const deviceNum = detail?.device ?? -1;
    const deviceLabel = deviceNum === -1 ? 'CPU' : `GPU ${deviceNum}`;
    const readyPending = !detail?.model_ready && !detail?.model_failed && !!enabled;
    const readyLabel = detail?.model_ready
        ? '已就绪'
        : detail?.model_failed
          ? '加载失败'
          : enabled
            ? '加载中'
            : '未加载';
    const statusTone =
        enabled === null
            ? { tag: 'default' as const, text: '未知' }
            : cannotEnable
              ? { tag: 'warning' as const, text: '不可用' }
              : enabled
                ? { tag: 'success' as const, text: '运行中' }
                : { tag: 'default' as const, text: '已关闭' };

    return (
        <div className="w-full space-y-6">
            <header>
                <Title
                    level={4}
                    className="!m-0 !font-black !text-slate-800 flex items-center gap-2"
                >
                    <SettingOutlined className="text-slate-600" /> 系统设置
                </Title>
                <Text className="text-slate-400 text-xs">基础设施与 AI 能力开关</Text>
            </header>

            <section className="rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden">
                {/* 主控区 */}
                <div className="px-5 pt-5 pb-4 sm:px-6">
                    <div className="flex items-start justify-between gap-6">
                        <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-2">
                                <span className="inline-flex h-8 w-8 items-center justify-center rounded-xl bg-slate-900 text-white">
                                    <RobotOutlined />
                                </span>
                                <h3 className="m-0 text-base font-bold text-slate-800">
                                    FinBERT 中文金融情感
                                </h3>
                                <Tag
                                    color={statusTone.tag}
                                    className="m-0 border-none text-[11px]"
                                >
                                    {statusTone.text}
                                </Tag>
                            </div>
                            <p className="mt-2 mb-0 text-xs leading-relaxed text-slate-500">
                                入库资讯走中文金融情感推理；关闭后仅用字典法，CPU
                                零开销。切换即时生效，无需重启。
                            </p>
                        </div>

                        <div className="shrink-0 flex flex-col items-end gap-1.5 pt-0.5">
                            <Switch
                                checked={!!enabled}
                                loading={loading || enabled === null}
                                onChange={handleToggle}
                                checkedChildren="开"
                                unCheckedChildren="关"
                                disabled={cannotEnable}
                            />
                            <span className="text-[11px] text-slate-400">
                                {notInstalled
                                    ? '需先装权重'
                                    : noFramework
                                      ? '需补装框架'
                                      : enabled
                                        ? '推理已启用'
                                        : '仅字典法'}
                            </span>
                        </div>
                    </div>
                </div>

                {/* 状态条 */}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 px-5 pb-4 sm:px-6">
                    <StatusPill
                        icon={<ThunderboltOutlined />}
                        label="推理设备"
                        value={deviceLabel}
                        ok={deviceNum !== -1}
                    />
                    <StatusPill
                        icon={
                            readyPending ? (
                                <LoadingOutlined />
                            ) : detail?.model_ready ? (
                                <CheckCircleFilled />
                            ) : (
                                <CloseCircleFilled />
                            )
                        }
                        label="模型状态"
                        value={readyLabel}
                        ok={!!detail?.model_ready}
                        pending={readyPending}
                    />
                    <StatusPill
                        icon={<HddOutlined />}
                        label="权重文件"
                        value={notInstalled ? '未安装' : '已就位'}
                        ok={!notInstalled}
                    />
                    <StatusPill
                        icon={<ApiOutlined />}
                        label="推理框架"
                        value={noFramework ? '缺 torch' : '可用'}
                        ok={!noFramework}
                    />
                </div>

                {/* 阻塞告警 */}
                {(notInstalled || noFramework) && (
                    <div className="px-5 pb-4 sm:px-6 space-y-2">
                        {notInstalled && (
                            <Alert
                                type="warning"
                                showIcon
                                className="rounded-xl text-xs !py-2 !px-3"
                                message="模型权重未安装"
                                description={
                                    <span>
                                        未检测到 <code className="text-[11px]">{modelName}</code>{' '}
                                        （路径{' '}
                                        <code className="text-[11px]">
                                            /app/models/finbert-zh-base
                                        </code>
                                        ）。请先执行{' '}
                                        <code className="text-[11px]">
                                            backend/scripts/download_finbert.py
                                        </code>{' '}
                                        后再开启。
                                    </span>
                                }
                            />
                        )}
                        {noFramework && !notInstalled && (
                            <Alert
                                type="warning"
                                showIcon
                                className="rounded-xl text-xs !py-2 !px-3"
                                message="缺少 PyTorch 推理框架"
                                description={
                                    <span>
                                        权重已就绪，但镜像未含 torch/transformers。请在服务器执行{' '}
                                        <code className="text-[11px]">
                                            sudo bash deploy/install-model-deps.sh
                                        </code>{' '}
                                        补装后重试。
                                    </span>
                                }
                            />
                        )}
                    </div>
                )}

                {/* 页脚元信息 */}
                <div className="border-t border-slate-100 bg-slate-50/60 px-5 py-3 sm:px-6">
                    <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
                        <Text className="text-[11px] text-slate-400 truncate">
                            模型 <span className="font-mono text-slate-500">{modelName}</span>
                            <span className="mx-1.5 text-slate-300">·</span>
                            约 391M · 离线推理
                        </Text>
                        <Text className="text-[11px] text-slate-400">
                            首次开启约 10–20s 加载；历史资讯需重建 enrichment
                        </Text>
                    </div>
                </div>
            </section>

            <AutoUpdateCard />
        </div>
    );
};

export default AdminSystemSettings;
