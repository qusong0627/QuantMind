import React, { useEffect, useState } from 'react';
import { Alert, Button, Checkbox, InputNumber, message, Space, Switch, TimePicker } from 'antd';
import dayjs, { Dayjs } from 'dayjs';
import { ClockCircleOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { adminService } from '../../services/adminService';

export interface MarketSyncSchedule {
    market: string;
    label: string;
    enabled: boolean;
    time: string;
    days: number;
    datasets: string[];
}

interface SyncSchedulePanelProps {
    /** 市场标识: A / US / HK / BC / FUTURES / CUSTOM */
    market: string;
    /** 该市场当前勾选的数据集（用于默认填充） */
    selectedDatasets?: string[];
    defaultDays?: number;
    /** 面板标题（默认按上游同步文案；CUSTOM 重建等场景可覆盖） */
    title?: string;
    /** 面板底部提示（默认按上游同步文案） */
    hint?: string;
    /** 立即执行按钮文案（默认「立即同步一次」） */
    runLabel?: string;
    /** 隐藏「最近 N 个交易日 / 数据集」字段（本地重建等场景无此概念） */
    hideWindowFields?: boolean;
}

/** 每市场定时同步配置面板 — 每天 HH:MM 定时同步上游数据（精确到分钟，建议次日 00:00 以后按需错峰）。 */
export const SyncSchedulePanel: React.FC<SyncSchedulePanelProps> = ({
    market,
    selectedDatasets = [],
    defaultDays = 5,
    title = '定时同步（每天自动同步上游数据，建议设置到次日 00:00 以后）',
    hint = '按需错峰触发，避免集中请求；同步在后台执行（Celery），到点自动触发，时区 Asia/Shanghai。',
    runLabel = '立即同步一次',
    hideWindowFields = false,
}) => {
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [running, setRunning] = useState(false);
    const [enabled, setEnabled] = useState(false);
    const [time, setTime] = useState<Dayjs>(dayjs('00:30', 'HH:mm'));
    const [days, setDays] = useState(defaultDays);
    const [datasets, setDatasets] = useState<string[]>([]);

    useEffect(() => {
        loadSchedule();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [market]);

    const loadSchedule = async () => {
        setLoading(true);
        try {
            const resp = await adminService.getSyncSchedule(market);
            if (resp?.data) {
                const s = resp.data;
                setEnabled(!!s.enabled);
                setTime(dayjs(s.time, 'HH:mm').isValid() ? dayjs(s.time, 'HH:mm') : dayjs('00:30', 'HH:mm'));
                setDays(s.days ?? defaultDays);
                setDatasets(s.datasets?.length ? s.datasets : [...selectedDatasets]);
            }
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '未知错误';
            message.error(`加载定时配置失败: ${msg}`);
        } finally {
            setLoading(false);
        }
    };

    const handleSave = async () => {
        setSaving(true);
        try {
            await adminService.saveSyncSchedule(market, {
                enabled,
                time: time.format('HH:mm'),
                days,
                datasets,
            });
            message.success('定时同步配置已保存');
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '未知错误';
            message.error(`保存定时配置失败: ${msg}`);
        } finally {
            setSaving(false);
        }
    };

    const handleRunNow = async () => {
        setRunning(true);
        try {
            await adminService.runSyncScheduleNow(market);
            message.success('已派发同步任务（后台执行）');
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '未知错误';
            message.error(`触发同步失败: ${msg}`);
        } finally {
            setRunning(false);
        }
    };

    return (
        <div className="mt-4 p-3 rounded-lg border border-dashed border-amber-400/60 bg-amber-50/40">
            <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-semibold text-amber-700 flex items-center">
                    <ClockCircleOutlined className="mr-1" />
                    {title}
                </span>
                <Switch
                    size="small"
                    checked={enabled}
                    onChange={setEnabled}
                    loading={loading}
                    checkedChildren="开"
                    unCheckedChildren="关"
                />
            </div>
            {enabled && (
                <>
                    <div className="flex flex-wrap items-center gap-2">
                        <Space size="small">
                            <span className="text-xs text-gray-600">每天</span>
                            <TimePicker
                                size="small"
                                format="HH:mm"
                                minuteStep={5}
                                value={time}
                                onChange={(v) => v && setTime(v)}
                                style={{ width: 90 }}
                            />
                            {!hideWindowFields && (
                                <>
                                    <span className="text-xs text-gray-600">同步最近</span>
                                    <InputNumber
                                        size="small"
                                        min={1}
                                        max={365}
                                        value={days}
                                        onChange={(v) => setDays(v ?? defaultDays)}
                                        style={{ width: 70 }}
                                    />
                                    <span className="text-xs text-gray-600">
                                        {market === 'BC' ? '个自然日' : '个交易日'}
                                    </span>
                                </>
                            )}
                            {hideWindowFields && (
                                <span className="text-xs text-gray-600">自动执行</span>
                            )}
                        </Space>
                    </div>
                    {!hideWindowFields && (
                        <div className="text-xs text-gray-500 mt-1">
                            {datasets.length > 0
                                ? `定时同步数据集: ${datasets.join(', ')}（来自当前勾选）`
                                : '未指定数据集时按各市场默认全量同步'}
                        </div>
                    )}
                    <Alert
                        className="mt-2"
                        type="info"
                        showIcon
                        message={<span className="text-xs">{hint}</span>}
                    />
                </>
            )}
            <div className="flex gap-2 mt-2">
                <Button size="small" type="primary" ghost onClick={handleSave} loading={saving}>
                    保存定时配置
                </Button>
                <Button
                    size="small"
                    icon={<ThunderboltOutlined />}
                    onClick={handleRunNow}
                    loading={running}
                    disabled={!enabled}
                >
                    {runLabel}
                </Button>
            </div>
        </div>
    );
};
