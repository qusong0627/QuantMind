import React, { useState, useEffect, useCallback } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '../components-v2/ui/Card';
import { Button } from '../components-v2/ui/Button';
import { Badge } from '../components-v2/ui/Badge';
import { Factor, FactorQuality, UniverseInfo } from '../types-v2';
import type { PageId } from '../components-v2/layout/Layout';
import { formatNumber, getQualityBadgeClass } from '../utils-v2';
import { getFactors, getFactorDetail, getUniverses, getFactoryFactors, classifyQuality, UNIVERSE_LABELS } from '../services-v2/api';
import { alphaAgentService, MarketInfo } from '../services/alphaAgentService';
import {
  Database,
  Search,
  Download,
  RefreshCw,
  TrendingUp,
  Code,
  Calendar,
  BarChart3,
  AlertCircle,
  Play,
  X,
  Copy,
  Check,
} from 'lucide-react';
import { useTaskContext } from '../context-v2/TaskContext';

const MARKET_LABELS: Record<string, string> = {
  a_share: 'A股',
  crypto: '加密货币',
  hong_kong: '港股',
  us_stock: '美股',
};

const MARKET_COLORS: Record<string, string> = {
  a_share: 'bg-red-500/15 text-red-400 border-red-500/30',
  crypto: 'bg-yellow-500/15 text-yellow-400 border-yellow-500/30',
  hong_kong: 'bg-blue-500/15 text-blue-400 border-blue-500/30',
  us_stock: 'bg-green-500/15 text-green-400 border-green-500/30',
};

