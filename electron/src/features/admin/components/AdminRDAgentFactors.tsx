/**
 * AlphaAgent 因子挖掘 — 管理后台可视化页
 *
 * 模块：
 *  ① 统计概览（4 卡）
 *  ② 触发演化（表单 → POST /quantbot/chat）
 *  ③ 活跃任务列表（5s 轮询 /quantbot/tasks）
 *  ④ 因子列表 Table（/alpha-agent/factors）
 *  ⑤ 因子详情 Drawer（factor_code + metrics + 一键回测）
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useDispatch } from 'react-redux';
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  message,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Timeline,
  Tooltip,
  Typography,
} from 'antd';
import {
  ExperimentOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
  CopyOutlined,
  PlayCircleOutlined,
  ClockCircleOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
  RightOutlined,
  DownOutlined,
  RocketOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';

import {
  rdAgentService,
  buildEvolutionMessage,
  FACTOR_TYPE_OPTIONS,
  MARKET_OPTIONS,
  type FactorStatus,
  type RDAgentFactor,
  type RDAgentStats,
  type QuantBotTask,
} from '../services/rdAgentService';
import { useBacktestCenterStore } from '../../../stores/backtestCenterStore';
import { setCurrentTab } from '../../../store/slices/aiStrategySlice';
import { RdMinedMaterializePanel } from './RdMinedMaterializePanel';

const { Title, Text, Paragraph } = Typography;

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

const FACTOR_STATUS_META: Record<FactorStatus, { color: string; label: string }> = {
  pending:     { color: 'default',    label: '待处理' },
  backtesting: { color: 'processing', label: '回测中' },
  completed:   { color: 'success',    label: '已完成' },
  failed:      { color: 'error',      label: '失败' },
};

const TASK_STATUS_META: Record<string, { color: string; icon: React.ReactNode; label: string }> = {
  pending:    { color: '#94a3b8', icon: <ClockCircleOutlined />, label: '排队中' },
  running:    { color: '#3b82f6', icon: <LoadingOutlined spin />, label: '运行中' },
  completed:  { color: '#10b981', icon: <CheckCircleOutlined />, label: '已完成' },
  failed:     { color: '#ef4444', icon: <CloseCircleOutlined />, label: '失败' },
};

function fmtNumber(v: number | null | undefined, digits = 4): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Number(v).toFixed(digits);
}

function fmtTime(s?: string): string {
  if (!s) return '—';
  try { return dayjs(s).format('MM-DD HH:mm:ss'); } catch { return s; }
}

function shortId(id: string, n = 12): string {
  if (!id) return '';
  if (id.length <= n) return id;
  return `${id.slice(0, n)}…`;
}

function parseMeta(factor: Partial<RDAgentFactor> | any): Record<string, any> {
  if (!factor) return {};
  // 后端 list 接口返回 metadata（已解析），保留对 metadata_json 字符串的兼容
  if (factor.metadata && typeof factor.metadata === 'object') return factor.metadata as Record<string, any>;
  const raw = factor.metadata_json ?? factor.metadata;
  if (!raw) return {};
  if (typeof raw === 'string') {
    try { return JSON.parse(raw); } catch { return {}; }
  }
  return raw as Record<string, any>;
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export const AdminRDAgentFactors: React.FC = () => {
  // 数据
  const [stats, setStats] = useState<RDAgentStats | null>(null);
  const [factors, setFactors] = useState<RDAgentFactor[]>([]);
  const [tasks, setTasks] = useState<QuantBotTask[]>([]);

  // UI 状态
  const [loadingStats, setLoadingStats] = useState(false);
  const [loadingFactors, setLoadingFactors] = useState(false);
  const [loadingTasks, setLoadingTasks] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const [market, setMarket] = useState<string>('a_share');
  const [statusFilter, setStatusFilter] = useState<FactorStatus | undefined>(undefined);
  const [taskHighlight, setTaskHighlight] = useState<string | undefined>(undefined);

  const [selectedFactor, setSelectedFactor] = useState<RDAgentFactor | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerLoading, setDrawerLoading] = useState(false);
  const [backtestingId, setBacktestingId] = useState<string | null>(null);

  // 跳转回测中心需要的 hook
  const navigate = useNavigate();
  const dispatch = useDispatch();
  const updateBacktestConfig = useBacktestCenterStore(s => s.updateBacktestConfig);
  const setActiveModule = useBacktestCenterStore(s => s.setActiveModule);

  // 任务详情折叠（默认全部折叠，避免失败任务的长堆栈占满屏幕）
  const [expandedTasks, setExpandedTasks] = useState<Set<string>>(new Set());
  const [showAllTasks, setShowAllTasks] = useState(false);
  const toggleTaskExpand = (taskId: string) => {
    setExpandedTasks((() => {
      const next = new Set(expandedTasks);
      if (next.has(taskId)) next.delete(taskId); else next.add(taskId);
      return next;
    })());
  };

  const [form] = Form.useForm();

  // ---------------------- 数据加载 ----------------------

  const reloadStats = useCallback(async () => {
    setLoadingStats(true);
    try {
      const s = await rdAgentService.getStats(market);
      setStats(s);
    } catch (err: any) {
      message.error(`加载统计失败: ${err?.message || err}`);
    } finally {
      setLoadingStats(false);
    }
  }, [market]);

  const reloadFactors = useCallback(async () => {
    setLoadingFactors(true);
    try {
      const list = await rdAgentService.listFactors({
        status: statusFilter,
        limit: 100,
        market,
      });
      setFactors(list);
    } catch (err: any) {
      message.error(`加载因子列表失败: ${err?.message || err}`);
    } finally {
      setLoadingFactors(false);
    }
  }, [statusFilter, market]);

  const reloadTasks = useCallback(async () => {
    setLoadingTasks(true);
    try {
      const list = await rdAgentService.listTasks(market);
      setTasks(list);
    } catch {
      // 静默 — 未登录或暂未实现都不打扰
    } finally {
      setLoadingTasks(false);
    }
  }, [market]);

  // 首次加载
  useEffect(() => {
    void reloadStats();
    void reloadFactors();
    void reloadTasks();
  }, [reloadStats, reloadFactors, reloadTasks]);

  // 任务轮询
  useEffect(() => {
    const timer = window.setInterval(() => {
      void reloadTasks();
      void reloadStats();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [reloadTasks, reloadStats]);

  // 任务列表跳变时，如果某条任务从 running → completed，自动刷一次因子表
  const prevRunningRef = React.useRef<string[]>([]);
  useEffect(() => {
    const running = tasks.filter(t => t.status === 'running').map(t => t.task_id);
    const finishedSinceLast = prevRunningRef.current.filter(id => !running.includes(id));
    if (finishedSinceLast.length > 0) {
      void reloadFactors();
    }
    prevRunningRef.current = running;
  }, [tasks, reloadFactors]);

  // ---------------------- 触发演化 ----------------------

  const onSubmit = async (values: { factor_type: string; loop_n: number; description?: string }) => {
    setSubmitting(true);
    try {
      const msg = buildEvolutionMessage({ ...values, market });
      const r = await rdAgentService.triggerEvolution(msg, market);
      if (r.intent !== 'factor_evolution') {
        message.warning(`意图识别为 ${r.intent}，未触发因子演化，回复：${r.answer || ''}`);
        return;
      }
      message.success(`已启动演化任务 ${shortId(r.task_id || '', 16)}`);
      // 立即刷新任务列表
      void reloadTasks();
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || String(err);
      message.error(`触发失败：${detail}`);
    } finally {
      setSubmitting(false);
    }
  };

  // ---------------------- 因子详情 ----------------------

  const openDetail = async (factor: RDAgentFactor) => {
    setSelectedFactor(factor);
    setDrawerOpen(true);
    setDrawerLoading(true);
    try {
      const full = await rdAgentService.getFactor(factor.factor_id);
      if (full) setSelectedFactor(full);
    } catch (err: any) {
      message.error(`加载因子详情失败: ${err?.message || err}`);
    } finally {
      setDrawerLoading(false);
    }
  };

  const onBacktest = async (factor: RDAgentFactor) => {
    setBacktestingId(factor.factor_id);
    try {
      const r = await rdAgentService.backtestFactor(factor.factor_id);
      message.success(r.message || '已发起快速验证');
      await reloadFactors();
      if (selectedFactor?.factor_id === factor.factor_id) {
        await openDetail(factor);
      }
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || String(err);
      message.error(`快速验证失败：${detail}`);
    } finally {
      setBacktestingId(null);
    }
  };

  // 「完整回测」：把因子代码注入回测中心 → 跳到主页 backtest tab
  const onFullBacktest = (factor: RDAgentFactor) => {
    if (!factor.factor_code) {
      message.warning('该因子无代码，无法发起完整回测');
      return;
    }

    const strategyName = factor.factor_name || factor.factor_id;
    const header = `# RD因子: ${strategyName}\n# Factor ID: ${factor.factor_id}\n# 自动从 AlphaAgent 因子挖掘注入到回测中心\n\n`;

    updateBacktestConfig({
      strategy_code: header + factor.factor_code,
      strategy_name: strategyName,
      factor_id: factor.factor_id,
      factor_source: 'rd-agent',
    } as any);
    setActiveModule('quick-backtest');

    dispatch(setCurrentTab('backtest' as any));
    navigate('/');
    message.success(`已将因子 "${strategyName}" 加载到回测中心`);
  };

  const copyCode = (code?: string) => {
    if (!code) {
      message.warning('该因子无代码可复制');
      return;
    }
    try {
      void navigator.clipboard.writeText(code);
      message.success('因子代码已复制');
    } catch {
      message.error('复制失败，浏览器不支持 clipboard API');
    }
  };

  // ---------------------- 派生：按高亮任务过滤 ----------------------

  const visibleFactors = useMemo(() => {
    if (!taskHighlight) return factors;
    return factors.filter(f => parseMeta(f).task_id === taskHighlight);
  }, [factors, taskHighlight]);

  // ---------------------- 列定义 ----------------------

  const columns = [
    {
      title: '因子名',
      dataIndex: 'factor_name',
      key: 'factor_name',
      width: 220,
      render: (v: string, row: RDAgentFactor) => (
        <Tooltip title={row.factor_id}>
          <Button type="link" className="!p-0" onClick={() => openDetail(row)}>
            {v || row.factor_id}
          </Button>
        </Tooltip>
      ),
    },
    {
      title: '市场',
      key: 'market',
      width: 80,
      render: (_: any, row: RDAgentFactor) => {
        const m = parseMeta(row).market;
        const opt = MARKET_OPTIONS.find(o => o.value === m);
        return opt ? <Tag color={opt.color} className="text-[10px] m-0">{opt.cn}</Tag> : <span className="text-slate-300 text-xs">—</span>;
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (s: FactorStatus) => {
        const meta = FACTOR_STATUS_META[s] || { color: 'default', label: s };
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
    },
    { title: 'IC',        dataIndex: 'ic_value',     key: 'ic',       width: 90, render: (v: number) => fmtNumber(v, 4) },
    { title: '夏普',      dataIndex: 'sharpe_ratio', key: 'sharpe',   width: 90, render: (v: number) => fmtNumber(v, 3) },
    { title: '年化收益',  dataIndex: 'annual_return',key: 'ar',       width: 100, render: (v: number) => v != null ? `${(v * 100).toFixed(2)}%` : '—' },
    { title: '最大回撤',  dataIndex: 'max_drawdown', key: 'mdd',      width: 100, render: (v: number) => v != null ? `${(v * 100).toFixed(2)}%` : '—' },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 150,
      render: fmtTime,
    },
    {
      title: '操作',
      key: 'actions',
      width: 280,
      render: (_: any, row: RDAgentFactor) => (
        <Space size={4}>
          <Button size="small" onClick={() => openDetail(row)}>查看</Button>
          <Tooltip title="原地跑一遍 IC/夏普，回填到本表">
            <Button
              size="small"
              type="primary"
              ghost
              loading={backtestingId === row.factor_id}
              disabled={row.status === 'backtesting' || !row.factor_code}
              onClick={() => onBacktest(row)}
            >
              快速验证
            </Button>
          </Tooltip>
          <Tooltip title="把因子代码加载到回测中心进行完整回测">
            <Button
              size="small"
              type="primary"
              icon={<RocketOutlined />}
              disabled={!row.factor_code}
              onClick={() => onFullBacktest(row)}
            >
              完整回测
            </Button>
          </Tooltip>
          <Tooltip title="复制因子代码">
            <Button size="small" icon={<CopyOutlined />} onClick={() => copyCode(row.factor_code)} />
          </Tooltip>
        </Space>
      ),
    },
  ];

  // ---------------------- 渲染 ----------------------

  return (
    <div className="space-y-6">
      {/* 标题 */}
      <div className="flex items-center justify-between">
        <div>
          <Title level={3} className="!mb-1 !text-slate-800">
            RD因子挖掘
          </Title>
          <Text className="text-slate-500 text-sm">
            基于 LLM 的自动化因子演化引擎 — 一键触发、实时进度、自动落库、可视化回测
          </Text>
        </div>
        <Space size="middle">
          <Segmented
            value={market}
            onChange={(val) => setMarket(val as string)}
            options={MARKET_OPTIONS.map(m => ({
              value: m.value,
              label: (
                <Space size={4}>
                  <span>{m.cn}</span>
                </Space>
              ),
            }))}
          />
          <Button icon={<ReloadOutlined />} onClick={() => { void reloadStats(); void reloadFactors(); void reloadTasks(); }}>
            刷新
          </Button>
        </Space>
      </div>

      {/* ① 统计概览 */}
      <Row gutter={16}>
        <Col span={6}>
          <Card><Statistic loading={loadingStats} title="总因子数" value={stats?.total ?? 0} prefix={<ExperimentOutlined />} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic loading={loadingStats} title="已完成" value={stats?.completed ?? 0} valueStyle={{ color: '#10b981' }} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic loading={loadingStats} title="平均 IC" value={fmtNumber(stats?.avg_ic, 4)} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic loading={loadingStats} title="最佳夏普" value={fmtNumber(stats?.best_sharpe, 3)} valueStyle={{ color: '#3b82f6' }} /></Card>
        </Col>
      </Row>

      {/* ② 触发演化 */}
      <Card
        title={
          <Space>
            <ThunderboltOutlined className="text-amber-500" />
            触发因子演化
            <Tag color={MARKET_OPTIONS.find(m => m.value === market)?.color || 'default'}>
              {MARKET_OPTIONS.find(m => m.value === market)?.cn || market}
            </Tag>
          </Space>
        }
        extra={<Text className="text-xs text-slate-400">单轮约 5–15 分钟，后台 Docker 跑 LLM 演化</Text>}
      >
        <Form
          form={form}
          layout="inline"
          initialValues={{ factor_type: 'momentum', loop_n: 5 }}
          onFinish={onSubmit}
        >
          <Form.Item label="因子类型" name="factor_type" rules={[{ required: true }]}>
            <Select style={{ width: 160 }}>
              {FACTOR_TYPE_OPTIONS.map(opt => (
                <Select.Option key={opt.value} value={opt.value}>{opt.cn} ({opt.label})</Select.Option>
              ))}
            </Select>
          </Form.Item>
          <Form.Item label="演化轮数" name="loop_n" rules={[{ required: true }]}>
            <InputNumber min={1} max={20} style={{ width: 100 }} />
          </Form.Item>
          <Form.Item label="附加描述" name="description" style={{ flex: 1, minWidth: 240 }}>
            <Input placeholder="可选：偏向短周期 / 偏向行业中性 等" allowClear />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={submitting} icon={<PlayCircleOutlined />}>
              启动演化
            </Button>
          </Form.Item>
        </Form>
      </Card>

      {/* ③ 活跃任务列表 */}
      <Card
        title={
          <Space>
            {showAllTasks ? '最近任务（全部）' : '活跃任务（运行中）'}
            {loadingTasks && <Spin size="small" />}
          </Space>
        }
        extra={
          <Space>
            {taskHighlight && (
              <Button size="small" onClick={() => setTaskHighlight(undefined)}>清除任务过滤</Button>
            )}
            <Button size="small" onClick={() => setShowAllTasks(!showAllTasks)}>
              {showAllTasks ? '只看运行中' : '查看全部'}
            </Button>
          </Space>
        }
      >
        {(() => {
          const visibleTasks = showAllTasks
            ? tasks
            : tasks.filter(t => t.status === 'running' || t.status === 'pending');
          if (visibleTasks.length === 0) {
            return (
              <Empty description={
                showAllTasks
                  ? '暂无任务，使用上方表单或在 QuantBot 聊天里输入「挖一批动量因子」即可触发'
                  : '当前没有正在运行的任务'
              } />
            );
          }
          return (
            <Timeline
              items={visibleTasks.slice(0, 10).map(t => {
                const meta = TASK_STATUS_META[t.status] || TASK_STATUS_META.pending;
                const isHighlight = taskHighlight === t.task_id;
                const isExpanded = expandedTasks.has(t.task_id);
                const hasError = t.status === 'failed' && t.error_message;
                const errorTooLong = hasError && (t.error_message || '').length > 80;
                return {
                  color: meta.color,
                  dot: meta.icon as any,
                  children: (
                    <div className={`rounded-lg p-3 -mx-3 transition ${isHighlight ? 'bg-violet-50 ring-1 ring-violet-300' : 'hover:bg-slate-50'}`}>
                      <div className="flex items-center justify-between mb-1">
                        <Space size={6}>
                          <Button
                            type="text"
                            size="small"
                            className="!px-1"
                            icon={isExpanded ? <DownOutlined /> : <RightOutlined />}
                            onClick={() => toggleTaskExpand(t.task_id)}
                          />
                          <Text strong>{meta.label}</Text>
                          <Text className="text-xs text-slate-400">{shortId(t.task_id, 16)}</Text>
                          {hasError && !isExpanded && (
                            <Tag color="error" className="!ml-1">点击展开错误详情</Tag>
                          )}
                        </Space>
                        <Space>
                          <Text className="text-xs text-slate-400">{fmtTime(t.created_at)}</Text>
                          <Tooltip title={isHighlight ? '取消因子表过滤' : '在下方因子表只显示本任务因子'}>
                            <Button
                              size="small"
                              type={isHighlight ? 'primary' : 'default'}
                              ghost={isHighlight}
                              onClick={() => setTaskHighlight(isHighlight ? undefined : t.task_id)}
                            >
                              {isHighlight ? '已过滤' : '只看本任务因子'}
                            </Button>
                          </Tooltip>
                        </Space>
                      </div>

                      {/* 折叠态：1 行 progress 摘要；展开态：完整 progress + 错误堆栈 */}
                      {!isExpanded ? (
                        <Text className="text-sm text-slate-600 line-clamp-1">
                          {t.progress || (hasError ? (t.error_message || '').split('\n')[0].slice(0, 100) : '—')}
                        </Text>
                      ) : (
                        <div className="space-y-2 mt-2">
                          {t.progress && (
                            <Alert type="info" message={t.progress} className="!py-1" />
                          )}
                          {t.status === 'completed' && t.factor_ids && t.factor_ids.length > 0 && (
                            <Badge status="success" text={`生成 ${t.factor_ids.length} 个因子`} />
                          )}
                          {hasError && (
                            <Alert
                              type="error"
                              message={
                                <Space>
                                  <span>任务失败</span>
                                  {errorTooLong && (
                                    <Button
                                      size="small"
                                      icon={<CopyOutlined />}
                                      onClick={() => { void navigator.clipboard.writeText(t.error_message || ''); message.success('错误详情已复制'); }}
                                    >
                                      复制
                                    </Button>
                                  )}
                                </Space>
                              }
                              description={
                                <pre className="text-xs whitespace-pre-wrap m-0 max-h-64 overflow-auto">
                                  {t.error_message}
                                </pre>
                              }
                            />
                          )}
                        </div>
                      )}
                    </div>
                  ),
                };
              })}
            />
          );
        })()}
      </Card>

      {/* ③.5 物化面板：挖掘 → 训练库的中间环节，仅 A 股因子会物化 */}
      {market === 'a_share' && (
        <RdMinedMaterializePanel
          onCompleted={() => { void reloadStats(); void reloadFactors(); void reloadTasks(); }}
        />
      )}

      {/* ④ 因子列表 */}
      <Card
        title={
          <Space>
            因子列表
            {taskHighlight && <Tag color="purple">已按任务 {shortId(taskHighlight, 12)} 过滤</Tag>}
          </Space>
        }
        extra={
          <Space>
            <Select
              allowClear
              placeholder="按状态过滤"
              style={{ width: 140 }}
              value={statusFilter}
              onChange={v => setStatusFilter(v)}
            >
              {(['pending','backtesting','completed','failed'] as FactorStatus[]).map(s => (
                <Select.Option key={s} value={s}>{FACTOR_STATUS_META[s].label}</Select.Option>
              ))}
            </Select>
          </Space>
        }
      >
        <Table
          rowKey="factor_id"
          size="small"
          loading={loadingFactors}
          columns={columns as any}
          dataSource={visibleFactors}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          scroll={{ x: 1300 }}
        />
      </Card>

      {/* ⑤ 因子详情 Drawer */}
      <Drawer
        title={
          selectedFactor
            ? (<Space><ExperimentOutlined />{selectedFactor.factor_name || selectedFactor.factor_id}</Space>)
            : '因子详情'
        }
        width={760}
        open={drawerOpen}
        onClose={() => { setDrawerOpen(false); setSelectedFactor(null); }}
        destroyOnHidden
        extra={selectedFactor && (
          <Space>
            <Button icon={<CopyOutlined />} onClick={() => copyCode(selectedFactor.factor_code)}>复制代码</Button>
            <Tooltip title="原地跑 IC/夏普，回填到本因子">
              <Button
                ghost
                type="primary"
                loading={backtestingId === selectedFactor.factor_id}
                disabled={selectedFactor.status === 'backtesting' || !selectedFactor.factor_code}
                onClick={() => onBacktest(selectedFactor)}
              >
                快速验证
              </Button>
            </Tooltip>
            <Tooltip title="把因子代码加载到回测中心进行完整回测">
              <Button
                type="primary"
                icon={<RocketOutlined />}
                disabled={!selectedFactor.factor_code}
                onClick={() => onFullBacktest(selectedFactor)}
              >
                完整回测
              </Button>
            </Tooltip>
          </Space>
        )}
      >
        {drawerLoading || !selectedFactor ? (
          <div className="text-center py-20"><Spin /></div>
        ) : (
          <Tabs
            items={[
              {
                key: 'overview',
                label: '基本信息',
                children: (
                  <Descriptions bordered column={2} size="small">
                    <Descriptions.Item label="Factor ID" span={2}>
                      <Text copyable code>{selectedFactor.factor_id}</Text>
                    </Descriptions.Item>
                    <Descriptions.Item label="名称">{selectedFactor.factor_name || '—'}</Descriptions.Item>
                    <Descriptions.Item label="状态">
                      <Tag color={FACTOR_STATUS_META[selectedFactor.status]?.color}>
                        {FACTOR_STATUS_META[selectedFactor.status]?.label || selectedFactor.status}
                      </Tag>
                    </Descriptions.Item>
                    <Descriptions.Item label="IC">{fmtNumber(selectedFactor.ic_value, 4)}</Descriptions.Item>
                    <Descriptions.Item label="夏普">{fmtNumber(selectedFactor.sharpe_ratio, 3)}</Descriptions.Item>
                    <Descriptions.Item label="年化收益">
                      {selectedFactor.annual_return != null ? `${(selectedFactor.annual_return * 100).toFixed(2)}%` : '—'}
                    </Descriptions.Item>
                    <Descriptions.Item label="最大回撤">
                      {selectedFactor.max_drawdown != null ? `${(selectedFactor.max_drawdown * 100).toFixed(2)}%` : '—'}
                    </Descriptions.Item>
                    <Descriptions.Item label="所属用户">{selectedFactor.user_id || '—'}</Descriptions.Item>
                    <Descriptions.Item label="市场">
                      {(() => {
                        const m = parseMeta(selectedFactor).market;
                        const opt = MARKET_OPTIONS.find(o => o.value === m);
                        return opt ? <Tag color={opt.color}>{opt.cn}</Tag> : '—';
                      })()}
                    </Descriptions.Item>
                    <Descriptions.Item label="任务 ID">
                      <Text className="text-xs">{parseMeta(selectedFactor).task_id || '—'}</Text>
                    </Descriptions.Item>
                    <Descriptions.Item label="创建时间">{fmtTime(selectedFactor.created_at)}</Descriptions.Item>
                    <Descriptions.Item label="更新时间">{fmtTime(selectedFactor.updated_at)}</Descriptions.Item>
                  </Descriptions>
                ),
              },
              {
                key: 'code',
                label: '因子代码',
                children: selectedFactor.factor_code ? (
                  <pre className="bg-slate-900 text-slate-100 text-xs rounded-lg p-4 overflow-auto max-h-[60vh]">
                    <code>{selectedFactor.factor_code}</code>
                  </pre>
                ) : (
                  <Empty description="该因子未保存代码" />
                ),
              },
              {
                key: 'meta',
                label: '原始元数据',
                children: (
                  <pre className="bg-slate-50 text-slate-700 text-xs rounded-lg p-4 overflow-auto max-h-[60vh]">
                    <code>{JSON.stringify(parseMeta(selectedFactor), null, 2)}</code>
                  </pre>
                ),
              },
            ]}
          />
        )}
      </Drawer>

      {/* 底部说明 */}
      <Alert
        type="info"
        showIcon
        message="说明"
        description={
          <Paragraph className="!mb-0 text-sm text-slate-500">
            演化通过 <Text code>quantmind-rdagent</Text> 一次性 Docker 容器执行，
            支持的因子类型：价值 / 动量 / 波动 / 质量 / 成长 / 技术 / 综合。
            <br />
            <Text code>快速验证</Text> 在后台原地跑 IC/夏普并回填到因子表；
            <Text code>完整回测</Text> 将因子代码加载到「回测中心」走完整流程（K 线、分组收益、换手率等）。
          </Paragraph>
        }
      />
    </div>
  );
};

export default AdminRDAgentFactors;
