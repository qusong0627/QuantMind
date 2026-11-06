import React, { useEffect, useRef, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from './ui/Card';
import { RealtimeMetrics } from '../types-v2';
import { formatNumber, formatPercent } from '../utils-v2';
import { Check, Loader2, Sparkles, FileUp, Play } from 'lucide-react';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { explainFactor, exportFactorToIde } from '../services-v2/api';

interface FactorListProps {
  metrics: RealtimeMetrics | null;
  onBacktestFactor?: (factorId: string) => void;
}

const MARKET_LABELS: Record<string, string> = {
  a_share: 'A股',
};

export const FactorList: React.FC<FactorListProps> = ({ metrics, onBacktestFactor }) => {
  const [exporting, setExporting] = useState<Record<string, 'loading' | 'done' | 'error'>>({});
  const [explanations, setExplanations] = useState<Record<string, { loading: boolean; text?: string; error?: string; revision?: number | null }>>({});
  const explanationControllers = useRef(new Map<string, AbortController>());
  const [expandedFactorId, setExpandedFactorId] = useState<string | null>(null);
  const sessionGenerationRef = useRef(getSessionGeneration());

  useEffect(() => {
    sessionGenerationRef.current = getSessionGeneration();
    const unregister = registerSessionReset(() => {
      explanationControllers.current.forEach((controller) => controller.abort());
      explanationControllers.current.clear();
      sessionGenerationRef.current = getSessionGeneration();
      setExporting({});
      setExplanations({});
      setExpandedFactorId(null);
    });
    return () => { unregister(); explanationControllers.current.forEach((controller) => controller.abort()); explanationControllers.current.clear(); };
  }, []);

  const handleExplain = async (factorId: string) => {
    const generation = sessionGenerationRef.current;
    explanationControllers.current.get(factorId)?.abort();
    const controller = new AbortController(); explanationControllers.current.set(factorId, controller);
    setExplanations((prev) => ({ ...prev, [factorId]: { loading: true } }));
    try {
      const result = await explainFactor(factorId, controller.signal);
      if (controller.signal.aborted || generation !== sessionGenerationRef.current) return;
      if (result.success && result.data?.explanation) {
        const text = result.data.explanation;
        setExplanations((prev) => ({ ...prev, [factorId]: { loading: false, text, revision: result.data?.source_revision_no } }));
      } else {
        setExplanations((prev) => ({ ...prev, [factorId]: { loading: false, error: '解释失败' } }));
      }
    } catch (failure) {
      if (controller.signal.aborted || generation !== sessionGenerationRef.current) return;
      setExplanations((prev) => ({ ...prev, [factorId]: { loading: false, error: failure instanceof Error ? failure.message : '请求失败' } }));
    } finally {
      if (explanationControllers.current.get(factorId) === controller) explanationControllers.current.delete(factorId);
    }
  };

  const handleExport = async (factorId: string) => {
    const generation = sessionGenerationRef.current;
    setExporting((prev => ({ ...prev, [factorId]: 'loading' })) as unknown as Record<string, 'loading' | 'done' | 'error'>);
    try {
      const result = await exportFactorToIde(factorId);
      if (generation !== sessionGenerationRef.current) return;
      if (result.success) {
        setExporting((prev => ({ ...prev, [factorId]: 'done' })) as unknown as Record<string, 'loading' | 'done' | 'error'>);
      } else {
        setExporting((prev => ({ ...prev, [factorId]: 'error' })) as unknown as Record<string, 'loading' | 'done' | 'error'>);
      }
    } catch {
      if (generation !== sessionGenerationRef.current) return;
      setExporting((prev => ({ ...prev, [factorId]: 'error' })) as unknown as Record<string, 'loading' | 'done' | 'error'>);
    }
  };

  const truncate = (str: string, max: number) => {
    if (!str) return '';
    return str.length > max ? str.substring(0, max) + '...' : str;
  };

  const formatMetric = (value: number | undefined | null, type: 'number' | 'percent' = 'number') => {
    if (value == null || !Number.isFinite(value)) return <span className="text-muted-foreground/50">—</span>;
    return type === 'percent' ? formatPercent(value) : formatNumber(value, 4);
  };

  return (
    <Card className="glass card-hover animate-fade-in-up w-full">
      <CardHeader className="pb-3">
        <CardTitle className="text-sm flex items-center gap-2">
          <div className="h-2 w-2 rounded-full bg-purple-500 animate-pulse" />
          当前因子库 RankIC Top 10
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border/50">
                <th className="py-3 px-4 text-center font-medium text-muted-foreground w-1/6">因子名</th>
                <th className="py-3 px-4 text-center font-medium text-muted-foreground w-1/4">公式</th>
                <th className="py-3 px-4 text-center font-medium text-muted-foreground">市场</th>
                <th className="py-3 px-4 text-center font-medium text-muted-foreground">IC</th>
                <th className="py-3 px-4 text-center font-medium text-muted-foreground">RankIC</th>
                <th className="py-3 px-4 text-center font-medium text-muted-foreground">ICIR</th>
                <th className="py-3 px-4 text-center font-medium text-muted-foreground">RankICIR</th>
                <th className="py-3 px-4 text-center font-medium text-muted-foreground">ARR</th>
                <th className="py-3 px-4 text-center font-medium text-muted-foreground">MDD</th>
                <th className="py-3 px-4 text-center font-medium text-muted-foreground">Sharpe</th>
                <th className="py-3 px-4 text-center font-medium text-muted-foreground">操作</th>
              </tr>
            </thead>
            <tbody>
              {metrics?.top10Factors && metrics.top10Factors.length > 0 ? (
                metrics.top10Factors.map((factor) => (
                  <React.Fragment key={factor.factorId}>
                      <tr
                        className="group hover:bg-muted/50 transition-colors border-b border-border/50 last:border-0 cursor-pointer"
                        onClick={() => setExpandedFactorId(expandedFactorId === factor.factorId ? null : factor.factorId)}
                        title="点击查看因子详情"
                      >
                        <td className="py-3 px-4 font-medium max-w-[150px] truncate text-center" title={factor.factorName}>
                          {truncate(factor.factorName, 15)}
                        </td>
                        <td className="py-3 px-4 font-mono text-xs text-muted-foreground max-w-[200px] truncate text-center">
                          {truncate(factor.factorExpression, 30)}
                        </td>
                        <td className="py-3 px-4 text-center">
                          {factor.market ? (
                            <span className="inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] font-medium bg-secondary/30 text-muted-foreground">
                              {MARKET_LABELS[factor.market] || factor.market}
                            </span>
                          ) : (
                            <span className="text-muted-foreground/40 text-xs">—</span>
                          )}
                        </td>
                        <td className="py-3 px-4 text-center font-mono">{formatMetric(factor.ic)}</td>
                        <td className="py-3 px-4 text-center font-mono font-bold text-primary">{formatMetric(factor.rankIc)}</td>
                        <td className="py-3 px-4 text-center font-mono">{formatMetric(factor.icir)}</td>
                        <td className="py-3 px-4 text-center font-mono">{formatMetric(factor.rankIcir)}</td>
                        <td className="py-3 px-4 text-center font-mono text-success">{formatMetric(factor.annualReturn, 'percent')}</td>
                        <td className="py-3 px-4 text-center font-mono text-destructive">{formatMetric(factor.maxDrawdown, 'percent')}</td>
                        <td className="py-3 px-4 text-center font-mono">{formatMetric(factor.sharpeRatio)}</td>
                        <td className="py-3 px-4 text-center">
                          <div className="flex items-center justify-center gap-1">
                            {exporting[factor.factorId] === 'done' ? (
                              <span className="inline-flex items-center gap-1 text-xs text-green-500">
                                <Check size={12} /> 已导出
                              </span>
                            ) : exporting[factor.factorId] === 'loading' ? (
                              <Loader2 size={14} className="animate-spin text-muted-foreground" />
                            ) : (
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handleExport(factor.factorId);
                                }}
                                className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md
                                           bg-blue-500/10 text-blue-400 hover:bg-blue-500/20
                                           border border-blue-500/20 transition-colors"
                                title="导出因子代码到 AI-IDE 工作空间"
                              >
                                <FileUp size={12} /> 导出IDE
                              </button>
                            )}
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setExpandedFactorId(expandedFactorId === factor.factorId ? null : factor.factorId);
                              }}
                              className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md bg-primary/10 text-primary hover:bg-primary/20 border border-primary/20 transition-colors"
                              title="查看因子详情与解读"
                            >
                              <Sparkles size={12} /> 详情
                            </button>
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                onBacktestFactor?.(factor.factorId);
                              }}
                              disabled={!onBacktestFactor}
                              className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md
                                         bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20
                                         border border-emerald-500/20 transition-colors"
                              title="回测此因子"
                            >
                              <Play size={12} /> 回测
                            </button>
                          </div>
                        </td>
                      </tr>
                    {expandedFactorId === factor.factorId && (
                      <tr>
                        <td colSpan={11} className="p-4 bg-secondary/20">
                          <div className="space-y-4 max-w-4xl mx-auto">
                        <div>
                          <div className="flex items-center justify-between mb-2">
                            <h4 className="font-bold text-primary text-sm">{factor.factorName}</h4>
                            <div className="px-2 py-0.5 rounded-full bg-primary/10 text-primary text-xs font-medium border border-primary/20">
                               Score: {formatNumber(factor.rankIc == null ? null : factor.rankIc * 100, 1)}
                            </div>
                          </div>
                          <div className="p-3 bg-secondary/40 rounded-lg font-mono text-xs break-all border border-border/50 text-foreground/90 shadow-inner">
                            {factor.factorExpression}
                          </div>
                        </div>
                        
                        <div className="grid grid-cols-2 gap-3 p-3 bg-background/40 rounded-lg border border-border/30">
                          <div>
                            <span className="text-xs text-muted-foreground block mb-0.5">年化收益 (ARR)</span>
                            <span className="font-bold text-success text-xs">{formatPercent(factor.annualReturn)}</span>
                          </div>
                          <div>
                            <span className="text-xs text-muted-foreground block mb-0.5">夏普比率 (Sharpe)</span>
                            <span className="font-bold text-xs">{formatNumber(factor.sharpeRatio, 2)}</span>
                          </div>
                          <div>
                            <span className="text-xs text-muted-foreground block mb-0.5">最大回撤 (MDD)</span>
                            <span className="font-bold text-destructive text-xs">{formatPercent(factor.maxDrawdown)}</span>
                          </div>
                          <div>
                            <span className="text-xs text-muted-foreground block mb-0.5">卡玛比率 (CR)</span>
                            <span className="font-bold text-primary text-xs">{formatNumber(factor.calmarRatio, 2)}</span>
                          </div>
                        </div>

                        {/* AI Explanation */}
                        {explanations[factor.factorId]?.text ? (
                          <div className="p-3 bg-primary/5 rounded-lg border border-primary/20">
                            <div className="flex items-center gap-1.5 mb-2">
                              <Sparkles className="h-3.5 w-3.5 text-primary" />
                              <span className="text-xs font-medium text-primary">AI 解读</span>
                              <span className="text-xs text-muted-foreground">{explanations[factor.factorId]?.revision ? `基于代码版本 ${explanations[factor.factorId].revision}` : '基于当时公式快照'}</span>
                              <button type="button" className="text-xs text-primary" onClick={() => void handleExplain(factor.factorId)}>刷新解释</button>
                            </div>
                            <div className="text-xs text-foreground/80 whitespace-pre-line leading-relaxed">
                              {explanations[factor.factorId].text}
                            </div>
                          </div>
                        ) : (
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              handleExplain(factor.factorId);
                            }}
                            disabled={explanations[factor.factorId]?.loading}
                            className="w-full flex items-center justify-center gap-1.5 px-3 py-2 text-xs rounded-lg
                                       bg-primary/10 text-primary hover:bg-primary/20 disabled:opacity-50
                                       border border-primary/20 transition-colors"
                          >
                            {explanations[factor.factorId]?.loading ? (
                              <><Loader2 size={12} className="animate-spin" /> AI 解读中...</>
                            ) : explanations[factor.factorId]?.error ? (
                              <span className="text-destructive">{explanations[factor.factorId].error}</span>
                            ) : (
                              <><Sparkles size={12} /> AI 解读因子</>
                            )}
                          </button>
                        )}

                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))
              ) : (
                <tr>
                  <td colSpan={11} className="py-8 text-center text-muted-foreground">
                    暂无因子数据
                  </td>
                </tr>
              )}

            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
};
