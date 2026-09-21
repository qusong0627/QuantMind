/**
 * CSV导出服务
 * 支持将各种数据导出为CSV格式
 */

import { disclaimerRows } from '../../utils/exportDisclaimer';

export interface CSVExportOptions {
  filename?: string;
  delimiter?: string;
  includeHeaders?: boolean;
  encoding?: string;
  /** 数据覆盖区间（人读文本）。拿不到就别传 —— 免责段会省略该行，不编造。 */
  dataRange?: string;
  /** 覆盖免责段的「生成时间」，仅供测试固定时钟用。 */
  now?: Date;
}

// 类型定义
export type TradeExport = {
  timestamp: string | number;
  symbol: string;
  side: 'buy' | 'sell' | string;
  price: number;
  size: number;
  commission: number;
  slippage: number;
  pnl?: number;
};

export type OHLCVBar = {
  timestamp: string | number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type BacktestResultExport = {
  metrics: {
    totalReturn: number;
    annualizedReturn: number;
    sharpeRatio: number;
    maxDrawdown: number;
    winRate: number;
    profitFactor: number;
    totalTrades: number;
    winningTrades: number;
    losingTrades: number;
  };
  trades?: TradeExport[];
  equity?: Array<{ date: string; value: number }>;
};

export class CSVExporter {
  private defaultOptions: CSVExportOptions = {
    filename: 'export.csv',
    delimiter: ',',
    includeHeaders: true,
    encoding: 'utf-8'
  };

  /**
   * 导出通用数据
   */
  export<T extends Record<string, unknown>>(
    data: T[],
    options: CSVExportOptions = {}
  ): void {
    const opts = { ...this.defaultOptions, ...options };

    if (data.length === 0) {
      throw new Error('No data to export');
    }

    const csv = this.convertToCSV(data, opts);
    this.downloadCSV(csv, opts.filename!, opts.encoding!);
  }

  /**
   * 导出交易记录
   */
  exportTrades(trades: TradeExport[], filename: string = 'trades.csv'): void {
    const formattedTrades = trades.map(trade => ({
      '时间': new Date(trade.timestamp).toLocaleString(),
      '交易对': trade.symbol,
      '方向': trade.side === 'buy' ? '买入' : '卖出',
      '价格': trade.price.toFixed(2),
      '数量': trade.size,
      '手续费': trade.commission.toFixed(4),
      '滑点': trade.slippage.toFixed(4),
      '盈亏': trade.pnl?.toFixed(2) || '0.00'
    }));

    this.export(formattedTrades, { filename });
  }

  /**
   * 导出K线数据
   */
  exportOHLCV(data: OHLCVBar[], filename: string = 'kline.csv'): void {
    const formattedData = data.map(bar => ({
      '时间': new Date(bar.timestamp).toLocaleString(),
      '开盘': bar.open.toFixed(2),
      '最高': bar.high.toFixed(2),
      '最低': bar.low.toFixed(2),
      '收盘': bar.close.toFixed(2),
      '成交量': bar.volume.toFixed(2)
    }));

    this.export(formattedData, { filename });
  }

  /**
   * 导出回测结果
   */
  exportBacktestResults(result: BacktestResultExport, filename: string = 'backtest_results.csv'): void {
    const { metrics, trades } = result;

    // 导出绩效指标
    const metricsData = [{
      '总收益率': `${(metrics.totalReturn * 100).toFixed(2)}%`,
      '年化收益率': `${(metrics.annualizedReturn * 100).toFixed(2)}%`,
      '夏普比率': metrics.sharpeRatio.toFixed(2),
      '最大回撤': `${(metrics.maxDrawdown * 100).toFixed(2)}%`,
      '胜率': `${(metrics.winRate * 100).toFixed(2)}%`,
      '盈亏比': metrics.profitFactor.toFixed(2),
      '交易次数': metrics.totalTrades,
      '盈利次数': metrics.winningTrades,
      '亏损次数': metrics.losingTrades
    }];

    this.export(metricsData, {
      filename: filename.replace('.csv', '_metrics.csv')
    });

    // 导出交易记录
    if (trades && trades.length > 0) {
      this.exportTrades(trades, filename.replace('.csv', '_trades.csv'));
    }
  }

  /**
   * 导出告警历史
   */
  exportAlertHistory(history: { triggerTime: string | number; alertName: string; value: number; message: string }[], filename: string = 'alert_history.csv'): void {
    const formattedHistory = history.map(item => ({
      '时间': new Date(item.triggerTime).toLocaleString(),
      '告警名称': item.alertName,
      '触发值': item.value.toFixed(2),
      '消息': item.message
    }));

    this.export(formattedHistory, { filename });
  }

  /**
   * 转换为CSV格式
   */
  private convertToCSV<T extends Record<string, unknown>>(
    data: T[],
    options: CSVExportOptions
  ): string {
    const { delimiter, includeHeaders } = options;
    const lines: string[] = [];

    // 添加表头
    if (includeHeaders) {
      const headers = Object.keys(data[0]);
      lines.push(headers.map(h => this.escapeCSV(h)).join(delimiter));
    }

    // 添加数据行
    data.forEach(row => {
      const values = Object.values(row).map(v => this.escapeCSV(String(v)));
      lines.push(values.join(delimiter));
    });

    // 免责段压尾（生成时间 / 数据区间 / 不构成投资建议）。
    // 空行分隔 + 数据行宽度不变：本类此前完全不写免责段，而它导出的是交易记录、
    // K线与回测结果 —— 正是最容易被当成操作依据的三类。措辞取自单源，见
    // `utils/exportDisclaimer`（与后端 `shared/export_disclaimer.py` 同金样）。
    lines.push('');
    disclaimerRows(options).forEach(([label, value]) => {
      lines.push([label, value].map(v => this.escapeCSV(v)).join(delimiter));
    });

    return lines.join('\n');
  }

  /**
   * 转义CSV特殊字符
   */
  private escapeCSV(value: string): string {
    if (value.includes(',') || value.includes('"') || value.includes('\n')) {
      return `"${value.replace(/"/g, '""')}"`;
    }
    return value;
  }

  /**
   * 下载CSV文件
   */
  private downloadCSV(csv: string, filename: string, encoding: string): void {
    // 添加BOM以支持Excel正确显示中文
    const BOM = '\uFEFF';
    const blob = new Blob([BOM + csv], { type: `text/csv;charset=${encoding}` });
    const url = URL.createObjectURL(blob);

    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    // 释放URL对象
    setTimeout(() => URL.revokeObjectURL(url), 100);
  }

  /**
   * 转换为CSV字符串（不下载）
   */
  toCSVString<T extends Record<string, unknown>>(
    data: T[],
    options: CSVExportOptions = {}
  ): string {
    const opts = { ...this.defaultOptions, ...options };
    return this.convertToCSV(data, opts);
  }
}

// 单例
export const csvExporter = new CSVExporter();
