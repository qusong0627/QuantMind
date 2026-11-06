import { afterEach, describe, expect, it } from 'vitest';
import { getSessionScopedStorageKey } from '../../../store/sessionScopedStorage';
import { getStoredDirectionConfig } from './miningDirections';

describe('mining direction preferences', () => {
  afterEach(() => localStorage.clear());

  it('reads preferences only from the active tenant and user key', () => {
    localStorage.setItem('user', JSON.stringify({ tenant_id: 'tenant-a', user_id: 'alice' }));
    const aliceKey = getSessionScopedStorageKey('autoalpha_config');
    expect(aliceKey).not.toBeNull();
    localStorage.setItem(aliceKey!, JSON.stringify({
      selectedMiningDirections: ['Alice private direction'],
      miningDirectionMode: 'random',
    }));

    expect(getStoredDirectionConfig()).toEqual({
      labels: ['Alice private direction'],
      mode: 'random',
    });

    localStorage.setItem('user', JSON.stringify({ tenant_id: 'tenant-a', user_id: 'bob' }));
    expect(getStoredDirectionConfig()).toEqual({ labels: [], mode: 'selected' });
  });

  it('fails closed when no authenticated owner key can be built', () => {
    localStorage.setItem('autoalpha_config', JSON.stringify({
      selectedMiningDirections: ['legacy shared direction'],
      miningDirectionMode: 'random',
    }));
    expect(getStoredDirectionConfig()).toEqual({ labels: [], mode: 'selected' });
  });

  it('accepts the stored quota mode and rejects unknown modes', () => {
    localStorage.setItem('user', JSON.stringify({ tenant_id: 'tenant-a', user_id: 'alice' }));
    const aliceKey = getSessionScopedStorageKey('autoalpha_config');
    expect(aliceKey).not.toBeNull();
    localStorage.setItem(aliceKey!, JSON.stringify({
      selectedMiningDirections: ['Alice private direction'],
      miningDirectionMode: 'quota',
    }));
    expect(getStoredDirectionConfig()).toEqual({
      labels: ['Alice private direction'],
      mode: 'quota',
    });

    localStorage.setItem(aliceKey!, JSON.stringify({
      selectedMiningDirections: ['Alice private direction'],
      miningDirectionMode: 'bogus',
    }));
    expect(getStoredDirectionConfig()).toEqual({
      labels: ['Alice private direction'],
      mode: 'selected',
    });
  });
});
