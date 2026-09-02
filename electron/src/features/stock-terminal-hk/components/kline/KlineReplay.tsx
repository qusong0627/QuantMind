/** K 线回放控制条：截断渲染已加载数据，滑块 + 播放/暂停 + 倍速 */

import { useEffect, useRef } from 'react';
import { Pause, Play, SkipBack, SkipForward, Rewind } from 'lucide-react';
import { Slider, Select } from 'antd';

interface Props {
  active: boolean;
  onToggle: () => void;
  cursor: number;          // 0..1 当前回放进度
  onCursor: (v: number) => void;
  playing: boolean;
  onPlaying: (v: boolean) => void;
  speed: number;
  onSpeed: (v: number) => void;
  totalBars: number;
  cursorIndex: number;     // 当前展示的 bar 数（含起点）
}

export function KlineReplay({
  active, onToggle, cursor, onCursor, playing, onPlaying, speed, onSpeed,
  totalBars, cursorIndex,
}: Props) {
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (timer.current) { clearInterval(timer.current); timer.current = null; }
    if (!active || !playing) return;
    timer.current = setInterval(() => {
      onCursor(Math.min(1, cursor + 0.002 * speed));
    }, 60);
    return () => { if (timer.current) clearInterval(timer.current); };
  }, [active, playing, cursor, speed, onCursor]);

  // 播放到末尾自动暂停
  useEffect(() => {
    if (active && playing && cursor >= 1) onPlaying(false);
  }, [cursor, active, playing, onPlaying]);

  if (!active) {
    return (
      <button
        onClick={onToggle}
        className="flex items-center gap-1 text-[11px] font-bold text-slate-500 hover:text-blue-600 transition-colors px-2 py-1 rounded-lg hover:bg-blue-50"
      >
        <Rewind className="w-3 h-3" /> 回放
      </button>
    );
  }

  return (
    <div className="flex items-center gap-2 bg-slate-50 border border-slate-200 rounded-xl px-3 py-1.5 w-[380px]">
      <button onClick={() => onCursor(0)} className="text-slate-500 hover:text-blue-600" title="回到起点">
        <SkipBack className="w-3.5 h-3.5" />
      </button>
      <button onClick={() => onPlaying(!playing)} className="text-blue-600" title={playing ? '暂停' : '播放'}>
        {playing ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4" />}
      </button>
      <button onClick={() => onCursor(1)} className="text-slate-500 hover:text-blue-600" title="跳到末尾">
        <SkipForward className="w-3.5 h-3.5" />
      </button>
      <Slider
        value={cursor * 100}
        onChange={(v: number) => onCursor(v / 100)}
        tooltip={{ formatter: () => `${cursorIndex}/${totalBars} 根` }}
        className="flex-1 min-w-[80px]"
        styles={{ handle: { width: 12, height: 12 } }}
      />
      <Select
        size="small"
        variant="borderless"
        value={speed}
        onChange={onSpeed}
        options={[0.5, 1, 2, 4].map(s => ({ label: `${s}x`, value: s }))}
        style={{ width: 58 }}
      />
      <button
        onClick={onToggle}
        className="text-[10px] font-bold text-rose-500 hover:text-rose-600 shrink-0"
        title="退出回放"
      >
        退出
      </button>
    </div>
  );
}
