import { useCallback, useState } from 'react';
import type { MarketId } from './arena/api/client';
import { ArenaNavContext, parseArenaModelPath } from './arena/arenaNav';
import { ArenaRouterContext, useArenaRouterState } from './arena/arenaRouter';
import ModelDetail from './arena/pages/ModelDetail';
// 作用域化后的 arena 全局样式（neo-brutalism 视觉语言 + 设计变量）
import './arena/styles/globals.css';
// 宿主适配层（手写，必须排在生成样式**之后**才能覆盖；见文件头注释）
import './qm-arena-overrides.css';
// 视觉打磨层（手写，排在适配层之后 —— 它同样是覆盖式写法，见文件头注释）
import './qm-arena-theme.css';

/**
 * arena 页面的宿主外壳：把搬过来的页面**框在自己的作用域里**。
 *
 * 三件事，缺一不可：
 *  1. `qm-arena-root` 根节点 —— arena 的 CSS 已全部作用域化到这个类（见 tools/port-from-arena.mjs），
 *     少了它整棵子树的样式都不生效（表现为「裸 HTML」）。
 *  2. 参数与跳转两个 context —— arena 页面原依赖 react-router（`?market=` / `?view=` /
 *     跳 `/model/:market/:agent`）。放进交易台后：参数走内存态（不与 ?tab= 深链互踩），
 *     跳转走**栏内下钻**（不注册路由）。
 *  3. 下钻出口 —— 总控的模型表格点一行会 `nav('/model/cn/xxx')`，这里接住并渲染
 *     ModelDetail（模型详情），返回按钮回到列表。
 *
 * `className` 决定滚动归属：整栏（tab）用默认的「自己滚」，嵌进设置页卡片时传
 * `min-h-full`（让外层卡片滚，避免双滚动条）。
 *
 * 末尾那块占位高度：交易台的 `.bottom-dock` 是 absolute 覆盖层（不占布局），
 * arena 这片内容区一直铺到 y≈975，而 Dock 顶沿在 y=936 —— 底部约 40px 恒定被压住
 * （实况页底排模型卡、「DS V4-PRO 止盈 清仓 复盘」对话卡首当其冲）。让位一律用
 * **实体占位块**（padding/负 margin 在滚动容器上都会被吃掉），算式沿用仓库约定
 * `max(12px, calc(var(--dock-height) - 12px))`：有 Dock 时 52px（40px 让位 + 12px
 * 呼吸），无 Dock 时 `--dock-height` 为 0，max() 兜住负值留 12px。
 * 见 SignalsExplorerPage.tsx / PersonalCenter.tsx 同款写法。
 */
export interface ArenaSurfaceProps {
  children: React.ReactNode;
  className?: string;
}

const ArenaSurface = ({ children, className }: ArenaSurfaceProps) => {
  const router = useArenaRouterState();
  const [drill, setDrill] = useState<{ market: string; agent: string } | null>(null);

  const nav = useCallback((to: string) => {
    const target = parseArenaModelPath(to);
    if (target) setDrill(target);
    // 未识别的跳转目标（arena 以后新增的页面）静默忽略：宁可不动，也不要把
    // 交易台导航到不存在的路由上去
  }, []);

  return (
    <ArenaRouterContext.Provider value={router}>
      <ArenaNavContext.Provider value={nav}>
        <div className={`qm-arena-root ${className ?? 'h-full w-full overflow-auto bg-white'}`}>
          {drill ? (
            <ModelDetail
              market={drill.market as MarketId}
              agent={drill.agent}
              onBack={() => setDrill(null)}
            />
          ) : (
            children
          )}
          {/* 给悬浮 Dock 让位：必须是实体块，不能改 padding —— 整栏形态下根节点
              自己就是滚动容器，`min-h-full` 形态下由外层设置卡片滚，两种形态都靠
              它把最后一行顶出 Dock 覆盖区。下钻到 ModelDetail 时同样生效。 */}
          <div aria-hidden className="h-[max(12px,calc(var(--dock-height)-12px))] shrink-0" />
        </div>
      </ArenaNavContext.Provider>
    </ArenaRouterContext.Provider>
  );
};

export default ArenaSurface;
