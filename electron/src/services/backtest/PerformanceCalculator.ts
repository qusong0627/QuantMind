/**
 * 绩效计算器
 * 计算回测的各项绩效指标
 */

import {
  Trade,
  EquityCurve,
  PerformanceMetrics,
  DrawdownAnalysis,
  DrawdownPeriod
} from '../../types/backtest';

export class PerformanceCalculator {
  /**
   * 计算完整的绩效指标
   */
  calculateMetrics(
    trades: Trade[],
    equityCurve: EquityCurve,
    initialCapital: number,
    startDate: Date,
    endDate: Date
  ): PerformanceMetrics {
    const tradingDays = equityCurve.returns.length;
    const tradingYears = tradingDays / 252;

    // 基本统计
    const completedTrades = trades.filter(t => t.pnl !== undefined);
    const winningTrades = completedTrades.filter(t => t.pnl! > 0);
    const losingTrades = completedTrades.filter(t => t.pnl! < 0);

    // 计算收益
    const finalEquity = equityCurve.values[equityCurve.values.length - 1];
    const totalReturn = (finalEquity - initialCapital) / initialCapital;
    const annualizedReturn = tradingYears > 0
      ? Math.pow(1 + totalReturn, 1 / tradingYears) - 1
      : 0;

    // 计算夏普比率
    const sharpeRatio = this.calculateSharpeRatio(equityCurve.returns);

    // 计算最大回撤
    const drawdownAnalysis = this.calculateDrawdown(equityCurve.values);

    // 胜率和盈亏比
    const winRate = completedTrades.length > 0
      ? winningTrades.length / completedTrades.length
      : 0;

    const averageWin = winningTrades.length > 0
      ? winningTrades.reduce((sum, t) => sum + t.pnl!, 0) / winningTrades.length
      : 0;

    const averageLoss = losingTrades.length > 0
      ? losingTrades.reduce((sum, t) => sum + t.pnl!, 0) / losingTrades.length
      : 0;

    const profitFactor = Math.abs(averageLoss) > 0
      ? averageWin / Math.abs(averageLoss)
      : 0;

    // 计算周期收益
    const dailyReturns = this.calculatePeriodReturns(equityCurve, 'daily');
    const weeklyReturns = this.calculatePeriodReturns(equityCurve, 'weekly');
    const monthlyReturns = this.calculatePeriodReturns(equityCurve, 'monthly');

    return {
      totalReturn,
      annualizedReturn,
      sharpeRatio,
      maxDrawdown: drawdownAnalysis.maxDrawdown,
      maxDrawdownDuration: drawdownAnalysis.maxDrawdownDuration,
      winRate,
      profitFactor,
      averageWin,
      averageLoss,
      totalTrades: completedTrades.length,
      winningTrades: winningTrades.length,
      losingTrades: losingTrades.length,
      dailyReturns,
      weeklyReturns,
      monthlyReturns
    };
  }

  /**
   * 计算夏普比率
   * Sharpe = (R_a - R_f) / σ_a
   * 使用样本标准差 (n-1)，无风险利率默认 2%
   */
  private calculateSharpeRatio(returns: number[]): number {
    if (returns.length < 2) return 0;

    const n = returns.length;
    const avgReturn = returns.reduce((sum, r) => sum + r, 0) / n;
    const variance = returns.reduce((sum, r) => sum + Math.pow(r - avgReturn, 2), 0) / (n - 1);
    const stdDev = Math.sqrt(variance);

    if (stdDev === 0) return 0;

    const annualizedReturn = avgReturn * 252;
    const annualizedVol = stdDev * Math.sqrt(252);
    const riskFreeRate = 0.02;
    return (annualizedReturn - riskFreeRate) / annualizedVol;
  }

  /**
   * 计算回撤
   */
  calculateDrawdown(equityValues: number[]): DrawdownAnalysis {
    const drawdowns: number[] = [];
    const drawdownPeriods: DrawdownPeriod[] = [];

    if (!Array.isArray(equityValues) || equityValues.length === 0) {
      return {
        maxDrawdown: 0,
        maxDrawdownDuration: 0,
        drawdownPeriods: []
      };
    }

    let peak = equityValues[0];
    let peakIndex = 0;
    let inDrawdown = false;
    let drawdownStart = 0;

    equityValues.forEach((value, index) => {
      if (value > peak) {
        if (inDrawdown) {
          const troughSlice = equityValues.slice(drawdownStart, index);
          const trough = troughSlice.length ? Math.min(...troughSlice) : peak;
          const lastDrawdown = drawdowns.length ? drawdowns[drawdowns.length - 1] : 0;
          drawdownPeriods.push({
            start: drawdownStart,
            end: index - 1,
            peak: peak,
            trough,
            drawdown: lastDrawdown,
            duration: index - drawdownStart,
            recovery: index
          });
          inDrawdown = false;
        }
        peak = value;
        peakIndex = index;
      }

      const drawdown = peak > 0 ? (peak - value) / peak : 0;
      drawdowns.push(drawdown);

      if (drawdown > 0 && !inDrawdown) {
        inDrawdown = true;
        drawdownStart = peakIndex;
      }
    });

    if (inDrawdown) {
      const troughSlice = equityValues.slice(drawdownStart);
      const trough = troughSlice.length ? Math.min(...troughSlice) : peak;
      const lastDrawdown = drawdowns.length ? drawdowns[drawdowns.length - 1] : 0;
      drawdownPeriods.push({
        start: drawdownStart,
        end: equityValues.length - 1,
        peak: peak,
        trough,
        drawdown: lastDrawdown,
        duration: equityValues.length - drawdownStart
      });
    }

    const maxDrawdown = drawdowns.length ? Math.max(...drawdowns) : 0;
    const maxDrawdownPeriod = drawdownPeriods.find(d => d.drawdown === maxDrawdown);
    const maxDrawdownDuration = maxDrawdownPeriod?.duration || 0;

    return {
      maxDrawdown,
      maxDrawdownDuration,
      drawdownPeriods
    };
  }

