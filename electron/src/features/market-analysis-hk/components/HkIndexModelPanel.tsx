/**
 * 港股宽基指数 · 模型排序分（只描述位置，不输出买卖字眼）。
 *
 * 数据来自 `/api/v1/market-analysis-hk/indices/model-scores`：
 * 取当前港股模型最新 pred 截面，按 HSI/HSCEI/HSTECH/HSCCI 成分聚合。
 * HSCCI 无成分快照时如实显示「暂无成分快照」，不拿别的指数代填。
 */
import React, { useEffect, useState } from 'react';
import { BrainCircuit, Info, Sparkles } from 'lucide-react';
import { getIndexModelScores } from '../services/api';
import type {
  HkIndexModelItem,
  HkIndexModelResponse,
  HkIndexModelConstituent,
} from '../types';

interface Props {
  /** 父页面刷新计数：变化时重新拉取 */
  refreshKey?: number;
  onPick?: (symbol: string) => void;
}

const POSITION_STYLE: Record<string, string> = {
  靠前: 'border-indigo-200 bg-indigo-50 text-indigo-700',
  居中: 'border-slate-200 bg-slate-50 text-slate-600',
  靠后: 'border-sky-200 bg-sky-50 text-sky-700',
};

function fmtScore(v: number | null): string {
  return v == null || !Number.isFinite(v) ? '—' : v.toFixed(3);
}

function fmtPct(v: number | null): string {
  return v == null || !Number.isFinite(v) ? '—' : `${(v * 100).toFixed(1)}%`;
}

function ConstituentChips({
  rows,
  tone,
  onPick,
}: {
  rows: HkIndexModelConstituent[];
  tone: 'top' | 'bottom';
  onPick?: (symbol: string) => void;
}) {
  if (!rows.length) return <span className="text-[10px] text-slate-300">—</span>;
  return (
    <div className="flex flex-wrap gap-1">
      {rows.map((x) => (
        <button
          key={`${tone}-${x.symbol}`}
          type="button"
          onClick={() => onPick?.(x.symbol)}
          title={`${x.name}（${x.symbol}）· 排序分 ${fmtScore(x.score)}${x.weight != null ? ` · 权重 ${x.weight.toFixed(2)}%` : ''}`}
          className={`inline-flex max-w-full items-center gap-1 rounded-full border px-1.5 py-[1px] text-[9px] font-bold transition-colors ${
            tone === 'top'
              ? 'border-indigo-100 bg-indigo-50/70 text-indigo-600 hover:border-indigo-300'
              : 'border-sky-100 bg-sky-50/70 text-sky-600 hover:border-sky-300'
          }`}
        >
          <span className="max-w-[7.5rem] truncate">{x.name}</span>
          <span className="font-mono opacity-70">{fmtScore(x.score)}</span>
        </button>
      ))}
    </div>
  );
}

