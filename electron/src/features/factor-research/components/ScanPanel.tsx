/**
 * 因子研究 —— 「扫描」面板：把盘上的因子与快照目录对一遍，**只列差异**。
 *
 * 为什么单独做这个入口：因子目录（factors.json）是**快照产物**，不是每次读盘
 * 现算的。所以新挖到的因子写进 quantdb 后，界面上看不见；而重算一次私人库要
 * 5~15 分钟并重写 5.5GB 宽表。这里让人先花一秒看清「有什么新的」，再决定付不付。
 *
 * 只读：本面板**不发起**构建。要重算必须用户点按钮，且交给既有快照面板
 * （进度/日志/轮询都在那边，前端也只有一处发起构建）。
 *
 * 差异口径由后端 /factor-research/scan 与重算共用同一份扫描实现给出（见
 * backend/services/engine/factor_research/discovery.py）：两边一旦分叉，这里就会
 * 承诺一个重算根本不会发生的结果。
 *
 * 布局（2026-11-06）：单个库可能一次新增 400+ 个因子（JKP A股因子库首扫 404 个），
 * 平铺会把整页撑爆。现在按库分组、组内限高滚动、超阈值折叠，并支持顶部按名筛选。
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  GraduationCap,
  Loader2,
  RefreshCw,
  ScanSearch,
  Search,
} from 'lucide-react';
import { getPromoteStatus, getScanSources, postPromote } from '../services/factorResearchService';
import type { FactorDataset, ScanDiff, ScanDiffItem } from '../services/factorResearchService';
import type { ResearchMarket } from '../types/factorResearch';
import { Card } from './common';

interface Props {
  /** 扫描的数据集（页面上恒为 private；其它数据集后端 400） */
  dataset: FactorDataset;
  /** 本页当前市场（扫描本市场数据根） */
  market: ResearchMarket;
  /** 去重算快照（交给快照面板：那里有进度与日志） */
  onRebuild: () => void;
  /** 关掉本面板 */
  onClose: () => void;
}

/** 组内一次最多平铺多少个标签；超过则折叠，避免 400+ 标签同时进 DOM。 */
const COLLAPSE_THRESHOLD = 40;
/** 折叠态下先展示多少个（剩下的点「展开全部」）。 */
const COLLAPSED_SHOW = 24;
/** 组内列表高度上限（px）——超过就内部滚动，不撑高卡片。 */
const GROUP_BODY_MAX_H = 168;

interface LibGroup {
  lib: string;
  label: string;
  items: ScanDiffItem[];
}

/** 新增项按来源库归组（保留后端给的库顺序） */
function groupByLibrary(items: ScanDiffItem[]): LibGroup[] {
  const groups = new Map<string, LibGroup>();
  for (const it of items) {
    const g = groups.get(it.library) || { lib: it.library, label: it.library_label || it.library, items: [] };
    g.items.push(it);
    groups.set(it.library, g);
  }
  return [...groups.values()];
}

const Stat: React.FC<{ label: string; value: React.ReactNode; tone?: string }> = ({ label, value, tone }) => (
  <span className="inline-flex items-baseline gap-1">
    <span className="text-[10px] text-slate-400">{label}</span>
    <span className={`text-[11px] font-bold font-mono ${tone || 'text-slate-700'}`}>{value}</span>
  </span>
);

const Chip: React.FC<{ code: string; title?: string; tone?: 'emerald' | 'amber' }> = ({ code, title, tone = 'emerald' }) => (
  <span
    className={`rounded border px-1.5 py-0.5 text-[10px] font-mono text-slate-600 ${
      tone === 'emerald' ? 'border-emerald-100 bg-white' : 'border-amber-100 bg-white'
    }`}
    title={title || code}
  >
    {code}
  </span>
);

