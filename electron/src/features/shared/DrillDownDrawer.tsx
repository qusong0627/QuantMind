/**
 * 下钻容器（T-FE-03）：任意数字 → 来源链（条目化 + 原始载荷 + 逐层穿透）。
 *
 * 设计目标（前端设计 §一.4）：界面上的关键数字都能证明"我从哪来"——
 * entries 展示该数字所在块的字段分解与 source；raw 展示原始载荷（可复制核对）。
 * v2（逐层穿透）：条目可携带 ``drill``（下一层）；容器内维护层级栈与面包屑，
 * 支持逐层穿透到最底层载荷（曲线点→快照→订单→成交→信号→机会）。
 *
 * 展示形态（presentation）：
 * - ``drawer``（默认，右侧抽屉）：交由既有页面（个股终端等）沿用；
 * - ``modal``（居中弹窗）：交易台统一交互——点击即中间弹出，条目行式排版，
 *   原始载荷默认折叠（核对时展开）。
 */

import React, { useEffect, useMemo, useState } from 'react';
import { Drawer, Modal } from 'antd';
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
  /** 展示形态：drawer=右侧抽屉（默认）；modal=居中弹窗（交易台） */
  presentation?: 'drawer' | 'modal';
}

export const DrillDownDrawer: React.FC<DrillDownDrawerProps> = ({
  open,
  title,
  subtitle,
  entries,
  raw,
  onClose,
  presentation = 'drawer',
}) => {
  const [copied, setCopied] = useState(false);
  // 层级栈：空 = 根层（props 提供）；元素 = 逐层穿透的下一层
  const [stack, setStack] = useState<DrillLevelSpec[]>([]);
  // 原始载荷默认折叠（核对时展开，减少视觉噪音）
  const [rawOpen, setRawOpen] = useState(false);

  // 关闭即复位；打开/换内容（title 变化）也回到根层——
  // 注意只依赖字符串字段：entries/raw 每次父渲染都是新对象，依赖它们会误清层级栈
  useEffect(() => {
    setStack([]);
    setRawOpen(false);
  }, [open, title, subtitle]);

  // 穿透/返回换层时收起载荷（新层的载荷上下文已变）
  useEffect(() => {
    setRawOpen(false);
  }, [stack.length]);

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

  const content = (
    <div className="space-y-3 max-h-[68vh] overflow-y-auto custom-scrollbar pr-1">
      <header className="pr-8">
        {stack.length > 0 && (
          <button
            type="button"
            onClick={() => backTo(stack.length - 1)}
            className="mb-1.5 inline-flex items-center gap-1 text-[11px] font-medium text-slate-500 hover:text-blue-600 transition-colors"
          >
            <ChevronLeft className="w-3 h-3" />
            返回上一层
          </button>
        )}
        <nav className="flex flex-wrap items-center gap-1 text-[11px] text-slate-400 mb-1">
          <button
            type="button"
            onClick={() => backTo(0)}
            className={`truncate max-w-[220px] ${stack.length ? 'hover:text-blue-600 underline decoration-dotted underline-offset-2' : 'text-slate-500 font-semibold'}`}
            title={title}
          >
            {title}
          </button>
          {stack.map((level, i) => (
            <React.Fragment key={`${level.title}-${i}`}>
              <span className="text-slate-300">/</span>
              <button
                type="button"
                onClick={() => backTo(i + 1)}
                className={`truncate max-w-[220px] ${i === stack.length - 1 ? 'text-slate-500 font-semibold' : 'hover:text-blue-600 underline decoration-dotted underline-offset-2'}`}
                title={level.title}
              >
                {level.title}
              </button>
            </React.Fragment>
          ))}
        </nav>
        <h3 className="text-lg font-bold text-slate-900 tracking-tight">{current.title}</h3>
        {current.subtitle && (
          <p className="text-xs text-slate-500 mt-0.5 leading-5">{current.subtitle}</p>
        )}
      </header>

      <div className="rounded-xl border border-slate-200/80 bg-white divide-y divide-slate-100 overflow-hidden shadow-[0_1px_2px_rgba(15,23,42,0.03)]">
        {current.entries.map((entry, index) => {
          const body = (
            <>
              <div className="flex items-baseline justify-between gap-3">
                <span className="text-[12px] text-slate-500 shrink-0">{entry.label}</span>
                <span className="text-[13px] text-slate-800 font-medium text-right tabular-nums">
                  {entry.value}
                  {entry.drill && (
                    <ChevronRight className="inline w-3.5 h-3.5 ml-1 -mt-0.5 text-slate-300 group-hover:text-blue-500 transition-colors" />
                  )}
                </span>
              </div>
              {entry.source && (
                <div className="text-[10px] text-slate-400 mt-1 font-mono">来源：{entry.source}</div>
              )}
              {entry.hint && (
                <div className="text-[11px] text-slate-500 mt-1 leading-4">{entry.hint}</div>
              )}
            </>
          );
          return entry.drill ? (
            <button
              key={`${entry.label}-${index}`}
              type="button"
              onClick={() => drillInto(entry.drill as DrillLevelSpec)}
              title="逐层穿透：点击进入下一层"
              className="group w-full text-left px-3.5 py-2.5 hover:bg-blue-50/40 transition-colors"
            >
              {body}
            </button>
          ) : (
            <div key={`${entry.label}-${index}`} className="px-3.5 py-2.5">
              {body}
            </div>
          );
        })}
        {current.entries.length === 0 && (
          <p className="text-xs text-slate-400 px-3.5 py-2.5">无可下钻条目</p>
        )}
      </div>

      <div className="rounded-xl border border-slate-200/80 bg-slate-50/50 overflow-hidden">
        <div className="flex items-center justify-between px-3.5 py-2">
          <button
            type="button"
            onClick={() => setRawOpen(!rawOpen)}
            className="inline-flex items-center gap-1 text-xs font-medium text-slate-600 hover:text-slate-900 transition-colors"
            aria-expanded={rawOpen}
          >
            <ChevronRight
              className={`w-3.5 h-3.5 transition-transform ${rawOpen ? 'rotate-90' : ''}`}
            />
            原始载荷（核对用）
          </button>
          <button
            type="button"
            onClick={() => void copyRaw()}
            className="text-[11px] inline-flex items-center gap-1 px-2 py-0.5 rounded-lg border border-slate-200 bg-white hover:bg-slate-50 text-slate-600 transition-colors"
          >
            {copied ? <Check className="w-3 h-3 text-emerald-600" /> : <Copy className="w-3 h-3" />}
            {copied ? '已复制' : '复制 JSON'}
          </button>
        </div>
        {rawOpen && (
          <pre className="text-[10px] leading-4 text-slate-600 bg-white border-t border-slate-100 px-3.5 py-2.5 max-h-[320px] overflow-auto whitespace-pre-wrap break-all font-mono">
            {rawText}
          </pre>
        )}
      </div>
    </div>
  );

  if (presentation === 'modal') {
    return (
      <Modal
        open={open}
        onCancel={onClose}
        footer={null}
        centered
        width={780}
        title={null}
        destroyOnHidden
        styles={{ content: { borderRadius: 20 } }}
      >
        {content}
      </Modal>
    );
  }

  return (
    <Drawer open={open} onClose={onClose} width={520} title={null} destroyOnHidden>
      {content}
    </Drawer>
  );
};
