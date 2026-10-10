/**
 * 列头漏斗的筛选浮层（Excel 式三形态：文本 / 数值区间 / 下拉）。
 *
 * 位置用 `fixed` + 触发按钮的 rect 定位：表格滚动容器带 `overflow-y-auto`，
 * 留在 `<th>` 内的绝对定位浮层会被裁掉（滚动容器即裁剪盒）。
 * 关闭走三个出口：点浮层外 / Esc / 表格滚动（锚点会跑，浮层必须跟着消失）。
 * 触发漏斗按钮带 `data-lb-funnel`，点它不算「外部点击」——否则再点一次
 * 会先关后开、看起来关不上。
 */
import React, { useEffect, useRef } from 'react';
import { X } from 'lucide-react';
import type { ColFilterConfig, NumericFilter } from './leaderboardFilters';

export type ColFilterValue =
  | { kind: 'text'; text: string }
  | { kind: 'select'; value: string }
  | { kind: 'range'; filter: NumericFilter };

interface Props {
  label: string;
  config: ColFilterConfig;
  value: ColFilterValue;
  anchor: { x: number; y: number };
  onChange: (v: ColFilterValue) => void;
  onClose: () => void;
}

/** 原值 → 输入框显示值（按 scale 换算成界面单位） */
const toDisplay = (v: number | undefined, scale: number): string =>
  v === undefined ? '' : String(Math.round(v * scale * 1e6) / 1e6);

/** 输入框 → 原值（非法输入按「未填」处理，空串 = 清除该侧） */
const fromDisplay = (s: string, scale: number): number | undefined => {
  const t = s.trim();
  if (!t) return undefined;
  const n = Number(t);
  return Number.isFinite(n) ? n / scale : undefined;
};

export const ColumnFilterPopover: React.FC<Props> = ({ label, config, value, anchor, onChange, onClose }) => {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const el = e.target as HTMLElement;
      if (el.closest('[data-lb-funnel]')) return; // 交给漏斗自身做开/关切换
      if (ref.current && !ref.current.contains(el)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [onClose]);

  const scale = config.scale ?? 1;
  const left = Math.min(anchor.x, Math.max(8, (typeof window !== 'undefined' ? window.innerWidth : 1024) - 272));
  const top = Math.min(anchor.y, Math.max(8, (typeof window !== 'undefined' ? window.innerHeight : 768) - 200));

  const isActive =
    value.kind === 'range'
      ? value.filter.min !== undefined || value.filter.max !== undefined
      : value.kind === 'text'
        ? value.text.trim() !== ''
        : value.value !== '';

  const clear = () => {
    if (value.kind === 'range') onChange({ kind: 'range', filter: {} });
    else if (value.kind === 'text') onChange({ kind: 'text', text: '' });
    else onChange({ kind: 'select', value: '' });
  };

  return (
    <div
      ref={ref}
      data-testid="lb-filter-popover"
      style={{ left, top }}
      className="fixed z-50 w-[256px] rounded-xl border border-slate-200 bg-white p-2.5 shadow-xl"
    >
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[11px] font-extrabold text-slate-700">筛选 · {label}</span>
        <button onClick={onClose} aria-label="关闭筛选浮层" className="text-slate-300 hover:text-slate-500">
          <X className="w-3 h-3" />
        </button>
      </div>

      {value.kind === 'text' && (
        <input
          data-testid="lb-ftext"
          autoFocus
          value={value.text}
          onChange={(e) => onChange({ kind: 'text', text: e.target.value })}
          placeholder={config.hint || '包含…'}
          className="w-full rounded-lg border border-slate-200 bg-slate-50/70 px-2 py-1 text-[11px] outline-none focus:border-indigo-300 focus:bg-white"
        />
      )}

      {value.kind === 'select' && (
        <select
          data-testid="lb-fmv"
          value={value.value}
          onChange={(e) => onChange({ kind: 'select', value: e.target.value })}
          className="w-full rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px]"
        >
          <option value="">全部</option>
          {(config.options || []).map((o) => (
            <option key={o} value={o}>{o}</option>
          ))}
        </select>
      )}

      {value.kind === 'range' && (
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center gap-1.5">
            <input
              data-testid="lb-fmin"
              inputMode="decimal"
              value={toDisplay(value.filter.min, scale)}
              onChange={(e) =>
                onChange({ kind: 'range', filter: { ...value.filter, min: fromDisplay(e.target.value, scale) } })
              }
              placeholder="最小"
              aria-label={`${label} 最小`}
              className="w-full min-w-0 rounded-lg border border-slate-200 bg-slate-50/70 px-2 py-1 text-[11px] font-mono outline-none focus:border-indigo-300 focus:bg-white"
            />
            <span className="text-[10px] text-slate-300 shrink-0">~</span>
            <input
              data-testid="lb-fmax"
              inputMode="decimal"
              value={toDisplay(value.filter.max, scale)}
              onChange={(e) =>
                onChange({ kind: 'range', filter: { ...value.filter, max: fromDisplay(e.target.value, scale) } })
              }
              placeholder="最大"
              aria-label={`${label} 最大`}
              className="w-full min-w-0 rounded-lg border border-slate-200 bg-slate-50/70 px-2 py-1 text-[11px] font-mono outline-none focus:border-indigo-300 focus:bg-white"
            />
          </div>
          {config.absToggle && (
            <label className="flex items-center gap-1.5 text-[10px] font-bold text-slate-500 select-none">
              <input
                data-testid="lb-fabs"
                type="checkbox"
                checked={!!value.filter.abs}
                onChange={(e) => onChange({ kind: 'range', filter: { ...value.filter, abs: e.target.checked || undefined } })}
                className="w-3 h-3 accent-indigo-600"
              />
              按绝对值（正反方向都算强）
            </label>
          )}
        </div>
      )}

      <div className="mt-1.5 flex items-center justify-between gap-2">
        <span className="text-[9px] text-slate-400 leading-4">{config.hint || ''}</span>
        {isActive && (
          <button
            data-testid="lb-fclear"
            onClick={clear}
            className="shrink-0 text-[10px] font-bold text-indigo-500 hover:text-indigo-600"
          >
            清除本列
          </button>
        )}
      </div>
    </div>
  );
};
