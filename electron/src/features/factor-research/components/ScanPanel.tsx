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
 */
import React, { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, GraduationCap, Loader2, RefreshCw, ScanSearch } from 'lucide-react';
import { getPromoteStatus, getScanSources, postPromote } from '../services/factorResearchService';
import type { ScanDiff, ScanDiffItem } from '../services/factorResearchService';
import { Card } from './common';

interface Props {
  /** 去重算快照（交给快照面板：那里有进度与日志） */
  onRebuild: () => void;
  /** 关掉本面板 */
  onClose: () => void;
}

/** 新增项按来源库归组（保留后端给的库顺序） */
function groupByLibrary(items: ScanDiffItem[]): Array<{ lib: string; label: string; items: ScanDiffItem[] }> {
  const groups = new Map<string, { lib: string; label: string; items: ScanDiffItem[] }>();
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

export const ScanPanel: React.FC<Props> = ({ onRebuild, onClose }) => {
  const [diff, setDiff] = useState<ScanDiff | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // 初值即「扫描中」：首屏就是一次在途请求，否则会先闪一下空态
  const [scanning, setScanning] = useState(true);
  // 一键毕业（毕业桥子进程）：promoting=轮询中；完成后自动重新扫描
  const [promoting, setPromoting] = useState(false);
  const [promoteStep, setPromoteStep] = useState('');
  const [promoteErr, setPromoteErr] = useState<string | null>(null);

  const scan = useCallback(async () => {
    setScanning(true);
    setErr(null);
    try {
      setDiff(await getScanSources());
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setScanning(false);
    }
  }, []);

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

  return (
    <div className="flex-1 min-h-0 flex items-start justify-center pt-6">
      <Card
        title="扫描因子来源"
        className="w-[760px] max-h-full"
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
        <div className="flex flex-col min-h-0">
          <div
            data-testid="scan-summary"
            className="shrink-0 flex items-center gap-3 flex-wrap rounded-lg bg-slate-50 border border-slate-100 px-3 py-1.5"
          >
            <ScanSearch className="w-3.5 h-3.5 text-slate-400 shrink-0" />
            <Stat label="盘上扫到" value={diff?.discovered_count ?? '—'} />
            <Stat label="快照目录" value={diff?.catalog_count ?? '—'} />
            <Stat label="新增" value={nNew} tone={nNew ? 'text-emerald-600' : 'text-slate-400'} />
            <Stat label="消失" value={nMissing} tone={nMissing ? 'text-amber-600' : 'text-slate-400'} />
            <Stat label="未变" value={diff?.unchanged_count ?? '—'} />
            {diff?.snapshot_at && (
              <span className="text-[10px] text-slate-400">快照建于 {diff.snapshot_at}</span>
            )}
          </div>

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

          <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar mt-2 space-y-2">
            {isLatest && (
              <div className="text-[11px] text-slate-500 py-3 text-center">
                已是最新：盘上的因子与快照目录完全一致，没有需要重算的内容。
              </div>
            )}

            {groups.map((g) => (
              <div
                key={g.lib}
                data-testid={`scan-new-${g.lib}`}
                className="rounded-xl border border-emerald-100 bg-emerald-50/40 px-2.5 py-1.5"
              >
                <div className="flex items-baseline gap-2">
                  <span className="text-[11px] font-bold text-emerald-700">{g.label}</span>
                  <span className="text-[10px] font-mono text-emerald-600">{g.items.length} 个新增</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {g.items.map((it) => (
                    <span
                      key={it.code}
                      className="rounded border border-emerald-100 bg-white px-1.5 py-0.5 text-[10px] font-mono text-slate-600"
                      title={it.code}
                    >
                      {it.code}
                    </span>
                  ))}
                </div>
              </div>
            ))}

            {nMissing > 0 && (
              <div data-testid="scan-missing" className="rounded-xl border border-amber-100 bg-amber-50/40 px-2.5 py-1.5">
                <div className="flex items-baseline gap-2">
                  <span className="text-[11px] font-bold text-amber-700">快照里还在、盘上已找不到</span>
                  <span className="text-[10px] font-mono text-amber-600">{nMissing} 个</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {diff?.missing.map((it) => (
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
