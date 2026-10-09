import { describe, test, expect } from 'vitest';
import { groupServicesByPlane, sortAnomalyFirst } from '../servicePlanes';
import type { DashboardServiceInfo } from '../../types';

const svc = (service: string, over: Partial<DashboardServiceInfo> = {}): DashboardServiceInfo => ({
    service,
    status: 'healthy',
    score: 100,
    healthy: true,
    ...over,
});

describe('groupServicesByPlane', () => {
    test('按平面归组且平面顺序固定：核心 → 数据 → 调度 → 接入', () => {
        const planes = groupServicesByPlane([
            svc('web'),
            svc('redis'),
            svc('api'),
            svc('celery_beat'),
            svc('engine'),
        ]);

        expect(planes.map((p) => p.key)).toEqual(['core', 'data', 'scheduler', 'edge']);
        expect(planes[0].services.map((s) => s.service)).toEqual(['api', 'engine']);
    });

    test('未知服务进「其他」而不是从界面上消失', () => {
        const planes = groupServicesByPlane([svc('api'), svc('brand_new_thing')]);

        const other = planes.find((p) => p.key === 'other');
        expect(other?.services.map((s) => s.service)).toEqual(['brand_new_thing']);
    });

    test('组内异常优先：不可达/异常排健康前，再按评分升序', () => {
        const planes = groupServicesByPlane([
            svc('api'),
            svc('engine', { healthy: false, status: 'unreachable', score: 0 }),
            svc('trade', { healthy: false, status: 'degraded', score: 70 }),
        ]);

        expect(planes[0].services.map((s) => s.service)).toEqual(['engine', 'trade', 'api']);
    });

    test('空输入返回空数组；空平面不出现', () => {
        expect(groupServicesByPlane([])).toEqual([]);
        const onlyCore = groupServicesByPlane([svc('trade')]);
        expect(onlyCore.map((p) => p.key)).toEqual(['core']);
    });
});

describe('sortAnomalyFirst', () => {
    test('不改原数组，返回新数组', () => {
        const input = [svc('api'), svc('engine', { healthy: false, status: 'unreachable', score: 0 })];
        const sorted = sortAnomalyFirst(input);

        expect(sorted).not.toBe(input);
        expect(input[0].service).toBe('api');
        expect(sorted[0].service).toBe('engine');
    });
});
