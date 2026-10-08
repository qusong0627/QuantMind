import { createContext, useContext } from 'react';

/**
 * arena 页面里的「跳转」出口（原 `react-router-dom` 的 `useNavigate`）。
 *
 * arena 里只有一处跳转：总控的模型表点一行 → `/model/:market/:agent`（模型详情）。
 * 移植后不注册路由，改由宿主（ArenaSurface）在**本栏内**下钻：这里把路径解析成
 * `{ market, agent }` 交给宿主渲染 ModelDetail —— 调用点仍写 `nav('/model/...')`，
 * 与 arena 原版逐字一致，将来 arena 改跳转目标也只需在解析处补一条。
 */

export type ArenaNavFn = (to: string) => void;

const noop: ArenaNavFn = () => {
  // 不在 ArenaSurface 里时应静默（例如单测直接渲染页面）
};

export const ArenaNavContext = createContext<ArenaNavFn>(noop);

export const useArenaNav = (): ArenaNavFn => useContext(ArenaNavContext);

/** `/model/cn/deepseek-v4-pro` → `{ market: 'cn', agent: 'deepseek-v4-pro' }`；不认识的路径返回 null */
export function parseArenaModelPath(to: string): { market: string; agent: string } | null {
  const m = /^\/model\/([^/]+)\/([^/?#]+)/.exec(to);
  if (!m) return null;
  return { market: decodeURIComponent(m[1]), agent: decodeURIComponent(m[2]) };
}
