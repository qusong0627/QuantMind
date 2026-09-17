/**
 * 今日交易台（FE-B / T-FE-04）：一屏闭环——管线进度 + 候选信号 + 调仓计划（预演）
 * + 今日执行 + 账户盈亏 + 系统健康；每个数字带 source 下钻（来源脚注/Tooltip）。
 *
 * 排版（浅色专业金融风）：管线步进条整行 → 信号/计划主行 → 盈亏/执行/健康次行 → 证据矩阵；
 * 所有点击下钻统一走居中弹窗（DrillDownDrawer presentation="modal"）。
 *
 * 两种形态（2026-09-17）：
 * - 默认：独立页面（PAGE_LAYOUT 外框 + 完整头部）——保留供 /desk 路由与深链使用；
 * - embedded：内嵌进模拟交易栏目页签（外框由宿主提供，仅保留管线状态/数据时间/刷新细条）。
 */

import React, { useEffect, useState } from 'react';
import { Gauge, RefreshCw } from 'lucide-react';
import { PAGE_LAYOUT } from '../../config/pageLayout';
import { getDeskToday } from './services/deskService';
import type { DeskToday } from './types';
import { evidenceRingDrillEntries, executionItemDrillEntries, pipelineStepDrillEntries, pipelineSummary, planDrillEntries, planOrderDrillEntries, pnlDrillEntries, signalItemDrillEntries, statusStyle, symbolLabel } from './deskModel';
import { DrillDownDrawer, type DrillEntry } from '../shared/DrillDownDrawer';
import { UiModeToggle } from '../shared/UiModeToggle';
import { ComplianceFooter } from '../../components/shared/compliance/ComplianceChrome';
import { PipelineBar } from './components/PipelineBar';
import { EvidenceMatrix } from './components/EvidenceMatrix';
import { PlanCard } from './components/PlanCard';
import { ExecutionCard, HealthCard, PnlCard, SignalsCard } from './components/DeskCards';

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败';
}

interface DrawerState {
  title: string;
  subtitle?: string;
  entries: DrillEntry[];
  raw: unknown;
}

