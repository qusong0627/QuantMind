import React from 'react';
import { FeatureDriverItem } from '../../../services/inferenceCenterService';
import { ArrowUpRight, ArrowDownRight, Zap, ShieldAlert, Sparkles, Inbox } from 'lucide-react';

interface FeatureDriversPanelProps {
  drivers: FeatureDriverItem[];
  /** 'shap'=真·模型SHAP归因 | 'heuristic'=特征启发式系数 */
  source?: 'shap' | 'heuristic';
}

export const FeatureDriversPanel: React.FC<FeatureDriversPanelProps> = ({ drivers, source }) => {
  const positiveDrivers = drivers.filter(d => d.direction === 'positive' || d.impact > 0);
  const negativeDrivers = drivers.filter(d => d.direction === 'negative' || d.impact < 0);

  return (
    <div className="flex flex-col h-full bg-white/70 backdrop-blur-md rounded-2xl p-5 border border-white/80 shadow-sm">
      <div className="flex items-center justify-between pb-3 border-b border-slate-100 mb-3">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg bg-indigo-50 border border-indigo-100 flex items-center justify-center text-indigo-600">
            <Sparkles className="w-4 h-4" />
          </div>
          <div>
            <h4 className="text-sm font-bold text-slate-800 m-0">单股因子贡献与归因透视 (SHAP Drivers)</h4>
            <p className="text-[11px] text-slate-600 m-0">驱动未来预测得分的核心正负向特征</p>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {source === 'shap' ? (
            <span className="text-[11px] font-bold text-indigo-600 bg-indigo-50 px-2 py-0.5 rounded-md border border-indigo-100">
              模型 SHAP 归因
            </span>
          ) : (
            <span className="text-[11px] font-semibold text-slate-600 bg-slate-50 px-2 py-0.5 rounded-md border border-slate-200">
              特征启发式
            </span>
          )}
          <span className="text-[11px] text-slate-600 font-mono">Top {drivers.length}</span>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 flex-1 min-h-0 overflow-y-auto">
        {drivers.length === 0 ? (
          <div className="col-span-2 flex flex-col items-center justify-center gap-2 py-8 text-center">
            <div className="w-10 h-10 rounded-xl bg-slate-50 border border-slate-100 flex items-center justify-center text-slate-300">
              <Inbox className="w-5 h-5" />
            </div>
            <p className="text-xs font-semibold text-slate-700 m-0">暂无因子贡献数据</p>
            <p className="text-[11px] text-slate-600 m-0 leading-relaxed max-w-[240px]">
              未匹配到该标的的行情特征，无法归因预测分数。
              <br />
              请确认标的代码与市场正确后重试。
            </p>
          </div>
        ) : (
          <>
            {/* 正向驱动因子 (A股红代表正向推动) */}
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-1.5 text-xs font-bold text-rose-700 bg-rose-50/80 px-2.5 py-1 rounded-lg border border-rose-100/80">
                <ArrowUpRight className="w-3.5 h-3.5 text-rose-600" />
                <span>正向推动力 (Top Positive)</span>
              </div>
              <div className="flex flex-col gap-2">
                {positiveDrivers.map((d, i) => (
                  <div
                    key={`pos-${i}`}
                    className="flex items-center justify-between p-2.5 rounded-xl bg-white border border-slate-100 shadow-xs hover:border-rose-200 transition-colors"
                  >
                    <div className="flex flex-col min-w-0 pr-2">
                      <span className="text-xs font-bold text-slate-800 truncate">{d.name}</span>
                      <div className="flex items-center gap-1.5 mt-0.5">
                        {d.category && (
                          <span className="text-[10px] text-slate-600 bg-slate-50 px-1.5 py-0.2 rounded border border-slate-100">
                            {d.category}
                          </span>
                        )}
                        {d.value !== undefined && (
                          <span className="text-[10px] text-slate-700 font-mono">值: {d.value}</span>
                        )}
                      </div>
                    </div>
                    <div className="text-right shrink-0">
                      <span className="text-xs font-black font-mono text-rose-600">
                        +{Math.abs(d.impact * 100).toFixed(2)}%
                      </span>
                      <div className="w-12 h-1 bg-rose-100 rounded-full mt-1 overflow-hidden">
                        <div
                          className="h-full bg-rose-500 rounded-full"
                          style={{ width: `${Math.min(100, Math.abs(d.impact) * 2000)}%` }}
                        />
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* 负向抑制因子 (A股绿代表负向抑制) */}
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-1.5 text-xs font-bold text-emerald-700 bg-emerald-50/80 px-2.5 py-1 rounded-lg border border-emerald-100/80">
                <ArrowDownRight className="w-3.5 h-3.5 text-emerald-600" />
                <span>负向抑制力 (Top Negative)</span>
              </div>
              <div className="flex flex-col gap-2">
                {negativeDrivers.map((d, i) => (
                  <div
                    key={`neg-${i}`}
                    className="flex items-center justify-between p-2.5 rounded-xl bg-white border border-slate-100 shadow-xs hover:border-emerald-200 transition-colors"
                  >
                    <div className="flex flex-col min-w-0 pr-2">
                      <span className="text-xs font-bold text-slate-800 truncate">{d.name}</span>
                      <div className="flex items-center gap-1.5 mt-0.5">
                        {d.category && (
                          <span className="text-[10px] text-slate-600 bg-slate-50 px-1.5 py-0.2 rounded border border-slate-100">
                            {d.category}
                          </span>
                        )}
                        {d.value !== undefined && (
                          <span className="text-[10px] text-slate-700 font-mono">值: {d.value}</span>
                        )}
                      </div>
                    </div>
                    <div className="text-right shrink-0">
                      <span className="text-xs font-black font-mono text-emerald-600">
                        -{Math.abs(d.impact * 100).toFixed(2)}%
                      </span>
                      <div className="w-12 h-1 bg-emerald-100 rounded-full mt-1 overflow-hidden">
                        <div
                          className="h-full bg-emerald-500 rounded-full"
                          style={{ width: `${Math.min(100, Math.abs(d.impact) * 2000)}%` }}
                        />
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
};
