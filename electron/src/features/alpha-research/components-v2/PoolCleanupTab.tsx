/**
 * 因子池「清理建议」tab（P3）：只建议不自动删。
 *
 * 数据纪律：
 * - 每条建议必须带着**数字证据**（后端 pool_cleanup 生成，detail 原样展示），
 *   用户看完判据自己决定归档哪些；界面绝不自动勾选、绝不提供「一键全删」；
 * - 归档 = 时间戳（非删除）：打标后退出注入/列表/谱系图，随时可恢复；
 * - 「已归档」计数用 report.archivedCount（全量），列表按 updated_at 取前
 *   500 行过滤出归档行——归档动作会刷新 updated_at，最近归档必在头部；
 * - 阈值缺失显示 `—`（MISSING_METRIC_TEXT），绝不做 0 渲染。
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from './ui/Card';
import { Button } from './ui/Button';
import { MISSING_METRIC_TEXT } from '../services-v2/metricRegistry';
import {
  archivePoolFactors,
  getPoolCleanupSuggestions,
  getPoolFactors,
  unarchivePoolFactors,
  type PoolCleanupReport,
  type PoolCleanupSuggestion,
  type PoolFactorRow,
} from '../services-v2/api';
import { Archive, ArchiveRestore, RefreshCw } from 'lucide-react';

const ARCHIVED_FETCH_LIMIT = 500;

const SEVERITY_STYLE: Record<string, string> = {
  high: 'bg-red-500/15 text-red-600 border-red-500/30',
  medium: 'bg-amber-500/15 text-amber-600 border-amber-500/30',
};

const REASON_STYLE: Record<string, string> = {
  duplicate: 'bg-violet-500/15 text-violet-600 border-violet-500/30',
  weak_icir: 'bg-amber-500/15 text-amber-600 border-amber-500/30',
  no_diversity: 'bg-slate-400/15 text-slate-500 border-slate-400/30',
};

const REASON_FALLBACK_LABEL: Record<string, string> = {
  duplicate: '冗余被支配',
  weak_icir: '预测力垫底',
  no_diversity: '零多样性贡献',
};

function fmtNum(value: number | null | undefined, digits = 3): string {
  return value == null || !Number.isFinite(value)
    ? MISSING_METRIC_TEXT
    : value.toFixed(digits);
}

function fmtTime(value: string | null | undefined): string {
  if (!value) return MISSING_METRIC_TEXT;
  const dt = new Date(value);
  return Number.isNaN(dt.getTime()) ? String(value) : dt.toLocaleString('zh-CN');
}

const severityLabel = (severity: string): string =>
  severity === 'high' ? '高' : '中';

const SeverityBadge: React.FC<{ severity: string }> = ({ severity }) => (
  <span
    className={`inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] font-bold ${
      SEVERITY_STYLE[severity] ?? SEVERITY_STYLE.medium
    }`}
    title={severity === 'high' ? '高：冗余被支配，或两条判据同时命中' : '中：单条软判据命中'}
  >
    {severityLabel(severity)}
  </span>
);

const CriteriaCard: React.FC<{ report: PoolCleanupReport }> = ({ report }) => {
  const { criteria, sota } = report;
  return (
    <Card className="glass">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">清理判据（只建议，不自动删）</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-xs">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div className="rounded-lg bg-secondary/30 p-3 space-y-1">
            <div className="font-medium text-foreground">冗余被支配</div>
            <p className="text-muted-foreground">
              与池内某因子 |ρ| ≥ {criteria.corrDup.toFixed(2)} 且对方更强
              （ICIR 优先，缺失时比池评分）；平手或无可比指标不判。
            </p>
          </div>
          <div className="rounded-lg bg-secondary/30 p-3 space-y-1">
            <div className="font-medium text-foreground">预测力垫底</div>
            <p className="text-muted-foreground">
              ICIR ≤ 池内后 {(criteria.weakIcirQuantile * 100).toFixed(0)}% 分位
              （阈值 {fmtNum(criteria.weakIcirThreshold)}，样本{' '}
              {criteria.icirSampleSize}，少于 {criteria.minIcirSample} 不判）；
              缺失 ≠ 弱。
            </p>
          </div>
          <div className="rounded-lg bg-secondary/30 p-3 space-y-1">
            <div className="font-medium text-foreground">零多样性贡献</div>
            <p className="text-muted-foreground">
              留一贡献 ≤ 0：移除后池有效因子数不降；未算过（无池刷新）不判。
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-muted-foreground">
          <span>
            评估范围：<span className="font-mono text-foreground">{report.poolSize}</span> 个活跃因子
          </span>
          <span>
            池 SOTA 标杆（{sota.count} 条）：IC{' '}
            <span className="font-mono text-foreground">{fmtNum(sota.bestIc, 4)}</span> · ICIR{' '}
            <span className="font-mono text-foreground">{fmtNum(sota.bestIcir)}</span> · PFS{' '}
            <span className="font-mono text-foreground">{fmtNum(sota.bestPfs)}</span>
          </span>
          <span>归档 = 打时间戳（非删除），随时可恢复</span>
        </div>
      </CardContent>
    </Card>
  );
};

interface PoolCleanupTabProps {
  market: string;
  universe: string;
}

export const PoolCleanupTab: React.FC<PoolCleanupTabProps> = ({ market, universe }) => {
  const [report, setReport] = useState<PoolCleanupReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [archivedRows, setArchivedRows] = useState<PoolFactorRow[]>([]);

  const loadReport = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getPoolCleanupSuggestions({ market, universe, limit: 100 });
      if (res.success && res.data) {
        setReport(res.data);
        setError(null);
      } else {
        setError(res.error ?? '清理建议加载失败');
      }
    } finally {
      setLoading(false);
    }
  }, [market, universe]);

  const loadArchived = useCallback(async () => {
    const res = await getPoolFactors({
      market,
      universe,
      limit: ARCHIVED_FETCH_LIMIT,
      offset: 0,
      sort: 'updated_at',
      includeArchived: true,
    });
    if (res.success && res.data) {
      setArchivedRows(res.data.items.filter((r) => r.archivedAt != null));
    }
  }, [market, universe]);

  useEffect(() => {
    setSelected(new Set());
    setNotice(null);
    loadReport();
    loadArchived();
  }, [loadReport, loadArchived]);

  const items = report?.items ?? [];
  const allSelected = items.length > 0 && items.every((it) => selected.has(it.factorId));

  const toggleAll = useCallback(() => {
    setSelected(allSelected ? new Set() : new Set(items.map((it) => it.factorId)));
  }, [allSelected, items]);

  const toggleOne = useCallback((factorId: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(factorId)) next.delete(factorId);
      else next.add(factorId);
      return next;
    });
  }, []);

  const handleArchive = useCallback(async () => {
    const ids = [...selected];
    if (ids.length === 0) return;
    const ok = window.confirm(
      `归档所选 ${ids.length} 个因子？\n\n归档只打时间戳、不删除数据：归档后不再参与提示词注入、池列表与谱系图，随时可在此页恢复。`,
    );
    if (!ok) return;
    setBusy(true);
    setNotice(null);
    try {
      const res = await archivePoolFactors(ids);
      if (res.success && res.data) {
        const skipped = res.data.skipped.length;
        setNotice(
          `已归档 ${res.data.archived} 个${skipped > 0 ? `；跳过 ${skipped} 个（不在池/非本人/已归档）` : ''}`,
        );
        setSelected(new Set());
        await Promise.all([loadReport(), loadArchived()]);
      } else {
        setNotice(res.error ?? '归档失败');
      }
    } finally {
      setBusy(false);
    }
  }, [selected, loadReport, loadArchived]);

  const handleRestoreOne = useCallback(
    async (factorId: string) => {
      setBusy(true);
      setNotice(null);
      try {
        const res = await unarchivePoolFactors([factorId]);
        if (res.success && res.data) {
          setNotice(
            res.data.restored > 0 ? '已恢复，重新参与注入与池视图' : '恢复失败（不在池或非本人）',
          );
          await Promise.all([loadReport(), loadArchived()]);
        } else {
          setNotice(res.error ?? '恢复失败');
        }
      } finally {
        setBusy(false);
      }
    },
    [loadReport, loadArchived],
  );

  const summaryChips = useMemo(() => {
    const summary = report?.summary ?? {};
    return Object.entries(summary).filter(([, v]) => v > 0);
  }, [report]);

  return (
    <div className="space-y-4">
      {error ? (
        <Card className="glass">
          <CardContent className="p-6 text-sm text-destructive">{error}</CardContent>
        </Card>
      ) : (
        <>
          {report && <CriteriaCard report={report} />}

          <Card className="glass">
            <CardHeader className="pb-2">
              <div className="flex flex-wrap items-center gap-2">
                <CardTitle className="text-sm mr-auto">
                  清理建议{report ? `（全量 ${report.total}，列出前 ${items.length}）` : ''}
                </CardTitle>
                {summaryChips.map(([code, count]) => (
                  <span
                    key={code}
                    className={`inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${
                      REASON_STYLE[code] ?? REASON_STYLE.no_diversity
                    }`}
                  >
                    {REASON_FALLBACK_LABEL[code] ?? code} × {count}
                  </span>
                ))}
                <Button variant="ghost" size="sm" disabled={loading} onClick={loadReport}>
                  <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  disabled={busy || selected.size === 0}
                  onClick={handleArchive}
                >
                  <Archive className="h-3.5 w-3.5 mr-1" /> 归档所选（{selected.size}）
                </Button>
              </div>
              {notice && <div className="text-xs text-primary font-medium">{notice}</div>}
            </CardHeader>
            <CardContent>
              {items.length === 0 ? (
                <div className="p-8 text-center text-sm text-muted-foreground">
                  {report
                    ? '池内没有触发判据的因子——不需要清理。'
                    : '正在加载清理建议…'}
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border/50 text-xs text-muted-foreground">
                        <th className="py-2 px-2 w-8">
                          <input
                            type="checkbox"
                            aria-label="全选"
                            checked={allSelected}
                            onChange={toggleAll}
                          />
                        </th>
                        <th className="py-2 px-2 text-left font-medium">因子</th>
                        <th className="py-2 px-2 text-center font-medium">严重度</th>
                        <th className="py-2 px-2 text-left font-medium">判据与证据</th>
                        <th className="py-2 px-2 text-center font-medium">ICIR</th>
                        <th className="py-2 px-2 text-center font-medium">池评分</th>
                        <th className="py-2 px-2 text-center font-medium">|ρ|</th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((it) => (
                        <CleanupRow
                          key={it.factorId}
                          item={it}
                          checked={selected.has(it.factorId)}
                          onToggle={toggleOne}
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="glass">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">
                已归档{report ? `（${report.archivedCount}）` : ''}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {archivedRows.length === 0 ? (
                <div className="p-4 text-center text-sm text-muted-foreground">
                  还没有归档的因子。归档后的因子在这里可以随时恢复。
                </div>
              ) : (
                archivedRows.map((row) => (
                  <div
                    key={row.factorId}
                    className="flex items-center gap-3 rounded-lg bg-secondary/30 p-2.5 text-xs"
                  >
                    <span className="font-medium truncate max-w-[40%]" title={row.factorName}>
                      {row.factorName}
                    </span>
                    <span className="font-mono text-[10px] text-muted-foreground">
                      {row.factorId.slice(0, 12)}
                    </span>
                    <span className="text-muted-foreground ml-auto">
                      归档于 {fmtTime(row.archivedAt)}
                    </span>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busy}
                      onClick={() => handleRestoreOne(row.factorId)}
                    >
                      <ArchiveRestore className="h-3.5 w-3.5 mr-1" /> 恢复
                    </Button>
                  </div>
                ))
              )}
              {report && report.archivedCount > archivedRows.length && (
                <p className="text-[11px] text-muted-foreground">
                  还有 {report.archivedCount - archivedRows.length} 个更早归档的因子不在本页
                  （列表按更新时间取前 {ARCHIVED_FETCH_LIMIT} 行）。
                </p>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
};

interface CleanupRowProps {
  item: PoolCleanupSuggestion;
  checked: boolean;
  onToggle: (factorId: string) => void;
}

const CleanupRow: React.FC<CleanupRowProps> = ({ item, checked, onToggle }) => (
  <tr className="border-b border-border/40 last:border-0 hover:bg-muted/40 transition-colors align-top">
    <td className="py-2 px-2">
      <input
        type="checkbox"
        aria-label={`选择 ${item.factorName}`}
        checked={checked}
        onChange={() => onToggle(item.factorId)}
      />
    </td>
    <td className="py-2 px-2 max-w-[200px]">
      <div className="truncate font-medium" title={item.factorFormulation || item.factorName}>
        {item.factorName}
      </div>
      <div className="truncate font-mono text-[10px] text-muted-foreground">
        {item.factorId.slice(0, 12)}
      </div>
    </td>
    <td className="py-2 px-2 text-center">
      <SeverityBadge severity={item.severity} />
    </td>
    <td className="py-2 px-2 space-y-1">
      {item.reasons.map((reason) => (
        <div key={reason.code} className="flex items-start gap-1.5">
          <span
            className={`inline-flex shrink-0 items-center rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${
              REASON_STYLE[reason.code] ?? REASON_STYLE.no_diversity
            }`}
          >
            {reason.label || REASON_FALLBACK_LABEL[reason.code] || reason.code}
          </span>
          <span className="text-[11px] text-muted-foreground leading-snug">{reason.detail}</span>
        </div>
      ))}
    </td>
    <td className="py-2 px-2 text-center font-mono">{fmtNum(item.icir)}</td>
    <td className="py-2 px-2 text-center font-mono">{fmtNum(item.poolScore, 4)}</td>
    <td
      className="py-2 px-2 text-center font-mono"
      title={item.maxPoolCorrWith ? `最相似：${item.maxPoolCorrWith}` : undefined}
    >
      {fmtNum(item.maxPoolCorr, 3)}
    </td>
  </tr>
);

export default PoolCleanupTab;
