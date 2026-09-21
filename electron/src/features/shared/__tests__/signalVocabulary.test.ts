/**
 * 内部信号词汇（BUY/SELL/HOLD）的**中性译法**。
 *
 * 纪律：这个映射只用于**展示面**。下单表单、委托列表、成交台账这些执行上下文
 * 必须继续说「买入/卖出」——用户在那儿要按的是真按钮，含糊化反而危险。
 */

import { describe, expect, it } from 'vitest';
import {
    SIGNAL_POSITION_HINT,
    SIGNAL_POSITION_LABELS,
    signalPositionLabel,
} from '../signalVocabulary';

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
