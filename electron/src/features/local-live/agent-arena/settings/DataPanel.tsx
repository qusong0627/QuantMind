import { Suspense, lazy } from 'react';
import ArenaLoading from '../ArenaLoading';
import ArenaSurface from '../ArenaSurface';

/**
 * 「设置 → 数据」内嵌面板：arena 的数据平台页（本机 parquet 仓库浏览 / 数据集预览 /
 * 数据源开关 / 同步任务）。
 *
 * 它读的就是 QuantMind 那份数据（arena 侧把 quantmind 的 data/* 目录只读挂进去，
 * 同源同格式），所以这里看到的数据集与「数据管理」页是同一批文件。
 * 滚动同样交给外层设置卡片（`min-h-full`）。
 */
const DataPlatform = lazy(() => import('../arena/pages/DataPlatform'));

const DataPanel = () => (
  <ArenaSurface className="qm-arena-root min-h-full w-full bg-white">
    <Suspense fallback={<ArenaLoading label="数据平台" />}>
      <DataPlatform />
    </Suspense>
  </ArenaSurface>
);

export default DataPanel;
