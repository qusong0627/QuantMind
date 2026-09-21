/**
 * 「实盘交易」栏目的**存在性探测**——公开仓与本机之间唯一的接线点。
 *
 * 栏目源码在 `electron/src/features/local-live/`，被 `.gitignore` 排除：
 * 公开仓里没有这个目录，`import.meta.glob` 匹配为空 → 底部栏不出现「实盘交易」、
 * 路由不注册。开发者本机放上该目录即可完整启用，**不需要改一行公开代码**。
 *
 * 为什么必须用 `import.meta.glob` 而不是 `import('../local-live/xxx')`：
 * 静态动态导入是**构建期解析**，缺目录时 Rollup 直接报 unresolved import →
 * 公开仓构建失败。glob 的静态模式匹配为空时返回 `{}`，是唯一"缺目录也不报错"
 * 的形态。这一点有测试钉着（`__tests__/localLiveNotTracked.test.ts`）。
 *
 * 为什么用 glob 而不是 `existsSync`：后者在渲染进程里没有 fs，且会把判断推迟到
 * 运行期——构建产物里仍会留下对不存在模块的引用。
 */

import type { ComponentType } from 'react';

/** 栏目入口文件名（垫片按此名查找，契约在此声明） */
const ENTRY_BASENAME = 'LiveTradingPage.tsx';

const pages = import.meta.glob('../local-live/*.tsx');

type PageModule = { default: ComponentType };
type PageLoader = () => Promise<PageModule>;

/** 本机是否具备「实盘交易」栏目。公开仓恒为 false。 */
export const isLocalLiveAvailable: boolean = Object.keys(pages).length > 0;

/**
 * 栏目入口的懒加载器；本机没有该目录时返回 `null`。
 *
 * 调用方（`App.tsx`）只在非 null 时才构造 `React.lazy` 并注册路由——
 * 公开仓里这个函数返回 null，整条链路不参与构建。
 */
export function loadLocalLivePage(): PageLoader | null {
    const hit = Object.entries(pages).find(([key]) =>
        key.endsWith(`/${ENTRY_BASENAME}`),
    );
    if (!hit) return null;
    return hit[1] as PageLoader;
}
