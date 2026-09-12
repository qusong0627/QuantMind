/** 港股市场分析 · 共享 UI 小组件（已提升为跨市场共享）
 *
 * 实现在 `features/market-analysis-shared/ui.tsx`，本文件仅保留原路径的
 * re-export，使港股既有组件零改动。新代码请直接从共享目录引入。
 */

export {
  PctText,
  NumText,
  SectionCard,
  RankRow,
  EmptyHint,
  PeriodChips,
  fmtInt,
  fmtYi,
  DateBadge,
} from '../../../market-analysis-shared/ui';
