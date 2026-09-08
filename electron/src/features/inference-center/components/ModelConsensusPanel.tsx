/** 多模型共识矩阵（A股推理页已改用 ModelScoreCurveGrid，本组件现仅港股推理页使用）。 */
import React from 'react';
import { ModelConsensusItem } from '../../../services/inferenceCenterService';
import { Layers, ShieldCheck, CheckCircle2, TrendingUp, Inbox } from 'lucide-react';

interface ModelConsensusPanelProps {
  consensus: ModelConsensusItem[];
  consensusScore: number;
  /** 用户自选的共识模型数量（0=自动取当日全部） */
  selectedCount?: number;
}

export const ModelConsensusPanel: React.FC<ModelConsensusPanelProps> = ({
  consensus,
  consensusScore,
  selectedCount = 0,
}) => {
  return (
    <div className="flex flex-col h-full bg-white/70 backdrop-blur-md rounded-2xl p-5 border border-white/80 shadow-sm">
      <div className="flex items-center justify-between pb-3 border-b border-slate-100 mb-3">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg bg-blue-50 border border-blue-100 flex items-center justify-center text-blue-600">
            <Layers className="w-4 h-4" />
          </div>
          <div>
            <h4 className="text-sm font-bold text-slate-800 m-0">多模型横向共识矩阵 (Consensus Grid)</h4>
            <p className="text-[11px] text-slate-400 m-0">异构多模型对同一标的的综合预测研判</p>
          </div>
        </div>
        <div className="flex items-center gap-1.5 bg-blue-50 border border-blue-100 px-3 py-1 rounded-xl">
          <span className="text-[11px] text-slate-500 font-semibold">综合共识得分:</span>
          <span className="text-sm font-black font-mono text-blue-600">{consensusScore.toFixed(1)}/100</span>
        </div>
      </div>

      {selectedCount > 0 && consensus.length > 0 && (
        <div className="flex items-center gap-1 text-[10px] text-slate-400 -mt-1 mb-1">
          <span className="font-bold text-violet-600">自选模式</span>
          <span>已匹配 {consensus.length}/{selectedCount} 个模型当日分数</span>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 flex-1 min-h-0 overflow-y-auto">
        {consensus.length === 0 ? (
          selectedCount > 0 ? (
            <div className="col-span-2 flex flex-col items-center justify-center gap-2 py-8 text-center">
              <div className="w-10 h-10 rounded-xl bg-amber-50 border border-amber-100 flex items-center justify-center text-amber-400">
                <Inbox className="w-5 h-5" />
              </div>
              <p className="text-xs font-semibold text-slate-500 m-0">所选 {selectedCount} 个模型当日均无持久化分数</p>
              <p className="text-[11px] text-slate-400 m-0 leading-relaxed max-w-[260px]">
                自选共识只显示选定模型在基准日的真实推理分数。
                <br />
                可调整基准日、更换所选模型，或清空选择恢复自动模式。
              </p>
            </div>
          ) : (
            <div className="col-span-2 flex flex-col items-center justify-center gap-2 py-8 text-center">
              <div className="w-10 h-10 rounded-xl bg-slate-50 border border-slate-100 flex items-center justify-center text-slate-300">
                <Inbox className="w-5 h-5" />
              </div>
              <p className="text-xs font-semibold text-slate-500 m-0">暂无多模型共识数据</p>
              <p className="text-[11px] text-slate-400 m-0 leading-relaxed max-w-[240px]">
                该标的在所选基准日未匹配到多个模型的持久化推理分数。
                <br />
                可切换模型或基准日重试，或改用其他标的。
              </p>
            </div>
          )
        ) : (
          consensus.map((item, idx) => (
            <div
              key={item.model_id || idx}
              className="flex flex-col justify-between p-3 rounded-xl bg-white border border-slate-100 shadow-xs hover:shadow-sm transition-all"
            >
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-1.5 min-w-0 pr-2">
                  <div className="w-2 h-2 rounded-full bg-blue-500" />
                  <span className="text-xs font-bold text-slate-800 truncate">{item.model_name}</span>
                </div>
              </div>

              <div className="flex items-center justify-between pt-2 border-t border-slate-50">
                <span className="text-[11px] text-slate-400 font-medium">模型信号分数</span>
                <span className={`text-xs font-black font-mono ${item.expected_return >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
                  {item.expected_return.toFixed(4)}
                </span>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
};
