/** Versioned QuantDB factor sources for model training. */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  Alert, Button, Card, Checkbox, Form, Input, Modal, Popover, Select, Space, Table, Tag, Tooltip, Typography, message,
} from 'antd';
import { DatabaseOutlined, ReloadOutlined, SettingOutlined } from '@ant-design/icons';
import { adminService } from '../services/adminService';
import type { QuantDBFactorStat, QuantDBFactorStatsMeta } from '../types';
import { RdMinedMaterializePanel } from './RdMinedMaterializePanel';
import { TrainingCatalogStatusBar } from './quantdb/TrainingCatalogStatusBar';
import { SourceVitalStrip } from './quantdb/SourceVitalStrip';
import {
  FACTOR_COLUMN_SETTINGS,
  buildFactorColumns,
  filterFactorColumns,
  type FactorDirectoryRow,
  type TrainingMapping,
} from './quantdb/factorDirectoryColumns';
import { attachStatsToRows, countEnabledFeatures } from './quantdb/catalogMath';
import { MISSING, fmtSigned } from './quantdb/statFormat';
import { formatPartitionDate } from './quantdb/utils';
import { StatusDot } from './ui/AdminPrimitives';

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

/** 草稿映射行（与 factorDirectoryColumns 的 TrainingMapping 同型）。 */
type Mapping = TrainingMapping;

/** 列设置偏好（列可见性）的本地存储键；值 = 被隐藏列的 key 数组。 */
const COL_SETTINGS_STORAGE_KEY = 'qm.admin.trainingDatasets.cols.v1';

/**
 * 深链 source 参数的合法形状，与后端 `_validate_source`
 * （quantdb_factor_catalog.py：正则放行，另查排除列表）对齐。
 * 这里只做形状预检，真实存在性由 /sources 返回的库清单兜底（不在清单里
 * 会走 load() 的「切到市场默认源」分支，不会把页面卡在空态）。
 */
const SOURCE_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;

