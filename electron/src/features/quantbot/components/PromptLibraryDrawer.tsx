/**
 * 提示词库抽屉（QuantBot 顶栏入口）— 意图示例（34 条）+ 技能模板（25 条）合并为一个库。
 * 双栏版式对齐技能中心 PromptsLibrary：左 = 搜索 + 分组列表（示例区块 / 模板区块），
 * 右 = 详情卡片 + 全文 + 复制。列表行右侧悬浮复制按钮支持一键复制。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Drawer, message } from 'antd';
import {
  BookMarked,
  ChevronRight,
  ClipboardCheck,
  Copy,
  FileText,
  Lightbulb,
  Search,
  Sparkles,
  X,
} from 'lucide-react';
import {
  ALL_PROMPTS,
  EXAMPLE_CATEGORY_ORDER,
  PROMPT_LIBRARY_TOTAL,
  TEMPLATE_CATEGORY_ORDER,
  filterPrompts,
  groupByCategory,
  type LibPrompt,
} from './promptLibraryModel';
import { copyText } from '../utils/clipboard';

interface CategoryStyle {
  dot: string;
  chipBg: string;
  chipText: string;
  chipBorder: string;
  accent: string;
}

/** 示例意图五类配色（与模板分类同一视觉语言） */
const INTENT_STYLE: Record<string, CategoryStyle> = {
  写策略: { dot: '#7c3aed', chipBg: '#f5f3ff', chipText: '#6d28d9', chipBorder: '#ede9fe', accent: '#8b5cf6' },
  选股筛选: { dot: '#2563eb', chipBg: '#eff6ff', chipText: '#1d4ed8', chipBorder: '#dbeafe', accent: '#3b82f6' },
  分析问答: { dot: '#0e7490', chipBg: '#ecfeff', chipText: '#0e7490', chipBorder: '#cffafe', accent: '#06b6d4' },
  数据运维: { dot: '#16a34a', chipBg: '#f0fdf4', chipText: '#15803d', chipBorder: '#dcfce7', accent: '#22c55e' },
  操作帮助: { dot: '#d97706', chipBg: '#fffbeb', chipText: '#b45309', chipBorder: '#fef3c7', accent: '#f59e0b' },
};

/** 模板分类配色（与技能中心 PromptsLibrary 保持一致） */
const TEMPLATE_STYLE: Record<string, CategoryStyle> = {
  研究分析: { dot: '#2563eb', chipBg: '#eff6ff', chipText: '#1d4ed8', chipBorder: '#dbeafe', accent: '#3b82f6' },
  '策略·因子·模型·回测': { dot: '#7c3aed', chipBg: '#f5f3ff', chipText: '#6d28d9', chipBorder: '#ede9fe', accent: '#8b5cf6' },
  交易: { dot: '#ea580c', chipBg: '#fff7ed', chipText: '#c2410c', chipBorder: '#ffedd5', accent: '#f97316' },
  平台运营: { dot: '#16a34a', chipBg: '#f0fdf4', chipText: '#15803d', chipBorder: '#dcfce7', accent: '#22c55e' },
  环境初始化: { dot: '#0e7490', chipBg: '#ecfeff', chipText: '#0e7490', chipBorder: '#cffafe', accent: '#06b6d4' },
};

const DEFAULT_STYLE: CategoryStyle = {
  dot: '#64748b',
  chipBg: '#f1f5f9',
  chipText: '#475569',
  chipBorder: '#e2e8f0',
  accent: '#94a3b8',
};

interface PromptLibraryDrawerProps {
  open: boolean;
  onClose: () => void;
}

