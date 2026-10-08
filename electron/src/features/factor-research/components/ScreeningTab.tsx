/**
 * 因子研究 —— 筛选页签：质量门槛 + 同源去重的保留清单（训练特征选择用）。
 *
 * 这个页签与页面其余部分有一处**根本不同**：它列的是盘上算出来的因子
 * （`backend/scripts/screen_factors.py` 扫特征库），而排行榜/单因子分析/报告
 * 都活在快照目录的 code 空间里。两套命名空间实测是对得上的，但不是同一份数据：
 * 362 个保留因子里 331 个落在私人库目录、31 个落在经典库目录、0 个重叠。
 * 所以「点进去看」和「注册进训练」都必须先问一句「这个名字在目录里是谁」，
 * 而答案由页面建好索引传进来（页面才拿得到两份目录）。
 *
 * 三条不能省的取舍：
 *
 * 1. **勾选框只对「真能注册」的行可用**。注册的落库端点是按私人库的
 *    `factors_meta` 解析来源库的，经典库那份是 None 且 l2 是「动量」这类中文
 *    字面量，过不了标识符校验——放行经典库的行，用户点下去只会拿到一屏 skipped。
 *    这与排行榜页 `canRegister = isAdmin && dataset === 'private'` 是同一条口径。
 * 2. **报告按钮只在快照里真有这个数据集时才渲染**。因子报告的快照是另一个脚本
 *    按自己的清单生成的，`factor_research` 子库在那边压根不存在。
 * 3. **找不到落点的行照常显示**，只是不可点、置灰并说明原因——它们确实通过了
 *    筛选，平白消失会让人以为筛选结果少算了。
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  Check,
  CheckSquare,
  Copy,
  Filter,
  LineChart,
  MinusSquare,
  ShieldAlert,
  Square,
} from 'lucide-react';
import { getScreening } from '../services/factorResearchService';
import { getFactorDatasets } from '../services/factorReportService';
import type { FactorDataset } from '../services/factorResearchService';
import type { ScreeningResponse } from '../types/factorResearch';
import { Card, fmtNum } from './common';

/** 筛选清单里的名字在快照目录里的落点。 */
export interface FactorLocation {
  dataset: FactorDataset;
  /** 目录里的 code。实测与筛选名逐字相同，但两份数据各自生成，不假定相等。 */
  code: string;
}

interface Props {
  /** 筛选名 → 目录落点。页面按两个数据集建的索引；查不到 = 快照里还没有它。 */
  index: Map<string, FactorLocation>;
  /** 已勾选（恒为私人库 code，见文件头第 1 条）。 */
  selected: string[];
  onToggle: (code: string) => void;
  /** 整批勾选/取消（表头全选、清空）。select=false 表示从选中集合里移除这些。 */
  onToggleMany: (codes: string[], select: boolean) => void;
  /** 点行进单因子分析；需要换数据集时由页面负责。 */
  onOpenSingle: (code: string, dataset: FactorDataset) => void;
  /** 点「报告」进因子报告页签。 */
  onOpenReport: (reportDataset: string, code: string) => void;
  /** 是否具备注册能力（管理员）。行是否可勾还要看它自己落在哪个数据集。 */
  canRegisterToTraining: boolean;
  onRegister: () => void;
  /**
   * 建索引用的那两份目录的可信度。点不进去的行有两种截然不同的成因，
   * 界面必须分开说 —— 详见下面 `unreachableHint`。
   */
  catalogStatus: 'loading' | 'degraded' | 'ready';
}

/** 子库显示名。因子报告那边的数据集标签更完整，但只在能对上时才拿得到。 */
const LIB_LABEL: Record<string, string> = {
  alpha_library: 'Alpha 库',
  l1_factors: 'L1 因子',
  l2_factors: 'L2 因子',
  jq110: 'JQ110',
  alpha360: 'Alpha360',
  tdxgs: '通达信公式',
  factor_research: '因子研究',
  factor_defs: '因子清单库',
  // 早期子库名，保留以免历史快照显示成裸标识符
  alpha101: 'Alpha101',
  gtja191: 'GTJA191',
  alpha158: 'Alpha158',
};

