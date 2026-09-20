import { describe, expect, it } from 'vitest';
import {
  containsSimulationWording,
  modeCopy,
  normalizeTradingMode,
  SIMULATION_WORDING,
} from '../tradingModeCopy';

describe('tradingModeCopy', () => {
  it('normalizes every spelling the platform uses for the two modes', () => {
    expect(normalizeTradingMode('REAL')).toBe('REAL');
    expect(normalizeTradingMode('real')).toBe('REAL');
    expect(normalizeTradingMode('实盘')).toBe('REAL');
    expect(normalizeTradingMode('live')).toBe('REAL');

    expect(normalizeTradingMode('SIMULATION')).toBe('SIMULATION');
    expect(normalizeTradingMode('sim')).toBe('SIMULATION');
    expect(normalizeTradingMode('模拟')).toBe('SIMULATION');

    // 未知/空 → 模拟（与后端 Form 默认 SIMULATION 一致，不擅自升级为实盘）
    expect(normalizeTradingMode(undefined)).toBe('SIMULATION');
    expect(normalizeTradingMode(null)).toBe('SIMULATION');
    expect(normalizeTradingMode('who-knows')).toBe('SIMULATION');
  });

  it('never labels a REAL deployment with simulation wording (D1/D2/D3)', () => {
    const real = modeCopy('REAL');
    expect(real.isReal).toBe(true);
    for (const text of [real.short, real.full, real.startButton, real.stopButton, real.wizardTitle, real.bannerTitle, real.bannerSubtitle]) {
      expect(containsSimulationWording(text)).toBe(false);
    }
    expect(real.startButton).toContain('实盘');
  });

  it('marks a SIMULATION deployment as simulation everywhere', () => {
    const sim = modeCopy('SIMULATION');
    expect(sim.isReal).toBe(false);
    expect(sim.startButton).toContain('模拟');
    expect(sim.wizardTitle).toContain('模拟');
  });

  it('gives REAL and SIMULATION distinct badge styling (one style source)', () => {
    expect(modeCopy('REAL').badgeClass).not.toBe(modeCopy('SIMULATION').badgeClass);
  });

  it('detects the simulation wording families used across the UI', () => {
    expect(containsSimulationWording('启动模拟交易')).toBe(true);
    expect(containsSimulationWording('全自动实盘模拟控制台')).toBe(true); // D3 的实际文案
    expect(containsSimulationWording('模拟执行参数')).toBe(true);
    expect(containsSimulationWording('启动实盘交易')).toBe(false);
    expect(containsSimulationWording('')).toBe(false);
    expect(SIMULATION_WORDING.length).toBeGreaterThan(0);
  });
});
