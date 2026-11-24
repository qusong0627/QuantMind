import React from 'react';
import { Button, Collapse } from 'antd';
import { clsx } from 'clsx';
import {
  ArrowRight,
  BarChart3,
  CheckCircle2,
  Download,
  Layers3,
  LineChart,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
} from 'lucide-react';
import type { TrainingResult } from '../trainingUtils';
import { CopyValue, fmtNum, Shell } from './primitives';

const IC_SCALE = 0.25;

export const ICLadder: React.FC<{ result: TrainingResult | null }> = ({ result }) => {
  const rows = (['train', 'val', 'test'] as const).map((split, i) => ({
    label: ['训练集', '验证集', '测试集'][i],
    ...result?.metrics?.[split],
  }));
  const toneOf = (v: number) => (v >= 0.1 ? 'bg-emerald-500' : v >= 0.05 ? 'bg-amber-400' : 'bg-rose-400');
  return (
    <Shell title="IC 阶梯" hint="柱长按 0.25 封顶；绿 ≥0.10、黄 ≥0.05、红 <0.05" icon={<BarChart3 size={14} />}>
      <div className="space-y-2.5">
        {rows.map((row) => {
          const ic = Number.isFinite(row.ic) ? row.ic : 0;
          const width = Math.max(2, Math.min(100, (Math.abs(ic) / IC_SCALE) * 100));
          return (
            <div key={row.label}>
              <div className="flex items-center justify-between gap-2">
                <span className="w-10 shrink-0 text-[10px] font-bold text-slate-500">{row.label}</span>
                <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-slate-100">
                  <div className={clsx('h-full rounded-full transition-all', toneOf(ic))} style={{ width: `${width}%` }} />
                </div>
                <span className="w-[52px] shrink-0 text-right font-mono text-[11px] font-black tabular-nums text-slate-700">
                  {fmtNum(row.ic)}
                </span>
              </div>
              <div className="mt-0.5 flex items-center justify-end gap-2 text-[9px] text-slate-400">
                <span>RankIC <span className="font-mono font-bold text-slate-500">{fmtNum(row.rank_ic)}</span></span>
                <span>ICIR <span className="font-mono font-bold text-slate-500">{fmtNum(row.rank_icir, 2)}</span></span>
              </div>
            </div>
          );
        })}
      </div>
    </Shell>
  );
};

export const VerdictSection: React.FC<{
  result: TrainingResult;
  runId: string;
  onExportConfig: () => void;
  onCompare: () => void;
}> = ({ result, runId, onExportConfig, onCompare }) => {
  const registration = result.modelRegistration;
  const ready = registration?.status === 'ready';
  return (
    <Shell
      title="训练结论"
      icon={<Sparkles size={14} />}
      action={
        <div className="flex items-center gap-1.5">
          <Button size="small" icon={<Download size={12} />} className="rounded-lg text-[11px]" onClick={onExportConfig}>
            导出配置
          </Button>
          <Button size="small" icon={<LineChart size={12} />} className="rounded-lg text-[11px]" onClick={onCompare}>
            对比 run
          </Button>
        </div>
      }
    >
      <div className="flex items-start gap-2">
        <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-emerald-50 text-emerald-600">
          <CheckCircle2 size={14} />
        </span>
        <div className="min-w-0">
          <div className="text-[12px] font-black text-slate-800">{result.summary.status}</div>
          <p className="mt-0.5 text-[10px] leading-relaxed text-slate-500">{result.summary.notes}</p>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2 rounded-xl bg-slate-50/80 px-2.5 py-2">
        <span className={clsx('inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-black ring-1', ready ? 'bg-emerald-50 text-emerald-600 ring-emerald-100' : 'bg-amber-50 text-amber-600 ring-amber-100')}>
          <ShieldCheck size={10} /> {ready ? '已入库' : registration?.status || '未入库'}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-slate-500" title={registration?.modelId || runId}>
          {(registration?.modelId || runId).slice(0, 28)}
        </span>
        <CopyValue value={registration?.modelId || runId} display="复制 model_id" />
      </div>
    </Shell>
  );
};

