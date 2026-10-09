/** Versioned QuantDB factor sources for model training. */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert, Button, Card, Col, Form, Input, Modal, Row, Select, Space, Statistic,
  Switch, Table, Tag, Tooltip, Typography, message,
} from 'antd';
import { DatabaseOutlined, EditOutlined, InfoCircleOutlined, PlusOutlined, ReloadOutlined, RocketOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { adminService } from '../services/adminService';
import { RdMinedMaterializePanel } from './RdMinedMaterializePanel';

const { Title, Text } = Typography;

// 市场切换（数据源选项以后端 /sources labels 为准，此表仅作加载前的占位）
  const MARKET_OPTIONS = [
    { value: 'CN', label: 'A股' },
    { value: 'HK', label: '港股' },
    { value: 'US', label: '美股' },
    { value: 'CRYPTO', label: '区块链' },
    { value: 'FUTURES', label: '期货' },
    { value: 'CUSTOM', label: '自定义市场' },
  ];

const MARKET_SOURCE_FALLBACK: Record<string, { value: string; label: string }[]> = {
  CN: [
    { value: 'l1_factors', label: 'L1 因子（默认）' },
    { value: 'l1_l2_factors', label: 'L1 + L2 合并宽表' },
    { value: 'l2_factors', label: 'L2 因子' },
  ],
  HK: [
    { value: 'l1_factors', label: 'L1 因子（默认）' },
    { value: 'ccass_factors', label: 'CCASS 持仓结构' },
    { value: 'south_factors', label: '南向资金结构' },
  ],
  US: [{ value: 'l1_factors', label: 'L1 因子（默认）' }],
  CRYPTO: [{ value: 'l1_factors', label: 'L1 因子（默认）' }],
  FUTURES: [{ value: 'l1_factors', label: 'L1 因子（默认）' }],
  CUSTOM: [{ value: 'l1_factors', label: 'L1 因子（默认）' }],
};

const CATEGORY_OPTIONS = [
  ['momentum', '动量'], ['volatility', '波动与风险'], ['money_flow', '成交额与资金'],
  ['turnover', '换手与流动性'], ['volume_turnover', '成交量与换手率'], ['technical', '技术指标'], ['fundamental', '基本面与估值'],
  ['style', '截面风格'], ['industry', '行业轮动'], ['chip', '筹码分布'],
  ['concept', '概念板块'], ['money_flow_l2', '逐笔资金流'], ['order_flow', '撤单与委托流'],
  ['toxicity', '信息不对称与毒性'], ['microstructure', '价差与微观结构'], ['holding_structure', '持仓结构'], ['other', '其他因子'],
].map(([value, label]) => ({ value, label }));

type Mapping = {
  mapping_id: string; source_dataset: string; source_column: string; key: string;
  feature_name: string; enabled: boolean; default_selected: boolean; required: boolean;
  category_id?: string; category_name?: string; order_no?: number;
  /** 长描述：B 特征字典用户编辑优先，缺省回退代码字典精确条目 */
  explanation?: string;
};

type FactorDirectoryRow = {
  row_no: number;
  source_column: string;
  factor: string;
  style: string;
  explanation: string;
  is_present: boolean;
  mapping?: Mapping;
};

/**
 * 数一份目录版本里**启用**的特征数（草稿与已发布版本同一形状，一个函数通吃）。
 *
 * 口径必须是 enabled，**不能**用后端给的 `feature_count`：那个字段是
 * `sum(category.feature_count)`，而计数器在遍历 mapping 时每个都 +1，
 * 不按 enabled 过滤（`quantdb_factor_catalog.py:471`）；训练侧却只用启用的
 * （`trainingUtils.tsx:1145` 的 `feature.enabled !== false`）。发布确认里拿
 * 含 disabled 的数去比大小，会在口径其实没变时报「减少」、真减少时又少报。
 *
 * 这里用 `!== false` 而不是真值判断，是为了和训练侧**逐字同口径**：
 * 后端两个入口都发 `bool(...)`，正常不会有 undefined，但比较口径一旦分家，
 * 这个确认框就开始撒谎。
 */
const countEnabledFeatures = (catalog: any): number =>
  (catalog?.categories || [])
    .flatMap((category: any) => category.features || [])
    .filter((feature: any) => feature.enabled !== false).length;

function unavailableSourceHint(status: Record<string, any>, sourceLabel: string): string {
  if (!status.files) {
    return `尚未同步 ${sourceLabel} 数据`;
  }
  if ((status.missing_required || []).length > 0) {
    return '数据字段尚未满足训练条件';
  }
  return '暂未满足直读训练条件';
}

function unavailableSourceAction(status: Record<string, any>): string {
  if (!status.files) return '请在“数据下载”中勾选并同步，完成后点击“字段发现”。';
  if ((status.missing_required || []).length > 0) return '请补齐行情字段后重新执行“字段发现”。';
  return '请刷新字段状态后重试。';
}

export const AdminTrainingDatasets: React.FC = () => {
  const [market, setMarket] = useState('CN');
  const [source, setSource] = useState('l1_factors');
  const [sources, setSources] = useState<Record<string, any>>({});
  const [sourceLabels, setSourceLabels] = useState<Record<string, string>>({});
  const [fields, setFields] = useState<any[]>([]);
  const [published, setPublished] = useState<any | null>(null);
  const [draft, setDraft] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Mapping | null>(null);
  const [keyword, setKeyword] = useState('');
  const [form] = Form.useForm();

  const sourceOptions = useMemo(() => {
    const ids = Object.keys(sources);
    if (ids.length > 0 && sourceLabels[ids[0]]) {
      return ids.map((id) => ({ value: id, label: sourceLabels[id] }));
    }
    return MARKET_SOURCE_FALLBACK[market] || MARKET_SOURCE_FALLBACK.CN;
  }, [sources, sourceLabels, market]);

  // 市场切换时重置数据源为后端默认
  const handleMarketChange = (next: string) => {
    setMarket(next);
    setSources({});
    setSourceLabels({});
    setFields([]);
    setDraft(null);
    setPublished(null);
  };

  const mappings = useMemo<Mapping[]>(
    () => (draft?.categories || []).flatMap((category: any) => category.features || []), [draft],
  );
  const pending = useMemo(() => {
    const mappingsByColumn = new Map(mappings.map(mapping => [mapping.source_column, mapping]));
    return fields.filter((field) => {
      const mapping = mappingsByColumn.get(field.column_name);
      return !mapping || mapping.category_id === 'other' || mapping.feature_name === mapping.key;
    });
  }, [fields, mappings]);
  const factorRows = useMemo<FactorDirectoryRow[]>(() => {
    const mappingsByColumn = new Map(mappings.map(mapping => [mapping.source_column, mapping]));
    return fields
      .map((field) => {
        const mapping = mappingsByColumn.get(field.column_name);
        return {
          row_no: 0,
          source_column: field.column_name,
          factor: mapping?.key || field.column_name,
          style: mapping?.category_name || field.dictionary?.category_name || '待分类',
          explanation: mapping?.explanation || mapping?.feature_name || field.dictionary?.explanation || '尚未填写中文解释',
          is_present: Boolean(field.is_present),
          mapping,
        };
      })
      .sort((a, b) => a.factor.localeCompare(b.factor))
      .map((row, index) => ({ ...row, row_no: index + 1 }));
  }, [fields, mappings]);
  const visibleFactorRows = useMemo(() => {
    const term = keyword.trim().toLowerCase();
    if (!term) return factorRows;
    return factorRows.filter(row => [row.factor, row.style, row.explanation]
      .some(value => value.toLowerCase().includes(term)));
  }, [factorRows, keyword]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const sourceResult = await adminService.getQuantDBFactorSources(market);
      const statuses = sourceResult.sources || {};
      setSources(statuses);
      setSourceLabels(sourceResult.labels || {});
      const ids = Object.keys(statuses);
      let activeSource = source;
      if (ids.length > 0 && !ids.includes(activeSource)) {
        // 当前数据源不属于该市场：切到市场默认源，由 source 变化重新触发加载
        activeSource = sourceResult.default_source && ids.includes(sourceResult.default_source)
          ? sourceResult.default_source : ids[0];
        setSource(activeSource);
        setFields([]);
        setDraft(null);
        setPublished(null);
        setLoading(false);
        return;
      }
      const fieldsResult = await adminService.getQuantDBFactorFields(activeSource, market);
      setFields(fieldsResult.fields || []);
      try {
        setPublished(await adminService.getQuantDBFactorCatalog(activeSource, undefined, market));
      } catch { setPublished(null); }
      if (draft) {
        // 手上已经有草稿：只按 id 刷新它，**不**再去问「这个库有哪些版本」。
        // 无条件重新认领的话，一个后来出现的更新草稿会把用户正在编辑的这份顶掉，
        // 而左侧目录里已经改好的分类与中文解释会**静默失去落点**（用户不会收到
        // 任何提示，只会发现改动没了）。
        try { setDraft(await adminService.getQuantDBFactorCatalog(activeSource, draft.version_id, market)); }
        catch { setDraft(null); }
      } else {
        // 首屏 / 换库：认领该来源库现有的最新草稿。
        //
        // 这一支此前根本不存在——取草稿的逻辑只挂在 `if (draft)` 下，而首屏 `draft`
        // 恒为 null。于是本页**只认自己当场新建的草稿**，别处写进来的（因子研究页
        // 「注册到训练目录」就是典型）一律看不见；而发布按钮只在这一页，整条链就
        // 死在这里：注册进去了 → 看不见 → 发布不了 → 训练永远用不上。
        try {
          const versions = await adminService.listQuantDBFactorVersions(activeSource, market);
          const existing = (versions?.versions || []).find((v: any) => v.status === 'draft');
          // 认领失败就保持「未创建」，不静默假装成功。
          // 这里不会和上面的分支来回弹成死循环：认领失败时 draft 本就是 null，
          // 再置一次 null 不触发重渲染，依赖不变也就不会重跑。
          setDraft(
            existing
              ? await adminService.getQuantDBFactorCatalog(activeSource, existing.version_id, market)
              : null,
          );
        } catch (error: any) {
          message.error(error?.response?.data?.detail || '读取目录版本失败，草稿可能未显示');
          setDraft(null);
        }
      }
    } catch (error: any) {
      message.error(error?.response?.data?.detail || error?.message || '加载训练数据集失败');
    } finally { setLoading(false); }
  }, [source, market, draft?.version_id]);

  useEffect(() => { load(); }, [load]);

  const refreshDiscovery = async () => {
    setLoading(true);
    try {
      await adminService.refreshQuantDBFactorSources(market);
      message.success('字段发现已刷新');
      await load();
    } catch (error: any) {
      message.error(error?.response?.data?.detail || '字段发现失败');
    } finally { setLoading(false); }
  };

  const createDraft = async () => {
    try {
      const values = await form.validateFields();
      setCreating(true);
      const created = await adminService.createQuantDBFactorDraft(values.version_name, source, market);
      const seeded = await adminService.seedQuantDBFactorDraft(created.version_id);
      setDraft(await adminService.getQuantDBFactorCatalog(source, created.version_id, market));
      setCreating(false);
      message.success(
        seeded?.default_selected_fields > 0
          ? '草稿已创建：全部字段已默认启用，核心因子已默认勾选'
          : '草稿已创建：全部字段已默认启用，请手动勾选训练因子',
      );
    } catch (error: any) {
      setCreating(false);
      if (error?.errorFields) return;
      message.error(error?.response?.data?.detail || error?.message || '创建草稿失败；请先执行字段刷新');
    }
  };

  const saveMapping = async (mapping: Mapping) => {
    if (!draft) return;
    try {
      await adminService.saveQuantDBFactorMapping(draft.version_id, {
        mapping_id: mapping.mapping_id,
        source_dataset: source,
        source_column: mapping.source_column,
        feature_key: mapping.key,
        display_name: mapping.feature_name,
        category_id: mapping.category_id || 'other',
        category_name: mapping.category_name || '其他因子',
        enabled: mapping.enabled,
        default_selected: mapping.default_selected,
        required: mapping.required,
        sort_order: mapping.order_no || 0,
      });
      setDraft(await adminService.getQuantDBFactorCatalog(source, draft.version_id));
    } catch (error: any) {
      message.error(error?.response?.data?.detail || '保存映射失败');
    }
  };

  const publish = () => {
    if (!draft) return;
    // 发布是**即时改变线上口径**的动作：后端把当前 published 转 archived、
    // 把这份草稿扶正（quantdb_factor_catalog.py:866-890），此后新的训练任务
    // 就用新口径了。此前这里直接 POST、没有确认——但直到草稿能在这页显示之前，
    // 这个按钮根本够不着，所以它其实是修复草稿发现之后**才第一次真正可点**的，
    // 确认框是随那次修复一起必须补上的。
    const nextEnabled = countEnabledFeatures(draft);
    const currentEnabled = countEnabledFeatures(published);
    // 只在「有旧版本可比」且「确实变小」时告警。首次发布（published 为空）
    // 不是缩小，不该吓人。
    const shrinks = Boolean(published) && nextEnabled < currentEnabled;
    Modal.confirm({
      title: '发布这份草稿？',
      okText: '发布',
      cancelText: '取消',
      okButtonProps: shrinks ? { danger: true } : undefined,
      content: <div className="space-y-2">
        <div>
          将把「<Text strong>{draft.version_name}</Text>」发布为线上版本
          （启用 <Text strong>{nextEnabled}</Text> 个特征）
          {published ? <>，替换当前「<Text strong>{published.version_name}</Text>」</> : null}。
          仅后续训练任务使用它。
        </div>
        {shrinks ? (
          <div className="text-red-600">
            注意：线上启用特征将从 <Text strong>{currentEnabled}</Text> 个
            减少到 <Text strong>{nextEnabled}</Text> 个。
          </div>
        ) : null}
        {published ? (
          <div className="text-xs text-gray-500">
            当前版本会转为「已归档」，仍保留在库里，可再复制为草稿发回来。
          </div>
        ) : null}
      </div>,
      onOk: async () => {
        try {
          await adminService.publishQuantDBFactorDraft(draft.version_id);
          message.success('映射版本已发布；仅后续训练任务会使用它');
          setDraft(null);
          await load();
        } catch (error: any) { message.error(error?.response?.data?.detail || '发布失败'); }
      },
    });
  };

  const clonePublished = async () => {
    if (!published) return;
    try {
      const created = await adminService.cloneQuantDBFactorCatalog(
        published.version_id, `${published.version_name} 副本`,
      );
      setDraft(await adminService.getQuantDBFactorCatalog(source, created.version_id));
      message.success('已复制为草稿，可安全编辑');
    } catch (error: any) { message.error(error?.response?.data?.detail || '复制发布版本失败'); }
  };

  const factorColumns: ColumnsType<FactorDirectoryRow> = [
    { title: '编号', dataIndex: 'row_no', width: 68, align: 'center' },
    { title: '因子', dataIndex: 'factor', width: 200, render: (value) => <Text code>{value}</Text> },
    { title: '风格', dataIndex: 'style', width: 130, render: (value) => <Tag color={value === '待分类' ? 'default' : 'blue'}>{value}</Tag> },
    { title: '中文解释', dataIndex: 'explanation', ellipsis: true, render: (value) => (
      <Tooltip title={value} placement="topLeft"><Text ellipsis className="text-xs">{value}</Text></Tooltip>
    ) },
    { title: '状态', width: 80, render: (_, row) => row.is_present ? <Tag color="green">已发现</Tag> : <Tag>已删除</Tag> },
    { title: '训练配置', width: 230, render: (_, row) => row.mapping ? <Space size={8} wrap>
      <Tooltip title="启用：该因子参与训练（左开关）"><Space size={2}>启用<Switch size="small" checked={row.mapping.enabled} onChange={checked => saveMapping({ ...row.mapping!, enabled: checked })} /></Space></Tooltip>
      <Tooltip title="默认：训练时默认勾选该因子（右开关）"><Space size={2}>默认<Switch size="small" checked={row.mapping.default_selected} disabled={!row.mapping.enabled} onChange={checked => saveMapping({ ...row.mapping!, default_selected: checked })} /></Space></Tooltip>
      <Button type="text" size="small" icon={<EditOutlined />} onClick={() => {
        setEditing(row.mapping!);
        form.setFieldsValue({ feature_key: row.mapping!.key, display_name: row.mapping!.feature_name, category_id: row.mapping!.category_id, category_name: row.mapping!.category_name });
      }} />
    </Space> : <Text type="secondary" className="text-xs">创建草稿后配置</Text> },
  ];

  return <div className="p-6 space-y-4">
    <div className="flex items-center justify-between">
      <div><Title level={4} className="!mb-0"><DatabaseOutlined /> 模型训练数据集</Title>
        <Text type="secondary">仅读取各市场 ML 数据集原始因子；映射草稿发布后才影响新的训练任务。</Text></div>
      <Space wrap>
        <Select value={market} options={MARKET_OPTIONS} style={{ width: 100 }} onChange={handleMarketChange} />
        <Select value={source} options={sourceOptions} style={{ width: 220 }} loading={loading && !sourceOptions.length} onChange={value => { setSource(value); setDraft(null); }} />
        <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
        <Button type="primary" icon={<ReloadOutlined />} loading={loading} onClick={refreshDiscovery}>字段发现</Button>
      </Space>
    </div>

    {market === 'CUSTOM' && (
      <Alert
        type="warning"
        showIcon
        message="自定义市场：自传 parquet，后端仅扫描因子"
        description="宿主机 ./data/quantcustom/6_ml_datasets/l1_factors/dt=YYYYMMDD/*.parquet（bind mount 自动同步进容器 /data/quantcustom）；至少包含 symbol + date（或 dt 分区）+ 自定因子列，不强制 OHLCV。缺 close 列时仅扫描浏览、无法构建训练标签。放好文件后点「字段发现」，再建草稿发布。"
      />
    )}

    {/* RD 挖掘因子物化：挖掘 → rd_mined 训练库的中间环节，落在 CUSTOM 市场 */}
    {market === 'CUSTOM' && (
      <RdMinedMaterializePanel onCompleted={() => { void load(); }} />
    )}

    <Row gutter={[16, 16]}>
      {sourceOptions.map(option => {
        const status = sources[option.value] || {};
        const unavailableHint = unavailableSourceHint(status, option.label.replace('（默认）', ''));
        const unavailableAction = unavailableSourceAction(status);
        return <Col xs={24} md={8} key={option.value}><Card size="small" title={option.label}>
          <Statistic title={status.ready ? '可用于直读训练' : '等待数据同步'} value={status.files || 0} suffix="个分区文件" valueStyle={{ color: status.ready ? '#3f8600' : '#cf1322', fontSize: 18 }} />
          <div className="mt-2 text-xs text-gray-500">覆盖：{status.min_date || '--'} ～ {status.max_date || '--'} · {status.column_count || 0} 字段</div>
          {!status.ready && <div className="mt-3 flex items-start gap-1.5 text-xs leading-5 text-amber-700">
            <InfoCircleOutlined className="mt-1 shrink-0" />
            <span><span className="font-medium">{unavailableHint}</span> · {unavailableAction}</span>
          </div>}
        </Card></Col>;
      })}
    </Row>

    <Alert type="info" showIcon message="每份目录版本只对应一个来源库" description="默认 L1 因子。跨源训练不在本页拼接：请在「模型训练」页选好锚库后，用「附加因子库」加入其他已发布目录的库（各库版本仍是各自独立发布的这一份）。数据或 OHLCV 覆盖不完整时，直读训练入口会拒绝提交。" />

    <Row gutter={[16, 16]}>
      <Col xs={24} lg={18}><Card title="因子目录" extra={<Space><Input allowClear value={keyword} onChange={event => setKeyword(event.target.value)} placeholder="搜索因子、分类或中文解释" style={{ width: 220 }} /><Tag>{factorRows.length} 个已发现字段</Tag>{draft && <Tag color="orange">{pending.length} 个待分类</Tag>}</Space>}>
        <div className="mb-3 text-xs text-gray-500">内置字典已依据 300 因子设计方案填充默认分类与中文解释；草稿中的修改优先于字典，发布后才影响新训练任务。</div>
        <Table size="small" rowKey="source_column" dataSource={visibleFactorRows} columns={factorColumns} pagination={{ pageSize: 20, showSizeChanger: false }} scroll={{ x: 880, y: 500 }} />
      </Card></Col>
      <Col xs={24} lg={6}><Card size="small" title="分类映射草稿" extra={draft ? <Tag color="blue">编辑中</Tag> : <Tag>未创建</Tag>}>
        {draft ? <div className="space-y-3">
          <div><Text strong className="block truncate">{draft.version_name}</Text><Text type="secondary" className="text-xs">{mappings.length} 个映射 · {mappings.filter(item => item.enabled).length} 个启用</Text></div>
          <Alert type="info" showIcon message="在左侧目录完成分类与中文解释" className="text-xs" />
          <Button block type="primary" icon={<RocketOutlined />} onClick={publish}>发布此草稿</Button>
        </div> : <Form form={form} layout="vertical" onFinish={createDraft}>
          <Text type="secondary" className="block mb-3 text-xs">新建后自动导入当前数据源全部字段。</Text>
          <Form.Item name="version_name" rules={[{ required: true, message: '请输入版本名称' }]}><Input placeholder="例如：2026-08 默认因子集" /></Form.Item>
          <Button block type="primary" htmlType="submit" loading={creating} icon={<PlusOutlined />}>新建草稿</Button>
        </Form>}
      </Card></Col>
    </Row>

    <Card title="已发布特征集版本" extra={published ? <Space><Tag color="green">当前活动版本</Tag><Button size="small" disabled={!!draft} onClick={clonePublished}>复制为草稿</Button></Space> : <Tag>未发布</Tag>}>
      {published ? <Space wrap><Text strong>{published.version_name}</Text><Tag>{published.version_id}</Tag><Tag>{published.feature_count} 个映射字段</Tag><Text type="secondary">发布后不可修改；需要调整时创建新的草稿版本。</Text></Space> : <Text type="secondary">此数据源尚未发布映射版本，训练页不会将它作为 QuantDB 直读训练集。</Text>}
    </Card>

    <Modal title="编辑逻辑映射" open={!!editing} onCancel={() => setEditing(null)} onOk={async () => {
      const values = await form.validateFields(); if (editing) { await saveMapping({ ...editing, ...values }); setEditing(null); }
    }}>
      <Form form={form} layout="vertical"><Form.Item name="feature_key" label="逻辑因子 ID" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="display_name" label="中文解释" rules={[{ required: true }]}><Input.TextArea rows={3} /></Form.Item>
        <Form.Item name="category_id" label="分类" rules={[{ required: true }]}><Select options={CATEGORY_OPTIONS} onChange={(value) => form.setFieldValue('category_name', CATEGORY_OPTIONS.find(item => item.value === value)?.label)} /></Form.Item>
        <Form.Item name="category_name" hidden rules={[{ required: true }]}><Input /></Form.Item></Form>
    </Modal>
  </div>;
};