const DATASET_LABEL: Record<FactorDataset, string> = { classic: '经典库', private: '私人库' };

type CatalogStatus = 'loading' | 'degraded' | 'ready';

/**
 * 「这行点不进去」的三种成因，说法必须分开：
 * - loading：目录还在路上（刚进页签那一两秒，私人库目录 1.1 MB），等它一下就好；
 * - degraded：目录没取回来（引擎那一下没答理），该重试，**不**该去重算快照；
 * - ready：目录到手了、里面确实没这个名字，那才是「新挖到但没重算进目录」，
 *   这时才该去点「快照」。把最后这条说给前两种听，等于指人去跑十分钟的错路。
 */
const unreachableHint = (status: CatalogStatus): string => {
  if (status === 'loading') return '正在读取因子目录，稍候…';
  if (status === 'degraded') return '因子目录没取回来，暂时定位不到它属于哪个库。切走再回到本页签会重试。';
  return '该因子不在快照目录里（新挖到但还没重算快照）。可在右上角「快照」重算后再来。';
};

export const ScreeningTab: React.FC<Props> = ({
  index,
  selected,
  onToggle,
  onToggleMany,
  onOpenSingle,
  onOpenReport,
  canRegisterToTraining,
  onRegister,
  catalogStatus,
}) => {
  const [data, setData] = useState<ScreeningResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lib, setLib] = useState<string>('全部');
  const [dropView, setDropView] = useState<'dup' | 'gate'>('dup');
  const [copied, setCopied] = useState(false);
  /** 因子报告那边**有快照**的数据集。没有它就不知道该不该给某行渲染「报告」。 */
  const [reportDatasets, setReportDatasets] = useState<Set<string>>(new Set());

  useEffect(() => {
    let alive = true;
    getScreening()
      .then((d) => { if (alive) setData(d); })
      .catch((e: unknown) => { if (alive) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);

  // 报告数据集清单。失败就当作「没有」——少几个跳转按钮，好过渲染一个点了会
  // 落到别的因子上的入口。
  useEffect(() => {
    let alive = true;
    getFactorDatasets()
      .then((res) => {
        if (!alive) return;
        setReportDatasets(new Set((res.items || []).filter((d) => d.available).map((d) => d.dataset)));
      })
      .catch(() => undefined);
    return () => { alive = false; };
  }, []);

  const libOptions = useMemo(() => {
    const s = new Set((data?.kept || []).map((k) => k.library));
    return ['全部', ...Array.from(s)];
  }, [data]);

  const keptView = useMemo(() => {
    const rows = data?.kept || [];
    return lib === '全部' ? rows : rows.filter((r) => r.library === lib);
  }, [data, lib]);

  /** 该行能不能被勾进「注册到训练目录」：必须是管理员，且这一行落在私人库里。 */
  const isRegistrable = useMemo(
    () => (name: string) =>
      canRegisterToTraining && index.get(name)?.dataset === 'private',
    [canRegisterToTraining, index],
  );

  const registrableCodes = useMemo(
    () => keptView.filter((r) => isRegistrable(r.name)).map((r) => r.name),
    [keptView, isRegistrable],
  );
  const selectedInView = registrableCodes.filter((c) => selected.includes(c)).length;
  const allInViewSelected = registrableCodes.length > 0 && selectedInView === registrableCodes.length;
  const someInViewSelected = selectedInView > 0 && !allInViewSelected;

  const copyKept = async () => {
    const text = keptView.map((r) => r.name).join('\n');
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* 剪贴板不可用时忽略 */
    }
  };

  if (loading) return <div className="flex-1 rounded-2xl bg-slate-50 animate-pulse" />;
  if (error) return <div className="flex-1 flex items-center justify-center text-xs text-rose-500">{error}</div>;
  if (!data || !data.counts?.total_considered) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-2 text-slate-400">
        <Filter className="w-8 h-8 text-slate-200" />
        <span className="text-xs">暂无筛选结果</span>
        <span className="text-[10px] text-slate-300">
          运行 <code className="font-mono">python3 backend/scripts/screen_factors.py</code> 生成
        </span>
      </div>
    );
  }

  const c = data.counts;
  const tiles = [
    { label: '参与筛选', value: c.total_considered, tone: 'text-slate-700' },
    { label: '过门槛候选', value: c.candidates, tone: 'text-slate-700' },
    { label: '最终保留', value: c.kept, tone: 'text-rose-600' },
    { label: '门槛剔除', value: c.gated_out, tone: 'text-slate-400' },
    { label: '去重剔除', value: c.deduped, tone: 'text-amber-600' },
  ];

  return (
    <div className="flex-1 min-h-0 flex flex-col gap-2">
      {/* 门槛与统计 */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="flex items-center gap-1.5 rounded-full bg-slate-100 border border-slate-200 px-3 py-1 text-[11px] font-bold text-slate-500">
          <ShieldAlert className="w-3 h-3 text-indigo-500" />
          |IC| ≥ {data.gates?.min_abs_ic} · |ICIR| ≥ {data.gates?.min_abs_icir} · 去重 |ρ| ≥ {data.gates?.corr_threshold}
        </div>
        {tiles.map((t) => (
          <span key={t.label} className="inline-flex items-center gap-1.5 rounded-full bg-white border border-slate-200 px-3 py-1 text-[11px] font-bold text-slate-500">
            {t.label}
            <b className={`font-mono ${t.tone}`}>{t.value}</b>
          </span>
        ))}
        <div className="flex-1" />
        <span className="text-[10px] text-slate-400">
          {data.cross_corr} · {data.generated_at ? `生成于 ${String(data.generated_at).slice(0, 16).replace('T', ' ')}` : ''}
        </span>
      </div>

      <div className="flex-1 min-h-0 flex gap-2">
        {/* 保留清单 */}
        <Card
          title={`保留清单（${keptView.length}）· 已去重，可直接进训练特征`}
          className="flex-1"
          extra={
            <div className="flex items-center gap-1.5">
              <div className="flex items-center gap-1 rounded-full bg-slate-100 border border-slate-200 p-0.5">
                {libOptions.map((o) => (
                  <button
                    key={o}
                    onClick={() => setLib(o)}
                    className={`rounded-full px-2.5 py-[2px] text-[10px] font-bold transition-colors ${
                      lib === o ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500 hover:text-slate-700'
                    }`}
                  >
                    {LIB_LABEL[o] || o}
                  </button>
                ))}
              </div>
              <button
                onClick={copyKept}
                className="flex items-center gap-1 rounded-full border border-blue-200 bg-blue-50 px-2.5 py-[3px] text-[10px] font-bold text-blue-600 hover:bg-blue-100"
              >
                {copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                {copied ? '已复制' : `复制 ${keptView.length} 个特征名`}
              </button>
            </div>
          }
        >
          {/* Card 只给一个 flex-1 的容器（不是 flex-col），所以工具条与表格必须
              自己包成一根纵向 flex：否则表格的 h-full 从容器顶量起，会把工具条
              那段高度顶到容器外，表格底部被裁掉。 */}
          <div className="h-full flex flex-col min-h-0">
            {/* 选中态工具条：只在管理员下出现——勾选在这个页签里唯一的用途就是注册 */}
            {canRegisterToTraining && (
              <div className="shrink-0 flex items-center gap-2 px-1 pb-1.5 mb-1 border-b border-slate-100">
                <span className="text-[10px] text-slate-400">
                  勾选可注册的因子（私人库）→ 写入训练目录草稿
                </span>
                <div className="flex-1" />
                {selected.length > 0 && (
                  <span className="text-[10px] font-bold text-indigo-600">已选 {selected.length}</span>
                )}
                <button
                  onClick={() => onToggleMany(registrableCodes, !allInViewSelected)}
                  disabled={registrableCodes.length === 0}
                  className="rounded-full border border-slate-200 bg-white px-2.5 py-[2px] text-[10px] font-bold text-slate-500 hover:bg-slate-50 disabled:opacity-40"
                >
                  {allInViewSelected ? '取消本页全选' : `全选可注册（${registrableCodes.length}）`}
                </button>
                {selected.length > 0 && (
                  <button
                    onClick={() => onToggleMany(selected, false)}
                    className="rounded-full border border-slate-200 bg-white px-2.5 py-[2px] text-[10px] font-bold text-slate-500 hover:bg-slate-50"
                  >
                    清空
                  </button>
                )}
                <button
                  onClick={onRegister}
                  disabled={selected.length === 0}
                  className="rounded-full bg-slate-900 px-3 py-[3px] text-[10px] font-bold text-white hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  注册到训练目录（{selected.length}）
                </button>
              </div>
            )}

            <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar">
              <table className="w-full text-[11px]">
                <thead className="sticky top-0 bg-white z-10">
                  <tr className="text-slate-400 font-bold">
                    {canRegisterToTraining && (
                      <th className="text-left py-1.5 w-7 pl-1">
                        <button
                          onClick={() => onToggleMany(registrableCodes, !allInViewSelected)}
                          disabled={registrableCodes.length === 0}
                          aria-label={allInViewSelected ? '取消全选' : '全选可注册的因子'}
                          title={allInViewSelected ? '取消全选' : '全选本页可注册的因子'}
                          className="align-middle disabled:opacity-40"
                        >
                          {allInViewSelected ? (
                            <CheckSquare className="w-3.5 h-3.5 text-blue-600" />
                          ) : someInViewSelected ? (
                            <MinusSquare className="w-3.5 h-3.5 text-blue-400" />
                          ) : (
                            <Square className="w-3.5 h-3.5 text-slate-300" />
                          )}
                        </button>
                      </th>
                    )}
                    <th className="text-left py-1.5 w-8">#</th>
                    <th className="text-left py-1.5 w-40">因子</th>
                    <th className="text-left py-1.5 w-24">子库</th>
                    <th className="text-left py-1.5">说明</th>
                    <th className="text-right py-1.5 w-16">|IC|</th>
                    <th className="text-right py-1.5 w-16">|ICIR|</th>
                    <th className="text-right py-1.5 w-12">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {keptView.map((r, i) => {
                    const loc = index.get(r.name);
                    const checked = selected.includes(r.name);
                    const registrable = isRegistrable(r.name);
                    // 快照里没有它 → 既点不进单因子分析，也没有报告可看。
                    const unreachable = !loc;
                    const reportOk = !!reportDatasets.has(r.library) && !unreachable;
                    return (
                      <tr
                        key={r.name}
                        onClick={unreachable ? undefined : () => onOpenSingle(loc.code, loc.dataset)}
                        title={
                          unreachable
                            ? unreachableHint(catalogStatus)
                            : `打开 ${r.name} 的单因子分析（${DATASET_LABEL[loc.dataset]}）`
                        }
                        className={`border-t border-slate-100 ${
                          unreachable ? 'opacity-60' : 'cursor-pointer hover:bg-slate-50/70'
                        } ${checked ? 'bg-blue-50/50' : ''}`}
                      >
                        {canRegisterToTraining && (
                          <td
                            className="py-1.5 pl-1"
                            onClick={(e) => {
                              e.stopPropagation();
                              if (registrable) onToggle(r.name);
                            }}
                          >
                            <span
                              title={
                                registrable
                                  ? '加入注册清单'
                                  : loc?.dataset === 'classic'
                                    ? '经典因子库不支持注册：后端没有 classic 目录，且来源库名过不了标识符校验'
                                    : unreachable
                                      // 带前缀是为了与**整行**的提示区分开：两者正文
                                      // 相同，同一个 title 挂两处，找元素的人（和
                                      // 读屏的人）都要猜是哪一个。
                                      ? `无法勾选｜${unreachableHint(catalogStatus)}`
                                      : '仅管理员可注册'
                              }
                              className={registrable ? 'cursor-pointer' : 'cursor-not-allowed'}
                            >
                              {checked ? (
                                <CheckSquare className="w-3.5 h-3.5 text-blue-600" />
                              ) : (
                                <Square className={`w-3.5 h-3.5 ${registrable ? 'text-slate-300' : 'text-slate-200'}`} />
                              )}
                            </span>
                          </td>
                        )}
                        <td className="py-1.5 font-mono text-slate-400">{i + 1}</td>
                        <td className="py-1.5">
                          <div className="flex items-center gap-1">
                            <span className="font-mono font-bold text-slate-700 truncate" title={r.name}>
                              {r.name}
                            </span>
                            {loc && (
                              <span
                                className={`shrink-0 rounded-full border px-1.5 py-[1px] text-[9px] font-bold ${
                                  loc.dataset === 'private'
                                    ? 'border-indigo-100 bg-indigo-50 text-indigo-500'
                                    : 'border-slate-200 bg-slate-50 text-slate-400'
                                }`}
                              >
                                {DATASET_LABEL[loc.dataset]}
                              </span>
                            )}
                          </div>
                        </td>
                        <td className="py-1.5 text-[10px] text-slate-400">{LIB_LABEL[r.library] || r.library}</td>
                        <td className="py-1.5 text-slate-500 text-[10px] truncate max-w-[220px]">{r.display_name}</td>
                        <td className="py-1.5 text-right font-mono text-slate-600">{fmtNum(Math.abs(r.ic_mean || 0), 4)}</td>
                        <td className="py-1.5 text-right font-mono font-bold text-slate-700">{fmtNum(Math.abs(r.icir || 0), 3)}</td>
                        <td className="py-1.5 text-right">
                          {reportOk && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                onOpenReport(r.library, r.name);
                              }}
                              title={`在因子报告里打开 ${r.name}`}
                              aria-label={`在因子报告里打开 ${r.name}`}
                              className="rounded-lg p-1 text-slate-300 hover:bg-indigo-50 hover:text-indigo-600"
                            >
                              <LineChart className="w-3.5 h-3.5" />
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </Card>

        {/* 剔除明细 */}
        <div className="w-[380px] shrink-0 flex flex-col bg-white rounded-2xl border border-slate-200/80 shadow-sm overflow-hidden">
          <div className="px-3 py-2 border-b border-slate-100 flex items-center gap-1.5">
            <div className="flex items-center gap-1 rounded-full bg-slate-100 border border-slate-200 p-0.5">
              <button
                onClick={() => setDropView('dup')}
                className={`rounded-full px-2.5 py-[2px] text-[10px] font-bold transition-colors ${dropView === 'dup' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500'}`}
              >
                同源去重（{data.dropped_duplicate.length}）
              </button>
              <button
                onClick={() => setDropView('gate')}
                className={`rounded-full px-2.5 py-[2px] text-[10px] font-bold transition-colors ${dropView === 'gate' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500'}`}
              >
                门槛剔除（{data.dropped_gated.length}）
              </button>
            </div>
          </div>
          <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar p-2 space-y-0.5">
            {dropView === 'dup'
              ? data.dropped_duplicate.map((d, i) => (
                  <div key={`${d.name}-${i}`} className="flex items-center justify-between px-2 py-1 rounded-lg hover:bg-slate-50">
                    <span className="text-[11px] font-mono text-slate-500 truncate">{d.name}</span>
                    <span className="text-[10px] text-slate-400 shrink-0 ml-2">
                      → <b className="font-mono text-slate-600">{d.duplicate_of}</b>
                      <span className="ml-1 rounded bg-amber-50 border border-amber-100 px-1 py-[1px] font-mono text-amber-600">ρ={d.abs_corr.toFixed(2)}</span>
                    </span>
                  </div>
                ))
              : data.dropped_gated.map((d) => (
                  <div key={d.name} className="flex items-center justify-between px-2 py-1 rounded-lg hover:bg-slate-50">
                    <span className="text-[11px] font-mono text-slate-500 truncate">{d.name}</span>
                    <span className="text-[10px] text-slate-400 shrink-0 ml-2">{d.reason}</span>
                  </div>
                ))}
          </div>
          <div className="px-3 py-1.5 border-t border-slate-100 text-[10px] text-slate-400">
            去重为并查集聚类（传递闭包）：同簇只留 |ICIR| 最强者；ρ 列为与保留代表的直接相关
          </div>
        </div>
      </div>
    </div>
  );
};
