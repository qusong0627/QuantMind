import React, { useEffect, useMemo, useState } from 'react';
import { Button, Empty, Input, Segmented, Spin, Table, Tag, Tooltip, Typography, message } from 'antd';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Info,
  Search,
  ShieldAlert,
} from 'lucide-react';
import { clsx } from 'clsx';
import { modelTrainingService } from '../services/modelTrainingService';

const { Text } = Typography;

type DriftFeature = {
  feature: string;
  psi: number;
  rank_disp: number;
  level: 'stable' | 'medium' | 'severe';
  benign_scale: boolean;
  rank_reliable: boolean;
  z?: number;
  envelope?: number;
  noise_floor?: number;
};

const fmtNum = (value: unknown, digits = 4): string => {
  const num = Number(value);
  return value === null || value === undefined || !Number.isFinite(num) ? '—' : num.toFixed(digits);
};

const LEVEL_META: Record<string, { label: string; color: string; bar: string; text: string }> = {
  stable: { label: '稳定', color: 'green', bar: 'bg-emerald-400', text: 'text-emerald-600' },
  medium: { label: '中等', color: 'gold', bar: 'bg-amber-400', text: 'text-amber-600' },
  severe: { label: '严重', color: 'red', bar: 'bg-rose-500', text: 'text-rose-600' },
};

const OVERALL_META: Record<string, { label: string; badge: string; text: string }> = {
  stable: { label: '稳定', badge: 'bg-emerald-50 text-emerald-600', text: 'text-emerald-600' },
  warning: { label: '预警', badge: 'bg-amber-50 text-amber-600', text: 'text-amber-600' },
  severe: { label: '严重', badge: 'bg-rose-50 text-rose-600', text: 'text-rose-600' },
};

const Stat: React.FC<{ label: string; value: React.ReactNode; hint?: string; tone?: string }> = ({
  label,
  value,
  hint,
  tone,
}) => (
  <div className="rounded-xl border border-slate-100 bg-slate-50/70 px-3 py-2.5">
    <div className="text-[9px] font-bold uppercase tracking-wider text-slate-400">{label}</div>
    <div className={clsx('mt-0.5 truncate text-[13px] font-black', tone || 'text-slate-800')}>{value}</div>
    {hint ? <div className="mt-0.5 truncate text-[9px] text-slate-400">{hint}</div> : null}
  </div>
);

