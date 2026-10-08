import { Suspense, lazy } from 'react';
import ArenaLoading from '../ArenaLoading';
import ArenaSurface from '../ArenaSurface';

/**
 * 「设置 → 关于」内嵌面板：arena 关于页（智能体谱系 / 架构 / 数据源 / 口径说明，纯静态）。
 *
 * 2026-09-23 用户口径「关于能放设置那里吗」——它本来是和总控/数据同级的说明性页面，
 * 单独占一条侧栏只为读一次文档，实在不划算；进设置后侧栏也正好少一栏。
 *
 * 嵌在设置页卡片里，所以**滚动交给外层卡片**（`min-h-full`），别自己再套一层滚动条
 * （同 ControlPanel 的约定）。
 */
const About = lazy(() => import('../arena/pages/About'));

const AboutPanel = () => (
    <ArenaSurface className="qm-arena-root min-h-full w-full bg-white">
        <Suspense fallback={<ArenaLoading label="关于" />}>
            <About />
        </Suspense>
    </ArenaSurface>
);

export default AboutPanel;
