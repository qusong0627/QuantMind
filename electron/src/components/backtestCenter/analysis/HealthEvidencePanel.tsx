/**
 * 回测体检面板（T-P4-06 证据卡渲染）——加载 result.health 后交由统一视图渲染。
 * 报告缺失时展示"待生成"说明——不伪造数字（缺省如实展示）。
 */

import React, { useEffect, useState } from 'react';
import { HelpCircle, RefreshCw } from 'lucide-react';
import type { HealthReport } from '../../../services/backtestService';
import { HealthReportView } from './HealthReportView';

interface HealthEvidencePanelProps {
  backtestId: string;
}

export const HealthEvidencePanel: React.FC<HealthEvidencePanelProps> = ({ backtestId }) => {
  const [loading, setLoading] = useState(false);
  const [report, setReport] = useState<HealthReport | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (backtestId) {
      void loadReport();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backtestId]);

  const loadReport = async () => {
    setLoading(true);
    setError('');
    try {
      const { backtestService } = await import('../../../services/backtestService');
      const result = await backtestService.getResult(backtestId, true);
      setReport((result?.health as HealthReport | null) ?? null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '加载体检报告失败');
      setReport(null);
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <RefreshCw className="w-8 h-8 text-blue-500 animate-spin mx-auto mb-2" />
          <p className="text-sm text-gray-600">正在读取体检报告...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-amber-50 border border-amber-200 rounded-2xl p-4 text-sm text-amber-800">
        {error}
      </div>
    );
  }

  if (!report) {
    return (
      <div className="bg-gray-50 rounded-2xl border border-gray-200 p-8 text-center">
        <HelpCircle className="w-8 h-8 text-gray-300 mx-auto mb-2" />
        <p className="text-sm text-gray-600">该回测暂无体检报告</p>
        <p className="text-xs text-gray-400 mt-1">
          体检在回测完成后自动生成（九项统计检验）；净值曲线不足 30 个点时不出报告（不产垃圾结论）。
        </p>
      </div>
    );
  }

  return <HealthReportView report={report} />;
};
