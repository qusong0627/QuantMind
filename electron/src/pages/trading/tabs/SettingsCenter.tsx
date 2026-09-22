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
import { isLiveTradingEnabled } from '../../../config/tradingFlags';
import { modeCopy } from '../utils/tradingModeCopy';

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

/**
 * 追加到设置页顶部按钮条的面板（与 RealTradingExtraTab 同一约定：**公开树不感知调用方**）。
 *
 * 本机实盘栏用它把 arena 的「总控」「数据」两块嵌进设置里 —— 用户口径「总控放设置里面、
 * 数据也放设置里面」，即不在交易台另起入口。无调用方时 `extraPanels` 为 undefined，
 * 追加分支整段不参与渲染，公开仓形态与本机制引入前逐位相同。
 */
export interface SettingsExtraPanel {
  id: string;
  label: string;
  icon?: React.ComponentType<{ size?: number | string; className?: string }>;
  render: () => React.ReactNode;
  /** 点开这一栏时的标题色（缺省 indigo，与内置栏一致） */
  accent?: 'indigo' | 'rose';
}

interface SettingsCenterProps {
  userId: string;
  isActive: boolean;
  /**
   * 是否允许出现实盘配置面板（券商实盘接入 / 大 QMT 真单镜像）。缺省允许。
   *
   * 由调用方按**本栏口径**给：定死模拟盘的「模拟交易」栏目传 false——两栏各管一边
   * 之后，实盘配置留在模拟栏里就等于把实盘又搬回来了（改完凭证下一步就是下单）。
   * 公开树只有一栏、模式由开关切换，不传 = 维持原行为。
   */
  liveConfigVisible?: boolean;
  /**
   * 本栏的**生效模式**（`resolveConsoleTradingMode` 之后的值，不是全局偏好）。
   * 只用来给标题取词。缺省按模拟盘——未知模式一律判为模拟，与
   * `normalizeTradingMode` 同向：宁可少认一个实盘。
   */
  tradingMode?: 'real' | 'simulation';
  /**
   * 追加面板（排在「大 QMT 真单镜像」之后）。缺省不追加 —— 见 `SettingsExtraPanel`。
   * 与内置栏同规矩：**条件挂载**（只有选中时才渲染），切走即卸载，不留后台轮询。
   */
  extraPanels?: readonly SettingsExtraPanel[];
}

const SettingsCenter: React.FC<SettingsCenterProps> = ({ userId, isActive, liveConfigVisible = true, tradingMode = 'simulation', extraPanels }) => {
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

  // 实盘开关关闭（或本栏不含实盘配置）时只有凭证页存在；初值直接落 credentials，
  // 避免首帧选中一个不渲染的面板
  const liveEnabled = isLiveTradingEnabled() && liveConfigVisible;
  const [activeTab, setActiveTab] = useState<string>('credentials');

  // 大 QMT 真单镜像仅 A 股；切换市场后回到凭证页，避免停在无入口的面板上
  useEffect(() => {
    if (currentMarket !== 'CN' && activeTab === 'mirror') {
      setActiveTab('credentials');
    }
  }, [currentMarket, activeTab]);

  // 实盘开关在会话内失效（重新构建后热更新）时同样回落到凭证页。
  // 只管两个**跟随实盘开关**的内置栏：追加面板（extraPanels）的可见性由调用方决定，
  // 在这里一并踢回凭证页会把本机实盘栏的「总控/数据」误伤（开关一断就再也点不开）。
  useEffect(() => {
    if (!liveEnabled && (activeTab === 'brokers' || activeTab === 'mirror')) {
      setActiveTab('credentials');
    }
  }, [liveEnabled, activeTab]);

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
          {/* 标题跟随本栏模式：实盘栏里顶着「模拟交易设置」配真券商账号，
              正是本次分栏要消除的错配（`modeCopy` 是模式文案唯一事实源）。 */}
          {modeCopy(tradingMode).full}设置
        </h3>
        <p className="text-xs text-gray-500 mt-1">
          {liveEnabled
            ? '管理接入凭证、券商实盘通道与大 QMT 真单镜像。'
            : '管理接入凭证与 API 密钥。'}
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
        {liveEnabled && (
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
        )}
        {liveEnabled && currentMarket === 'CN' && (
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
        {/* 追加面板（本机实盘栏：总控 / 数据）。不跟实盘开关联动——内容由调用方负责 */}
        {extraPanels?.map((panel) => {
          const Icon = panel.icon;
          const isOn = activeTab === panel.id;
          return (
            <button
              key={panel.id}
              onClick={() => setActiveTab(panel.id)}
              className={`px-4 py-2 rounded-xl text-xs font-bold transition-colors ${
                isOn
                  ? `bg-white ${panel.accent === 'rose' ? 'text-rose-700 border-rose-200' : 'text-indigo-700 border-indigo-200'} border shadow-sm`
                  : 'bg-white/60 text-gray-500 border border-gray-200 hover:text-gray-700'
              }`}
            >
              {Icon && <Icon size={13} className="inline mr-1.5 -mt-0.5" />}
              {panel.label}
            </button>
          );
        })}
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

        {/* 券商实盘接入卡片：实盘开关关闭时整个不挂载——
            注意这两个面板原本用 `hidden` 类常驻挂载，只藏 tab 不够，
            组件仍会发请求（在实盘关闭的部署上会拿到 403 并刷错误提示）。 */}
        {liveEnabled && (
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
        )}

        {/* 大 QMT 真单镜像（仅 A 股） */}
        {liveEnabled && (
        <div
          className={`h-full bg-white rounded-3xl border border-gray-200 shadow-sm overflow-y-auto custom-scrollbar ${
            activeTab === 'mirror' ? '' : 'hidden'
          }`}
        >
          <div className="p-5">
            <QmtMirrorCard />
          </div>
        </div>
        )}

        {/* 追加面板（本机实盘栏）：与内置栏同规矩——**选中才挂载**，切走即卸载。
            面板自己铺满卡片并自管滚动（内嵌 arena 页面时给 min-h-full，滚动留给本卡片）。 */}
        {extraPanels?.map((panel) =>
          activeTab === panel.id ? (
            <div
              key={panel.id}
              className="h-full bg-white rounded-3xl border border-gray-200 shadow-sm overflow-y-auto custom-scrollbar"
            >
              {panel.render()}
            </div>
          ) : null,
        )}
      </div>
    </div>
  );
};

export default SettingsCenter;
