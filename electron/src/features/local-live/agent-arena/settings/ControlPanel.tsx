import { Suspense, lazy } from 'react';
import ArenaLoading from '../ArenaLoading';
import ArenaSurface from '../ArenaSurface';

/**
 * 「设置 → 总控」内嵌面板：arena 的总控台（交易所/桥/MCP/dsh 状态、三市场 agent 权益表、
 * 以及原来的「交易所设置」子页 TradingSettings）。
 *
 * 嵌在设置页卡片里，所以滚动交给外层卡片（`min-h-full`），别自己再套一层滚动条。
 */
const Control = lazy(() => import('../arena/pages/Control'));

const ControlPanel = () => (
  <ArenaSurface className="qm-arena-root min-h-full w-full bg-white">
    <Suspense fallback={<ArenaLoading label="总控" />}>
      <Control />
    </Suspense>
  </ArenaSurface>
);

export default ControlPanel;
