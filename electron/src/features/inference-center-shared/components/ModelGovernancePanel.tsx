/**
 * 模型治理工作区：把该市场的模型资产摊开做体检。
 *
 * 机构环境里「哪个模型能出区间 / 哪个能出归因 / 哪个周期没记录」不该靠逐个点开试错。
 * 这里按模型一行给出可执行结论，并对**元数据自相矛盾**的模型单独标红——
 * 那类模型在研判页会表现为「功能莫名不可用」，根因却藏在模型产物里。
 */

import React, { useMemo, useState } from 'react';
import { Input, Select, Table, Tag, Tooltip } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { clsx } from 'clsx';
import { AlertTriangle, PackageCheck, Search, ShieldCheck } from 'lucide-react';
import type { UserModelRecord } from '../../../services/modelTrainingService';
import {
  extractModelTypeShort,
  getMeta,
  getMetrics,
  modelDisplayName,
  resolveMetricNumber,
} from '../../../pages/modelRegistryUtils';

/** 与后端 `_SHAP_TREE_FRAMEWORKS` 保持一致：只有这三个框架有原生 SHAP 通道 */
const TREE_FRAMEWORKS = new Set(['lightgbm', 'xgboost', 'catboost']);
/** 与后端 `_TREE_ARTIFACT_FRAMEWORKS` 保持一致的产物扩展名 → 框架 */
const TREE_ARTIFACT_SUFFIX: Record<string, string> = {
  '.lgb': 'lightgbm',
  '.xgb': 'xgboost',
  '.cbm': 'catboost',
};

interface GovernanceRow {
  key: string;
  model: UserModelRecord;
  name: string;
  algo: string;
  horizon: number | null;
  targetMode: string;
  featureCount: number | null;
  dataSource: string;
  status: string;
  isDefault: boolean;
  ic: number | null;
  /** ic 是否为 Rank IC（截面模型的标准口径）；false 表示退回普通 IC 或缺失 */
  icIsRank: boolean;
  /** 归因可用性：tree=原生 SHAP / none=架构无通道 / mismatch=元数据与产物矛盾 */
  attribution: 'tree' | 'none' | 'mismatch';
  attributionHint: string;
  /**
   * 推理必需产物的缺项：`[]` = 健全，非空 = 「点了也跑不起来」，
   * `null` = 后端未返回该字段（老版本）→ 显示「未检查」而非「健全」。
   */
  assetGaps: string[] | null;
  updatedAt: string | null;
}

/** 从产物扩展名判框架（元数据 framework 字段会被同批其他算法覆写，不可全信） */
function frameworkFromArtifact(modelFile: string): string | null {
  const dot = modelFile.lastIndexOf('.');
  if (dot < 0) return null;
  return TREE_ARTIFACT_SUFFIX[modelFile.slice(dot).toLowerCase()] ?? null;
}

function buildRow(m: UserModelRecord): GovernanceRow {
  const meta = getMeta(m);
  const metrics = getMetrics(m);
  const rawHorizon = meta.target_horizon_days ?? meta.target_horizon;
  const horizon = Number.isFinite(Number(rawHorizon)) && Number(rawHorizon) > 0 ? Number(rawHorizon) : null;
  const modelType = String(meta.model_type ?? '').toLowerCase();
  const framework = String(meta.framework ?? '').toLowerCase();
  const modelFile = String(meta.model_file ?? m.model_file ?? '');
  const artifactFw = frameworkFromArtifact(modelFile);

  // 归因可用性判定 —— 与后端同一条规则：框架支持 → 可归因；
  // 框架说深度但 model_type/产物是树 → 元数据自相矛盾，后端要靠注册表补救。
  let attribution: GovernanceRow['attribution'];
  let attributionHint: string;
  if (TREE_FRAMEWORKS.has(framework)) {
    attribution = 'tree';
    attributionHint = '可提供原生 SHAP 单因子归因';
  } else if (artifactFw && artifactFw !== framework) {
    attribution = 'mismatch';
    attributionHint = `元数据 framework=${framework || '空'} 与产物 ${modelFile}（${artifactFw}）不一致，研判页归因将按注册表恢复；建议重训修复产物`;
  } else {
    attribution = 'none';
    attributionHint = `架构 ${framework || modelType || '未知'} 无树模型 SHAP 通道，研判页将明确标注不支持`;
  }

  // 本平台模型的评估指标写在 metadata.metrics 下（metrics_json 里只有 auc/rmse，
  // 直接取 `ic` 会恒空）。截面排序模型的行业口径是 **Rank IC**，缺失时才退回普通 IC。
  const rankIc = resolveMetricNumber(metrics, ['test_rank_ic', 'rank_ic', 'val_rank_ic']);
  const plainIc = resolveMetricNumber(metrics, ['test_ic', 'ic', 'val_ic']);
  const ic = rankIc ?? plainIc;
  const featureCount = Number(meta.feature_count) || null;

  return {
    key: m.model_id,
    model: m,
    name: modelDisplayName(m),
    algo: extractModelTypeShort(m) || String(meta.model_type ?? '—'),
    horizon,
    targetMode: String(meta.target_mode ?? '—'),
    featureCount,
    dataSource: String(meta.data_source ?? '—'),
    status: String(m.status ?? '—'),
    isDefault: Boolean(m.is_default),
    ic,
    icIsRank: rankIc != null,
    attribution,
    attributionHint,
    // 老后端不返回该字段时按「未检查」处理而不是「健全」——把缺字段读成健康
    // 会让一个全表皆坏的注册表看起来一切正常。
    assetGaps: Array.isArray(m.asset_gaps) ? m.asset_gaps : null,
    updatedAt: m.updated_at ?? m.activated_at ?? null,
  };
}

