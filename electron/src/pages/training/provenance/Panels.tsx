import React from 'react';
import { Button } from 'antd';
import { ClipboardCopy, Copy, Download, SlidersHorizontal, Terminal } from 'lucide-react';
import { copyText } from './primitives';

export const LogsPanel: React.FC<{ logs?: string }> = ({ logs }) => {
  const text = logs || '';
  const lines = text ? text.split('\n').length : 0;
  return (
    <div className="flex h-full flex-col overflow-hidden rounded-2xl border border-slate-800/80 bg-slate-950">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/10 px-3 py-2">
        <div className="flex items-center gap-2 text-[11px] text-slate-300">
          <Terminal size={13} className="text-emerald-400" />
          <span className="font-black">训练日志</span>
          <span className="font-mono text-[9px] text-slate-500">
            {lines} 行 · {text.length.toLocaleString()} 字符
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <Button
            size="small"
            icon={<Copy size={12} />}
            className="!border-white/10 !bg-white/5 !text-[10px] !font-bold !text-slate-200 hover:!border-white/20 hover:!bg-white/10"
            disabled={!text}
            onClick={() => void copyText(text, '日志已复制')}
          >
            复制
          </Button>
          <Button
            size="small"
            icon={<Download size={12} />}
            className="!border-white/10 !bg-white/5 !text-[10px] !font-bold !text-slate-200 hover:!border-white/20 hover:!bg-white/10"
            disabled={!text}
            onClick={() => {
              const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
              const url = URL.createObjectURL(blob);
              const a = document.createElement('a');
              a.href = url;
              a.download = 'training.log';
              a.click();
              URL.revokeObjectURL(url);
            }}
          >
            下载
          </Button>
        </div>
      </div>
      <pre className="min-h-0 flex-1 overflow-auto custom-scrollbar px-4 py-3 text-[11px] leading-relaxed text-slate-200">
        {text || '该训练没有保存日志。'}
      </pre>
    </div>
  );
};

export const ConfigPanel: React.FC<{
  request: Record<string, unknown> | null;
  onExport: () => void;
}> = ({ request, onExport }) => {
  const rawFeatures = request?.features ?? request?.resolved_features;
  const featureCount = Array.isArray(rawFeatures) ? rawFeatures.length : 0;
  return (
    <div className="flex h-full flex-col overflow-hidden rounded-2xl border border-slate-200/80 bg-white">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-3 py-2">
        <div className="flex items-center gap-2 text-[11px]">
          <SlidersHorizontal size={13} className="text-indigo-500" />
          <span className="font-black text-slate-700">提交配置</span>
          <span className="text-[9px] text-slate-400">
            {request ? `${Object.keys(request).length} 个顶层字段 · ${featureCount} 个特征` : '未保存载荷'}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <Button
            size="small"
            icon={<ClipboardCopy size={12} />}
            className="rounded-lg text-[10px]"
            disabled={!request}
            onClick={() => request && void copyText(JSON.stringify(request, null, 2), '配置 JSON 已复制')}
          >
            复制
          </Button>
          <Button size="small" icon={<Download size={12} />} className="rounded-lg text-[10px]" disabled={!request} onClick={onExport}>
            下载 JSON
          </Button>
        </div>
      </div>
      <pre className="min-h-0 flex-1 overflow-auto custom-scrollbar bg-slate-50/60 px-4 py-3 text-[10px] leading-relaxed text-slate-700">
        {request ? JSON.stringify(request, null, 2) : '该训练没有保存提交载荷。'}
      </pre>
    </div>
  );
};
