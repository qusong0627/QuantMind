/**
 * 策略版本与变更抽屉（T-FE-10）：状态机/版本号/参数锁 + 版本 diff（参数表 + 代码行级）+ 生效参数及来源。
 *
 * 数据源：/api/v1/strategies/{id}/versions（保存时同事务快照，T-FE-10 后端契约）；
 * 来源列与模板默认值比对（模板经 strategy_type 关联，缺失如实标"用户自定义"）。
 */

import React, { useEffect, useMemo, useState } from 'react';
import { Drawer } from 'antd';
import { History, Lock, RefreshCw } from 'lucide-react';
import { strategyManagementService } from '../../../services/strategyManagementService';
import { strategyTemplateService } from '../../../features/strategy-wizard/services/strategyTemplateService';
import {
  lineDiff,
  paramDiffEntries,
  paramSourceRows,
  pickVersions,
  renderParamValue,
  type VersionRecord,
} from './strategyDiffModel';

interface StrategyVersionDrawerProps {
  open: boolean;
  strategy: { id: string; name: string; version?: number; strategyType?: string; parameters?: Record<string, unknown>; rawStatus?: string } | null;
  onClose: () => void;
}

const SOURCE_LABEL: Record<string, { text: string; cls: string }> = {
  template_default: { text: '模板默认', cls: 'bg-slate-100 text-slate-500 border-slate-200' },
  modified: { text: '已修改', cls: 'bg-amber-50 text-amber-700 border-amber-200' },
  user_custom: { text: '用户自定义', cls: 'bg-blue-50 text-blue-700 border-blue-200' },
};

