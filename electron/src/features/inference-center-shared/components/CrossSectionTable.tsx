/**
 * 截面选股工作区：全市场打分榜（全宽表格）。
 *
 * 与左栏「截面轨」的分工：轨是常驻的窄条，用来盯住最近一批的 Top 名单；
 * 这里是把整批明细摊开做**检索**——按位置/行业/板块/分数区间过滤，按任意数值列排序，
 * 导出名单交给下游研究（组合构建、风控复核）。
 *
 * 口径提示：分数是模型原始输出，**未经横截面标准化时不同批次不可直接比大小**，
 * 因此本期/上期的对比只在本批内部有效，导出文件名带上 run_id 以便追溯。
 */

import React, { useCallback, useMemo, useState } from 'react';
import { Button, Input, Select, Table, Tag, Tooltip } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { clsx } from 'clsx';
import { Download, Filter, Search, X } from 'lucide-react';
import type { InferenceRankingItem, InferenceRankingResult } from '../../../services/modelTrainingService';
import { signalPositionLabel } from '../../shared/signalVocabulary';
import { positionTagClass } from '../positionLabels';

/** 分数变化超过该值才标出方向，避免把浮点噪声渲染成「变动」 */
const DELTA_EPSILON = 1e-6;
/** 导出上限：防止误点把百万行明细写成 CSV 把浏览器卡死 */
const EXPORT_LIMIT = 20000;

type SignalKey = 'buy' | 'sell' | 'hold';

/**
 * 展示面只用位置词（靠前/居中/靠后），不出现方向性字眼。
 * 内部筛选 key 仍是 buy/sell/hold，保持与后端契约一一对应。
 */
const SIGNAL_META: Record<SignalKey, { label: string; cls: string }> = {
  buy: { label: signalPositionLabel('BUY'), cls: positionTagClass('靠前') },
  sell: { label: signalPositionLabel('SELL'), cls: positionTagClass('靠后') },
  hold: { label: signalPositionLabel('HOLD'), cls: positionTagClass('居中') },
};

function signalKeyOf(v: string | undefined): SignalKey {
  const k = String(v || '').toLowerCase();
  if (k === 'buy' || k === 'sell') return k;
  return 'hold';
}

/** 分数 → 显示色（正红负绿，与全站一致） */
const scoreClass = (v: number | null | undefined): string =>
  v == null ? 'text-slate-400' : v > 0 ? 'text-rose-600' : v < 0 ? 'text-emerald-600' : 'text-slate-600';

const fmt = (v: number | null | undefined, digits = 4): string =>
  v == null || Number.isNaN(v) ? '—' : v.toFixed(digits);

