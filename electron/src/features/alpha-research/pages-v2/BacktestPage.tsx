/**
 * 回测中心（T-FB-12..15）——因子挖掘 → 跨市场样本外回测的三区装配页。
 *
 * 用户诉求原文：「回测的排名、列表，都要清晰」「我挖掘到那么多因子，都不清楚
 * 适合哪些市场」。三区各答一问：
 *   A 派发台（BatchDispatchPanel）：把哪些因子发到哪些市场；
 *   B 适配矩阵（MatrixHeatmap）：逐格看「能不能跑 / 跑出来怎样」＋排名；
 *   C 运行台账（RunLedger）：批次留档 + run 级明细（可筛可排可对比）。
 * 钻取：矩阵格 / 台账行 → FactorMarketReport 报告抽屉（含跨市场叠加）。
 *
 * 口径纪律：CN 列是样本内基准（挖掘原始市场），非 CN 为样本外重算——「适合
 * 哪个市场」只由样本外列回答（Best 徽标亦仅样本外）。
 *
 * 本页自包含：旧版「独立回测」（startBacktestTask 任务链路 + recharts 死图 +
 * 写死 CSI300 区间）已全量退役，不再复用旧 TaskContext 与旧历史面板。
 */
import React, { useCallback, useState } from 'react';
import { BarChart3 } from 'lucide-react';
import { PageHeader } from '../components-v2/layout/PageHeader';
import { BatchDispatchPanel } from '../components-v2/backtest/BatchDispatchPanel';
import { MatrixHeatmap } from '../components-v2/backtest/MatrixHeatmap';
import { RunLedger } from '../components-v2/backtest/RunLedger';
import { FactorMarketReport } from '../components-v2/backtest/FactorMarketReport';
import type { DrillTarget } from '../types-v2/backtestCenter';

export const BacktestPage: React.FC = () => {
  /**
   * 页级因子选择（派发台 / 矩阵 / 台账共享）。初始为空——不自动全选：
   * 因子库常超单批上限 200，自动全选会直接禁用派发按钮（用户还得先清），
   * 由派发台「全选筛选」按需收敛更顺。
   */
  const [selectedFactorIds, setSelectedFactorIds] = useState<string[]>([]);
  /** null = 全部市场（与后端「不传 markets 即全市场」同语义） */
  const [markets, setMarkets] = useState<string[] | null>(null);
  /** 派发台刚派发的批次（台账自动展开并轮询） */
  const [activeBatchId, setActiveBatchId] = useState<string | null>(null);
  /** 批次终态后自增 → 矩阵 / 台账重取（各组件内部以自己的 seq 防陈旧） */
  const [refreshToken, setRefreshToken] = useState(0);
  const [drill, setDrill] = useState<DrillTarget | null>(null);

  const handleBatchDispatched = useCallback((batchId: string) => {
    setActiveBatchId(batchId);
    setRefreshToken((t) => t + 1); // 新批次已落库：台账立即能看到它
  }, []);

  const handleBatchSettled = useCallback(() => {
    setRefreshToken((t) => t + 1);
  }, []);

  const handleOpenReport = useCallback((target: DrillTarget) => {
    setDrill(target);
  }, []);

  return (
    <div className="space-y-6 animate-fade-in-up" data-testid="backtest-center">
      <PageHeader
        icon={BarChart3}
        title="回测中心"
        subtitle="因子 × 市场适配矩阵——CN 为样本内基准，其余市场为样本外重算；批量派发、逐格报告、运行台账"
      />

      {/* A 区：批量派发台 */}
      <BatchDispatchPanel
        factorIds={selectedFactorIds}
        onFactorIdsChange={setSelectedFactorIds}
        markets={markets}
        onMarketsChange={setMarkets}
        activeBatchId={activeBatchId}
        onBatchDispatched={handleBatchDispatched}
        onBatchSettled={handleBatchSettled}
      />

      {/* B 区：适配矩阵（核心画面） */}
      <MatrixHeatmap
        factorIds={selectedFactorIds}
        markets={markets}
        refreshToken={refreshToken}
        onOpenCell={handleOpenReport}
      />

      {/* C 区：运行台账 */}
      <RunLedger
        factorIds={selectedFactorIds}
        activeBatchId={activeBatchId}
        refreshToken={refreshToken}
        onOpenRun={handleOpenReport}
      />

      {/* 钻取：报告抽屉 */}
      <FactorMarketReport target={drill} onClose={() => setDrill(null)} />
    </div>
  );
};

export default BacktestPage;
