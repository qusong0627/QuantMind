/**
 * 内部信号词汇（BUY/SELL/HOLD）的**中性译法**。
 *
 * 纪律：这个映射只用于**展示面**。下单表单、委托列表、成交台账这些执行上下文
 * 必须继续说「买入/卖出」——用户在那儿要按的是真按钮，含糊化反而危险。
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import {
    SIGNAL_POSITION_HINT,
    SIGNAL_POSITION_LABELS,
    signalPositionLabel,
} from '../signalVocabulary';

/**
 * 与后端共用同一份金样：`backend/shared/research_score.py` 的 `SIGNAL_POSITION_LABELS`
 * 是同一条口径的第二份实现。只改一侧，另一侧的测试当场红。
 *
 * 路径与 `researchScore.test.ts` 同款：金样存在**后端包内**，因为后端测试跑在容器里、
 * 只挂了 `./backend`，放后端侧两边才都读得到。直接读文件而非 `import`——金样在
 * `electron/` 之外，走模块解析会撞上 vite 的 root 限制。
 */
const GOLDEN_PATH = path.resolve(
    __dirname,
    '../../../../..',
    'backend/tests/fixtures/signalPositionGolden.json',
);
const golden: {
    labels: Record<string, string>;
    bannedWords: string[];
    unknownInputs: string[];
} = JSON.parse(readFileSync(GOLDEN_PATH, 'utf-8'));

describe('signalPositionLabel', () => {
    it('三态各有中性译法，且都不含方向动词', () => {
        for (const side of ['BUY', 'SELL', 'HOLD']) {
            const label = signalPositionLabel(side);
            expect(label).not.toBe('—');
            for (const word of ['买', '卖', '多', '空', '建议']) {
                expect(label).not.toContain(word);
            }
        }
    });

    it('大小写与空白都归一', () => {
        expect(signalPositionLabel('buy')).toBe(signalPositionLabel('BUY'));
        expect(signalPositionLabel('  sell ')).toBe(signalPositionLabel('SELL'));
    });

    it('未知值与缺失一律 —，不猜', () => {
        // 猜一个方向出来是最坏的结果：既错又是在给建议
        expect(signalPositionLabel('STRONG_BUY')).toBe('—');
        expect(signalPositionLabel(null)).toBe('—');
        expect(signalPositionLabel(undefined)).toBe('—');
        expect(signalPositionLabel('')).toBe('—');
    });

    it('译法表与函数一致（表是唯一来源）', () => {
        expect(signalPositionLabel('BUY')).toBe(SIGNAL_POSITION_LABELS['BUY']);
        expect(signalPositionLabel('HOLD')).toBe(SIGNAL_POSITION_LABELS['HOLD']);
    });

    it('提示语说明这是位置而非建议，并声明同源可比', () => {
        expect(SIGNAL_POSITION_HINT).toContain('位置');
        expect(SIGNAL_POSITION_HINT).toContain('不构成');
        for (const word of ['买入', '卖出', '看多', '看空']) {
            expect(SIGNAL_POSITION_HINT).not.toContain(word);
        }
    });
});

describe('与后端共用金样（防两侧漂移）', () => {
    // 每条都先断言金样非空：空金样会让用例零项通过，那是最坏的一种绿。
    it('译法表逐条一致', () => {
        expect(Object.keys(golden.labels).length).toBeGreaterThan(0);
        expect(SIGNAL_POSITION_LABELS).toEqual(golden.labels);
    });

    it('金样禁词表逐条不命中', () => {
        expect(golden.bannedWords.length).toBeGreaterThan(0);
        for (const label of Object.values(golden.labels)) {
            for (const word of golden.bannedWords) {
                expect(label).not.toContain(word);
            }
        }
    });

    it('金样未知输入一律 —', () => {
        expect(golden.unknownInputs.length).toBeGreaterThan(0);
        for (const raw of golden.unknownInputs) {
            expect(signalPositionLabel(raw)).toBe('—');
        }
    });
});
