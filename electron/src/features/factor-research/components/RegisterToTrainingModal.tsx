/**
 * 因子研究 —— 「注册到训练目录」弹窗（把勾选的因子写进训练因子目录**草稿**）。
 *
 * 这条出口此前并不存在：研究页能多选、能对比合成，但没有任何一步会碰到
 * `qm_training_factor_mapping`（弹窗与报告里的「已写入训练目录」是假文案，
 * 已一并删除）。现在它真的落库了，所以措辞要如实——**只写草稿**，能不能进
 * 训练要看管理员在训练数据集页显式发布。
 *
 * 结果逐条回报：能注册的和被跳过的分开列，跳过带原因，不静默吞。
 */
import React, { useEffect, useId, useMemo, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, Loader2, PackagePlus, X } from 'lucide-react';
import { adminService } from '../../admin/services/adminService';
import type { ResearchFactorRegistrationResult } from '../../admin/types';
import type { FactorDataset } from '../services/factorResearchService';
import type { FactorMeta } from '../types/factorResearch';
import { extractApiError } from '../../../utils/apiError';

interface Props {
  codes: string[];
  factors: FactorMeta[];
  /** 与后端 `store.factors_meta()` 的数据集口径一致：classic | private */
  dataset: FactorDataset;
  onClose: () => void;
}

type Phase = 'confirm' | 'submitting' | 'done';

const DATASET_LABEL: Record<FactorDataset, string> = {
  classic: '经典因子',
  private: '私人因子库',
};

