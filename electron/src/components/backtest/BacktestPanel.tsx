/**
 * 回测面板主组件
 * 整合策略编辑、回测执行和结果展示
 */

import React, { useState } from 'react';
import type { Strategy, BacktestConfig, BacktestResult, OHLCV } from '../../types/backtest';
import { BacktestEngine } from '../../services/backtest';
import { StrategyEditor } from './StrategyEditor';
import { EquityCurveChart } from './EquityCurve';
import { TradeList } from './TradeList';
import { PerformanceMetricsPanel } from './PerformanceMetrics';

export const BacktestPanel: React.FC = () => {
  const [strategy, setStrategy] = useState<Strategy | undefined>();
  const [config, setConfig] = useState<BacktestConfig>({
    symbol: '000001.SZ',
    startDate: '2026-01-02',
    endDate: '2026-12-31',
    initialCapital: 100000,
    commission: 0.001,
    slippage: 0.001,
    leverage: 1,
    riskPerTrade: 0.02
  });
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeView, setActiveView] = useState<'editor' | 'results'>('editor');

  // 处理回测执行
  const handleRunBacktest = async (strategyToTest: Strategy) => {
    setLoading(true);
    setError(null);

    try {
      if (!config.symbol) {
        throw new Error('请先填写股票代码');
      }

      // 获取真实行情数据
      const { backtestService } = await import('../../services/backtestService');
      const marketData = await backtestService.getMarketData(
        config.symbol,
        config.startDate,
        config.endDate
      );

      if (!marketData.data || marketData.data.length === 0) {
        throw new Error('未获取到有效行情数据，请检查日期范围或股票代码');
      }

      const bars: OHLCV[] = marketData.data.map((item) => ({
        date: item.date,
        timestamp: new Date(item.date).getTime(),
        open: item.open,
        high: item.high,
        low: item.low,
        close: item.close,
        volume: item.volume,
      }));
      // 执行回测
      const engine = new BacktestEngine();
      const backtestResult = await engine.run(strategyToTest, config, bars);

      setResult(backtestResult);
      setActiveView('results');
    } catch (err) {
      setError(err instanceof Error ? err.message : '回测执行失败');
      console.error('Backtest error:', err);
    } finally {
      setLoading(false);
    }
  };

  // 处理配置更新
  const handleConfigChange = (field: keyof BacktestConfig, value: any) => {
    setConfig({ ...config, [field]: value });
  };

  return (
    <div className="backtest-panel">
      {/* 顶部导航 */}
      <div className="panel-nav">
        <button
          className={`nav-btn ${activeView === 'editor' ? 'active' : ''}`}
          onClick={() => setActiveView('editor')}
        >
          策略编辑
        </button>
        <button
          className={`nav-btn ${activeView === 'results' ? 'active' : ''}`}
          onClick={() => setActiveView('results')}
          disabled={!result}
        >
          回测结果
        </button>
      </div>

      {/* 配置面板 */}
      <div className="config-panel">
        <h4>回测配置</h4>
        <div className="config-grid">
          <div className="config-item">
            <label>品种代码</label>
            <input
              type="text"
              value={config.symbol}
              onChange={e => handleConfigChange('symbol', e.target.value)}
            />
          </div>
          <div className="config-item">
            <label>开始日期</label>
            <input
              type="date"
              value={config.startDate ? new Date(config.startDate).toISOString().split('T')[0] : ''}
              onChange={e => handleConfigChange('startDate', e.target.value)}
            />
          </div>
          <div className="config-item">
            <label>结束日期</label>
            <input
              type="date"
              value={config.endDate ? new Date(config.endDate).toISOString().split('T')[0] : ''}
              onChange={e => handleConfigChange('endDate', e.target.value)}
            />
          </div>
          <div className="config-item">
            <label>初始资金</label>
            <input
              type="number"
              value={config.initialCapital}
              onChange={e => handleConfigChange('initialCapital', Number(e.target.value))}
            />
          </div>
          <div className="config-item">
            <label>手续费率</label>
            <input
              type="number"
              step="0.0001"
              value={config.commission}
              onChange={e => handleConfigChange('commission', Number(e.target.value))}
            />
          </div>
          <div className="config-item">
            <label>滑点率</label>
            <input
              type="number"
              step="0.0001"
              value={config.slippage}
              onChange={e => handleConfigChange('slippage', Number(e.target.value))}
            />
          </div>
        </div>
      </div>

      {/* 内容区域 */}
      <div className="panel-content">
        {activeView === 'editor' && (
          <StrategyEditor
            strategy={strategy}
            onSave={setStrategy}
            onTest={handleRunBacktest}
          />
        )}

        {activeView === 'results' && result && (
          <div className="results-container">
            {/* 权益曲线 */}
            <EquityCurveChart
              equityCurve={result.equity}
              initialCapital={config.initialCapital}
            />

            {/* 绩效指标 */}
            <PerformanceMetricsPanel metrics={result.metrics} />

            {/* 交易记录 */}
            <TradeList trades={result.trades} />

            {/* 执行信息 */}
            <div className="execution-info">
              <h4>执行信息</h4>
              <div className="info-grid">
                <div className="info-item">
                  <span className="info-label">执行时间:</span>
                  <span className="info-value">{result.executionTime}ms</span>
                </div>
                <div className="info-item">
                  <span className="info-label">开始时间:</span>
                  <span className="info-value">
                    {new Date(result.startTime).toLocaleString()}
                  </span>
                </div>
                <div className="info-item">
                  <span className="info-label">结束时间:</span>
                  <span className="info-value">
                    {new Date(result.endTime).toLocaleString()}
                  </span>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* 加载状态 */}
        {loading && (
          <div className="loading-overlay">
            <div className="loading-spinner"></div>
            <div className="loading-text">正在执行回测...</div>
          </div>
        )}

        {/* 错误信息 */}
        {error && (
          <div className="error-message">
            <div className="error-icon">❌</div>
            <div className="error-text">{error}</div>
          </div>
        )}
      </div>

      <style>{`
        .backtest-panel {
          display: flex;
          flex-direction: column;
          height: 100%;
          background: #1e1e1e;
        }

        .panel-nav {
          display: flex;
          background: #252526;
          border-bottom: 1px solid #3e3e42;
        }

        .nav-btn {
          padding: 14px 28px;
          background: transparent;
          border: none;
          color: #969696;
          cursor: pointer;
          border-bottom: 2px solid transparent;
          font-size: 14px;
          font-weight: 500;
          transition: all 0.2s;
        }

        .nav-btn:hover:not(:disabled) {
          color: #d4d4d4;
        }

        .nav-btn.active {
          color: #d4d4d4;
          border-bottom-color: #0e639c;
        }

        .nav-btn:disabled {
          opacity: 0.5;
          cursor: not-allowed;
        }

        .config-panel {
          background: #252526;
          padding: 16px;
          border-bottom: 1px solid #3e3e42;
        }

        .config-panel h4 {
          margin: 0 0 12px 0;
          color: #d4d4d4;
          font-size: 14px;
          font-weight: 600;
        }

        .config-grid {
          display: grid;
          grid-template-columns: repeat(6, 1fr);
          gap: 12px;
        }

        .config-item {
          display: flex;
          flex-direction: column;
          gap: 6px;
        }

        .config-item label {
          font-size: 12px;
          color: #969696;
        }

        .config-item input {
          background: #3c3c3c;
          border: 1px solid #555;
          color: #d4d4d4;
          padding: 6px 10px;
          border-radius: 16px;
          font-size: 13px;
        }

        .panel-content {
          flex: 1;
          overflow: auto;
          position: relative;
        }

        .results-container {
          padding: 16px;
        }

        .execution-info {
          background: #252526;
          border-radius: 16px;
          padding: 16px;
        }

        .execution-info h4 {
          margin: 0 0 12px 0;
          color: #d4d4d4;
          font-size: 16px;
        }

        .info-grid {
          display: grid;
          grid-template-columns: repeat(3, 1fr);
          gap: 16px;
        }

        .info-item {
          display: flex;
          flex-direction: column;
          gap: 6px;
        }

        .info-label {
          font-size: 12px;
          color: #969696;
        }

        .info-value {
          font-size: 14px;
          color: #d4d4d4;
          font-family: var(--font-mono);
        }

        .loading-overlay {
          position: absolute;
          top: 0;
          left: 0;
          right: 0;
          bottom: 0;
          background: rgba(0, 0, 0, 0.8);
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          gap: 20px;
          z-index: 1000;
        }

        .loading-spinner {
          width: 50px;
          height: 50px;
          border: 4px solid #3e3e42;
          border-top-color: #0e639c;
          border-radius: 50%;
          animation: spin 1s linear infinite;
        }

        @keyframes spin {
          to { transform: rotate(360deg); }
        }

        .loading-text {
          color: #d4d4d4;
          font-size: 16px;
        }

        .error-message {
          position: absolute;
          top: 50%;
          left: 50%;
          transform: translate(-50%, -50%);
          background: #3e1e1e;
          border: 2px solid #f66151;
          border-radius: 16px;
          padding: 24px;
          display: flex;
          flex-direction: column;
          align-items: center;
          gap: 12px;
          min-width: 300px;
        }

        .error-icon {
          font-size: 48px;
        }

        .error-text {
          color: #f66151;
          font-size: 14px;
          text-align: center;
        }

        @media (max-width: 1400px) {
          .config-grid {
            grid-template-columns: repeat(3, 1fr);
          }
        }

        @media (max-width: 768px) {
          .config-grid {
            grid-template-columns: repeat(2, 1fr);
          }
        }
      `}</style>
    </div>
  );
};
