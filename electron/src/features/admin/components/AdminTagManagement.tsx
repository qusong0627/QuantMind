/**
 * 标签管理（管理员）
 *
 * 对 finance_lexicon 表的 CRUD 操作：查看 / 新增 / 编辑 / 删除 / 启禁用
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Button,
  Card,
  Col,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  TagsOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { newsService, type LexiconTag } from '../../news/services/newsService';

const { Title, Text } = Typography;

const KIND_OPTIONS = [
  { value: 'sentiment_pos', label: '情感(利好)' },
  { value: 'sentiment_neg', label: '情感(利空)' },
  { value: 'event', label: '事件/实体' },
  { value: 'department', label: '部门' },
];

const EVENT_TAG_OPTIONS = [
  { value: '国家', label: '国家' },
  { value: '地区', label: '地区' },
  { value: '省份', label: '省份' },
  { value: '城市', label: '城市' },
  { value: '领导人', label: '领导人' },
  { value: '调研', label: '调研' },
  { value: '部门', label: '部门' },
  { value: '产业', label: '产业' },
  { value: '政策', label: '政策' },
  { value: '地缘', label: '地缘' },
  { value: '外汇', label: '外汇' },
  { value: '加密', label: '加密' },
  { value: '财报', label: '财报' },
  { value: '市场', label: '市场' },
  { value: '宏观', label: '宏观' },
  { value: '期货', label: '期货' },
  { value: '监管', label: '监管' },
  { value: '行业板块', label: '行业板块' },
  { value: '概念板块', label: '概念板块' },
];

export const AdminTagManagement: React.FC = () => {
  const [tags, setTags] = useState<LexiconTag[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [loading, setLoading] = useState(false);
  const [filterEventTag, setFilterEventTag] = useState<string | undefined>();
  const [filterKind, setFilterKind] = useState<string | undefined>();
  const [filterKeyword, setFilterKeyword] = useState('');
  const [modalOpen, setModalOpen] = useState(false);
  const [editingTag, setEditingTag] = useState<LexiconTag | null>(null);
  const [form] = Form.useForm();

  const loadTags = useCallback(async () => {
    setLoading(true);
    try {
      const r = await newsService.adminListTags({
        page,
        page_size: pageSize,
        event_tag: filterEventTag,
        kind: filterKind,
        keyword: filterKeyword || undefined,
      });
      setTags(r.items ?? []);
      setTotal(r.total ?? 0);
    } catch {
      setTags([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, filterEventTag, filterKind, filterKeyword]);

  useEffect(() => {
    loadTags();
  }, [loadTags]);

  // 统计概览（基于当前加载列表前端统计；如需全局统计再加后端接口）
  const stats = useMemo(() => {
    const enabled = tags.filter((t) => t.enabled).length;
    const kindCount: Record<string, number> = {};
    tags.forEach((t) => {
      kindCount[t.kind] = (kindCount[t.kind] || 0) + 1;
    });
    const topKind = Object.entries(kindCount).sort((a, b) => b[1] - a[1])[0];
    const topKindLabel = topKind
      ? (KIND_OPTIONS.find((o) => o.value === topKind[0])?.label ?? topKind[0])
      : '—';
    return { enabled, disabled: total - enabled, topKind, topKindLabel };
  }, [tags, total]);

  const handleCreate = () => {
    setEditingTag(null);
    form.resetFields();
    form.setFieldsValue({ kind: 'event', weight: 1.0, enabled: true });
    setModalOpen(true);
  };

  const handleEdit = (tag: LexiconTag) => {
    setEditingTag(tag);
    form.setFieldsValue({
      term: tag.term,
      kind: tag.kind,
      event_tag: tag.event_tag,
      weight: tag.weight,
      note: tag.note,
    });
    setModalOpen(true);
  };

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      if (editingTag) {
        await newsService.adminUpdateTag(editingTag.id, values);
        message.success('词条已更新');
      } else {
        await newsService.adminCreateTag(values);
        message.success('词条已创建');
      }
      setModalOpen(false);
      loadTags();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '操作失败';
      message.error(msg);
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await newsService.adminDeleteTag(id);
      message.success('词条已删除');
      loadTags();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '删除失败';
      message.error(msg);
    }
  };

  const handleToggle = async (tag: LexiconTag) => {
    try {
      await newsService.adminToggleTag(tag.id);
      loadTags();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '操作失败';
      message.error(msg);
    }
  };

  const kindColor = (kind: string) => {
    if (kind === 'sentiment_pos') return 'red';
    if (kind === 'sentiment_neg') return 'green';
    if (kind === 'event') return 'blue';
    if (kind === 'department') return 'geekblue';
    return 'default';
  };

  const columns: ColumnsType<LexiconTag> = [
    {
      title: 'ID',
      dataIndex: 'id',
      width: 70,
      sorter: (a, b) => a.id - b.id,
    },
    {
      title: '词条',
      dataIndex: 'term',
      width: 160,
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: '类型',
      dataIndex: 'kind',
      width: 120,
      render: (v: string) => <Tag color={kindColor(v)}>{v}</Tag>,
    },
    {
      title: '标签',
      dataIndex: 'event_tag',
      width: 100,
      render: (v: string | null) => v ? <Tag>{v}</Tag> : <Text type="secondary">-</Text>,
    },
    {
      title: '权重',
      dataIndex: 'weight',
      width: 80,
      sorter: (a, b) => a.weight - b.weight,
    },
    {
      title: '备注',
      dataIndex: 'note',
      ellipsis: true,
    },
    {
      title: '状态',
      dataIndex: 'enabled',
      width: 80,
      render: (v: boolean, record) => (
        <Switch
          size="small"
          checked={v}
          onChange={() => handleToggle(record)}
        />
      ),
    },
    {
      title: '操作',
      width: 120,
      render: (_, record) => (
        <Space size="small">
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => handleEdit(record)}
          />
          <Popconfirm
            title="确定删除此词条？"
            onConfirm={() => handleDelete(record.id)}
            okText="删除"
            cancelText="取消"
          >
            <Button type="link" size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="p-6 space-y-4">
      {/* 顶部标题与统计 */}
      <div className="flex items-center justify-between pb-1">
        <div>
          <Title level={4} style={{ margin: 0, fontWeight: 700 }}>
            <TagsOutlined style={{ marginRight: 8, color: '#6366f1' }} />
            标签管理
          </Title>
          <Text type="secondary" style={{ fontSize: 13 }}>
            新闻情感 / 事件词条词典（finance_lexicon）· 支持情感词、事件实体、部门词条维护
          </Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={loadTags} style={{ borderRadius: 6 }}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={handleCreate} style={{ borderRadius: 6 }}>新增词条</Button>
        </Space>
      </div>

      {/* 统计概览卡片 */}
      <Row gutter={14}>
        <Col span={8}>
          <Card size="small" variant="borderless" style={{ background: '#eef2ff', borderRadius: 10 }}>
            <Statistic title="词条总数" value={total} suffix="条" valueStyle={{ color: '#4338ca', fontWeight: 700 }} style={{ textAlign: 'center' }} />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small" variant="borderless" style={{ background: '#f0fdf4', borderRadius: 10 }}>
            <Statistic title="已启用" value={stats.enabled} suffix="条" valueStyle={{ color: '#16a34a', fontWeight: 700 }} style={{ textAlign: 'center' }} />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small" variant="borderless" style={{ background: '#fff7ed', borderRadius: 10 }}>
            <Statistic title="最多类型" value={stats.topKindLabel} suffix={stats.topKind ? `(${stats.topKind[1]} 条)` : ''} valueStyle={{ color: '#d97706', fontWeight: 700 }} style={{ textAlign: 'center' }} />
          </Card>
        </Col>
      </Row>

      {/* 筛选栏 */}
      <Card styles={{ body: { padding: 0 } }} style={{ borderRadius: 10, overflow: 'hidden', border: '1px solid #e2e8f0' }}>
        <div className="flex flex-wrap items-center gap-3 px-4 py-3 border-b border-slate-100 bg-slate-50/50">
          <Select
            allowClear
            placeholder="按标签类型筛选"
            value={filterEventTag}
            onChange={(v) => { setFilterEventTag(v); setPage(1); }}
            options={EVENT_TAG_OPTIONS}
            style={{ minWidth: 160 }}
          />
          <Select
            allowClear
            placeholder="按情感/事件筛选"
            value={filterKind}
            onChange={(v) => { setFilterKind(v); setPage(1); }}
            options={KIND_OPTIONS}
            style={{ minWidth: 160 }}
          />
          <Input.Search
            allowClear
            placeholder="搜索词条..."
            value={filterKeyword}
            onChange={(e) => setFilterKeyword(e.target.value)}
            onSearch={() => { setPage(1); loadTags(); }}
            style={{ width: 240 }}
          />
        </div>

        <Table
          rowKey="id"
          columns={columns}
          dataSource={tags}
          loading={loading}
          size="middle"
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: (t) => `共 ${t} 条`,
            onChange: (p, ps) => { setPage(p); setPageSize(ps); },
          }}
        />
      </Card>

      <Modal
        title={editingTag ? '编辑词条' : '新增词条'}
        open={modalOpen}
        onOk={handleSave}
        onCancel={() => setModalOpen(false)}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={form} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item name="term" label="词条" rules={[{ required: true, message: '请输入词条' }]}>
            <Input placeholder="如：国务院、央行、利好" />
          </Form.Item>
          <Form.Item name="kind" label="类型" rules={[{ required: true, message: '请选择类型' }]}>
            <Select options={KIND_OPTIONS} />
          </Form.Item>
          <Form.Item name="event_tag" label="标签分类">
            <Select allowClear options={EVENT_TAG_OPTIONS} placeholder="选择标签分类（可选）" />
          </Form.Item>
          <Form.Item name="weight" label="权重">
            <InputNumber min={0} max={10} step={0.1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="note" label="备注">
            <Input.TextArea rows={2} placeholder="备注说明（可选）" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default AdminTagManagement;
