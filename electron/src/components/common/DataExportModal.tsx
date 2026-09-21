import React, { useState, useCallback } from 'react';
import { motion } from 'framer-motion';
import { Download, FileText, Table, BarChart3, Calendar, Filter } from 'lucide-react';
import { Buffer } from 'buffer';

import { buildCsvText } from '../../utils/csvExport';
import { dataRangeRow, formatExportTime } from '../../utils/exportDisclaimer';

interface ExportOptions {
  format: 'csv' | 'xlsx' | 'pdf' | 'json';
  dateRange: {
    start: Date;
    end: Date;
  };
  includeCharts: boolean;
  includeTrades: boolean;
  includeStrategies: boolean;
  includeMarketData: boolean;
}

interface DataExportModalProps {
  isOpen: boolean;
  onClose: () => void;
  onExport: (options: ExportOptions) => Promise<void>;
}

export const DataExportModal: React.FC<DataExportModalProps> = ({
  isOpen,
  onClose,
  onExport
}) => {
  const [exportOptions, setExportOptions] = useState<ExportOptions>({
    format: 'xlsx',
    dateRange: {
      start: new Date(Date.now() - 30 * 24 * 60 * 60 * 1000), // 30天前
      end: new Date()
    },
    includeCharts: true,
    includeTrades: true,
    includeStrategies: true,
    includeMarketData: false
  });
  const [isExporting, setIsExporting] = useState(false);

  const handleExport = useCallback(async () => {
    setIsExporting(true);
    try {
      await onExport(exportOptions);
      onClose();
    } catch (error) {
      console.error('导出失败:', error);
    } finally {
      setIsExporting(false);
    }
  }, [exportOptions, onExport, onClose]);

  const formatOptions = [
    { value: 'xlsx', label: 'Excel (.xlsx)', icon: Table },
    { value: 'csv', label: 'CSV文件', icon: FileText },
    { value: 'pdf', label: 'PDF报告', icon: FileText },
    { value: 'json', label: 'JSON数据', icon: BarChart3 }
  ];

  if (!isOpen) return null;

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center"
      onClick={onClose}
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.9, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.9, y: 20 }}
        className="bg-white rounded-xl shadow-2xl w-full max-w-md mx-4"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="p-6 border-b border-gray-200">
          <div className="flex items-center space-x-3">
            <div className="p-2 bg-blue-100 rounded-lg">
              <Download className="w-6 h-6 text-blue-600" />
            </div>
            <div>
              <h3 className="text-xl font-semibold text-gray-900">数据导出</h3>
              <p className="text-sm text-gray-500">选择导出格式和数据范围</p>
            </div>
          </div>
        </div>

        {/* 内容区域 */}
        <div className="p-6 space-y-6">
          {/* 导出格式 */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-3">
              导出格式
            </label>
            <div className="grid grid-cols-2 gap-2">
              {formatOptions.map((option) => {
                const IconComponent = option.icon;
                return (
                  <button
                    key={option.value}
                    onClick={() => (setExportOptions as any)(prev => ({ ...prev, format: option.value as any }))}
                    className={`p-3 border rounded-lg flex items-center space-x-2 transition-all ${
                      exportOptions.format === option.value
                        ? 'border-blue-500 bg-blue-50 text-blue-700'
                        : 'border-gray-200 hover:border-gray-300'
                    }`}
                  >
                    <IconComponent className="w-4 h-4" />
                    <span className="text-sm font-medium">{option.label}</span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* 日期范围 */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-3">
              <Calendar className="w-4 h-4 inline mr-1" />
              时间范围
            </label>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-gray-500 mb-1">开始日期</label>
                <input
                  type="date"
                  value={exportOptions.dateRange.start.toISOString().split('T')[0]}
                  onChange={(e) => (setExportOptions as any)(prev => ({
                    ...prev,
                    dateRange: { ...prev.dateRange, start: new Date(e.target.value) }
                  }))}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">结束日期</label>
                <input
                  type="date"
                  value={exportOptions.dateRange.end.toISOString().split('T')[0]}
                  onChange={(e) => (setExportOptions as any)(prev => ({
                    ...prev,
                    dateRange: { ...prev.dateRange, end: new Date(e.target.value) }
                  }))}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                />
              </div>
            </div>
          </div>

          {/* 包含数据 */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-3">
              <Filter className="w-4 h-4 inline mr-1" />
              包含数据
            </label>
            <div className="space-y-2">
              {[
                { key: 'includeTrades', label: '交易记录' },
                { key: 'includeStrategies', label: '策略数据' },
                { key: 'includeCharts', label: '图表数据' },
                { key: 'includeMarketData', label: '市场数据' }
              ].map((item) => (
                <label key={item.key} className="flex items-center space-x-3 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={exportOptions[item.key as keyof ExportOptions] as boolean}
                    onChange={(e) => (setExportOptions as any)(prev => ({
                      ...prev,
                      [item.key]: e.target.checked
                    }))}
                    className="w-4 h-4 text-blue-600 border-gray-300 rounded focus:ring-blue-500"
                  />
                  <span className="text-sm text-gray-700">{item.label}</span>
                </label>
              ))}
            </div>
          </div>
        </div>

        {/* 底部按钮 */}
        <div className="p-6 border-t border-gray-200 flex space-x-3">
          <button
            onClick={onClose}
            disabled={isExporting}
            className="flex-1 px-4 py-2 border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 transition-colors disabled:opacity-50"
          >
            取消
          </button>
          <button
            onClick={handleExport}
            disabled={isExporting}
            className="flex-1 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 flex items-center justify-center space-x-2"
          >
            {isExporting ? (
              <>
                <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                <span>导出中...</span>
              </>
            ) : (
              <>
                <Download className="w-4 h-4" />
                <span>开始导出</span>
              </>
            )}
          </button>
        </div>
      </motion.div>
    </motion.div>
  );
};

// 数据导出服务
export class DataExportService {
  static async exportData(options: ExportOptions): Promise<void> {
    try {
      // 模拟数据收集
      const data = await this.collectData(options);

      // 根据格式进行导出
      switch (options.format) {
        case 'xlsx':
          await this.exportToExcel(data, options);
          break;
        case 'csv':
          await this.exportToCSV(data, options);
          break;
        case 'pdf':
          await this.exportToPDF(data, options);
          break;
        case 'json':
          await this.exportToJSON(data, options);
          break;
      }
    } catch (error) {
      console.error('导出失败:', error);
      throw error;
    }
  }

  private static async collectData(options: ExportOptions): Promise<any> {
    const data: any = {};

    if (options.includeTrades) {
      data.trades = await this.getTradeData(options.dateRange);
    }

    if (options.includeStrategies) {
      data.strategies = await this.getStrategyData(options.dateRange);
    }

    if (options.includeCharts) {
      data.charts = await this.getChartData(options.dateRange);
    }

    if (options.includeMarketData) {
      data.marketData = await this.getMarketData(options.dateRange);
    }

    return data;
  }

  private static async getTradeData(dateRange: { start: Date; end: Date }) {
    // 模拟交易数据
    return [
      { date: '2024-01-01', symbol: 'AAPL', action: 'buy', quantity: 100, price: 150.00 },
      { date: '2024-01-02', symbol: 'AAPL', action: 'sell', quantity: 100, price: 152.00 }
    ];
  }

  private static async getStrategyData(dateRange: { start: Date; end: Date }) {
    // 模拟策略数据
    return [
      { name: '均线策略', performance: '+12.5%', trades: 25, winRate: '68%' },
      { name: '动量策略', performance: '+8.3%', trades: 18, winRate: '72%' }
    ];
  }

  private static async getChartData(dateRange: { start: Date; end: Date }) {
    // 模拟图表数据
    return [
      { timestamp: '2024-01-01', profitCurve: 100, risk: 20 },
      { timestamp: '2024-01-02', profitCurve: 102, risk: 22 }
    ];
  }

  private static async getMarketData(dateRange: { start: Date; end: Date }) {
    // 模拟市场数据
    return [
      { symbol: 'AAPL', price: 150.00, change: '+1.2%' },
      { symbol: 'GOOGL', price: 2800.00, change: '-0.5%' }
    ];
  }

  private static async exportToExcel(data: any, options: ExportOptions) {
    // 在实际项目中，这里会使用XLSX库
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
    this.downloadFile(blob, `quantmind_export_${Date.now()}.xlsx`);
  }

  private static async exportToCSV(data: any, options: ExportOptions) {
    const csv = this.convertToCSV(data, options);
    const blob = new Blob([csv], { type: 'text/csv' });
    this.downloadFile(blob, `quantmind_export_${Date.now()}.csv`);
  }

  private static async exportToPDF(data: any, options: ExportOptions) {
    // 在实际项目中，这里会使用PDF库
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/pdf' });
    this.downloadFile(blob, `quantmind_export_${Date.now()}.pdf`);
  }

  private static async exportToJSON(data: any, options: ExportOptions) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    this.downloadFile(blob, `quantmind_export_${Date.now()}.json`);
  }

  private static convertToCSV(data: any, options: ExportOptions): string {
    const items = data.trades || [];
    if (items.length === 0) return '';

    const headers = Object.keys(items[0]);
    const rows = items.map((item: any) => headers.map((header) => item[header]));

    // 走共享实现（utils/csvExport）：转义、BOM、免责段一次到位。
    // 此前这里手拼 `join(',')` —— 值里带逗号就破列（代码/名称列很常见），
    // 且完全没有免责段。区间取弹窗里用户选的那个，它就是本次导出的覆盖面；
    // `formatExportTime` 的补零保证切片出来的日期段是 YYYY-MM-DD。
    const range = options?.dateRange;
    const day = (d: Date) => formatExportTime(d).slice(0, 10);

    return buildCsvText(headers, rows, {
      dataRange: range ? dataRangeRow(day(range.start), day(range.end)) : undefined,
    });
  }

  private static async downloadFile(blob: Blob, filename: string) {
    // 使用 Electron 的原生文件保存对话框
    if (window.electronAPI) {
      try {
        const arrayBuffer = await blob.arrayBuffer();
        const buffer = Buffer.from(arrayBuffer);

        const result = await window.electronAPI.exportSaveFile({
          data: buffer,
          filename: filename,
          fileType: filename.split('.').pop() || 'txt'
        });

        if (result.success) {
          console.log('文件保存成功:', result.path);
        } else if (!result.canceled) {
          console.error('文件保存失败:', result.error);
        }
      } catch (error) {
        console.error('Electron 文件保存失败:', error);
        // 降级到浏览器下载
        this.fallbackDownload(blob, filename);
      }
    } else {
      // 浏览器环境使用传统下载方式
      this.fallbackDownload(blob, filename);
    }
  }

  private static fallbackDownload(blob: Blob, filename: string) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }
}