export const AdminTrainingDatasets: React.FC = () => {
  // 深链预选：因子研究页「去发布」带 ?market&source 跳进来（见 FactorResearchPage）。
  // 惰性初始化只读首挂载的 searchParams；非法值一律回落默认，不阻塞加载。
  const [searchParams] = useSearchParams();
  const [market, setMarket] = useState(() => {
    const fromUrl = searchParams.get('market');
    return fromUrl && MARKET_OPTIONS.some((option) => option.value === fromUrl) ? fromUrl : 'CN';
  });
  const [source, setSource] = useState(() => {
    const fromUrl = searchParams.get('source');
    return fromUrl && SOURCE_PATTERN.test(fromUrl) ? fromUrl : 'l1_factors';
  });
  /**
   * 已应用的深链参数键（`market|source`）。首挂载的键已由上面的惰性初始化
   * 消费，ref 就以它开局——否则 follow-up effect 会把首屏参数再应用一遍，
   * 把正在进行的首次 load() 重置掉。
   */
  const appliedParamsKeyRef = useRef<string>(
    `${searchParams.get('market') || ''}|${searchParams.get('source') || ''}`,
  );
  const [sources, setSources] = useState<Record<string, any>>({});
  const [sourceLabels, setSourceLabels] = useState<Record<string, string>>({});
  const [fields, setFields] = useState<any[]>([]);
  const [stats, setStats] = useState<Record<string, QuantDBFactorStat | null>>({});
  const [statsMeta, setStatsMeta] = useState<QuantDBFactorStatsMeta | null>(null);
  const [published, setPublished] = useState<any | null>(null);
  const [draft, setDraft] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Mapping | null>(null);
  const [keyword, setKeyword] = useState('');
  const [hiddenCols, setHiddenCols] = useState<ReadonlySet<string>>(() => {
    try {
      const raw = localStorage.getItem(COL_SETTINGS_STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return new Set(
        Array.isArray(parsed) ? parsed.filter((value: unknown) => typeof value === 'string') : [],
      );
    } catch {
      // localStorage 不可用/内容损坏：默认全列可见，不因偏好读失败拖垮页面
      return new Set();
    }
  });
  const [editForm] = Form.useForm();

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
    setStats({});
    setStatsMeta(null);
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
    const base = fields
      .map((field) => {
        const mapping = mappingsByColumn.get(field.column_name);
        return {
          row_no: 0,
          source_column: field.column_name,
          factor: mapping?.key || field.column_name,
          style: mapping?.category_name || field.dictionary?.category_name || '待分类',
          explanation: mapping?.explanation || mapping?.feature_name || field.dictionary?.explanation || '尚未填写中文解释',
          is_present: Boolean(field.is_present),
          field,
          mapping,
        };
      })
      .sort((a, b) => a.factor.localeCompare(b.factor))
      .map((row, index) => ({ ...row, row_no: index + 1 }));
    // 统计挂接：物理列名直接命中，逻辑因子 ID 兜底（撞列守卫在 catalogMath 内）
    return attachStatsToRows(base, stats);
  }, [fields, mappings, stats]);
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
        setStats({});
        setStatsMeta(null);
        setDraft(null);
        setPublished(null);
        setLoading(false);
        return;
      }
      const fieldsResult = await adminService.getQuantDBFactorFields(activeSource, market);
      setFields(fieldsResult.fields || []);
      setStats(fieldsResult.stats || {});
      setStatsMeta(fieldsResult.stats_meta || null);
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

  // 深链参数在**挂载之后**的变化（同路由重导航：页面还挂着，只是 URL 换了）。
  // 键没变就放行——URL 里残留的旧参数不压过用户手动切过的库（抢库保护）。
  useEffect(() => {
    const rawMarket = searchParams.get('market');
    const rawSource = searchParams.get('source');
    const nextKey = `${rawMarket || ''}|${rawSource || ''}`;
    if (nextKey === appliedParamsKeyRef.current) return;
    appliedParamsKeyRef.current = nextKey;
    const nextMarket = rawMarket && MARKET_OPTIONS.some((option) => option.value === rawMarket) ? rawMarket : null;
    const nextSource = rawSource && SOURCE_PATTERN.test(rawSource) ? rawSource : null;
    if (nextMarket && nextMarket !== market) handleMarketChange(nextMarket);
    if (nextSource && nextSource !== source) {
      // 与 switchSource 同语义：连同旧库草稿一起清掉，防止 load() 拿旧草稿 id
      // 去新库下拉取（那是张冠李戴）。
      setSource(nextSource);
      setDraft(null);
    }
  }, [searchParams, market, source]);

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

  const switchSource = (value: string) => {
    if (value === source) return;
    setSource(value);
    setDraft(null);
  };

  const createDraft = async (versionName: string) => {
    try {
      setCreating(true);
      const created = await adminService.createQuantDBFactorDraft(versionName, source, market);
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

  const openEdit = (mapping: Mapping) => {
    setEditing(mapping);
    editForm.setFieldsValue({
      feature_key: mapping.key,
      display_name: mapping.feature_name,
      category_id: mapping.category_id,
      category_name: mapping.category_name,
    });
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

  const toggleColumn = (key: string, visible: boolean) => {
    setHiddenCols((prev) => {
      const next = new Set(prev);
      if (visible) next.delete(key); else next.add(key);
      try {
        localStorage.setItem(COL_SETTINGS_STORAGE_KEY, JSON.stringify([...next]));
      } catch {
        // localStorage 不可用（隐私模式等）：本次会话内内存生效即可
      }
      return next;
    });
  };

  const factorColumns = filterFactorColumns(
    buildFactorColumns({
      statsNDates: statsMeta?.window?.n_dates ?? null,
      onToggleEnabled: (row, checked) => {
        if (row.mapping) void saveMapping({ ...row.mapping, enabled: checked });
      },
      onToggleDefault: (row, checked) => {
        if (row.mapping) void saveMapping({ ...row.mapping, default_selected: checked });
      },
      onEdit: (row) => {
        if (row.mapping) openEdit(row.mapping);
      },
    }),
    hiddenCols,
  );

  /** 展开行：身份与释义 + 质量口径注释（表内列放不下的长文本与出处都在这）。 */
  const renderExpandedRow = (row: FactorDirectoryRow) => {
    const field = row.field || {};
    const dictionary = (field as any).dictionary || {};
    const stat = row.stat;
    const qualitySource = stat
      ? stat.source === 'report'
        ? `因子报告日频口径（评估期 ${statsMeta?.window?.start || MISSING} ~ ${statsMeta?.window?.end || MISSING}`
          + `${statsMeta?.window?.n_dates != null ? ` · ${statsMeta.window.n_dates} 交易日` : ''}`
          + `${statsMeta?.window?.horizon ? ` · ${statsMeta.window.horizon}` : ''}），快照 ${statsMeta?.report_date || MISSING}`
        : '私域研究快照口径：82 采样日、方向已统一为“越大越好”——与日频因子报告的 IC 不可比'
      : statsMeta?.available
        ? '该因子未命中因子报告快照（报告中没有这一列）'
        : statsMeta?.reason || '质量统计不可用';
    return (
      <div className="grid gap-x-10 gap-y-2 px-2 py-1 text-xs leading-5 text-slate-600 md:grid-cols-2">
        <div className="space-y-1">
          <div><span className="text-slate-400">完整释义：</span>{row.explanation}</div>
          <div>
            <span className="text-slate-400">物理列名：</span>
            <Text code className="text-[11px]">{row.source_column}</Text>
            <span className="ml-3 text-slate-400">数据类型：</span>
            <span className="admin-num">{(field as any).data_type || MISSING}</span>
          </div>
          <div>
            <span className="text-slate-400">库级登记覆盖：</span>
            <span className="admin-num">
              {formatPartitionDate((field as any).min_date)} ~ {formatPartitionDate((field as any).max_date)}
            </span>
            <span className="text-slate-400">（字段注册值，非逐日有效性）</span>
          </div>
          <div><span className="text-slate-400">字典分类：</span>{dictionary.category_name || MISSING}</div>
        </div>
        <div className="space-y-1">
          <div><span className="text-slate-400">质量口径：</span>{qualitySource}</div>
          <div>
            <span className="text-slate-400">t 值：</span>
            <span className="admin-num">{stat ? fmtSigned(stat.t_value) : MISSING}</span>
            <span className="ml-3 text-slate-400">子库：</span>
            {stat?.library || MISSING}
          </div>
          <div className="text-slate-400">
            统计缺失一律显示「—」，不代表 0；「窗口覆盖」= 有效天数 ÷ 该库报告评估期总天数。
          </div>
        </div>
      </div>
    );
  };

  const columnSettingsPanel = (
    <div className="w-52 space-y-2">
      {FACTOR_COLUMN_SETTINGS.map(({ group, columns }) => (
        <div key={group}>
          <div className="mb-1 text-[11px] font-medium text-slate-400">{group}</div>
          {columns.map(({ key, label }) => (
            <div key={key} className="leading-6">
              <Checkbox
                checked={!hiddenCols.has(key)}
                onChange={(event) => toggleColumn(key, event.target.checked)}
              >
                <span className="text-xs">{label}</span>
              </Checkbox>
            </div>
          ))}
        </div>
      ))}
    </div>
  );

  return <div className="p-6 space-y-4">
    <div className="flex items-center justify-between">
      <div><Title level={4} className="!mb-0"><DatabaseOutlined /> 模型训练数据集</Title>
        <Text type="secondary">仅读取各市场 ML 数据集原始因子；映射草稿发布后才影响新的训练任务。</Text></div>
      <Space wrap>
        <Select value={market} options={MARKET_OPTIONS} style={{ width: 100 }} onChange={handleMarketChange} />
        <Select value={source} options={sourceOptions} style={{ width: 220 }} loading={loading && !sourceOptions.length} onChange={switchSource} />
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

    {/* 发布状态条：线上口径 vs 草稿口径的主任务区（原右侧草稿栏 + 底部版本卡） */}
    <TrainingCatalogStatusBar
      published={published}
      draft={draft}
      creating={creating}
      onCreateDraft={(name) => { void createDraft(name); }}
      onClonePublished={() => { void clonePublished(); }}
      onPublish={publish}
    />

    {/* 数据与质量快照态势条（选中库） */}
    <SourceVitalStrip
      label={(sourceLabels[source] || source).replace('（默认）', '')}
      status={sources[source] || {}}
      fieldCount={fields.length}
      statsMeta={statsMeta}
    />

    {/* 库状态速览：单行 chips，点击切库；未就绪原因挂在 Tooltip 上 */}
    <div className="flex flex-wrap items-center gap-2">
      {sourceOptions.map((option) => {
        const status = sources[option.value] || {};
        const active = option.value === source;
        const hint = !status.files
          ? `尚未同步 ${option.label.replace('（默认）', '')} 数据`
          : (status.missing_required || []).length > 0
            ? '数据字段尚未满足训练条件'
            : '暂未满足直读训练条件';
        const action = !status.files
          ? '请在“数据下载”中勾选并同步，完成后点击“字段发现”。'
          : (status.missing_required || []).length > 0
            ? '请补齐行情字段后重新执行“字段发现”。'
            : '请刷新字段状态后重试。';
        const chip = (
          <button
            type="button"
            onClick={() => switchSource(option.value)}
            className={`flex items-center gap-2 rounded-md border px-3 py-1.5 text-xs transition-colors ${
              active
                ? 'border-sky-300 bg-sky-50 text-slate-800'
                : 'border-slate-200 bg-white text-slate-500 hover:border-slate-300 hover:text-slate-700'
            }`}
          >
            <StatusDot tone={status.ready ? 'ok' : status.files ? 'warn' : 'bad'} />
            <span className="font-medium">{option.label}</span>
            <span className="admin-num text-slate-400">{status.files || 0} 分区</span>
            {status.ready ? (
              <Tag color="green" className="!mr-0 !px-1 !text-[10px] !leading-4">就绪</Tag>
            ) : (
              <Tag className="!mr-0 !px-1 !text-[10px] !leading-4">未就绪</Tag>
            )}
          </button>
        );
        return status.ready ? (
          <React.Fragment key={option.value}>{chip}</React.Fragment>
        ) : (
          <Tooltip key={option.value} title={`${hint} · ${action}`}>{chip}</Tooltip>
        );
      })}
    </div>

    <Alert type="info" showIcon message="每份目录版本只对应一个来源库" description="默认 L1 因子。跨源训练不在本页拼接：请在「模型训练」页选好锚库后，用「附加因子库」加入其他已发布目录的库（各库版本仍是各自独立发布的这一份）。数据或 OHLCV 覆盖不完整时，直读训练入口会拒绝提交。" />

    <Card
      title="因子目录"
      extra={<Space wrap>
        <Input allowClear value={keyword} onChange={event => setKeyword(event.target.value)} placeholder="搜索因子、分类或中文解释" style={{ width: 220 }} />
        <Tag>{factorRows.length} 个已发现字段</Tag>
        {draft && <Tag color="orange">{pending.length} 个待分类</Tag>}
        <Popover trigger="click" placement="bottomRight" content={columnSettingsPanel}>
          <Button icon={<SettingOutlined />}>列设置</Button>
        </Popover>
      </Space>}
    >
      <div className="mb-3 text-xs text-slate-400">
        内置字典已依据 300 因子设计方案填充默认分类与中文解释；草稿中的修改优先于字典，发布后才影响新训练任务。
        质量指标与数据量来自因子报告快照（仅 A 股口径），缺失显示「—」；点击行首箭头可展开完整释义与口径注释。
      </div>
      <Table
        size="small"
        rowKey="source_column"
        dataSource={visibleFactorRows}
        columns={factorColumns}
        pagination={{ pageSize: 50, showSizeChanger: false }}
        scroll={{ x: 1720, y: 560 }}
        expandable={{ expandedRowRender: renderExpandedRow }}
      />
    </Card>

    <Modal title="编辑逻辑映射" open={!!editing} onCancel={() => setEditing(null)} onOk={async () => {
      const values = await editForm.validateFields();
      if (editing) { await saveMapping({ ...editing, ...values }); setEditing(null); }
    }}>
      <Form form={editForm} layout="vertical">
        <Form.Item name="feature_key" label="逻辑因子 ID" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="display_name" label="中文解释" rules={[{ required: true }]}><Input.TextArea rows={3} /></Form.Item>
        <Form.Item name="category_id" label="分类" rules={[{ required: true }]}><Select options={CATEGORY_OPTIONS} onChange={(value) => editForm.setFieldValue('category_name', CATEGORY_OPTIONS.find(item => item.value === value)?.label)} /></Form.Item>
        <Form.Item name="category_name" hidden rules={[{ required: true }]}><Input /></Form.Item>
      </Form>
    </Modal>
  </div>;
};
