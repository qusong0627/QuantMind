/**
 * 自助体检（T-FE-15）：粘贴 / 上传净值曲线（CSV 或回测结果 JSON）→ 九项报告。
 *
 * 纪律（与后端一致）：只读自查——不落评估档案、不参与晋级门禁；
 * 样本不足/口径不符如实报错，不伪造结论。
 */

import React, { useRef, useState } from 'react';
import { FileUp, Stethoscope } from 'lucide-react';
import { message } from 'antd';
import { uploadHealthCheck } from '../../services/evalCenterService';
import { HealthReportView } from '../../../../components/backtestCenter/analysis/HealthReportView';
import type { HealthReport } from '../../../../services/backtestService';

interface UploadState {
  report: HealthReport | null;
  reportText: string;
  points: number;
  disclaimer: string;
  source: string;
}

export const SelfHealthUpload: React.FC = () => {
  const [content, setContent] = useState('');
  const [trials, setTrials] = useState(1);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<UploadState | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const handleFile = async (file: File | null) => {
    if (!file) return;
    try {
      const text = await file.text();
      setContent(text);
      setError('');
    } catch {
      setError('文件读取失败');
    }
  };

  const run = async () => {
    setRunning(true);
    setError('');
    setResult(null);
    try {
      const resp = await uploadHealthCheck(content, trials);
      setResult({
        report: (resp.data.report as unknown as HealthReport) ?? null,
        reportText: resp.data.report_text,
        points: resp.data.points,
        disclaimer: resp.data.disclaimer,
        source: resp.data.source,
      });
      message.success('体检完成');
    } catch (err: unknown) {
      const text = err instanceof Error ? err.message : '体检失败';
      setError(text);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-4 space-y-3">
      <header className="flex items-center gap-2">
        <Stethoscope className="w-4 h-4 text-blue-600" />
        <h3 className="text-sm font-semibold text-slate-800">自助体检（上传净值曲线）</h3>
        <span className="text-[11px] text-slate-400">只读自查 · 不落档案 · 不参与晋级门禁</span>
      </header>

      <div className="flex flex-wrap items-center gap-2 text-xs">
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          className="inline-flex items-center gap-1 rounded-xl border border-gray-200 px-3 py-1.5 hover:bg-gray-50 text-slate-600"
        >
          <FileUp className="w-3.5 h-3.5" />
          选择文件（CSV / 回测结果 JSON）
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".csv,.json,.txt,text/csv,application/json"
          className="hidden"
          onChange={(e) => void handleFile(e.target.files?.[0] ?? null)}
        />
        <label className="inline-flex items-center gap-1 text-slate-500">
          试验次数 N
          <input
            type="number"
            min={1}
            max={10000}
            value={trials}
            onChange={(e) => setTrials(Math.max(1, Number(e.target.value) || 1))}
            className="w-16 rounded-lg border border-gray-200 px-2 py-1 text-xs"
            title="参数扫描次数（DSR 去胀）；无扫描记录填 1"
          />
        </label>
      </div>

      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        placeholder={'或直接粘贴净值序列：\n- CSV：date,close（每行一个交易日，≥30 行）\n- JSON：[100.5, 101.2, ...] 或回测结果文件 {"equity_curve": [{"date","value"}]}'}
        rows={6}
        className="w-full rounded-xl border border-gray-200 p-3 text-xs font-mono text-slate-700 focus:outline-none focus:border-blue-400"
      />

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => void run()}
          disabled={running || !content.trim()}
          className="rounded-xl bg-blue-600 px-4 py-2 text-xs font-bold text-white hover:bg-blue-500 disabled:opacity-50"
        >
          {running ? '体检中…' : '开始体检'}
        </button>
        {error && <span className="text-xs text-rose-600">{error}</span>}
        {result && (
          <span className="text-[11px] text-slate-500">
            {result.points} 个点位 · {result.source}
          </span>
        )}
      </div>

      {result?.report && (
        <div className="space-y-2 pt-1">
          <div className="text-[11px] text-amber-700 bg-amber-50 border border-amber-100 rounded-xl px-3 py-1.5">
            {result.disclaimer}
          </div>
          <HealthReportView report={result.report} />
        </div>
      )}
    </div>
  );
};
