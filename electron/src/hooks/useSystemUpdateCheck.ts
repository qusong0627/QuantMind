/**
 * 系统更新检查（自动提醒的唯一数据源）
 * ====================================
 *
 * 同一个 queryKey 让「全局提醒 / 后台徽章 / 后台概览 / 设置页」共享一份结果：
 * 用户登录后自动轮询（默认 10 分钟），窗口重新获得焦点时也会刷新；后端还有
 * 15 分钟磁盘缓存兜底，所以多页面共存不会放大上游请求。
 *
 * 强制刷新（refresh(true)）会绕过后端缓存，仅在用户明确点「检查更新」时使用。
 */
import { useCallback } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
    systemService,
    type SystemUpdateInfo,
    type SystemVersion,
} from '../services/systemService';

export const SYSTEM_VERSION_QUERY_KEY = ['system', 'version'] as const;

/** 自动检查间隔：用户最多晚 10 分钟看到新提交。 */
export const SYSTEM_UPDATE_POLL_MS = 10 * 60 * 1000;

export interface SystemUpdateCheck {
    version: SystemVersion | undefined;
    update: SystemUpdateInfo | null;
    isLoading: boolean;
    isFetching: boolean;
    /** 主动检查（force=true 绕过后端缓存）；失败返回 null，不抛给调用方。 */
    refresh: (force?: boolean) => Promise<SystemVersion | null>;
}

export function useSystemUpdateCheck(options?: {
    enabled?: boolean;
    pollMs?: number;
}): SystemUpdateCheck {
    const enabled = options?.enabled ?? true;
    const pollMs = options?.pollMs ?? SYSTEM_UPDATE_POLL_MS;
    const queryClient = useQueryClient();

    const query = useQuery<SystemVersion, Error>({
        queryKey: SYSTEM_VERSION_QUERY_KEY,
        queryFn: () => systemService.getVersion(false),
        enabled,
        // 版本信息本身变化很慢；10 分钟轮询 + 窗口聚焦刷新已足够。
        staleTime: 5 * 60 * 1000,
        refetchInterval: enabled ? pollMs : false,
        refetchOnWindowFocus: true,
        retry: 1,
    });

    const refresh = useCallback(
        async (force = false): Promise<SystemVersion | null> => {
            try {
                const next = await systemService.getVersion(force);
                queryClient.setQueryData(SYSTEM_VERSION_QUERY_KEY, next);
                return next;
            } catch {
                return null;
            }
        },
        [queryClient],
    );

    return {
        version: query.data,
        update: query.data?.update ?? null,
        isLoading: query.isLoading,
        isFetching: query.isFetching,
        refresh,
    };
}
