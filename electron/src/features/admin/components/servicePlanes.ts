/**
 * 服务集群按平面分组（系统概览页 2026-10-09 机构版改版）。
 *
 * 13 个服务平铺一屏卡片时没有层次 —— 后端四服务、数据层、调度、生态混在一起，
 * 断一个要逐张卡找。这里把「谁是谁的依赖」编码成平面：核心服务 → 数据层 →
 * 调度 → 接入/生态；**未知服务不丢**，归入「其他」，新组件接入时照常出现。
 */
import type { DashboardServiceInfo } from '../types';

export interface ServicePlane {
    key: string;
    label: string;
    services: DashboardServiceInfo[];
}

const PLANE_ORDER: ReadonlyArray<{ key: string; label: string; names: readonly string[] }> = [
    { key: 'core', label: '核心服务', names: ['api', 'engine', 'trade', 'stream'] },
    { key: 'data', label: '数据层', names: ['postgres', 'redis', 'data_gateway'] },
    { key: 'scheduler', label: '调度与任务', names: ['celery', 'celery_beat'] },
    { key: 'edge', label: '接入与生态', names: ['web', 'qwenpaw', 'rsshub', 'huntly', 'dsh'] },
];

const KNOWN = new Set(PLANE_ORDER.flatMap((p) => [...p.names]));

/** 异常优先：不可达/异常排前面，再按评分升序（分低的更值得看），最后按名字稳定排序。 */
export function sortAnomalyFirst(services: DashboardServiceInfo[]): DashboardServiceInfo[] {
    return [...services].sort((a, b) => {
        const aOk = a.healthy && a.status === 'healthy' ? 1 : 0;
        const bOk = b.healthy && b.status === 'healthy' ? 1 : 0;
        if (aOk !== bOk) return aOk - bOk;
        if (a.score !== b.score) return a.score - b.score;
        return a.service.localeCompare(b.service);
    });
}

export function groupServicesByPlane(services: DashboardServiceInfo[]): ServicePlane[] {
    const planes: ServicePlane[] = PLANE_ORDER.map((p) => ({
        key: p.key,
        label: p.label,
        services: sortAnomalyFirst(services.filter((s) => p.names.includes(s.service))),
    })).filter((p) => p.services.length > 0);
    const rest = sortAnomalyFirst(services.filter((s) => !KNOWN.has(s.service)));
    if (rest.length > 0) {
        planes.push({ key: 'other', label: '其他', services: rest });
    }
    return planes;
}