/** 单个来源库分组：标题行可折叠，组内限高滚动，大列表默认只平铺前 N 个。 */
const LibraryGroup: React.FC<{ group: LibGroup; keyword: string }> = ({ group, keyword }) => {
  const [open, setOpen] = useState(true);
  const [showAll, setShowAll] = useState(false);

  const matched = useMemo(() => {
    const q = keyword.trim().toLowerCase();
    return q ? group.items.filter((it) => it.code.toLowerCase().includes(q)) : group.items;
  }, [group.items, keyword]);

  // 只有「用户点了展开」或「本来就少于阈值」才全量平铺；无筛选词时同样要折叠，
  // 否则单库 400+ 仍会一次性进 DOM（首屏卡顿 + 视觉淹没）。
  const cap = showAll || matched.length <= COLLAPSE_THRESHOLD ? matched.length : COLLAPSED_SHOW;
  const shown = matched.slice(0, cap);
  const hidden = matched.length - shown.length;

  return (
    <div
      data-testid={`scan-new-${group.lib}`}
      className="rounded-xl border border-emerald-100 bg-emerald-50/40 px-2.5 py-1.5"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-baseline gap-2 text-left"
        title={open ? '收起该库' : '展开该库'}
      >
        {open ? (
          <ChevronDown className="w-3 h-3 shrink-0 text-emerald-500" />
        ) : (
          <ChevronRight className="w-3 h-3 shrink-0 text-emerald-500" />
        )}
        <span className="text-[11px] font-bold text-emerald-700">{group.label}</span>
        <span className="text-[10px] font-mono text-emerald-600">{group.items.length} 个新增</span>
        {keyword.trim() && (
          <span className="text-[10px] font-mono text-slate-400">命中 {matched.length}</span>
        )}
      </button>

      {open && (
        <>
          <div className="mt-1 overflow-y-auto custom-scrollbar" style={{ maxHeight: GROUP_BODY_MAX_H }}>
            <div className="flex flex-wrap gap-1 pr-1">
              {shown.map((it) => (
                <Chip key={it.code} code={it.code} />
              ))}
              {matched.length === 0 && (
                <span className="text-[10px] text-slate-400">没有匹配的因子名</span>
              )}
            </div>
          </div>
          {hidden > 0 && (
            <button
              type="button"
              onClick={() => setShowAll(true)}
              className="mt-1 rounded-full border border-emerald-200 bg-white px-2 py-0.5 text-[10px] font-bold text-emerald-600 hover:bg-emerald-50"
            >
              展开全部（还有 {hidden} 个）
            </button>
          )}
          {showAll && matched.length > COLLAPSE_THRESHOLD && (
            <button
              type="button"
              onClick={() => setShowAll(false)}
              className="mt-1 ml-1 rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-bold text-slate-500 hover:bg-slate-50"
            >
              收起
            </button>
          )}
        </>
      )}
    </div>
  );
};

