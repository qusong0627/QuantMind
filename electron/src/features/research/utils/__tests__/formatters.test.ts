/**
 * normalizeRoe 口径：百分数（与后端 `_format_candidate_record` 契约一致）。
 *
 * 小数（|v| <= 1.5，如 0.1192）→ ×100 转百分数；其余原样返回。
 * 两侧阈值同为 1.5，任一侧改动另一侧立刻红。
 */

import { describe, expect, it } from 'vitest';
import { normalizeRoe } from '../formatters';

describe('normalizeRoe 换算', () => {
    it('小数口径 → ×100 转百分数', () => {
        expect(normalizeRoe(0.1192)).toBeCloseTo(11.92, 4);
        expect(normalizeRoe(-0.05)).toBeCloseTo(-5, 4);
    });

    it('已是百分数 → 原样返回', () => {
        expect(normalizeRoe(11.76)).toBeCloseTo(11.76, 4);
        expect(normalizeRoe(-23.4)).toBeCloseTo(-23.4, 4);
    });

    it('1.5 界线：界内视作小数、界外视作百分数', () => {
        expect(normalizeRoe(1.5)).toBeCloseTo(150, 4);
        expect(normalizeRoe(1.51)).toBeCloseTo(1.51, 4);
    });

    it('缺失/非数值回落 0（展示层另有 null 守卫）', () => {
        expect(normalizeRoe(null)).toBe(0);
        expect(normalizeRoe(undefined)).toBe(0);
        expect(normalizeRoe(Number.NaN)).toBe(0);
    });
});
