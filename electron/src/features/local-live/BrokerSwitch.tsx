/**
 * 实盘顶栏券商切换：一眼看到当前实盘通道，点一下在 QMT / 通达信之间切换。
 *
 * 与「设置 → 券商接入」写的是同一把钥匙 `broker:selected:{market}`：
 * 切完对手动下单、止损执行器、决策轮派发、镜像/补单同时生效（单一活跃券商）。
 */
import React, { useCallback, useEffect, useState } from 'react';
import { Dropdown, message } from 'antd';
import { BankOutlined, DownOutlined } from '@ant-design/icons';
import { useAppSelector } from '../../store';
import { selectCurrentMarket } from '../../store/slices/uiSlice';
import { authService } from '../auth/services/authService';
import { SERVICE_URLS } from '../../config/services';

const apiBase = `${SERVICE_URLS.API_GATEWAY}/api/v1`;

interface BrokerItem {
  broker: string;
  label: string;
  configured: boolean;
}

export const BrokerSwitch: React.FC = () => {
  const market = useAppSelector(selectCurrentMarket);
  const [items, setItems] = useState<BrokerItem[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const authHeaders = useCallback(() => {
    const token = authService.getAccessToken();
    return token ? { Authorization: `Bearer ${token}` } : undefined;
  }, []);

  const load = useCallback(async () => {
    try {
      const resp = await fetch(`${apiBase}/broker-config-status?market=${market}`, {
        headers: authHeaders(),
      });
      if (!resp.ok) return;
      const data = await resp.json();
      setItems(data?.brokers ?? []);
      setSelected(data?.selected ?? null);
    } catch {
      /* 静默：顶栏不显示即可，不打扰交易主流程 */
    }
  }, [market, authHeaders]);

  useEffect(() => {
    void load();
  }, [load]);

  const select = async (broker: string) => {
    if (saving || broker === selected) return;
    setSaving(true);
    try {
      const resp = await fetch(`${apiBase}/broker-config/selected/${market}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ broker }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      setSelected(broker);
      const label = items.find((i) => i.broker === broker)?.label || broker;
      message.success(`实盘通道已切到 ${label}（下单 / 止损 / 决策轮即时生效）`);
    } catch (e) {
      message.error(e instanceof Error ? e.message : '切换失败');
    } finally {
      setSaving(false);
    }
  };

  if (!items.length && !selected) return null;

  const current = items.find((i) => i.broker === selected);
  const menu = {
    items: items.map((i) => ({
      key: i.broker,
      label: `${i.label}${i.configured ? '' : '（未配置）'}${i.broker === selected ? '  ✓' : ''}`,
    })),
    onClick: ({ key }: { key: string }) => {
      void select(key);
    },
  };

  return (
    <Dropdown menu={menu} trigger={['click']}>
      <button
        type="button"
        className="flex items-center gap-1 rounded-full border border-gray-200 bg-white px-3 py-1 text-xs text-gray-600 hover:border-blue-300"
        title="切换本市场实盘券商（与「设置 → 券商接入」同源）"
      >
        <BankOutlined />
        实盘：{current?.label || selected?.toUpperCase() || '未选择'}
        <DownOutlined style={{ fontSize: 10 }} />
      </button>
    </Dropdown>
  );
};