const PromptLibraryDrawer: React.FC<PromptLibraryDrawerProps> = ({ open, onClose }) => {
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<LibPrompt>(ALL_PROMPTS[0]);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const filtered = useMemo(() => filterPrompts(ALL_PROMPTS, query), [query]);

  const exampleGroups = useMemo(
    () => groupByCategory(filtered.filter((p) => p.kind === 'example'), EXAMPLE_CATEGORY_ORDER),
    [filtered],
  );
  const templateGroups = useMemo(
    () => groupByCategory(filtered.filter((p) => p.kind === 'template'), TEMPLATE_CATEGORY_ORDER),
    [filtered],
  );
  const exampleCount = exampleGroups.reduce((s, [, items]) => s + items.length, 0);
  const templateCount = templateGroups.reduce((s, [, items]) => s + items.length, 0);

  // 选中项被搜索过滤掉时回落到第一条
  useEffect(() => {
    if (filtered.length > 0 && !filtered.some((p) => p.id === selected.id)) {
      setSelected(filtered[0]);
    }
  }, [filtered, selected.id]);

  useEffect(() => () => {
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
  }, []);

  const handleCopy = async (p: LibPrompt) => {
    const ok = await copyText(p.body);
    if (!ok) {
      message.warning('复制失败（剪贴板不可用），请在右侧全文处手动选择复制');
      return;
    }
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
    setCopiedId(p.id);
    copyTimerRef.current = setTimeout(() => setCopiedId(null), 2000);
    message.success(p.kind === 'example' ? '已复制示例——粘贴到对话框直接发送' : '已复制模板——替换 {占位符} 后发送');
  };

  const renderGroups = (groups: Array<[string, LibPrompt[]]>, styles: Record<string, CategoryStyle>) =>
    groups.map(([category, items]) => {
      const cat = styles[category] ?? DEFAULT_STYLE;
      return (
        <div key={category} className="mb-4 last:mb-0">
          <div className="flex items-center gap-2 px-1 pb-1.5">
            <span className="h-1.5 w-1.5 rounded-full shrink-0" style={{ background: cat.dot }} />
            <span className="text-[10px] font-black uppercase tracking-[0.10em] text-slate-400">{category}</span>
            <span className="text-[10px] font-mono text-slate-300">{items.length}</span>
          </div>
          <div className="space-y-1">
            {items.map((p) => {
              const active = p.id === selected.id;
              const isCopied = copiedId === p.id;
              return (
                <button
                  key={p.id}
                  onClick={() => setSelected(p)}
                  className={`group relative flex w-full items-center gap-2.5 rounded-xl py-2.5 pl-3 pr-2 text-left border transition-all ${
                    active
                      ? 'bg-indigo-50 border-indigo-100 shadow-sm'
                      : 'bg-white border-transparent hover:bg-slate-50 hover:border-slate-100'
                  }`}
                >
                  {active && <span className="absolute left-0 top-2 bottom-2 w-0.5 bg-indigo-500 rounded-full" />}
                  <span
                    className={`w-7 h-7 rounded-lg flex items-center justify-center shrink-0 border ${
                      active ? 'bg-white border-indigo-100 text-indigo-600' : 'bg-slate-50 border-slate-100 text-slate-400'
                    }`}
                  >
                    {p.kind === 'example' ? <Lightbulb className="w-3.5 h-3.5" /> : <FileText className="w-3.5 h-3.5" />}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span
                      className={`block truncate text-[12.5px] leading-tight ${
                        active ? 'font-semibold text-indigo-700' : 'font-medium text-slate-700'
                      }`}
                    >
                      {p.title}
                    </span>
                    <span className="block truncate text-[11px] leading-tight text-slate-400">
                      {p.kind === 'example' ? p.body : p.description}
                    </span>
                  </span>
                  <span
                    role="button"
                    tabIndex={-1}
                    title="一键复制"
                    onClick={(e) => {
                      e.stopPropagation();
                      void handleCopy(p);
                    }}
                    className={`flex w-6 h-6 items-center justify-center rounded-md shrink-0 transition-opacity ${
                      isCopied
                        ? 'opacity-100 text-emerald-600'
                        : 'opacity-0 group-hover:opacity-100 text-slate-400 hover:bg-white hover:text-indigo-600'
                    }`}
                  >
                    {isCopied ? <ClipboardCheck className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
                  </span>
                  <ChevronRight
                    className={`w-3 h-3 shrink-0 transition-colors ${active ? 'text-indigo-400' : 'text-slate-300 group-hover:text-slate-400'}`}
                  />
                </button>
              );
            })}
          </div>
        </div>
      );
    });

  const style =
    (selected.kind === 'example' ? INTENT_STYLE[selected.category] : TEMPLATE_STYLE[selected.category]) ?? DEFAULT_STYLE;
  const isSelectedCopied = copiedId === selected.id;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      placement="right"
      width="min(1180px, 96vw)"
      closable={false}
      zIndex={1200}
      destroyOnHidden
      styles={{ body: { padding: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' } }}
    >
      <div className="flex h-full w-full min-w-0 overflow-hidden">
        {/* 左列：搜索 + 分组列表（示例区块 + 模板区块） */}
        <div className="w-[300px] shrink-0 flex flex-col border-r border-gray-200 bg-white overflow-hidden">
          <div className="px-4 pt-4 pb-3 border-b border-gray-100 shrink-0">
            <div className="flex items-center gap-2 text-[11px] font-black uppercase tracking-[0.12em] text-slate-400 mb-2.5">
              <BookMarked className="w-3.5 h-3.5" />
              提示词库
              <span className="text-[10px] font-bold normal-case tracking-normal text-slate-400 bg-slate-100 px-1.5 py-0.5 rounded-md">
                {filtered.length}/{PROMPT_LIBRARY_TOTAL}
              </span>
              <button
                type="button"
                onClick={onClose}
                className="ml-auto flex w-6 h-6 items-center justify-center rounded-md text-slate-400 hover:bg-slate-100 hover:text-slate-600 transition-colors"
                title="关闭"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索 标题 / 描述 / 正文"
                className="w-full h-8 pl-8 pr-8 rounded-full border border-slate-200 bg-white text-[12px] placeholder:text-slate-400 focus:outline-none focus:border-indigo-300 focus:ring-2 focus:ring-indigo-100"
              />
              {query && (
                <button
                  onClick={() => setQuery('')}
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 w-6 h-6 rounded-md flex items-center justify-center text-slate-400 hover:bg-slate-100"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
          </div>

          <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar px-3 py-3">
            {filtered.length === 0 ? (
              <div className="text-center py-10 text-xs text-slate-400">无匹配结果</div>
            ) : (
              <>
                <div className="flex items-center gap-2 px-1 pb-2">
                  <span className="text-[10px] font-black uppercase tracking-[0.12em] text-slate-500">示例提示词</span>
                  <span className="text-[10px] font-mono text-slate-300">{exampleCount}</span>
                  <span className="ml-auto text-[10px] text-slate-300">点选即发</span>
                </div>
                {renderGroups(exampleGroups, INTENT_STYLE)}

                <div className="my-3 border-t border-dashed border-slate-200" />

                <div className="flex items-center gap-2 px-1 pb-2">
                  <span className="text-[10px] font-black uppercase tracking-[0.12em] text-slate-500">提示词模板</span>
                  <span className="text-[10px] font-mono text-slate-300">{templateCount}</span>
                  <span className="ml-auto text-[10px] text-slate-300">{'{占位符}'} 替换后用</span>
                </div>
                {renderGroups(templateGroups, TEMPLATE_STYLE)}
              </>
            )}
          </div>

          <div className="px-4 py-2.5 border-t border-gray-100 bg-slate-50/60 text-[10px] leading-relaxed text-slate-400">
            点列表行右侧 <Copy className="w-2.5 h-2.5 inline -mt-0.5" /> 一键复制，或选中后在右侧复制；粘贴到下方对话框即用。
            <br />
            写操作（下单 / 清仓 / 上实盘）不由助手直达执行——一律在正式页面经二次确认后生效。
          </div>
        </div>

        {/* 右列：详情卡片 + 全文 */}
        <div className="flex min-w-0 flex-1 flex-col overflow-hidden bg-gray-50/50">
          <div className="flex-1 min-h-0 overflow-y-auto p-4 custom-scrollbar">
            <div className="mx-auto max-w-[820px] space-y-4">
              <div className="rounded-3xl bg-white border border-purple-100/80 shadow-sm p-5">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-3 flex-wrap">
                      <div
                        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-white shadow-md"
                        style={{ background: `linear-gradient(135deg, ${style.accent}, #a855f7)` }}
                      >
                        {selected.kind === 'example' ? <Lightbulb className="w-4 h-4" /> : <Sparkles className="w-4 h-4" />}
                      </div>
                      <h3 className="text-[16px] font-bold text-slate-800 tracking-tight">{selected.title}</h3>
                      <span className="shrink-0 rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-bold text-slate-500">
                        {selected.kind === 'example' ? '示例' : '模板'}
                      </span>
                      <span
                        className="shrink-0 rounded-full border px-2.5 py-0.5 text-[11px] font-bold"
                        style={{ background: style.chipBg, color: style.chipText, borderColor: style.chipBorder }}
                      >
                        {selected.category}
                      </span>
                    </div>
                    <p className="mt-2 pl-[48px] text-[12.5px] leading-relaxed text-slate-500">{selected.description}</p>
                    <p className="mt-1 pl-[48px] text-[11px] text-slate-400">
                      {selected.kind === 'template' ? (
                        <>
                          产出：<span className="font-medium text-slate-500">{selected.outputs}</span>
                          <span className="mx-1.5 text-slate-300">·</span>
                          <span className="font-mono text-[10px] text-slate-400">{selected.name}</span>
                        </>
                      ) : (
                        <>直接复制发送 · 无需替换占位符</>
                      )}
                    </p>
                  </div>
                  <button
                    onClick={() => void handleCopy(selected)}
                    className="flex shrink-0 items-center gap-1.5 rounded-xl px-4 py-2 text-[12px] font-bold text-white shadow-lg transition-all active:translate-y-px"
                    style={{
                      background: isSelectedCopied
                        ? 'linear-gradient(135deg, #059669, #10b981)'
                        : 'linear-gradient(135deg, #4f46e5, #a855f7)',
                      boxShadow: isSelectedCopied
                        ? '0 8px 20px -6px rgba(16,185,129,0.45)'
                        : '0 8px 20px -6px rgba(99,102,241,0.45)',
                    }}
                  >
                    {isSelectedCopied ? <ClipboardCheck className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
                    {isSelectedCopied ? '已复制' : '复制提示词'}
                  </button>
                </div>
              </div>

              <div className="rounded-3xl bg-white border border-slate-200/80 shadow-sm overflow-hidden">
                <div className="flex items-center gap-2 px-4 py-2.5 border-b border-slate-100 bg-slate-50/60">
                  <span className="h-2 w-2 rounded-full bg-indigo-400" />
                  <span className="text-[11px] font-bold text-slate-600">提示词全文</span>
                  <span className="ml-auto text-[10px] font-mono text-slate-400 hidden sm:inline">
                    {selected.kind === 'template' ? '{占位符} 替换后粘贴到 QuantBot' : '粘贴到下方对话框直接发送'}
                  </span>
                </div>
                <div className="px-4 py-2.5 flex items-center gap-1.5 text-[11px] text-slate-400 bg-amber-50/40 border-b border-amber-100/60">
                  <Lightbulb className="w-3 h-3 text-amber-500 shrink-0" />
                  {selected.kind === 'example'
                    ? '复制后粘贴到下方 QuantBot 对话框直接发送；生成的分析报告会归档到顶栏「调研报告」。'
                    : '复制后在 QuantBot 对话中粘贴使用，把 {占位符} 替换为实际内容；生成的报告自动归档到顶栏「调研报告」。'}
                </div>
                <pre
                  className="whitespace-pre-wrap p-5 text-[12.5px] leading-relaxed text-slate-700 overflow-x-auto"
                  style={{ fontFamily: 'var(--font-mono)' }}
                >
                  {selected.body}
                </pre>
              </div>
            </div>
          </div>
        </div>
      </div>
    </Drawer>
  );
};

export default PromptLibraryDrawer;
