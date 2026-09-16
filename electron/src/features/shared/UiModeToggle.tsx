/** 简单/专业模式切换（T-FE-02）：顶栏与底部导航共用同一控件 */

import React from 'react';
import { useUiMode } from './useUiMode';

interface UiModeToggleProps {
  /** compact：底部导航内的更小尺寸 */
  compact?: boolean;
  className?: string;
}

export const UiModeToggle: React.FC<UiModeToggleProps> = ({ compact = false, className }) => {
  const { isSimple, setMode } = useUiMode();
  const pad = compact ? 'px-2 py-[3px] text-[9px]' : 'px-2.5 py-1 text-[10px]';
  return (
    <div
      role="radiogroup"
      aria-label="界面模式：简单 / 专业"
      className={`flex items-center rounded-full border border-slate-200 bg-white/70 p-0.5 shadow-sm ${className || ''}`}
    >
      <button
        type="button"
        role="radio"
        aria-checked={isSimple}
        onClick={() => setMode('simple')}
        className={`rounded-full ${pad} font-bold transition-colors ${
          isSimple ? 'bg-slate-800 text-white' : 'text-slate-500 hover:text-slate-700'
        }`}
        title="简单模式：术语用人话解释，只留结论与建议"
      >
        简单
      </button>
      <button
        type="button"
        role="radio"
        aria-checked={!isSimple}
        onClick={() => setMode('professional')}
        className={`rounded-full ${pad} font-bold transition-colors ${
          !isSimple ? 'bg-blue-600 text-white' : 'text-slate-500 hover:text-slate-700'
        }`}
        title="专业模式：展开九项检验/雷达/原始载荷等全部机构口径"
      >
        专业
      </button>
    </div>
  );
};
