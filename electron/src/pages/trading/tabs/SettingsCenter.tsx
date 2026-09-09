import { useAppSelector } from '../../../store';
import BrokerConfigCard from '../components/BrokerConfigCard';
import BrokerChannelCard from '../components/BrokerChannelCard';
import QmtMirrorCard from '../components/QmtMirrorCard';
import { selectCurrentMarket } from '../../../store/slices/uiSlice';
import React, { useEffect, useState } from 'react';
import { BankOutlined } from '@ant-design/icons';
import {
  Check,
  Copy,
  Eye,
  EyeOff,
  Key,
  RefreshCw,
  Settings,
  ShieldAlert,
  ShieldCheck,
} from 'lucide-react';
import { SERVICE_URLS } from '../../../config/services';

// 与后端 ApiKeyInfo 对齐：/api-keys/init 是幂等接口，永不返回 secret_key
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

interface SettingsCenterProps {
  userId: string;
  isActive: boolean;
}

const SettingsCenter: React.FC<SettingsCenterProps> = ({ userId, isActive }) => {
    const currentMarket = useAppSelector(selectCurrentMarket);
  const apiGatewayBase = SERVICE_URLS.API_GATEWAY.replace(/\/+$/, '');
  const authHeader = () => ({
    'Content-Type': 'application/json',
    Authorization: `Bearer ${localStorage.getItem('access_token') || ''}`,
  });

  const [copied, setCopied] = useState('');
  const [keyInfo, setKeyInfo] = useState<ApiKeyInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [showAccessKey, setShowAccessKey] = useState(false);
  const [showSecretKey, setShowSecretKey] = useState(false);
  const [secretKey, setSecretKey] = useState<string | null>(null);

  const [activeTab, setActiveTab] = useState<'credentials' | 'brokers' | 'mirror'>('credentials');

  const handleCopy = async (text: string, key: string) => {
    await navigator.clipboard.writeText(text);
    setCopied(key);
    setTimeout(() => setCopied(''), 2000);
  };

  const maskValue = (value: string) => value.replace(/(.{8}).*(.{4})$/, '$1••••••••••••$2');

  const fetchBootstrap = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${apiGatewayBase}/api/v1/api-keys/init`, {
        method: 'POST',
        headers: authHeader(),
      });
      if (!res.ok) {
        throw new Error('init failed');
      }
      const data: ApiKeyInfo = await res.json();
      setKeyInfo({
        ...data,
        access_key: String(data.access_key || '').trim(),
      });
    } catch (e) {
      console.error('Failed to init api key', e);
    } finally {
      setLoading(false);
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

  useEffect(() => {
    if (!isActive) return;
    fetchBootstrap();
  }, [isActive, userId]);

  if (!isActive) return null;

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <div className="px-4 pt-4 pb-3 border-b border-gray-200 bg-gray-50/30 shrink-0">
        <h3 className="text-xl font-bold text-gray-800 flex items-center">
          <Settings className="mr-3 text-blue-600" size={24} />
          模拟交易设置
        </h3>
        <p className="text-xs text-gray-500 mt-1">
          管理接入凭证与 API 密钥。
        </p>
      </div>

      {/* 顶部切换按钮 */}
      <div className="px-4 py-3 bg-gray-50/30 shrink-0 flex items-center gap-2">
        <button
          onClick={() => setActiveTab('credentials')}
          className={`px-4 py-2 rounded-xl text-xs font-bold transition-colors ${
            activeTab === 'credentials'
              ? 'bg-white text-indigo-700 border border-indigo-200 shadow-sm'
              : 'bg-white/60 text-gray-500 border border-gray-200 hover:text-gray-700'
          }`}
        >
          <Key size={13} className="inline mr-1.5 -mt-0.5" />
          接入凭证 / API 密钥
        </button>
        <button
          onClick={() => setActiveTab('brokers')}
          className={`px-4 py-2 rounded-xl text-xs font-bold transition-colors ${
            activeTab === 'brokers'
              ? 'bg-white text-indigo-700 border border-indigo-200 shadow-sm'
              : 'bg-white/60 text-gray-500 border border-gray-200 hover:text-gray-700'
          }`}
        >
          <BankOutlined className="inline mr-1.5 -mt-0.5" />
          券商实盘接入
        </button>
        {currentMarket === 'CN' && (
        <button
          onClick={() => setActiveTab('mirror')}
          className={`px-4 py-2 rounded-xl text-xs font-bold transition-colors ${
            activeTab === 'mirror'
              ? 'bg-white text-rose-700 border border-rose-200 shadow-sm'
              : 'bg-white/60 text-gray-500 border border-gray-200 hover:text-gray-700'
          }`}
        >
          <ShieldAlert size={13} className="inline mr-1.5 -mt-0.5" />
          大 QMT 真单镜像
        </button>
        )}
      </div>

      {/* 内容区：两个面板各自独立滚动 */}
      <div className="flex-1 min-h-0 px-4 pb-4">
        <div
          className={`h-full bg-white rounded-3xl border border-gray-200 shadow-sm overflow-y-auto custom-scrollbar ${
            activeTab === 'credentials' ? '' : 'hidden'
          }`}
        >
          <div className="p-5 flex flex-col gap-4">
          <div className="space-y-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-sm font-bold text-gray-900">接入凭证</div>
                <div className="text-xs text-gray-500 mt-1">
                  Access Key 用于鉴权，Secret Key 仅在重置后展示一次，请立即保存。
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

            <div className="grid grid-cols-2 gap-3">
              <div className="rounded-2xl border border-gray-100 bg-white px-4 py-3 min-w-0">
                <div className="flex items-center gap-2 mb-2">
                  <div className="p-2 bg-indigo-50 rounded-xl text-indigo-600 shrink-0">
                    <Key size={18} />
                  </div>
                  <div className="text-xs text-gray-500">Access Key</div>
                </div>
                <div className="min-w-0">
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

              <div className="rounded-2xl border border-gray-100 bg-white px-4 py-3 min-w-0">
                <div className="flex items-center gap-2 mb-2">
                  <div className="p-2 bg-amber-50 rounded-xl text-amber-700 shrink-0">
                    <Key size={18} />
                  </div>
                  <div className="text-xs text-gray-500">Secret Key</div>
                </div>
                <div className="min-w-0">
                    <div className="flex items-center gap-2">
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
        </div>

        {/* 券商实盘接入卡片 */}
        <div
          className={`h-full bg-white rounded-3xl border border-gray-200 shadow-sm overflow-y-auto custom-scrollbar ${
            activeTab === 'brokers' ? '' : 'hidden'
          }`}
        >
          <div className="p-5 space-y-4">
            <BrokerChannelCard market={currentMarket} />
            <BrokerConfigCard market={currentMarket} />
          </div>
        </div>

        {/* 大 QMT 真单镜像（仅 A 股） */}
        <div
          className={`h-full bg-white rounded-3xl border border-gray-200 shadow-sm overflow-y-auto custom-scrollbar ${
            activeTab === 'mirror' ? '' : 'hidden'
          }`}
        >
          <div className="p-5">
            <QmtMirrorCard />
          </div>
        </div>
      </div>
    </div>
  );
};

export default SettingsCenter;
