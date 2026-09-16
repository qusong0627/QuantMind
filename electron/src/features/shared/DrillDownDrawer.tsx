/**
 * 下钻抽屉（T-FE-03）：任意数字 → 来源链（条目化 + 原始载荷 + 逐层穿透）。
 *
 * 设计目标（前端设计 §一.4）：界面上的关键数字都能证明"我从哪来"——
 * entries 展示该数字所在块的字段分解与 source；raw 展示原始载荷（可复制核对）。
 * v2（逐层穿透）：条目可携带 ``drill``（下一层）；抽屉内维护层级栈与面包屑，
 * 支持逐层穿透到最底层载荷（曲线点→快照→订单→成交→信号→机会）。
 */

import React, { useEffect, useMemo, useState } from 'react';
import { Drawer } from 'antd';
import { ChevronLeft, ChevronRight, Copy, Check } from 'lucide-react';

export interface DrillEntry {
  label: string;
  value: React.ReactNode;
  source?: string;
  hint?: string;
  /** 逐层穿透：该条目的下一层（点击进入） */
  drill?: DrillLevelSpec;
}

export interface DrillLevelSpec {
  title: string;
  subtitle?: string;
  entries: DrillEntry[];
  raw?: unknown;
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
  // 层级栈：空 = 根层（props 提供）；元素 = 逐层穿透的下一层
  const [stack, setStack] = useState<DrillLevelSpec[]>([]);

  // 关闭即复位；打开/换内容（title 变化）也回到根层——
  // 注意只依赖字符串字段：entries/raw 每次父渲染都是新对象，依赖它们会误清层级栈
  useEffect(() => {
    setStack([]);
  }, [open, title, subtitle]);

  const current: DrillLevelSpec = stack.length
    ? stack[stack.length - 1]
    : { title, subtitle, entries, raw };

  const rawText = useMemo(() => {
    try {
      return JSON.stringify(current.raw ?? {}, null, 2);
    } catch {
      return String(current.raw);
    }
  }, [current.raw]);

  const copyRaw = async () => {
    try {
      await navigator.clipboard.writeText(rawText);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // 剪贴板不可用（权限/非安全上下文）：不打扰，用户可手动选中
    }
  };

  const drillInto = (level: DrillLevelSpec) => {
    setStack([...stack, level]); // 传值更新（repo 约定：函数式 setter 在 tsc 下类型报错）
  };
  const backTo = (depth: number) => {
    // depth=0 → 根层；depth=n → 截断到第 n 层
    setStack(stack.slice(0, depth));
  };

  return (
    <Drawer open={open} onClose={onClose} width={520} title={null} destroyOnHidden>
      <div className="space-y-3">
        <header>
          {stack.length > 0 && (
            <button
              type="button"
              onClick={() => backTo(stack.length - 1)}
              className="mb-1.5 inline-flex items-center gap-1 text-[11px] text-slate-500 hover:text-blue-600"
            >
              <ChevronLeft className="w-3 h-3" />
              返回上一层
            </button>
          )}
          <nav className="flex flex-wrap items-center gap-1 text-[11px] text-slate-400 mb-1">
            <button
              type="button"
              onClick={() => backTo(0)}
              className={`truncate max-w-[180px] ${stack.length ? 'hover:text-blue-600 underline decoration-dotted' : 'text-slate-600 font-semibold'}`}
              title={title}
            >
              {title}
            </button>
            {stack.map((level, i) => (
              <React.Fragment key={`${level.title}-${i}`}>
                <span>/</span>
                <button
                  type="button"
                  onClick={() => backTo(i + 1)}
                  className={`truncate max-w-[180px] ${i === stack.length - 1 ? 'text-slate-600 font-semibold' : 'hover:text-blue-600 underline decoration-dotted'}`}
                  title={level.title}
                >
                  {level.title}
                </button>
              </React.Fragment>
            ))}
          </nav>
          <h3 className="text-base font-bold text-slate-800">{current.title}</h3>
          {current.subtitle && <p className="text-xs text-slate-500 mt-0.5">{current.subtitle}</p>}
        </header>

        <div className="space-y-1.5">
          {current.entries.map((entry, index) => {
            const body = (
              <>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs text-slate-500">{entry.label}</span>
                  <span className="text-sm text-slate-800 font-medium text-right">
                    {entry.value}
                    {entry.drill && <ChevronRight className="inline w-3 h-3 ml-1 text-slate-400" />}
                  </span>
                </div>
                {entry.source && (
                  <div className="text-[10px] text-slate-400 mt-1">来源：{entry.source}</div>
                )}
                {entry.hint && <div className="text-[11px] text-slate-500 mt-1">{entry.hint}</div>}
              </>
            );
            return entry.drill ? (
              <button
                key={`${entry.label}-${index}`}
                type="button"
                onClick={() => drillInto(entry.drill as DrillLevelSpec)}
                title="逐层穿透：点击进入下一层"
                className="w-full text-left rounded-xl border border-gray-100 p-2.5 hover:border-blue-200 hover:bg-blue-50/40 transition-colors"
              >
                {body}
              </button>
            ) : (
              <div key={`${entry.label}-${index}`} className="rounded-xl border border-gray-100 p-2.5">
                {body}
              </div>
            );
          })}
          {current.entries.length === 0 && <p className="text-xs text-slate-400">无可下钻条目</p>}
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
