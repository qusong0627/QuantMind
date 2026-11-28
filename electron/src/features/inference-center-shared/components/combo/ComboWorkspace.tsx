/**
 * 组合排名工作区（布局与状态编排）。
 *
 * 左栏组合/模板，右栏四个 Tab；数据统一由 `useComboWorkspace` 持有（单一状态源）。
 * 页面不出现方向性字眼：榜单/滚动/计划只说「靠前/居中/靠后」「新进/留存/退出」。
 */

import React, { useEffect, useState } from 'react';
import { Button, Empty, Select, Tabs, Tag, Tooltip } from 'antd';
import { CalendarDays, RefreshCw } from 'lucide-react';
import type { BlendRecord } from '../../../../services/blendTypes';
import { useComboWorkspace } from '../../hooks/useComboWorkspace';
import { ComboList } from './ComboList';
import { ComboBuilderModal } from './ComboBuilderModal';
import { ComboRankingTable } from './ComboRankingTable';
import { RollingBuildPanel } from './RollingBuildPanel';
import { PeriodReturnPanel } from './PeriodReturnPanel';
import { RebalancePlanPanel } from './RebalancePlanPanel';
import { ComboEvidencePanel } from './ComboEvidencePanel';

export interface ComboWorkspaceProps {
  market: string;
  marketLabel: string;
  /** 治理面板「组合排名」带入的模型；nonce 变化触发一次预填 */
  seedModelIds?: string[];
  seedNonce?: number;
  /** 推理中心「排除黑名单」开关：榜单按交易黑名单剔除 */
  excludeBlacklist?: boolean;
}

const POSITION_HINT =
  '位置仅描述该标的在本组合当日综合分排序中的相对位置，不构成任何交易建议。';

