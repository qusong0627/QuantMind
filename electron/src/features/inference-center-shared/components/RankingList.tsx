/**
 * 截面推理排名榜。
 *
 * 既是「本次推理跑出了哪些票」的结果视图，也是「个股预测推理」的入口：
 * 点任意一行即把该标的送进右栏个股预测（主从联动）。
 *
 * 版面按行情终端惯例做成密集表格（固定表头 + 细线分行 + 等宽数字），
 * 而不是卡片列表 —— 一屏能读的标的数翻倍，列也对得齐。
 * 配色沿用全站涨红跌绿：正分红色、负分绿色。
 */

import React from 'react';
import { Typography, Spin } from 'antd';
import { clsx } from 'clsx';
import { ChevronRight, TrendingUp } from 'lucide-react';
import type { InferenceRankingItem } from '../../../services/modelTrainingService';

const { Text } = Typography;

/** 表格列宽：与表头共用一份，避免表体表头错位 */
const GRID_COLS = 'grid-cols-[30px_62px_minmax(0,1fr)_74px_70px_14px]';

interface RankingListProps {
  rankings: InferenceRankingItem[];
  loading: boolean;
  /** 当前右栏正在展示的标的（高亮） */
  activeCode?: string;
  onSelect: (item: InferenceRankingItem) => void;
  /** 空态文案 */
  emptyHint?: string;
}

export const RankingList: React.FC<RankingListProps> = ({
  rankings,
  loading,
  activeCode,
  onSelect,
  emptyHint = '执行单日推理后显示排名',
}) => {
  if (loading) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-2">
        <Spin size="small" />
        <Text className="text-xs text-slate-500">正在加载排名...</Text>
      </div>
    );
  }

  if (rankings.length === 0) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-2 bg-slate-50/40">
        <TrendingUp size={18} className="text-slate-300" />
        <Text className="text-xs text-slate-500 font-semibold">{emptyHint}</Text>
      </div>
    );
  }

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      {/* 表头：与行同栅格，滚动时不位移 */}
      <div
        className={clsx(
          'shrink-0 grid items-center gap-2 px-3 h-7 bg-slate-50 border-b border-slate-200',
          'text-[10px] font-bold text-slate-500 tracking-wider select-none',
          GRID_COLS,
        )}
      >
        <span>#</span>
        <span>代码</span>
        <span>名称</span>
        <span className="truncate">行业</span>
        <span className="text-right">信号分</span>
        <span />
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar overscroll-contain">
        {rankings.map((r) => {
          const isActive = Boolean(activeCode && activeCode === r.code);
          return (
            <button
              key={r.code}
              type="button"
              onClick={() => onSelect(r)}
              title={`用 ${r.name || r.code} 做个股预测`}
              className={clsx(
                'group w-full grid items-center gap-2 px-3 h-8 border-b border-slate-100 text-left transition-colors cursor-pointer',
                GRID_COLS,
                isActive
                  ? 'bg-blue-50/80 shadow-[inset_2px_0_0_0_#2563eb]'
                  : 'hover:bg-slate-50',
              )}
            >
              <span
                className={clsx(
                  'w-5 h-5 rounded flex items-center justify-center text-[10px] font-bold font-mono tabular-nums',
                  r.rank <= 3 ? 'bg-rose-500 text-white' : 'bg-slate-100 text-slate-500',
                )}
              >
                {r.rank}
              </span>
              <Text className="text-xs font-bold text-slate-700 font-mono truncate">{r.code}</Text>
              <Text className={clsx('text-xs truncate', isActive ? 'font-bold text-blue-800' : 'text-slate-700')}>
                {r.name || '—'}
              </Text>
              <Text className="text-[11px] text-slate-400 truncate">{r.industry || '—'}</Text>
              <span className="flex items-center justify-end gap-0.5">
                <Text
                  className={clsx(
                    'text-xs font-mono font-bold tabular-nums',
                    r.score >= 0 ? 'text-rose-600' : 'text-emerald-600',
                  )}
                >
                  {r.score.toFixed(4)}
                </Text>
                {r.signal === 'buy' ? (
                  <Text className="text-[11px] font-bold text-rose-500 leading-none">↑</Text>
                ) : r.signal === 'sell' ? (
                  <Text className="text-[11px] font-bold text-emerald-500 leading-none">↓</Text>
                ) : null}
              </span>
              {/* 联动入口：箭头常驻（不挤压列宽），hover / 选中时点亮 */}
              <ChevronRight
                size={13}
                className={clsx(
                  'transition-colors',
                  isActive ? 'text-blue-600' : 'text-slate-300 group-hover:text-blue-500',
                )}
              />
            </button>
          );
        })}
      </div>
    </div>
  );
};