export const StrategyVersionDrawer: React.FC<StrategyVersionDrawerProps> = ({ open, strategy, onClose }) => {
  const [versions, setVersions] = useState<VersionRecord[]>([]);
  const [templateParams, setTemplateParams] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [baseVersion, setBaseVersion] = useState<number | null>(null);
  const [targetVersion, setTargetVersion] = useState<number | null>(null);

  useEffect(() => {
    if (!open || !strategy?.id) return;
    let cancelled = false;
    setLoading(true);
    setError('');
    strategyManagementService
      .getStrategyVersions(strategy.id, true)
      .then((list) => {
        if (cancelled) return;
        setVersions(list || []);
        const { latest, previous } = pickVersions(list || []);
        setTargetVersion(latest?.version ?? null);
        setBaseVersion(previous?.version ?? latest?.version ?? null);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '版本历史加载失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    // 模板默认参数（来源列对照；无模板如实降级）
    if (strategy?.strategyType) {
      strategyTemplateService
        .getTemplates()
        .then((ts) => {
          if (cancelled) return;
          const tpl = (ts || []).find((t) => t.id === strategy.strategyType) as
            | { params?: Array<{ name?: string; default?: unknown; value?: unknown }> }
            | undefined;
          if (tpl?.params) {
            const map: Record<string, unknown> = {};
            for (const p of tpl.params) {
              const key = String(p.name || '');
              if (key) map[key] = p.default ?? p.value ?? null;
            }
            setTemplateParams(map);
          } else {
            setTemplateParams(null);
          }
        })
        .catch(() => setTemplateParams(null));
    } else {
      setTemplateParams(null);
    }
    return () => {
      cancelled = true;
    };
  }, [open, strategy?.id, strategy?.strategyType]);

  const base = useMemo(() => versions.find((v) => v.version === baseVersion) || null, [versions, baseVersion]);
  const target = useMemo(() => versions.find((v) => v.version === targetVersion) || null, [versions, targetVersion]);
  const paramDiff = useMemo(() => paramDiffEntries(base?.parameters, target?.parameters), [base, target]);
  const codeDiff = useMemo(() => lineDiff(String(base?.code || ''), String(target?.code || '')), [base, target]);
  const changedLines = codeDiff.filter((r) => r.kind !== 'same');
  const sourceRows = useMemo(
    () => paramSourceRows(target?.parameters ?? strategy?.parameters, templateParams),
    [target, strategy?.parameters, templateParams]
  );

  const isLocked = ['sim', 'live'].includes(String(strategy?.rawStatus || '').toLowerCase());

  return (
    <Drawer open={open} onClose={onClose} width={760} title={null} destroyOnHidden>
      <div className="space-y-4">
        <header className="flex items-center gap-2">
          <History className="w-4 h-4 text-blue-600" />
          <h3 className="text-base font-bold text-slate-800">版本与变更 · {strategy?.name}</h3>
          <span className="text-xs text-slate-400">v{strategy?.version ?? base?.version ?? '—'}</span>
          {isLocked && (
            <span
              className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full bg-amber-50 border border-amber-200 text-amber-700"
              title="参数锁（T-P3-01）：运行中策略修改内容必须显式升版本（携带当前 version）"
            >
              <Lock className="w-3 h-3" /> 参数锁（运行中）
            </span>
          )}
        </header>

        {loading ? (
          <div className="flex items-center gap-2 text-xs text-slate-400 py-4">
            <RefreshCw className="w-3.5 h-3.5 animate-spin" /> 正在加载版本历史...
          </div>
        ) : error ? (
          <div className="text-xs text-rose-600">{error}</div>
        ) : versions.length === 0 ? (
          <div className="text-xs text-slate-500 bg-gray-50 rounded-xl p-4">
            暂无版本记录（本次上线后的保存会开始留快照；历史策略可从现在起积累）
          </div>
        ) : (
          <>
            {/* 版本选择 */}
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="text-slate-500">对比</span>
              <select
                value={baseVersion ?? ''}
                onChange={(e) => setBaseVersion(Number(e.target.value))}
                className="rounded-lg border border-gray-200 px-2 py-1"
              >
                {versions.map((v) => (
                  <option key={v.version} value={v.version}>
                    v{v.version}（{v.created_at ? String(v.created_at).slice(0, 16).replace('T', ' ') : '—'}）
                  </option>
                ))}
              </select>
              <span className="text-slate-400">→</span>
              <select
                value={targetVersion ?? ''}
                onChange={(e) => setTargetVersion(Number(e.target.value))}
                className="rounded-lg border border-gray-200 px-2 py-1"
              >
                {versions.map((v) => (
                  <option key={v.version} value={v.version}>
                    v{v.version}（{v.created_at ? String(v.created_at).slice(0, 16).replace('T', ' ') : '—'}）
                  </option>
                ))}
              </select>
              <span className="text-slate-400">
                代码变更 {changedLines.length} 行 · 参数变更 {paramDiff.length} 项
              </span>
            </div>

            {/* 参数差异 */}
            <div className="rounded-xl border border-gray-100 p-3">
              <h4 className="text-sm font-semibold text-slate-800 mb-2">参数差异</h4>
              {paramDiff.length === 0 ? (
                <p className="text-xs text-slate-400">两版本参数一致</p>
              ) : (
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-slate-400 text-left">
                      <th className="font-medium py-1">参数</th>
                      <th className="font-medium py-1">旧值（v{base?.version}）</th>
                      <th className="font-medium py-1">新值（v{target?.version}）</th>
                    </tr>
                  </thead>
                  <tbody>
                    {paramDiff.map((d) => (
                      <tr key={d.key} className="border-t border-gray-50">
                        <td className="py-1 text-slate-700">{d.key}</td>
                        <td className={`py-1 ${d.kind === 'added' ? 'text-slate-300' : 'text-slate-500'}`}>
                          {renderParamValue(d.from)}
                        </td>
                        <td className={`py-1 ${d.kind === 'removed' ? 'text-slate-300' : 'text-red-600'}`}>
                          {renderParamValue(d.to)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            {/* 代码差异 */}
            <div className="rounded-xl border border-gray-100 p-3">
              <h4 className="text-sm font-semibold text-slate-800 mb-2">代码差异（行级）</h4>
              {changedLines.length === 0 ? (
                <p className="text-xs text-slate-400">两版本代码一致</p>
              ) : (
                <pre className="max-h-[320px] overflow-auto rounded-lg bg-slate-50 p-2 text-[10px] leading-4">
                  {codeDiff.map((row, idx) => (
                    <div
                      key={idx}
                      className={
                        row.kind === 'added'
                          ? 'bg-red-50 text-red-700'
                          : row.kind === 'removed'
                            ? 'bg-emerald-50 text-emerald-700 line-through decoration-emerald-300'
                            : 'text-slate-500'
                      }
                    >
                      <span className="inline-block w-10 text-slate-300 text-right mr-2">
                        {row.kind === 'added' ? `+${row.newLine}` : row.kind === 'removed' ? `-${row.oldLine}` : row.oldLine}
                      </span>
                      {row.text}
                    </div>
                  ))}
                </pre>
              )}
            </div>
          </>
        )}

        {/* 生效参数及来源 */}
        <div className="rounded-xl border border-gray-100 p-3">
          <h4 className="text-sm font-semibold text-slate-800 mb-2">
            生效参数及来源（v{target?.version ?? strategy?.version ?? '—'}）
          </h4>
          {sourceRows.length === 0 ? (
            <p className="text-xs text-slate-400">无参数</p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {sourceRows.map((row) => {
                const meta = SOURCE_LABEL[row.source];
                return (
                  <span
                    key={row.key}
                    className={`text-[11px] px-2 py-0.5 rounded-full border ${meta.cls}`}
                    title={`${row.key} = ${row.value}（${meta.text}）`}
                  >
                    {row.key}={row.value}
                    <span className="opacity-60 ml-1">· {meta.text}</span>
                  </span>
                );
              })}
            </div>
          )}
          <p className="text-[10px] text-slate-400 mt-2">
            来源对照：与模板（{strategy?.strategyType || '—'}）默认值一致=模板默认；不同=已修改；模板无此键=用户自定义
          </p>
        </div>
      </div>
    </Drawer>
  );
};