export const ComboWorkspace: React.FC<ComboWorkspaceProps> = ({
  market,
  marketLabel,
  seedModelIds,
  seedNonce,
  excludeBlacklist = false,
}) => {
  const state = useComboWorkspace(market, excludeBlacklist);
  const [builderOpen, setBuilderOpen] = useState(false);
  const [builderModelIds, setBuilderModelIds] = useState<string[]>([]);
  const [builderTemplate, setBuilderTemplate] = useState(false);

  const openBuilder = (modelIds: string[] = [], fromTemplate = false) => {
    setBuilderModelIds(modelIds);
    setBuilderTemplate(fromTemplate);
    setBuilderOpen(true);
  };

  // 治理面板带入的模型：每次点击都重新打开构建器（nonce 递增）
  useEffect(() => {
    if (!seedNonce || !seedModelIds || seedModelIds.length < 2) return;
    openBuilder(seedModelIds, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedNonce]);

  const handleCreated = (blend: BlendRecord) => {
    void state.refreshBlends().then(() => {
      state.select({ kind: 'blend', id: blend.blend_id });
    });
  };

  const isTemplate = state.selection?.kind === 'template';
  const blendName =
    state.selectedBlend?.name ?? state.selectedTemplate?.name ?? '组合排名';
  const holdDays = state.selectedBlend?.rolling_window ?? null;

  const dateHint = isTemplate
    ? '模板未保存：只预览最新交易日'
    : state.datesLoading
      ? '读取可用交易日…'
      : '只列有完整评分的交易日';

  return (
    <div className="flex-1 min-h-0 flex gap-3" data-testid="combo-workspace">
      <ComboList
        blends={state.blends}
        templates={state.templates}
        selection={state.selection}
        baselineTemplateId={state.baselineTemplateId}
        loading={state.snapshotLoading}
        error={state.snapshotError}
        onSelect={state.select}
        onNew={() => openBuilder([], false)}
      />

      <div className="flex-1 min-w-0 min-h-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden">
        <div className="shrink-0 flex items-center gap-3 px-3 py-2 border-b border-slate-100">
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <span className="text-xs font-black text-slate-800 truncate">{blendName}</span>
              <Tag color="blue" className="!m-0 text-[10px]">{marketLabel}</Tag>
              {isTemplate && <Tag className="!m-0 text-[10px]">模板预览</Tag>}
              {state.ranking?.blacklist?.applied && (
                <Tag color="red" className="!m-0 text-[10px]">
                  黑名单剔除 {state.ranking.blacklist.removed ?? 0}
                </Tag>
              )}
            </div>
            <Tooltip title={POSITION_HINT}>
              <div className="text-[10px] text-slate-400 cursor-help">
                位置词仅描述截面相对位置 · 不构成任何交易建议
              </div>
            </Tooltip>
          </div>

          <div className="ml-auto flex items-center gap-2">
            <span className="flex items-center gap-1 text-[11px] text-slate-500">
              <CalendarDays size={13} className="text-slate-400" /> 数据日
            </span>
            <Select
              size="small"
              className="!w-36"
              value={isTemplate ? undefined : state.date ?? undefined}
              onChange={(v) => state.setDate(v)}
              disabled={isTemplate || state.dates.length === 0}
              loading={state.datesLoading}
              placeholder={dateHint}
              options={state.dates.map((d) => ({ value: d, label: d }))}
              showSearch
            />
            <Button
              size="small"
              icon={<RefreshCw size={13} />}
              loading={state.rankingLoading}
              onClick={state.reloadRanking}
            >
              刷新榜单
            </Button>
          </div>
        </div>

        {state.snapshotError && (
          <div className="shrink-0 flex items-center gap-2 px-3 pt-2 text-[11px] text-rose-600">
            <span className="min-w-0 break-all">{state.snapshotError}</span>
            <Button
              size="small"
              type="link"
              className="!h-auto !px-1 text-[11px]"
              loading={state.snapshotLoading}
              onClick={() => void state.reloadSnapshot()}
            >
              重新加载
            </Button>
          </div>
        )}

        <Tabs
          size="small"
          activeKey={state.tab}
          onChange={(key) => state.setTab(key as typeof state.tab)}
          className="flex-1 min-h-0 px-3 [&_.ant-tabs-content]:h-full [&_.ant-tabs-content-holder]:min-h-0 [&_.ant-tabs-tabpane]:h-full"
          items={[
            {
              key: 'ranking',
              label: '当日榜单',
              children: (
                <ComboRankingTable
                  ranking={state.ranking}
                  loading={state.rankingLoading}
                  error={state.rankingError}
                  onRetry={state.reloadRanking}
                  blendName={blendName}
                  holdDays={holdDays}
                  isTemplate={isTemplate}
                />
              ),
            },
            {
              key: 'rolling',
              label: '滚动建仓',
              children: isTemplate ? (
                <div className="h-full flex items-center justify-center bg-white border border-slate-200 rounded-xl">
                  <Empty description="保存为组合后可查看滚动建仓（每日新进 × 滚动窗口）">
                    <Button type="primary" onClick={() => openBuilder([], true)}>
                      保存当前模板为组合
                    </Button>
                  </Empty>
                </div>
              ) : (
                <RollingBuildPanel
                  rolling={state.rolling}
                  capital={state.rollingCapital}
                  onCapitalCommit={state.setRollingCapital}
                  loading={state.rollingLoading}
                  error={state.rollingError}
                  onRetry={state.reloadRolling}
                />
              ),
            },
            {
              key: 'period',
              label: '周期收益',
              children: isTemplate ? (
                <div className="h-full flex items-center justify-center bg-white border border-slate-200 rounded-xl">
                  <Empty description="保存为组合后可计算周期收益（模板参数 × 自定义区间）">
                    <Button type="primary" onClick={() => openBuilder([], true)}>
                      保存当前模板为组合
                    </Button>
                  </Empty>
                </div>
              ) : (
                <PeriodReturnPanel
                  blend={state.selectedBlend}
                  period={state.period}
                  loading={state.periodLoading}
                  error={state.periodError}
                  dates={state.dates}
                  bounds={state.bounds}
                  onCalculate={(query) => void state.loadPeriod(query)}
                  onRetry={state.reloadPeriod}
                />
              ),
            },
            {
              key: 'plan',
              label: '调仓计划',
              children: (
                <RebalancePlanPanel
                  plan={state.plan}
                  blend={state.selectedBlend}
                  loading={state.planLoading}
                  error={state.planError}
                  onRetry={state.reloadPlan}
                  onGenerate={state.generatePlan}
                  onConfirm={state.confirmPlan}
                  dates={state.dates}
                  isTemplate={isTemplate}
                  onSaveTemplate={() => openBuilder([], true)}
                />
              ),
            },
            {
              key: 'evidence',
              label: '证据与回放',
              children: (
                <ComboEvidencePanel
                  evidence={state.evidence}
                  loading={state.evidenceLoading}
                  error={state.evidenceError}
                  onRetry={state.reloadEvidence}
                />
              ),
            },
          ]}
        />
      </div>

      <ComboBuilderModal
        open={builderOpen}
        market={market}
        marketLabel={marketLabel}
        initialModelIds={builderModelIds}
        initialTemplate={builderTemplate ? state.selectedTemplate : null}
        onClose={() => setBuilderOpen(false)}
        onCreated={handleCreated}
      />
    </div>
  );
};

export default ComboWorkspace;
