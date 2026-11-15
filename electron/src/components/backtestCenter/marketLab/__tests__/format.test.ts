import { describe, it, expect } from 'vitest';
import { errText, fmt, fmtMoney, fmtPct, fmtWhen, sanitizeStrategyTitle } from '../format';

describe('market lab format', () => {
  it('formats percent values that are already percentages', () => {
    // TradingView 口径：入参 62.35 = 62.35%
    expect(fmtPct(62.35)).toBe('+62.35%');
    expect(fmtPct(-3.1)).toBe('-3.10%');
    expect(fmtPct(3.1, 1, false)).toBe('3.1%');
  });

  it('renders missing values as — instead of 0', () => {
    expect(fmtPct(null)).toBe('—');
    expect(fmt(undefined)).toBe('—');
    expect(fmtMoney(null)).toBe('—');
  });

  it('formats money with the given unit and no decimals by default', () => {
    expect(fmtMoney(123456.7, '¥')).toBe('¥123,457');
    expect(fmtMoney(123456.7, '')).toBe('123,457');
  });

  it('prefers the backend detail in axios errors', () => {
    expect(errText({ response: { data: { detail: '未知复权口径: bad' } } })).toBe(
      '未知复权口径: bad',
    );
    expect(errText(new Error('boom'))).toBe('boom');
  });

  it('returns an empty string for unparsable timestamps', () => {
    expect(fmtWhen('not-a-date')).toBe('');
    expect(fmtWhen(undefined)).toBe('');
  });

  it('rewrites buy/sell words in displayed strategy titles', () => {
    expect(sanitizeStrategyTitle('TASC 2026.08农业卖出模型')).toBe('TASC 2026.08农业离场模型');
    expect(sanitizeStrategyTitle('WJ买入策略')).toBe('WJ入场策略');
    expect(sanitizeStrategyTitle('双均线交叉')).toBe('双均线交叉');
  });
});
