/**
 * 因子报告（Alphalens 式）——因子研究「因子报告」页签（2026-09-17 由技能中心迁入）
 *
 * 三个问题一屏回答：
 *  1) 分位收益：把股票按因子值分 10 组，Q10−Q1 是不是单调、价差多大（区分真因子与噪声）
 *  2) 换手：这组因子每天换掉多少仓位（决定交易成本能否吃得住）
 *  3) 相关性：它是不是别人的复制品（|ρ|>0.9 的因子只该留一个）
 *
 * 数据集可切换：Alpha 库 / L1 / L2 / L1+L2 / 空档挖掘 / RD-Agent 挖掘因子 等
 * （清单与快照状态由后端 /factor-report/datasets 给，页面不写死）。
 *
 * 2026-10-09 版面与流程整改：
 *  - 数据集切换从「右栏顶部胶囊行」收敛到**整页顶栏 + 下拉**（类别多后胶囊排不下）；
 *  - 新增「快照落后」徽章与**重建快照**按钮 —— 快照是构建产物，因子挖掘/数据
 *    更新后不重建就永远看不见；「刷新」只是重读同一份快照。滞后判据（盘上
 *    N 个 vs 快照 M 个）由后端 datasets 接口给出，前端不做口径判断。
 *
 * 数据：backend/services/engine/factor_report（快照 + 预计算序列），
 * 快照由 backend/scripts/build_factor_report.py --dataset <名> 生成（或本页按钮触发）。
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  AlertTriangle,
  Check,
  ChevronDown,
  Hammer,
  Layers,
  RefreshCw,
  SearchX,
  Sparkles,
  Target,
} from 'lucide-react';
import { FactorRankList } from './FactorRankList';
import { FactorClusterModal } from './FactorClusterModal';
import { FactorPortfolioModal } from './FactorPortfolioModal';
import { FactorDetailTabs } from './FactorDetailTabs';
import {
  getFactorCorrelation,
  getFactorDatasets,
  getFactorDetail,
  getFactorRelated,
  getFactorReportBuildStatus,
  getFactorSummary,
  startFactorReportBuild,
} from '../../services/factorReportService';
import type {
  FactorCorrelation,
  FactorDatasetInfo,
  FactorDetail,
  FactorDetailParams,
  FactorRelated,
  FactorReportMeta,
  FactorSummary,
} from '../../types/factorReport';

interface Props {
  /**
   * 深链目标：进入时直接落到某个数据集的某个因子（筛选页的「报告」按钮带过来）。
   *
   * 两个都不传 = 原行为（列全部数据集，自动选第一个可用集的第一个因子）。
   * 调用方应带上 `key`（见 FactorResearchPage）——换了目标就重挂载，
   * 免得「只读一次入参」的约定被一个不卸载的父级悄悄破坏。
   */
  initialDataset?: string;
  initialCode?: string;
}