const ATTR_META: Record<GovernanceRow['attribution'], { label: string; cls: string }> = {
  tree: { label: '可用', cls: 'text-emerald-700 bg-emerald-50 border-emerald-200' },
  none: { label: '不支持', cls: 'text-slate-500 bg-slate-50 border-slate-200' },
  mismatch: { label: '产物异常', cls: 'text-rose-700 bg-rose-50 border-rose-200' },
};

const fmtDate = (v: string | null): string => (v ? String(v).slice(0, 10) : '—');

interface ModelGovernancePanelProps {
  models: UserModelRecord[];
  loading: boolean;
  marketLabel: string;
  /** 点行 → 跳到单票研判并预选该模型 */
  onInspect?: (model: UserModelRecord) => void;
}

export const ModelGovernancePanel: React.FC<ModelGovernancePanelProps> = ({
  models,
  loading,
  marketLabel,
  onInspect,
}) => {
  const [keyword, setKeyword] = useState('');
  const [algo, setAlgo] = useState<string | null>(null);
  const [horizon, setHorizon] = useState<number | null>(null);
  const [onlyProblems, setOnlyProblems] = useState(false);

  const rows = useMemo(() => models.map(buildRow), [models]);

  const algos = useMemo(
    () => [...new Set(rows.map((r) => r.algo).filter((a) => a && a !== '—'))].sort(),
    [rows],
  );
  const horizons = useMemo(
    () => [...new Set(rows.map((r) => r.horizon).filter((h): h is number => h != null))].sort((a, b) => a - b),
    [rows],
  );

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return rows.filter((r) => {
      if (algo && r.algo !== algo) return false;
      if (horizon != null && r.horizon !== horizon) return false;
      // 风险项 = 不可执行（资产缺项）/ 归因异常 / 周期未记录。
      // 资产缺项必须进这一档：它是唯一「不管怎么点都跑不起来」的类别。
      if (
        onlyProblems &&
        r.attribution !== 'mismatch' &&
        r.horizon != null &&
        (r.assetGaps == null || r.assetGaps.length === 0)
      ) {
        return false;
      }
      if (kw && !`${r.name} ${r.model.model_id}`.toLowerCase().includes(kw)) return false;
      return true;
    });
  }, [rows, keyword, algo, horizon, onlyProblems]);

  const stats = useMemo(() => {
    const noHorizon = rows.filter((r) => r.horizon == null).length;
    const mismatch = rows.filter((r) => r.attribution === 'mismatch').length;
    const attrOk = rows.filter((r) => r.attribution === 'tree').length;
    const noIc = rows.filter((r) => r.ic == null).length;
    const broken = rows.filter((r) => (r.assetGaps?.length ?? 0) > 0).length;
    return { total: rows.length, noHorizon, mismatch, attrOk, noIc, broken };
  }, [rows]);

  const columns: ColumnsType<GovernanceRow> = useMemo(() => [
    {
      title: '模型', dataIndex: 'name', width: 240, ellipsis: true, fixed: 'left',
      render: (v: string, r) => (
        <Tooltip title={r.model.model_id}>
          <span className="text-xs font-bold text-slate-800 truncate">{v}</span>
          {r.isDefault && (
            <Tag color="gold" className="!ml-1.5 !mr-0 text-[10px] leading-tight">默认</Tag>
          )}
        </Tooltip>
      ),
    },
    {
      title: '架构', dataIndex: 'algo', width: 96,
      render: (v: string) => <span className="text-[11px] font-bold text-slate-600">{v || '—'}</span>,
    },
    {
      title: '周期', dataIndex: 'horizon', width: 84, align: 'center',
      sorter: (a, b) => (a.horizon ?? 999) - (b.horizon ?? 999),
      render: (v: number | null) =>
        v == null ? (
          <Tooltip title="metadata 未记录 target_horizon_days，分数口径不可考">
            <span className="text-[10px] font-bold text-amber-700 bg-amber-50 border border-amber-200 rounded px-1 py-0.5">未记录</span>
          </Tooltip>
        ) : (
          <span className="font-mono text-[11px] font-bold text-blue-700">T+{v}</span>
        ),
    },
    {
      title: '目标口径', dataIndex: 'targetMode', width: 92,
      render: (v: string) => (
        <span className={clsx('text-[11px]', v === 'return' ? 'text-slate-600' : 'text-amber-700 font-bold')}>{v}</span>
      ),
    },
    {
      title: '特征数', dataIndex: 'featureCount', width: 84, align: 'right',
      sorter: (a, b) => (a.featureCount ?? 0) - (b.featureCount ?? 0),
      render: (v: number | null) => <span className="font-mono text-[11px] text-slate-600">{v ?? '—'}</span>,
    },
    {
      title: '数据源', dataIndex: 'dataSource', width: 132, ellipsis: true,
      render: (v: string) => <span className="text-[11px] font-mono text-slate-500">{v}</span>,
    },
    {
      title: '测试 Rank IC', dataIndex: 'ic', width: 108, align: 'right',
      sorter: (a, b) => (a.ic ?? -9) - (b.ic ?? -9),
      render: (v: number | null, r) => (
        <Tooltip title={v == null ? '该模型未记录 IC 评估指标' : r.icIsRank ? '测试集 Rank IC（截面排序能力，行业标准口径）' : '测试集普通 IC（该模型未记录 Rank IC，已退回）'}>
          <span className={clsx('font-mono text-[11px] font-bold cursor-help', v == null ? 'text-slate-300' : v >= 0 ? 'text-rose-600' : 'text-emerald-600')}>
            {v == null ? '—' : v.toFixed(4)}
            {v != null && !r.icIsRank && <span className="ml-0.5 text-[9px] text-slate-400">IC</span>}
          </span>
        </Tooltip>
      ),
    },
    {
      title: '归因', dataIndex: 'attribution', width: 92, align: 'center',
      filters: (Object.keys(ATTR_META) as GovernanceRow['attribution'][]).map((k) => ({ text: ATTR_META[k].label, value: k })),
      onFilter: (val, r) => r.attribution === val,
      render: (v: GovernanceRow['attribution'], r) => {
        const m = ATTR_META[v];
        return (
          <Tooltip title={r.attributionHint}>
            <span className={clsx('inline-block px-1.5 py-0.5 rounded border text-[10px] font-black cursor-help', m.cls)}>
              {m.label}
            </span>
          </Tooltip>
        );
      },
    },
    {
      title: '资产', dataIndex: 'assetGaps', width: 96, align: 'center',
      filters: [
        { text: '健全', value: 'ok' },
        { text: '缺项', value: 'gap' },
        { text: '未检查', value: 'unknown' },
      ],
      onFilter: (val, r) =>
        val === 'unknown' ? r.assetGaps == null : val === 'gap' ? (r.assetGaps?.length ?? 0) > 0 : r.assetGaps?.length === 0,
      render: (_v: string[] | null, r) => {
        if (r.assetGaps == null) {
          return (
            <Tooltip title="后端未返回资产检查结果（接口版本较旧）">
              <span className="text-[10px] text-slate-400">未检查</span>
            </Tooltip>
          );
        }
        if (r.assetGaps.length === 0) {
          return (
            <Tooltip title="目录、推理脚本、权重、pred.parquet 齐备">
              <span className="text-[10px] font-bold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded px-1 py-0.5">健全</span>
            </Tooltip>
          );
        }
        return (
          <Tooltip
            title={`缺 ${r.assetGaps.join('、')}。该模型点执行必然失败；若目录已迁走请从注册表移除或重训。`}
          >
            <span className="text-[10px] font-bold text-rose-700 bg-rose-50 border border-rose-200 rounded px-1 py-0.5">
              缺 {r.assetGaps.length} 项
            </span>
          </Tooltip>
        );
      },
    },
    {
      title: '状态', dataIndex: 'status', width: 84,
      render: (v: string) => (
        <span className={clsx('text-[11px] font-bold', v === 'ready' || v === 'active' ? 'text-emerald-700' : 'text-amber-700')}>{v}</span>
      ),
    },
    {
      title: '更新', dataIndex: 'updatedAt', width: 96,
      sorter: (a, b) => String(a.updatedAt ?? '').localeCompare(String(b.updatedAt ?? '')),
      render: (v: string | null) => <span className="font-mono text-[11px] text-slate-500">{fmtDate(v)}</span>,
    },
  ], []);

  return (
    <div className="flex-1 min-w-0 min-h-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden">
      {/* ── 体检摘要 + 过滤 ───────────────────────────────── */}
      <div
        data-testid="governance-summary"
        className="shrink-0 px-3 py-2 border-b border-slate-200 bg-slate-50/60 flex items-center gap-2 flex-wrap"
      >
        <span className="flex items-center gap-1.5 text-[11px] font-bold text-slate-700 whitespace-nowrap">
          <PackageCheck size={13} className="text-blue-600" />
          {marketLabel}模型资产
          <span className="font-mono font-black text-slate-900">{stats.total}</span>
        </span>
        <span className="w-px h-4 bg-slate-200" />
        <span className="text-[10px] text-slate-500 whitespace-nowrap">
          可归因 <strong className="font-mono text-emerald-700">{stats.attrOk}</strong>
        </span>
        <span className="text-[10px] text-slate-500 whitespace-nowrap">
          无 IC 记录 <strong className={clsx('font-mono', stats.noIc > 0 ? 'text-amber-700' : 'text-slate-400')}>{stats.noIc}</strong>
        </span>
        <span className="text-[10px] text-slate-500 whitespace-nowrap">
          周期未记录 <strong className={clsx('font-mono', stats.noHorizon > 0 ? 'text-amber-700' : 'text-slate-400')}>{stats.noHorizon}</strong>
        </span>
        <span className="text-[10px] text-slate-500 whitespace-nowrap">
          产物异常 <strong className={clsx('font-mono', stats.mismatch > 0 ? 'text-rose-700' : 'text-slate-400')}>{stats.mismatch}</strong>
        </span>
        <span className="text-[10px] text-slate-500 whitespace-nowrap">
          资产缺失 <strong className={clsx('font-mono', stats.broken > 0 ? 'text-rose-700' : 'text-slate-400')}>{stats.broken}</strong>
        </span>
        {stats.broken > 0 && (
          <span className="flex items-center gap-1 text-[10px] font-bold text-rose-700 bg-rose-50 border border-rose-200 rounded px-1.5 py-0.5">
            <AlertTriangle size={10} /> {stats.broken} 个模型点了也跑不起来
          </span>
        )}
        {stats.mismatch > 0 && (
          <span className="flex items-center gap-1 text-[10px] font-bold text-rose-700 bg-rose-50 border border-rose-200 rounded px-1.5 py-0.5">
            <AlertTriangle size={10} /> 需修复模型产物
          </span>
        )}

        <div className="ml-auto flex items-center gap-2 shrink-0">
          <div className="flex items-center bg-white border border-slate-200 hover:border-blue-400 focus-within:border-blue-500 focus-within:ring-2 focus-within:ring-blue-100 rounded-md pl-2.5 pr-2 h-8 w-[170px] transition-all">
            <Search size={13} className="text-slate-400 shrink-0 mr-1.5" />
            <Input
              variant="borderless"
              placeholder="模型名 / ID"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              className="p-0 text-xs"
              style={{ flex: 1, minWidth: 50, padding: 0 }}
            />
          </div>
          <Select
            allowClear size="small" placeholder="架构" value={algo}
            onChange={(v) => setAlgo(v ?? null)}
            className="!min-w-[104px]"
            options={algos.map((a) => ({ value: a, label: a }))}
          />
          <Select
            allowClear size="small" placeholder="周期" value={horizon}
            onChange={(v) => setHorizon(v ?? null)}
            className="!min-w-[90px]"
            options={horizons.map((h) => ({ value: h, label: `T+${h}` }))}
          />
          <button
            type="button"
            onClick={() => setOnlyProblems(!onlyProblems)}
            title="只看有风险的模型（资产缺项 / 产物异常 / 周期未记录）"
            className={clsx(
              'flex items-center gap-1 text-[11px] font-bold px-2 h-8 rounded-md border transition-colors whitespace-nowrap',
              onlyProblems
                ? 'bg-rose-50 border-rose-300 text-rose-700'
                : 'bg-white border-slate-200 text-slate-500 hover:border-rose-200 hover:text-rose-600',
            )}
          >
            <ShieldCheck size={12} /> 仅看风险项
          </button>
        </div>
      </div>

      <div className="flex-1 min-h-0" data-testid="governance-table">
        <Table<GovernanceRow>
          rowKey="key"
          size="small"
          loading={loading}
          dataSource={filtered}
          columns={columns}
          scroll={{ x: 1180, y: 'calc(100vh - 300px)' }}
          pagination={{ pageSize: 50, size: 'small', showSizeChanger: true, pageSizeOptions: ['50', '100', '200'] }}
          onRow={(r) => ({
            onClick: () => onInspect?.(r.model),
            className: onInspect ? 'cursor-pointer' : undefined,
          })}
          locale={{ emptyText: loading ? ' ' : '该市场没有已注册模型' }}
        />
      </div>
    </div>
  );
};
