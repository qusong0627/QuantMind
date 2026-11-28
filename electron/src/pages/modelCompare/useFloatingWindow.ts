/**
 * 模型对比浮窗的拖动 / 缩放 / 最大化交互。
 *
 * 交互模型对齐 modern-web-guidance 的 drag 指南：
 * - 专用拖动手柄（不是整个弹窗），按钮语义 + 方向键移动，键盘用户也能摆窗口；
 * - pointerdown 记录指针相对窗口的偏移，move 全程用偏移量，不跳位；
 * - setPointerCapture 让指针移出手柄后仍持续拖动，lostpointercapture 收尾；
 * - 每次移动/缩放都过 clampWindowRect，窗口永远留在视口内。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from 'react';
import {
  clampWindowRect,
  defaultWindowRect,
  maximizedWindowRect,
  viewportSize,
  type WindowRect,
} from './modelCompareLayout';

export type WindowInteractionMode = 'move' | 'resize-e' | 'resize-s' | 'resize-se';

interface InteractionState {
  mode: WindowInteractionMode;
  pointerId: number;
  startX: number;
  startY: number;
  origin: WindowRect;
}

const KEYBOARD_STEP = 16;
const KEYBOARD_FAST_STEP = 64;

function arrowDelta(key: string, step: number): { x: number; y: number } | null {
  switch (key) {
    case 'ArrowLeft':
      return { x: -step, y: 0 };
    case 'ArrowRight':
      return { x: step, y: 0 };
    case 'ArrowUp':
      return { x: 0, y: -step };
    case 'ArrowDown':
      return { x: 0, y: step };
    default:
      return null;
  }
}

function applyPointerDelta(
  mode: WindowInteractionMode,
  origin: WindowRect,
  dx: number,
  dy: number,
): WindowRect {
  switch (mode) {
    case 'move':
      return { ...origin, x: origin.x + dx, y: origin.y + dy };
    case 'resize-e':
      return { ...origin, width: origin.width + dx };
    case 'resize-s':
      return { ...origin, height: origin.height + dy };
    case 'resize-se':
      return { ...origin, width: origin.width + dx, height: origin.height + dy };
    default:
      return origin;
  }
}

export interface FloatingWindowApi {
  rect: WindowRect;
  maximized: boolean;
  announcement: string;
  beginInteraction: (
    mode: WindowInteractionMode,
  ) => (event: ReactPointerEvent<HTMLElement>) => void;
  handlePointerMove: (event: ReactPointerEvent<HTMLElement>) => void;
  endInteraction: (event: ReactPointerEvent<HTMLElement>) => void;
  handleMoveKeyDown: (event: ReactKeyboardEvent<HTMLElement>) => void;
  handleResizeKeyDown: (
    event: ReactKeyboardEvent<HTMLElement>,
    mode?: WindowInteractionMode,
  ) => void;
  toggleMaximize: () => void;
}

export function useFloatingWindow(open: boolean, modelCount: number): FloatingWindowApi {
  const [rect, setRect] = useState<WindowRect>(() => defaultWindowRect(modelCount));
  const [maximized, setMaximized] = useState(false);
  const [announcement, setAnnouncement] = useState('');
  const rectRef = useRef(rect);
  const restoreRef = useRef<WindowRect | null>(null);
  const interactionRef = useRef<InteractionState | null>(null);
  const initializedRef = useRef(false);
  const wasOpenRef = useRef(false);
  const lastModelCountRef = useRef(0);

  useEffect(() => {
    rectRef.current = rect;
  }, [rect]);

  // 每次打开：模型数量变了就按列数重新给默认尺寸（5 个模型不至于只露出 3 个），
  // 数量没变就保留用户上次调好的大小，只做视口收敛。
  useEffect(() => {
    if (!open) {
      wasOpenRef.current = false;
      return;
    }
    if (wasOpenRef.current) return;
    wasOpenRef.current = true;
    setRect((prev) => {
      const shouldRefit = !initializedRef.current || lastModelCountRef.current !== modelCount;
      initializedRef.current = true;
      lastModelCountRef.current = modelCount;
      return shouldRefit ? defaultWindowRect(modelCount) : clampWindowRect(prev);
    });
  }, [open, modelCount]);

  // 浏览器窗口缩放 / 旋转：把窗口收敛回视口，避免拖到屏幕外找不回来。
  useEffect(() => {
    const handleViewportResize = () => setRect((prev) => clampWindowRect(prev));
    window.addEventListener('resize', handleViewportResize);
    return () => window.removeEventListener('resize', handleViewportResize);
  }, []);

  const beginInteraction = useCallback(
    (mode: WindowInteractionMode) => (event: ReactPointerEvent<HTMLElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      interactionRef.current = {
        mode,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        origin: rectRef.current,
      };
      restoreRef.current = null;
      setMaximized(false);
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },
    [],
  );

  const handlePointerMove = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    const state = interactionRef.current;
    if (!state || state.pointerId !== event.pointerId) return;
    const next = applyPointerDelta(
      state.mode,
      state.origin,
      event.clientX - state.startX,
      event.clientY - state.startY,
    );
    setRect(clampWindowRect(next));
  }, []);

  const endInteraction = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    const state = interactionRef.current;
    if (!state || state.pointerId !== event.pointerId) return;
    interactionRef.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    setAnnouncement(state.mode === 'move' ? '对比窗口已移动' : '对比窗口大小已调整');
  }, []);

  const handleMoveKeyDown = useCallback((event: ReactKeyboardEvent<HTMLElement>) => {
    const delta = arrowDelta(event.key, event.shiftKey ? KEYBOARD_FAST_STEP : KEYBOARD_STEP);
    if (!delta) return;
    event.preventDefault();
    restoreRef.current = null;
    setMaximized(false);
    setRect((prev) => clampWindowRect({ ...prev, x: prev.x + delta.x, y: prev.y + delta.y }));
    setAnnouncement('对比窗口已移动');
  }, []);

  const handleResizeKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLElement>, mode: WindowInteractionMode = 'resize-se') => {
      const delta = arrowDelta(event.key, event.shiftKey ? KEYBOARD_FAST_STEP : KEYBOARD_STEP);
      if (!delta) return;
      event.preventDefault();
      restoreRef.current = null;
      setMaximized(false);
      setRect((prev) =>
        clampWindowRect({
          ...prev,
          // 只调宽的热区不响应上下键，只调高的不响应左右键
          width: prev.width + (mode === 'resize-s' ? 0 : delta.x),
          height: prev.height + (mode === 'resize-e' ? 0 : delta.y),
        }),
      );
      setAnnouncement('对比窗口大小已调整');
    },
    [],
  );

  const toggleMaximize = useCallback(() => {
    if (restoreRef.current) {
      setRect(clampWindowRect(restoreRef.current));
      restoreRef.current = null;
      setMaximized(false);
      setAnnouncement('对比窗口已还原');
      return;
    }
    restoreRef.current = rectRef.current;
    setRect(maximizedWindowRect(viewportSize()));
    setMaximized(true);
    setAnnouncement('对比窗口已最大化');
  }, []);

  return {
    rect,
    maximized,
    announcement,
    beginInteraction,
    handlePointerMove,
    endInteraction,
    handleMoveKeyDown,
    handleResizeKeyDown,
    toggleMaximize,
  };
}
