import { useAppSelector } from '../../../store';
import BrokerConfigCard from '../components/BrokerConfigCard';
import BrokerChannelCard from '../components/BrokerChannelCard';
import QmtMirrorCard from '../components/QmtMirrorCard';
import { selectCurrentMarket } from '../../../store/slices/uiSlice';
import React, { useEffect, useState } from 'react';
import { BankOutlined } from '@ant-design/icons';
import { KeyRound, Settings, ShieldAlert } from 'lucide-react';
import { isLiveTradingEnabled } from '../../../config/tradingFlags';
import { modeCopy } from '../utils/tradingModeCopy';

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
   * 是否允许出现实盘配置面板（券商实盘接入 / 真单镜像）。缺省允许。
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
   * 追加面板（排在「真单镜像」之后）。缺省不追加 —— 见 `SettingsExtraPanel`。
   * 与内置栏同规矩：**条件挂载**（只有选中时才渲染），切走即卸载，不留后台轮询。
   */
  extraPanels?: readonly SettingsExtraPanel[];
}

const SettingsCenter: React.FC<SettingsCenterProps> = ({ isActive, liveConfigVisible = true, tradingMode = 'simulation', extraPanels }) => {
  const currentMarket = useAppSelector(selectCurrentMarket);
  // 接入凭证 / API 密钥已迁至「后台管理 → 系统设置」（2026-11-28）：
  // 那是外部 SDK 的长期凭据，不是交易账户设置。本页只留实盘配置与调用方追加面板。
  const liveEnabled = isLiveTradingEnabled() && liveConfigVisible;
  // 凭证页迁走后，无实盘面板时默认落在「已迁移」说明页；追加面板仍保持原本的
  // 「点了才挂载」行为（不因为凭证页没了就自动挂载第一块）
  const fallbackTab = () => 'migrated';
  const [activeTab, setActiveTab] = useState<string>(() =>
    liveEnabled ? 'brokers' : fallbackTab(),
  );

  // 真单镜像仅 A 股；切换市场后回到券商通道（或迁移提示）
  useEffect(() => {
    if (currentMarket !== 'CN' && activeTab === 'mirror') {
      setActiveTab(liveEnabled ? 'brokers' : fallbackTab());
    }
  }, [activeTab, currentMarket, liveEnabled]);

  // 实盘开关在会话内失效（重新构建后热更新）时回落到迁移提示。
  // 只管两个**跟随实盘开关**的内置栏：追加面板（extraPanels）的可见性由调用方决定，
  // 在这里一并踢走会把本机实盘栏的「总控/数据」误伤（开关一断就再也点不开）。
  useEffect(() => {
    if (!liveEnabled && (activeTab === 'brokers' || activeTab === 'mirror')) {
      setActiveTab(fallbackTab());
    }
  }, [activeTab, liveEnabled]);

  if (!isActive) return null;

  const showTabStrip = liveEnabled || (extraPanels?.length ?? 0) > 0;

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
            ? '管理券商实盘通道与真单镜像；接入凭证已迁至「后台管理 → 系统设置」。'
            : '接入凭证 / API 密钥已迁至「后台管理 → 系统设置」。'}
        </p>
      </div>

      {/* 顶部切换按钮（凭证已迁走；没有实盘面板/追加面板时不渲染整条） */}
      {showTabStrip && (
      <div data-testid="settings-tabs" className="px-4 py-3 bg-gray-50/30 shrink-0 flex items-center gap-2">
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
          真单镜像
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
      )}

      {/* 内容区：各面板独立滚动 */}
      <div className="flex-1 min-h-0 px-4 pb-4">
        {activeTab === 'migrated' && (
        <div className="h-full bg-white rounded-3xl border border-gray-200 shadow-sm flex flex-col items-center justify-center text-center p-8">
          <div className="w-14 h-14 rounded-2xl bg-indigo-50 text-indigo-600 flex items-center justify-center">
            <KeyRound size={26} />
          </div>
          <h4 className="mt-4 text-base font-bold text-gray-900">接入凭证 / API 密钥已迁至后台管理</h4>
          <p className="mt-2 max-w-md text-xs leading-relaxed text-gray-500">
            这是外部系统 / 智能体接入本平台的 SDK 长期凭据（Access Key / Secret Key），
            不是交易账户设置。请到「后台管理 → 系统设置 → SDK 接入凭证」维护。
          </p>
          <button
            type="button"
            onClick={() => { window.location.hash = '#/admin/settings'; }}
            className="mt-5 inline-flex items-center gap-1.5 rounded-xl bg-indigo-600 px-4 py-2 text-xs font-bold text-white hover:bg-indigo-700 transition-colors"
          >
            <KeyRound size={14} /> 前往系统设置
          </button>
        </div>
        )}

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

        {/* 真单镜像（仅 A 股；券商可为大 QMT 执行端或通达信 TDX 桥） */}
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
