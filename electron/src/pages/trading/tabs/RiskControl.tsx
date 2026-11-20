/** 模拟盘 Tab：风险控制（用户级，仅管理本人规则） */

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
    message,
} from 'antd';
import {
    ExperimentOutlined,
    PlusOutlined,
    ReloadOutlined,
} from '@ant-design/icons';
import { ShieldCheck } from 'lucide-react';
import type { ColumnsType } from 'antd/es/table';
import { riskControlService } from '../../../services/riskControlService';
import type { RiskEventAdmin, RiskRuleAdmin } from '../../../features/admin/types';

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
    pct: -8,
    index: '000300.SH',
    priority: 10,
};

function buildParameters(values: RuleFormValues): Record<string, any> {
    // 交易模式后端强制锁定 SIMULATION，这里只传业务阈值。
    const parameters: Record<string, any> = {};
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

interface Props {
    tenantId?: string;
}

const isGlobalRule = (rule: RiskRuleAdmin) => !!rule.applies_to_all;

export const RiskControlTab: React.FC<Props> = ({ tenantId = 'default' }) => {
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
                riskControlService.listRules(false),
                riskControlService.listEvents({ limit: 200 }),
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
        if (isGlobalRule(rule)) return;
        setEditing(rule);
        const params = rule.parameters || {};
        form.setFieldsValue({
            rule_name: rule.rule_name,
            rule_type: rule.rule_type,
            description: rule.description || '',
            is_active: rule.is_active,
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
            // 作用域后端强制为本人模拟盘规则，前端不再传递。
            applies_to_all: false as boolean,
            user_ids: null as number[] | null,
            priority: values.priority,
            parameters: buildParameters(values),
        };
        try {
            if (editing) {
                await riskControlService.updateRule(editing.id, payload);
                message.success('规则已更新');
            } else {
                await riskControlService.createRule(payload);
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
            await riskControlService.updateRule(rule.id, { is_active });
            await loadAll();
        } catch (error: any) {
            message.error(error?.response?.data?.detail || error?.message || '更新失败');
        }
    };

    const removeRule = async (rule: RiskRuleAdmin) => {
        try {
            await riskControlService.deleteRule(rule.id);
            message.success('已删除');
            await loadAll();
        } catch (error: any) {
            message.error(error?.response?.data?.detail || error?.message || '删除失败');
        }
    };

    const runDryRun = async (rule: RiskRuleAdmin) => {
        try {
            const rows = await riskControlService.dryRunRule(rule.id, { tenant_id: tenantId });
            if (!rows?.length) {
                message.info('当前持仓不会触发此规则');
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
            { title: '名称', dataIndex: 'rule_name', width: 180, align: 'center' },
            {
                title: '类型',
                dataIndex: 'rule_type',
                width: 160,
                align: 'center',
                render: (value: string) => (
                    <Tag color={isTriggerType(value) ? 'red' : 'default'}>
                        {RULE_TYPE_LABEL[value] || value}
                    </Tag>
                ),
            },
            {
                title: '阈值',
                width: 180,
                align: 'center',
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
                align: 'center',
                render: (_, rule) =>
                    isGlobalRule(rule) ? <Tag color="blue">全局</Tag> : <Tag color="green">本人</Tag>,
            },
            {
                title: '启用',
                width: 80,
                align: 'center',
                render: (_, rule) =>
                    isGlobalRule(rule) ? (
                        <Tag color={rule.is_active ? 'success' : 'default'}>{rule.is_active ? '生效中' : '已停用'}</Tag>
                    ) : (
                        <Switch checked={rule.is_active} onChange={(checked) => void toggleRule(rule, checked)} />
                    ),
            },
            {
                title: '操作',
                width: 220,
                align: 'center',
                render: (_, rule) => (
                    <Space>
                        {!isGlobalRule(rule) && (
                            <Button type="link" size="small" onClick={() => openEdit(rule)}>
                                编辑
                            </Button>
                        )}
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
                        {!isGlobalRule(rule) && (
                            <Popconfirm title="确认删除该规则？" onConfirm={() => void removeRule(rule)}>
                                <Button type="link" size="small" danger>
                                    删除
                                </Button>
                            </Popconfirm>
                        )}
                    </Space>
                ),
            },
        ],
        // eslint-disable-next-line react-hooks/exhaustive-deps
        [],
    );

    const eventColumns: ColumnsType<RiskEventAdmin> = [
        { title: '时间', dataIndex: 'created_at', width: 180, align: 'center', render: (value: string) => value?.replace('T', ' ').slice(0, 19) },
        { title: '类型', dataIndex: 'rule_type', width: 150, align: 'center', render: (value: string) => RULE_TYPE_LABEL[value] || value },
        { title: '标的', dataIndex: 'symbol', width: 110, align: 'center' },
        {
            title: '状态',
            dataIndex: 'status',
            width: 120,
            align: 'center',
            render: (value: string) => <Tag color={STATUS_COLOR[value] || 'default'}>{value}</Tag>,
        },
        { title: '涨跌', dataIndex: 'pnl_pct', width: 90, align: 'center', render: formatPct },
        { title: '数量', dataIndex: 'quantity', width: 80, align: 'center' },
        { title: '说明', dataIndex: 'message', ellipsis: true, align: 'center' },
    ];

    return (
        <div className="h-full overflow-auto p-6 pb-[96px]">
            {/* 页头 */}
            <div className="mb-5 flex flex-wrap items-center gap-4">
                <h3 className="flex items-center whitespace-nowrap text-base font-bold text-gray-800">
                    <ShieldCheck className="mr-2 text-blue-600" size={18} />
                    风险控制
                </h3>
                <p className="text-xs text-gray-500">
                    触发规则独立扫描（默认 60 秒）；触及后模拟盘自动平仓并当日禁买。全局规则仅只读展示本人规则可新建/编辑/删除。
                </p>
                <div className="ml-auto">
                    <Space>
                        <Button icon={<ReloadOutlined />} onClick={() => void loadAll()}>
                            刷新
                        </Button>
                        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                            新建规则
                        </Button>
                    </Space>
                </div>
            </div>

            {/* 规则列表 */}
            <div className="mb-5 rounded-2xl border border-slate-200/70 bg-white p-5 shadow-sm">
                <div className="mb-3 flex items-center gap-2">
                    <span className="text-sm font-bold text-slate-800">规则列表</span>
                    <Tag color="blue">{rules.length} 条</Tag>
                </div>
                <Table
                    rowKey="id"
                    loading={loading}
                    columns={ruleColumns}
                    dataSource={rules}
                    pagination={{ pageSize: 20, hideOnSinglePage: true, showTotal: (total) => `共 ${total} 条` }}
                    size="middle"
                />
            </div>

            {/* 最近触发 */}
            <div className="rounded-2xl border border-slate-200/70 bg-white p-5 shadow-sm">
                <div className="mb-3 flex items-center gap-2">
                    <span className="text-sm font-bold text-slate-800">最近触发（本人）</span>
                    <Tag color="blue">{events.length} 条</Tag>
                </div>
                <Table
                    rowKey="id"
                    loading={loading}
                    columns={eventColumns}
                    dataSource={events}
                    pagination={{ pageSize: 20, hideOnSinglePage: true, showTotal: (total) => `共 ${total} 条` }}
                    size="middle"
                />
            </div>

            <Drawer
                title={editing ? '编辑风控规则' : '新建风控规则'}
                open={drawerOpen}
                width={520}
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
                    {(ruleType === 'position_stop_loss' ||
                        ruleType === 'position_take_profit' ||
                        ruleType === 'market_index_move') && (
                        <Form.Item
                            name="pct"
                            label={ruleType === 'position_take_profit' ? '止盈 (%)' : '阈值 (%)'}
                            extra={
                                ruleType === 'position_stop_loss'
                                    ? '相对成本例如 -8 表示跌 8% 平该标的'
                                    : ruleType === 'market_index_move'
                                      ? '指数当日涨跌幅例如 -3 表示跌 3% 平全部可卖仓'
                                      : '相对成本例如 15 表示涨 15% 平该标的'
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

export default RiskControlTab;