// `onNavigate` 收 `PageId` 而不是 `string`：同级的 HomePage / MiningDashboardPage /
// Layout 都是这么写的，只有这里松了一格。松的代价是实打实的——回调最终落到
// `setCurrentPage`，收 `string` 就意味着任何拼错的页面名都能编译通过，
// 然后静默切到一个不存在的页（`currentPage === 'xxx'` 全不命中，白屏）。
export const FactorLibraryPage: React.FC<{ onNavigate?: (page: PageId) => void }> = ({ onNavigate }) => {
  const { startBacktestTask } = useTaskContext();
  const [factors, setFactors] = useState<Factor[]>([]);
  const [filteredFactors, setFilteredFactors] = useState<Factor[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [qualityFilter, setQualityFilter] = useState<FactorQuality | 'all'>('all');
  const [marketFilter, setMarketFilter] = useState<string>('all');
  const [universeFilter, setUniverseFilter] = useState<string>('all');
  const [universes, setUniverses] = useState<UniverseInfo[]>([]);
  const [markets, setMarkets] = useState<MarketInfo[]>([]);
  const [selectedFactor, setSelectedFactor] = useState<any | null>(null);
  const [copiedExpr, setCopiedExpr] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    alphaAgentService.listMarkets().then(setMarkets).catch(() => {});
    getUniverses()
      .then((res) => setUniverses(res.data?.universes ?? []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    loadFactors();
  }, [marketFilter, universeFilter]);

  useEffect(() => {
    filterFactors();
  }, [factors, searchQuery, qualityFilter, marketFilter, universeFilter]);

  const loadFactors = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const [resp, factoryResp] = await Promise.all([
        getFactors({
          market: marketFilter !== 'all' ? marketFilter : undefined,
          universe: universeFilter !== 'all' ? universeFilter : undefined,
          limit: 200,
        }),
        getFactoryFactors().catch(() => null),
      ]);
      if (resp.success && resp.data) {
        const apiFactors: Factor[] = resp.data.factors.map((f: any) => {
          const bt = f.backtestResults || {};
          return {
            // 先透传 normalizeAgentFactor 产出的全部键（rre/pfsQuality/annTurnover…），
            // 下面的显式赋值再覆盖需要归一化的字段——漏字段=新指标在列表页静默消失。
            ...f,
            factorId: f.factorId || '',
            factorName: f.factorName || 'Unknown',
            factorExpression: f.factorExpression || '',
            factorDescription: f.factorDescription || '',
            quality: (f.quality || 'low') as FactorQuality,
            market: f.market || f.metadata?.market || undefined,
            universe: f.universe || f.metadata?.universe || undefined,
            // Prioritize specific metrics from backtest results to match detail view
            ic: (typeof bt['IC'] === 'number' ? bt['IC'] : (f.ic || bt['1day.excess_return_without_cost.information_coefficient'] || 0)),
            icir: (typeof bt['ICIR'] === 'number' ? bt['ICIR'] : (f.icir || bt['1day.excess_return_without_cost.information_coefficient_ir'] || 0)),
            rankIc: (typeof bt['Rank IC'] === 'number' ? bt['Rank IC'] : (f.rankIc || bt['rank_ic'] || bt['1day.excess_return_without_cost.rank_ic'] || 0)),
            rankIcir: (typeof bt['Rank ICIR'] === 'number' ? bt['Rank ICIR'] : (f.rankIcir || bt['rank_ic_ir'] || bt['1day.excess_return_without_cost.rank_ic_ir'] || 0)),
            round: f.round || 0,
            direction: String(f.direction ?? ''),
            createdAt: f.createdAt || new Date().toISOString(),
            // Extra fields from API
            backtestResults: f.backtestResults,
            factorFormulation: f.factorFormulation,
            annualReturn: f.annualReturn,
            maxDrawdown: f.maxDrawdown,
            sharpeRatio: f.sharpeRatio,
          };
        });
        // 因子工厂产出（只读、共享）：并入列表，禁用回测/训练操作
        const generatedAt = factoryResp?.data?.generatedAt ?? '';
        const factoryFactors: Factor[] = (factoryResp?.data?.factors ?? []).map((f) => ({
          factorId: f.factorId,
          factorName: f.factorName,
          factorExpression: f.factorExpression,
          factorDescription: `因子工厂产出 · 字段 ${f.field || '—'} · 覆盖率 ${(f.coverage * 100).toFixed(0)}%`,
          quality: classifyQuality(f.ic),
          market: 'a_share',
          universe: 'all_a',
          ic: f.ic,
          icir: f.icir,
          rankIc: 0,
          rankIcir: 0,
          sharpeRatio: 0,
          annualReturn: 0,
          maxDrawdown: 0,
          round: 0,
          direction: f.ic >= 0 ? '正向' : '反向',
          createdAt: generatedAt,
          readOnly: true,
          source: 'factor_factory',
          coverage: f.coverage,
        }));
        setFactors([...factoryFactors, ...apiFactors]);
      }
    } catch (err: any) {
      console.error('Failed to load factors from API:', err);
      const status = err?.response?.status;
      if (status === 401 || status === 403) {
        setError('登录已过期，请刷新页面重新登录。');
      } else if (status) {
        setError(`后端返回错误 (HTTP ${status})。请稍后重试。`);
      } else {
        setError('无法连接 AlphaAgent 接口，请检查网络或登录状态。');
      }
      // Show empty state with error message instead of mock data
      setFactors([]);
    } finally {
      setIsLoading(false);
    }
  }, [marketFilter, universeFilter]);

  const filterFactors = () => {
    let filtered = factors;
    if (marketFilter !== 'all') {
      filtered = filtered.filter((f) => f.market === marketFilter);
    }
    if (universeFilter !== 'all') {
      filtered = filtered.filter((f) => f.universe === universeFilter);
    }
    if (qualityFilter !== 'all') {
      filtered = filtered.filter((f) => f.quality === qualityFilter);
    }
    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(
        (f) =>
          f.factorName.toLowerCase().includes(query) ||
          f.factorExpression.toLowerCase().includes(query) ||
          f.factorDescription.toLowerCase().includes(query)
      );
    }
    setFilteredFactors(filtered);
  };

  const copyToClipboard = async (text: string): Promise<boolean> => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      return true;
    } catch {
      return false;
    }
  };

  const handleCopyExpression = async () => {
    const expr = selectedFactor?.factorExpression || selectedFactor?.factor_expression || '';
    if (!expr) return;
    if (await copyToClipboard(expr)) {
      setCopiedExpr(true);
      setTimeout(() => setCopiedExpr(false), 1500);
    }
  };

  const handleExport = () => {
    const dataStr = JSON.stringify(factors, null, 2);
    const blob = new Blob([dataStr], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `factors_${new Date().toISOString().split('T')[0]}.json`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const handleSelectFactor = async (factor: Factor) => {
    // 工厂因子只读、无明细接口，直接用列表数据
    if (factor.readOnly) {
      setSelectedFactor(factor);
      return;
    }
    // Try to load full detail from API
    try {
      const resp = await getFactorDetail(factor.factorId);
      if (resp.success && resp.data?.factor) {
        setSelectedFactor({ ...factor, ...resp.data.factor });
        return;
      }
    } catch {
      // fallback
    }
    setSelectedFactor(factor);
  };

  const stats = {
    total: factors.length,
    high: factors.filter((f) => f.quality === 'high').length,
    medium: factors.filter((f) => f.quality === 'medium').length,
    low: factors.filter((f) => f.quality === 'low').length,
  };

  return (
    <div className="space-y-6 animate-fade-in-up">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold flex items-center gap-3">
            <Database className="h-8 w-8 text-primary" />
            因子库
          </h1>
          <p className="text-muted-foreground mt-1">
            浏览和管理挖掘的因子（含因子工厂批量产出，只读）
          </p>
        </div>
        <div className="flex gap-3">
          <Button variant="outline" onClick={loadFactors} disabled={isLoading}>
            <RefreshCw className={`h-4 w-4 mr-2 ${isLoading ? 'animate-spin' : ''}`} />
            刷新
          </Button>
          <Button variant="primary" onClick={handleExport}>
            <Download className="h-4 w-4 mr-2" />
            导出
          </Button>
        </div>
      </div>

      {/* Error Banner */}
      {error && (
        <div className="glass rounded-lg p-4 flex items-center gap-3 bg-warning/10 border-warning/50">
          <AlertCircle className="h-5 w-5 text-warning flex-shrink-0" />
          <span className="text-sm text-warning">{error}</span>
        </div>
      )}

      {/* Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <Card className="glass card-hover">
          <CardContent className="p-4 flex flex-col items-center justify-center text-center gap-1">
            <div className="p-3 rounded-lg bg-primary/20">
              <BarChart3 className="h-6 w-6 text-primary" />
            </div>
            <div className="text-sm text-muted-foreground">总因子数</div>
            <div className="text-2xl font-bold">{stats.total}</div>
          </CardContent>
        </Card>

        <Card className="glass card-hover">
          <CardContent className="p-4 flex flex-col items-center justify-center text-center gap-1">
            <div className="p-3 rounded-lg bg-success/20">
              <TrendingUp className="h-6 w-6 text-success" />
            </div>
            <div className="text-sm text-muted-foreground">高质量</div>
            <div className="text-2xl font-bold text-success">{stats.high}</div>
          </CardContent>
        </Card>

        <Card className="glass card-hover">
          <CardContent className="p-4 flex flex-col items-center justify-center text-center gap-1">
            <div className="p-3 rounded-lg bg-warning/20">
              <BarChart3 className="h-6 w-6 text-warning" />
            </div>
            <div className="text-sm text-muted-foreground">中等质量</div>
            <div className="text-2xl font-bold text-warning">{stats.medium}</div>
          </CardContent>
        </Card>

        <Card className="glass card-hover">
          <CardContent className="p-4 flex flex-col items-center justify-center text-center gap-1">
            <div className="p-3 rounded-lg bg-destructive/20">
              <BarChart3 className="h-6 w-6 text-destructive" />
            </div>
            <div className="text-sm text-muted-foreground">低质量</div>
            <div className="text-2xl font-bold text-destructive">{stats.low}</div>
          </CardContent>
        </Card>
      </div>

      {/* Filters */}
      <Card className="glass">
        <CardContent className="p-4">
          <div className="flex flex-col gap-4">
            {/* Market filter */}
            <div className="flex flex-wrap gap-2">
              <Button
                variant={marketFilter === 'all' ? 'primary' : 'outline'}
                size="sm"
                onClick={() => setMarketFilter('all')}
              >
                全部市场
              </Button>
              {markets.map((m) => (
                <Button
                  key={m.market_id}
                  variant={marketFilter === m.market_id ? 'primary' : 'outline'}
                  size="sm"
                  onClick={() => setMarketFilter(m.market_id)}
                >
                  {m.market_name}
                </Button>
              ))}
            </div>
            {/* Universe filter — A-share stock pools */}
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-muted-foreground mr-1">股票池</span>
              <Button
                variant={universeFilter === 'all' ? 'primary' : 'outline'}
                size="sm"
                onClick={() => setUniverseFilter('all')}
              >
                全部
              </Button>
              {(universes.length > 0
                ? universes
                : (Object.keys(UNIVERSE_LABELS) as Array<keyof typeof UNIVERSE_LABELS>).map(
                    (id) => ({ id, name: UNIVERSE_LABELS[id], stockCount: 0, indexSymbol: null }),
                  )
              ).map((u) => (
                <Button
                  key={u.id}
                  variant={universeFilter === u.id ? 'primary' : 'outline'}
                  size="sm"
                  onClick={() => setUniverseFilter(u.id)}
                >
                  {u.name}
                </Button>
              ))}
            </div>
            {/* Search + quality filter */}
            <div className="flex flex-col md:flex-row gap-4">
              <div className="flex-1">
                <div className="relative">
                  <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <input
                    type="text"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    placeholder="搜索因子名称、表达式或描述..."
                    className="w-full pl-10 pr-4 py-2 rounded-lg border border-input bg-background text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                </div>
              </div>
              <div className="flex gap-2">
                <Button
                  variant={qualityFilter === 'all' ? 'primary' : 'outline'}
                  size="sm"
                  onClick={() => setQualityFilter('all')}
                >
                  全部 ({stats.total})
                </Button>
                <Button
                  variant={qualityFilter === 'high' ? 'primary' : 'outline'}
                  size="sm"
                  onClick={() => setQualityFilter('high')}
                >
                  高质量 ({stats.high})
                </Button>
                <Button
                  variant={qualityFilter === 'medium' ? 'primary' : 'outline'}
                  size="sm"
                  onClick={() => setQualityFilter('medium')}
                >
                  中等 ({stats.medium})
                </Button>
                <Button
                  variant={qualityFilter === 'low' ? 'primary' : 'outline'}
                  size="sm"
                  onClick={() => setQualityFilter('low')}
                >
                  低质量 ({stats.low})
                </Button>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Factor List */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {filteredFactors.map((factor) => (
          <Card
            key={factor.factorId}
            className="glass card-hover cursor-pointer"
            onClick={() => handleSelectFactor(factor)}
          >
            <CardHeader className="pb-3">
              <div className="flex items-start justify-between">
                <div className="flex-1">
                  <CardTitle className="text-base">{factor.factorName}</CardTitle>
                  <div className="flex items-center gap-2 mt-2">
                    <Badge className={getQualityBadgeClass(factor.quality)}>
                      {factor.quality === 'high' ? '高' : factor.quality === 'medium' ? '中' : '低'}
                    </Badge>
                    {factor.readOnly && (
                      <span className="inline-flex items-center rounded-md border border-primary/30 bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                        工厂
                      </span>
                    )}
                    {factor.market && (
                      <span className={`inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium ${MARKET_COLORS[factor.market] || 'bg-secondary text-muted-foreground'}`}>
                        {MARKET_LABELS[factor.market] || factor.market}
                      </span>
                    )}
                    {factor.round > 0 && (
                      <span className="text-xs text-muted-foreground">
                        Round {factor.round}
                      </span>
                    )}
                    {factor.direction && (
                      <span className="text-xs text-muted-foreground">
                        方向 {factor.direction}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-sm text-muted-foreground line-clamp-2">
                {factor.factorDescription}
              </p>
              <div className="rounded-lg bg-secondary/30 p-3">
                <div className="flex items-center gap-2 mb-2">
                  <Code className="h-3 w-3 text-muted-foreground" />
                  <span className="text-xs text-muted-foreground">表达式</span>
                </div>
                <code className="text-xs font-mono line-clamp-2">
                  {factor.factorExpression}
                </code>
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div>
                  <span className="text-muted-foreground">IC: </span>
                  <span className="font-mono font-medium">{formatNumber(factor.ic, 4)}</span>
                </div>
                <div>
                  <span className="text-muted-foreground">RankIC: </span>
                  <span className="font-mono font-medium">{formatNumber(factor.rankIc, 4)}</span>
                </div>
                <div>
                  <span className="text-muted-foreground">ICIR: </span>
                  <span className="font-mono font-medium">{formatNumber(factor.icir, 3)}</span>
                </div>
                <div>
                  <span className="text-muted-foreground">RankICIR: </span>
                  <span className="font-mono font-medium">{formatNumber(factor.rankIcir, 3)}</span>
                </div>
              </div>
              {/* Action buttons */}
              <div className="flex items-center gap-2 pt-1 border-t border-border/30">
                {factor.readOnly ? (
                  <span className="text-xs text-muted-foreground flex-1">
                    工厂因子为批量产出（只读），请在训练/特征目录中使用
                  </span>
                ) : (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 text-xs flex-1"
                    onClick={async (e) => {
                      e.stopPropagation();
                      try {
                        await startBacktestTask({
                          factorId: factor.factorId,
                          universe: factor.universe || 'csi300',
                        });
                        onNavigate?.('backtest');
                      } catch (err) {
                        console.error('Backtest failed:', err);
                      }
                    }}
                  >
                    <Play className="h-3 w-3 mr-1" /> 回测
                  </Button>
                )}
              </div>
              {factor.createdAt && (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Calendar className="h-3 w-3" />
                  {new Date(factor.createdAt).toLocaleString('zh-CN')}
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Empty State */}
      {filteredFactors.length === 0 && !isLoading && (
        <Card className="glass">
          <CardContent className="p-12 text-center">
            <Database className="h-16 w-16 mx-auto text-muted-foreground mb-4" />
            <h3 className="text-lg font-medium mb-2">暂无因子</h3>
            <p className="text-sm text-muted-foreground">
              {searchQuery || qualityFilter !== 'all'
                ? '没有符合筛选条件的因子'
                : '开始挖掘因子后，结果将显示在这里'}
            </p>
          </CardContent>
        </Card>
      )}

      {/* Factor Detail Modal */}
      {selectedFactor && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-6"
          onClick={() => setSelectedFactor(null)}
        >
          <Card
            className="glass-strong max-w-3xl w-full max-h-[80vh] overflow-y-auto animate-scale-in"
            onClick={(e: React.MouseEvent) => e.stopPropagation()}
          >
            <CardHeader>
              <div className="flex items-start justify-between">
                <div className="flex-1">
                  <CardTitle className="text-xl">
                    {selectedFactor.factorName || selectedFactor.factor_name}
                  </CardTitle>
                  <div className="flex items-center gap-2 mt-2">
                    <Badge className={getQualityBadgeClass(selectedFactor.quality || 'medium')}>
                      {selectedFactor.quality === 'high'
                        ? '高质量'
                        : selectedFactor.quality === 'medium'
                        ? '中等质量'
                        : '低质量'}
                    </Badge>
                    {(selectedFactor.market || selectedFactor.metadata?.market) && (
                      <span className={`inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium ${MARKET_COLORS[selectedFactor.market || selectedFactor.metadata?.market] || 'bg-secondary text-muted-foreground'}`}>
                        {MARKET_LABELS[selectedFactor.market || selectedFactor.metadata?.market] || selectedFactor.market || selectedFactor.metadata?.market}
                      </span>
                    )}
                    {(selectedFactor.universe || selectedFactor.metadata?.universe) && (
                      <span className="inline-flex items-center rounded-md border border-primary/30 bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                        {UNIVERSE_LABELS[
                          (selectedFactor.universe || selectedFactor.metadata?.universe) as keyof typeof UNIVERSE_LABELS
                        ] || selectedFactor.universe || selectedFactor.metadata?.universe}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground mt-2">
                    质量按 |IC| 分级（≥0.05 高 / ≥0.02 中）；IC 为负表示因子与收益负相关，可反向使用。
                  </p>
                </div>
                <Button variant="ghost" onClick={() => setSelectedFactor(null)}>
                  <X className="w-4 h-4" />
                </Button>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              {/* Description */}
              <div>
                <h4 className="text-sm font-medium mb-2">因子描述</h4>
                <p className="text-sm text-muted-foreground">
                  {selectedFactor.factorDescription || selectedFactor.factor_description || '无描述'}
                </p>
              </div>

              {/* Expression */}
              <div>
                <div className="flex items-center justify-between mb-2">
                  <h4 className="text-sm font-medium">因子表达式</h4>
                  <button
                    type="button"
                    onClick={handleCopyExpression}
                    className="inline-flex items-center gap-1 rounded-md border border-border/60 px-2 py-1 text-xs text-muted-foreground hover:text-primary hover:bg-secondary/40 transition-colors"
                    title="拷贝因子表达式"
                  >
                    {copiedExpr ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />}
                    {copiedExpr ? '已拷贝' : '拷贝'}
                  </button>
                </div>
                <div className="rounded-lg bg-secondary/30 p-4">
                  <code className="text-sm font-mono break-all">
                    {selectedFactor.factorExpression || selectedFactor.factor_expression || ''}
                  </code>
                </div>
              </div>

              {/* Formulation */}
              {(selectedFactor.factorFormulation || selectedFactor.factor_formulation) && (
                <div>
                  <h4 className="text-sm font-medium mb-2">数学公式</h4>
                  <div className="rounded-lg bg-secondary/30 p-4">
                    <code className="text-sm font-mono break-all">
                      {selectedFactor.factorFormulation || selectedFactor.factor_formulation}
                    </code>
                  </div>
                </div>
              )}

              {/* Backtest Results */}
              {(selectedFactor.backtestResults || selectedFactor.backtest_results) && (
                <div>
                  <h4 className="text-sm font-medium mb-2">回测指标</h4>
                  <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                    {Object.entries(
                      selectedFactor.backtestResults || selectedFactor.backtest_results || {}
                    ).map(([key, val]) => (
                      <div key={key} className="rounded-lg bg-secondary/30 p-3">
                        <div className="text-xs text-muted-foreground truncate" title={key}>
                          {key}
                        </div>
                        <div className="text-sm font-bold font-mono mt-1">
                          {typeof val === 'number' ? formatNumber(val, 4) : String(val)}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Meta */}
              <div>
                <h4 className="text-sm font-medium mb-2">元信息</h4>
                <div className="space-y-2 text-sm">
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">因子ID:</span>
                    <span className="font-mono">
                      {selectedFactor.factorId || selectedFactor.factor_id || ''}
                    </span>
                  </div>
                  {(selectedFactor.createdAt || selectedFactor.added_at) && (
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">创建时间:</span>
                      <span>
                        {new Date(
                          selectedFactor.createdAt || selectedFactor.added_at
                        ).toLocaleString('zh-CN')}
                      </span>
                    </div>
                  )}
                </div>
              </div>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
};
