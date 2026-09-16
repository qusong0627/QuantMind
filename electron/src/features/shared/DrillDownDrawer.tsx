/**
 * 下钻抽屉（T-FE-03）：任意数字 → 来源链（条目化 + 原始载荷）。
 *
 * 设计目标（前端设计 §一.4）：界面上的关键数字都能证明"我从哪来"——
 * entries 展示该数字所在块的字段分解与 source；raw 展示原始载荷（可复制核对）。
 * v1 覆盖块级下钻；逐层穿透（曲线点→快照→订单→成交→信号→机会）随各页面接入推进。
 */

import React, { useMemo, useState } from 'react';
import { Drawer } from 'antd';
import { Copy, Check } from 'lucide-react';

export interface DrillEntry {
  label: string;
  value: React.ReactNode;
  source?: string;
  hint?: string;
}

interface DrillDownDrawerProps {
  open: boolean;
  title: string;
  subtitle?: string;
  entries: DrillEntry[];
  raw?: unknown;
  onClose: () => void;
}

export const DrillDownDrawer: React.FC<DrillDownDrawerProps> = ({
  open,
  title,
  subtitle,
  entries,
  raw,
  onClose,
}) => {
  const [copied, setCopied] = useState(false);
  const rawText = useMemo(() => {
    try {
      return JSON.stringify(raw ?? {}, null, 2);
    } catch {
      return String(raw);
    }
  }, [raw]);

  const copyRaw = async () => {
    try {
      await navigator.clipboard.writeText(rawText);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // 剪贴板不可用（权限/非安全上下文）：不打扰，用户可手动选中
    }
  };

  return (
    <Drawer open={open} onClose={onClose} width={480} title={null} destroyOnHidden>
      <div className="space-y-3">
        <header>
          <h3 className="text-base font-bold text-slate-800">{title}</h3>
          {subtitle && <p className="text-xs text-slate-500 mt-0.5">{subtitle}</p>}
        </header>

        <div className="space-y-1.5">
          {entries.map((entry, index) => (
            <div key={`${entry.label}-${index}`} className="rounded-xl border border-gray-100 p-2.5">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-slate-500">{entry.label}</span>
                <span className="text-sm text-slate-800 font-medium text-right">{entry.value}</span>
              </div>
              {entry.source && (
                <div className="text-[10px] text-slate-400 mt-1">来源：{entry.source}</div>
              )}
              {entry.hint && <div className="text-[11px] text-slate-500 mt-1">{entry.hint}</div>}
            </div>
          ))}
          {entries.length === 0 && <p className="text-xs text-slate-400">无可下钻条目</p>}
        </div>

        <div className="rounded-xl border border-gray-100 p-2.5">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-xs font-semibold text-slate-700">原始载荷（核对用）</span>
            <button
              type="button"
              onClick={() => void copyRaw()}
              className="text-[11px] inline-flex items-center gap-1 px-2 py-0.5 rounded-lg border border-gray-200 hover:bg-gray-50 text-slate-600"
            >
              {copied ? <Check className="w-3 h-3 text-emerald-600" /> : <Copy className="w-3 h-3" />}
              {copied ? '已复制' : '复制 JSON'}
            </button>
          </div>
          <pre className="text-[10px] leading-4 text-slate-600 bg-slate-50 rounded-lg p-2 max-h-[320px] overflow-auto whitespace-pre-wrap break-all">
            {rawText}
          </pre>
        </div>
      </div>
    </Drawer>
  );
};
