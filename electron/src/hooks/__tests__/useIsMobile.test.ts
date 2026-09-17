import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useIsMobile, MOBILE_MAX_WIDTH } from '../useIsMobile';

type Listener = (e: { matches: boolean }) => void;

/** 可手动触发 change 的 matchMedia 替身（setupTests 的全局 mock 不能派发事件） */
function mockMatchMedia(initialMatches: boolean) {
  const listeners = new Set<Listener>();
  const mql = {
    matches: initialMatches,
    media: `(max-width: ${MOBILE_MAX_WIDTH}px)`,
    onchange: null,
    addEventListener: (_type: string, cb: Listener) => { listeners.add(cb); },
    removeEventListener: (_type: string, cb: Listener) => { listeners.delete(cb); },
    addListener: (cb: Listener) => { listeners.add(cb); },
    removeListener: (cb: Listener) => { listeners.delete(cb); },
    dispatchEvent: () => true,
  };
  const spy = vi.fn().mockReturnValue(mql);
  Object.defineProperty(window, 'matchMedia', { writable: true, value: spy });
  return {
    spy,
    listeners,
    emit: (matches: boolean) => {
      mql.matches = matches;
      listeners.forEach((cb) => cb({ matches }));
    },
  };
}

describe('useIsMobile', () => {
  const original = window.matchMedia;
  afterEach(() => {
    Object.defineProperty(window, 'matchMedia', { writable: true, value: original });
  });

  it('命中移动端断点时返回 true', () => {
    const { spy } = mockMatchMedia(true);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(true);
    expect(spy).toHaveBeenCalledWith(`(max-width: ${MOBILE_MAX_WIDTH}px)`);
  });

  it('未命中断点时返回 false', () => {
    mockMatchMedia(false);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);
  });

  it('媒体查询变化时实时更新，卸载时移除监听', () => {
    const { emit, listeners } = mockMatchMedia(false);
    const { result, unmount } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);

    act(() => emit(true));
    expect(result.current).toBe(true);

    unmount();
    expect(listeners.size).toBe(0);
  });

  it('支持自定义断点', () => {
    const { spy } = mockMatchMedia(false);
    renderHook(() => useIsMobile(480));
    expect(spy).toHaveBeenCalledWith('(max-width: 480px)');
  });
});
