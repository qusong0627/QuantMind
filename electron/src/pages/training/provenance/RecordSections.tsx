import React from 'react';
import { Empty, Tag } from 'antd';
import { clsx } from 'clsx';
import { Activity, ClipboardCopy, GitBranch, SlidersHorizontal } from 'lucide-react';
import {
  getObjectiveMetricDescription,
  getTargetModeDescription,
  type TrainingResult,
  type WfaDiagnosticResult,
} from '../trainingUtils';
import { copyText, CopyValue, Fact, fmtNum, Shell } from './primitives';

export const ConfigSection: React.FC<{ result: TrainingResult }> = ({ result }) => {
  const md = result.metadata;
  const periods = result.request.timePeriods;
  const targetContract = md.target_id
    ? `${md.target_id}@v${md.target_version ?? 0} · ${(md.target_hash || '').slice(0, 8)}`
    : '未引用注册表目标（legacy 平铺训练）';
  return (
    <Shell title="训练配置" hint="提交时钉扎的口径；结果页与回放按此复现" icon={<SlidersHorizontal size={14} />}>
      <Fact label="训练目标">
        <span className="font-bold text-slate-800">T+{md.target_horizon_days}</span>
        <span className="ml-1.5 text-slate-500">{getTargetModeDescription(md.target_mode)}</span>
      </Fact>
      <Fact label="目标契约" mono>{targetContract}</Fact>
      <Fact label="标签公式" mono>{md.label_formula || '—'}</Fact>
      <Fact label="时间窗口" mono>{md.training_window || '—'}</Fact>
      <Fact label="目标函数">{getObjectiveMetricDescription(md.objective, md.metric)}</Fact>
      <Fact label="基准">{md.benchmark ? <span className="font-mono">{md.benchmark}</span> : '—'}</Fact>
      <div className="mt-2 grid gap-1 rounded-xl bg-slate-50/80 p-2 sm:grid-cols-3">
        {([
          ['训练段', periods.train],
          ['验证段', periods.val],
          ['测试段', periods.test],
        ] as const).map(([label, range]) => (
          <div key={label} className="rounded-lg bg-white/70 px-2 py-1.5">
            <div className="text-[9px] font-bold text-slate-400">{label}</div>
            <div className="mt-0.5 font-mono text-[9px] leading-tight text-slate-600">
              {range[0] || '—'} → {range[1] || '—'}
            </div>
          </div>
        ))}
      </div>
    </Shell>
  );
};

export const LineageSection: React.FC<{ result: TrainingResult }> = ({ result }) => {
  const lineage = result.metadata.lineage;
  const catalogVersions = Object.entries(lineage?.factor_catalog_versions || {})
    .map(([lib, vid]) => `${lib}:${String(vid).slice(-8)}`)
    .join(' · ');
  return (
    <Shell
      title="可复现血缘"
      hint="代码 / 镜像 / 配置哈希 / 因子目录，缺一项都无法逐位复现"
      icon={<GitBranch size={14} />}
      action={
        <button
          type="button"
          onClick={() => void copyText(JSON.stringify(lineage ?? {}, null, 2), '血缘已复制')}
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[9px] font-bold text-slate-400 transition-colors hover:bg-indigo-50 hover:text-indigo-600"
        >
          <ClipboardCopy size={10} /> 复制
        </button>
      }
    >
      {lineage ? (
        <>
          <Fact label="config hash" mono><CopyValue value={lineage.config_hash} /></Fact>
          <Fact label="git commit" mono><CopyValue value={lineage.git_commit} display={lineage.git_commit?.slice(0, 10)} /></Fact>
          <Fact label="训练镜像" mono><CopyValue value={lineage.app_image} /></Fact>
          <Fact label="因子目录" mono>{catalogVersions || '—'}</Fact>
          <Fact label="schema hash" mono><CopyValue value={lineage.factor_schema_hash} display={lineage.factor_schema_hash?.slice(0, 12)} /></Fact>
          <Fact label="seed" mono>{lineage.seed ?? '—'}</Fact>
        </>
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="老训练未记录血缘" className="!my-4" />
      )}
    </Shell>
  );
};

