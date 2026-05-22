import React, { useEffect, useState } from 'react';
import { Card, Row, Col, Typography, Spin, Alert, Badge, Statistic, Button, Divider, Tooltip } from 'antd';
import { DatabaseOutlined, CloudServerOutlined, ReloadOutlined, CheckCircleOutlined, CloseCircleOutlined, SyncOutlined, ClockCircleOutlined } from '@ant-design/icons';
import { adminService } from '../services/adminService';

const { Title, Text, Paragraph } = Typography;

interface MarketSourcesStatus {
    online_source: {
        status: string;
        latest_date: string | null;
        row_count: number;
        error: string | null;
    };
    offline_source: {
        checked_at: string;
        trade_date: string;
        qlib_data: {
            exists: boolean;
            calendar_last_date: string | null;
            instruments: { total: number };
        };
        feature_snapshots: {
            exists: boolean;
            latest_date: string | null;
        };
        error?: string;
    };
}

export const AdminMarketMonitor: React.FC = () => {
    const [loading, setLoading] = useState(true);
    const [data, setData] = useState<MarketSourcesStatus | null>(null);
    const [error, setError] = useState<string | null>(null);

    const fetchData = async () => {
        setLoading(true);
        setError(null);
        try {
            // Note: Add this API method to adminService.ts if it doesn't exist.
            // For now, we'll use a direct fetch to the backend if needed, but assuming adminService will handle it.
            const response = await adminService.getMarketSourcesStatus();
            if (response.success && response.data) {
                setData(response.data as MarketSourcesStatus);
            } else {
                setError(response.message || '获取监控数据失败');
            }
        } catch (err: any) {
            setError(err.message || '请求监控接口失败');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        fetchData();
        const interval = setInterval(fetchData, 30000); // 自动刷新，每30秒
        return () => clearInterval(interval);
    }, []);

    const renderStatusBadge = (status: string, isError: boolean = false) => {
        if (isError || status === 'unreachable' || status === 'error') {
            return <Badge status="error" text="异常 / 离线" className="text-red-500 font-medium" />;
        }
        if (status === 'degraded' || status === 'empty') {
            return <Badge status="warning" text="数据不完整 / 降级" className="text-orange-500 font-medium" />;
        }
        if (status === 'checking') {
            return <Badge status="processing" text="检测中" className="text-blue-500 font-medium" />;
        }
        return <Badge status="success" text="正常 / 在线" className="text-emerald-500 font-medium" />;
    };

    if (loading && !data) {
        return (
            <div className="flex items-center justify-center h-64">
                <Spin size="large" tip="加载行情源监控数据..." />
            </div>
        );
    }

    if (error && !data) {
        return <Alert type="error" message="监控加载失败" description={error} showIcon />;
    }

    const online = data?.online_source;
    const offline = data?.offline_source;

    return (
        <div className="p-8 max-w-7xl mx-auto">
            <div className="flex justify-between items-center mb-8">
                <div>
                    <Title level={2} className="!mb-1 !text-slate-800">行情源监控</Title>
                    <Text className="text-slate-500">实时监控在线与离线数据源的健康状况与覆盖范围</Text>
                </div>
                <Button 
                    type="primary" 
                    icon={<ReloadOutlined />} 
                    onClick={fetchData} 
                    loading={loading}
                    className="bg-indigo-600 rounded-lg shadow-sm"
                >
                    手动刷新
                </Button>
            </div>

            <Row gutter={[24, 24]}>
                {/* 106 Online Source */}
                <Col xs={24} lg={12}>
                    <Card 
                        bordered={false} 
                        className="h-full rounded-2xl shadow-sm border border-slate-100 hover:shadow-md transition-shadow"
                        bodyStyle={{ padding: '24px' }}
                    >
                        <div className="flex items-start justify-between mb-6">
                            <div className="flex items-center space-x-3">
                                <div className="w-12 h-12 rounded-xl bg-blue-50 flex items-center justify-center">
                                    <CloudServerOutlined className="text-blue-600 text-2xl" />
                                </div>
                                <div>
                                    <Title level={4} className="!mb-0 !text-slate-800">在线行情源</Title>
                                    <Text className="text-xs text-slate-400 font-mono">IP: 106.53.100.144</Text>
                                </div>
                            </div>
                            <div className="px-3 py-1 bg-slate-50 rounded-lg border border-slate-100">
                                {renderStatusBadge(online?.status || 'error')}
                            </div>
                        </div>

                        <Divider className="my-4" />

                        <div className="space-y-4">
                            <div className="bg-slate-50 p-4 rounded-xl">
                                <div className="flex items-center text-slate-500 mb-1">
                                    <ClockCircleOutlined className="mr-2" />
                                    <Text className="text-xs font-bold uppercase tracking-wider">最新行情日期</Text>
                                </div>
                                <Text className="text-2xl font-black text-slate-700 font-mono">
                                    {online?.latest_date ? online.latest_date : '--'}
                                </Text>
                            </div>

                            <div className="bg-slate-50 p-4 rounded-xl">
                                <div className="flex items-center text-slate-500 mb-1">
                                    <DatabaseOutlined className="mr-2" />
                                    <Text className="text-xs font-bold uppercase tracking-wider">最新交易日数据量</Text>
                                </div>
                                <div className="flex items-baseline space-x-2">
                                    <Text className="text-2xl font-black text-slate-700 font-mono">
                                        {online?.row_count?.toLocaleString() || 0}
                                    </Text>
                                    <Text className="text-sm text-slate-400">条记录</Text>
                                </div>
                                {online?.row_count && online.row_count < 4000 ? (
                                    <Text className="text-xs text-orange-500 mt-1 block">
                                        ⚠️ 数据量偏少，可能尚未收盘或存在缺失
                                    </Text>
                                ) : null}
                            </div>
                        </div>

                        {online?.error && (
                            <Alert 
                                type="error" 
                                message="连接异常" 
                                description={online.error} 
                                showIcon 
                                className="mt-4 rounded-xl"
                            />
                        )}
                    </Card>
                </Col>

                {/* 139 Offline Source */}
                <Col xs={24} lg={12}>
                    <Card 
                        bordered={false} 
                        className="h-full rounded-2xl shadow-sm border border-slate-100 hover:shadow-md transition-shadow"
                        bodyStyle={{ padding: '24px' }}
                    >
                        <div className="flex items-start justify-between mb-6">
                            <div className="flex items-center space-x-3">
                                <div className="w-12 h-12 rounded-xl bg-purple-50 flex items-center justify-center">
                                    <DatabaseOutlined className="text-purple-600 text-2xl" />
                                </div>
                                <div>
                                    <Title level={4} className="!mb-0 !text-slate-800">离线数据资产</Title>
                                    <Text className="text-xs text-slate-400 font-mono">源: 139.199.75.121</Text>
                                </div>
                            </div>
                            <div className="px-3 py-1 bg-slate-50 rounded-lg border border-slate-100">
                                {renderStatusBadge(offline?.error ? 'error' : 'healthy')}
                            </div>
                        </div>

                        <Divider className="my-4" />

                        <div className="space-y-4">
                            <div className="bg-slate-50 p-4 rounded-xl flex justify-between items-center">
                                <div>
                                    <div className="flex items-center text-slate-500 mb-1">
                                        <SyncOutlined className="mr-2" />
                                        <Text className="text-xs font-bold uppercase tracking-wider">Qlib 二进制引擎</Text>
                                    </div>
                                    <div className="flex items-baseline space-x-2">
                                        <Text className="text-xl font-black text-slate-700 font-mono">
                                            {offline?.qlib_data?.calendar_last_date || '--'}
                                        </Text>
                                        <Text className="text-xs text-slate-400">
                                            ({offline?.qlib_data?.instruments?.total || 0} 标的)
                                        </Text>
                                    </div>
                                </div>
                                {offline?.qlib_data?.exists ? (
                                    <CheckCircleOutlined className="text-emerald-500 text-xl" />
                                ) : (
                                    <CloseCircleOutlined className="text-red-500 text-xl" />
                                )}
                            </div>

                            <div className="bg-slate-50 p-4 rounded-xl flex justify-between items-center">
                                <div>
                                    <div className="flex items-center text-slate-500 mb-1">
                                        <DatabaseOutlined className="mr-2" />
                                        <Text className="text-xs font-bold uppercase tracking-wider">AI 特征快照 (Parquet)</Text>
                                    </div>
                                    <Text className="text-xl font-black text-slate-700 font-mono">
                                        {offline?.feature_snapshots?.latest_date || '--'}
                                    </Text>
                                </div>
                                {offline?.feature_snapshots?.exists ? (
                                    <CheckCircleOutlined className="text-emerald-500 text-xl" />
                                ) : (
                                    <CloseCircleOutlined className="text-red-500 text-xl" />
                                )}
                            </div>
                        </div>

                        {offline?.error && (
                            <Alert 
                                type="error" 
                                message="读取异常" 
                                description={offline.error} 
                                showIcon 
                                className="mt-4 rounded-xl"
                            />
                        )}
                        
                        <div className="mt-4 text-right">
                            <Text className="text-[10px] text-slate-400">
                                最后检查时间: {offline?.checked_at ? new Date(offline.checked_at).toLocaleString() : '--'}
                            </Text>
                        </div>
                    </Card>
                </Col>
            </Row>
        </div>
    );
};
