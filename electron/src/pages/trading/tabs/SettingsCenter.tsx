import React, { useEffect, useState } from 'react';
import {
  Activity,
  Check,
  Copy,
  Download,
  Eye,
  EyeOff,
  Key,
  RefreshCw,
  Settings,
  ShieldCheck,
} from 'lucide-react';
import { SERVICE_URLS } from '../../../config/services';

interface ApiKeyBootstrapInfo {
  id: number;
  access_key: string;
  name: string;
  permissions: string[];
  is_active: boolean;
  created_at: string;
  expires_at?: string | null;
  last_used_at?: string | null;
  secret_key?: string | null;
  just_created?: boolean;
}

interface BindingStatusInfo {
  online: boolean;
  user_id: string;
  tenant_id: string;
  account_id?: string | null;
  hostname?: string | null;
  client_version?: string | null;
  last_seen_at?: string | null;
  heartbeat_at?: string | null;
  account_reported_at?: string | null;
  stale_reason?: string | null;
}

interface RotateSecretInfo {
  access_key: string;
  secret_key: string;
}

interface SettingsCenterProps {
  userId: string;
  isActive: boolean;
}

interface QMTAgentReleaseAssetInfo {
  asset: string;
  key: string;
  file_name: string;
  download_url: string;
  sha256?: string | null;
  content_type?: string | null;
  expires_in: number;
}

interface QMTAgentReleaseDownloadInfo {
  product: string;
  channel: string;
  version: string;
  build_time?: string | null;
  manifest_key?: string | null;
  manifest_url?: string | null;
  selected_asset: string;
  installer?: QMTAgentReleaseAssetInfo | null;
  portable?: QMTAgentReleaseAssetInfo | null;
}

