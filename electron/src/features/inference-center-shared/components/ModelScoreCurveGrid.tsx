import React, { useEffect, useMemo, useState } from 'react';
import { Pagination } from 'antd';
import { Layers } from 'lucide-react';
import { AlertTriangle } from 'lucide-react';
import { ModelConsensusItem, SingleStockPredictionResponse } from '../../../services/inferenceCenterService';
import { InferenceScoreChart } from '../../stock-terminal/components/InferenceScoreChart';
import {
  EXEC_FAIL_LABEL,
  SKIP_REASON_EXCLUDED_FROM_BANNER,
  SKIP_REASON_LABEL,
} from '../consensusLabels';

interface ModelScoreCurveGridProps {
  /** 基准日当日的多模型分数（决定分页与平均分） */
  consensus: ModelConsensusItem[];
  consensusScore: number;
  /** 覆盖度：scored/total 很小时「共识」不成立，须在标题区显式提示 */
  coverage?: SingleStockPredictionResponse['consensus_coverage'];
  /** 后端生成的覆盖不足说明句 */
  coverageNote?: string | null;
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
  coverage,
  coverageNote,
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

  // 样本不足 3 个模型时「看多占比」不构成共识，措辞必须降级（后端 is_thin 为准）
  const isThin = Boolean(coverage?.is_thin);
  const skipEntries = useMemo(
    () =>
      Object.entries(coverage?.skip_reasons ?? {}).filter(
        // `exec_failed` 已有独立的失败清单（带模型与原因），不必在「未参与」里再说一遍
        ([k, n]) => n > 0 && !SKIP_REASON_EXCLUDED_FROM_BANNER.has(k),
      ),
    [coverage],
  );
  const failedModels = coverage?.failed_models ?? [];

  // 卡片身份由外层 wrapper 提供，这里不再叠一层（双边框 + 双层内边距）
  return (
    <div className="flex flex-col h-full p-4">
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
          <div className={`flex items-center gap-1.5 border px-3 py-1 rounded-xl ${isThin ? 'bg-amber-50 border-amber-200' : 'bg-blue-50 border-blue-100'}`}>
            <span className="text-[11px] text-slate-700 font-semibold">{isThin ? '样本过少·单模型观点' : '平均分'}:</span>
            <span className={`text-sm font-black font-mono ${avgScore >= 0 ? 'text-rose-600' : 'text-emerald-600'}`}>
              {avgScore.toFixed(4)}
            </span>
            <span className="text-[10px] text-slate-600 font-mono">
              ({coverage ? `${coverage.scored}/${coverage.total}` : consensus.length} 模型)
            </span>
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

      {/* 覆盖度说明：共识的含金量取决于参与模型数，样本过少必须显式降级措辞 */}
      {isThin && (
        <div className="shrink-0 -mt-1 mb-2 rounded-lg px-3 py-2 text-[11px] leading-relaxed bg-amber-50 border border-amber-200 text-amber-800 flex items-start gap-2">
          <AlertTriangle size={13} className="shrink-0 mt-0.5 text-amber-600" />
          <div className="min-w-0">
            <span className="font-bold">共识样本不足：{coverage?.scored}/{coverage?.total} 个模型</span>
            {coverage?.trade_date && <span className="font-mono"> · 基准日 {coverage.trade_date}</span>}
            {skipEntries.length > 0 && (
              <span className="text-amber-700">
                （未参与：{skipEntries.map(([k, n]) => `${SKIP_REASON_LABEL[k] ?? k} ${n}`).join('、')}）
              </span>
            )}
            <div className="mt-0.5 text-amber-700">
              {coverageNote || '当前占比仅代表少数模型的观点，不构成多模型共识，请结合下方各模型 30 天曲线单独判断。'}
            </div>
          </div>
        </div>
      )}

      {/* 点名失败清单：即使样本数够（4 点 3 回）也要说清少的那一个卡在哪 */}
      {failedModels.length > 0 && (
        <div className="shrink-0 -mt-1 mb-1 rounded-lg px-3 py-1.5 text-[10px] leading-relaxed bg-rose-50 border border-rose-200 text-rose-800">
          <span className="font-bold">{failedModels.length} 个点名模型未能算出分数：</span>
          <span>
            {failedModels
              .map((f) => `${f.model_id}（${EXEC_FAIL_LABEL[f.error] ?? f.error}${f.detail ? `：${f.detail}` : ''}）`)
              .join('；')}
          </span>
        </div>
      )}

      {Number(selectedCount) > 0 && consensus.length > 0 && (
        <div className="flex items-center gap-1 text-[10px] text-slate-600 -mt-1 mb-1">
          <span className="font-bold text-violet-600">自选模式</span>
          <span>已匹配 {consensus.length}/{selectedCount} 个模型当日分数</span>
          <span className="text-slate-600">
            · {isThin ? '看多占比（样本过少）' : '看多占比'} {consensusScore.toFixed(1)}%
          </span>
        </div>
      )}

      {/* 这里必须能纵向裁切：小卡高度由 flex 决定，一旦允许滚动条出现，
          卡片会在「撑满」和「溢出可滚」之间抖。前提是下方小卡真的会长缩 —— 见那里的注释。
          列数也跟着实际条数走：只有 2 个模型时还留 3 列，第三列就是一条空槽，
          图还白白窄掉三分之一。 */}
      <div className="flex-1 min-h-0 overflow-x-auto overflow-y-hidden custom-scrollbar pb-2">
        {consensus.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 py-8 text-center">
            <p className="text-xs font-semibold text-slate-700 m-0">暂无多模型分数数据</p>
            <p className="text-[11px] text-slate-600 m-0 leading-relaxed max-w-[300px]">
              该标的在所选基准日未匹配到多个模型的持久化推理分数。
              <br />
              可切换模型或基准日重试，或改用其他标的。
            </p>
            {skipEntries.length > 0 && (
              <p className="text-[10px] text-slate-500 m-0 leading-relaxed max-w-[300px]">
                该市场共 {coverage?.total} 个模型，未匹配原因：
                {skipEntries.map(([k, n]) => `${SKIP_REASON_LABEL[k] ?? k} ${n} 个`).join('、')}。
              </p>
            )}
          </div>
        ) : (
          <div
            className="grid gap-3 h-full"
            style={{ gridTemplateColumns: `repeat(${Math.min(pageItems.length, PAGE_SIZE)}, minmax(0, 1fr))` }}
          >
            {pageItems.map((item, idx) => (
              <div
                key={item.model_id || `row-${idx}`}
                className="flex flex-col h-full min-h-0 rounded-xl bg-white border border-slate-200 shadow-xs overflow-hidden min-w-0"
              >
                <div className="shrink-0 flex items-center justify-between px-3 pt-2 pb-1">
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
                {/* 曲线高度**不能写死**。原先硬编码 158px，小卡自然高就是
                    28(标题) + 158 + 29(页脚) = 215px；而外层 `overflow-y-hidden` 的
                    容器只给 146px（实测），于是每次都被裁掉 79px —— 页脚整条消失，
                    曲线底部连同负值量程标签、x 轴日期一起被切。改成随容器长缩。 */}
                <div className="flex-1 min-h-[120px] min-w-0">
                  <InferenceScoreChart
                    symbol={suffixSymbol}
                    modelId={item.model_id || undefined}
                    selectedDate={asOfDate}
                    endDate={asOfDate}
                    height="100%"
                    days={30}
                    compact
                  />
                </div>
                <div className="shrink-0 px-3 py-1.5 bg-slate-50/70 border-t border-slate-100 flex items-center justify-between text-[10px] leading-none">
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