export const FactorReportPanel: React.FC<Props> = ({ initialDataset, initialCode }) => {
  const [datasets, setDatasets] = useState<FactorDatasetInfo[]>([]);
  const [dataset, setDataset] = useState<string>(() => initialDataset || 'alpha_library');
  /**
   * 深链要看的因子没在快照里时的原样记录。**必须**留空而不是回落到「第一个因子」：
   * 筛选页列的是盘上算出来的因子，报告快照是另一个脚本按自己的清单生成的，两者
   * 会不同步（新挖到的因子就是典型）。回落会让用户点 A 看到 B，而界面一切正常。
   */
  const [missingCode, setMissingCode] = useState<string | null>(null);
  /** 深链目标只对**第一次**装载生效；取用一次即清空。 */
  const pendingCodeRef = useRef<string | null>(initialCode || null);
  /**
   * 摘要请求的序号。`loadSummary` 是这里唯一没有取消保护的请求，而它要写
   * factors / meta / selected / missingCode / unavailable 五处状态。深链落到
   * 一个大快照（慢），用户在它回来之前切了数据集，慢的那份后到、赢：左边列出的
   * 是 A 库的因子，顶栏写着 B 库，明细拿 A 的因子名去问 B 库 —— 而 `want` 落空
   * 还会把 missingCode 说成 B 库没有这个因子。每次发起自增，回来时不等于最新
   * 就直接丢弃。
   */
  const summarySeqRef = useRef(0);
  const [factors, setFactors] = useState<FactorSummary[]>([]);
  const [meta, setMeta] = useState<FactorReportMeta | null>(null);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<FactorDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [related, setRelated] = useState<FactorRelated | null>(null);
  const [correlation, setCorrelation] = useState<FactorCorrelation | null>(null);
  const [corrLoading, setCorrLoading] = useState(false);
  const [clusterOpen, setClusterOpen] = useState(false);
  const [portfolioOpen, setPortfolioOpen] = useState(false);
  // 数据集下拉
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  // 重建快照：buildDataset = 正在重建的目标（null = 空闲）；与当前浏览的 dataset
  // 解耦 —— 重建 X 时用户可以切去看 Y，完成后按实际在看的数据集决定要不要刷新摘要。
  const [buildDataset, setBuildDataset] = useState<string | null>(null);
  const [buildStep, setBuildStep] = useState<string | null>(null);
  const [buildError, setBuildError] = useState<string | null>(null);
  const datasetRef = useRef(dataset);
  // 分组 / 成本 / 基准 —— 后端已把它们纳入缓存键，改了必须重新请求
  const [params, setParams] = useState<FactorDetailParams>({
    longGroup: 3, shortGroup: 9, costBps: 20, bench: '000300.SH',
  });

  useEffect(() => {
    datasetRef.current = dataset;
  }, [dataset]);

  const refreshDatasets = async (): Promise<FactorDatasetInfo[] | null> => {
    try {
      const res = await getFactorDatasets();
      const items = res.items || [];
      setDatasets(items);
      return items;
    } catch {
      return null;
    }
  };

  // 数据集清单（含快照状态 + 滞后探测；未生成的数据集置灰）
  useEffect(() => {
    void (async () => {
      const items = await refreshDatasets();
      const first = (items || []).find((d) => d.available);
      // 深链优先：带了 initialDataset 就不能再改写成「第一个可用集」，
      // 否则用户点的那个因子会被安到一个不相干的数据集上。
      if (first && !initialDataset) setDataset(first.dataset);
    })();
  }, [initialDataset]);

  // 下拉点空白处关闭
  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [menuOpen]);

  // 快照摘要（一次拉全量，前端做筛选/搜索）
  const loadSummary = async (ds: string, pickFirst = false, want: string | null = null) => {
    const seq = (summarySeqRef.current += 1);
    setListLoading(true);
    try {
      const res = await getFactorSummary({ dataset: ds, sort: 'abs_ic' });
      if (seq !== summarySeqRef.current) return; // 已经有更新的请求发出去了，这份作废
      if (!res.available) {
        setUnavailable(res.reason || '快照尚未生成');
        setFactors([]);
        setMeta(null);
        return;
      }
      setUnavailable(null);
      const list = res.factors || [];
      setFactors(list);
      setMeta(res.meta || null);
      if (want) {
        // 点名要哪只就选哪只；不在就记下来，绝不改选别人。
        const hit = list.some((f) => f.name === want);
        setMissingCode(hit ? null : want);
        if (hit) setSelected(want);
      } else if (pickFirst && list.length > 0) {
        // 函数式更新在 tsc 下会报类型错（历史坑），改用传值
        setSelected(list[0].name);
        setMissingCode(null);
      }
      // want 与 pickFirst 都没有 = 「刷新」按钮：既不换选中项，也不动 missingCode，
      // 否则刷新一下就把「没找到」的提示洗掉，用户会以为已经好了。
    } catch (e) {
      if (seq !== summarySeqRef.current) return;
      setUnavailable(e instanceof Error ? e.message : '因子报告加载失败');
    } finally {
      // 只有最新那次才收转圈：过期的先到会把还在路上的新请求转圈关掉。
      if (seq === summarySeqRef.current) setListLoading(false);
    }
  };

  useEffect(() => {
    if (!dataset) return;
    setSelected(null);
    setDetail(null);
    setRelated(null);
    setCorrelation(null);
    const want = pendingCodeRef.current;
    pendingCodeRef.current = null; // 只生效一次
    void loadSummary(dataset, want === null, want);
  }, [dataset]);

  // 选中因子变化 → 明细 + 相关因子 → 相关性矩阵
  // ⚠️ 明细依赖 params：改分组/成本/基准会改变多空曲线本身，必须重新取数（后端已纳入缓存键）
  useEffect(() => {
    if (!selected || !dataset) return;
    let cancelled = false;
    setDetailLoading(true);
    setDetail(null);
    getFactorDetail(selected, dataset, params)
      .then((d) => !cancelled && setDetail(d))
      .catch(() => !cancelled && setDetail(null))
      .finally(() => !cancelled && setDetailLoading(false));

    return () => {
      cancelled = true;
    };
  }, [selected, dataset, params]);

  // 相关性与分组/成本/基准无关 → 单独一个 effect，改参数时不重复拉
  useEffect(() => {
    if (!selected || !dataset) return;
    let cancelled = false;
    setCorrLoading(true);
    getFactorRelated(selected, dataset, 7)
      .then(async (r) => {
        if (cancelled) return;
        setRelated(r);
        const names = [selected, ...r.related.map((x) => x.name)];
        const corr = await getFactorCorrelation(names, dataset);
        if (!cancelled) setCorrelation(corr);
      })
      .catch(() => {
        if (!cancelled) {
          setRelated(null);
          setCorrelation(null);
        }
      })
      .finally(() => !cancelled && setCorrLoading(false));

    return () => {
      cancelled = true;
    };
  }, [selected, dataset]);

  const rebuild = async () => {
    if (buildDataset) return;
    setMenuOpen(false);
    setBuildError(null);
    setBuildStep(null);
    setBuildDataset(dataset);
    try {
      await startFactorReportBuild(dataset);
    } catch (e) {
      setBuildDataset(null);
      setBuildError(e instanceof Error ? e.message : '重建启动失败');
    }
  };

  // 重建轮询：完成后刷新数据集状态；正在浏览的正是重建目标时才重载摘要。
  useEffect(() => {
    if (!buildDataset) return;
    const target = buildDataset;
    let cancelled = false;
    const tick = async () => {
      try {
        const st = await getFactorReportBuildStatus(target);
        if (cancelled) return;
        setBuildStep(st.step || null);
        if (!st.running) {
          setBuildDataset(null);
          await refreshDatasets();
          if (target === datasetRef.current) void loadSummary(target, false);
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
  }, [buildDataset]);

  const current = useMemo(() => factors.find((f) => f.name === selected) || null, [factors, selected]);
  const currentDs = useMemo(() => datasets.find((d) => d.dataset === dataset) || null, [datasets, dataset]);
  // 展示用差值（口径判断在后端 stale/stale_reason；这里只是把数字摆给人看）
  const pendingNew = currentDs?.stale && currentDs.disk_n_factors != null && currentDs.snapshot_n_factors != null
    ? currentDs.disk_n_factors - currentDs.snapshot_n_factors
    : null;

  return (
    <div className="flex flex-col h-full min-h-0 bg-gray-50/40">
      {/* 顶栏：数据集下拉（全宽置顶）+ 快照元信息 + 落后提示 + 操作 */}
      <header className="shrink-0 flex items-center gap-3 border-b border-gray-200 bg-white px-3 py-2">
        <div className="w-8 h-8 shrink-0 rounded-xl bg-gradient-to-br from-indigo-500 to-violet-500 flex items-center justify-center shadow-sm">
          <Sparkles className="w-4 h-4 text-white" />
        </div>

        <div className="relative shrink-0" ref={menuRef}>
          <button
            data-testid="report-dataset-dropdown"
            onClick={() => setMenuOpen((v) => !v)}
            title="切换因子数据集"
            className="flex items-center gap-1.5 rounded-full border border-slate-200 bg-white pl-3 pr-2 py-1 text-[11px] font-bold text-slate-700 hover:border-indigo-300 hover:text-indigo-700"
          >
            {currentDs?.label || dataset}
            {currentDs?.n_factors ? (
              <span className="font-mono font-normal text-slate-400">{currentDs.n_factors}</span>
            ) : null}
            <ChevronDown className={`w-3.5 h-3.5 text-slate-400 transition-transform ${menuOpen ? 'rotate-180' : ''}`} />
          </button>
          {menuOpen && (
            <div
              data-testid="report-dataset-menu"
              className="absolute left-0 top-full z-40 mt-1 w-[380px] max-h-[60vh] overflow-y-auto custom-scrollbar rounded-xl border border-slate-200 bg-white py-1 shadow-xl"
            >
              <div className="px-3 py-1 text-[10px] font-bold text-slate-400">因子数据集</div>
              {(datasets.length
                ? datasets
                : [{ dataset: 'alpha_library', label: 'Alpha 库', available: true } as FactorDatasetInfo]
              ).map((d) => (
                // ⚠️「快照尚未生成」的数据集**必须可点选**，不能 disabled：
                // 重建按钮（顶栏/横幅）的目标 = 当前选中的数据集，禁选等于把
                // 唯一需要重建的那个数据集锁死 —— 想生成快照却点不进去，死路。
                // 选中后主体显示「快照不可用」横幅 + 重建按钮，完成即自动刷新。
                <button
                  key={d.dataset}
                  data-testid={`report-dataset-option-${d.dataset}`}
                  onClick={() => {
                    setDataset(d.dataset);
                    setMenuOpen(false);
                  }}
                  className={`w-full flex items-start gap-2 px-3 py-1.5 text-left transition-colors hover:bg-indigo-50/60 ${
                    d.available ? '' : 'opacity-70'
                  }`}
                >
                  <span className="flex-1 min-w-0">
                    <span className="flex items-center gap-1.5">
                      <span className={`text-[11px] font-bold ${dataset === d.dataset ? 'text-indigo-700' : 'text-slate-700'}`}>
                        {d.label}
                      </span>
                      {d.stale && (
                        <span className="rounded bg-amber-100 px-1 py-px text-[9px] font-bold text-amber-700">
                          快照落后
                        </span>
                      )}
                    </span>
                    <span className="block text-[10px] text-slate-400 truncate">
                      {d.available
                        ? `${d.n_factors ?? '—'} 个因子${d.start ? ` · ${d.start}~${d.end}` : ''}${d.stale_reason ? ` · ${d.stale_reason}` : ''}`
                        : `快照尚未生成 · 选中后点「重建快照」${d.disk_n_factors ? `（盘上已有 ${d.disk_n_factors} 个因子）` : ''}`}
                    </span>
                  </span>
                  {dataset === d.dataset && <Check className="w-3.5 h-3.5 text-indigo-600 mt-0.5 shrink-0" />}
                </button>
              ))}
            </div>
          )}
        </div>

        <span className="flex-1 min-w-0 truncate text-[10px] text-slate-400 font-mono">
          {meta
            ? `${meta.universe} · ${meta.horizon.replace('fwd_ret_', 'T+')} 前瞻 · ${meta.start}~${meta.end} · ${meta.n_dates} 个交易日 · 快照 ${meta.generated_at}`
            : unavailable
              ? '快照尚未生成'
              : '加载中…'}
        </span>

        {currentDs?.stale && !buildDataset && (
          <button
            data-testid="report-stale-badge"
            onClick={() => void rebuild()}
            title={`${currentDs.stale_reason || '盘上数据比快照新'} —— 点此重建快照`}
            className="shrink-0 flex items-center gap-1 rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-[10px] font-bold text-amber-700 hover:bg-amber-100"
          >
            <AlertTriangle className="w-3 h-3" />
            {pendingNew && pendingNew > 0 ? `盘上 +${pendingNew} 个因子未进快照` : '盘上数据比快照新'}
          </button>
        )}
        {buildDataset && (
          <span
            data-testid="report-build-step"
            className="shrink-0 max-w-[280px] truncate text-[10px] text-indigo-500"
            title={buildStep || ''}
          >
            重建 {datasets.find((d) => d.dataset === buildDataset)?.label || buildDataset} 中… {buildStep || ''}
          </span>
        )}
        {buildError && <span className="shrink-0 text-[10px] text-rose-500">{buildError}</span>}

        <div className="shrink-0 flex items-center gap-2">
          <button
            onClick={() => setPortfolioOpen(true)}
            className="flex items-center gap-1.5 rounded-full bg-emerald-600 px-3 py-1 text-[11px] font-bold text-white shadow-sm hover:bg-emerald-500 active:scale-95"
            title="按 ICIR 门槛与相关性去重，给出推荐因子集与权重（仅本页计算，不写入训练目录）"
          >
            <Target className="w-3 h-3" />
            组合构建
          </button>
          <button
            onClick={() => setClusterOpen(true)}
            className="flex items-center gap-1.5 rounded-full bg-indigo-600 px-3 py-1 text-[11px] font-bold text-white shadow-sm hover:bg-indigo-500 active:scale-95"
            title="按相关性找同源因子簇，每簇只留一个代表（含 PDF 报告）"
          >
            <Layers className="w-3 h-3" />
            因子去重
          </button>
          <button
            onClick={() => void loadSummary(dataset, false)}
            className="flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-bold text-slate-500 hover:text-indigo-600 hover:border-indigo-200"
            title="重新读取快照（不重算；盘上新增的因子要先重建快照才会进来）"
          >
            <RefreshCw className={`w-3 h-3 ${listLoading ? 'animate-spin' : ''}`} />
            刷新
          </button>
          <button
            data-testid="report-rebuild"
            onClick={() => void rebuild()}
            disabled={!!buildDataset}
            className={`flex items-center gap-1.5 rounded-full px-3 py-1 text-[11px] font-bold shadow-sm active:scale-95 disabled:opacity-50 ${
              currentDs?.stale
                ? 'bg-amber-500 text-white hover:bg-amber-400'
                : 'border border-slate-200 bg-white text-slate-500 hover:text-indigo-600 hover:border-indigo-200'
            }`}
            title="让服务器重建本数据集的报告快照（后台运行、完成后自动刷新；盘上新增的因子与最新数据将进入报告）"
          >
            <Hammer className={`w-3 h-3 ${buildDataset ? 'animate-pulse' : ''}`} />
            {buildDataset ? '重建中…' : '重建快照'}
          </button>
        </div>
      </header>

      {/* 主体：左因子榜 + 右详情 */}
      <div className="flex flex-1 min-h-0">
        <aside className="w-[280px] shrink-0 border-r border-gray-200 bg-white flex flex-col min-h-0">
          <FactorRankList
            factors={factors}
            selected={selected}
            onSelect={setSelected}
            loading={listLoading}
          />
        </aside>

        <main className="flex-1 min-w-0 flex flex-col gap-3 p-3 min-h-0">
          {unavailable ? (
            <div className="flex-1 min-h-0 flex flex-col items-center justify-center gap-2 rounded-2xl border border-dashed border-amber-200 bg-amber-50/50 text-center px-6">
              <AlertCircle className="w-5 h-5 text-amber-500" />
              <span className="text-xs font-bold text-amber-700">因子报告快照不可用</span>
              <span className="text-[11px] text-amber-600/90 leading-5 max-w-xl">{unavailable}</span>
              <button
                data-testid="report-unavailable-rebuild"
                onClick={() => void rebuild()}
                disabled={!!buildDataset}
                className="mt-1 flex items-center gap-1.5 rounded-full bg-amber-500 px-3 py-1 text-[11px] font-bold text-white hover:bg-amber-400 disabled:opacity-50"
              >
                <Hammer className="w-3 h-3" />
                {buildDataset ? '重建中…' : '重建快照'}
              </button>
            </div>
            // 带上 `!selected`：横幅只解释「深链那个没找到」，一旦用户自己从左栏挑了
            // 一个因子，就得让位给明细。少了这个条件就是一条死路 —— 挑了因子、
            // 明细也取回来了，屏幕上却还是那条横幅，点刷新也不消失（刷新刻意不动
            // missingCode），看起来像整个面板卡死了。
          ) : missingCode && !selected ? (
            <div className="flex-1 min-h-0 flex flex-col items-center justify-center gap-2 rounded-2xl border border-dashed border-slate-300 bg-white/60 text-center px-6">
              <SearchX className="w-5 h-5 text-slate-400" />
              <span className="text-xs font-bold text-slate-600">
                该因子不在「{currentDs?.label || dataset}」的报告快照里
              </span>
              <span className="text-[11px] text-slate-400 leading-5 max-w-xl">
                <code className="font-mono text-slate-500">{missingCode}</code> 在盘上算得出来（所以出现在筛选清单里），
                但这个数据集的报告快照是另一个脚本按自己的清单生成的，两者会不同步。换个数据集，或等快照重建后再来。
              </span>
            </div>
          ) : !selected ? (
            <div className="flex-1 min-h-0 flex items-center justify-center rounded-2xl border border-dashed border-slate-200 bg-white/60">
              <span className="text-xs text-slate-400">请选择一个因子查看机构级报告</span>
            </div>
          ) : (
            <FactorDetailTabs
              factor={selected}
              dataset={dataset}
              summary={current}
              library={factors}
              detail={detail}
              loading={detailLoading}
              params={params}
              onParams={setParams}
              correlation={correlation}
              related={related}
              corrLoading={corrLoading}
              onPick={setSelected}
            />
          )}
        </main>
      </div>

      <FactorPortfolioModal
        open={portfolioOpen}
        dataset={dataset}
        datasetLabel={currentDs?.label || dataset}
        onClose={() => setPortfolioOpen(false)}
        onPick={(f) => setSelected(f)}
      />

      <FactorClusterModal
        open={clusterOpen}
        dataset={dataset}
        datasetLabel={currentDs?.label || dataset}
        onClose={() => setClusterOpen(false)}
        onPick={(f) => setSelected(f)}
      />
    </div>
  );
};
