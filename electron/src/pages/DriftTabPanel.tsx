import React, { useEffect, useMemo, useState } from 'react';
import { Card, Table, Tag, Typography, Spin, Input, Select, Tooltip, Collapse, message } from 'antd';
import { Info, ShieldAlert, CheckCircle2, AlertTriangle } from 'lucide-react';
import { modelTrainingService } from '../services/modelTrainingService';

const { Text } = Typography;

type DriftFeature = {
  feature: string;
  psi: number;
  rank_disp: number;
  level: 'stable' | 'medium' | 'severe';
  benign_scale: boolean;
  rank_reliable: boolean;
};

export const DriftTabPanel: React.FC<{ modelId: string; marketLabel?: string }> = ({ modelId, marketLabel }) => {
  const [loading, setLoading] = useState(true);
  const [drift, setDrift] = useState<any>(null);
  const [search, setSearch] = useState('');
  const [levelFilter, setLevelFilter] = useState<'all' | 'stable' | 'medium' | 'severe'>('all');
  const [benignOnly, setBenignOnly] = useState(false);

  useEffect(() => {
    if (!modelId) return;
    (async () => {
      setLoading(true);
      try {
        const d = await modelTrainingService.getModelDrift(modelId);
        setDrift(d);
      } catch (e: any) {
        message.error(e?.message ?? '加载漂移失败');
        setDrift({ enabled: false, reason: e?.message });
      } finally {
        setLoading(false);
      }
    })();
  }, [modelId]);

  const features: DriftFeature[] = drift?.top_drift_features ?? drift?.features ?? [];
  const filtered = useMemo(() => {
    return features.filter((f) => {
      if (search && !f.feature.toLowerCase().includes(search.toLowerCase())) return false;
      if (levelFilter !== 'all' && f.level !== levelFilter) return false;
      if (benignOnly && !f.benign_scale) return false;
      return true;
    });
  }, [features, search, levelFilter, benignOnly]);

  const overall = drift?.overall ?? '—';
  // 评级文字颜色：用于无容器纯文本居中展示「总体评级」
  const overallTextColor =
    overall === 'severe' ? 'text-red-600' : overall === 'warning' ? 'text-amber-500' : overall === 'stable' ? 'text-emerald-600' : 'text-slate-500';

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Spin />
      </div>
    );
  }
  if (!drift?.enabled) {
    return (
      <Card className="rounded-2xl">
        <div className="text-sm text-slate-500 flex items-center gap-2">
          <ShieldAlert size={16} className="text-amber-500" />
          暂无漂移数据：{drift?.reason ?? '该模型未记录 drift（老模型或训练时未启用）'}
        </div>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {/* 说明 独立卡 */}
      <Card className="rounded-2xl border-slate-200" size="small">
        <div className="mb-2 flex items-center gap-2">
          <span className="text-xs font-black text-slate-700">数据漂移</span>
          {marketLabel && <Tag color="blue" className="m-0 text-[10px] font-bold">{marketLabel}</Tag>}
          <span className="text-[10px] text-slate-400">随所选模型所属市场</span>
        </div>
        <Collapse
          ghost
          items={[
            {
              key: 'explain',
              label: (
                <span className="text-xs font-black text-slate-700 flex items-center gap-1.5">
                  <Info size={12} className="text-blue-500" />
                  口径说明（双通道 PSI · 身份级 rank 位移）
                </span>
              ),
              children: (
                <div className="text-xs text-slate-600 leading-relaxed space-y-2">
                  <div>
                    <span className="font-bold text-slate-700">level PSI</span>：原始水平值 10 分箱 PSI，基准为训练段分位数边，`clip 1e-6`；`&lt;0.05 稳定 / 0.05-0.2 中等 / &gt;0.2 严重`。
                  </div>
                  <div>
                    <span className="font-bold text-slate-700">rank_disp</span>：每只票训练段 vs 近期（30日）的截面 `rank(pct)` 均值位移 `|Δ|` 平均 `0~1`，`&lt;0.10 稳定 / 0.10-0.25 中等 / ≥0.25 严重`，以此为主判级。
                  </div>
                  <div>
                    <span className="font-bold text-slate-700">benign_scale</span>：`level_psi≥0.05 且 rank_disp&lt;0.10 且可靠 → 良性量纲膨胀`（牛市量能抬升但相对位置未变）。
                  </div>
                  <div>
                    <span className="font-bold text-slate-700">总体评级（overall）</span>：`severe≥3 或 ratio≥0.3 或 severe+medium≥max(5,0.3N) → 严重`；`severe≥1 或 medium≥3 → 预警`。
                  </div>
                </div>
              ),
            },
          ]}
        />
      </Card>

      {/* 总览：四卡统一为「标题 / 主内容 / 注释」三行结构 */}
      <div className="grid grid-cols-4 gap-3">
        <Card size="small" className="rounded-2xl text-center">
          <div className="text-[11px] text-slate-400 font-bold">总体评级</div>
          <div className={`mt-2 text-2xl font-black leading-none ${overallTextColor}`}>
            {overall === 'severe' ? '严重' : overall === 'warning' ? '预警' : overall === 'stable' ? '稳定' : overall}
          </div>
          <div className="mt-2 text-[10px] text-slate-400">max rank_disp {Number(drift.max_psi ?? 0).toFixed(4)}</div>
        </Card>
        <Card size="small" className="rounded-2xl text-center">
          <div className="text-[11px] text-slate-400 font-bold">稳定 / 中 / 重</div>
          <div className="mt-2 flex min-h-[32px] items-center justify-center">
            <span className="text-sm font-mono font-bold text-slate-800">
              {drift.drift?.stable ?? 0} / {drift.drift?.medium ?? 0} / {drift.drift?.severe ?? 0}
            </span>
          </div>
          <div className="mt-1 text-[10px] text-slate-400">按结构漂移（rank_disp）判级统计</div>
        </Card>
        <Card size="small" className="rounded-2xl text-center">
          <div className="text-[11px] text-slate-400 font-bold">因子总数</div>
          <div className="mt-2 flex min-h-[32px] items-center justify-center">
            <span className="text-sm font-mono font-bold text-slate-800">{features.length}</span>
          </div>
          <div className="mt-1 text-[10px] text-slate-400">参与漂移检测的特征数</div>
        </Card>
        <Card size="small" className="rounded-2xl text-center">
          <div className="text-[11px] text-slate-400 font-bold">检测窗口</div>
          <div className="mt-2 flex min-h-[32px] items-center justify-center">
            <span className="text-[11px] font-mono text-slate-600 leading-snug">
              训练 {drift.train_start}~{drift.train_end}
              <br />
              近期 {drift.recent_start}~{drift.recent_end}
            </span>
          </div>
        </Card>
      </div>

      <Card size="small" className="rounded-2xl">
        <div className="flex flex-wrap gap-2 items-center">
          <Input placeholder="搜索因子" value={search} onChange={(e) => setSearch(e.target.value)} className="w-64 rounded-xl" allowClear />
          <Select
            value={levelFilter}
            onChange={(v) => setLevelFilter(v)}
            className="w-32"
            options={[
              { value: 'all', label: '全部等级' },
              { value: 'stable', label: 'stable' },
              { value: 'medium', label: 'medium' },
              { value: 'severe', label: 'severe' },
            ]}
          />
          <label className="flex items-center gap-1 text-xs text-slate-600 cursor-pointer">
            <input type="checkbox" checked={benignOnly} onChange={(e) => setBenignOnly(e.target.checked)} />
            仅良性膨胀
          </label>
          <span className="text-xs text-slate-400">共 {filtered.length} / {features.length}</span>
        </div>
      </Card>

      <Card className="rounded-2xl" size="small">
        <Table
          rowKey="feature"
          dataSource={filtered}
          pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 项` }}
          columns={[
            { title: '因子', dataIndex: 'feature', key: 'feature', align: 'center', render: (v: string) => <Text code className="text-xs whitespace-nowrap">{v}</Text> },
            {
              title: 'PSI(水平)',
              dataIndex: 'psi',
              key: 'psi',
              align: 'center',
              sorter: (a: DriftFeature, b: DriftFeature) => a.psi - b.psi,
              render: (v: number) => <span className="font-mono text-xs whitespace-nowrap">{Number(v).toFixed(4)}</span>,
            },
            {
              title: 'rank_disp(结构)',
              dataIndex: 'rank_disp',
              key: 'rank_disp',
              align: 'center',
              sorter: (a: DriftFeature, b: DriftFeature) => a.rank_disp - b.rank_disp,
              defaultSortOrder: 'descend',
              render: (v: number) => <span className="font-mono text-xs font-bold whitespace-nowrap">{Number(v).toFixed(4)}</span>,
            },
            {
              title: '等级',
              dataIndex: 'level',
              key: 'level',
              align: 'center',
              render: (v: string) =>
                v === 'severe' ? (
                  <Tag color="red" icon={<AlertTriangle size={10} />} className="whitespace-nowrap inline-flex items-center gap-1">严重</Tag>
                ) : v === 'medium' ? (
                  <Tag color="gold" className="whitespace-nowrap">中等</Tag>
                ) : (
                  <Tag color="green" icon={<CheckCircle2 size={10} />} className="whitespace-nowrap inline-flex items-center gap-1">稳定</Tag>
                ),
            },
            {
              title: '良性膨胀',
              dataIndex: 'benign_scale',
              key: 'benign_scale',
              align: 'center',
              render: (v: boolean) => (v ? <Tag color="blue" className="whitespace-nowrap">良性</Tag> : <span className="text-slate-300 whitespace-nowrap">—</span>),
            },
            {
              title: '可靠',
              dataIndex: 'rank_reliable',
              key: 'rank_reliable',
              align: 'center',
              render: (v: boolean) => (v ? <span className="text-emerald-600 text-xs whitespace-nowrap inline-flex items-center gap-1"><CheckCircle2 size={10} />是</span> : <Tooltip title="交集<50，rank不可估计，已回退按水平判级"><span className="text-amber-600 text-xs whitespace-nowrap inline-flex items-center gap-1"><AlertTriangle size={10} />否</span></Tooltip>),
            },
          ]}
        />
      </Card>
    </div>
  );
};

export default DriftTabPanel;