const SettingsCenter: React.FC<SettingsCenterProps> = ({ userId, isActive }) => {
  const apiGatewayBase = SERVICE_URLS.API_GATEWAY.replace(/\/+$/, '');
  const authHeader = () => ({
    'Content-Type': 'application/json',
    Authorization: `Bearer ${localStorage.getItem('access_token') || ''}`,
  });

  const [copied, setCopied] = useState('');
  const [keyInfo, setKeyInfo] = useState<ApiKeyBootstrapInfo | null>(null);
  const [bindingStatus, setBindingStatus] = useState<BindingStatusInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [statusLoading, setStatusLoading] = useState(false);
  const [showAccessKey, setShowAccessKey] = useState(false);
  const [showSecretKey, setShowSecretKey] = useState(false);
  const [secretKey, setSecretKey] = useState<string | null>(null);
  const hasBindingData = Boolean(
    bindingStatus?.account_id ||
    bindingStatus?.hostname ||
    bindingStatus?.client_version ||
    bindingStatus?.heartbeat_at ||
    bindingStatus?.account_reported_at
  );

  const handleCopy = async (text: string, key: string) => {
    await navigator.clipboard.writeText(text);
    setCopied(key);
    setTimeout(() => setCopied(''), 2000);
  };

  const maskValue = (value: string) => value.replace(/(.{8}).*(.{4})$/, '$1••••••••••••$2');
  const displayBindingValue = (value?: string | null) => {
    const text = String(value || '').trim();
    return text || '无数据上报';
  };
  const formatStaleReason = (reason?: string | null) => {
    switch (reason) {
      case 'heartbeat_stale':
        return '心跳已过期';
      case 'account_snapshot_stale':
        return '账户快照已过期';
      case 'no_data':
        return '当前无数据上报';
      default:
        return reason || '当前无数据上报';
    }
  };

  const fetchBootstrap = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${apiGatewayBase}/api/v1/api-keys/qmt-agent/bootstrap`, {
        method: 'POST',
        headers: authHeader(),
      });
      if (!res.ok) {
        throw new Error('bootstrap failed');
      }
      const data: ApiKeyBootstrapInfo = await res.json();
      setKeyInfo({
        ...data,
        access_key: String(data.access_key || '').trim(),
        secret_key: data.secret_key ? String(data.secret_key).trim() : null,
      });
      setSecretKey(data.secret_key ? String(data.secret_key).trim() : null);
    } catch (e) {
      console.error('Failed to bootstrap qmt agent key', e);
    } finally {
      setLoading(false);
    }
  };

  const fetchBindingStatus = async () => {
    setStatusLoading(true);
    try {
      const res = await fetch(
        `${apiGatewayBase}/api/v1/internal/strategy/bridge/binding/status?user_id=${userId}`,
        { headers: { Authorization: `Bearer ${localStorage.getItem('access_token') || ''}` } }
      );
      if (!res.ok) {
        throw new Error('binding status failed');
      }
      const data: BindingStatusInfo = await res.json();
      setBindingStatus(data);
    } catch (e) {
      console.error('Failed to fetch qmt binding status', e);
    } finally {
      setStatusLoading(false);
    }
  };

  const rotateSecret = async () => {
    if (!keyInfo?.access_key) return;
    setLoading(true);
    try {
      const res = await fetch(
        `${apiGatewayBase}/api/v1/api-keys/${keyInfo.access_key}/rotate-secret`,
        {
          method: 'POST',
          headers: authHeader(),
        }
      );
      if (!res.ok) {
        throw new Error('rotate secret failed');
      }
      const data: RotateSecretInfo = await res.json();
      setSecretKey(String(data.secret_key || '').trim());
      setShowSecretKey(true);
    } catch (e) {
      console.error('Failed to rotate secret key', e);
    } finally {
      setLoading(false);
    }
  };

  const handleDownloadQMT = async () => {
    const downloadUrl = 'https://www.quantmindai.cn/qmt-service';
    try {
      if (window.electronAPI?.openExternal) {
        const result = await window.electronAPI.openExternal(downloadUrl);
        if (!result?.success) {
          throw new Error(result?.error || 'open external browser failed');
        }
        return;
      }
      window.open(downloadUrl, '_blank', 'noopener,noreferrer');
    } catch (e) {
      console.error('Failed to download qmt agent client', e);
    }
  };

  useEffect(() => {
    if (!isActive) return;
    fetchBootstrap();
    fetchBindingStatus();
  }, [isActive, userId]);

  useEffect(() => {
    if (!isActive) return undefined;
    const timer = window.setInterval(() => {
      fetchBindingStatus();
    }, 15000);
    return () => window.clearInterval(timer);
  }, [isActive, userId]);

  if (!isActive) return null;

  return (
    <div className="h-full flex flex-col p-4 pb-[100px] bg-gray-50/30 overflow-y-auto custom-scrollbar">
      <div className="mb-4 pb-3 border-b border-gray-200">
        <h3 className="text-xl font-bold text-gray-800 flex items-center">
          <Settings className="mr-3 text-blue-600" size={24} />
          QMT 实盘连接中心
        </h3>
        <p className="text-xs text-gray-500 mt-1">
          Electron 仅负责用户维护与凭证管理；QMT Agent 需要作为独立程序部署在客户服务端并长期运行。
        </p>
      </div>

      <div className="bg-white rounded-3xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="grid grid-cols-1 xl:grid-cols-2 items-stretch">
          <div className="p-5 border-b xl:border-b-0 xl:border-r border-gray-100 flex flex-col gap-4 min-h-[640px]">
            <div className="space-y-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-sm font-bold text-gray-900">接入凭证</div>
                  <div className="text-xs text-gray-500 mt-1">
                    Access Key 用于绑定，Secret Key 仅在首次创建或重置后展示一次。
                  </div>
                </div>
                <button
                  onClick={fetchBootstrap}
                  disabled={loading}
                  className="shrink-0 text-xs text-indigo-500 hover:text-indigo-700 font-medium flex items-center gap-1"
                >
                  <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
                  刷新
                </button>
              </div>

              <div className="space-y-3">
                <div className="rounded-2xl border border-gray-100 bg-white px-4 py-3">
                  <div className="flex items-center gap-3">
                    <div className="p-2 bg-indigo-50 rounded-xl text-indigo-600 shrink-0">
                      <Key size={18} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="text-xs text-gray-500 mb-1">Access Key</div>
                      <div className="flex items-center gap-2 min-w-0 bg-white px-3 py-2 rounded-2xl border border-gray-100">
                        <code className="text-xs font-mono text-indigo-700 truncate flex-1">
                          {keyInfo ? (showAccessKey ? keyInfo.access_key : maskValue(keyInfo.access_key)) : '-'}
                        </code>
                        {keyInfo && (
                          <>
                            <button onClick={() => setShowAccessKey(!showAccessKey)} className="p-1 text-gray-500 hover:text-gray-700">
                              {showAccessKey ? <EyeOff size={14} /> : <Eye size={14} />}
                            </button>
                            <button onClick={() => handleCopy(keyInfo.access_key, 'access_key')} className="p-1 text-gray-500 hover:text-indigo-600">
                              {copied === 'access_key' ? <Check size={14} className="text-green-500" /> : <Copy size={14} />}
                            </button>
                            <div className={`hidden sm:flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-bold ${keyInfo.is_active ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                              <ShieldCheck size={10} />
                              {keyInfo.is_active ? '可用' : '已禁用'}
                            </div>
                          </>
                        )}
                      </div>
                    </div>
                  </div>
                </div>

                <div className="rounded-2xl border border-gray-100 bg-white px-4 py-3">
                  <div className="flex items-center gap-3">
                    <div className="p-2 bg-amber-50 rounded-xl text-amber-700 shrink-0">
                      <Key size={18} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="text-xs text-gray-500 mb-1">Secret Key</div>
                      <div className="flex items-center gap-3">
                        <div className="flex items-center gap-2 bg-white px-3 py-2 rounded-2xl border border-gray-100 flex-1 min-w-0">
                          <code className="text-xs font-mono text-amber-900 truncate flex-1">
                            {secretKey ? (showSecretKey ? secretKey : maskValue(secretKey)) : '未展示，点击右侧按钮重新生成'}
                          </code>
                          {secretKey && (
                            <>
                              <button onClick={() => setShowSecretKey(!showSecretKey)} className="p-1 text-gray-500 hover:text-gray-700">
                                {showSecretKey ? <EyeOff size={14} /> : <Eye size={14} />}
                              </button>
                              <button onClick={() => handleCopy(secretKey, 'secret_key')} className="p-1 text-gray-500 hover:text-amber-700">
                                {copied === 'secret_key' ? <Check size={14} className="text-green-500" /> : <Copy size={14} />}
                              </button>
                            </>
                          )}
                        </div>
                        <button
                          onClick={rotateSecret}
                          disabled={!keyInfo || loading}
                          className="shrink-0 px-3 py-2 rounded-xl bg-gray-900 text-white text-xs font-bold hover:bg-black disabled:opacity-50"
                        >
                          重置密钥
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div className="rounded-2xl border border-gray-100 bg-white px-4 py-4">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <Download size={16} className="text-blue-600" />
                    <span className="text-sm font-bold text-gray-800">下载独立 QMT Agent 包</span>
                  </div>
                  <p className="text-xs text-gray-500 leading-relaxed">
                    跳转至 QuantMind 官网下载 Windows 安装器。
                  </p>
                </div>
                <button
                  onClick={handleDownloadQMT}
                  className="shrink-0 flex items-center justify-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-xl text-xs font-bold transition-all"
                >
                  <Download size={14} />
                  下载安装器
                </button>
              </div>
            </div>
          </div>

          <div className="p-5 flex flex-col h-full min-h-[640px]">
            <div className="flex items-center gap-3">
              <div className={`p-3 rounded-xl ${bindingStatus?.online ? 'bg-green-50 text-green-600' : 'bg-white text-gray-500'}`}>
                <Activity size={20} />
              </div>
              <div>
                <h4 className="text-base font-bold text-gray-800">QMT Agent 在线状态</h4>
                <p className="text-xs text-gray-500 mt-1">
                  {statusLoading
                    ? '正在刷新状态...'
                    : !hasBindingData
                      ? '当前无数据上报'
                      : bindingStatus?.online
                        ? 'Agent 在线，可执行 REAL 启动门禁'
                        : 'Agent 未在线或最近心跳/账户快照已过期'}
                </p>
              </div>
            </div>

            {bindingStatus?.stale_reason && (
              <div className="mt-4 text-xs text-amber-700 bg-amber-50 border border-amber-100 rounded-xl px-3 py-2">
                当前状态未通过门禁：{formatStaleReason(bindingStatus.stale_reason)}
              </div>
            )}

            <div className="mt-4 grid grid-cols-2 gap-3 text-sm">
              <div className="rounded-2xl border border-gray-100 bg-white px-4 py-3">
                <div className="text-xs text-gray-500 mb-1">资金账号</div>
                <div className="font-semibold text-gray-900">{displayBindingValue(bindingStatus?.account_id)}</div>
              </div>
              <div className="rounded-2xl border border-gray-100 bg-white px-4 py-3">
                <div className="text-xs text-gray-500 mb-1">终端名称</div>
                <div className="font-semibold text-gray-900">{displayBindingValue(bindingStatus?.hostname)}</div>
              </div>
              <div className="rounded-2xl border border-gray-100 bg-white px-4 py-3">
                <div className="text-xs text-gray-500 mb-1">最近心跳</div>
                <div className="font-semibold text-gray-900">{displayBindingValue(bindingStatus?.heartbeat_at)}</div>
              </div>
              <div className="rounded-2xl border border-gray-100 bg-white px-4 py-3">
                <div className="text-xs text-gray-500 mb-1">账户快照</div>
                <div className="font-semibold text-gray-900">{displayBindingValue(bindingStatus?.account_reported_at)}</div>
              </div>
              <div className="rounded-2xl border border-gray-100 bg-white px-4 py-3">
                <div className="text-xs text-gray-500 mb-1">Agent 版本</div>
                <div className="font-semibold text-gray-900">{displayBindingValue(bindingStatus?.client_version)}</div>
              </div>
              <div className="rounded-2xl border border-gray-100 bg-white px-4 py-3">
                <div className="text-xs text-gray-500 mb-1">接入租户</div>
                <div className="font-semibold text-gray-900">{displayBindingValue(bindingStatus?.tenant_id)}</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default SettingsCenter;
