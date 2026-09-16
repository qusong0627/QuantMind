/** T-FE-02 简单/专业模式：默认值、持久化、切换与非法值归一 */

import { describe, expect, it, beforeEach, vi } from 'vitest';

const PREF_KEY = 'qm:ui_mode_pref';

const loadInitialMode = async () => {
  const { default: uiReducer } = await import('../slices/uiSlice');
  return uiReducer(undefined, { type: '@@INIT' }).uiMode;
};

describe('uiSlice 界面模式', () => {
  beforeEach(() => {
    vi.resetModules();
    localStorage.clear();
  });

  it('未保存偏好时默认简单模式（简单是默认视图，不是裁剪）', async () => {
    await expect(loadInitialMode()).resolves.toBe('simple');
  });

  it('保存过 professional 偏好时恢复专业模式', async () => {
    localStorage.setItem(PREF_KEY, 'professional');
    await expect(loadInitialMode()).resolves.toBe('professional');
  });

  it('非法存值回退简单模式', async () => {
    localStorage.setItem(PREF_KEY, 'fancy');
    await expect(loadInitialMode()).resolves.toBe('simple');
  });

  it('setUiMode 更新状态并持久化；非法 payload 归一为 simple', async () => {
    const { default: uiReducer, setUiMode } = await import('../slices/uiSlice');
    const state = uiReducer(undefined, { type: '@@INIT' });

    const pro = uiReducer(state, setUiMode('professional'));
    expect(pro.uiMode).toBe('professional');
    expect(localStorage.getItem(PREF_KEY)).toBe('professional');

    const back = uiReducer(pro, setUiMode('simple'));
    expect(back.uiMode).toBe('simple');
    expect(localStorage.getItem(PREF_KEY)).toBe('simple');

    const bogus = uiReducer(pro, setUiMode('anything' as never));
    expect(bogus.uiMode).toBe('simple');
  });
});
