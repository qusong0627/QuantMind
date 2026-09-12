/** A 股市场分析 · 申万热力矩形图（已提升为跨市场共享组件）
 *
 * 实现在 `features/market-analysis-shared/SectorHeatmapChart.tsx`，
 * 本文件仅保留原路径的 re-export，使既有调用方（A 股页 / 港股页）零改动。
 * 新代码请直接从 `features/market-analysis-shared/SectorHeatmapChart` 引入。
 */

export {
  SectorHeatmapChart as ShenwanHeatmapChart,
  SectorHeatmapChart,
  type SectorHeatmapItem as ShenwanSectorItem,
  type SectorHeatmapItem,
} from '../../market-analysis-shared/SectorHeatmapChart';
