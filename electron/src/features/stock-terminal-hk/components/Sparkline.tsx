/** 微缩 K 线折线：近 N 日收盘价走势，纯 SVG，懒加载（IntersectionObserver）。
 *  红涨绿跌（A股口径）。仅对进入可视区的股票拉取日线，避免首屏拖慢。 */
import { useEffect, useRef, useState } from 'react';
import { stockTerminalService } from '../services/stockTerminalService';

interface Props {
  symbol: string;   // 600519.SH
  days?: number;    // 取近 N 日，默认 15
}

const W = 46;
const H = 18;

export function Sparkline({ symbol, days = 15 }: Props) {
  const [closes, setCloses] = useState<number[] | null>(null);
  const [seen, setSeen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);

  // 懒加载：进入可视区才拉数据
  useEffect(() => {
    if (seen) return;
    const el = ref.current;
    if (!el) return;
    const ob = new IntersectionObserver((entries) => {
      if (entries.some(e => e.isIntersecting)) {
        setSeen(true);
        ob.disconnect();
      }
    }, { rootMargin: '100px' });
    ob.observe(el);
    return () => ob.disconnect();
  }, [seen]);

  useEffect(() => {
    if (!seen) return;
    let cancelled = false;
    stockTerminalService.getDailyKline(symbol, days + 5).then(bars => {
      if (cancelled) return;
      const cs = bars.slice(-days).map(b => b.close).filter(Number.isFinite);
      setCloses(cs.length >= 2 ? cs : []);
    }).catch(() => { if (!cancelled) setCloses([]); });
    return () => { cancelled = true; };
  }, [seen, symbol, days]);

  if (!seen) {
    return <span ref={ref} className="inline-block" style={{ width: W, height: H }} />;
  }
  if (!closes) {
    return <span className="inline-block bg-slate-50 rounded animate-pulse" style={{ width: W, height: H }} />;
  }
  if (closes.length < 2) {
    return <span className="inline-block text-[7px] text-slate-300 text-center leading-none" style={{ width: W, height: H, lineHeight: `${H}px` }}>--</span>;
  }

  const min = Math.min(...closes);
  const max = Math.max(...closes);
  const span = max - min || 1;
  const stepX = W / (closes.length - 1);
  const pts = closes.map((c, i) => {
    const x = i * stepX;
    const y = H - ((c - min) / span) * (H - 2) - 1;  // 上下留1px
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  // 末值 vs 首值：涨红跌绿
  const up = closes[closes.length - 1] >= closes[0];
  const color = up ? '#e11d48' : '#10b981';   // rose-600 / emerald-500

  return (
    <span className="inline-block align-middle" title={`近${closes.length}日 ${(closes[closes.length-1]/closes[0]-1)*100 >= 0 ? '+' : ''}${((closes[closes.length-1]/closes[0]-1)*100).toFixed(1)}%`}>
      <svg width={W} height={H} className="block">
        <polyline points={pts} fill="none" stroke={color} strokeWidth="1.2" strokeLinejoin="round" strokeLinecap="round" />
      </svg>
    </span>
  );
}
