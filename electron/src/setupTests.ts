/**
 * 测试设置文件
 *
 * @author QuantMind Team
 * @date 2025-11-12
 */

import { expect, afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';
import '@testing-library/jest-dom';
import 'fake-indexeddb/auto';

const createStorageMock = () => {
  const store = new Map<string, string>();
  return {
    get length() {
      return store.size;
    },
    clear: vi.fn(() => {
      store.clear();
    }),
    getItem: vi.fn((key: string) => (store.has(key) ? store.get(key)! : null)),
    key: vi.fn((index: number) => Array.from(store.keys())[index] ?? null),
    removeItem: vi.fn((key: string) => {
      store.delete(key);
    }),
    setItem: vi.fn((key: string, value: string) => {
      store.set(key, String(value));
    }),
  };
};

const ensureStorage = (name: 'localStorage' | 'sessionStorage') => {
  const storage = (globalThis as Record<string, unknown>)[name];
  if (
    typeof storage !== 'object' ||
    storage === null ||
    typeof (storage as Record<string, unknown>).getItem !== 'function' ||
    typeof (storage as Record<string, unknown>).setItem !== 'function'
  ) {
    Object.defineProperty(globalThis, name, {
      writable: true,
      configurable: true,
      value: createStorageMock(),
    });
  }
};

ensureStorage('localStorage');
ensureStorage('sessionStorage');

// 每个测试后自动清理
afterEach(() => {
  cleanup();
});

// Mock环境变量
vi.mock('import.meta', () => ({
  env: {
    VITE_API_BASE_URL: 'http://localhost:8000',
    VITE_WS_BASE_URL: 'ws://localhost:8000',
  },
}));

// Mock window.matchMedia
if (typeof window !== 'undefined') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });

  // rc-table 会调用带 pseudoElt 参数的 getComputedStyle，jsdom 默认会抛 not implemented
  const originalGetComputedStyle = window.getComputedStyle.bind(window);
  window.getComputedStyle = ((elt: Element) => originalGetComputedStyle(elt)) as typeof window.getComputedStyle;
}

// Mock ResizeObserver (must be constructable with `new`)
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}

Object.defineProperty(globalThis, 'ResizeObserver', {
  writable: true,
  configurable: true,
  value: MockResizeObserver,
});

// jsdom 不实现 Element.scrollIntoView（日志面板等挂载即调），测试环境补空实现
if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn();
}