const DeskTodayPage: React.FC<{ embedded?: boolean }> = ({ embedded = false }) => {
  const [desk, setDesk] = useState<DeskToday | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [drawer, setDrawer] = useState<DrawerState | null>(null);

  useEffect(() => {
    void load();
  }, []);

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const resp = await getDeskToday({ health: true, plan: true });
      setDesk(resp?.data || null);
    } catch (err: unknown) {
      setError(errorText(err));
    } finally {
      setLoading(false);
    }
  };

  const pipeline = pipelineSummary(desk?.pipeline);
  const worstStyle = statusStyle(pipeline.worst);

  const pipelineChip = (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-slate-100 border border-slate-200 px-3 py-1 text-[11px] font-bold text-slate-600">
      <span className={`h-1.5 w-1.5 rounded-full ${worstStyle.dot}`} />
      管线 {worstStyle.label}
    </span>
  );
  const asOfChip = desk?.as_of && (
    <span className="hidden md:inline-flex rounded-full bg-slate-100 border border-slate-200 px-3 py-1 text-[11px] font-bold text-slate-500 font-mono tabular-nums">
      {new Date(desk.as_of).toLocaleString('zh-CN')}
    </span>
  );
  const refreshBtn = (
    <button
      type="button"
      onClick={() => void load()}
      disabled={loading}
      className="px-3 py-1.5 text-xs rounded-xl border border-slate-200 bg-white hover:bg-slate-50 hover:border-slate-300 text-slate-700 disabled:opacity-50 transition-colors"
    >
      <span className="inline-flex items-center gap-1">
        <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
        刷新
      </span>
    </button>
  );

  const scrollBody = (
    <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-4">
      {error && (
        <div className="bg-amber-50 border border-amber-200 rounded-2xl p-3 text-xs text-amber-800">
          {error}
        </div>
      )}

      {loading && !desk ? (
        <div className="flex items-center justify-center h-64">
          <RefreshCw className="w-6 h-6 text-blue-500 animate-spin" />
        </div>
      ) : desk ? (
        <>
          {/* T-FE-03 v2 逐层穿透：管线四步 → 证据环 → 证据项 */}
          <PipelineBar
            steps={desk.pipeline}
            onStepDrill={(step) =>
              setDrawer({
                title: `管线步骤 · ${step.label}`,
                subtitle: '与体检断言同源（点击「对应证据环」继续下钻）',
                entries: pipelineStepDrillEntries(step, desk.evidence) as DrillEntry[],
                raw: step,
              })
            }
          />

          {/* 主行：候选信号（输入）→ 调仓计划（行动） */}
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
            <div className="lg:col-span-5 xl:col-span-4 flex flex-col">
              <SignalsCard
                signals={desk.signals}
                onItemDrill={(item) =>
                  setDrawer({
                    title: `信号 · ${symbolLabel(item.symbol, item.name)}`,
                    subtitle: '字段分解 → 原始条目 → 信号块载荷（engine_signal_scores）',
                    entries: signalItemDrillEntries(item, desk.signals) as DrillEntry[],
                    raw: item,
                  })
                }
              />
            </div>
            <div className="lg:col-span-7 xl:col-span-8 flex flex-col">
              <PlanCard
                plan={desk.plan}
                onExecuted={() => void load()}
                onOrderDrill={(order) =>
                  setDrawer({
                    title: `计划单 · ${symbolLabel(order.symbol, order.name)}`,
                    subtitle: '单字段 → 触发类别/当日信号 → 原始条目（dry-run 输出）',
                    entries: planOrderDrillEntries(order, desk.plan, desk.signals) as DrillEntry[],
                    raw: order,
                  })
                }
                onDrillDown={() =>
                  setDrawer({
                    title: '调仓计划 · 来源链',
                    subtitle: '预演与执行共用同一 RebalanceCalculator（dry-run，未执行）',
                    entries: planDrillEntries(desk.plan) as DrillEntry[],
                    raw: desk.plan,
                  })
                }
              />
            </div>
          </div>

          {/* 次行：账户状态（盈亏 hero / 执行 / 健康） */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <PnlCard
              pnl={desk.pnl}
              onDrillDown={() =>
                setDrawer({
                  title: '账户盈亏 · 来源链',
                  subtitle: '资金快照行字段分解（数字直接来自该行，不重算）',
                  entries: pnlDrillEntries(desk.pnl) as DrillEntry[],
                  raw: desk.pnl,
                })
              }
            />
            <ExecutionCard
              execution={desk.execution}
              onItemDrill={(item) =>
                setDrawer({
                  title: `执行 · ${symbolLabel(item.symbol, item.name)}`,
                  subtitle: '订单字段 → 取价来源说明 → 原始条目（sim_orders 投影）',
                  entries: executionItemDrillEntries(item, desk.execution) as DrillEntry[],
                  raw: item,
                })
              }
            />
            <HealthCard health={desk.health} />
          </div>

          <EvidenceMatrix
            evidence={desk.evidence}
            onDrill={(ring) =>
              setDrawer({
                title: `证据环 · ${ring.label}（${statusStyle(ring.level).label}）`,
                subtitle: `${ring.artifact} · ${ring.frequency}——每项可核对来源`,
                entries: evidenceRingDrillEntries(ring) as DrillEntry[],
                raw: ring,
              })
            }
          />
        </>
      ) : (
        <div className="bg-gray-50 rounded-2xl border border-gray-200 p-10 text-center text-sm text-gray-500">
          暂无数据——点击右上角刷新重试
        </div>
      )}

      {/* T-FE-17 免责页脚：交易台为消费默认入口，全流程可见资质边界 */}
      <ComplianceFooter />
    </div>
  );

  const drawerNode = (
    <DrillDownDrawer
      open={!!drawer}
      presentation="modal"
      title={drawer?.title || ''}
      subtitle={drawer?.subtitle}
      entries={drawer?.entries || []}
      raw={drawer?.raw}
      onClose={() => setDrawer(null)}
    />
  );

  // 内嵌模式（模拟交易栏目页签）：外框由宿主提供，仅保留状态细条
  if (embedded) {
    return (
      <div className="h-full flex flex-col overflow-hidden bg-white">
        <div className="shrink-0 flex items-center justify-between gap-2 px-4 py-2 border-b border-gray-100 bg-white">
          {pipelineChip}
          <div className="flex items-center gap-2">
            {asOfChip}
            {refreshBtn}
          </div>
        </div>
        {scrollBody}
        {drawerNode}
      </div>
    );
  }

  return (
    <div className={PAGE_LAYOUT.outerClass}>
      <div className={PAGE_LAYOUT.frameClass}>
        <header className={PAGE_LAYOUT.headerClass} style={{ height: `${PAGE_LAYOUT.headerHeight}px` }}>
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-10 h-10 bg-gradient-to-br from-blue-600 to-indigo-600 rounded-2xl flex items-center justify-center shadow-lg shadow-blue-500/20 shrink-0">
              <Gauge className="w-5 h-5 text-white" />
            </div>
            <div className="flex items-center gap-2.5 ml-1 min-w-0">
              <h1 className="text-xl font-bold text-slate-800 tracking-tight">今日交易台</h1>
              <div className="h-4 w-[1px] bg-slate-200 self-center shrink-0" />
              <span className="text-sm font-medium text-slate-500 truncate">
                数据 → 推理 → 信号 → 计划 → 执行 → 结算，一屏看全
              </span>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {pipelineChip}
            {asOfChip}
            <UiModeToggle />
            {refreshBtn}
          </div>
        </header>

        {scrollBody}
      </div>

      {drawerNode}
    </div>
  );
};

export default DeskTodayPage;
