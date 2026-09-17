/**
 * 个股终端浮窗（市场分析顶栏搜索联动）——无遮罩浮动窗口：
 * - 拖动标题栏移动、右下角手柄缩放（最小 900×560，钳制在视口内）；
 * - 最大化/还原、最小化到右下悬浮条（点击恢复，可一边看盘一边留着）；
 * - 不带遮罩，背后页面可继续操作。
 * 内容复用个股终端（CN）页面 + initialSymbol 自动选中；懒加载 + 关闭即卸载。
 */
import React, { Suspense, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { CandlestickChart, Maximize2, Minimize2, RotateCcw, X } from 'lucide-react';

const StockTerminalPage = React.lazy(() => import('../../stock-terminal/pages/StockTerminalPage'));

interface StockTerminalModalProps {
  open: boolean;
  symbol: string | null;
  onClose: () => void;
}

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

const MIN_W = 900;
const MIN_H = 560;

/** 300857.SZ → SZ300857（前端展示口径；传入终端的仍是原始 symbol） */
const displaySymbol = (s: string): string => {
  const m = s.match(/^(\d{6})\.(SH|SZ|BJ)$/i);
  return m ? `${m[2].toUpperCase()}${m[1]}` : s;
};

function defaultRect(): Rect {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const w = Math.min(1360, Math.round(vw * 0.84));
  const h = Math.min(900, Math.round(vh * 0.84));
  return {
    x: Math.max(8, Math.round((vw - w) / 2)),
    y: Math.max(16, Math.round((vh - h) / 2) - 24),
    w,
    h,
  };
}

export const StockTerminalModal: React.FC<StockTerminalModalProps> = ({ open, symbol, onClose }) => {
  const [rect, setRect] = useState<Rect>(() => defaultRect());
  const [maximized, setMaximized] = useState(false);
  const [minimized, setMinimized] = useState(false);
  const prevRectRef = useRef<Rect | null>(null);
  const dragRef = useRef<{ mode: 'move' | 'resize'; sx: number; sy: number; orig: Rect } | null>(null);

  // 打开时重置为居中默认尺寸
  useEffect(() => {
    if (!open) return;
    setRect(defaultRect());
    setMaximized(false);
    setMinimized(false);
  }, [open, symbol]);

  if (!open || !symbol) return null;

  const startDrag = (mode: 'move' | 'resize') => (e: React.MouseEvent) => {
    if (maximized) return;
    e.preventDefault();
    e.stopPropagation();
    dragRef.current = { mode, sx: e.clientX, sy: e.clientY, orig: { ...rect } };
    const onMove = (ev: MouseEvent) => {
      const d = dragRef.current;
      if (!d) return;
      const dx = ev.clientX - d.sx;
      const dy = ev.clientY - d.sy;
      if (d.mode === 'move') {
        setRect({
          ...d.orig,
          x: Math.min(Math.max(0, d.orig.x + dx), window.innerWidth - 160),
          y: Math.min(Math.max(0, d.orig.y + dy), window.innerHeight - 48),
        });
      } else {
        setRect({
          ...d.orig,
          w: Math.max(MIN_W, Math.min(window.innerWidth - d.orig.x - 8, d.orig.w + dx)),
          h: Math.max(MIN_H, Math.min(window.innerHeight - d.orig.y - 8, d.orig.h + dy)),
        });
      }
    };
    const onUp = () => {
      dragRef.current = null;
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };

  const toggleMaximize = () => {
    if (maximized) {
      setRect(prevRectRef.current || defaultRect());
      setMaximized(false);
    } else {
      prevRectRef.current = { ...rect };
      setMaximized(true);
    }
  };

  const style: React.CSSProperties = maximized
    ? { left: 8, top: 8, width: 'calc(100vw - 16px)', height: 'calc(100vh - 16px)' }
    : { left: rect.x, top: rect.y, width: rect.w, height: rect.h };

  const chromeBtn = 'flex w-7 h-7 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-100 hover:text-slate-600 transition-colors';

  return createPortal(
    <>
      {/* 最小化悬浮条：点击恢复，随时调出来看 */}
      {minimized ? (
        <div className="fixed right-4 bottom-24 z-[1150] flex items-center gap-1 bg-white border border-slate-200 rounded-full shadow-xl pl-3 pr-1.5 py-1.5">
          <button
            type="button"
            onClick={() => setMinimized(false)}
            className="flex items-center gap-2 text-xs font-bold text-slate-700 hover:text-blue-600 transition-colors"
            title="恢复个股终端"
          >
            <span className="w-5 h-5 rounded-md bg-gradient-to-br from-blue-500 to-violet-500 flex items-center justify-center">
              <CandlestickChart className="w-3 h-3 text-white" />
            </span>
            个股终端
            <span className="font-mono text-slate-500">{displaySymbol(symbol)}</span>
          </button>
          <button
            type="button"
            onClick={onClose}
            className="w-6 h-6 flex items-center justify-center rounded-full text-slate-300 hover:bg-slate-100 hover:text-slate-500"
            title="关闭"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      ) : (
        <div
          className="fixed z-[1150] flex flex-col bg-white rounded-2xl border border-slate-200 shadow-2xl overflow-hidden"
          style={style}
        >
          {/* 标题栏：可拖动 */}
          <div
            className="flex items-center gap-2.5 px-3 h-11 shrink-0 border-b border-slate-100 bg-gradient-to-r from-slate-50/80 via-white to-white cursor-move select-none"
            onMouseDown={startDrag('move')}
            onDoubleClick={toggleMaximize}
          >
            <div className="w-6 h-6 rounded-lg bg-gradient-to-br from-blue-500 to-violet-500 flex items-center justify-center shadow-md shrink-0">
              <CandlestickChart className="w-3.5 h-3.5 text-white" />
            </div>
            <span className="text-sm font-bold text-slate-800 tracking-tight">个股终端</span>
            <span className="text-[11px] font-mono font-bold text-slate-500 bg-slate-100 px-2 py-0.5 rounded">{displaySymbol(symbol)}</span>
            <div className="ml-auto flex items-center gap-0.5" onMouseDown={(e) => e.stopPropagation()}>
              <button type="button" onClick={toggleMaximize} className={chromeBtn} title={maximized ? '还原' : '最大化'}>
                {maximized ? <RotateCcw className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
              </button>
              <button type="button" onClick={() => setMinimized(true)} className={chromeBtn} title="最小化（保留悬浮条，随时恢复）">
                <Minimize2 className="w-4 h-4" />
              </button>
              <button type="button" onClick={onClose} className={chromeBtn} title="关闭">
                <X className="w-4 h-4" />
              </button>
            </div>
          </div>

          {/* 主体：个股终端（CN）；浮窗内无 Dock 遮挡，缩小底部预留 */}
          <div className="flex-1 min-h-0 overflow-hidden bg-[#f8fafc]">
            <Suspense
              fallback={
                <div className="h-full flex items-center justify-center">
                  <div className="w-8 h-8 border-3 border-blue-500 border-t-transparent rounded-full animate-spin" />
                </div>
              }
            >
              <StockTerminalPage initialSymbol={symbol} bottomReserve={12} />
            </Suspense>
          </div>

          {/* 缩放手柄（右下角） */}
          {!maximized && (
            <div
              className="absolute right-0 bottom-0 w-5 h-5 cursor-nwse-resize"
              onMouseDown={startDrag('resize')}
              title="拖动缩放"
            >
              <svg viewBox="0 0 20 20" className="w-full h-full text-slate-300">
                <path d="M19 8 L8 19 M19 14 L14 19" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinecap="round" />
              </svg>
            </div>
          )}
        </div>
      )}
    </>,
    document.body,
  );
};

export default StockTerminalModal;
