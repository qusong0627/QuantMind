import React from 'react';
import { Button } from 'antd';
import { clsx } from 'clsx';
import { Clock3, Copy, Database, Download, GitBranch, History } from 'lucide-react';
import type { ModelTrainingRunStatus } from '../../../services/modelTrainingService';
import type { TrainingRunPreviewMeta } from '../TrainingRunDetailTabs';
import type { TrainingResult } from '../trainingUtils';
import { copyText, fmtNum, fmtTime, MARKET_LABELS, MetaChip, StatTile, statusMeta } from './primitives';

const icTone = (v?: number) =>
  v === undefined || !Number.isFinite(v)
    ? 'text-slate-400'
    : v >= 0.1
      ? 'text-emerald-600'
      : v >= 0.05
        ? 'text-amber-600'
        : 'text-rose-500';

export const ProvenanceHeader: React.FC<{
  runId: string;
  meta: TrainingRunPreviewMeta;
  detail: ModelTrainingRunStatus | null;
  result: TrainingResult | null;
  onExportConfig: () => void;
}> = ({ runId, meta, detail, result, onExportConfig }) => {
  const status = statusMeta(detail?.status);
  const metaMd = result?.metadata;
  const market = metaMd?.market || meta.market || '';
  const horizon = metaMd?.target_horizon_days ?? meta.target_horizon_days;
  const valIc = result?.metrics?.val.rank_ic;
  const testIc = result?.metrics?.test.rank_ic;
  const trained = metaMd?.feature_count;
  const requested = metaMd?.requested_feature_count_original ?? metaMd?.requested_feature_count;
  const resolved = metaMd?.resolved_feature_count;
  const wfa = result?.wfa;
  const seed = metaMd?.lineage?.seed;

  return (
    <header className="relative shrink-0 overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
      <div className="h-1 bg-gradient-to-r from-indigo-500 via-violet-500 to-sky-400" />
      <div className="flex flex-wrap items-start justify-between gap-3 px-4 pb-1 pt-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center gap-1 text-[9px] font-black uppercase tracking-[0.2em] text-indigo-500">
              <History size={11} /> 训练运行档案
            </span>
            <span className={clsx('inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10px] font-black ring-1', status.chip)}>
              <span className={clsx('h-1.5 w-1.5 rounded-full', status.dot)} />
              {status.label}
            </span>
          </div>
          <div className="mt-1 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <h2 className="truncate text-base font-black leading-tight text-slate-900">
              {meta.display_name || result?.modelName || '训练任务'}
            </h2>
            <span className="font-mono text-[10px] text-slate-400">run {runId.slice(0, 18)}</span>
            <button
              type="button"
              onClick={() => void copyText(runId, 'run_id 已复制')}
              className="inline-flex items-center gap-0.5 rounded px-1 py-0.5 text-[9px] font-bold text-slate-400 transition-colors hover:bg-indigo-50 hover:text-indigo-600"
            >
              <Copy size={9} /> 复制 ID
            </button>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            {market ? <MetaChip icon={<Database size={9} />}>{MARKET_LABELS[market] || market}</MetaChip> : null}
            {horizon ? <MetaChip icon={<Clock3 size={9} />}>T+{horizon}</MetaChip> : null}
            {metaMd?.target_mode ? <MetaChip>{metaMd.target_mode === 'classification' ? '分类目标' : '回归目标'}</MetaChip> : null}
            <MetaChip icon={<GitBranch size={9} />}>{fmtTime(meta.created_at)}</MetaChip>
            {typeof metaMd?.lineage?.seed === 'number' ? <MetaChip>seed {metaMd.lineage.seed}</MetaChip> : null}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <Button size="small" icon={<Download size={12} />} className="rounded-lg text-[11px]" onClick={onExportConfig}>
            导出配置
          </Button>
        </div>
      </div>
      <div className="mx-4 my-1 grid grid-cols-2 gap-y-1 border-t border-slate-100 py-1 sm:grid-cols-4">
        <StatTile
          label="验证集 RankIC"
          value={fmtNum(valIc)}
          hint={result?.metrics ? `ICIR ${fmtNum(result.metrics.val.rank_icir, 2)}` : '结果未回传'}
          tone={icTone(valIc)}
          accent="bg-violet-400"
        />
        <StatTile
          label="测试集 RankIC"
          value={fmtNum(testIc)}
          hint={result?.metrics ? `ICIR ${fmtNum(result.metrics.test.rank_icir, 2)}` : '结果未回传'}
          tone={icTone(testIc)}
          accent="bg-indigo-400"
        />
        <StatTile
          label="入模特征"
          value={trained === undefined ? '—' : String(trained)}
          hint={requested !== undefined ? `请求 ${requested} · 解析 ${resolved ?? '—'}` : '等待结果'}
          accent="bg-sky-400"
        />
        <StatTile
          label={wfa?.enabled ? 'WFA 稳定性' : '随机种子'}
          value={wfa?.enabled ? (wfa.stability === 'stable' ? '稳定' : '不稳定') : seed === undefined ? '—' : String(seed)}
          hint={wfa?.enabled ? `IC 均值 ${fmtNum(wfa.ic_mean, 3)}` : '可复现参数'}
          tone={wfa?.enabled ? (wfa.stability === 'stable' ? 'text-emerald-600' : 'text-amber-600') : 'text-slate-800'}
          accent="bg-emerald-400"
        />
      </div>
    </header>
  );
};