/** CSV 单元格转义：含逗号/引号/换行时用双引号包裹并转义内部引号 */
function csvCell(v: unknown): string {
  const s = v == null ? '' : String(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export interface CrossSectionCsvMeta {
  runId?: string;
  inferenceDate?: string;
  targetDate?: string;
  modelId?: string;
  /** 筛选前的总行数，用于元信息行 */
  total: number;
}

/**
 * 截面名单 CSV（纯函数，便于单测）。
 * 表头与单元格一律用位置词；内部 key（buy/sell/hold）不出现在导出文件里。
 */
export function buildCrossSectionCsv(rows: InferenceRankingItem[], meta: CrossSectionCsvMeta): string {
  const head = [
    '排名', '代码', '名称', '分数', '位置', '行业', '板块', '趋势',
    '市值(亿)', '市值分层', '上期分数', '分数变化', '负向标签',
  ];
  const body = rows.map((r) => {
    const delta = r.prev_score == null ? null : r.score - r.prev_score;
    return [
      r.rank, r.code, r.name, r.score, SIGNAL_META[signalKeyOf(r.signal)].label,
      r.industry ?? '', r.board ?? '', r.trend ?? '',
      r.market_cap_yi ?? '', r.market_cap_tier ?? '',
      r.prev_score ?? '', delta == null ? '' : delta.toFixed(6),
      r.negative_tag ?? '',
    ];
  });
  const metaLines = [
    `# 批次 run_id=${meta.runId ?? ''} 数据日=${meta.inferenceDate ?? ''} 目标日=${meta.targetDate ?? ''} 模型=${meta.modelId ?? ''}`,
    `# 导出 ${body.length} / ${meta.total} 行（已应用筛选）`,
  ];
  return [...metaLines, head.map(csvCell).join(','), ...body.map((r) => r.map(csvCell).join(','))].join('\r\n');
}

interface CrossSectionTableProps {
  ranking: InferenceRankingResult | null;
  loading: boolean;
  /** 非空 = 展示的是更早批次（最近一批明细未落库） */
  fallbackFrom: string | null;
  /** 点行 → 送单票研判 */
  onSelect: (item: InferenceRankingItem) => void;
  /** 当前联动中的标的（高亮行） */
  activeCode?: string;
  /** 显示中性化分位列（影子层，0~1 分位） */
  neutralMode?: boolean;
}

export const CrossSectionTable: React.FC<CrossSectionTableProps> = ({
  ranking,
  loading,
  fallbackFrom,
  onSelect,
  activeCode,
  neutralMode = false,
}) => {
  const [keyword, setKeyword] = useState('');
  const [signals, setSignals] = useState<SignalKey[]>([]);
  const [industry, setIndustry] = useState<string | null>(null);
  const [board, setBoard] = useState<string | null>(null);
  const [tier, setTier] = useState<string | null>(null);

  const rows = useMemo(() => ranking?.rankings ?? [], [ranking]);

  /** 单值候选项（行业/板块/分层）从当前批次实际出现过的值里取，不写死字典 */
  const options = useMemo(() => {
    const uniq = (pick: (r: InferenceRankingItem) => string | undefined) => {
      const set = new Set<string>();
      for (const r of rows) {
        const v = pick(r);
        if (v) set.add(String(v));
      }
      return [...set].sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'));
    };
    return {
      industries: uniq((r) => r.industry),
      boards: uniq((r) => r.board),
      tiers: uniq((r) => r.market_cap_tier),
    };
  }, [rows]);

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return rows.filter((r) => {
      if (signals.length > 0 && !signals.includes(signalKeyOf(r.signal))) return false;
      if (industry && r.industry !== industry) return false;
      if (board && r.board !== board) return false;
      if (tier && r.market_cap_tier !== tier) return false;
      if (kw) {
        const hay = `${r.code ?? ''} ${r.name ?? ''}`.toLowerCase();
        if (!hay.includes(kw)) return false;
      }
      return true;
    });
  }, [rows, keyword, signals, industry, board, tier]);

  const hasFilter = Boolean(keyword || signals.length || industry || board || tier);
  const resetFilters = useCallback(() => {
    setKeyword('');
    setSignals([]);
    setIndustry(null);
    setBoard(null);
    setTier(null);
  }, []);

  const exportCsv = useCallback(() => {
    const csv = buildCrossSectionCsv(filtered.slice(0, EXPORT_LIMIT), {
      runId: ranking?.run_id,
      inferenceDate: ranking?.inference_date,
      targetDate: ranking?.target_date,
      modelId: ranking?.model_id,
      total: rows.length,
    });
    // BOM：Excel 打开 UTF-8 CSV 不加 BOM 会把中文渲染成乱码
    const blob = new Blob([`﻿${csv}`], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `截面打分_${ranking?.inference_date ?? 'unknown'}_${(ranking?.model_id ?? 'model').slice(0, 24)}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [filtered, rows.length, ranking?.run_id, ranking?.inference_date, ranking?.target_date, ranking?.model_id]);

  const columns: ColumnsType<InferenceRankingItem> = useMemo(() => [
    {
      title: '#', dataIndex: 'rank', width: 56, fixed: 'left',
      sorter: (a, b) => a.rank - b.rank,
      render: (v: number) => <span className="font-mono text-[11px] font-bold text-slate-400">{v}</span>,
    },
    {
      title: '代码', dataIndex: 'code', width: 108, fixed: 'left',
      render: (v: string) => <span className="font-mono text-[11px] font-bold text-blue-700">{v}</span>,
    },
    {
      title: '名称', dataIndex: 'name', width: 130, ellipsis: true,
      render: (v: string, r) => (
        <Tooltip title={r.negative_tag ? `负向标记：${r.negative_tag}` : undefined}>
          <span className="text-xs font-bold text-slate-800 truncate">{v}</span>
        </Tooltip>
      ),
    },
    {
      title: '分数', dataIndex: 'score', width: 104, align: 'right',
      sorter: (a, b) => a.score - b.score,
      defaultSortOrder: neutralMode ? undefined : 'descend',
      render: (v: number) => <span className={clsx('font-mono text-xs font-black', scoreClass(v))}>{fmt(v)}</span>,
    },
    ...(neutralMode
      ? [
          {
            title: '中性排名',
            dataIndex: 'neutral_rank',
            width: 82,
            align: 'right' as const,
            sorter: (a: InferenceRankingItem, b: InferenceRankingItem) =>
              (a.neutral_rank ?? 999999) - (b.neutral_rank ?? 999999),
            render: (v: number | null) =>
              v == null ? (
                <span className="text-[11px] text-slate-300">—</span>
              ) : (
                <span className="rounded-md bg-indigo-50 px-1.5 py-0.5 font-mono text-[11px] font-black text-indigo-700">
                  #{v}
                </span>
              ),
          },
          {
            title: (
              <Tooltip title="影子层：按流通市值、60日 Beta、20日动量做截面中性化后的分位（0~1）">
                <span>中性化分位</span>
              </Tooltip>
            ),
            dataIndex: 'neutral_score',
            width: 132,
            align: 'right' as const,
            sorter: (a: InferenceRankingItem, b: InferenceRankingItem) =>
              (a.neutral_score ?? -1) - (b.neutral_score ?? -1),
            render: (v: number | null) =>
              v == null ? (
                <span className="text-[11px] text-slate-300">—</span>
              ) : (
                <div className="flex items-center justify-end gap-1.5">
                  <div className="h-1.5 w-14 overflow-hidden rounded-full bg-slate-100">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-indigo-400 to-indigo-600"
                      style={{ width: `${v * 100}%` }}
                    />
                  </div>
                  <span className="font-mono text-[11px] font-black text-indigo-700">
                    {(v * 100).toFixed(1)}
                  </span>
                </div>
              ),
          },
          {
            title: '名次变化',
            dataIndex: 'neutral_delta',
            width: 82,
            align: 'right' as const,
            sorter: (a: InferenceRankingItem, b: InferenceRankingItem) =>
              (a.neutral_delta ?? 0) - (b.neutral_delta ?? 0),
            render: (v: number | null) =>
              v == null ? (
                <span className="text-[11px] text-slate-300">—</span>
              ) : v > 0 ? (
                <span className="font-mono text-[11px] font-black text-emerald-600">↑{v}</span>
              ) : v < 0 ? (
                <span className="font-mono text-[11px] font-bold text-slate-400">↓{Math.abs(v)}</span>
              ) : (
                <span className="text-[11px] text-slate-300">—</span>
              ),
          },
        ]
      : []),
    {
      title: '位置', dataIndex: 'signal', width: 72, align: 'center',
      filters: (Object.keys(SIGNAL_META) as SignalKey[]).map((k) => ({ text: SIGNAL_META[k].label, value: k })),
      onFilter: (val, r) => signalKeyOf(r.signal) === val,
      render: (v: string) => {
        const m = SIGNAL_META[signalKeyOf(v)];
        return <span className={clsx('inline-block px-1.5 rounded border text-[10px] font-black', m.cls)}>{m.label}</span>;
      },
    },
    {
      title: '较上期', key: 'delta', width: 92, align: 'right',
      sorter: (a, b) => ((a.score - (a.prev_score ?? a.score)) - (b.score - (b.prev_score ?? b.score))),
      render: (_, r) => {
        if (r.prev_score == null) return <span className="text-[11px] text-slate-300">—</span>;
        const d = r.score - r.prev_score;
        if (Math.abs(d) < DELTA_EPSILON) return <span className="font-mono text-[11px] text-slate-400">持平</span>;
        return (
          <span className={clsx('font-mono text-[11px] font-bold', scoreClass(d))}>
            {d > 0 ? '▲' : '▼'}{Math.abs(d).toFixed(4)}
          </span>
        );
      },
    },
    {
      title: '行业', dataIndex: 'industry', width: 120, ellipsis: true,
      render: (v: string) => <span className="text-[11px] text-slate-600">{v || '—'}</span>,
    },
    {
      title: '板块', dataIndex: 'board', width: 96, ellipsis: true,
      render: (v: string) => <span className="text-[11px] text-slate-600">{v || '—'}</span>,
    },
    {
      title: '趋势', dataIndex: 'trend', width: 96, ellipsis: true,
      render: (v: string) => (v ? <Tag className="!m-0 text-[10px] leading-tight">{v}</Tag> : <span className="text-slate-300">—</span>),
    },
    {
      title: '市值(亿)', dataIndex: 'market_cap_yi', width: 100, align: 'right',
      sorter: (a, b) => (a.market_cap_yi ?? -1) - (b.market_cap_yi ?? -1),
      render: (v: number | null) => <span className="font-mono text-[11px] text-slate-600">{v == null ? '—' : v.toFixed(1)}</span>,
    },
    {
      title: '分层', dataIndex: 'market_cap_tier', width: 84,
      render: (v: string) => <span className="text-[11px] text-slate-500">{v || '—'}</span>,
    },
    {
      title: '负向标记', dataIndex: 'negative_tag', width: 110, ellipsis: true,
      render: (v: string) =>
        v ? (
          <span className="text-[10px] font-bold text-amber-700 bg-amber-50 border border-amber-200 rounded px-1 py-0.5">{v}</span>
        ) : (
          <span className="text-slate-300">—</span>
        ),
    },
  ], [neutralMode]);

  return (
    <div
      className={clsx(
        'flex-1 min-w-0 min-h-0 flex flex-col bg-white rounded-xl overflow-hidden border',
        neutralMode
          ? 'border-indigo-200 ring-1 ring-indigo-100 shadow-[0_0_0_3px_rgba(99,102,241,0.06)]'
          : 'border-slate-200',
      )}
    >
      {neutralMode && (
        <div className="h-0.5 w-full shrink-0 bg-gradient-to-r from-indigo-500 via-violet-500 to-sky-400" />
      )}
      {/* ── 过滤条 ─────────────────────────────────────────── */}
      <div className="shrink-0 px-3 py-2 border-b border-slate-200 bg-slate-50/60 flex items-center gap-2 flex-wrap">
        <div className="flex items-center bg-white border border-slate-200 hover:border-blue-400 focus-within:border-blue-500 focus-within:ring-2 focus-within:ring-blue-100 rounded-md pl-2.5 pr-2 h-8 transition-all w-[190px] shrink-0">
          <Search size={13} className="text-slate-400 shrink-0 mr-1.5" />
          <Input
            variant="borderless"
            placeholder="代码 / 名称"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            className="p-0 font-mono text-xs"
            style={{ flex: 1, minWidth: 60, padding: 0 }}
          />
        </div>

        <Select
          mode="multiple"
          allowClear
          size="small"
          placeholder="位置"
          maxTagCount={2}
          value={signals}
          onChange={(v) => setSignals(v as SignalKey[])}
          className="!min-w-[104px] shrink-0"
          options={(Object.keys(SIGNAL_META) as SignalKey[]).map((k) => ({ value: k, label: SIGNAL_META[k].label }))}
        />
        <Select
          allowClear
          size="small"
          showSearch
          placeholder="行业"
          value={industry}
          onChange={(v) => setIndustry(v ?? null)}
          className="!min-w-[130px] shrink-0"
          options={options.industries.map((v) => ({ value: v, label: v }))}
        />
        <Select
          allowClear
          size="small"
          showSearch
          placeholder="板块"
          value={board}
          onChange={(v) => setBoard(v ?? null)}
          className="!min-w-[110px] shrink-0"
          options={options.boards.map((v) => ({ value: v, label: v }))}
        />
        <Select
          allowClear
          size="small"
          placeholder="市值分层"
          value={tier}
          onChange={(v) => setTier(v ?? null)}
          className="!min-w-[110px] shrink-0"
          options={options.tiers.map((v) => ({ value: v, label: v }))}
        />

        {hasFilter && (
          <button
            type="button"
            onClick={resetFilters}
            className="flex items-center gap-1 text-[11px] font-bold text-slate-500 hover:text-slate-800 px-1.5 py-0.5 rounded hover:bg-slate-100 transition-colors shrink-0"
          >
            <X size={11} /> 清除筛选
          </button>
        )}

        <div className="ml-auto flex items-center gap-2 shrink-0">
          <span className="flex items-center gap-1 text-[11px] text-slate-500 font-semibold">
            <Filter size={11} className="text-slate-400" />
            <span className="font-mono font-black text-slate-700">{filtered.length}</span>
            <span className="text-slate-400">/ {rows.length}</span>
          </span>
          <Button
            size="small"
            icon={<Download size={12} />}
            onClick={exportCsv}
            disabled={filtered.length === 0}
            className="rounded-md text-[11px] font-bold shrink-0"
          >
            导出 CSV
          </Button>
        </div>
      </div>

      {/* ── 批次提示 ───────────────────────────────────────── */}
      {ranking && (
        <div className="shrink-0 px-3 py-1 bg-slate-50 border-b border-slate-100 flex items-center gap-3 flex-wrap">
          <span className="text-[10px] text-slate-500">
            数据日 <strong className="font-mono text-slate-700">{ranking.inference_date}</strong>
          </span>
          <span className="text-[10px] text-slate-500">
            目标日 <strong className="font-mono text-blue-700">{ranking.target_date}</strong>
          </span>
          <span className="text-[10px] text-slate-500">
            批次 <strong className="font-mono text-slate-700">{ranking.run_id.slice(0, 8)}</strong>
          </span>
          {fallbackFrom && (
            <span className="text-[10px] font-bold text-amber-700 bg-amber-50 border border-amber-200 rounded px-1.5 py-0.5">
              最近一批明细未落库，当前为更早批次 {fallbackFrom.slice(0, 8)}
            </span>
          )}
          <span className="ml-auto text-[10px] text-slate-400">
            分数为模型原始输出，跨批次不可直接比大小
          </span>
        </div>
      )}

      {/* ── 明细表 ─────────────────────────────────────────── */}
      <div className="flex-1 min-h-0">
        <Table<InferenceRankingItem>
          rowKey={(r) => `${r.code}-${r.rank}`}
          size="small"
          loading={loading}
          dataSource={filtered}
          columns={columns}
          scroll={{ x: 1360, y: 'calc(100vh - 320px)' }}
          pagination={{ pageSize: 50, size: 'small', showSizeChanger: true, pageSizeOptions: ['50', '100', '200'] }}
          onRow={(r) => ({
            onClick: () => onSelect(r),
            className: clsx('cursor-pointer', r.code === activeCode && 'bg-blue-50/70'),
          })}
          locale={{ emptyText: loading ? ' ' : '该批次无排名明细' }}
        />
      </div>
    </div>
  );
};