  /**
   * 计算周期收益率
   */
  private calculatePeriodReturns(
    equityCurve: EquityCurve,
    period: 'daily' | 'weekly' | 'monthly'
  ): number[] {
    const periodReturns: number[] = [];
    const values = equityCurve.values;
    const timestamps = equityCurve.timestamps;

    if (values.length < 2) return periodReturns;

    let periodMs: number;
    switch (period) {
      case 'daily':
        periodMs = 24 * 60 * 60 * 1000;
        break;
      case 'weekly':
        periodMs = 7 * 24 * 60 * 60 * 1000;
        break;
      case 'monthly':
        periodMs = 30 * 24 * 60 * 60 * 1000;
        break;
    }

    let periodStart = 0;
    let periodStartValue = values[0];

    for (let i = 1; i < timestamps.length; i++) {
      const timeDiff = timestamps[i] - timestamps[periodStart];

      if (timeDiff >= periodMs) {
        const periodReturn = (values[i] - periodStartValue) / periodStartValue;
        periodReturns.push(periodReturn);

        periodStart = i;
        periodStartValue = values[i];
      }
    }

    return periodReturns;
  }

  /**
   * 构建权益曲线
   */
  buildEquityCurve(
    trades: Trade[],
    initialCapital: number,
    timestamps: number[]
  ): EquityCurve {
    const values: number[] = [initialCapital];
    const returns: number[] = [0];
    const curveTimestamps: number[] = [Array.isArray(timestamps) && timestamps.length ? timestamps[0] : Date.now()];

    let currentEquity = initialCapital;

    // 构建逐笔权益曲线
    for (const trade of trades) {
      if (trade.pnl !== undefined) {
        currentEquity += trade.pnl;

        values.push(currentEquity);
        curveTimestamps.push(typeof trade.timestamp === 'number' ? trade.timestamp : Date.now());

        const periodReturn = values.length > 1
          ? (values[values.length - 1] - values[values.length - 2]) / values[values.length - 2]
          : 0;
        returns.push(periodReturn);
      }
    }

    // 计算回撤
    const drawdownAnalysis = this.calculateDrawdown(values);

    return {
      timestamps: curveTimestamps,
      values,
      drawdowns: values.map((v, i) => {
        const peak = Math.max(...values.slice(0, i + 1));
        return peak > 0 ? (peak - v) / peak : 0;
      }),
      returns
    };
  }

  /**
   * 计算卡玛比率 (Calmar Ratio)
   */
  calculateCalmarRatio(annualizedReturn: number, maxDrawdown: number): number {
    if (maxDrawdown === 0) return 0;
    return annualizedReturn / maxDrawdown;
  }

  /**
   * 计算索提诺比率 (Sortino Ratio)
   * 下行基于 MAR = 年化 2% 无风险利率
   */
  calculateSortinoRatio(returns: number[]): number {
    if (returns.length < 2) return 0;

    const avgReturn = returns.reduce((sum, r) => sum + r, 0) / returns.length;
    const dailyRf = 0.02 / 252;
    const downside = returns.filter(r => r < dailyRf);

    if (downside.length === 0) return 0;

    const downsideVariance = downside.reduce(
      (sum, r) => sum + Math.pow(r - dailyRf, 2),
      0
    ) / (downside.length - 1);

    const downsideDeviation = Math.sqrt(downsideVariance);

    if (downsideDeviation === 0) return 0;

    const annualizedReturn = avgReturn * 252;
    const annualizedDownside = downsideDeviation * Math.sqrt(252);
    return (annualizedReturn - 0.02) / annualizedDownside;
  }

  /**
   * 计算信息比率 (Information Ratio)
   */
  calculateInformationRatio(returns: number[], benchmarkReturns: number[]): number {
    if (returns.length !== benchmarkReturns.length || returns.length < 2) {
      return 0;
    }

    const excessReturns = returns.map((r, i) => r - benchmarkReturns[i]);
    const avgExcessReturn = excessReturns.reduce((sum, r) => sum + r, 0) / excessReturns.length;

    const trackingError = Math.sqrt(
      excessReturns.reduce((sum, r) => sum + Math.pow(r - avgExcessReturn, 2), 0) / excessReturns.length
    );

    if (trackingError === 0) return 0;

    return avgExcessReturn / trackingError * Math.sqrt(252);
  }
}