/** Tab 循环的候选。排除 `tabindex="-1"`（面板自身就靠它接管初始焦点）。 */
const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export const RegisterToTrainingModal: React.FC<Props> = ({ codes, factors, dataset, onClose }) => {
  const [phase, setPhase] = useState<Phase>('confirm');
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ResearchFactorRegistrationResult | null>(null);
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  /** 按下时是否落在遮罩上。拖选文本后松手在遮罩会派发 click，那时不该关闭。 */
  const pressedOverlay = useRef(false);
  /** phase 的镜像，供键盘监听读取——直接依赖 phase 会让下面的 effect 重跑，
   *  把焦点重新抢回面板（用户在结果列表里翻到一半会被弹回去）。 */
  const phaseRef = useRef(phase);
  phaseRef.current = phase;

  // 键盘与焦点。挂载一次即可，故依赖只有 onClose。
  useEffect(() => {
    const restoreTo = document.activeElement as HTMLElement | null;
    // 打开即把焦点移进面板：`aria-modal` 已经对外声明「背后是惰性的」，
    // 焦点却留在触发按钮上，键盘用户一按 Tab 就走进被声明为惰性的区域。
    panelRef.current?.focus();

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        // 提交中忽略——请求已在途，关掉只会让用户以为没写进去。
        if (phaseRef.current !== 'submitting') onClose();
        return;
      }
      if (e.key !== 'Tab') return;
      const focusables = panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE);
      if (!focusables?.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === panelRef.current)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };

    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      restoreTo?.focus?.(); // 关闭后焦点回到原来的触发按钮，别丢在 body 上
    };
  }, [onClose]);

  const byCode = useMemo(() => new Map(factors.map((f) => [f.code, f])), [factors]);

  /** 按来源库分组——注册是「一个来源库一份草稿」，分组即最终落库结构 */
  const groups = useMemo(() => {
    const out = new Map<string, string[]>();
    codes.forEach((c) => {
      const lib = byCode.get(c)?.l2 || '未知来源库';
      out.set(lib, [...(out.get(lib) || []), c]);
    });
    return [...out.entries()];
  }, [codes, byCode]);

  const skippedByCode = useMemo(
    () => new Map((result?.skipped || []).map((s) => [s.code, s.reason])),
    [result],
  );
  const registeredCodes = useMemo(
    () => new Set((result?.registered || []).map((r) => r.code)),
    [result],
  );

  const submit = async () => {
    setPhase('submitting');
    setError(null);
    try {
      const out = await adminService.registerResearchFactorsToTraining({ dataset, codes });
      setResult(out);
      setPhase('done');
    } catch (e: unknown) {
      // 后端拒绝原因都是中文 detail（未知市场/已发布/字段未刷新…），
      // 只取 axios 的英文 message 会让用户无从下手。
      setError(extractApiError(e, '注册失败，请稍后重试'));
      setPhase('confirm');
    }
  };

  const nRegistered = result?.registered.length ?? 0;
  const nSkipped = result?.skipped.length ?? 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-[2px] p-4"
      onMouseDown={(e) => {
        // 只在「按下与松开都在遮罩上」时才算点击遮罩。缺这一半，面板里
        // 选中文字后把鼠标拖到遮罩上松手（click 目标=共同祖先=遮罩）就会
        // 误关弹窗，把刚看的结果丢掉。
        pressedOverlay.current = e.target === e.currentTarget;
      }}
      onClick={(e) => {
        if (e.target !== e.currentTarget || !pressedOverlay.current) return;
        if (phase !== 'submitting') onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="w-full max-w-2xl max-h-[80vh] flex flex-col overflow-hidden rounded-2xl bg-white shadow-2xl ring-1 ring-slate-900/5 outline-none"
      >
        {/* 头部：深色带 + 目标信息，和站内白底卡片拉开层次 */}
        <header className="shrink-0 bg-slate-900 px-5 py-3.5 flex items-start gap-3">
          <div className="w-9 h-9 shrink-0 rounded-xl bg-gradient-to-br from-indigo-400 to-violet-500 flex items-center justify-center">
            <PackagePlus className="w-4.5 h-4.5 text-white" />
          </div>
          <div className="min-w-0 flex-1">
            <h3 id={titleId} className="text-sm font-extrabold text-white tracking-tight">
              注册到训练目录
            </h3>
            <p className="mt-0.5 text-[11px] text-slate-400">
              {DATASET_LABEL[dataset] || dataset} · <span className="font-mono">{codes.length}</span> 个因子 ·
              写入市场 <span className="font-mono">CN</span>
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={phase === 'submitting'}
            aria-label="关闭"
            title="关闭"
            className="shrink-0 rounded-lg p-1 text-slate-400 hover:bg-white/10 hover:text-white disabled:opacity-30"
          >
            <X className="w-4 h-4" />
          </button>
        </header>

        {/* 只写草稿——这句必须显眼：注册 ≠ 进训练 */}
        <div className="shrink-0 flex items-start gap-2 border-b border-amber-100 bg-amber-50/70 px-5 py-2">
          <AlertTriangle className="w-3.5 h-3.5 mt-[1px] shrink-0 text-amber-500" />
          <p className="text-[11px] leading-relaxed text-amber-800">
            只写入<span className="font-bold">草稿</span>，不会改变当前训练的线上口径；要让模型真正用上，
            需管理员在「训练数据集」页发布该草稿版本。
            {phase === 'confirm' && ' 按来源库各建/找一份草稿。'}
          </p>
        </div>

        {/* 主体 */}
        <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar px-5 py-3">
          {phase === 'done' && result ? (
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-2">
                <div className="rounded-xl border border-emerald-100 bg-emerald-50/60 px-3 py-2">
                  <div className="text-[10px] font-bold text-emerald-700">已写入草稿</div>
                  <div className="text-xl font-extrabold font-mono text-emerald-700">{nRegistered}</div>
                </div>
                <div
                  className={`rounded-xl border px-3 py-2 ${
                    nSkipped ? 'border-amber-100 bg-amber-50/60' : 'border-slate-200 bg-slate-50'
                  }`}
                >
                  <div className={`text-[10px] font-bold ${nSkipped ? 'text-amber-700' : 'text-slate-400'}`}>
                    已跳过
                  </div>
                  <div
                    className={`text-xl font-extrabold font-mono ${nSkipped ? 'text-amber-700' : 'text-slate-400'}`}
                  >
                    {nSkipped}
                  </div>
                </div>
              </div>

              <div className="space-y-1">
                {Object.entries(result.versions).map(([lib, vid]) => {
                  const n = result.registered.filter((r) => r.source_dataset === lib).length;
                  return (
                    <div
                      key={vid}
                      className="flex items-center gap-2 rounded-lg border border-slate-200/80 bg-white px-2.5 py-1.5"
                    >
                      <span className="font-bold text-[11px] text-slate-700">{lib}</span>
                      <span className="text-[10px] text-slate-400">草稿</span>
                      <span className="font-mono text-[10px] text-indigo-600 truncate">{vid}</span>
                      <div className="flex-1" />
                      <span className="shrink-0 text-[10px] font-mono text-slate-500">{n} 列</span>
                    </div>
                  );
                })}
              </div>

              <div className="space-y-[3px]">
                {codes.map((c) => {
                  const ok = registeredCodes.has(c);
                  const reason = skippedByCode.get(c);
                  const f = byCode.get(c);
                  return (
                    <div key={c} className="flex items-start gap-2 text-[11px]">
                      {ok ? (
                        <CheckCircle2 className="w-3.5 h-3.5 mt-[1px] shrink-0 text-emerald-500" />
                      ) : (
                        <AlertTriangle className="w-3.5 h-3.5 mt-[1px] shrink-0 text-amber-500" />
                      )}
                      <span className="font-bold text-slate-700 shrink-0">{f?.name_cn || c}</span>
                      <span className="font-mono text-[10px] text-slate-400 shrink-0 mt-[1px]">{c}</span>
                      {reason && <span className="text-amber-700">{reason}</span>}
                    </div>
                  );
                })}
              </div>

              {!nRegistered && (
                <p className="text-[11px] text-slate-500">
                  一个都没写进去。常见原因：该因子所属库还没在「训练数据集」页刷新过字段，
                  刷新后再回来注册即可。
                </p>
              )}
            </div>
          ) : (
            <div className="space-y-2.5">
              {groups.map(([lib, groupCodes]) => (
                <div key={lib} className="rounded-xl border border-slate-200/80 overflow-hidden">
                  <div className="flex items-center gap-2 bg-slate-50 px-3 py-1.5">
                    <span className="text-[11px] font-extrabold text-slate-700">{lib}</span>
                    <span className="rounded-full border border-slate-200 bg-white px-1.5 py-[1px] text-[9px] font-bold text-slate-500">
                      来源库
                    </span>
                    <div className="flex-1" />
                    <span className="text-[10px] font-mono text-slate-500">{groupCodes.length} 个</span>
                  </div>
                  <div className="divide-y divide-slate-100">
                    {groupCodes.map((c) => {
                      const f = byCode.get(c);
                      return (
                        <div key={c} className="flex items-center gap-2 px-3 py-1">
                          <span className="text-[11px] font-bold text-slate-700">{f?.name_cn || c}</span>
                          <span className="font-mono text-[10px] text-slate-400">{c}</span>
                          {f && !f.available && (
                            <span className="rounded-full border border-amber-100 bg-amber-50 px-1.5 py-[1px] text-[9px] font-bold text-amber-600">
                              数据不足
                            </span>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 底部 */}
        <footer className="shrink-0 flex items-center gap-2 border-t border-slate-200/80 px-5 py-3">
          {/* truncate 会切掉长原因（如逐条 422 明细），title 让鼠标悬停仍可读全 */}
          {error && (
            <span className="flex-1 text-[11px] text-rose-500 truncate" title={error}>
              {error}
            </span>
          )}
          {!error && (
            <span className="flex-1 text-[10px] text-slate-400">
              {phase === 'done' ? '改动即时生效于草稿，可随时在训练数据集页回滚。' : '写入后按来源库归入对应草稿。'}
            </span>
          )}
          {phase === 'done' ? (
            <button
              onClick={onClose}
              className="rounded-full bg-slate-900 px-4 py-1.5 text-[11px] font-bold text-white hover:bg-slate-700"
            >
              关闭
            </button>
          ) : (
            <>
              <button
                onClick={onClose}
                disabled={phase === 'submitting'}
                className="rounded-full border border-slate-200 px-3.5 py-1.5 text-[11px] font-bold text-slate-500 hover:bg-slate-50 disabled:opacity-40"
              >
                取消
              </button>
              <button
                onClick={submit}
                disabled={phase === 'submitting' || codes.length === 0}
                className="flex items-center gap-1.5 rounded-full bg-indigo-600 px-4 py-1.5 text-[11px] font-bold text-white hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {phase === 'submitting' ? (
                  <Loader2 className="w-3 h-3 animate-spin" />
                ) : (
                  <PackagePlus className="w-3 h-3" />
                )}
                {phase === 'submitting' ? '注册中…' : `注册 ${codes.length} 个`}
              </button>
            </>
          )}
        </footer>
      </div>
    </div>
  );
};
