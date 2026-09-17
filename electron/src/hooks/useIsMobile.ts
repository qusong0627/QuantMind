import { useEffect, useState } from 'react';

/** 移动端断点：与登录/注册页 (`window.innerWidth < 768`) 同一口径 */
export const MOBILE_MAX_WIDTH = 767;

/**
 * 视图是否为移动端宽度（默认 ≤767px）。
 * 用 matchMedia 而不是 resize 事件：旋屏/浏览器工具栏收起等场景同样能触发。
 */
export function useIsMobile(maxWidth: number = MOBILE_MAX_WIDTH): boolean {
  const query = `(max-width: ${maxWidth}px)`;
  const [isMobile, setIsMobile] = useState<boolean>(
    () => typeof window !== 'undefined' && window.matchMedia(query).matches,
  );

  useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    setIsMobile(mql.matches);
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, [query]);

  return isMobile;
}

export default useIsMobile;
