import { Suspense, lazy } from 'react';
import ArenaLoading from '../ArenaLoading';
import ArenaSurface from '../ArenaSurface';

/** 「行情回测」栏：arena 的 MarketLab（工作台 / 策略排行 / 单策略选股 + pine 策略库） */
const MarketLab = lazy(() => import('../arena/pages/MarketLab'));

const MarketLabTab = () => (
  <ArenaSurface>
    <Suspense fallback={<ArenaLoading label="行情回测" />}>
      <MarketLab />
    </Suspense>
  </ArenaSurface>
);

export default MarketLabTab;
