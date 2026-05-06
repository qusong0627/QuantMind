import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Col, Descriptions, Input, Row, Space, Spin, Statistic, Table, Tag, message, Typography, Progress, Divider, Tooltip, Empty } from 'antd';
import { 
    DatabaseOutlined, 
    ReloadOutlined, 
    CloudSyncOutlined, 
    CheckCircleFilled, 
    WarningFilled, 
    FileTextOutlined,
    ThunderboltOutlined,
    CompassOutlined,
    LineChartOutlined,
    InfoCircleOutlined,
    CodeOutlined,
    SafetyCertificateOutlined,
    UserOutlined
} from '@ant-design/icons';
import dayjs from 'dayjs';
import { adminService } from '../services/adminService';
import {
    AdminFeatureSnapshotsOlderSample,
    AdminFeatureSnapshotsInvalidSample,
    AdminDataStatusResult,
    AdminOfficialDataUpdateSyncResult,
} from '../types';

const { Title, Text, Paragraph } = Typography;

export const AdminDataManagement: React.FC = () => {
    const [loading, setLoading] = useState(false);
    const [data, setData] = useState<AdminDataStatusResult | null>(null);
    const [syncLoading, setSyncLoading] = useState(false);
    const [syncResult, setSyncResult] = useState<AdminOfficialDataUpdateSyncResult | null>(null);
    const [apiBaseUrl, setApiBaseUrl] = useState('https://api.quantmind.cloud/api/v1');
    const [accessKey, setAccessKey] = useState('');
    const [secretKey, setSecretKey] = useState('');
    const [version, setVersion] = useState('');
    const [configScript, setConfigScript] = useState('');

    const loadDataStatus = async (refresh = false) => {
        setLoading(true);
        try {
            const resp = await adminService.getDataStatus(refresh);
            setData(resp);
            if (refresh) {
                message.success(resp.message || '后台扫描任务已启动，请稍后刷新查看最新状态');
            }
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '未知错误';
            message.error(`数据状态同步失败: ${msg}`);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        loadDataStatus();
    }, []);

    const qlib = data?.qlib_data;
    const snapshots = data?.feature_snapshots;
    const checkedAt = data?.checked_at ? dayjs(data.checked_at).format('HH:mm:ss') : '—';
    const olderSamples = snapshots?.topn_samples?.older_samples || [];
    const invalidSamples = snapshots?.topn_samples?.invalid_samples || [];
    const sampleSize = snapshots?.topn_samples?.sample_size || 20;

    const coverageRate = useMemo(() => {
        const c = snapshots?.latest_date_coverage;
        if (!c) return 0;
        const total = c.at_target_count + c.older_count + c.invalid_count;
        if (total <= 0) return 0;
        return Math.round((c.at_target_count / total) * 10000) / 100;
    }, [snapshots]);

    const olderColumns = [
        {
            title: '股票代码',
            dataIndex: 'symbol',
            key: 'symbol',
            width: 100,
            render: (v: string) => <span className="font-mono font-black text-indigo-600">{v}</span>,
        },
        {
            title: '最新日期',
            dataIndex: 'last_date',
            key: 'last_date',
            width: 120,
            render: (v: string) => <Text className="font-mono text-slate-500">{v}</Text>
        },
        {
            title: '滞后天数',
            dataIndex: 'lag_days',
            key: 'lag_days',
            width: 100,
            align: 'right' as const,
            render: (v: number) => (
                <Tag color={v > 60 ? '#f43f5e' : v > 10 ? '#f59e0b' : '#10b981'} className="m-0 border-none font-bold rounded-lg px-2">
                    {v}天
                </Tag>
            ),
        },
    ];

    const invalidColumns = [
        {
            title: '股票代码',
            dataIndex: 'symbol',
            key: 'symbol',
            width: 100,
            render: (v: string) => <span className="font-mono font-black text-rose-600">{v}</span>,
        },
        {
            title: '原因',
            dataIndex: 'reason',
            key: 'reason',
            render: (v: string) => <Tag color="error" className="m-0 border-none rounded-md px-2 text-[11px] font-bold uppercase tracking-tight">{v}</Tag>,
        },
        {
            title: '文件路径',
            dataIndex: 'file',
            key: 'file',
            ellipsis: true as const,
            render: (v?: string) => <Text className="text-slate-400 font-mono text-[10px] italic">{v || '—'}</Text>,
        },
    ];

    const handleGenerateScript = () => {
        const lines = [
            '#!/usr/bin/env bash',
            'set -e',
            '# QuantMind Data Sync Script',
            `export OFFICIAL_DATA_API_URL="${apiBaseUrl.trim()}"`,
            `export ACCESS_KEY="${accessKey.trim()}"`,
            `export SECRET_KEY="${secretKey.trim()}"`,
            '',
            'python backend/scripts/sync_official_data_update.py \\',
            `  --api-base-url "${apiBaseUrl.trim()}" \\`,
            `  --access-key "${accessKey.trim()}" \\`,
            `  --secret-key "${secretKey.trim()}"${version.trim() ? ` \\ \n  --version "${version.trim()}"` : ''}`,
        ];
        setConfigScript(lines.join('\n'));
        message.success('脚本配置已生成');
    };

    const handleSyncOfficialData = async () => {
        if (!accessKey.trim() || !secretKey.trim()) {
            message.warning('鉴权凭证（Access/Secret Key）不能为空');
            return;
        }
        setSyncLoading(true);
        try {
            const resp = await adminService.syncOfficialDataUpdate({
                apiBaseUrl: apiBaseUrl.trim(),
                accessKey: accessKey.trim(),
                secretKey: secretKey.trim(),
                version: version.trim() || undefined,
            });
            setSyncResult(resp);
            if (resp.success) {
                message.success('数据全自动增量同步已启动');
                await loadDataStatus(true);
            } else {
                message.error(resp.error || '同步任务执行异常');
            }
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '未知网络错误';
            message.error(`同步失败: ${msg}`);
        } finally {
            setSyncLoading(false);
        }
    };

    return (
        <div className="space-y-10 animate-in fade-in slide-in-from-bottom-4 duration-700">
            {/* Header Section */}
            <div className="flex flex-col lg:flex-row justify-between items-start lg:items-center gap-6">
                <div>
                    <Title level={1} className="!m-0 !font-black !text-4xl !tracking-tighter !text-slate-900 uppercase">
                        数据管理
                    </Title>
                    <div className="flex items-center mt-2 space-x-3">
                        <Tag className="rounded-full bg-slate-100 border-none text-slate-500 font-bold px-3">
                            节点: QUANT-OSS-01
                        </Tag>
                        <Text className="text-slate-400 font-medium text-sm flex items-center">
                            <InfoCircleOutlined className="mr-1.5" />
                            最后扫描时间: <span className="text-indigo-500 font-bold ml-1">{checkedAt}</span>
                        </Text>
                    </div>
                </div>
                <Space size="middle">
                    <Button
                        type="primary"
                        icon={<ThunderboltOutlined />}
                        className="rounded-2xl h-11 px-8 bg-indigo-600 border-none font-bold shadow-lg shadow-indigo-100"
                        loading={loading}
                        onClick={() => loadDataStatus(true)}
                    >
                        强制深度扫描
                    </Button>
                    <Button
                        icon={<ReloadOutlined />}
                        className="rounded-2xl h-11 px-8 border-slate-200 text-slate-600 font-bold hover:bg-slate-50 transition-all"
                        loading={loading}
                        onClick={() => loadDataStatus(false)}
                    >
                        刷新
                    </Button>
                </Space>
            </div>

            {/* Quick Stats Grid */}
            <Row gutter={[24, 24]}>
                <Col xs={24} sm={12} lg={6}>
                    <Card className="rounded-[2rem] border-none shadow-xl shadow-slate-200/40 bg-white group overflow-hidden">
                        <div className="absolute top-0 right-0 w-24 h-24 bg-blue-500 opacity-[0.03] rounded-bl-[4rem]" />
                        <Statistic 
                            title={<span className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Qlib 日历最后日期</span>} 
                            value={qlib?.calendar_last_date || '—'} 
                            valueStyle={{ fontWeight: 900, letterSpacing: '-0.02em', color: '#1e293b' }}
                            prefix={<CompassOutlined className="text-blue-500 mr-2" />}
                        />
                    </Card>
                </Col>
                <Col xs={24} sm={12} lg={6}>
                    <Card className="rounded-[2rem] border-none shadow-xl shadow-slate-200/40 bg-white group overflow-hidden">
                        <div className="absolute top-0 right-0 w-24 h-24 bg-indigo-500 opacity-[0.03] rounded-bl-[4rem]" />
                        <Statistic 
                            title={<span className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">快照最新日期</span>} 
                            value={snapshots?.max_date || '—'} 
                            valueStyle={{ fontWeight: 900, letterSpacing: '-0.02em', color: '#1e293b' }}
                            prefix={<LineChartOutlined className="text-indigo-500 mr-2" />}
                        />
                    </Card>
                </Col>
                <Col xs={24} sm={12} lg={6}>
                    <Card className="rounded-[2rem] border-none shadow-xl shadow-slate-200/40 bg-white group overflow-hidden">
                        <div className="absolute top-0 right-0 w-24 h-24 bg-emerald-500 opacity-[0.03] rounded-bl-[4rem]" />
                        <Statistic 
                            title={<span className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Parquet 文件总数</span>} 
                            value={snapshots?.file_count ?? 0} 
                            suffix="个"
                            valueStyle={{ fontWeight: 900, letterSpacing: '-0.02em', color: '#1e293b' }}
                            prefix={<DatabaseOutlined className="text-emerald-500 mr-2" />}
                        />
                    </Card>
                </Col>
                <Col xs={24} sm={12} lg={6}>
                    <Card className="rounded-[2rem] border-none shadow-xl shadow-slate-200/40 bg-white group overflow-hidden">
                        <div className="flex flex-col">
                            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-2">覆盖率</span>
                            <div className="flex items-center space-x-4">
                                <Progress 
                                    type="circle" 
                                    percent={coverageRate} 
                                    size={48} 
                                    strokeWidth={12}
                                    strokeColor={{ '0%': '#6366f1', '100%': '#10b981' }}
                                    format={() => <span className="text-[10px] font-black text-slate-700">{Math.round(coverageRate)}%</span>}
                                />
                                <div>
                                    <div className="text-2xl font-black text-slate-800 tracking-tight">{coverageRate}%</div>
                                    <div className="text-[10px] font-bold text-emerald-500">良好</div>
                                </div>
                            </div>
                        </div>
                    </Card>
                </Col>
            </Row>

            {/* Main Content Area */}
            <Row gutter={[32, 32]}>
                <Col span={24} lg={15} className="space-y-8">
                    {/* Qlib Section */}
                    <Card
                        title={
                            <div className="flex items-center space-x-3 py-1">
                                <div className="w-8 h-8 rounded-lg bg-blue-50 flex items-center justify-center text-blue-600">
                                    <DatabaseOutlined />
                                </div>
                                <span className="font-black text-slate-800 tracking-tight text-lg uppercase">Qlib 基础设施详情</span>
                            </div>
                        }
                        className="rounded-[2.5rem] border-none shadow-2xl shadow-slate-200/30"
                        styles={{ body: { padding: '32px' } }}
                    >
                        {!qlib?.exists ? (
                            <Alert
                                type="error"
                                showIcon
                                message={<span className="font-bold">Qlib 目录不存在</span>}
                                description={<span className="text-xs italic opacity-70">{qlib?.qlib_dir || '路径未定义'}</span>}
                                className="rounded-2xl"
                            />
                        ) : (
                            <div className="grid grid-cols-2 md:grid-cols-3 gap-y-8 gap-x-12">
                                {[
                                    { label: 'Qlib 路径', value: qlib.qlib_dir, span: 3, full: true },
                                    { label: '日历总天数', value: qlib.calendar_total_days },
                                    { label: '日历区间', value: `${qlib.calendar_start_date} → ${qlib.calendar_last_date}`, span: 2 },
                                    { label: '标的总数', value: qlib.instruments?.total, highlight: true },
                                    { label: '特征目录数', value: qlib.feature_dirs_total },
                                    { label: '交易所分布', value: `SH: ${qlib.instruments?.sh} | SZ: ${qlib.instruments?.sz} | BJ: ${qlib.instruments?.bj}`, span: 3, italic: true }
                                ].map((item, i) => (
                                    <div key={i} className={`flex flex-col space-y-1 ${item.span === 3 ? 'col-span-full' : item.span === 2 ? 'col-span-2' : ''}`}>
                                        <Text className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">{item.label}</Text>
                                        <Text className={`text-slate-800 ${item.full ? 'font-mono text-xs break-all' : 'font-black text-lg'} ${item.highlight ? 'text-indigo-600' : ''} ${item.italic ? 'italic text-slate-500' : ''}`}>
                                            {item.value ?? '—'}
                                        </Text>
                                    </div>
                                ))}
                            </div>
                        )}
                    </Card>

                    {/* Snapshots Section */}
                    <Card
                        title={
                            <div className="flex items-center space-x-3 py-1">
                                <div className="w-8 h-8 rounded-lg bg-indigo-50 flex items-center justify-center text-indigo-600">
                                    <FileTextOutlined />
                                </div>
                                <span className="font-black text-slate-800 tracking-tight text-lg uppercase">特征快照分析</span>
                            </div>
                        }
                        className="rounded-[2.5rem] border-none shadow-2xl shadow-slate-200/30"
                        styles={{ body: { padding: '32px' } }}
                    >
                        {!snapshots?.exists ? (
                            <Empty description="暂无快照数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                        ) : (
                            <div className="space-y-10">
                                <div className="grid grid-cols-2 md:grid-cols-4 gap-y-8">
                                    {[
                                        { label: '总行数', value: snapshots.total_rows?.toLocaleString(), color: 'text-indigo-600' },
                                        { label: '扫描成功', value: snapshots.scanned_files, color: 'text-emerald-500' },
                                        { label: '扫描失败', value: snapshots.failed_files, color: 'text-rose-500' },
                                        { label: '数据完整性', value: snapshots.error ? '异常' : '正常', color: snapshots.error ? 'text-rose-500' : 'text-emerald-500' }
                                    ].map((item, i) => (
                                        <div key={i} className="flex flex-col">
                                            <Text className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">{item.label}</Text>
                                            <Text className={`font-black text-xl tracking-tighter ${item.color}`}>{item.value ?? '—'}</Text>
                                        </div>
                                    ))}
                                </div>

                                {snapshots.suggested_periods && (
                                    <div className="p-6 rounded-3xl bg-slate-50 border border-slate-100">
                                        <div className="flex items-center space-x-2 mb-4">
                                            <CompassOutlined className="text-slate-400" />
                                            <span className="text-xs font-black text-slate-600 uppercase tracking-widest">推荐训练区间</span>
                                        </div>
                                        <div className="flex flex-wrap gap-4">
                                            {Object.entries(snapshots.suggested_periods).map(([key, period]: [string, any]) => (
                                                <div key={key} className="flex-1 min-w-[140px] p-4 bg-white rounded-2xl shadow-sm border border-slate-100">
                                                    <Text className="text-[10px] font-bold text-slate-400 uppercase block mb-1">{key} 集</Text>
                                                    <Text className="font-mono text-[11px] font-black text-slate-700">{period[0]} ~ {period[1]}</Text>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                )}
                            </div>
                        )}
                    </Card>
                </Col>

                <Col span={24} lg={9} className="space-y-8">
                    {/* Sync Panel */}
                    <Card
                        className="rounded-[2.5rem] border-none shadow-xl shadow-slate-200/40 bg-white"
                        styles={{ body: { padding: '32px' } }}
                    >
                        <div className="flex items-center justify-between mb-8">
                            <div className="flex items-center space-x-3">
                                <div className="w-10 h-10 rounded-xl bg-indigo-50 flex items-center justify-center">
                                    <CloudSyncOutlined className="text-indigo-600 text-xl" />
                                </div>
                                <span className="text-slate-800 font-black text-xl uppercase tracking-tight">同步引擎</span>
                            </div>
                            <Tooltip title="安全连接已激活">
                                <Tag className="m-0 bg-emerald-50 text-emerald-600 border-none rounded-full px-3 py-0.5 text-[10px] font-bold">安全</Tag>
                            </Tooltip>
                        </div>

                        <div className="space-y-5">
                            <div className="space-y-1.5">
                                <Text className="text-[10px] font-bold text-slate-400 uppercase tracking-widest ml-1">Access Key</Text>
                                <Input 
                                    prefix={<UserOutlined className="text-indigo-400/50" />}
                                    value={accessKey} 
                                    onChange={e => setAccessKey(e.target.value)} 
                                    className="h-12 bg-slate-50 border-slate-100 rounded-xl text-slate-700 font-mono text-sm focus:bg-white transition-all" 
                                    placeholder="qm_live_..."
                                />
                            </div>
                            <div className="space-y-1.5">
                                <Text className="text-[10px] font-bold text-slate-400 uppercase tracking-widest ml-1">Secret Key</Text>
                                <Input.Password 
                                    prefix={<SafetyCertificateOutlined className="text-indigo-400/50" />}
                                    value={secretKey} 
                                    onChange={e => setSecretKey(e.target.value)} 
                                    className="h-12 bg-slate-50 border-slate-100 rounded-xl text-slate-700 font-mono text-sm focus:bg-white transition-all" 
                                    placeholder="••••••••••••"
                                />
                            </div>
                            <Button 
                                type="primary" 
                                block 
                                className="h-12 rounded-xl bg-indigo-600 hover:bg-indigo-700 border-none font-black text-base shadow-lg shadow-indigo-100 mt-4 transition-all"
                                loading={syncLoading}
                                onClick={handleSyncOfficialData}
                            >
                                启动数据同步
                            </Button>
                            <div className="flex justify-center space-x-4">
                                <Button type="link" onClick={handleGenerateScript} className="text-indigo-600 font-bold text-xs hover:text-indigo-700 uppercase tracking-widest px-0">
                                    <CodeOutlined className="mr-1" /> 生成配置脚本
                                </Button>
                            </div>
                        </div>

                        {configScript && (
                            <div className="mt-8 relative animate-in fade-in slide-in-from-top-2">
                                <div className="absolute top-3 right-3 z-10">
                                    <Tag className="bg-slate-200/50 border-none text-slate-500 text-[9px] font-black uppercase">Shell 脚本</Tag>
                                </div>
                                <Input.TextArea
                                    className="bg-slate-50 border-slate-100 rounded-2xl text-indigo-700 font-mono text-[11px] leading-relaxed p-6"
                                    rows={8}
                                    value={configScript}
                                    readOnly
                                />
                            </div>
                        )}
                    </Card>

                    {/* Issue Tracker cards */}
                    {snapshots?.exists && (olderSamples.length > 0 || invalidSamples.length > 0) && (
                        <div className="space-y-6">
                            <Card 
                                title={<span className="font-black text-rose-500 tracking-tight uppercase text-sm flex items-center"><WarningFilled className="mr-2" /> 数据滞后（Top {sampleSize}）</span>}
                                className="rounded-3xl border-none shadow-xl shadow-slate-200/20"
                                styles={{ body: { padding: '0 12px 12px' } }}
                            >
                                <Table<AdminFeatureSnapshotsOlderSample>
                                    size="small"
                                    pagination={false}
                                    rowKey={(r) => `${r.symbol}-${r.last_date}`}
                                    dataSource={olderSamples}
                                    columns={olderColumns}
                                    className="custom-table"
                                    locale={{ emptyText: '无滞后数据' }}
                                    scroll={{ y: 240 }}
                                />
                            </Card>
                            <Card 
                                title={<span className="font-black text-slate-400 tracking-tight uppercase text-sm flex items-center"><InfoCircleOutlined className="mr-2" /> 无效文件</span>}
                                className="rounded-3xl border-none shadow-xl shadow-slate-200/20"
                                styles={{ body: { padding: '0 12px 12px' } }}
                            >
                                <Table<AdminFeatureSnapshotsInvalidSample>
                                    size="small"
                                    pagination={false}
                                    rowKey={(r) => `${r.symbol}-${r.reason}-${r.file || ''}`}
                                    dataSource={invalidSamples}
                                    columns={invalidColumns}
                                    className="custom-table"
                                    locale={{ emptyText: '所有文件正常' }}
                                    scroll={{ y: 240 }}
                                />
                            </Card>
                        </div>
                    )}
                </Col>
            </Row>
        </div>
    );
};
