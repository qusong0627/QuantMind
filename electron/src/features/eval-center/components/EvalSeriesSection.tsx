/**
 * 一问一图（三段式第 3 段）：`/eval/series` 长序列侧车 → 每张图回答一个问题。
 *
 * 侧车不在盘上时（`meta.available=false`，或对象从未产出序列）**如实展示后端给的
 * `meta.note`**，不排一排空图占位；某个序列单独缺省时留着图位、写清原因。
 */

import React, { useEffect, useState } from 'react';
import { CircleHelp, LineChart } from 'lucide-react';
import type { EvalSeriesData, EvalSeriesMeta } from '../types/evalCenter';
import { getObjectSeries } from '../services/evalCenterService';
import { EChartsChart } from '../../../components/common/EChartsChart';
import { buildSeriesCharts, type SeriesChart } from './evalSeriesModel';
import { NO_EVIDENCE_CARD } from './evalTones';
import { CARD, CardHeader } from '../../desk/components/cardKit';

interface EvalSeriesSectionProps {
  objectType: string;
  objectId: string;
}

interface SeriesState {
  loading: boolean;
  error: string;
  data: EvalSeriesData | null;
  meta: EvalSeriesMeta | null;
}

const EMPTY: SeriesState = { loading: false, error: '', data: null, meta: null };

function unavailableText(meta: EvalSeriesMeta | null): string {
  const note = String(meta?.note || '').trim();
  if (note) return note;
  return `该对象暂无长序列侧车（原因码 ${meta?.reason || 'missing'}），评分卡只带标量；下次评分产出序列后自动出现`;
}

export const EvalSeriesSection: React.FC<EvalSeriesSectionProps> = ({
  objectType,
  objectId,
}) => {
  const [state, setState] = useState<SeriesState>(EMPTY);

  useEffect(() => {
    let alive = true;
    if (!objectType || !objectId) {
      setState(EMPTY);
      return () => {
        alive = false;
      };
    }
    setState({ ...EMPTY, loading: true });
    void (async () => {
      try {
        const resp = await getObjectSeries(objectType, objectId);
        if (!alive) return;
        setState({
          loading: false,
          error: '',
          data: resp?.data || null,
          meta: resp?.meta || null,
        });
      } catch (err: unknown) {
        if (!alive) return;
        setState({
          loading: false,
          error: err instanceof Error ? err.message : '序列接口失败',
          data: null,
          meta: null,
        });
      }
    })();
    return () => {
      alive = false;
    };
  }, [objectType, objectId]);

  const charts: SeriesChart[] = state.data ? buildSeriesCharts(objectType, state.data) : [];
  const showCharts = Boolean(state.meta?.available) && charts.length > 0;

  return (
    <section className={CARD}>
      <CardHeader
        icon={<LineChart className="h-4 w-4" />}
        title="一问一图"
        meta={<span className="text-[10px] text-slate-400">长序列与分数同源（后端摊平，前端不重算）</span>}
      />

      {state.loading && <p className="text-xs text-slate-400">正在读长序列侧车…</p>}

      {!state.loading && state.error && (
        <p className="rounded-xl border border-amber-200 bg-amber-50/60 px-3 py-2 text-xs text-amber-800">
          长序列读取失败：{state.error}
        </p>
      )}

      {!state.loading && !state.error && !showCharts && (
        <p className={`rounded-xl px-3 py-2 text-xs text-amber-800 ${NO_EVIDENCE_CARD}`}>
          {unavailableText(state.meta)}
        </p>
      )}

      {!state.loading && !state.error && showCharts && (
        <>
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            {charts.map((chart) => (
              <figure
                key={`${chart.kind}:${chart.key}`}
                className="rounded-xl border border-slate-200/80 bg-slate-50/40 p-2.5"
              >
                <figcaption className="mb-1.5 flex items-start gap-1.5">
                  <CircleHelp className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" />
                  <span className="text-[11px] font-semibold text-slate-700">{chart.question}</span>
                </figcaption>
                {chart.option ? (
                  <>
                    <div className="h-[168px] min-w-0">
                      <EChartsChart option={chart.option} />
                    </div>
                    {/* 有数据的图也要摊开后端 notes：样本只有 5 天、截断、几个点缺测 */}
                    {chart.note && (
                      <p className="mt-1 rounded-lg bg-amber-50/70 px-2 py-1 text-[10px] leading-4 text-amber-800">
                        {chart.note}
                      </p>
                    )}
                  </>
                ) : (
                  <div className={`flex h-[168px] items-center rounded-lg px-3 text-[11px] text-amber-800 ${NO_EVIDENCE_CARD}`}>
                    无图：{chart.answer}
                  </div>
                )}
                <p className="mt-1.5 text-[10px] leading-4 text-slate-500">{chart.answer}</p>
              </figure>
            ))}
          </div>
          <footer className="mt-2.5 text-[10px] text-slate-400">
            来源：data/eval_series/{state.meta?.object_type || objectType}/
            {state.meta?.object_id || objectId}.json（版本 v{state.meta?.version ?? '—'}
            {state.meta?.generated_at ? ` · 生成 ${state.meta.generated_at}` : ''}
            ；色取 A 股口径：红=正向/做多侧，绿=负向）
          </footer>
        </>
      )}
    </section>
  );
};
