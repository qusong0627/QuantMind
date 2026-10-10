/**
 * 池内供给面（T-MV-02）—— 设置页选方向时的「哪类挖满了、哪类还空着」读数。
 *
 * 数据源是池总览的分类聚合（归类单源 = 后端 factor_classify），与上方方向
 * 词表（L1 因子类别）**不是一一对应**：这里按「已挖因子实际归类」展示，
 * 宁可诚实标注、不做词表间的硬拼映射。
 *
 * 展示纪律与全站一致：缺失一律「—」（avg/median IC 的 0 与「没有 IC」是两回事）；
 * 饱和度「其他」行恒为 —（它不是挖掘方向，也不做分母，见后端口径注释）。
 */
import React, { useCallback, useEffect, useState } from 'react';
import { AlertCircle, BarChart3, Loader2, RefreshCw } from 'lucide-react';
import { getPoolOverview } from '../services-v2/api';
import type { PoolCategoryStat } from '../services-v2/api';

/** 默认口径 = 主挖掘范围；多市场供给面留给后续任务按市场切换 */
const DEFAULT_MARKET = 'a_share';
const DEFAULT_UNIVERSE = 'csi300';

const MARKET_LABELS: Record<string, string> = {
  a_share: 'A股',
  hong_kong: '港股',
  us_stock: '美股',
  crypto: '加密货币',
  futures: '期货',
};

const MISSING = '—';

/** 缺失显「—」，绝不上屏 0（IC 的 0 与「没算过」是两回事）。 */
function fmtNum(value: number | null, digits = 4): string {
  return value == null || !Number.isFinite(value) ? MISSING : value.toFixed(digits);
}

function fmtPct(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return MISSING;
  const pct = value * 100;
  return `${pct >= 10 ? pct.toFixed(0) : pct.toFixed(1)}%`;
}

interface SupplyFacePanelProps {
  market?: string;
  universe?: string;
}

export const SupplyFacePanel: React.FC<SupplyFacePanelProps> = ({
  market = DEFAULT_MARKET,
  universe = DEFAULT_UNIVERSE,
}) => {
  const [rows, setRows] = useState<PoolCategoryStat[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getPoolOverview({ market, universe })
      .then((res) => {
        if (cancelled) return;
        if (!res.success || !res.data) throw new Error(res.error || '加载失败');
        setRows(res.data.categoryBreakdown);
        setTotal(res.data.total);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const msg = err instanceof Error ? err.message : String(err);
        setError(`供给面读取失败：${msg}`);
        setRows([]);
        setTotal(0);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [market, universe, attempt]);

  const retry = useCallback(() => setAttempt((a) => a + 1), []);

  const scope = `${MARKET_LABELS[market] ?? market} · ${universe}`;

  return (
    <div className="pt-4 border-t border-border/50">
      <div className="flex items-center justify-between mb-3">
        <label className="text-sm font-medium inline-flex items-center gap-1.5">
          <BarChart3 className="h-4 w-4" />
          池内供给面（已挖因子归类）
        </label>
        <span className="text-xs text-muted-foreground">{scope}</span>
      </div>
      <p className="text-xs text-muted-foreground mb-3">
        已挖因子按大类归类（与上方方向词表非一一对应）；饱和度 = 该类计数 ÷ 最满大类，
        越低说明该类越空白、越值得挖。「其他」不参与饱和度。
      </p>

      {loading && (
        <div className="flex items-center gap-2 rounded-lg border border-border/50 bg-secondary/10 p-3 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          正在读取池内供给面…
        </div>
      )}

      {!loading && error && (
        <div className="flex items-center gap-2 rounded-lg border border-rose-200 bg-rose-50/70 p-3 text-xs font-bold text-rose-600">
          <AlertCircle className="h-3.5 w-3.5 shrink-0" />
          <span className="flex-1 min-w-0 break-all">{error}</span>
          <button
            type="button"
            onClick={retry}
            className="shrink-0 inline-flex items-center gap-1 rounded-full border border-rose-200 bg-white px-2.5 py-1 text-[11px] font-bold text-rose-600 hover:bg-rose-50 cursor-pointer"
          >
            <RefreshCw className="h-3 w-3" />
            重试
          </button>
        </div>
      )}

      {!loading && !error && rows.length === 0 && (
        <div className="rounded-lg border border-border/50 bg-secondary/10 p-3 text-xs text-muted-foreground">
          本池暂无已挖因子——挖出入池后这里会显示各大类的供给与质量读数。
        </div>
      )}

      {!loading && !error && rows.length > 0 && (
        <div className="space-y-1.5 rounded-lg border border-border/50 bg-secondary/10 p-3">
          {rows.map((c) => (
            <div key={c.category} className="flex items-center gap-3 py-0.5">
              <span
                className="w-28 shrink-0 truncate text-xs font-medium"
                title={`${c.label}（池内 ${c.count} 个，占 ${fmtPct(c.share)}）`}
              >
                {c.label}
              </span>
              <span className="w-14 shrink-0 font-mono text-[11px] text-muted-foreground">
                {c.count} 个
              </span>
              <span className="w-40 shrink-0 font-mono text-[11px] text-muted-foreground">
                均值 IC {fmtNum(c.avgIc)}
                {c.nIc < c.count ? `（${c.nIc}/${c.count}）` : ''}
              </span>
              <span className="w-32 shrink-0 font-mono text-[11px] text-muted-foreground">
                中位 {fmtNum(c.medianIc)}
              </span>
              <span className="flex flex-1 items-center gap-2 min-w-0">
                <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-border/60">
                  {c.saturation != null && (
                    <span
                      className="block h-full rounded-full bg-indigo-500/80"
                      style={{ width: `${Math.min(100, c.saturation * 100).toFixed(1)}%` }}
                    />
                  )}
                </span>
                <span className="w-12 shrink-0 text-right font-mono text-[11px] text-muted-foreground">
                  {fmtPct(c.saturation)}
                </span>
              </span>
            </div>
          ))}
          <p className="pt-1 text-[11px] text-muted-foreground">
            共 {total} 个在池因子；IC 为该类内已回测样本的聚合（覆盖率随行可见）。
          </p>
        </div>
      )}
    </div>
  );
};

export default SupplyFacePanel;
