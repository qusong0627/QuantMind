import type { ReactNode } from 'react';

/** 从分析提示词里提取【分析配置：模式名】→ 模型名旁的模式标签。
 *  每种模式（基线/苦行/情境感知/极限杠杆…）各次分析都要可见，
 *  不只情境感知。无标签（非分析用户轮）返回 null。 */
export function modeOf(content?: string | null): ReactNode {
  const m = content ? content.match(/【分析配置：([^】]+)】/) : null;
  if (!m) return null;
  const name = m[1].trim();
  const isAware = name === '情境感知';
  return (
    <span className={`mc-mode-chip ${isAware ? '' : 'plain'}`}>
      {isAware ? '🧠 ' : ''}
      {name}
    </span>
  );
}