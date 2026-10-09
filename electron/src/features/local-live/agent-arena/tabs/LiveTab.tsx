import { Suspense, lazy } from 'react';
import ArenaLoading from '../ArenaLoading';
import ArenaSurface from '../ArenaSurface';

/** 「盘中实况」栏：arena 的 Live 页面原样搬来（净值曲线 + 模型卡 + 右侧成交/持仓/新闻/对话） */
const Live = lazy(() => import('../arena/pages/Live'));

const LiveTab = () => (
  <ArenaSurface>
    <Suspense fallback={<ArenaLoading label="盘中实况" />}>
      <Live />
    </Suspense>
  </ArenaSurface>
);

export default LiveTab;