const FunnelStep: React.FC<{ label: string; value: number; tone: string; note?: string }> = ({
  label,
  value,
  tone,
  note,
}) => (
  <div className="flex min-w-0 flex-1 flex-col items-center rounded-xl border border-slate-200/80 bg-slate-50/60 px-2 py-2.5 text-center">
    <span className="text-[9px] font-bold text-slate-400">{label}</span>
    <span className={clsx('mt-0.5 font-mono text-lg font-black tabular-nums leading-none', tone)}>{value}</span>
    {note ? <span className="mt-1 text-[9px] leading-tight text-slate-400">{note}</span> : null}
  </div>
);

export const FeatureSection: React.FC<{ result: TrainingResult }> = ({ result }) => {
  const md = result.metadata;
  const original = md.requested_feature_count_original ?? md.requested_features_original?.length ?? md.requested_feature_count ?? 0;
  const resolved = md.resolved_feature_count ?? md.resolved_features?.length ?? md.requested_feature_count ?? 0;
  const trained = md.feature_count ?? 0;
  const missing = md.missing_features ?? [];
  const categories = Array.isArray(md.feature_categories) ? md.feature_categories : [];
  const selected = (md.factor_selection?.features ?? []).filter((f) => f.status === 'selected').map((f) => f.name);
  const factorNames = selected.length ? selected : md.resolved_features ?? [];

  return (
    <Shell
      title="数据与特征"
      hint="请求原文 → 目录解析 → 实际入模，任一环掉数都能在这里定位"
      icon={<Layers3 size={14} />}
      action={
        missing.length ? (
          <span className="inline-flex items-center gap-1 rounded-full bg-rose-50 px-2 py-0.5 text-[10px] font-black text-rose-600 ring-1 ring-rose-100">
            <TriangleAlert size={10} /> 缺失 {missing.length}
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-black text-emerald-600 ring-1 ring-emerald-100">
            <CheckCircle2 size={10} /> 全量入模
          </span>
        )
      }
    >
      <div className="flex items-stretch gap-1.5">
        <FunnelStep label="请求特征" value={original} tone="text-slate-700" note="提交载荷原文" />
        <div className="flex shrink-0 items-center text-slate-300"><ArrowRight size={14} /></div>
        <FunnelStep label="目录解析" value={resolved} tone="text-indigo-600" note="映射后可训练" />
        <div className="flex shrink-0 items-center text-slate-300"><ArrowRight size={14} /></div>
        <FunnelStep
          label="实际入模"
          value={trained}
          tone={missing.length ? 'text-amber-600' : 'text-emerald-600'}
          note={missing.length ? `${missing.length} 个物理列缺失` : '全部进入矩阵'}
        />
      </div>

      {categories.length ? (
        <div className="mt-3 flex flex-wrap items-center gap-1">
          <span className="text-[9px] font-black uppercase tracking-wider text-slate-400">分类</span>
          {categories.map((cat) => (
            <span
              key={cat}
              className="rounded-md border border-indigo-100 bg-indigo-50/70 px-1.5 py-0.5 text-[9px] font-bold text-indigo-700"
            >
              {cat}
            </span>
          ))}
        </div>
      ) : null}

      {missing.length ? (
        <div className="mt-3 rounded-xl border border-rose-100 bg-rose-50/60 px-3 py-2">
          <div className="text-[10px] font-black text-rose-600">目录有映射但物理列缺失</div>
          <div className="mt-1 break-all font-mono text-[10px] leading-relaxed text-rose-500/90">
            {missing.slice(0, 16).join('、')}
            {missing.length > 16 ? ` …(+${missing.length - 16})` : ''}
          </div>
        </div>
      ) : null}

      {factorNames.length ? (
        <Collapse
          ghost
          className="provenance-collapse -mx-2 mt-2"
          items={[
            {
              key: 'factors',
              label: (
                <span className="text-[10px] font-black text-slate-500">
                  入模因子清单 <span className="ml-1 font-mono text-slate-400">{factorNames.length}</span>
                </span>
              ),
              children: (
                <div className="flex max-h-40 flex-wrap gap-1 overflow-y-auto custom-scrollbar pr-1">
                  {factorNames.map((name) => (
                    <span
                      key={name}
                      className="rounded-md border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono text-[9px] font-semibold text-slate-600"
                    >
                      {name}
                    </span>
                  ))}
                </div>
              ),
            },
          ]}
        />
      ) : null}
    </Shell>
  );
};
