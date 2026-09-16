/**
 * 今日交易台（FE-B / T-FE-04）：一屏闭环——管线进度 + 候选信号 + 调仓计划（预演）
 * + 今日执行 + 账户盈亏 + 系统健康；每个数字带 source 下钻（来源脚注/Tooltip）。
 */

import React, { useEffect, useState } from 'react';
import { Gauge, RefreshCw } from 'lucide-react';
import { PAGE_LAYOUT } from '../../config/pageLayout';
import { getDeskToday } from './services/deskService';
import type { DeskToday } from './types';
import { evidenceRingDrillEntries, pipelineSummary, planDrillEntries, pnlDrillEntries, statusStyle } from './deskModel';
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

const DeskTodayPage: React.FC = () => {
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

  return (
    <div className={PAGE_LAYOUT.outerClass}>
      <div className={PAGE_LAYOUT.frameClass}>
        <header className={PAGE_LAYOUT.headerClass} style={{ height: `${PAGE_LAYOUT.headerHeight}px` }}>
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-10 h-10 bg-gradient-to-br from-blue-500 to-purple-500 rounded-2xl flex items-center justify-center shadow-lg shrink-0">
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
            <span className="hidden sm:inline-flex items-center gap-1.5 rounded-full bg-slate-100 border border-slate-200 px-3 py-1 text-[11px] font-bold text-slate-600">
              <span className={`h-1.5 w-1.5 rounded-full ${worstStyle.dot}`} />
              管线 {worstStyle.label}
            </span>
            {desk?.as_of && (
              <span className="hidden md:inline-flex rounded-full bg-slate-100 border border-slate-200 px-3 py-1 text-[11px] font-bold text-slate-500">
                {new Date(desk.as_of).toLocaleString('zh-CN')}
              </span>
            )}
            <UiModeToggle />
            <button
              type="button"
              onClick={() => void load()}
              disabled={loading}
              className="px-3 py-1.5 text-xs rounded-xl border border-gray-200 bg-white hover:bg-gray-100 text-gray-700 disabled:opacity-50"
            >
              <span className="inline-flex items-center gap-1">
                <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
                刷新
              </span>
            </button>
          </div>
        </header>

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
              <PipelineBar steps={desk.pipeline} />

              <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-4 gap-4">
                <SignalsCard signals={desk.signals} />
                <div className="xl:col-span-2">
                  <PlanCard
                    plan={desk.plan}
                    onExecuted={() => void load()}
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
                <ExecutionCard execution={desk.execution} />
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

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
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
                <HealthCard health={desk.health} />
              </div>
            </>
          ) : (
            <div className="bg-gray-50 rounded-2xl border border-gray-200 p-10 text-center text-sm text-gray-500">
              暂无数据——点击右上角刷新重试
            </div>
          )}

          {/* T-FE-17 免责页脚：交易台为消费默认入口，全流程可见资质边界 */}
          <ComplianceFooter />
        </div>
      </div>

      <DrillDownDrawer
        open={!!drawer}
        title={drawer?.title || ''}
        subtitle={drawer?.subtitle}
        entries={drawer?.entries || []}
        raw={drawer?.raw}
        onClose={() => setDrawer(null)}
      />
    </div>
  );
};

export default DeskTodayPage;
