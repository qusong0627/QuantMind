import { createContext, useCallback, useContext, useMemo, useState } from 'react';

/**
 * arena 页面用的 **内存态** 查询参数（对应原 `react-router-dom` 的 `useSearchParams`）。
 *
 * 为什么不直接用宿主 react-router：arena 的 `?market=cn`、`?view=exchange` 若写进
 * 交易台的 URL，就会和交易台自己的 `?tab=` 深链互相覆盖 —— 参数一改，RealTradingPage
 * 会以为用户切了页签。放进内存态后两边互不干扰；代价是这两个参数不跨刷新保留
 * （刷新后回到默认 cn / overview，可接受）。
 *
 * API 与 react-router v6/v7 的 `useSearchParams` 对齐到实际用到的范围：
 * 返回 `[params, setParams]`，`params.get(key)`，`setParams(obj, { replace })`。
 */

export type ArenaParamValue = string | number | boolean | undefined | null;
export type ArenaParamsInput = Record<string, ArenaParamValue> | URLSearchParams;

export interface ArenaRouterValue {
  /** 当前查询串（只读快照） */
  search: string;
  setParams: (next: ArenaParamsInput, opts?: { replace?: boolean }) => void;
}

const EMPTY: ArenaRouterValue = {
  search: '',
  setParams: () => {
    // 没有 Provider 时（理论上不该发生）静默不动，避免抛错把整页带崩
  },
};

export const ArenaRouterContext = createContext<ArenaRouterValue>(EMPTY);

function toSearchString(next: ArenaParamsInput): string {
  const usp = next instanceof URLSearchParams ? next : new URLSearchParams();
  if (!(next instanceof URLSearchParams)) {
    Object.entries(next).forEach(([k, v]) => {
      if (v === undefined || v === null) return;
      usp.set(k, String(v));
    });
  }
  return usp.toString();
}

/** Provider 由 ArenaSurface 挂载：每个栏一份独立参数，互不串味 */
export function useArenaRouterState(): ArenaRouterValue {
  const [search, setSearch] = useState('');
  const setParams = useCallback((next: ArenaParamsInput) => {
    setSearch(toSearchString(next));
  }, []);
  return useMemo(() => ({ search, setParams }), [search, setParams]);
}

export function useSearchParams(): [URLSearchParams, (next: ArenaParamsInput, opts?: { replace?: boolean }) => void] {
  const { search, setParams } = useContext(ArenaRouterContext);
  const params = useMemo(() => new URLSearchParams(search), [search]);
  return [params, setParams];
}
