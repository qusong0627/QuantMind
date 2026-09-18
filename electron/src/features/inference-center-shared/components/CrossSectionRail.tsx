/**
 * 左栏：市场截面推理工作台。
 *
 * 自上而下 = 推理的时间顺序：生效状态 → 前置预检 → 手动执行 → 产出（排名 / 历史）。
 * 排名榜每一行可点，把标的送进右栏个股预测（主从联动的触发点）。
 *
 * 版面取向：控制项压成细条、产出区独占剩余高度。
 * 预检与提示默认折叠（都是一眼状态 / 一次读过的文字），把纵向空间全让给排名榜与历史，
 * 否则下方两块数据区会被压到只剩几行看不见 —— 这是改版前的主要问题。
 */

import React, { useMemo, useState } from 'react';
import { Button, DatePicker, Switch, Tag, Tooltip, Typography, Spin, Empty } from 'antd';
import { clsx } from 'clsx';
import dayjs from 'dayjs';
import {
  Shield, Play, Calendar, Info, Star, RefreshCw, ChevronDown,
  CheckCircle2, AlertCircle, LayoutGrid, History,
} from 'lucide-react';
import { RankingList } from './RankingList';
import { InferenceHistoryPanel } from '../../../components/inference/InferenceHistoryPanel';
import { modelDisplayName, modelIdToDisplayName } from '../../../pages/modelRegistryUtils';
import type { InferenceRankingItem, UserModelRecord } from '../../../services/modelTrainingService';
import type { CrossSectionInference } from '../hooks/useCrossSectionInference';

const { Text } = Typography;

/** 预检明细里不单独占格的项：数据日/回退信息已在预检条头部与执行区体现 */
const PRECHECK_HIDDEN_KEYS = new Set(['calendar_trade_date', 'data_fallback']);
/** 预检明细最多展示条数，超出部分由「全部通过」汇总兜底 */
const PRECHECK_MAX_ITEMS = 8;

/** 手动执行的安全提示：原文保留，收进 ⓘ 悬浮说明以腾出纵向空间 */
const EXEC_HINT =
  '手动运行的结果会记录为"手动任务"。设为"默认"后，模拟交易将直接使用本次推理的结果。'
  + '勾选股票池后仅池内标的落信号，不进模型 pred.parquet。';

interface CrossSectionRailProps {
  model: UserModelRecord;
  cs: CrossSectionInference;
  /** 右栏当前标的（排名榜高亮） */
  activeCode?: string;
  onSelectRanking: (item: InferenceRankingItem) => void;
  /** 推理历史删除后回调（交给外层刷新排名依据） */
  onDeleteHistory: (runId: string) => Promise<void> | void;
  /** 推理股票池入口：仅 A 股传入，其余市场不渲染 */
  poolSlot?: React.ReactNode;
}

