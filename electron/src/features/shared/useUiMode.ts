/** 简单/专业模式（T-FE-02）：全局读取 + 切换封装 */

import { useAppDispatch, useAppSelector } from '../../store';
import { selectUiMode, setUiMode, type UiMode } from '../../store/slices/uiSlice';

export interface UiModeApi {
  mode: UiMode;
  isSimple: boolean;
  isProfessional: boolean;
  setMode: (mode: UiMode) => void;
  toggle: () => void;
}

export function useUiMode(): UiModeApi {
  const dispatch = useAppDispatch();
  const mode = useAppSelector(selectUiMode);
  return {
    mode,
    isSimple: mode === 'simple',
    isProfessional: mode === 'professional',
    setMode: (next) => dispatch(setUiMode(next)),
    toggle: () => dispatch(setUiMode(mode === 'simple' ? 'professional' : 'simple')),
  };
}
