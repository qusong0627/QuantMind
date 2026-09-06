/**
 * 特征字典管理（管理员）
 *
 * 编辑模型训练特征字典：分类增删改、特征增删改、启用/禁用、保存到后端 JSON 文件。
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Card,
  Checkbox,
  Divider,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
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
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  DatabaseOutlined,
  FolderOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { adminService } from '../services/adminService';
import type { AdminModelFeatureCatalog, AdminModelFeatureCategory, AdminModelFeatureItem } from '../types';

const { Title, Text } = Typography;

const MARKET_OPTIONS = [
  { value: 'CN', label: 'A股', color: 'red' },
  { value: 'HK', label: '港股', color: 'blue' },
  { value: 'US', label: '美股', color: 'green' },
  { value: 'CRYPTO', label: '加密', color: 'purple' },
  { value: 'FUTURES', label: '期货', color: 'orange' },
  { value: 'CUSTOM', label: '自定义市场', color: 'cyan' },
];

const ALL_MARKETS = MARKET_OPTIONS.map(m => m.value);

function marketsOf(feat: AdminModelFeatureItem): string[] {
  return feat.markets && feat.markets.length > 0 ? feat.markets : ALL_MARKETS;
}

function matchesMarket(feat: AdminModelFeatureItem, market: string): boolean {
  if (!market || market === 'ALL') return true;
  return marketsOf(feat).includes(market);
}

// ─── 辅助函数 ────────────────────────────────────────────────────────────────

function generateFeatureId(): string {
  return 'feat_' + Math.random().toString(36).slice(2, 10);
}

const KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

// ─── 主组件 ──────────────────────────────────────────────────────────────────

export const AdminFeatureCatalog: React.FC = () => {
  const [catalog, setCatalog] = useState<AdminModelFeatureCatalog | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [selectedCatId, setSelectedCatId] = useState<string | null>(null);
  const [marketFilter, setMarketFilter] = useState<string>('ALL');
  const [keyword, setKeyword] = useState('');
  // 全量目录（仅用于胶囊计数；按市场过滤后 catalog 为子集，计数仍以全量为准）
  const [fullCatalog, setFullCatalog] = useState<AdminModelFeatureCatalog | null>(null);

  // 分类编辑
  const [catModalOpen, setCatModalOpen] = useState(false);
  const [editingCat, setEditingCat] = useState<AdminModelFeatureCategory | null>(null);
  const [catForm] = Form.useForm();

  // 特征编辑
  const [featModalOpen, setFeatModalOpen] = useState(false);
  const [editingFeat, setEditingFeat] = useState<AdminModelFeatureItem | null>(null);
  const [featForm] = Form.useForm();
  const fileInputRef = React.useRef<HTMLInputElement>(null);

  const loadCatalog = useCallback(async (market?: string) => {
    setLoading(true);
    setLoadError(null);
    try {
      const activeMarket = typeof market === 'string' ? market : marketFilter;
      const data = await adminService.getModelFeatureCatalog(
        activeMarket && activeMarket !== 'ALL' ? activeMarket : undefined,
      );
      setCatalog(data);
      if (!activeMarket || activeMarket === 'ALL') {
        setFullCatalog(data);
      }
      if (!data.categories?.length) {
        setSelectedCatId(null);
      }
      setDirty(false);
    } catch (e: any) {
      const msg = e?.response?.data?.detail || e?.message || '加载特征字典失败';
      setLoadError(msg);
      message.error(msg);
    } finally {
      setLoading(false);
    }
  }, [marketFilter]);

  useEffect(() => { loadCatalog(); }, [loadCatalog]);

  const handleMarketChange = (next: string) => {
    setMarketFilter(next);
    setSelectedCatId(null);
    setKeyword('');
    loadCatalog(next);
  };

  // 首次加载后默认选中第一个分类
  useEffect(() => {
    if (catalog?.categories?.length && !selectedCatId) {
      setSelectedCatId(catalog.categories[0].id);
    }
  }, [catalog, selectedCatId]);

  const markDirty = () => setDirty(true);

  // ─── 保存 ────────────────────────────────────────────────────────────────

  const handleSave = async () => {
    if (!catalog) return;
    setSaving(true);
    try {
      const resp = await adminService.updateFeatureCatalog(catalog);
      message.success(`已保存 ${resp.feature_count} 个特征`);
      setDirty(false);
    } catch {
      message.error('保存失败');
    } finally {
      setSaving(false);
    }
  };

  // ─── 分类 CRUD ────────────────────────────────────────────────────────────

  const openAddCategory = () => {
    setEditingCat(null);
    catForm.resetFields();
    setCatModalOpen(true);
  };

  const openEditCategory = (cat: AdminModelFeatureCategory) => {
    setEditingCat(cat);
    catForm.setFieldsValue({ id: cat.id, name: cat.name, order: cat.order });
    setCatModalOpen(true);
  };

  const handleSaveCategory = async () => {
    const values = await catForm.validateFields();
    if (!catalog) return;

    const cats = [...catalog.categories];
    if (editingCat) {
      const idx = cats.findIndex(c => c.id === editingCat.id);
      if (idx >= 0) {
        cats[idx] = { ...cats[idx], id: values.id, name: values.name, order: values.order };
      }
    } else {
      if (cats.some(c => c.id === values.id)) {
        message.error('分类 ID 已存在');
        return;
      }
      cats.push({
        id: values.id,
        name: values.name,
        order: values.order ?? cats.length,
        feature_count: 0,
        features: [],
      });
    }
    cats.sort((a, b) => (a.order || 0) - (b.order || 0));
    setCatalog({ ...catalog, categories: cats });
    setSelectedCatId(values.id);
    setCatModalOpen(false);
    markDirty();
  };

  const handleDeleteCategory = (catId: string) => {
    if (!catalog) return;
    const cats = catalog.categories.filter(c => c.id !== catId);
    setCatalog({ ...catalog, categories: cats });
    if (selectedCatId === catId) {
      setSelectedCatId(cats[0]?.id ?? null);
    }
    markDirty();
  };

  // ─── 特征 CRUD ────────────────────────────────────────────────────────────

  const selectedCat = catalog?.categories.find(c => c.id === selectedCatId) ?? null;

  const marketCounts = React.useMemo(() => {
    const counts: Record<string, number> = { ALL: 0 };
    for (const m of ALL_MARKETS) counts[m] = 0;
    for (const cat of (fullCatalog ?? catalog)?.categories ?? []) {
      for (const f of cat.features) {
        counts.ALL += 1;
        for (const m of marketsOf(f)) {
          if (counts[m] !== undefined) counts[m] += 1;
        }
      }
    }
    return counts;
  }, [fullCatalog, catalog]);

  const visibleFeatures = React.useMemo(() => {
    const feats = selectedCat?.features ?? [];
    const term = keyword.trim().toLowerCase();
    return feats.filter(f => {
      if (!matchesMarket(f, marketFilter)) return false;
      if (!term) return true;
      return [f.key, f.feature_name, f.explanation ?? '', f.formula, f.source_table_fields]
        .some(v => (v || '').toLowerCase().includes(term));
    });
  }, [selectedCat, marketFilter, keyword]);

  const openAddFeature = () => {
    setEditingFeat(null);
    featForm.resetFields();
    featForm.setFieldsValue({ markets: ALL_MARKETS, explanation: '' });
    setFeatModalOpen(true);
  };

  const openEditFeature = (feat: AdminModelFeatureItem) => {
    setEditingFeat(feat);
    featForm.setFieldsValue({
      key: feat.key,
      feature_name: feat.feature_name,
      explanation: feat.explanation ?? '',
      formula: feat.formula,
      source_table_fields: feat.source_table_fields,
      markets: marketsOf(feat),
    });
    setFeatModalOpen(true);
  };

  const handleSaveFeature = async () => {
    const values = await featForm.validateFields();
    if (!catalog || !selectedCatId) return;

    // 处理 markets：全选时设为空数组（表示适用所有市场）
    const markets = values.markets && values.markets.length === ALL_MARKETS.length ? [] : (values.markets || []);
    const explanation = (values.explanation ?? '').trim().slice(0, 500);

    const cats = catalog.categories.map(cat => {
      if (cat.id !== selectedCatId) return cat;
      const features = [...cat.features];
      if (editingFeat) {
        const idx = features.findIndex(f => f.key === editingFeat.key);
        if (idx >= 0) {
          features[idx] = { ...features[idx], ...values, explanation, markets };
        }
      } else {
        if (features.some(f => f.key === values.key)) {
          message.error('特征 key 已存在');
          return cat;
        }
        features.push({
          feature_id: generateFeatureId(),
          key: values.key,
          feature_name: values.feature_name,
          explanation,
          formula: values.formula || '',
          source_table_fields: values.source_table_fields || '',
          enabled: true,
          order_no: features.length + 1,
          markets,
        });
      }
      return { ...cat, features, feature_count: features.length };
    });
    setCatalog({ ...catalog, categories: cats });
    setFeatModalOpen(false);
    markDirty();
  };

  // ─── 自定义因子导入（JSON / CSV 上传）─────────────────────────────────────

  const normalizeImportMarkets = (v: unknown): string[] => {
    const raw: string[] = Array.isArray(v)
      ? v.map(x => String(x).toUpperCase().trim())
      : String(v ?? '').split(/[,;|，；、\s]+/).map(x => x.toUpperCase().trim());
    const cleaned = raw.filter(x => ALL_MARKETS.includes(x));
    return cleaned.length === ALL_MARKETS.length ? [] : cleaned;
  };

  const splitCsvLine = (line: string): string[] => {
    const out: string[] = [];
    let cur = '';
    let quoted = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (quoted) {
        if (ch === '"') {
          if (line[i + 1] === '"') { cur += '"'; i++; }
          else { quoted = false; }
        } else { cur += ch; }
      } else if (ch === '"') { quoted = true; }
      else if (ch === ',') { out.push(cur); cur = ''; }
      else { cur += ch; }
    }
    out.push(cur);
    return out.map(s => s.trim());
  };

  const parseImportText = (text: string, filename: string) => {
    const items: { key: string; feature_name: string; explanation: string; formula: string; source_table_fields: string; markets: string[] }[] = [];
    if (/\.json$/i.test(filename)) {
      const raw = JSON.parse(text);
      const arr = Array.isArray(raw) ? raw : raw?.features;
      if (!Array.isArray(arr)) throw new Error('JSON 需为特征数组或 {features:[...]} 结构');
      for (const r of arr) {
        if (!r || typeof r !== 'object') continue;
        items.push({
          key: String((r as any).key ?? '').trim(),
          feature_name: String((r as any).feature_name ?? (r as any).description ?? '').trim(),
          explanation: String((r as any).explanation ?? (r as any).detail ?? '').trim().slice(0, 500),
          formula: String((r as any).formula ?? '').trim(),
          source_table_fields: String((r as any).source_table_fields ?? (r as any).source ?? '').trim(),
          markets: normalizeImportMarkets((r as any).markets),
        });
      }
    } else {
      const lines = text.split(/\r?\n/).filter(l => l.trim());
      if (!lines.length) throw new Error('CSV 文件为空');
      const head = splitCsvLine(lines[0]).map(h => h.toLowerCase());
      const col = (...names: string[]) => {
        for (const n of names) { const i = head.indexOf(n); if (i >= 0) return i; }
        return -1;
      };
      const iKey = col('key');
      const iName = col('feature_name', 'name', 'description', '名称');
      const iExpl = col('explanation', 'detail', '描述');
      const iFormula = col('formula', '公式');
      const iSource = col('source', 'source_table_fields', '数据来源');
      const iMarkets = col('markets', 'market', '市场');
      if (iKey < 0 || iName < 0) throw new Error('CSV 表头至少包含 key, feature_name 两列');
      for (const line of lines.slice(1, 201)) {
        const c = splitCsvLine(line);
        items.push({
          key: (c[iKey] ?? '').trim(),
          feature_name: (c[iName] ?? '').trim(),
          explanation: (iExpl >= 0 ? (c[iExpl] ?? '') : '').trim().slice(0, 500),
          formula: (iFormula >= 0 ? (c[iFormula] ?? '') : '').trim(),
          source_table_fields: (iSource >= 0 ? (c[iSource] ?? '') : '').trim(),
          markets: normalizeImportMarkets(iMarkets >= 0 ? c[iMarkets] : []),
        });
      }
    }
    return items;
  };

  const handleImportFile = async (file: File) => {
    if (!catalog || !selectedCatId) return;
    try {
      const text = await file.text();
      const parsed = parseImportText(text, file.name);
      const cats = catalog.categories.map(cat => {
        if (cat.id !== selectedCatId) return cat;
        const keys = new Set(cat.features.map(f => f.key));
        const features = [...cat.features];
        let added = 0;
        let skipped = 0;
        let invalid = 0;
        for (const p of parsed) {
          if (!KEY_RE.test(p.key) || !p.feature_name) { invalid++; continue; }
          if (keys.has(p.key)) { skipped++; continue; }
          keys.add(p.key);
          features.push({
            feature_id: generateFeatureId(),
            key: p.key,
            feature_name: p.feature_name,
            explanation: p.explanation,
            formula: p.formula,
            source_table_fields: p.source_table_fields,
            enabled: true,
            order_no: features.length + 1,
            markets: p.markets,
          });
          added++;
        }
        message.success(`导入 ${added} 个，跳过 ${skipped} 个（已存在），舍弃 ${invalid} 个（key/名称非法）`);
        return { ...cat, features, feature_count: features.length };
      });
      setCatalog({ ...catalog, categories: cats });
      markDirty();
    } catch (e: any) {
      message.error(`导入失败：${e?.message || '文件格式不支持（仅 JSON/CSV）'}`);
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleDeleteFeature = (featureKey: string) => {    if (!catalog || !selectedCatId) return;
    const cats = catalog.categories.map(cat => {
      if (cat.id !== selectedCatId) return cat;
      const features = cat.features.filter(f => f.key !== featureKey);
      return { ...cat, features, feature_count: features.length };
    });
    setCatalog({ ...catalog, categories: cats });
    markDirty();
  };

  const handleToggleFeature = (featureKey: string, enabled: boolean) => {
    if (!catalog || !selectedCatId) return;
    const cats = catalog.categories.map(cat => {
      if (cat.id !== selectedCatId) return cat;
      const features = cat.features.map(f =>
        f.key === featureKey ? { ...f, enabled } : f
      );
      return { ...cat, features };
    });
    setCatalog({ ...catalog, categories: cats });
    markDirty();
  };

  // ─── 表格列定义 ──────────────────────────────────────────────────────────

  const featureColumns: ColumnsType<AdminModelFeatureItem> = [
    {
      title: 'Key',
      dataIndex: 'key',
      width: 180,
      render: (key: string) => <Text code className="text-xs">{key}</Text>,
    },
    {
      title: '名称',
      dataIndex: 'feature_name',
      width: 180,
      ellipsis: { showTitle: false },
      render: (name: string) => (
        <Tooltip title={name} placement="topLeft">
          <span className="text-xs font-medium">{name}</span>
        </Tooltip>
      ),
    },
    {
      title: '描述（可编辑）',
      dataIndex: 'explanation',
      width: 260,
      ellipsis: { showTitle: false },
      render: (v: string | undefined, record) => v ? (
        <Tooltip title={v} placement="topLeft">
          <span className="text-xs text-slate-600">{v}</span>
        </Tooltip>
      ) : (
        <Button type="link" size="small" className="!p-0 !h-auto text-xs" onClick={() => openEditFeature(record)}>
          + 添加描述
        </Button>
      ),
    },
    {
      title: '公式',
      dataIndex: 'formula',
      width: 180,
      ellipsis: { showTitle: false },
      render: (v: string) => v ? (
        <Tooltip title={v} placement="topLeft">
          <Text type="secondary" className="text-xs font-mono">{v}</Text>
        </Tooltip>
      ) : '—',
    },
    {
      title: '数据来源',
      dataIndex: 'source_table_fields',
      width: 180,
      ellipsis: { showTitle: false },
      render: (v: string) => v ? (
        <Tooltip title={v} placement="topLeft">
          <Text type="secondary" className="text-xs font-mono">{v}</Text>
        </Tooltip>
      ) : '—',
    },
    {
      title: '市场',
      dataIndex: 'markets',
      width: 170,
      render: (markets: string[] | undefined) => {
        const list = markets && markets.length > 0 ? markets : ALL_MARKETS;
        return (
          <Space size={2} wrap>
            {list.map(m => {
              const opt = MARKET_OPTIONS.find(o => o.value === m);
              return <Tag key={m} color={opt?.color || 'default'} className="text-[10px] m-0">{opt?.label || m}</Tag>;
            })}
          </Space>
        );
      },
    },
    {
      title: '启用',
      dataIndex: 'enabled',
      width: 70,
      align: 'center',
      render: (enabled: boolean, record) => (
        <Switch
          size="small"
          checked={enabled}
          onChange={(checked) => handleToggleFeature(record.key, checked)}
        />
      ),
    },
    {
      title: '操作',
      width: 90,
      align: 'center',
      fixed: 'right' as const,
      render: (_: unknown, record) => (
        <Space size="small">
          <Tooltip title="编辑">
            <Button type="text" size="small" icon={<EditOutlined />} onClick={() => openEditFeature(record)} />
          </Tooltip>
          <Popconfirm title="确认删除此特征？" onConfirm={() => handleDeleteFeature(record.key)}>
            <Button type="text" size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  // ─── 渲染 ──────────────────────────────────────────────────────────────────

  if (loading && !catalog) {
    return <Card loading className="m-8"><div style={{ height: 200 }} /></Card>;
  }
  if (loadError && !catalog) {
    return (
      <Card className="m-8">
        <Empty
          description={
            <div className="space-y-2">
              <div className="text-rose-600 font-bold">加载失败</div>
              <div className="text-xs text-slate-500 break-all">{loadError}</div>
              <Button type="primary" onClick={() => loadCatalog()} className="mt-2" icon={<ReloadOutlined />}>
                重试
              </Button>
            </div>
          }
        />
      </Card>
    );
  }
  if (!catalog) {
    return (
      <Card className="m-8">
        <Empty
          description={
            <div className="space-y-2">
              <div className="text-slate-600">特征字典为空</div>
              <Button onClick={() => loadCatalog()} className="mt-2" icon={<ReloadOutlined />}>
                重新加载
              </Button>
            </div>
          }
        />
      </Card>
    );
  }

  const totalFeatures = catalog.categories.reduce((sum, c) => sum + c.features.length, 0);

  return (
    <div className="flex flex-col gap-3 overflow-hidden" style={{ height: 'calc(var(--app-h) - 148px)', minHeight: 520 }}>
      {/* 顶栏：标题（位置不变）+ 右侧搜索/刷新/保存 —— 固定不动 */}
      <div className="flex items-center justify-between flex-wrap gap-3 shrink-0">
        <div className="flex items-center gap-3">
          <DatabaseOutlined className="text-xl text-blue-500" />
          <div>
            <Title level={4} className="!m-0">特征字典管理</Title>
            <Text type="secondary" className="text-xs">
              {catalog.categories.length} 个分类 · {totalFeatures} 个特征 · 来源: {catalog.source || 'file'}
              {marketFilter !== 'ALL' ? ` · 当前市场: ${marketFilter}（${marketCounts[marketFilter] ?? 0}）` : ''}
            </Text>
          </div>
        </div>
        <Space wrap>
          <Input.Search
            allowClear
            placeholder="搜索 Key / 名称 / 描述 / 公式"
            value={keyword}
            onChange={e => setKeyword(e.target.value)}
            style={{ width: 260 }}
            className="feature-search-center"
          />
          <Button icon={<ReloadOutlined />} onClick={() => loadCatalog()} loading={loading}>刷新</Button>
          <Tooltip title={marketFilter !== 'ALL' ? '当前为市场过滤视图，请先取消胶囊筛选（回到全部）再保存，否则会丢失其他市场数据' : ''}>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              onClick={handleSave}
              loading={saving}
              disabled={!dirty || marketFilter !== 'ALL'}
            >
              保存
            </Button>
          </Tooltip>
        </Space>
      </div>

      {/* 市场胶囊切换：5 个市场，点选过滤，再点取消回到全部 —— 固定不动 */}
      <div className="flex items-center gap-2 flex-wrap shrink-0">
        {MARKET_OPTIONS.map(m => {
          const active = marketFilter === m.value;
          return (
            <Button
              key={m.value}
              shape="round"
              type={active ? 'primary' : 'default'}
              onClick={() => handleMarketChange(active ? 'ALL' : m.value)}
            >
              {m.label}（{marketCounts[m.value] ?? 0}）
            </Button>
          );
        })}
        {dirty && <Tag color="warning">未保存</Tag>}
      </div>

      <Divider className="!m-0 shrink-0" />

      {/* 主体：左右分栏占满剩余高度；左侧固定，右侧为双向滚动容器 */}
      <div className="flex gap-4 flex-1 min-h-0">
        {/* 左侧分类列表：固定不动，内部独立纵向滚动 */}
        <Card
          size="small"
          title={<span className="text-sm font-semibold">分类列表</span>}
          extra={
            <Button type="text" size="small" icon={<PlusOutlined />} onClick={openAddCategory}>
              新增
            </Button>
          }
          className="shrink-0"
          style={{ width: 280, height: '100%', display: 'flex', flexDirection: 'column' }}
          styles={{ body: { padding: 0, flex: 1, minHeight: 0, overflowY: 'auto' } }}
        >
          {catalog.categories.map(cat => (
            <div
              key={cat.id}
              className={`flex items-center justify-between px-4 py-3 cursor-pointer border-b border-gray-50 transition-colors ${
                selectedCatId === cat.id ? 'bg-blue-50 border-l-2 border-l-blue-500' : 'hover:bg-gray-50'
              }`}
              onClick={() => setSelectedCatId(cat.id)}
            >
              <div className="flex items-center gap-2 min-w-0">
                <FolderOutlined className={selectedCatId === cat.id ? 'text-blue-500' : 'text-gray-400'} />
                <div className="min-w-0">
                  <div className="text-sm font-medium truncate">{cat.name}</div>
                  <div className="text-xs text-gray-400">{cat.id} · {cat.features.length} 个特征</div>
                </div>
              </div>
              <Space size="small" onClick={e => e.stopPropagation()}>
                <Button type="text" size="small" icon={<EditOutlined />} onClick={() => openEditCategory(cat)} />
                <Popconfirm title="确认删除此分类及所有特征？" onConfirm={() => handleDeleteCategory(cat.id)}>
                  <Button type="text" size="small" danger icon={<DeleteOutlined />} />
                </Popconfirm>
              </Space>
            </div>
          ))}
          {catalog.categories.length === 0 && (
            <div className="p-8 text-center text-gray-400 text-sm">暂无分类</div>
          )}
        </Card>

        {/* 右侧特征表格：卡片 body 即双向滚动容器 */}
        <Card
          size="small"
          title={
            <span className="text-sm font-semibold">
              {selectedCat ? `${selectedCat.name} — 特征列表` : '请选择分类'}
            </span>
          }
          extra={
            selectedCat && (
              <Space size="small">
                {selectedCat.id === 'custom' && (
                  <>
                    <Tooltip title="上传 JSON（数组或 {features:[...]}）或 CSV（key,feature_name,…）批量导入，最多200条/次">
                      <Button
                        type="text"
                        size="small"
                        icon={<UploadOutlined />}
                        onClick={() => fileInputRef.current?.click()}
                      >
                        导入
                      </Button>
                    </Tooltip>
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept=".json,.csv"
                      style={{ display: 'none' }}
                      onChange={e => {
                        const f = e.target.files?.[0];
                        if (f) handleImportFile(f);
                      }}
                    />
                  </>
                )}
                <Button type="text" size="small" icon={<PlusOutlined />} onClick={openAddFeature}>
                  新增特征
                </Button>
              </Space>
            )
          }
          className="flex-1 min-w-0"
          style={{ height: '100%', display: 'flex', flexDirection: 'column', minWidth: 0 }}
          styles={{ body: { flex: 1, minHeight: 0, overflow: 'auto' } }}
        >
          {selectedCat ? (
            <Table
              dataSource={visibleFeatures}
              columns={featureColumns}
              rowKey="key"
              size="small"
              pagination={false}
              scroll={{ x: 1330 }}
              locale={{ emptyText: keyword || marketFilter !== 'ALL' ? '当前筛选下无特征' : '暂无特征' }}
            />
          ) : (
            <Empty description="请从左侧选择一个分类" />
          )}
        </Card>
      </div>

      {/* 分类编辑弹窗 */}
      <Modal
        title={editingCat ? '编辑分类' : '新增分类'}
        open={catModalOpen}
        onOk={handleSaveCategory}
        onCancel={() => setCatModalOpen(false)}
        destroyOnHidden
      >
        <Form form={catForm} layout="vertical">
          <Form.Item name="id" label="分类 ID" rules={[{ required: true, message: '请输入分类 ID' }]}>
            <Input placeholder="例如: momentum" disabled={!!editingCat} />
          </Form.Item>
          <Form.Item name="name" label="分类名称" rules={[{ required: true, message: '请输入分类名称' }]}>
            <Input placeholder="例如: 动量" />
          </Form.Item>
          <Form.Item name="order" label="排序" initialValue={0}>
            <Input type="number" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 特征编辑弹窗 */}
      <Modal
        title={editingFeat ? '编辑特征' : '新增特征'}
        open={featModalOpen}
        onOk={handleSaveFeature}
        onCancel={() => setFeatModalOpen(false)}
        destroyOnHidden
      >
        <Form form={featForm} layout="vertical">
          <Form.Item name="key" label="特征 Key" rules={[{ required: true, message: '请输入特征 Key' }]}>
            <Input placeholder="例如: mom_ret_1d" disabled={!!editingFeat} />
          </Form.Item>
          <Form.Item name="feature_name" label="特征名称" rules={[{ required: true, message: '请输入特征名称' }]}>
            <Input placeholder="例如: 1日收益率动量" maxLength={30} showCount />
          </Form.Item>
          <Form.Item name="explanation" label="因子描述（用户可编辑）" extra="≤500字，留空则训练页回退显示字典解释">
            <Input.TextArea placeholder="例如: 收盘价相对1日前收盘价的涨跌幅，衡量短期动量" rows={3} maxLength={500} showCount />
          </Form.Item>
          <Form.Item name="formula" label="公式">
            <Input placeholder="例如: (C_t/C_{t-1})-1" />
          </Form.Item>
          <Form.Item name="source_table_fields" label="数据来源">
            <Input placeholder="例如: stock_daily.ClosePrice" />
          </Form.Item>
          <Form.Item name="markets" label="适用市场" initialValue={ALL_MARKETS}>
            <Checkbox.Group options={MARKET_OPTIONS.map(m => ({ label: m.label, value: m.value }))} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};
