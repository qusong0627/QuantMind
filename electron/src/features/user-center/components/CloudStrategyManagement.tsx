import React, { useEffect, useState } from 'react';
import { Table, Button, message, Space, Tag, Modal, Tooltip, Empty } from 'antd';
import {
    CloudOutlined,
    DeleteOutlined,
    ReloadOutlined,
    RocketOutlined,
    ExperimentOutlined,
    ExclamationCircleOutlined
} from '@ant-design/icons';
import { userCenterService } from '../services/userCenterService';
import { strategyManagementService } from '../../../services/strategyManagementService';
import { useAuth } from '../../../features/auth/hooks';
import type { UserStrategy } from '../types';

const { confirm } = Modal;

const formatCreatedAt = (raw: unknown): string => {
    if (raw === null || raw === undefined) return '--';
    const text = String(raw).trim();
    if (!text || text === '0') return '--';
    const ts = Date.parse(text);
    if (Number.isNaN(ts)) {
        const asNumber = Number(text);
        if (!Number.isFinite(asNumber) || asNumber <= 0) return '--';
        return new Date(asNumber).toLocaleString('zh-CN');
    }
    return new Date(ts).toLocaleString('zh-CN');
};

const CloudStrategyManagement: React.FC = () => {
    const { user } = useAuth();
    const [loading, setLoading] = useState(false);
    const [syncing, setSyncing] = useState(false);
    const [strategies, setStrategies] = useState<UserStrategy[]>([]);
    const [pagination, setPagination] = useState({
        current: 1,
        pageSize: 10,
        total: 0,
    });

    const fetchStrategies = async (page: number = 1, pageSize: number = 10) => {
        if (!user) return;

        setLoading(true);
        try {
            // 2026-02-14 统一架构：使用 strategyManagementService 获取
            const items = await strategyManagementService.loadStrategies(
              undefined,
              localStorage.getItem('qm:current_market') || undefined,
            );
            
            // 转换为 UserStrategy 格式以适配表格（以当前类型定义为准：使用 name 字段）
            const mapped: UserStrategy[] = items.map((item: any) => ({
                ...item,
                name: item?.name ?? item?.strategy_name ?? '未命名策略',
            }));

            setStrategies(mapped);
            setPagination({
                current: page,
                pageSize,
                total: mapped.length,
            });
        } catch (error: any) {
            console.error('获取策略列表失败:', error);
            if (error.message && !error.message.includes('404')) {
                message.error('获取策略列表失败: ' + error.message);
            }
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        fetchStrategies();
    }, [user]);

    const handleDelete = (strategy: UserStrategy) => {
        confirm({
            title: '确认删除策略?',
            icon: <ExclamationCircleOutlined />,
            content: `您确定要删除策略 "${strategy.name}" 吗？此操作不可恢复。`,
            okText: '删除',
            okType: 'danger',
            cancelText: '取消',
            onOk: async () => {
                if (!user) return;
                try {
                    await strategyManagementService.deleteStrategy(strategy.id);
                    message.success('策略已删除');
                    fetchStrategies();
                } catch (error: any) {
                    message.error('删除策略失败: ' + error.message);
                }
            },
        });
    };

    const handleSyncTemplates = async () => {
        if (!user) return;
        setSyncing(true);
        try {
            const res = await strategyManagementService.syncTemplates();
            if (res.success) {
                message.success(res.message || '模板同步成功');
                fetchStrategies(1, pagination.pageSize);
            }
        } catch (error: any) {
            message.error('同步模板失败: ' + error.message);
        } finally {
            setSyncing(false);
        }
    };

    const getStatusTag = (status: string) => {
        const statusMap: Record<string, { color: string; text: string }> = {
            draft: { color: 'default', text: '草稿' },
            repository: { color: 'blue', text: '仓库' },
            live_trading: { color: 'success', text: '运行中' },
            active: { color: 'success', text: '运行中' },
            inactive: { color: 'default', text: '停止' },
            archived: { color: 'warning', text: '已归档' },
            paused: { color: 'warning', text: '暂停' },
        };

        const config = statusMap[status] || { color: 'default', text: status };
        return <Tag color={config.color}>{config.text}</Tag>;
    };

    const getTypeIcon = (type: string) => {
        // Simple mapping based on type string, default to experiment
        if (type?.toLowerCase().includes('quantitative') || type?.toLowerCase().includes('live')) return <RocketOutlined />;
        return <ExperimentOutlined />;
    };

    const columns = [
        {
            title: '策略名称',
            dataIndex: 'name', // 修改为 name
            key: 'name',
            render: (text: string, record: UserStrategy) => (
                <Space>
                    {getTypeIcon(record.strategy_type)}
                    <span className="font-medium">{text}</span>
                </Space>
            ),
        },
        {
            title: '类型',
            dataIndex: 'strategy_type',
            key: 'strategy_type',
            render: (text: string) => <Tag>{text || '通用'}</Tag>,
        },
        {
            title: '状态',
            dataIndex: 'status',
            key: 'status',
            render: (status: string) => getStatusTag(status),
        },
        {
            title: '创建时间',
            dataIndex: 'created_at',
            key: 'created_at',
            render: (text: string) => formatCreatedAt(text),
        },
        {
            title: '操作',
            key: 'action',
            render: (_: any, record: UserStrategy) => (
                <Space size="middle">
                    <Tooltip title="删除策略">
                        <Button
                            type="text"
                            danger
                            icon={<DeleteOutlined />}
                            onClick={() => handleDelete(record)}
                        />
                    </Tooltip>
                </Space>
            ),
        },
    ];

    return (
        <div className="p-0">
            <div className="flex justify-between items-center mb-6">
                <div>
                    <h2 className="text-lg font-medium flex items-center gap-2">
                        <CloudOutlined className="text-blue-500" />
                        云端策略管理
                    </h2>
                    <p className="text-gray-500 text-sm mt-1">管理您存储在云端的量化策略代码和配置</p>
                </div>
                <Space>
                    <Button
                        icon={<CloudOutlined />}
                        onClick={handleSyncTemplates}
                        loading={syncing}
                    >
                        同步模板
                    </Button>
                    <Button
                        icon={<ReloadOutlined />}
                        onClick={() => fetchStrategies(pagination.current, pagination.pageSize)}
                        loading={loading}
                    >
                        刷新
                    </Button>
                </Space>
            </div>

            <Table
                columns={columns}
                dataSource={strategies}
                rowKey="id"
                loading={loading}
                pagination={{
                    ...pagination,
                    onChange: (page, pageSize) => fetchStrategies(page, pageSize),
                    showSizeChanger: true,
                    showTotal: (total) => `共 ${total} 个策略`,
                }}
                locale={{
                    emptyText: (
                        <Empty
                            image={Empty.PRESENTED_IMAGE_SIMPLE}
                            description="暂无云端策略"
                        />
                    ),
                }}
            />
        </div>
    );
};

export default CloudStrategyManagement;