export const WfaSection: React.FC<{ wfa?: WfaDiagnosticResult }> = ({ wfa }) => {
  if (!wfa?.enabled || !wfa.windows?.length) return null;
  const maxAbs = Math.max(0.001, ...wfa.windows.map((w) => Math.abs(Number(w.ic) || 0)));
  return (
    <Shell
      title="WFA 稳定性诊断"
      hint={`${wfa.windows.length} 个窗口 · ${wfa.strategy === 'rolling' ? '滚动窗口' : '扩张窗口'} · 模型 ${wfa.model_type}`}
      icon={<Activity size={14} />}
      action={
        <Tag className={clsx('!m-0 !rounded-full !border-0 !px-2.5 !text-[10px] !font-black', wfa.stability === 'stable' ? '!bg-emerald-50 !text-emerald-600' : '!bg-amber-50 !text-amber-600')}>
          {wfa.stability === 'stable' ? '稳定' : '不稳定'}
        </Tag>
      }
    >
      <div className="mb-2 grid grid-cols-4 divide-x divide-slate-100 overflow-hidden rounded-xl border border-slate-200/80 bg-slate-50/60">
        {[
          { label: 'IC 均值', value: fmtNum(wfa.ic_mean), tone: wfa.ic_mean >= 0.05 ? 'text-emerald-600' : wfa.ic_mean >= 0 ? 'text-amber-600' : 'text-rose-500' },
          { label: 'IC 标准差', value: fmtNum(wfa.ic_std), tone: wfa.ic_std <= 0.02 ? 'text-emerald-600' : 'text-amber-600' },
          { label: 'ICIR', value: fmtNum(wfa.overall_icir, 3), tone: Number(wfa.overall_icir) >= 0.3 ? 'text-emerald-600' : 'text-slate-700' },
          { label: '正窗占比', value: `${Math.round((Number(wfa.positive_rate) || 0) * 100)}%`, tone: 'text-slate-700' },
        ].map((s) => (
          <div key={s.label} className="px-2 py-2 text-center">
            <div className="text-[8px] font-bold text-slate-400">{s.label}</div>
            <div className={clsx('mt-0.5 font-mono text-[12px] font-black tabular-nums', s.tone)}>{s.value}</div>
          </div>
        ))}
      </div>
      <div className="max-h-[190px] space-y-1 overflow-y-auto custom-scrollbar pr-1">
        {wfa.windows.map((w) => {
          const ic = Number(w.ic) || 0;
          return (
            <div key={w.window_idx} className="flex items-center gap-2 rounded-lg bg-slate-50/70 px-2 py-1">
              <span className="w-6 shrink-0 font-mono text-[9px] font-black text-slate-500">W{w.window_idx + 1}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-[9px] text-slate-400">
                {w.val_start} → {w.val_end}
              </span>
              <span className="h-1 w-16 shrink-0 overflow-hidden rounded-full bg-slate-200/70">
                <span
                  className={clsx('block h-full rounded-full', ic >= 0 ? 'bg-emerald-500' : 'bg-rose-400')}
                  style={{ width: `${Math.min(100, (Math.abs(ic) / maxAbs) * 100)}%` }}
                />
              </span>
              <span className={clsx('w-12 shrink-0 text-right font-mono text-[10px] font-black tabular-nums', ic >= 0.05 ? 'text-emerald-600' : ic >= 0 ? 'text-amber-600' : 'text-rose-500')}>
                {ic.toFixed(4)}
              </span>
            </div>
          );
        })}
      </div>
      <p className="mt-2 text-[9px] leading-relaxed text-slate-400">
        IC 均值 ≥0.05 且正窗占比 ≥75% 视为跨期稳定；IC 标准差 &gt;0.02 说明窗口间波动偏大。
      </p>
    </Shell>
  );
};
