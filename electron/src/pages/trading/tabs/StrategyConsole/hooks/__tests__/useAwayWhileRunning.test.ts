import { describe, expect, it, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useAwayWhileRunning } from '../useAwayWhileRunning';

/** jsdom 的 visibilityState 是只读的，用 getter 覆盖它。 */
const setVisibility = (state: 'visible' | 'hidden') => {
    Object.defineProperty(document, 'visibilityState', {
        configurable: true,
        get: () => state,
    });
};

const leaveAndReturn = () => {
    act(() => {
        setVisibility('hidden');
        document.dispatchEvent(new Event('visibilitychange'));
    });
    act(() => {
        setVisibility('visible');
        document.dispatchEvent(new Event('visibilitychange'));
    });
};

afterEach(() => {
    setVisibility('visible');
});

describe('useAwayWhileRunning', () => {
    it('初始为 false——还没离开过就不该提示', () => {
        // Arrange & Act
        const { result } = renderHook(() => useAwayWhileRunning(true));

        // Assert
        expect(result.current).toBe(false);
    });

    it('运行中离开再回来 → 记录为 true（这正是「关闭页面不影响运行」要拿证据说话的场景）', () => {
        // Arrange
        const { result } = renderHook(() => useAwayWhileRunning(true));

        // Act
        leaveAndReturn();

        // Assert
        expect(result.current).toBe(true);
    });

    it('未运行时离开再回来 → 仍为 false（没在跑就别提示「它一直在跑」）', () => {
        // Arrange
        const { result } = renderHook(() => useAwayWhileRunning(false));

        // Act
        leaveAndReturn();

        // Assert
        expect(result.current).toBe(false);
    });

    it('挂载后才启动策略，此后的离开也要被记录——监听器不能锁死首帧的 false', () => {
        // Arrange：首帧未运行，因此 ref 里初始是 false
        const { result, rerender } = renderHook(
            ({ running }: { running: boolean }) => useAwayWhileRunning(running),
            { initialProps: { running: false } },
        );

        // Act
        rerender({ running: true });
        leaveAndReturn();

        // Assert
        expect(result.current).toBe(true);
    });

    it('只记 hidden：回到可见不重置，反复切页签也不会重复触发状态翻转', () => {
        // Arrange
        const { result } = renderHook(() => useAwayWhileRunning(true));

        // Act
        leaveAndReturn();
        const afterFirst = result.current;
        act(() => {
            setVisibility('visible');
            document.dispatchEvent(new Event('visibilitychange'));
        });

        // Assert
        expect(afterFirst).toBe(true);
        expect(result.current).toBe(true);
    });

    it('策略停止后标记自动复位——下一轮启动不该顶着一句假提示', () => {
        // Arrange：跑着 → 离开 → 有标记
        const { result, rerender } = renderHook(
            ({ running }: { running: boolean }) => useAwayWhileRunning(running),
            { initialProps: { running: true } },
        );
        leaveAndReturn();
        expect(result.current).toBe(true);

        // Act：停止
        rerender({ running: false });

        // Assert
        expect(result.current).toBe(false);
    });

    it('卸载后不再响应事件（不留悬挂监听器）', () => {
        // Arrange
        const { result, unmount } = renderHook(() => useAwayWhileRunning(true));

        // Act
        unmount();
        leaveAndReturn();

        // Assert
        expect(result.current).toBe(false);
    });
});
