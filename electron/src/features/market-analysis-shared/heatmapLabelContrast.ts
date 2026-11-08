/**
 * 热力矩形图 · 标签字色（跨市场共享的纯函数）
 *
 * 背景：色块标签固定白字，浅色档（方向概率/情绪热度的中性灰 #cbd5e1/#e2e8f0、
 * 涨跌幅的 #34d399/#f87171/#94a3b8）上白字对比度只有 1.2~2.8:1，肉眼等同「没有文字」。
 * 这里按底色相对亮度选字色：深底白字保持原观感，浅底切深字。
 */

export const LABEL_TEXT_WHITE = '#ffffff';
export const LABEL_TEXT_DARK = '#0f172a';

/** 白字对比度低于该值就切深字（#10b981≈2.56、#34d399≈1.92、#94a3b8≈2.56 都会切；
 *  #dc2626≈4.83、#059669≈3.77 保持白字）。 */
export const WHITE_TEXT_MIN_CONTRAST = 3.5;

/** 解析 #rgb / #rrggbb；其它写法（rgb()/oklch()/变量名）返回 null，由调用方兜底白字。 */
export function parseHexColor(color: string | null | undefined): [number, number, number] | null {
  if (!color || typeof color !== 'string') return null;
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color.trim());
  if (!m) return null;
  const hex = m[1].length === 3 ? m[1].split('').map((c) => c + c).join('') : m[1];
  return [
    parseInt(hex.slice(0, 2), 16),
    parseInt(hex.slice(2, 4), 16),
    parseInt(hex.slice(4, 6), 16),
  ];
}

/** WCAG 相对亮度（0=黑，1=白）。 */
export function relativeLuminance([r, g, b]: [number, number, number]): number {
  const lin = (channel: number): number => {
    const c = channel / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

/** 按底色选标签字色（白/深二选一）。无法解析的底色按白字处理，保持旧行为。 */
export function heatmapLabelColor(backgroundColor: string | null | undefined): string {
  const rgb = parseHexColor(backgroundColor);
  if (!rgb) return LABEL_TEXT_WHITE;
  const whiteContrast = 1.05 / (relativeLuminance(rgb) + 0.05);
  return whiteContrast >= WHITE_TEXT_MIN_CONTRAST ? LABEL_TEXT_WHITE : LABEL_TEXT_DARK;
}
