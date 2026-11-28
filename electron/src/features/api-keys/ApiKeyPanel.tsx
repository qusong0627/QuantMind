/**
 * SDK 接入凭证（Access Key / Secret Key）—— 后台管理 · 系统设置。
 *
 * 2026-11-28 自模拟交易「设置中心」迁入：这对密钥不是交易账户设置，而是
 * **外部系统/智能体接入本平台的长期凭据**——外部节点拿它调
 * `POST /api/ext/v1/auth/session` 换短期令牌，再读取平台数据、投递任务、接收信号。
 * 所以入口归「后台管理 → 系统设置」。
 *
 * 两条不能省的口径：
 * - Secret 只在**重置后展示一次**（后端 bcrypt 存哈希），刷新后永远看不到明文；
 * - 重置会立刻作废旧 Secret，外部节点必须同步换配置 → 必须二次确认。
 */
import React, { useCallback, useEffect, useState } from 'react';
import { Button, Modal, Tag, Tooltip, message } from 'antd';
import {
  Check,
  Copy,
  Eye,
  EyeOff,
  KeyRound,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react';
import { SERVICE_URLS } from '../../config/services';

interface ApiKeyInfo {
  id: number;
  access_key: string;
  name: string;
  permissions: string[];
  is_active: boolean;
  created_at: string;
  expires_at?: string | null;
  last_used_at?: string | null;
}

interface RotateSecretInfo {
  access_key: string;
  secret_key: string;
}

const maskValue = (value: string) =>
  value.length > 12 ? value.replace(/(.{8}).*(.{4})$/, '$1••••••••••••$2') : '••••••••';

export const ApiKeyPanel: React.FC = () => {
  const apiGatewayBase = SERVICE_URLS.API_GATEWAY.replace(/\/+$/, '');
  const authHeader = useCallback(
    () => ({
      'Content-Type': 'application/json',
      Authorization: `Bearer ${localStorage.getItem('access_token') || ''}`,
    }),
    [],
  );

  const [keyInfo, setKeyInfo] = useState<ApiKeyInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [showAccessKey, setShowAccessKey] = useState(false);
  const [showSecretKey, setShowSecretKey] = useState(false);
  const [secretKey, setSecretKey] = useState<string | null>(null);
  const [copied, setCopied] = useState('');

  const handleCopy = async (text: string, key: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      window.setTimeout(() => setCopied(''), 2000);
    } catch {
      message.error('复制失败，请手动选择文本复制');
    }
  };

  const fetchBootstrap = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${apiGatewayBase}/api/v1/api-keys/init`, {
        method: 'POST',
        headers: authHeader(),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: ApiKeyInfo = await res.json();
      setKeyInfo({ ...data, access_key: String(data.access_key || '').trim() });
    } catch (e) {
      console.error('Failed to init api key', e);
      message.error('读取接入凭证失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  }, [apiGatewayBase, authHeader]);

  const rotateSecret = async () => {
    if (!keyInfo?.access_key) return;
    setLoading(true);
    try {
      const res = await fetch(
        `${apiGatewayBase}/api/v1/api-keys/${keyInfo.access_key}/rotate-secret`,
        { method: 'POST', headers: authHeader() },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: RotateSecretInfo = await res.json();
      setSecretKey(String(data.secret_key || '').trim());
      setShowSecretKey(true);
      message.success('Secret Key 已重置，请立即保存（只展示这一次）');
    } catch (e) {
      console.error('Failed to rotate secret key', e);
      message.error('重置 Secret Key 失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  };

  const confirmRotate = () => {
    Modal.confirm({
      title: '重置 Secret Key？',
      content:
        '重置后旧 Secret 立即失效，所有使用它的外部系统/智能体都必须同步更新配置后才能重新握手。',
      okText: '重置',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: rotateSecret,
    });
  };

  useEffect(() => {
    void fetchBootstrap();
  }, [fetchBootstrap]);

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-bold text-slate-800">SDK 接入凭证</div>
          <div className="mt-1 text-xs leading-relaxed text-slate-500">
            供<b>外部系统 / 智能体</b>接入本平台：用 Access Key + Secret Key 调
            <code className="mx-1 rounded bg-slate-100 px-1 py-0.5 text-[11px]">POST /api/ext/v1/auth/session</code>
            换短期令牌后，读取平台数据、投递任务、接收交易信号。
          </div>
          <div className="mt-1 text-[11px] text-amber-600">
            Secret Key 只在重置后展示一次，请立即保存；刷新页面不会再次显示。
          </div>
        </div>
        <Button
          size="small"
          icon={<RefreshCw size={13} className={loading ? 'animate-spin' : ''} />}
          onClick={() => void fetchBootstrap()}
          disabled={loading}
        >
          刷新
        </Button>
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3 min-w-0">
          <div className="mb-2 flex items-center gap-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-indigo-50 text-indigo-600">
              <KeyRound size={16} />
            </span>
            <span className="text-xs font-bold text-slate-500">Access Key</span>
            {keyInfo && (
              <Tag
                color={keyInfo.is_active ? 'success' : 'error'}
                className="m-0 ml-auto border-none text-[10px]"
              >
                <ShieldCheck size={10} className="mr-1 inline" />
                {keyInfo.is_active ? '可用' : '已禁用'}
              </Tag>
            )}
          </div>
          <div className="flex min-w-0 items-center gap-2 rounded-xl border border-slate-100 bg-slate-50/50 px-3 py-2">
            <code className="flex-1 truncate font-mono text-xs text-indigo-700">
              {keyInfo ? (showAccessKey ? keyInfo.access_key : maskValue(keyInfo.access_key)) : '—'}
            </code>
            {keyInfo && (
              <>
                <button
                  type="button"
                  aria-label={showAccessKey ? '隐藏 Access Key' : '显示 Access Key'}
                  onClick={() => setShowAccessKey(!showAccessKey)}
                  className="p-1 text-slate-400 hover:text-slate-700"
                >
                  {showAccessKey ? <EyeOff size={14} /> : <Eye size={14} />}
                </button>
                <button
                  type="button"
                  aria-label="复制 Access Key"
                  onClick={() => void handleCopy(keyInfo.access_key, 'access_key')}
                  className="p-1 text-slate-400 hover:text-indigo-600"
                >
                  {copied === 'access_key' ? <Check size={14} className="text-emerald-500" /> : <Copy size={14} />}
                </button>
              </>
            )}
          </div>
        </div>

        <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3 min-w-0">
          <div className="mb-2 flex items-center gap-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-amber-50 text-amber-700">
              <KeyRound size={16} />
            </span>
            <span className="text-xs font-bold text-slate-500">Secret Key</span>
            <Tooltip title="旧 Secret 会立即失效，外部系统需同步换配置">
              <Button
                size="small"
                danger
                className="ml-auto"
                disabled={!keyInfo || loading}
                onClick={confirmRotate}
              >
                重置密钥
              </Button>
            </Tooltip>
          </div>
          <div className="flex min-w-0 items-center gap-2 rounded-xl border border-slate-100 bg-slate-50/50 px-3 py-2">
            <code className="flex-1 truncate font-mono text-xs text-amber-900">
              {secretKey
                ? showSecretKey
                  ? secretKey
                  : maskValue(secretKey)
                : '未展示（重置后显示一次）'}
            </code>
            {secretKey && (
              <>
                <button
                  type="button"
                  aria-label={showSecretKey ? '隐藏 Secret Key' : '显示 Secret Key'}
                  onClick={() => setShowSecretKey(!showSecretKey)}
                  className="p-1 text-slate-400 hover:text-slate-700"
                >
                  {showSecretKey ? <EyeOff size={14} /> : <Eye size={14} />}
                </button>
                <button
                  type="button"
                  aria-label="复制 Secret Key"
                  onClick={() => void handleCopy(secretKey, 'secret_key')}
                  className="p-1 text-slate-400 hover:text-amber-700"
                >
                  {copied === 'secret_key' ? <Check size={14} className="text-emerald-500" /> : <Copy size={14} />}
                </button>
              </>
            )}
          </div>
        </div>
      </div>

      {keyInfo && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-400">
          <span>名称：<b className="text-slate-600">{keyInfo.name || '—'}</b></span>
          <span>权限：<b className="font-mono text-slate-600">{keyInfo.permissions?.join(' / ') || '—'}</b></span>
          <span>最近使用：{keyInfo.last_used_at || '从未'}</span>
          <span>到期：{keyInfo.expires_at || '永久'}</span>
        </div>
      )}
    </div>
  );
};

export default ApiKeyPanel;
