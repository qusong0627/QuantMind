import React, { useEffect, useState } from 'react';
import { Alert, Button, message, Space, Switch, Tag, TimePicker } from 'antd';
import dayjs, { Dayjs } from 'dayjs';
import { ExperimentOutlined, ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { adminService } from '../../services/adminService';

/** 后端 factor_fill_scheduler.FACTOR_DATASETS 的展示名 */
const DATASET_LABELS: Record<string, string> = {
    l1_factors: 'L1 因子',
    south_factors: '南向因子',
    ccass_factors: 'CCASS 因子',
};

const STATUS_TEXT: Record<string, string> = {
    filled: '已补齐',
    up_to_date: '已最新',
    error: '失败',
    source_missing: '来源缺失',
    skipped: '跳过',
    unknown_dataset: '未知数据集',
};

interface DatasetFreshness {
    latest: string | null;
    source_latest: string | null;
    up_to_date: boolean;
}

interface DatasetResult {
    status: string;
    latest?: string | null;
    source_latest?: string | null;
    error?: string;
}

interface FactorFillScheduleData {
    market: string;
    label: string;
    enabled: boolean;
    time: string;
    freshness: Record<string, DatasetFreshness>;
    last: {
        market?: string;
        status?: string;
        finished?: string;
        datasets?: Record<string, DatasetResult>;
    } | null;
}

interface FactorFillSchedulePanelProps {
    /** 市场标识: HK / US（因子自动填充目前只覆盖港股/美股） */
    market: string;
}

/** 因子数据集自动填充面板 — 每天定时检查各因子集是否落后于来源数据，落后才补建。 */
export const FactorFillSchedulePanel: React.FC<FactorFillSchedulePanelProps> = ({ market }) => {
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [running, setRunning] = useState(false);
    const [enabled, setEnabled] = useState(false);
    const [time, setTime] = useState<Dayjs>(dayjs('04:30', 'HH:mm'));
    const [data, setData] = useState<FactorFillScheduleData | null>(null);

    useEffect(() => {
        loadSchedule();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [market]);

    const loadSchedule = async () => {
        setLoading(true);
        try {
            const resp = await adminService.getFactorFillSchedule(market);
            if (resp?.data) {
                const s: FactorFillScheduleData = resp.data;
                setData(s);
                setEnabled(!!s.enabled);
                setTime(dayjs(s.time, 'HH:mm').isValid() ? dayjs(s.time, 'HH:mm') : dayjs('04:30', 'HH:mm'));
            }
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '未知错误';
            message.error(`加载因子填充配置失败: ${msg}`);
        } finally {
            setLoading(false);
        }
    };

    const handleSave = async () => {
        setSaving(true);
        try {
            await adminService.saveFactorFillSchedule(market, {
                enabled,
                time: time.format('HH:mm'),
            });
            message.success('因子自动填充配置已保存');
            await loadSchedule();
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '未知错误';
            message.error(`保存因子填充配置失败: ${msg}`);
        } finally {
            setSaving(false);
        }
    };

    const handleRunNow = async () => {
        setRunning(true);
        try {
            await adminService.runFactorFillNow(market);
            message.success('已派发因子填充任务（后台执行，完成后点「刷新」查看结果）');
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '未知错误';
            message.error(`触发因子填充失败: ${msg}`);
        } finally {
            setRunning(false);
        }
    };

    const lastDatasets = data?.last?.datasets ?? {};
    const lastSummary = Object.entries(lastDatasets)
        .map(([name, r]) => `${DATASET_LABELS[name] ?? name} ${STATUS_TEXT[r.status] ?? r.status}`)
        .join(' · ');

    return (
        <div className="mt-4 p-3 rounded-lg border border-dashed border-cyan-500/50 bg-cyan-50/40">
            <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-semibold text-cyan-700 flex items-center">
                    <ExperimentOutlined className="mr-1" />
                    因子自动填充（每天定时检查并补齐本市场因子数据集，落后才重建）
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
                            <span className="text-xs text-gray-600">自动检查并补齐</span>
                        </Space>
                    </div>
                    <Alert
                        className="mt-2"
                        type="info"
                        showIcon
                        message={
                            <span className="text-xs">
                                独立于定时同步：检查各因子集最新分区是否落后于来源数据（K线/南向/CCASS），
                                落后才补建，追平即秒级空跑；时区 Asia/Shanghai，建议排在该市场同步时间之后。
                            </span>
                        }
                    />
                </>
            )}

            {/* 各数据集新鲜度：因子集最新分区 vs 来源最新分区 */}
            {data && Object.keys(data.freshness).length > 0 && (
                <div className="flex flex-wrap items-center gap-1 mt-2">
                    <span className="text-xs text-gray-500 mr-1">因子集新鲜度:</span>
                    {Object.entries(data.freshness).map(([name, f]) => (
                        <Tag
                            key={name}
                            className="m-0"
                            color={f.up_to_date ? 'green' : f.source_latest ? 'orange' : 'default'}
                        >
                            {DATASET_LABELS[name] ?? name} {f.latest ?? '—'}
                            {!f.up_to_date && f.source_latest ? `（来源已到 ${f.source_latest}）` : ''}
                        </Tag>
                    ))}
                </div>
            )}

            {/* 最近一次填充结果 */}
            {data?.last?.finished && (
                <div className="text-xs text-gray-500 mt-1">
                    上次填充 {dayjs(data.last.finished).format('MM-DD HH:mm')}
                    {data.last.status && (
                        <Tag
                            className="m-0 ml-1"
                            color={data.last.status === 'ok' ? 'green' : data.last.status === 'partial' ? 'orange' : 'red'}
                        >
                            {data.last.status === 'ok' ? '成功' : data.last.status === 'partial' ? '部分成功' : '失败'}
                        </Tag>
                    )}
                    {lastSummary && <span className="ml-1">{lastSummary}</span>}
                </div>
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
                    立即填充一次
                </Button>
                <Button size="small" icon={<ReloadOutlined />} onClick={loadSchedule} loading={loading}>
                    刷新
                </Button>
            </div>
        </div>
    );
};