export const CrossSectionRail: React.FC<CrossSectionRailProps> = ({
  model,
  cs,
  activeCode,
  onSelectRanking,
  onDeleteHistory,
  poolSlot,
}) => {
  const [outputTab, setOutputTab] = useState<'ranking' | 'history'>('ranking');
  const [precheckOpen, setPrecheckOpen] = useState(false);

  const modelName = modelDisplayName(model);
  const latestRunLabel = cs.latestRun?.model_id === model.model_id
    ? modelName
    : modelIdToDisplayName(cs.latestRun?.model_id);

  // 「当前模拟生效」= 最近一次推理的预测目标日尚未过期（原逻辑三处重复，这里收敛成一处）
  const isEffective = useMemo(() => {
    const todayStr = dayjs().format('YYYY-MM-DD');
    return Boolean(
      cs.latestRun?.run_id &&
      cs.latestRun.prediction_trade_date &&
      cs.latestRun.prediction_trade_date >= todayStr,
    );
  }, [cs.latestRun]);

  const precheckItems = useMemo(
    () => (cs.precheck?.items ?? []).filter((it) => !PRECHECK_HIDDEN_KEYS.has(it.key)).slice(0, PRECHECK_MAX_ITEMS),
    [cs.precheck],
  );

  const rankings = cs.ranking?.rankings ?? [];

  return (
    <div className="h-full min-h-0 flex flex-col rounded-xl border border-slate-200 bg-white overflow-hidden">
      {/* ── 生效状态 + 自动调度（一行状态条）───────────────────── */}
      <div className="shrink-0 h-9 px-3 flex items-center gap-2 border-b border-slate-100 bg-slate-50/70">
        <span className="text-[11px] font-bold text-slate-500 shrink-0">当前模拟生效</span>
        <span
          className={clsx(
            'inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-wide shrink-0',
            isEffective ? 'text-emerald-600' : 'text-slate-400',
          )}
        >
          <span className={clsx('w-1.5 h-1.5 rounded-full', isEffective ? 'bg-emerald-500' : 'bg-slate-300')} />
          {isEffective ? 'Active' : 'Inactive'}
        </span>

        <span className="text-slate-200 shrink-0">|</span>
        {cs.latestRunLoading ? (
          <Spin size="small" />
        ) : isEffective ? (
          <span className="min-w-0 flex items-center gap-1.5">
            <span className="text-[11px] font-mono font-bold text-slate-700 truncate" title={cs.latestRun!.run_id}>
              {cs.latestRun!.run_id}
            </span>
            <Tag className="m-0 bg-emerald-500 text-white border-0 text-[10px] font-bold px-1.5 py-0 leading-tight shrink-0">
              {cs.latestRun!.prediction_trade_date}
            </Tag>
          </span>
        ) : (
          <span className="text-[11px] text-slate-400 truncate">暂无生效推理 · {latestRunLabel || '—'}</span>
        )}

        <div className="ml-auto flex items-center gap-2 shrink-0 pl-2 border-l border-slate-200">
          <span className="text-[11px] font-bold text-slate-600">自动调度</span>
          <Switch
            size="small"
            checked={cs.autoSettings?.enabled}
            loading={cs.autoSaving}
            onChange={cs.toggleAuto}
            className={cs.autoSettings?.enabled ? 'bg-blue-600' : ''}
          />
        </div>
      </div>

      {/* ── 前置预检（默认折叠成一行：状态一眼可见，明细按需展开）── */}
      <button
        type="button"
        aria-expanded={precheckOpen}
        onClick={() => setPrecheckOpen(!precheckOpen)}
        className="shrink-0 h-9 px-3 flex items-center gap-2 border-b border-slate-100 hover:bg-slate-50/70 transition-colors text-left"
      >
        <Shield size={13} className="text-slate-400 shrink-0" />
        <span className="text-[11px] font-bold text-slate-700 shrink-0">推理前置预检</span>
        {cs.precheck ? (
          <Tag
            color={cs.precheck.passed ? 'green' : 'red'}
            className="m-0 px-1.5 py-0 rounded border-0 font-bold text-[10px] leading-tight shrink-0"
          >
            {cs.precheck.passed ? 'PASS' : 'FAIL'}
          </Tag>
        ) : (
          <span className="text-[10px] text-slate-400 shrink-0">—</span>
        )}
        <span className="text-[11px] text-slate-400 truncate">
          数据截止 {cs.precheck?.data_trade_date ?? '—'}
        </span>
        <span className="ml-auto flex items-center gap-1.5 shrink-0">
          <Tooltip title="重新检查">
            <span
              role="button"
              tabIndex={0}
              onClick={(e) => {
                e.stopPropagation();
                cs.refreshPrecheck();
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.stopPropagation();
                  e.preventDefault();
                  cs.refreshPrecheck();
                }
              }}
              className="p-1 rounded hover:bg-slate-200/70 text-slate-400 hover:text-slate-600 transition-colors"
            >
              {cs.precheckLoading ? <Spin size="small" /> : <RefreshCw size={12} />}
            </span>
          </Tooltip>
          <ChevronDown
            size={13}
            className={clsx('text-slate-400 transition-transform', precheckOpen && 'rotate-180')}
          />
        </span>
      </button>

      {precheckOpen && (
        <div className="shrink-0 px-3 py-2 border-b border-slate-100 bg-slate-50/40 max-h-[190px] overflow-y-auto custom-scrollbar">
          {cs.precheck ? (
            <div className="grid grid-cols-2 gap-x-3 gap-y-1">
              {precheckItems.map((item) => (
                <div key={item.key} className="flex items-center gap-1.5 min-w-0 h-6">
                  {item.passed
                    ? <CheckCircle2 size={12} className="text-emerald-500 shrink-0" />
                    : <AlertCircle size={12} className="text-rose-500 shrink-0" />}
                  <Text className="text-[11px] text-slate-600 truncate" title={item.label}>{item.label}</Text>
                </div>
              ))}
            </div>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-xs">暂无预检</span>} />
          )}
        </div>
      )}

      {/* ── 手动执行（一条命令栏；说明与股票池按需占行）─────────── */}
      <div className="shrink-0 border-b border-slate-100">
        <div className="h-10 px-3 flex items-center gap-2">
          <Play size={13} className="text-blue-500 shrink-0" />
          <span className="text-[11px] font-bold text-slate-700 shrink-0">手动推理执行</span>
          <Tooltip title={EXEC_HINT}>
            <Info size={12} className="text-slate-400 shrink-0 cursor-help" />
          </Tooltip>

          <DatePicker
            value={cs.inferenceDate}
            onChange={cs.setInferenceDate}
            disabledDate={(d) => d.isAfter(dayjs())}
            allowClear={false}
            className="w-[124px] shrink-0"
            size="small"
          />

          <span className="flex items-center gap-1 shrink-0 px-2 h-6 bg-blue-50/60 rounded border border-blue-100">
            <Calendar size={11} className="text-blue-400 shrink-0" />
            <span className="text-[10px] font-bold text-blue-500">T+{cs.horizonDays}</span>
            <span className="text-[11px] font-mono font-bold text-blue-700 truncate max-w-[78px]">
              {cs.targetDateLoading ? '…' : cs.targetDate || '—'}
            </span>
          </span>

          <Button
            type="primary"
            size="small"
            onClick={cs.runInference}
            loading={cs.running}
            disabled={!cs.precheck?.passed}
            className="ml-auto shrink-0 rounded-md h-7 px-3 bg-blue-600 border-0 font-bold text-[11px]"
          >
            立即执行
          </Button>
          <Tooltip title={model.is_default ? '已是默认模型' : '设为默认模型'}>
            <Button
              size="small"
              icon={
                <Star
                  size={13}
                  fill={model.is_default ? '#fcd34d' : 'none'}
                  className={model.is_default ? 'text-yellow-300' : 'text-slate-400'}
                />
              }
              onClick={model.is_default ? undefined : cs.setDefaultModel}
              className={clsx(
                'shrink-0 rounded-md h-7 w-7 border transition-colors',
                model.is_default
                  ? 'border-yellow-100 bg-yellow-50/60 cursor-default'
                  : 'border-slate-200 hover:border-yellow-200 hover:bg-yellow-50/40',
              )}
            />
          </Tooltip>
        </div>

        {poolSlot && (
          <div className="px-3 pb-2 flex items-center gap-2">
            <span className="text-[10px] font-bold text-slate-400 shrink-0">推理股票池</span>
            {poolSlot}
          </div>
        )}
      </div>

      {/* ── 产出：排名 / 历史（独占剩余高度，内部滚动）────────── */}
      <div className="flex-1 min-h-0 flex flex-col">
        <div className="shrink-0 h-9 px-2 flex items-center gap-1 border-b border-slate-200">
          <button
            type="button"
            onClick={() => setOutputTab('ranking')}
            className={clsx(
              'flex items-center gap-1.5 px-2.5 h-7 rounded-md text-[11px] font-bold transition-colors',
              outputTab === 'ranking' ? 'bg-blue-50 text-blue-700' : 'text-slate-500 hover:text-slate-800',
            )}
          >
            <LayoutGrid size={12} />
            本次推理排名
            {rankings.length > 0 && (
              <span className="font-mono text-[10px] font-normal text-slate-400">
                {cs.ranking?.target_date} · {rankings.length}
              </span>
            )}
          </button>
          <button
            type="button"
            onClick={() => setOutputTab('history')}
            className={clsx(
              'flex items-center gap-1.5 px-2.5 h-7 rounded-md text-[11px] font-bold transition-colors',
              outputTab === 'history' ? 'bg-blue-50 text-blue-700' : 'text-slate-500 hover:text-slate-800',
            )}
          >
            <History size={12} />
            推理历史
          </button>
          {outputTab === 'ranking' && rankings.length > 0 && (
            <span className="ml-auto pr-1 text-[10px] text-slate-400 select-none">点击行 → 右栏个股预测</span>
          )}
        </div>

        {/* 明细回退提示：展示的不是最近一批时必须说清楚，否则日期对不上会当成 bug */}
        {outputTab === 'ranking' && cs.rankingFallbackFrom && (
          <div className="shrink-0 px-3 py-1 bg-amber-50 border-b border-amber-100 text-[10px] text-amber-700">
            最近一批（{cs.lastRun?.run_id}）无明细行，已回退展示 {cs.rankingFallbackFrom}
          </div>
        )}

        <div className="flex-1 min-h-0 flex flex-col">
          {outputTab === 'ranking' ? (
            <RankingList
              rankings={rankings}
              loading={cs.rankingLoading}
              activeCode={activeCode}
              onSelect={onSelectRanking}
              emptyHint={
                cs.lastRun
                  ? '该批次没有落库明细行（推理记录 signals_count 有值）'
                  : '执行单日推理后显示排名'
              }
            />
          ) : (
            <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar p-2">
              <InferenceHistoryPanel modelId={model.model_id} onDelete={onDeleteHistory} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