function IndexModelCard({
  item,
  onPick,
}: {
  item: HkIndexModelItem;
  onPick?: (symbol: string) => void;
}) {
  const positionCls = item.position ? POSITION_STYLE[item.position] : POSITION_STYLE['居中'];
  return (
    <div
      className={`rounded-2xl border px-4 py-3 shadow-sm transition-shadow hover:shadow-md ${
        item.available
          ? 'border-slate-200/80 bg-white/95'
          : 'border-dashed border-slate-200 bg-slate-50/60'
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-xs font-extrabold text-slate-800 whitespace-nowrap">{item.name}</span>
          <span className="text-[9px] font-mono text-slate-400">{item.code}</span>
        </div>
        {item.available ? (
          <span
            className={`shrink-0 rounded-full border px-2 py-[1px] text-[9px] font-extrabold ${positionCls}`}
            title="按成分股平均排序分：≥0.52 靠前 · ≤0.48 靠后 · 其余居中"
          >
            {item.position ?? '—'}
          </span>
        ) : (
          <span className="shrink-0 rounded-full border border-slate-200 bg-white px-2 py-[1px] text-[9px] font-bold text-slate-400">
            缺数据
          </span>
        )}
      </div>

      {item.available ? (
        <>
          <div className="mt-2 flex items-end justify-between gap-2">
            <div className="flex items-baseline gap-1.5">
              <span className="text-xl font-extrabold font-mono tracking-tight text-indigo-700">
                {fmtScore(item.mean_score)}
              </span>
              <span className="text-[9px] font-bold text-slate-400">成分均分</span>
            </div>
            <span className="font-mono text-[9px] text-slate-400">
              {item.n_scored}/{item.n_members} 只有分
            </span>
          </div>

          <div className="mt-2 grid grid-cols-3 gap-1.5">
            <div className="rounded-lg bg-slate-50 px-2 py-1">
              <div className="text-[9px] text-slate-400">高于中位</div>
              <div className="font-mono text-[11px] font-extrabold text-slate-700">{fmtPct(item.breadth)}</div>
            </div>
            <div className="rounded-lg bg-slate-50 px-2 py-1">
              <div className="text-[9px] text-slate-400">前 20% 占比</div>
              <div className="font-mono text-[11px] font-extrabold text-slate-700">
                {fmtPct(item.top_quintile_share)}
              </div>
            </div>
            <div className="rounded-lg bg-slate-50 px-2 py-1">
              <div className="text-[9px] text-slate-400">覆盖</div>
              <div className="font-mono text-[11px] font-extrabold text-slate-700">{fmtPct(item.coverage)}</div>
            </div>
          </div>

          <div className="mt-2 space-y-1">
            <div className="flex items-center gap-1 text-[9px] font-bold text-indigo-500">
              <Sparkles className="h-2.5 w-2.5" /> 排序靠前
            </div>
            <ConstituentChips rows={item.top_constituents} tone="top" onPick={onPick} />
            <div className="flex items-center gap-1 pt-0.5 text-[9px] font-bold text-sky-500">
              <Info className="h-2.5 w-2.5" /> 排序靠后
            </div>
            <ConstituentChips rows={item.bottom_constituents} tone="bottom" onPick={onPick} />
          </div>
        </>
      ) : (
        <div className="mt-3 rounded-lg border border-amber-100 bg-amber-50/70 px-2.5 py-2 text-[10px] leading-relaxed text-amber-700">
          {item.reason ?? '暂无数据'}
        </div>
      )}
    </div>
  );
}

export const HkIndexModelPanel: React.FC<Props> = ({ refreshKey, onPick }) => {
  const [data, setData] = useState<HkIndexModelResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError('');
    getIndexModelScores()
      .then((res) => {
        if (alive) setData(res);
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : '模型排序分加载失败');
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [refreshKey]);

  if (loading) {
    return (
      <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2 xl:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <div
            key={i}
            className="h-36 animate-pulse rounded-2xl border border-slate-200/70 bg-white/70"
          />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-2xl border border-amber-200 bg-amber-50/70 px-4 py-2.5 text-[11px] text-amber-700">
        宽基指数模型排序分加载失败：{error}
      </div>
    );
  }

  const model = data?.model;
  return (
    <div className="rounded-2xl border border-indigo-100/80 bg-gradient-to-r from-indigo-50/60 via-white/70 to-sky-50/50 px-4 py-3 shadow-xs">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex items-center gap-1.5 rounded-full border border-indigo-200 bg-white/80 px-2.5 py-1 text-[10px] font-extrabold text-indigo-700">
            <BrainCircuit className="h-3.5 w-3.5" />
            宽基指数 · 模型排序
          </span>
          {model ? (
            <span className="truncate text-[11px] font-bold text-slate-700" title={model.model_id}>
              {model.display_name || model.model_id}
            </span>
          ) : null}
          {model?.score_date ? (
            <span className="hidden rounded-full bg-white/80 px-2 py-0.5 font-mono text-[9px] text-slate-500 sm:inline">
              分数日 {model.score_date}
            </span>
          ) : null}
          {data?.membership_date ? (
            <span className="hidden rounded-full bg-white/80 px-2 py-0.5 font-mono text-[9px] text-slate-500 md:inline">
              成分日 {data.membership_date}
            </span>
          ) : null}
        </div>
        <span
          className="text-[9px] leading-tight text-slate-400"
          title={data?.wording ?? '研究排序分，不是涨跌预测或交易指令'}
        >
          研究排序分（0–1 截面分位）· 非涨跌预测 · 非交易指令
        </span>
      </div>

      {!data?.available ? (
        <div className="rounded-xl border border-amber-200 bg-amber-50/80 px-3 py-2 text-[11px] text-amber-700">
          {data?.reason ?? '暂无可用港股模型'}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2 xl:grid-cols-4">
          {data.indices.map((item) => (
            <IndexModelCard key={item.code} item={item} onPick={onPick} />
          ))}
        </div>
      )}

      {model ? (
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[9px] text-slate-400">
          <span className="font-mono">{model.model_id}</span>
          {model.horizon_days != null ? <span>预测周期 T+{model.horizon_days}</span> : null}
          {model.feature_count != null ? <span>{model.feature_count} 维特征</span> : null}
          {model.metrics?.ic != null ? (
            <span>
              RankIC {model.metrics.ic.toFixed(3)}
              {model.metrics.ic_t != null ? ` · t ${model.metrics.ic_t.toFixed(2)}` : ''}
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
};