export const DriftTabPanel: React.FC<{
  modelId: string;
  marketLabel?: string;
  featureLabelMap?: Record<string, string>;
}> = ({ modelId, marketLabel, featureLabelMap = {} }) => {
  const [loading, setLoading] = useState(true);
  const [drift, setDrift] = useState<any>(null);
  const [search, setSearch] = useState('');
  const [levelFilter, setLevelFilter] = useState<'all' | 'stable' | 'medium' | 'severe'>('all');
  const [benignOnly, setBenignOnly] = useState(false);
  const [explainOpen, setExplainOpen] = useState(false);

  useEffect(() => {
    if (!modelId) return;
    let alive = true;
    (async () => {
      setLoading(true);
      try {
        const data = await modelTrainingService.getModelDrift(modelId);
        if (alive) setDrift(data);
      } catch (e: any) {
        if (!alive) return;
        message.error(e?.message ?? '加载漂移失败');
        setDrift({ enabled: false, reason: e?.message });
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [modelId]);

  const features: DriftFeature[] = useMemo(
    () => (drift?.top_drift_features ?? drift?.features ?? []) as DriftFeature[],
    [drift],
  );

  const filtered = useMemo(
    () =>
      features.filter((f) => {
        const label = featureLabelMap[f.feature] || '';
        if (search && !f.feature.toLowerCase().includes(search.toLowerCase()) && !label.includes(search)) {
          return false;
        }
        if (levelFilter !== 'all' && f.level !== levelFilter) return false;
        if (benignOnly && !f.benign_scale) return false;
        return true;
      }),
    [features, search, levelFilter, benignOnly, featureLabelMap],
  );

  const counts = useMemo(() => {
    const fromDrift = drift?.drift;
    if (fromDrift && typeof fromDrift === 'object') {
      return {
        stable: Number(fromDrift.stable || 0),
        medium: Number(fromDrift.medium || 0),
        severe: Number(fromDrift.severe || 0),
      };
    }
    return {
      stable: features.filter((f) => f.level === 'stable').length,
      medium: features.filter((f) => f.level === 'medium').length,
      severe: features.filter((f) => f.level === 'severe').length,
    };
  }, [drift, features]);
  const total = counts.stable + counts.medium + counts.severe;

  const overallKey = String(drift?.overall || '').toLowerCase();
  const overallMeta = OVERALL_META[overallKey] ?? {
    label: drift?.overall || '—',
    badge: 'bg-slate-100 text-slate-500',
    text: 'text-slate-500',
  };

  const maxPsiFeature = features.reduce<DriftFeature | null>(
    (best, row) => (best === null || Number(row.psi || 0) > Number(best.psi || 0) ? row : best),
    null,
  );
  const topDispFeature = features.reduce<DriftFeature | null>(
    (best, row) =>
      best === null || Number(row.rank_disp || 0) > Number(best.rank_disp || 0) ? row : best,
    null,
  );

  const maxPsiScale = Math.max(0.2, ...features.map((row) => Number(row.psi || 0)));
  const maxDispScale = Math.max(0.3, ...features.map((row) => Number(row.rank_disp || 0)));

  const labelOf = (feature: string) => featureLabelMap[feature] || feature;

  const insight = useMemo(() => {
    if (features.length === 0) return '暂无可展示的漂移明细。';
    const parts = [
      `近 30 日相对训练基准，共检测 ${total.toLocaleString()} 个因子：${counts.severe} 个严重、${counts.medium} 个中等、${counts.stable} 个稳定。`,
    ];
    if (topDispFeature) {
      parts.push(
        `结构漂移最明显的是 ${labelOf(topDispFeature.feature)}（rank_disp ${fmtNum(topDispFeature.rank_disp)}，PSI ${fmtNum(topDispFeature.psi)}${topDispFeature.benign_scale ? '，属良性量纲膨胀' : ''}）。`,
      );
    }
    if (counts.severe > 0) {
      parts.push('存在严重漂移，建议复核这些因子的近期分布与可用性，必要时降权或重训。');
    } else if (counts.medium > 0) {
      parts.push('以中等漂移为主，暂未触发阻断；持续观察即可。');
    } else {
      parts.push('整体分布与训练期一致，可正常使用。');
    }
    return parts.join('');
  }, [features.length, total, counts, topDispFeature, labelOf]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Spin />
      </div>
    );
  }

  if (!drift?.enabled) {
    return (
      <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <ShieldAlert size={16} className="text-amber-500" />
          暂无漂移数据：{drift?.reason ?? '该模型未记录 drift（老模型或训练时未启用）'}
        </div>
      </div>
    );
  }

  return (
    <div className="h-[calc(var(--app-h)-340px)] overflow-y-auto custom-scrollbar px-1 pt-2 pb-2">
      <div className="space-y-3">
        {/* 头部 */}
        <div className="rounded-2xl border border-slate-200 bg-white px-5 py-3 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-sky-500 to-indigo-600 text-white">
                <Activity size={16} />
              </span>
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-sm font-black text-slate-800">数据漂移监测</span>
                  <span className={clsx('rounded-full px-2 py-0.5 text-[10px] font-bold', overallMeta.badge)}>
                    {overallMeta.label}
                  </span>
                </div>
                <div className="text-[11px] text-slate-400">
                  双通道 PSI · 身份级截面 rank 位移 · 共 {total.toLocaleString()} 个因子
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2">
              {marketLabel ? (
                <Tag bordered={false} color="blue" className="!m-0 !rounded-full !px-2.5 !text-[10px] !font-bold">
                  {marketLabel}
                </Tag>
              ) : null}
              <Button
                size="small"
                icon={<Info size={13} />}
                className="!rounded-lg !text-[11px]"
                onClick={() => setExplainOpen((value) => !value)}
              >
                口径说明
              </Button>
            </div>
          </div>
          {explainOpen ? (
            <div className="mt-3 grid gap-2 rounded-xl border border-slate-100 bg-slate-50/70 p-3 text-[11px] leading-relaxed text-slate-600 lg:grid-cols-2">
              <div>
                <span className="font-bold text-slate-700">level PSI</span>：原始水平值 10 分箱 PSI，基准为训练段分位数边，clip 1e-6；&lt;0.05 稳定 / 0.05–0.2 中等 / &gt;0.2 严重。
              </div>
              <div>
                <span className="font-bold text-slate-700">rank_disp</span>：每只票训练段 vs 近期（30 日）的截面 rank(pct) 均值位移 |Δ|，&lt;0.10 稳定 / 0.10–0.25 中等 / ≥0.25 严重，以此为主判级。
              </div>
              <div>
                <span className="font-bold text-slate-700">benign_scale</span>：level_psi ≥ 0.05 且 rank_disp &lt; 0.10 且可靠 → 良性量纲膨胀（牛市量能抬升但相对位置未变）。
              </div>
              <div>
                <span className="font-bold text-slate-700">overall</span>：severe ≥ 3 或 severe 占比 ≥ 30% → 严重；severe ≥ 1 或 medium ≥ 3 → 预警；否则稳定。
              </div>
            </div>
          ) : null}
        </div>

        {/* KPI */}
        <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
          <Stat
            label="总体评级"
            value={overallMeta.label}
            hint="按结构漂移（rank_disp）判级"
            tone={overallMeta.text}
          />
          <Stat
            label="漂移分布"
            value={`${counts.stable} / ${counts.medium} / ${counts.severe}`}
            hint="稳定 / 中等 / 严重"
          />
          <Stat
            label="最大水平 PSI"
            value={fmtNum(drift.max_psi)}
            hint={maxPsiFeature ? labelOf(maxPsiFeature.feature) : '10 分箱 PSI'}
          />
          <Stat
            label="近期检测窗口"
            value={`${drift.recent_start || '—'} ~ ${drift.recent_end || '—'}`}
            hint={`训练基准 ${drift.train_start || '—'} ~ ${drift.train_end || '—'}`}
          />
        </div>

        {/* 分布条 + 结论 */}
        <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex items-center justify-between text-[10px] font-bold uppercase tracking-wider text-slate-400">
            <span>漂移等级分布</span>
            <span>{total.toLocaleString()} 个因子</span>
          </div>
          <div className="mt-2 flex h-3 overflow-hidden rounded-full bg-slate-100">
            {(['stable', 'medium', 'severe'] as const).map((level) =>
              counts[level] > 0 ? (
                <Tooltip key={level} title={`${LEVEL_META[level].label}：${counts[level]}`}>
                  <div
                    className={clsx('h-full transition-all', LEVEL_META[level].bar)}
                    style={{ width: `${(counts[level] / Math.max(1, total)) * 100}%` }}
                  />
                </Tooltip>
              ) : null,
            )}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-4 text-[10px] text-slate-500">
            {(['stable', 'medium', 'severe'] as const).map((level) => (
              <span key={level} className="inline-flex items-center gap-1.5">
                <span className={clsx('h-2 w-2 rounded-full', LEVEL_META[level].bar)} />
                {LEVEL_META[level].label} {counts[level]}
                <span className="text-slate-300">
                  ({total > 0 ? ((counts[level] / total) * 100).toFixed(1) : '0.0'}%)
                </span>
              </span>
            ))}
          </div>
          <div className="mt-3 rounded-xl border border-slate-100 bg-slate-50/70 px-3 py-2 text-[11px] leading-relaxed text-slate-600">
            <span className="font-bold text-slate-700">结论：</span>
            {insight}
          </div>
        </div>

        {/* 明细表 */}
        <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="text-[13px] font-black text-slate-800">因子漂移明细</span>
              <span className="text-[10px] text-slate-400">
                Top {features.length} 漂移因子 · 默认按结构漂移排序
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Input
                size="small"
                allowClear
                placeholder="搜索因子 / 中文名"
                prefix={<Search size={12} className="text-slate-400" />}
                className="h-7 w-52 rounded-lg text-[11px]"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
              <Segmented
                size="small"
                value={levelFilter}
                onChange={(value) => setLevelFilter(value as typeof levelFilter)}
                options={[
                  { value: 'all', label: '全部' },
                  { value: 'stable', label: '稳定' },
                  { value: 'medium', label: '中等' },
                  { value: 'severe', label: '严重' },
                ]}
              />
              <label className="flex cursor-pointer items-center gap-1 text-[11px] text-slate-600">
                <input
                  type="checkbox"
                  checked={benignOnly}
                  onChange={(event) => setBenignOnly(event.target.checked)}
                />
                仅良性膨胀
              </label>
              <span className="text-[10px] text-slate-400">
                {filtered.length} / {features.length}
              </span>
            </div>
          </div>

          <Table<DriftFeature>
            className="mt-2"
            rowKey="feature"
            size="small"
            dataSource={filtered}
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的因子" /> }}
            pagination={{ pageSize: 15, size: 'small', showTotal: (value) => `共 ${value} 项` }}
            columns={[
              {
                title: '因子',
                dataIndex: 'feature',
                render: (feature: string) => (
                  <div className="min-w-0">
                    <div className="truncate text-[11px] font-bold text-slate-700">{labelOf(feature)}</div>
                    <div className="truncate font-mono text-[9px] text-slate-400">{feature}</div>
                  </div>
                ),
              },
              {
                title: 'PSI（水平）',
                dataIndex: 'psi',
                width: 170,
                sorter: (a: DriftFeature, b: DriftFeature) => Number(a.psi || 0) - Number(b.psi || 0),
                render: (value: number, row: DriftFeature) => (
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
                      <div
                        className={clsx('h-full rounded-full', LEVEL_META[row.level]?.bar || 'bg-slate-400')}
                        style={{ width: `${(Number(value || 0) / maxPsiScale) * 100}%` }}
                      />
                    </div>
                    <span className="w-14 text-right font-mono text-[10px] text-slate-600">
                      {fmtNum(value)}
                    </span>
                  </div>
                ),
              },
              {
                title: 'rank_disp（结构）',
                dataIndex: 'rank_disp',
                width: 180,
                defaultSortOrder: 'descend',
                sorter: (a: DriftFeature, b: DriftFeature) =>
                  Number(a.rank_disp || 0) - Number(b.rank_disp || 0),
                render: (value: number, row: DriftFeature) => (
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
                      <div
                        className={clsx('h-full rounded-full', LEVEL_META[row.level]?.bar || 'bg-slate-400')}
                        style={{ width: `${(Number(value || 0) / maxDispScale) * 100}%` }}
                      />
                    </div>
                    <span className="w-14 text-right font-mono text-[10px] font-bold text-slate-700">
                      {fmtNum(value)}
                    </span>
                  </div>
                ),
              },
              {
                title: '等级',
                dataIndex: 'level',
                width: 78,
                align: 'center',
                render: (level: string) => {
                  const meta = LEVEL_META[level] || LEVEL_META.stable;
                  return (
                    <Tag
                      bordered={false}
                      color={meta.color}
                      icon={
                        level === 'severe' ? (
                          <AlertTriangle size={10} />
                        ) : level === 'stable' ? (
                          <CheckCircle2 size={10} />
                        ) : undefined
                      }
                      className="!m-0 !inline-flex !items-center !gap-1 !rounded-full !px-2 !text-[10px]"
                    >
                      {meta.label}
                    </Tag>
                  );
                },
              },
              {
                title: '良性膨胀',
                dataIndex: 'benign_scale',
                width: 84,
                align: 'center',
                render: (value: boolean) =>
                  value ? (
                    <Tag bordered={false} color="blue" className="!m-0 !rounded-full !px-2 !text-[10px]">
                      良性
                    </Tag>
                  ) : (
                    <span className="text-[10px] text-slate-300">—</span>
                  ),
              },
              {
                title: (
                  <Tooltip title="交集 < 50 时 rank 不可估计，已回退按水平 PSI 判级">
                    <span className="cursor-help border-b border-dotted border-slate-300">可靠</span>
                  </Tooltip>
                ),
                dataIndex: 'rank_reliable',
                width: 76,
                align: 'center',
                render: (value: boolean) =>
                  value ? (
                    <span className="inline-flex items-center gap-1 text-[10px] text-emerald-600">
                      <CheckCircle2 size={10} /> 是
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-[10px] text-amber-600">
                      <AlertTriangle size={10} /> 回退
                    </span>
                  ),
              },
            ]}
          />
        </div>
      </div>
    </div>
  );
};

export default DriftTabPanel;
