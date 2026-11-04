/**
 * 全局「有新版本」提醒
 * ====================
 *
 * 挂在 App 根节点（登录后），自动轮询 /api/v1/system/version；落后上游 > 0
 * 时在底部浮出一张卡片，明确写出「落后哪个分支、几个提交、当前 → 最新」。
 *
 * 设计取舍：
 * - 不做一次性 toast：更新提醒是「用户需要行动的待办」，自动消失等于没提醒；
 * - 同一组 (当前 commit, 最新 commit, 落后数) 只提示一次，用户点「稍后」后
 *   sessionStorage 记住，下次开新会话或真出现新提交才再浮出；
 * - 普通用户只看到提醒（更新需要管理员权限），管理员多一个「查看并更新」，
 *   跳转到后台概览去执行（更新会重启服务，必须在那里的确认弹窗里二次确认）。
 */
import React, { useCallback, useState } from 'react';
import { useSelector } from 'react-redux';
import { useNavigate } from 'react-router-dom';
import { ArrowUpCircle, RefreshCw, X } from 'lucide-react';
import { useSystemUpdateCheck } from '../../hooks/useSystemUpdateCheck';
import type { SystemUpdateInfo } from '../../services/systemService';

const DISMISS_KEY = 'qm:system-update:dismissed';

/** 同一版本组合的稳定标识：当前 commit + 上游最新 commit + 落后数。 */
const updateKeyOf = (update: SystemUpdateInfo): string =>
    [
        update.current_short || update.current_commit || 'local',
        update.latest_commit || update.latest_short || 'upstream',
        String(update.behind),
    ].join(':');

const readDismissed = (): string => {
    try {
        return sessionStorage.getItem(DISMISS_KEY) || '';
    } catch {
        return '';
    }
};

const writeDismissed = (key: string): void => {
    try {
        sessionStorage.setItem(DISMISS_KEY, key);
    } catch {
        /* 隐私模式/禁用存储：退化为「本次渲染内不再提示」 */
    }
};

export const SystemUpdateNotifier: React.FC = () => {
    const navigate = useNavigate();
    const user = useSelector((state: any) => state.auth.user);
    const isAdmin = Boolean(user?.is_admin);
    const { update, isFetching, refresh } = useSystemUpdateCheck({
        enabled: Boolean(user),
    });
    const [dismissedKey, setDismissedKey] = useState<string>(readDismissed);

    const behind = update?.behind ?? 0;
    const updateKey = update && behind > 0 ? updateKeyOf(update) : '';
    const visible = Boolean(updateKey) && updateKey !== dismissedKey;

    const dismiss = useCallback(() => {
        if (!updateKey) return;
        writeDismissed(updateKey);
        setDismissedKey(updateKey);
    }, [updateKey]);

    if (!visible || !update) return null;

    const branch = update.upstream_branch || 'next';
    const behindText = `${update.behind}${update.behind_capped ? '+' : ''}`;
    const current = update.current_short || update.current_commit?.slice(0, 8) || '未知';
    const latest = update.latest_short || update.latest_commit?.slice(0, 8) || '';

    return (
        <div
            role="status"
            aria-live="polite"
            data-testid="system-update-notifier"
            className="fixed bottom-24 left-1/2 z-[9998] w-[min(92vw,580px)] -translate-x-1/2 overflow-hidden rounded-2xl border border-amber-200/80 bg-white shadow-[0_18px_50px_-12px_rgba(120,72,0,0.35)]"
        >
            {/* 顶部细色带：颜色语义 = 有可执行的更新 */}
            <div className="h-1 w-full bg-gradient-to-r from-amber-400 via-orange-400 to-amber-500" />

            <div className="flex items-start gap-3 px-4 pb-3 pt-3">
                <div className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-amber-50 text-amber-600 ring-1 ring-amber-200/70">
                    <ArrowUpCircle className="h-5 w-5" />
                </div>

                <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                        <span className="relative flex h-2 w-2">
                            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-70" />
                            <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-500" />
                        </span>
                        <span className="text-[13px] font-semibold tracking-wide text-slate-800">
                            发现新版本
                        </span>
                        <span className="rounded-full bg-amber-100/80 px-2 py-0.5 text-[11px] font-semibold text-amber-700">
                            落后 {behindText} 个提交
                        </span>
                    </div>

                    <p className="mt-1 m-0 text-[12px] leading-5 text-slate-600">
                        仓库 <code className="rounded bg-slate-100 px-1 py-0.5 text-[11px] font-semibold text-slate-700">{branch}</code>{' '}
                        分支有更新：当前 <span className="font-mono text-slate-700">{current}</span>
                        {latest ? (
                            <>
                                {' → '}最新 <span className="font-mono text-slate-700">{latest}</span>
                            </>
                        ) : null}
                    </p>
                    {update.latest_subject ? (
                        <p className="mt-0.5 m-0 truncate text-[11px] text-slate-400" title={update.latest_subject}>
                            最新提交：{update.latest_subject}
                        </p>
                    ) : null}

                    <div className="mt-2.5 flex items-center gap-2">
                        {isAdmin ? (
                            <button
                                type="button"
                                onClick={() => navigate('/admin/overview')}
                                className="inline-flex h-7 items-center gap-1.5 rounded-lg bg-slate-900 px-3 text-[12px] font-semibold text-white transition-colors hover:bg-slate-700"
                            >
                                <ArrowUpCircle className="h-3.5 w-3.5" />
                                查看并更新
                            </button>
                        ) : (
                            <span className="text-[11px] text-slate-500">
                                请联系管理员在后台执行更新（更新会重启服务）。
                            </span>
                        )}
                        <button
                            type="button"
                            onClick={() => void refresh(true)}
                            disabled={isFetching}
                            className="inline-flex h-7 items-center gap-1.5 rounded-lg border border-slate-200 px-2.5 text-[11px] font-medium text-slate-500 transition-colors hover:border-slate-300 hover:text-slate-700 disabled:opacity-50"
                        >
                            <RefreshCw className={`h-3 w-3 ${isFetching ? 'animate-spin' : ''}`} />
                            重新检查
                        </button>
                        <button
                            type="button"
                            onClick={dismiss}
                            className="ml-auto inline-flex h-7 items-center rounded-lg px-2 text-[11px] font-medium text-slate-400 transition-colors hover:bg-slate-50 hover:text-slate-600"
                        >
                            稍后
                        </button>
                    </div>
                </div>

                <button
                    type="button"
                    aria-label="关闭更新提醒"
                    onClick={dismiss}
                    className="-mr-1 -mt-1 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-slate-300 transition-colors hover:bg-slate-50 hover:text-slate-500"
                >
                    <X className="h-3.5 w-3.5" />
                </button>
            </div>
        </div>
    );
};
