import React, { useEffect, useMemo, useState } from 'react';
import { Pagination } from 'antd';
import { Layers } from 'lucide-react';
import { ModelConsensusItem } from '../../../services/inferenceCenterService';
import { InferenceScoreChart } from '../../stock-terminal/components/InferenceScoreChart';

interface ModelScoreCurveGridProps {
  /** 基准日当日的多模型分数（决定分页与平均分） */
  consensus: ModelConsensusItem[];
  consensusScore: number;
  /** 用户自选的共识模型数量（0=自动取当日全部） */
  selectedCount?: number;
  /** 后缀式代码（600519.SH），供各小卡拉取 30 天分数曲线 */
  suffixSymbol: string;
  /** 基准日：各小卡曲线上橙点标记 */
  asOfDate?: string;
}

const PAGE_SIZE = 3;

export const ModelScoreCurveGrid: React.FC<ModelScoreCurveGridProps> = ({
  consensus,
  consensusScore,
  selectedCount = 0,
  suffixSymbol,
  asOfDate,
}) => {
  const [page, setPage] = useState(1);
  // 新一轮推理结果到来时回到第一页
  useEffect(() => {
    setPage(1);
  }, [consensus]);

  // 平均分：全量模型均值（非仅本页），保留 4 位小数
  const avgScore = useMemo(() => {
    if (!consensus.length) return 0;
    const sum = consensus.reduce((acc, c) => acc + (Number.isFinite(c.score) ? c.score : 0), 0);
    return sum / consensus.length;
  }, [consensus]);

  const pageItems = useMemo(() => {
    const start = (page - 1) * PAGE_SIZE;
    return consensus.slice(start, start + PAGE_SIZE);
  }, [consensus, page]);

  return (
    <div className="flex flex-col h-full bg-white/70 backdrop-blur-md rounded-2xl p-5 border border-white/80 shadow-sm">
      <div className="flex items-center justify-between pb-3 border-b border-slate-100 mb-3">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg bg-blue-50 border border-blue-100 flex items-center justify-center text-blue-600">
            <Layers className="w-4 h-4" />
          </div>
          <div>
            <h4 className="text-sm font-bold text-slate-800 m-0">多模型分数与 30 天曲线</h4>
            <p className="text-[11px] text-slate-600 m-0">一模型一卡：基准日分数 + 近30天走势（窗口以基准日为终点，与K线重叠）</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1.5 bg-blue-50 border border-blue-100 px-3 py-1 rounded-xl">
            <span className="text-[11px] text-slate-700 font-semibold">平均分:</span>
            <span className={`text-sm font-black font-mono ${avgScore >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
              {avgScore.toFixed(4)}
            </span>
            <span className="text-[10px] text-slate-600 font-mono">({consensus.length}个模型)</span>
          </div>
          {consensus.length > PAGE_SIZE && (
            <Pagination
              size="small"
              simple
              current={page}
              pageSize={PAGE_SIZE}
              total={consensus.length}
              onChange={setPage}
              showSizeChanger={false}
            />
          )}
        </div>
      </div>

      {Number(selectedCount) > 0 && consensus.length > 0 && (
        <div className="flex items-center gap-1 text-[10px] text-slate-600 -mt-1 mb-1">
          <span className="font-bold text-violet-600">自选模式</span>
          <span>已匹配 {consensus.length}/{selectedCount} 个模型当日分数</span>
          <span className="text-slate-600">· 看多占比 {consensusScore.toFixed(1)}%</span>
        </div>
      )}

      <div className="flex-1 min-h-0 overflow-x-auto overflow-y-hidden custom-scrollbar pb-2">
        {consensus.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 py-8 text-center">
            <p className="text-xs font-semibold text-slate-700 m-0">暂无多模型分数数据</p>
            <p className="text-[11px] text-slate-600 m-0 leading-relaxed max-w-[260px]">
              该标的在所选基准日未匹配到多个模型的持久化推理分数。
              <br />
              可切换模型或基准日重试，或改用其他标的。
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-3">
            {pageItems.map((item, idx) => (
              <div
                key={item.model_id || `row-${idx}`}
                className="flex flex-col rounded-xl bg-white border border-slate-200 shadow-xs overflow-hidden min-w-0"
              >
                <div className="flex items-center justify-between px-3 pt-2 pb-1">
                  <div className="flex items-center gap-1.5 min-w-0 pr-2">
                    <div className={`w-2 h-2 rounded-full shrink-0 ${item.score >= 0 ? 'bg-rose-500' : 'bg-emerald-500'}`} />
                    <span className="text-xs font-bold text-slate-800 truncate" title={item.model_name}>
                      {item.model_name}
                    </span>
                  </div>
                  <span className={`text-xs font-black font-mono shrink-0 ${item.score >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
                    {Number(item.score).toFixed(4)}
                  </span>
                </div>
                <div style={{ height: 158 }}>
                  <InferenceScoreChart
                    symbol={suffixSymbol}
                    modelId={item.model_id || undefined}
                    selectedDate={asOfDate}
                    endDate={asOfDate}
                    height={158}
                    days={30}
                    compact
                  />
                </div>
                <div className="px-3 py-1.5 bg-slate-50/70 border-t border-slate-100 flex items-center justify-between text-[10px] leading-none">
                  <span className="flex items-center gap-1 text-slate-600">
                    <span className="px-1.5 py-0.5 rounded bg-white border border-slate-200 font-mono text-[10px]">{item.model_type || '—'}</span>
                    <span className="font-mono">T+{item.horizon || 5}</span>
                  </span>
                  <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold border ${item.rating === 'STRONG_BUY' ? 'bg-rose-50 text-rose-700 border-rose-200' : item.rating === 'BUY' ? 'bg-red-50 text-red-600 border-red-200' : item.rating === 'SELL' ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 'bg-slate-100 text-slate-600 border-slate-200'}`}>
                    {item.rating}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
