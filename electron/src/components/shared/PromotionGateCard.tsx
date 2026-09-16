/**
 * 晋级门槛卡（T-FE-12）：策略详情内展示"距晋级还差什么"。
 *
 * 数据源：/api/v1/eval/health/{strategy_id}（体检留档 + **门禁预演——与启动端点
 * 拦截逻辑同源**，展示即真实口径）；无留档 → 如实引导先跑回测（门禁会拦截）。
 */

import React, { useEffect, useState } from 'react';
import { DoorOpen, RefreshCw, ShieldAlert, ShieldCheck } from 'lucide-react';
import { getStrategyHealth } from '../../features/skills-center/services/evalCenterService';
import type { StrategyHealthArchive } from '../../features/skills-center/types/evalCenter';
import { gradeMeta } from '../../features/skills-center/components/eval-center/evalCenterModel';

interface PromotionGateCardProps {
  strategyId: string;
}

export const PromotionGateCard: React.FC<PromotionGateCardProps> = ({ strategyId }) => {
  const [archive, setArchive] = useState<StrategyHealthArchive | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    getStrategyHealth(strategyId)
      .then((resp) => {
        if (!cancelled) setArchive(resp?.data || null);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '门禁信息读取失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [strategyId]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-xs text-slate-400 py-3">
        <RefreshCw className="w-3.5 h-3.5 animate-spin" />
        正在读取晋级门槛...
      </div>
    );
  }
  if (error || !archive) {
    return <div className="text-xs text-rose-600 py-2">{error || '门禁信息不可用'}</div>;
  }

  const latest = archive.latest;
  const meta = gradeMeta(latest?.verdict ?? null, false);
  const allowed = archive.gate.allowed;

  return (
    <div className="space-y-2 text-xs">
      <div
        className={`flex items-start gap-2 rounded-xl border p-3 ${
          allowed ? 'bg-red-50 border-red-200 text-red-800' : 'bg-slate-50 border-slate-200 text-slate-700'
        }`}
      >
        {allowed ? (
          <ShieldCheck className="w-4 h-4 mt-0.5 shrink-0" />
        ) : (
          <ShieldAlert className="w-4 h-4 mt-0.5 shrink-0 text-amber-600" />
        )}
        <div>
          <div className="font-semibold">
            晋级门禁（进模拟盘）：{allowed ? '放行' : '拦截'}
            {latest ? (
              <span className={`ml-2 px-1.5 py-0.5 rounded-full border ${meta.className}`}>
                体检 {latest.verdict}
                {meta.isLowConfidence ? ' †' : ''}
              </span>
            ) : (
              <span className="ml-2 text-slate-400">尚无体检留档</span>
            )}
          </div>
          <div className="mt-1 leading-5">{archive.gate.note}</div>
        </div>
      </div>

      {latest && (
        <div className="rounded-xl border border-gray-100 p-3 space-y-1">
          <div className="text-slate-500">
            可信度 {Math.round(latest.confidence ?? 0)}/100 · 证据源 {latest.evidence_source || '—'} ·{' '}
            {latest.snapshot_date || '—'}
          </div>
          {(latest.reasons || []).slice(0, 2).map((r, i) => (
            <div key={`r-${i}`} className="text-slate-600">
              · {r}
            </div>
          ))}
          {(latest.suggestions || []).slice(0, 2).map((s, i) => (
            <div key={`s-${i}`} className="text-slate-500">
              建议 {i + 1}: {s}
            </div>
          ))}
        </div>
      )}

      {archive.history.length > 1 && (
        <div className="rounded-xl border border-gray-100 p-3">
          <div className="text-slate-500 mb-1">结论历史</div>
          {archive.history.slice(0, 5).map((point, i) => {
            const pm = gradeMeta(point.verdict, false);
            return (
              <div key={i} className="flex items-center gap-2 text-[11px]">
                <span className="text-slate-400 w-20">{point.snapshot_date || '—'}</span>
                <span className={`px-1.5 rounded border ${pm.className}`}>{point.verdict || '—'}</span>
                <span className="text-slate-500">可信度 {Math.round(point.confidence ?? 0)}</span>
              </div>
            );
          })}
        </div>
      )}

      <div className="flex items-center gap-1 text-[11px] text-slate-400">
        <DoorOpen className="w-3 h-3" />
        门槛口径：A/B 放行（B 标注收益来源）、L/E/未体检拦截（T-P3-05 门槛总表，与启动端点同一判定）
      </div>
    </div>
  );
};
