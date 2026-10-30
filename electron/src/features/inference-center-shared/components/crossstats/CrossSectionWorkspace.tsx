/**
 * 截面选股工作区（两种视图）。
 *
 * - 当日截面：既有全市场打分榜（排序 / 位置过滤 / 名单导出）；
 * - 历史统计：用**测试集**评估当前模型 Top50 的历史选股能力（本次新增）。
 *
 * 两个视图共用同一个模型上下文：切换视图不换模型，历史统计始终统计
 * 顶栏选中的那个模型。
 */

import React, { useEffect, useMemo, useState } from 'react';
import { Segmented, Tag } from 'antd';
import type {
  InferenceRankingItem,
  InferenceRankingResult,
} from '../../../../services/modelTrainingService';
import { neutralizationService } from '../../../../services/neutralizationService';
import { normalizeStockCode } from '../../../../utils/portfolioUtils';
import { CrossSectionTable } from '../CrossSectionTable';
import { SelectionStatsPanel } from './SelectionStatsPanel';

type CrossSectionView = 'today' | 'history';

export interface CrossSectionWorkspaceProps {
  ranking: InferenceRankingResult | null;
  loading: boolean;
  fallbackFrom: string | null;
  onSelect: (item: InferenceRankingItem) => void;
  activeCode?: string;
  modelId: string | null;
  modelName: string;
  /** 模型训练周期（T+H），历史统计「按模型周期换股」用 */
  horizonDays: number;
  market: string;
  marketLabel?: string;
}

const VIEW_HINTS: Record<CrossSectionView, string> = {
  today: '全市场截面打分排序、位置过滤与名单导出',
  history: '测试集历史统计 · 前复权收盘价 · 不计费用与滑点',
};

