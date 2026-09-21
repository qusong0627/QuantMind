import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Button,
    Drawer,
    Form,
    Input,
    InputNumber,
    Popconfirm,
    Select,
    Space,
    Switch,
    Table,
    Tag,
    Typography,
    message,
} from 'antd';
import {
    ExperimentOutlined,
    PlusOutlined,
    ReloadOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { adminService } from '../services/adminService';
import type { RiskEventAdmin, RiskRuleAdmin } from '../types';
import { isLiveTradingEnabled } from '../../../config/tradingFlags';

const { Title, Text } = Typography;

const RULE_TYPE_OPTIONS = [
    { label: '单股止损', value: 'position_stop_loss' },
    { label: '单股止盈', value: 'position_take_profit' },
    { label: '全市场指数条件', value: 'market_index_move' },
    { label: '单笔上限（闸门）', value: 'max_order_size' },
    { label: '持仓占比上限（闸门）', value: 'max_position_size' },
    { label: '日内笔数上限（闸门）', value: 'max_daily_trades' },
];

const RULE_TYPE_LABEL: Record<string, string> = Object.fromEntries(
    RULE_TYPE_OPTIONS.map((item) => [item.value, item.label]),
);

const STATUS_COLOR: Record<string, string> = {
    filled: 'green',
    dry_run: 'blue',
    alert_only: 'gold',
    skipped_t1: 'orange',
    skipped_no_quote: 'default',
    skipped_dedup: 'default',
    skipped_empty: 'default',
    failed: 'red',
    pending: 'processing',
};

const isTriggerType = (ruleType: string) =>
    ['position_stop_loss', 'position_take_profit', 'market_index_move'].includes(ruleType);

const pctToForm = (value: unknown) =>
    typeof value === 'number' ? Number((value * 100).toFixed(4)) : undefined;

const formatPct = (value?: number | null) =>
    typeof value === 'number' ? `${(value * 100).toFixed(2)}%` : '-';

interface RuleFormValues {
    rule_name: string;
    rule_type: string;
    description?: string;
    is_active: boolean;
    applies_to_all: boolean;
    user_ids_text?: string;
    trading_mode: string;
    pct?: number;
    index?: string;
    max_value?: number;
    max_percentage?: number;
    max_count?: number;
    priority: number;
}

const defaultForm: Partial<RuleFormValues> = {
    rule_type: 'position_stop_loss',
    is_active: true,
    applies_to_all: true,
    trading_mode: 'SIMULATION',
    pct: -8,
    index: '000300.SH',
    priority: 10,
};

function buildParameters(values: RuleFormValues): Record<string, any> {
    const parameters: Record<string, any> = {
        trading_mode: values.trading_mode,
        markets: ['CN'],
    };
    if (values.rule_type === 'position_stop_loss' || values.rule_type === 'position_take_profit') {
        parameters.pct = Number(values.pct) / 100;
    } else if (values.rule_type === 'market_index_move') {
        parameters.pct = Number(values.pct) / 100;
        parameters.index = values.index || '000300.SH';
    } else if (values.rule_type === 'max_order_size') {
        parameters.max_value = values.max_value;
    } else if (values.rule_type === 'max_position_size') {
        parameters.max_percentage = Number(values.max_percentage) / 100;
    } else if (values.rule_type === 'max_daily_trades') {
        parameters.max_count = values.max_count;
    }
    return parameters;
}

function parseUserIds(text?: string): number[] | null {
    if (!text?.trim()) return null;
    const ids = text
        .split(/[,，\s]+/)
        .map((item) => Number(item.trim()))
        .filter((item) => Number.isFinite(item));
    return ids.length ? ids : null;
}

export const AdminRiskControl: React.FC = () => {
    const [rules, setRules] = useState<RiskRuleAdmin[]>([]);
    const [events, setEvents] = useState<RiskEventAdmin[]>([]);
    const [loading, setLoading] = useState(false);
    const [drawerOpen, setDrawerOpen] = useState(false);
    const [editing, setEditing] = useState<RiskRuleAdmin | null>(null);
    const [form] = Form.useForm<RuleFormValues>();
    const ruleType = Form.useWatch('rule_type', form);

    const loadAll = useCallback(async () => {
        setLoading(true);
        try {
            const [ruleRows, eventRows] = await Promise.all([
                adminService.listRiskRules(false),
                adminService.listRiskEvents({ limit: 50 }),
            ]);
            setRules(ruleRows || []);
            setEvents(eventRows || []);
        } catch (error: any) {
            message.error(error?.response?.data?.detail || error?.message || '加载风控数据失败');
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void loadAll();
    }, [loadAll]);

    const openCreate = () => {
        setEditing(null);
        form.setFieldsValue(defaultForm);
        setDrawerOpen(true);
    };

    const openEdit = (rule: RiskRuleAdmin) => {
        setEditing(rule);
        const params = rule.parameters || {};
        form.setFieldsValue({
            rule_name: rule.rule_name,
            rule_type: rule.rule_type,
            description: rule.description || '',
            is_active: rule.is_active,
            applies_to_all: rule.applies_to_all,
            user_ids_text: (rule.user_ids || []).join(','),
            trading_mode: params.trading_mode || 'SIMULATION',
            pct: pctToForm(params.pct),
            index: params.index || '000300.SH',
            max_value: params.max_value,
            max_percentage: pctToForm(params.max_percentage),
            max_count: params.max_count,
            priority: rule.priority,
        });
        setDrawerOpen(true);
    };

    const submitRule = async () => {
        const values = await form.validateFields();
        const payload = {
            rule_name: values.rule_name,
            rule_type: values.rule_type,
            description: values.description,
            is_active: values.is_active,
            applies_to_all: values.applies_to_all,
            user_ids: values.applies_to_all ? null : parseUserIds(values.user_ids_text),
            priority: values.priority,
            parameters: buildParameters(values),
        };
        try {
            if (editing) {
                await adminService.updateRiskRule(editing.id, payload);
                message.success('规则已更新');
            } else {
                await adminService.createRiskRule(payload);
                message.success('规则已创建');
            }
            setDrawerOpen(false);
            await loadAll();
        } catch (error: any) {
            message.error(error?.response?.data?.detail || error?.message || '保存失败');
        }
    };

    const toggleRule = async (rule: RiskRuleAdmin, is_active: boolean) => {
        try {
            await adminService.updateRiskRule(rule.id, { is_active });
            await loadAll();
        } catch (error: any) {
            message.error(error?.response?.data?.detail || error?.message || '更新失败');
        }
    };

    const removeRule = async (rule: RiskRuleAdmin) => {
        try {
            await adminService.deleteRiskRule(rule.id);
            message.success('已删除');
            await loadAll();
        } catch (error: any) {
            message.error(error?.response?.data?.detail || error?.message || '删除失败');
        }
    };

    const runDryRun = async (rule: RiskRuleAdmin) => {
        const raw = window.prompt('预演用户 ID（模拟盘）', String((rule.user_ids || [0])[0] ?? 0));
        if (raw == null) return;
        const userId = Number(raw);
        if (!Number.isFinite(userId)) {
            message.error('用户 ID 无效');
            return;
        }
        try {
            const rows = await adminService.dryRunRiskRule(rule.id, { user_id: userId, tenant_id: 'default' });
            if (!rows?.length) {
                message.info('该用户当前不会触发此规则');
                return;
            }
            message.success(`预演 ${rows.length} 条：${rows.map((item) => `${item.symbol} ${item.status}`).join('；')}`);
            await loadAll();
        } catch (error: any) {
            message.error(error?.response?.data?.detail || error?.message || '预演失败');
        }
    };

    const ruleColumns: ColumnsType<RiskRuleAdmin> = useMemo(
        () => [
            { title: '名称', dataIndex: 'rule_name', width: 180 },
            {
                title: '类型',
                dataIndex: 'rule_type',
                width: 160,
                render: (value: string) => (
                    <Tag color={isTriggerType(value) ? 'red' : 'default'}>
                        {RULE_TYPE_LABEL[value] || value}
                    </Tag>
                ),
            },
            {
                title: '阈值',
                width: 180,
                render: (_, rule) => {
                    const params = rule.parameters || {};
                    if (typeof params.pct === 'number') {
                        const index = params.index ? ` ${params.index}` : '';
                        return `${formatPct(params.pct)}${index}`;
                    }
                    if (params.max_value) return `¥${params.max_value}`;
                    if (params.max_percentage) return formatPct(params.max_percentage);
                    if (params.max_count) return `${params.max_count} 笔`;
                    return '-';
                },
            },
            {
                title: '范围',
                width: 140,
                render: (_, rule) =>
                    rule.applies_to_all ? '全局' : `用户 ${(rule.user_ids || []).join(',') || '-'}`,
            },
            {
                title: '模式',
                width: 110,
                render: (_, rule) => rule.parameters?.trading_mode || 'SIMULATION',
            },
            {
                title: '启用',
                width: 80,
                render: (_, rule) => (
                    <Switch checked={rule.is_active} onChange={(checked) => void toggleRule(rule, checked)} />
                ),
            },
            {
                title: '操作',
                width: 220,
                render: (_, rule) => (
                    <Space>
                        <Button type="link" size="small" onClick={() => openEdit(rule)}>
                            编辑
                        </Button>
                        {isTriggerType(rule.rule_type) && (
                            <Button
                                type="link"
                                size="small"
                                icon={<ExperimentOutlined />}
                                onClick={() => void runDryRun(rule)}
                            >
                                预演
                            </Button>
                        )}
                        <Popconfirm title="确认删除该规则？" onConfirm={() => void removeRule(rule)}>
                            <Button type="link" size="small" danger>
                                删除
                            </Button>
                        </Popconfirm>
                    </Space>
                ),
            },
        ],
        [loadAll],
    );

    const eventColumns: ColumnsType<RiskEventAdmin> = [
        { title: '时间', dataIndex: 'created_at', width: 180, render: (value: string) => value?.replace('T', ' ').slice(0, 19) },
        { title: '用户', dataIndex: 'user_id', width: 80 },
        { title: '类型', dataIndex: 'rule_type', width: 150, render: (value: string) => RULE_TYPE_LABEL[value] || value },
        { title: '标的', dataIndex: 'symbol', width: 110 },
        {
            title: '状态',
            dataIndex: 'status',
            width: 120,
            render: (value: string) => <Tag color={STATUS_COLOR[value] || 'default'}>{value}</Tag>,
        },
        { title: '涨跌', dataIndex: 'pnl_pct', width: 90, render: formatPct },
        { title: '数量', dataIndex: 'quantity', width: 80 },
        { title: '说明', dataIndex: 'message', ellipsis: true },
    ];

    return (
        <div className="p-6 space-y-6 overflow-auto h-full">
            <div className="flex items-start justify-between gap-4">
                <div>
                    <Title level={4} className="!mb-1">
                        风险控制
                    </Title>
                    <Text type="secondary">
                        触发规则独立扫描（默认 60 秒），高于策略调仓窗口；触及后模拟盘自动平仓并当日禁买。实盘仅告警。
                    </Text>
                </div>
                <Space>
                    <Button icon={<ReloadOutlined />} onClick={() => void loadAll()}>
                        刷新
                    </Button>
                    <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                        新建规则
                    </Button>
                </Space>
            </div>

            <Table
                rowKey="id"
                loading={loading}
                columns={ruleColumns}
                dataSource={rules}
                pagination={false}
                size="middle"
            />

            <div>
                <Title level={5}>最近触发</Title>
                <Table
                    rowKey="id"
                    loading={loading}
                    columns={eventColumns}
                    dataSource={events}
                    pagination={false}
                    size="small"
                />
            </div>

            <Drawer
                title={editing ? '编辑风控规则' : '新建风控规则'}
                open={drawerOpen}
                width={460}
                onClose={() => setDrawerOpen(false)}
                footer={
                    <div className="flex justify-end gap-2">
                        <Button onClick={() => setDrawerOpen(false)}>关闭</Button>
                        <Button type="primary" onClick={() => void submitRule()}>
                            保存
                        </Button>
                    </div>
                }
            >
                <Form form={form} layout="vertical" initialValues={defaultForm}>
                    <Form.Item name="rule_name" label="名称" rules={[{ required: true, message: '请输入名称' }]}>
                        <Input maxLength={100} />
                    </Form.Item>
                    <Form.Item name="rule_type" label="类型" rules={[{ required: true }]}>
                        <Select options={RULE_TYPE_OPTIONS} />
                    </Form.Item>
                    <Form.Item name="description" label="说明">
                        <Input.TextArea rows={2} maxLength={500} />
                    </Form.Item>
                    <Form.Item name="trading_mode" label="交易模式">
                        <Select
                            options={[
                                { label: '仅模拟盘', value: 'SIMULATION' },
                                ...(isLiveTradingEnabled()
                                    ? [
                                        { label: '仅实盘告警', value: 'REAL' },
                                        { label: '模拟+实盘告警', value: 'BOTH' },
                                    ]
                                    : []),
                            ]}
                        />
                    </Form.Item>
                    {(ruleType === 'position_stop_loss' ||
                        ruleType === 'position_take_profit' ||
                        ruleType === 'market_index_move') && (
                        <Form.Item
                            name="pct"
                            label={ruleType === 'position_take_profit' ? '止盈 (%)' : '阈值 (%)'}
                            extra={
                                ruleType === 'position_stop_loss'
                                    ? '相对成本，例如 -8 表示跌 8% 平该标的'
                                    : ruleType === 'market_index_move'
                                      ? '指数当日涨跌幅，例如 -3 表示跌 3% 平全部可卖仓'
                                      : '相对成本，例如 15 表示涨 15% 平该标的'
                            }
                            rules={[{ required: true, message: '请输入阈值' }]}
                        >
                            <InputNumber className="w-full" />
                        </Form.Item>
                    )}
                    {ruleType === 'market_index_move' && (
                        <Form.Item name="index" label="指数代码">
                            <Input placeholder="000300.SH" />
                        </Form.Item>
                    )}
                    {ruleType === 'max_order_size' && (
                        <Form.Item name="max_value" label="单笔最大金额">
                            <InputNumber className="w-full" min={0} />
                        </Form.Item>
                    )}
                    {ruleType === 'max_position_size' && (
                        <Form.Item name="max_percentage" label="单票仓位上限 (%)">
                            <InputNumber className="w-full" min={1} max={100} />
                        </Form.Item>
                    )}
                    {ruleType === 'max_daily_trades' && (
                        <Form.Item name="max_count" label="日内最大笔数">
                            <InputNumber className="w-full" min={1} />
                        </Form.Item>
                    )}
                    <Form.Item name="applies_to_all" label="全局生效" valuePropName="checked">
                        <Switch />
                    </Form.Item>
                    <Form.Item noStyle shouldUpdate={(prev, next) => prev.applies_to_all !== next.applies_to_all}>
                        {({ getFieldValue }) =>
                            getFieldValue('applies_to_all') ? null : (
                                <Form.Item name="user_ids_text" label="用户 ID" rules={[{ required: true }]}>
                                    <Input placeholder="逗号分隔，例如 0,1001" />
                                </Form.Item>
                            )
                        }
                    </Form.Item>
                    <Form.Item name="priority" label="优先级">
                        <InputNumber className="w-full" min={0} max={100} />
                    </Form.Item>
                    <Form.Item name="is_active" label="启用" valuePropName="checked">
                        <Switch />
                    </Form.Item>
                </Form>
            </Drawer>
        </div>
    );
};
