/**
 * RSS 源管理（管理员）
 *
 * 代理 Huntly 的 /api/setting/feeds/* 与 /api/setting/folder/*
 * 极致紧凑、现代全圆角、精选金融源快捷填入、完全兼容 AntD 5 最新规范
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  FolderAddOutlined,
  FolderOpenOutlined,
  GlobalOutlined,
  PlusOutlined,
  ReadOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import {
  newsService,
  type HuntlyConnector,
  type HuntlyFeedPreview,
  type HuntlyFolder,
} from '../../news/services/newsService';

const { Title, Text } = Typography;

interface SourceRow extends HuntlyConnector {
  folderId: number | null;
  folderName: string;
}

const UNGROUPED_LABEL = '未分组';

// 精选优质预设源（100% 实测可用）
const PRESET_FEEDS = [
  { name: '同花顺 7x24直播', url: 'http://quantmind-rsshub:1200/10jqka/realtimenews', folder: 'A股快讯' },
  { name: '财联社 7x24快讯', url: 'http://quantmind-rsshub:1200/cls/telegraph', folder: 'A股快讯' },
  { name: '华尔街见闻 实时快讯', url: 'http://quantmind-rsshub:1200/wallstreetcn/news/global', folder: 'A股快讯' },
  { name: '格隆汇 市场快讯', url: 'http://quantmind-rsshub:1200/gelonghui/live', folder: 'A股快讯' },
  { name: '金十数据 实时快讯', url: 'http://quantmind-rsshub:1200/jin10/news', folder: 'A股快讯' },
  { name: '财新网 金融监管', url: 'http://quantmind-rsshub:1200/caixin/finance/regulation', folder: '宏观与监管' },
  { name: '36氪 商业快讯', url: 'http://quantmind-rsshub:1200/36kr/newsflashes', folder: '商业科技' },
  { name: 'arXiv 计算机金融', url: 'http://export.arxiv.org/rss/q-fin', folder: '量化研究' },
  { name: 'Qlib 官方更新', url: 'https://github.com/microsoft/qlib/releases.atom', folder: '量化研究' },
];

export const AdminRssSources: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const [folders, setFolders] = useState<HuntlyFolder[]>([]);

  // —— 新增源 modal
  const [addOpen, setAddOpen] = useState(false);
  const [addForm] = Form.useForm();
  const [previewing, setPreviewing] = useState(false);
  const [previewData, setPreviewData] = useState<HuntlyFeedPreview | null>(null);

  // —— 编辑源 modal
  const [editOpen, setEditOpen] = useState(false);
  const [editForm] = Form.useForm();
  const [editingId, setEditingId] = useState<number | null>(null);

  // —— 文件夹管理 modal
  const [folderOpen, setFolderOpen] = useState(false);
  const [folderName, setFolderName] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const r = await newsService.adminListFolders();
      setFolders(r.folders || []);
    } catch (e: any) {
      message.error(`加载失败: ${e?.response?.data?.detail || e?.message || e}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const folderOptions = useMemo(
    () => [
      { label: UNGROUPED_LABEL, value: 0 },
      ...folders
        .filter((f) => f.id != null)
        .map((f) => ({ label: f.name || `#${f.id}`, value: f.id as number })),
    ],
    [folders],
  );

  const rows: SourceRow[] = useMemo(() => {
    const out: SourceRow[] = [];
    folders.forEach((f) => {
      const folderId = f.id ?? null;
      const folderName = f.name || UNGROUPED_LABEL;
      (f.connectors || []).forEach((c) => {
        out.push({ ...c, folderId, folderName });
      });
    });
    return out;
  }, [folders]);

  const totalInboxCount = useMemo(() => {
    return rows.reduce((acc, curr) => acc + (curr.inboxCount || 0), 0);
  }, [rows]);

  const handlePreview = async () => {
    const url = addForm.getFieldValue('subscribe_url');
    if (!url) {
      message.warning('请先填写订阅地址');
      return;
    }
    setPreviewing(true);
    setPreviewData(null);
    try {
      const data = await newsService.adminPreviewFeed(url);
      setPreviewData(data);
      if (data?.title) {
        message.success(`预览成功：${data.title}`);
        if (!addForm.getFieldValue('name')) {
          addForm.setFieldsValue({ name: data.title });
        }
      }
    } catch (e: any) {
      const errorMsg = e?.response?.data?.detail || e?.message || '连接超时或目标源无响应';
      message.warning(`预览提示: ${errorMsg}`);
    } finally {
      setPreviewing(false);
    }
  };

  const applyPreset = (preset: { name: string; url: string; folder: string }) => {
    addForm.setFieldsValue({
      subscribe_url: preset.url,
      name: preset.name,
    });
    const foundFolder = folders.find((f) => f.name === preset.folder);
    if (foundFolder && foundFolder.id) {
      addForm.setFieldsValue({ folder_id: foundFolder.id });
    }
    message.info(`已填入「${preset.name}」`);
  };

  const handleAddSubmit = async () => {
    const values = await addForm.validateFields();
    try {
      await newsService.adminCreateSource({
        subscribe_url: String(values.subscribe_url).trim(),
        folder_id: values.folder_id ?? null,
        name: values.name?.trim() || undefined,
      });
      message.success('订阅源已添加');
      setAddOpen(false);
      addForm.resetFields();
      setPreviewData(null);
      refresh();
    } catch (e: any) {
      message.error(`添加失败: ${e?.response?.data?.detail || e?.message || e}`);
    }
  };

  const openEdit = async (row: SourceRow) => {
    setEditingId(row.id);
    try {
      const detail = await newsService.adminGetSourceSetting(row.id);
      editForm.setFieldsValue({
        name: detail.name,
        folder_id: detail.folderId ?? 0,
        fetch_interval_minutes:
          detail.fetchIntervalMinutes ?? detail.defaultFetchIntervalMinutes,
        enabled: detail.enabled,
        crawl_full_content: !!detail.crawlFullContent,
        subscribe_url: detail.subscribeUrl,
      });
      setEditOpen(true);
    } catch (e: any) {
      message.error(`加载详情失败: ${e?.response?.data?.detail || e?.message || e}`);
    }
  };

  const handleEditSubmit = async () => {
    if (editingId == null) return;
    const values = await editForm.validateFields();
    try {
      await newsService.adminUpdateSource(editingId, {
        name: values.name?.trim(),
        folder_id: values.folder_id ?? null,
        fetch_interval_minutes: values.fetch_interval_minutes,
        enabled: values.enabled,
        crawl_full_content: values.crawl_full_content,
      });
      message.success('已保存');
      setEditOpen(false);
      refresh();
    } catch (e: any) {
      message.error(`保存失败: ${e?.response?.data?.detail || e?.message || e}`);
    }
  };

  const handleDelete = async (row: SourceRow) => {
    try {
      await newsService.adminDeleteSource(row.id);
      message.success(`已删除：${row.name}`);
      refresh();
    } catch (e: any) {
      message.error(`删除失败: ${e?.response?.data?.detail || e?.message || e}`);
    }
  };

  const handleAddFolder = async () => {
    const name = folderName.trim();
    if (!name) {
      message.warning('文件夹名不能为空');
      return;
    }
    try {
      await newsService.adminCreateFolder(name);
      message.success(`文件夹「${name}」已创建`);
      setFolderName('');
      refresh();
    } catch (e: any) {
      message.error(`创建失败: ${e?.response?.data?.detail || e?.message || e}`);
    }
  };

  const handleRenameFolder = async (folder: HuntlyFolder) => {
    const next = window.prompt('重命名文件夹', folder.name || '');
    if (!next || !next.trim() || next.trim() === folder.name) return;
    try {
      await newsService.adminRenameFolder(folder.id as number, next.trim());
      message.success('已重命名');
      refresh();
    } catch (e: any) {
      message.error(`重命名失败: ${e?.response?.data?.detail || e?.message || e}`);
    }
  };

  const handleDeleteFolder = async (folder: HuntlyFolder) => {
    try {
      await newsService.adminDeleteFolder(folder.id as number);
      message.success(`已删除文件夹：${folder.name}`);
      refresh();
    } catch (e: any) {
      message.error(`删除失败: ${e?.response?.data?.detail || e?.message || e}`);
    }
  };

  const getFolderTagColor = (name: string) => {
    if (name === UNGROUPED_LABEL) return 'default';
    if (name.includes('A股') || name.includes('快讯')) return 'blue';
    if (name.includes('政策') || name.includes('宏观')) return 'purple';
    if (name.includes('量化') || name.includes('研究')) return 'cyan';
    if (name.includes('全球') || name.includes('市场')) return 'geekblue';
    return 'volcano';
  };

  const columns: ColumnsType<SourceRow> = [
    {
      title: '订阅源名称',
      dataIndex: 'name',
      key: 'name',
      width: 240,
      render: (text, row) => (
        <Space>
          {row.iconUrl ? (
            <img src={row.iconUrl} alt="" style={{ width: 18, height: 18, borderRadius: 4 }} />
          ) : (
            <GlobalOutlined style={{ color: '#1890ff', fontSize: 16 }} />
          )}
          <Text strong>{text || '(未命名)'}</Text>
          {row.type ? <Tag color="default" style={{ borderRadius: 4 }}>{row.type}</Tag> : null}
        </Space>
      ),
    },
    {
      title: '订阅地址 (Feed URL)',
      dataIndex: 'subscribeUrl',
      key: 'subscribeUrl',
      ellipsis: true,
      render: (u) => (
        <Tooltip title={u}>
          <Text type="secondary" copyable={{ tooltips: ['复制链接', '已复制'] }} ellipsis style={{ maxWidth: 360 }}>
            {u}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: '所属分类',
      dataIndex: 'folderName',
      key: 'folderName',
      width: 140,
      render: (n) => <Tag color={getFolderTagColor(n)} style={{ borderRadius: 4 }}>{n}</Tag>,
    },
    {
      title: '未读资讯',
      dataIndex: 'inboxCount',
      key: 'inboxCount',
      width: 100,
      align: 'right',
      render: (v) => (v ? <Badge count={v} overflowCount={999} /> : <Text type="secondary">0</Text>),
    },
    {
      title: '操作',
      key: 'actions',
      width: 130,
      align: 'center',
      render: (_, row) => (
        <Space size="middle">
          <Tooltip title="编辑属性">
            <Button type="text" size="small" icon={<EditOutlined />} onClick={() => openEdit(row)} style={{ borderRadius: 4 }} />
          </Tooltip>
          <Popconfirm
            title={`确定删除「${row.name || row.id}」吗？`}
            okText="删除"
            okButtonProps={{ danger: true, style: { borderRadius: 4 } }}
            cancelText="取消"
            cancelButtonProps={{ style: { borderRadius: 4 } }}
            onConfirm={() => handleDelete(row)}
          >
            <Tooltip title="删除订阅">
              <Button type="text" danger size="small" icon={<DeleteOutlined />} style={{ borderRadius: 4 }} />
            </Tooltip>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="p-6 space-y-4">
      {/* 顶部标题与统计概览 */}
      <div className="flex items-center justify-between pb-1">
        <div>
          <Title level={4} style={{ margin: 0, fontWeight: 700 }}>
            <ReadOutlined style={{ marginRight: 8, color: '#1890ff' }} />
            RSS 资讯源管理
          </Title>
          <Text type="secondary" style={{ fontSize: 13 }}>
            全网金融资讯聚合引擎 · 支持 RSS / Atom / RSSHub · 后端自动 NLP 抽取与情感计算
          </Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={refresh} style={{ borderRadius: 6 }}>
            刷新状态
          </Button>
          <Button icon={<FolderOpenOutlined />} onClick={() => setFolderOpen(true)} style={{ borderRadius: 6 }}>
            分类管理
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            style={{ borderRadius: 6 }}
            onClick={() => {
              setPreviewData(null);
              setAddOpen(true);
            }}
          >
            新增订阅源
          </Button>
        </Space>
      </div>

      {/* 状态统计卡片 */}
      <Row gutter={14}>
        <Col span={8}>
          <Card size="small" variant="borderless" style={{ background: '#f0f5ff', borderRadius: 10 }}>
            <Statistic
              title="当前活跃订阅源"
              value={rows.length}
              suffix="个"
              valueStyle={{ color: '#1d39c4', fontWeight: 700 }}
              style={{ textAlign: 'center' }}
            />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small" variant="borderless" style={{ background: '#f6ffed', borderRadius: 10 }}>
            <Statistic
              title="资讯分类目录"
              value={folders.filter((f) => f.id != null).length}
              suffix="组"
              valueStyle={{ color: '#389e0d', fontWeight: 700 }}
              style={{ textAlign: 'center' }}
            />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small" variant="borderless" style={{ background: '#fff7e6', borderRadius: 10 }}>
            <Statistic
              title="未读资讯总数"
              value={totalInboxCount}
              suffix="篇"
              valueStyle={{ color: '#d46b08', fontWeight: 700 }}
              style={{ textAlign: 'center' }}
            />
          </Card>
        </Col>
      </Row>

      {/* 订阅源列表表格 */}
      <Card styles={{ body: { padding: 0 } }} style={{ borderRadius: 10, overflow: 'hidden', border: '1px solid #e2e8f0' }}>
        <Spin spinning={loading}>
          {rows.length === 0 && !loading ? (
            <Empty
              description="暂无订阅源，点击右上角「新增订阅源」一键配置"
              style={{ padding: 40 }}
            />
          ) : (
            <Table<SourceRow>
              rowKey="id"
              dataSource={rows}
              columns={columns}
              pagination={{ pageSize: 15, showSizeChanger: true }}
              size="middle"
            />
          )}
        </Spin>
      </Card>

      {/* —— 新增 RSS 源 Modal (紧凑低矮、现代全圆角、高度严格统一) —— */}
      <Modal
        title={
          <Space size={8}>
            <div style={{ width: 24, height: 24, borderRadius: 6, background: '#e6f7ff', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <PlusOutlined style={{ color: '#1890ff', fontSize: 13 }} />
            </div>
            <span style={{ fontWeight: 600, fontSize: 15 }}>新增 RSS 订阅源</span>
          </Space>
        }
        open={addOpen}
        onCancel={() => setAddOpen(false)}
        onOk={handleAddSubmit}
        okText="确认添加"
        cancelText="取消"
        okButtonProps={{ style: { borderRadius: 6 } }}
        cancelButtonProps={{ style: { borderRadius: 6 } }}
        width={620}
        style={{ borderRadius: 12, overflow: 'hidden' }}
        styles={{ body: { padding: '14px 20px' } }}
      >
        {/* 1. 快捷精选标签条 */}
        <div style={{ background: '#f8fafc', padding: '8px 12px', borderRadius: 8, marginBottom: 12, border: '1px solid #e2e8f0' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
            <ThunderboltOutlined style={{ color: '#f59e0b', fontSize: 12 }} />
            <Text strong style={{ fontSize: 12, marginRight: 2 }}>常用源：</Text>
            {PRESET_FEEDS.map((item) => (
              <Tag
                key={item.name}
                color="blue"
                style={{ cursor: 'pointer', margin: '2px 0', borderRadius: 4, fontSize: 11, padding: '0 6px' }}
                onClick={() => applyPreset(item)}
              >
                + {item.name}
              </Tag>
            ))}
          </div>
        </div>

        <Form form={addForm} layout="vertical">
          {/* 订阅地址输入框 (统一 36px 高度) */}
          <Form.Item
            name="subscribe_url"
            label={<span style={{ fontSize: 13, fontWeight: 500 }}>订阅地址 (RSS / Atom URL)</span>}
            rules={[{ required: true, message: '请输入订阅地址' }]}
            style={{ marginBottom: 12 }}
          >
            <Input
              placeholder="https://example.com/feed.xml 或 http://quantmind-rsshub:1200/..."
              style={{ height: 36, borderRadius: 6 }}
              suffix={
                <Button
                  type="link"
                  size="small"
                  icon={<EyeOutlined />}
                  loading={previewing}
                  onClick={handlePreview}
                  style={{ padding: '0 4px', fontSize: 12, height: 28 }}
                >
                  测试预览
                </Button>
              }
            />
          </Form.Item>

          {/* 2. RSSHub 快捷生成面板 (高度 34px 统一) */}
          <div style={{ background: '#f0f9ff', padding: '10px 12px', borderRadius: 8, marginBottom: 12, border: '1px solid #bae6fd' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
              <Text type="secondary" style={{ fontSize: 11, fontWeight: 500, color: '#0369a1' }}>快捷生成 (本地 RSSHub 服务):</Text>
              <Text type="secondary" style={{ fontSize: 10 }}>回车自动填入</Text>
            </div>
            <Row gutter={8}>
              <Col span={8}>
                <Input
                  placeholder="Twitter 用户名"
                  style={{ height: 34, borderRadius: 6, background: '#ffffff', borderColor: '#bae6fd' }}
                  onPressEnter={(e) => {
                    const u = (e.target as HTMLInputElement).value.trim().replace(/^@/, '');
                    if (u) addForm.setFieldValue('subscribe_url', `http://quantmind-rsshub:1200/twitter/user/${u}`);
                  }}
                />
              </Col>
              <Col span={8}>
                <Input
                  placeholder="微博用户 UID"
                  style={{ height: 34, borderRadius: 6, background: '#ffffff', borderColor: '#bae6fd' }}
                  onPressEnter={(e) => {
                    const u = (e.target as HTMLInputElement).value.trim();
                    if (u) addForm.setFieldValue('subscribe_url', `http://quantmind-rsshub:1200/weibo/user/${u}`);
                  }}
                />
              </Col>
              <Col span={8}>
                <Input
                  placeholder="雪球用户 ID"
                  style={{ height: 34, borderRadius: 6, background: '#ffffff', borderColor: '#bae6fd' }}
                  onPressEnter={(e) => {
                    const u = (e.target as HTMLInputElement).value.trim();
                    if (u) addForm.setFieldValue('subscribe_url', `http://quantmind-rsshub:1200/xueqiu/user/${u}`);
                  }}
                />
              </Col>
            </Row>
          </div>

          {/* 3. 预览反馈结果 */}
          {previewData ? (
            <Alert
              type={previewData.subscribed ? 'warning' : 'success'}
              showIcon
              style={{ marginBottom: 12, borderRadius: 6, padding: '6px 12px' }}
              message={
                <Space>
                  <Text strong style={{ fontSize: 12 }}>{previewData.title || '(无标题)'}</Text>
                  {previewData.subscribed ? <Tag color="warning" style={{ borderRadius: 4 }}>已在列表中</Tag> : <Tag color="success" style={{ borderRadius: 4 }}>解析有效</Tag>}
                </Space>
              }
              description={
                previewData.description ? (
                  <Text type="secondary" ellipsis style={{ fontSize: 11, display: 'block', marginTop: 2 }}>
                    {previewData.description}
                  </Text>
                ) : null
              }
            />
          ) : null}

          {/* 4. 自定义名称与分类并排 (统一 36px 高度) */}
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="name" label={<span style={{ fontSize: 13 }}>自定义源名称 (可选)</span>} style={{ marginBottom: 4 }}>
                <Input placeholder="留空则自动提取源标题" style={{ height: 36, borderRadius: 6 }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="folder_id" label={<span style={{ fontSize: 13 }}>归入分类目录</span>} initialValue={0} style={{ marginBottom: 4 }}>
                <Select options={folderOptions} style={{ width: '100%', height: 36, borderRadius: 6 }} />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      {/* —— 编辑 modal (高度与 AntD5 规范统一) —— */}
      <Modal
        title="编辑订阅源"
        open={editOpen}
        onCancel={() => setEditOpen(false)}
        onOk={handleEditSubmit}
        okText="保存"
        cancelText="取消"
        okButtonProps={{ style: { borderRadius: 6 } }}
        cancelButtonProps={{ style: { borderRadius: 6 } }}
        width={560}
        style={{ borderRadius: 12, overflow: 'hidden' }}
        styles={{ body: { padding: '14px 20px' } }}
      >
        <Form form={editForm} layout="vertical">
          <Form.Item name="subscribe_url" label="订阅地址" style={{ marginBottom: 10 }}>
            <Input disabled style={{ height: 36, borderRadius: 6 }} />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入名称' }]} style={{ marginBottom: 10 }}>
            <Input style={{ height: 36, borderRadius: 6 }} />
          </Form.Item>
          <Form.Item name="folder_id" label="所在分类目录" style={{ marginBottom: 10 }}>
            <Select options={folderOptions} style={{ height: 36, borderRadius: 6 }} />
          </Form.Item>
          <Form.Item
            name="fetch_interval_minutes"
            label="抓取间隔（分钟）"
            style={{ marginBottom: 10 }}
          >
            <InputNumber min={1} max={1440} style={{ width: '100%', height: 36, borderRadius: 6 }} />
          </Form.Item>
          <Space size="large">
            <Form.Item name="enabled" label="启用抓取" valuePropName="checked" style={{ marginBottom: 0 }}>
              <Switch />
            </Form.Item>
            <Form.Item
              name="crawl_full_content"
              label="深度抓取全文"
              valuePropName="checked"
              tooltip="开启后将尝试自动解析文章正文完整内容"
              style={{ marginBottom: 0 }}
            >
              <Switch />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      {/* —— 文件夹管理 modal —— */}
      <Modal
        title={
          <Space>
            <FolderOpenOutlined style={{ color: '#1890ff' }} />
            <span>资讯分类目录管理</span>
          </Space>
        }
        open={folderOpen}
        onCancel={() => setFolderOpen(false)}
        footer={null}
        width={500}
        style={{ borderRadius: 12, overflow: 'hidden' }}
        styles={{ body: { padding: '14px 20px' } }}
      >
        <Space.Compact style={{ width: '100%', marginBottom: 14 }}>
          <Input
            placeholder="输入新分类名称 (如：A股快讯、量化研究)"
            value={folderName}
            onChange={(e) => setFolderName(e.target.value)}
            onPressEnter={handleAddFolder}
            style={{ height: 36, borderRadius: '6px 0 0 6px' }}
          />
          <Button type="primary" icon={<FolderAddOutlined />} onClick={handleAddFolder} style={{ height: 36, borderRadius: '0 6px 6px 0' }}>
            新建分类
          </Button>
        </Space.Compact>

        <Table<HuntlyFolder>
          rowKey={(f) => String(f.id ?? 0)}
          size="small"
          pagination={false}
          dataSource={folders.filter((f) => f.id != null)}
          columns={[
            {
              title: '分类名称',
              dataIndex: 'name',
              key: 'name',
              render: (n) => <Tag color={getFolderTagColor(n || '')} style={{ borderRadius: 4 }}>{n}</Tag>,
            },
            {
              title: '包含订阅源',
              key: 'count',
              width: 110,
              align: 'right',
              render: (_, f) => <Text strong>{(f.connectors || []).length} 个</Text>,
            },
            {
              title: '操作',
              key: 'actions',
              width: 140,
              align: 'center',
              render: (_, f) => (
                <Space>
                  <Button size="small" type="link" onClick={() => handleRenameFolder(f)}>
                    重命名
                  </Button>
                  <Popconfirm
                    title={`确定删除分类「${f.name}」？所属订阅源将移入「未分组」`}
                    onConfirm={() => handleDeleteFolder(f)}
                    okText="删除"
                    okButtonProps={{ danger: true, style: { borderRadius: 4 } }}
                    cancelText="取消"
                    cancelButtonProps={{ style: { borderRadius: 4 } }}
                  >
                    <Button size="small" type="link" danger>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Modal>
    </div>
  );
};

export default AdminRssSources;