export const CrossSectionWorkspace: React.FC<CrossSectionWorkspaceProps> = ({
  ranking,
  loading,
  fallbackFrom,
  onSelect,
  activeCode,
  modelId,
  modelName,
  horizonDays,
  market,
  marketLabel,
}) => {
  const [view, setView] = useState<CrossSectionView>('today');
  const [neutralMode, setNeutralMode] = useState(false);
  const [neutralMap, setNeutralMap] = useState<Record<string, number | null>>({});

  useEffect(() => {
    if (!neutralMode || !modelId || !ranking?.inference_date) return;
    let cancelled = false;
    const loadAll = async () => {
      const map: Record<string, number | null> = {};
      let offset = 0;
      let total = 1;
      while (offset < total && offset < 10000) {
        const page = await neutralizationService.getScores(
          modelId,
          ranking.inference_date,
          1000,
          offset,
        );
        total = page.total;
        for (const row of page.rows) map[row.symbol] = row.neutral_score;
        if (!page.rows.length) break;
        offset += page.rows.length;
      }
      return map;
    };
    void loadAll()
      .then((map) => {
        if (!cancelled) setNeutralMap(map);
      })
      .catch(() => {
        if (!cancelled) setNeutralMap({});
      });
    return () => {
      cancelled = true;
    };
  }, [neutralMode, modelId, ranking?.inference_date]);

  const adjustedRanking = useMemo<InferenceRankingResult | null>(() => {
    if (!ranking) return null;
    if (!neutralMode) return ranking;
    const enriched = ranking.rankings.map((row) => ({
      ...row,
      neutral_score: neutralMap[normalizeStockCode(row.code)] ?? null,
    }));
    const withScore = enriched
      .filter((row) => row.neutral_score != null)
      .sort((a, b) => (b.neutral_score ?? 0) - (a.neutral_score ?? 0));
    const withoutScore = enriched.filter((row) => row.neutral_score == null);
    let cursor = 0;
    const ranked = [
      ...withScore.map((row) => {
        cursor += 1;
        return {
          ...row,
          neutral_rank: cursor,
          neutral_delta: (row.rank || cursor) - cursor,
        };
      }),
      ...withoutScore.map((row) => ({
        ...row,
        neutral_rank: null,
        neutral_delta: null,
      })),
    ];
    return { ...ranking, rankings: ranked };
  }, [ranking, neutralMode, neutralMap]);

  const neutralAvailable =
    neutralMode && Boolean(adjustedRanking?.rankings.some((row) => row.neutral_score != null));

  const neutralStats = useMemo(() => {
    if (!neutralMode || !adjustedRanking) return null;
    const valid = adjustedRanking.rankings.filter((row) => row.neutral_score != null);
    return {
      valid: valid.length,
      up: valid.filter((row) => (row.neutral_delta ?? 0) > 0).length,
      down: valid.filter((row) => (row.neutral_delta ?? 0) < 0).length,
    };
  }, [neutralMode, adjustedRanking]);

  return (
    <div
      className="flex-1 min-h-0 min-w-0 flex flex-col gap-2"
      data-testid="cross-section-workspace"
    >
      <div className="shrink-0 flex items-center gap-3 min-w-0">
        <Segmented<CrossSectionView>
          size="small"
          value={view}
          onChange={(value) => setView(value)}
          options={[
            { value: 'today', label: '当日截面' },
            { value: 'history', label: '历史统计' },
          ]}
          data-testid="cross-section-view"
        />
        <span className="text-[11px] text-slate-500 truncate">{VIEW_HINTS[view]}</span>
        {view === 'today' && (
          <Segmented<'raw' | 'neutral'>
            size="small"
            value={neutralMode ? 'neutral' : 'raw'}
            onChange={(value) => setNeutralMode(value === 'neutral')}
            options={[
              { value: 'raw', label: '原始分' },
              { value: 'neutral', label: '中性化' },
            ]}
            data-testid="cross-section-neutral-mode"
          />
        )}
        {neutralMode && !neutralAvailable && (
          <span className="text-[11px] text-amber-600">
            该模型未开启中性化（影子）或当日无结果
          </span>
        )}
        <div className="ml-auto flex items-center gap-1.5 min-w-0">
          <Tag
            color="blue"
            className="!m-0 max-w-[220px] truncate"
            title={modelName}
            data-testid="cross-section-model"
          >
            {modelName}
          </Tag>
          <Tag className="!m-0 text-[10px]">训练周期 T+{Math.max(1, horizonDays || 1)}</Tag>
        </div>
      </div>

      {view === 'today' && neutralMode && (
        <div className="shrink-0 flex flex-wrap items-center gap-2 rounded-xl border border-indigo-200 bg-gradient-to-r from-indigo-50 via-white to-sky-50 px-3 py-1.5">
          <span className="rounded-md bg-indigo-600 px-1.5 py-0.5 text-[9px] font-black tracking-wide text-white">
            NL
          </span>
          <span className="text-[11px] font-black text-indigo-700">中性化视图</span>
          <span className="text-[10px] text-indigo-600/80">
            已剥离 流通市值 · 60日 Beta · 20日动量 · 覆盖 {neutralStats?.valid ?? 0} 只 · 榜单按中性化分位重排
          </span>
          {neutralStats && (
            <span className="ml-auto flex items-center gap-3 text-[10px] font-bold">
              <span className="text-emerald-600">名次上升 {neutralStats.up}</span>
              <span className="text-slate-500">下降 {neutralStats.down}</span>
            </span>
          )}
        </div>
      )}

      <div className="flex-1 min-h-0 min-w-0 flex flex-col">
        {view === 'today' ? (
          <CrossSectionTable
            ranking={adjustedRanking}
            loading={loading}
            fallbackFrom={fallbackFrom}
            onSelect={onSelect}
            activeCode={activeCode}
            neutralMode={neutralMode}
          />
        ) : (
          <SelectionStatsPanel
            modelId={modelId}
            horizonDays={horizonDays}
            market={market}
            marketLabel={marketLabel}
          />
        )}
      </div>
    </div>
  );
};

export default CrossSectionWorkspace;