export const ScanPanel: React.FC<Props> = ({ dataset, market, onRebuild, onClose }) => {
  const [diff, setDiff] = useState<ScanDiff | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // 初值即「扫描中」：首屏就是一次在途请求，否则会先闪一下空态
  const [scanning, setScanning] = useState(true);
  // 一键毕业（毕业桥子进程）：promoting=轮询中；完成后自动重新扫描
  const [promoting, setPromoting] = useState(false);
  const [promoteStep, setPromoteStep] = useState('');
  const [promoteErr, setPromoteErr] = useState<string | null>(null);
  // 顶部筛选：库多/因子多时按名找；空串 = 不过滤
  const [keyword, setKeyword] = useState('');

  const scan = useCallback(async () => {
    setScanning(true);
    setErr(null);
    try {
      setDiff(await getScanSources(dataset, market));
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setScanning(false);
    }
  }, [dataset, market]);

  useEffect(() => {
    void scan();
  }, [scan]);

  const startPromote = useCallback(async () => {
    if (promoting) return;
    setPromoteErr(null);
    setPromoteStep('');
    try {
      await postPromote();
      setPromoting(true);
    } catch (e: unknown) {
      setPromoteErr(e instanceof Error ? e.message : String(e));
    }
  }, [promoting]);

  // 毕业轮询：子进程结束后重新扫描 —— 待毕业数应清零，新增列表里也会出现
  // 刚毕业的挖掘因子（下一步就是「重算快照」把它们收进目录）。
  useEffect(() => {
    if (!promoting) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const st = await getPromoteStatus();
        if (cancelled) return;
        setPromoteStep(st.step || '');
        if (!st.running) {
          setPromoting(false);
          setPromoteStep('');
          void scan();
        }
      } catch {
        /* 轮询失败不中断 —— 下一拍再试 */
      }
    };
    void tick();
    const timer = window.setInterval(() => void tick(), 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [promoting, scan]);

  const nNew = diff?.new.length ?? 0;
  const nMissing = diff?.missing.length ?? 0;
  const isLatest = !!diff && nNew === 0 && nMissing === 0;
  const groups = groupByLibrary(diff?.new ?? []);
  const pipeline = diff?.pipeline || null;

  const q = keyword.trim().toLowerCase();
  const matchedNew = useMemo(
    () => (q ? (diff?.new ?? []).filter((it) => it.code.toLowerCase().includes(q)).length : nNew),
    [diff, q, nNew]
  );
  const matchedMissing = useMemo(
    () => (q ? (diff?.missing ?? []).filter((it) => it.code.toLowerCase().includes(q)).length : nMissing),
    [diff, q, nMissing]
  );
  // 有筛选时，空的库不再占位
  const visibleGroups = q ? groups.filter((g) => g.items.some((it) => it.code.toLowerCase().includes(q))) : groups;
  const visibleMissing = q
    ? (diff?.missing ?? []).filter((it) => it.code.toLowerCase().includes(q))
    : diff?.missing ?? [];

  return (
    <div className="flex-1 min-h-0 h-full flex items-start justify-center pt-4">
      <Card
        title="扫描因子来源"
        className="w-[760px] max-w-full h-full max-h-[calc(100vh-7.5rem)]"
        extra={
          <div className="flex items-center gap-2">
            <button
              data-testid="scan-rescan"
              onClick={() => void scan()}
              disabled={scanning}
              className="flex items-center gap-1 rounded-full border border-slate-200 bg-white px-2.5 py-0.5 text-[10px] font-bold text-slate-500 hover:bg-slate-50 disabled:opacity-50"
            >
              <RefreshCw className={`w-3 h-3 ${scanning ? 'animate-spin' : ''}`} />
              {scanning ? '扫描中…' : '重新扫描'}
            </button>
            <button
              onClick={onRebuild}
              className="flex items-center gap-1 rounded-full bg-indigo-600 px-3 py-0.5 text-[10px] font-bold text-white hover:bg-indigo-700"
              title="全量扫描 6_ml_datasets 重建索引（5~15 分钟，本地计算）"
            >
              重算快照
            </button>
            <button
              data-testid="scan-close"
              onClick={onClose}
              className="rounded-full border border-slate-200 bg-white px-2.5 py-0.5 text-[10px] font-bold text-slate-500 hover:bg-slate-50"
            >
              关闭
            </button>
          </div>
        }
      >
        <div className="flex h-full min-h-0 flex-col">
          <div
            data-testid="scan-summary"
            className="shrink-0 flex items-center gap-3 flex-wrap rounded-lg bg-slate-50 border border-slate-100 px-3 py-1.5"
          >
            <ScanSearch className="w-3.5 h-3.5 text-slate-400 shrink-0" />
            <Stat label="盘上扫到" value={diff?.discovered_count ?? '—'} />
            <Stat label="快照目录" value={diff?.catalog_count ?? '—'} />
            <Stat label="新增" value={q ? `${matchedNew}/${nNew}` : nNew} tone={nNew ? 'text-emerald-600' : 'text-slate-400'} />
            <Stat label="消失" value={q ? `${matchedMissing}/${nMissing}` : nMissing} tone={nMissing ? 'text-amber-600' : 'text-slate-400'} />
            <Stat label="未变" value={diff?.unchanged_count ?? '—'} />
            {diff?.snapshot_at && (
              <span className="text-[10px] text-slate-400">快照建于 {diff.snapshot_at}</span>
            )}
          </div>

          {/* 筛选框：单库 400+ 因子时按名找，比滚动快 */}
          {nNew > 20 && (
            <div className="shrink-0 mt-1.5 relative">
              <Search className="w-3 h-3 text-slate-400 absolute left-2 top-1/2 -translate-y-1/2" />
              <input
                data-testid="scan-filter"
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
                placeholder={`在 ${nNew} 个新增因子里筛选（名称包含）`}
                className="w-full rounded-lg border border-slate-200 bg-white pl-7 pr-2 py-1 text-[11px] text-slate-700 placeholder:text-slate-400 focus:border-indigo-300 focus:outline-none"
              />
            </div>
          )}

          {/* 快照不是按默认全量扫描建的时，说清楚「新增」为什么会偏多 —— 重算按钮
              走的是 auto 全量，两边的范围本来就不是一回事，不说明会看着像 bug。 */}
          {diff?.snapshot_source && diff.snapshot_source !== 'auto' && (
            <div
              data-testid="scan-source-note"
              className="shrink-0 mt-1.5 flex items-start gap-1.5 rounded-lg border border-amber-100 bg-amber-50 px-2.5 py-1.5 text-[10px] leading-relaxed text-amber-700"
            >
              <AlertTriangle className="w-3 h-3 mt-px shrink-0" />
              <span>
                本快照是按「{diff.snapshot_source}」建的，而重算走的是**全量扫描（auto）**：
                下面的「新增」包含 auto 会收进来、而当初这次构建故意没收的因子，属正常。
              </span>
            </div>
          )}

          {/* 毕业管道：新挖到的因子在这里「卡住」—— CUSTOM 挖掘库有、CN 盘面没有。
              不把这一站显出来，用户只会看到「挖掘跑完了但哪都没有」。
              一键毕业 = 毕业桥 --register（镜像 + 登记字段）；**不做 --publish**
              （发布进训练目录是人工闸门，不在此自动化）。 */}
          {pipeline && pipeline.state !== 'empty' && (
            <div
              data-testid="scan-pipeline"
              className={`shrink-0 mt-1.5 rounded-lg border px-2.5 py-1.5 ${
                pipeline.state === 'pending' ? 'border-indigo-100 bg-indigo-50/50' : 'border-emerald-100 bg-emerald-50/40'
              }`}
            >
              {pipeline.state === 'pending' ? (
                <>
                  <div className="flex items-center gap-2 flex-wrap">
                    <GraduationCap className="w-3.5 h-3.5 text-indigo-500 shrink-0" />
                    <span className="text-[11px] font-bold text-indigo-700">毕业管道：挖掘库 → 盘面</span>
                    <Stat label="CUSTOM 挖掘库" value={pipeline.custom_n_factors ?? '—'} />
                    <Stat label="CN 盘面" value={pipeline.cn_n_factors ?? '—'} />
                    <Stat label="待毕业" value={pipeline.pending_factors} tone="text-indigo-600" />
                    <Stat label="涉及分区" value={pipeline.pending_partitions} tone="text-slate-500" />
                    {pipeline.custom_last_dt && (
                      <span className="text-[10px] text-slate-400">数据至 {pipeline.custom_last_dt}</span>
                    )}
                    <button
                      data-testid="scan-promote"
                      onClick={() => void startPromote()}
                      disabled={promoting}
                      className="ml-auto flex items-center gap-1 rounded-full bg-indigo-600 px-3 py-0.5 text-[10px] font-bold text-white hover:bg-indigo-700 disabled:opacity-50"
                      title="把 CUSTOM 挖掘库镜像到 CN 并刷新字段注册（后台运行，完成后自动重新扫描）。不发布训练目录。"
                    >
                      {promoting ? <Loader2 className="w-3 h-3 animate-spin" /> : <GraduationCap className="w-3 h-3" />}
                      {promoting ? '毕业中…' : '一键毕业（镜像 + 登记字段）'}
                    </button>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {pipeline.pending_factor_names.slice(0, 24).map((name) => (
                      <span
                        key={name}
                        className="rounded border border-indigo-100 bg-white px-1.5 py-0.5 text-[10px] font-mono text-slate-600"
                        title={name}
                      >
                        {name}
                      </span>
                    ))}
                    {pipeline.pending_factors > 24 && (
                      <span className="text-[10px] text-slate-400">… 共 {pipeline.pending_factors} 个</span>
                    )}
                  </div>
                  <div className="mt-1 text-[10px] text-indigo-500/80">
                    新挖到的因子先落在 CUSTOM 挖掘库，镜像 + 登记后才在 CN 盘面可见；毕业后再点右上「重算快照」，
                    它们才会进入因子目录与报告。
                    {promoting && promoteStep ? <span className="ml-1 font-mono text-indigo-400">{promoteStep}</span> : null}
                  </div>
                </>
              ) : (
                <div className="flex items-center gap-1.5 text-[10px] text-emerald-700">
                  <CheckCircle2 className="w-3 h-3 shrink-0" />
                  毕业管道：CUSTOM 挖掘库与 CN 盘面已同步（{pipeline.cn_n_factors ?? '—'} 个因子）
                  {pipeline.custom_last_dt ? `，数据至 ${pipeline.custom_last_dt}` : ''}。
                </div>
              )}
              {promoteErr && <div className="mt-1 text-[10px] text-rose-500 break-all">{promoteErr}</div>}
            </div>
          )}

          {err && (
            <div data-testid="scan-error" className="shrink-0 mt-1.5 text-[10px] text-rose-500 break-all">
              {err}
            </div>
          )}

          <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar mt-2 space-y-2 pr-1">
            {isLatest && (
              <div className="text-[11px] text-slate-500 py-3 text-center">
                已是最新：盘上的因子与快照目录完全一致，没有需要重算的内容。
              </div>
            )}

            {visibleGroups.map((g) => (
              <LibraryGroup key={g.lib} group={g} keyword={keyword} />
            ))}

            {q && visibleGroups.length === 0 && nNew > 0 && (
              <div className="text-[11px] text-slate-500 py-3 text-center">没有匹配「{keyword}」的新增因子</div>
            )}

            {visibleMissing.length > 0 && (
              <div data-testid="scan-missing" className="rounded-xl border border-amber-100 bg-amber-50/40 px-2.5 py-1.5">
                <div className="flex items-baseline gap-2">
                  <span className="text-[11px] font-bold text-amber-700">快照里还在、盘上已找不到</span>
                  <span className="text-[10px] font-mono text-amber-600">{visibleMissing.length} 个</span>
                </div>
                <div className="mt-1 overflow-y-auto custom-scrollbar" style={{ maxHeight: GROUP_BODY_MAX_H }}>
                  <div className="flex flex-wrap gap-1 pr-1">
                    {visibleMissing.map((it) => (
                      <span
                        key={it.code}
                        className="rounded border border-amber-100 bg-white px-1.5 py-0.5 text-[10px] font-mono text-slate-600"
                        title={it.library_label || it.library || '来源未知'}
                      >
                        {it.code}
                        {it.library_label ? <span className="ml-1 text-slate-400">{it.library_label}</span> : null}
                      </span>
                    ))}
                  </div>
                </div>
                <div className="mt-1 text-[10px] text-amber-600/80">
                  重算后会从目录里移除（数据没了，快照留着只会读到空值）。
                </div>
              </div>
            )}
          </div>

          <div className="shrink-0 mt-1.5 text-[10px] text-slate-400">
            只读扫描：仅读各库最新分区的列名，不读数据、不写目录、不触发计算 · 全本地、不上传
          </div>
        </div>
      </Card>
    </div>
  );
};
