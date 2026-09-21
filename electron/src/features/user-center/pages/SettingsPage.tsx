/**
 * 设置中心页面
 */

import React, { useEffect, useState } from 'react';
import { useUserConfig, useNotificationSettings, usePrivacySettings } from '../hooks';
import { Form, Switch, message, Spin, Alert } from 'antd';
import { Bell, ShieldCheck, Mail, Smartphone, Zap, Globe, MessageCircle, BarChart3, Users, RefreshCw, Info } from 'lucide-react';
import {
  systemService,
  type ProgrammaticTradingDisclosure,
  type SystemVersion,
} from '../../../services/systemService';
import { ComplianceFooter } from '../../../components/shared/compliance/ComplianceChrome';

interface SettingsPageProps {
  userId: string;
}

const SettingsPage: React.FC<SettingsPageProps> = ({ userId }) => {
  const [versionInfo, setVersionInfo] = useState<SystemVersion | null>(null);
  const [disclosure, setDisclosure] = useState<ProgrammaticTradingDisclosure | null>(null);
  const [checkingUpdate, setCheckingUpdate] = useState(false);

  const loadVersion = (force = false) => {
    systemService
      .getVersion(force)
      .then((v) => setVersionInfo(v))
      .catch(() => setVersionInfo(null));
  };

  useEffect(() => {
    loadVersion();
    // 程序化交易报告的待填报信息：取不到就整块不渲染（宁可不显示，也不显示半截）
    systemService
      .getProgrammaticTradingDisclosure()
      .then(setDisclosure)
      .catch(() => setDisclosure(null));
  }, []);

  // 主动刷新「落后上游」检查（绕过缓存实时请求上游平台）
  const refreshUpdateCheck = () => {
    setCheckingUpdate(true);
    systemService
      .getVersion(true)
      .then((v) => setVersionInfo(v))
      .finally(() => setCheckingUpdate(false));
  };
  const { config, isLoading, error } = useUserConfig(userId);
  const {
    settings: notificationSettings,
    updateSettings: updateNotificationSettings,
    updateStatus: notificationUpdateStatus,
  } = useNotificationSettings(userId);
  const {
    settings: privacySettings,
    updateSettings: updatePrivacySettings,
    updateStatus: privacyUpdateStatus,
  } = usePrivacySettings(userId);

  const handleNotificationChange = async (key: string, value: boolean) => {
    try {
      await updateNotificationSettings({
        [key]: value,
      });
      message.success('通知设置已更新');
    } catch (err: any) {
      message.error(err.message || '更新失败');
    }
  };

  const handlePrivacyChange = async (key: string, value: boolean | string) => {
    try {
      await updatePrivacySettings({
        [key]: value,
      });
      message.success('隐私设置已更新');
    } catch (err: any) {
      message.error(err.message || '更新失败');
    }
  };

  if (isLoading) {
    return (
      <div style={{ textAlign: 'center', padding: 50 }}>
        <Spin size="large" tip="加载中...">
          <div style={{ height: 100 }} />
        </Spin>
      </div>
    );
  }

  if (error) {
    return <Alert message="错误" description={error} type="error" showIcon />;
  }

  return (
    <div className="settings-page max-w-4xl mx-auto space-y-8">
      {/* 系统信息 */}
      <div className="bg-white rounded-2xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="p-6 border-b border-gray-100 bg-slate-50/50 flex items-center gap-3">
          <Info className="w-5 h-5 text-slate-500" />
          <h2 className="text-base font-black text-slate-800 uppercase tracking-widest">系统信息</h2>
        </div>
        <div className="p-6 flex items-center gap-6 text-sm">
          <div className="flex items-center gap-2">
            <span className="text-slate-400 font-medium">版本</span>
            <span className="font-bold text-slate-700">{versionInfo ? versionInfo.version : '—'}</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-slate-400 font-medium">版本类型</span>
            <span className="font-bold text-slate-700">{versionInfo ? versionInfo.edition.toUpperCase() : '—'}</span>
          </div>
          {versionInfo?.update ? (
            versionInfo.update.behind > 0 ? (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200 px-3 py-1 font-medium">
                <span className="w-2 h-2 rounded-full bg-amber-500 animate-pulse" />
                {`落后上游 ${versionInfo.update.behind}${versionInfo.update.behind_capped ? '+' : ''} 个提交`}
              </span>
            ) : (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 px-3 py-1 font-medium">
                <span className="w-2 h-2 rounded-full bg-emerald-500" />
                已是最新
              </span>
            )
          ) : null}
          <button
            type="button"
            onClick={refreshUpdateCheck}
            disabled={checkingUpdate}
            className="ml-auto inline-flex items-center gap-1.5 text-xs text-slate-500 hover:text-blue-600 disabled:opacity-50"
            title="重新检查上游更新"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${checkingUpdate ? 'animate-spin' : ''}`} />
            {checkingUpdate ? '检查中…' : '检查更新'}
          </button>
        </div>

        {/* 程序化交易报告义务：本卡片只**提供**要填的信息，不代为报告、不做合规判定。
            放在「系统信息」下面是因为它本质上就是「我这个部署是什么版本」的延伸。 */}
        {disclosure && (
          <div className="px-6 pb-6 pt-4 border-t border-gray-100">
            <div className="flex items-baseline gap-2 flex-wrap">
              <h3 className="text-xs font-black text-slate-600 tracking-widest">程序化交易报告信息</h3>
              <span className="text-[11px] text-slate-400">
                从事程序化交易前须通过券商向交易所报告，下表信息可直接复制填报
              </span>
            </div>
            <div className="mt-3 flex items-start gap-2">
              <pre className="flex-1 min-w-0 overflow-x-auto rounded-lg bg-slate-50 border border-slate-100 px-3 py-2 text-[11px] leading-5 text-slate-700 font-mono whitespace-pre-wrap break-all">
                {disclosure.text}
              </pre>
              <button
                type="button"
                onClick={() => {
                  void navigator.clipboard?.writeText(disclosure.text).then(
                    () => message.success('已复制'),
                    () => message.error('复制失败，请手动选择文本'),
                  );
                }}
                className="shrink-0 inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1 text-[11px] text-slate-600 hover:bg-slate-50"
              >
                复制
              </button>
            </div>
            {disclosure.high_frequency && (
              <p className="mt-2 text-[10px] leading-4 text-slate-400">
                高频阈值（仅提示，本工具不拦单也不代为报告）：每秒申报+撤单 ≥
                {disclosure.high_frequency.orders_per_second} 笔（即每分钟 ≥
                {disclosure.high_frequency.orders_per_minute} 笔）、或单日 ≥
                {disclosure.high_frequency.orders_per_day} 笔。{disclosure.high_frequency.note}
              </p>
            )}
            <p className="mt-2 text-[10px] leading-4 text-slate-400">
              本工具不代为报告，也不判断你是否构成高频交易；口径与阈值以券商和交易所最新要求为准。
            </p>
          </div>
        )}
      </div>

      <ComplianceFooter />

      {/* 通知设置 */}
      <div className="bg-white rounded-2xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="p-6 border-b border-gray-100 bg-slate-50/50 flex items-center gap-3">
          <Bell className="w-5 h-5 text-blue-500" />
          <h2 className="text-base font-black text-slate-800 uppercase tracking-widest">通知设置</h2>
        </div>
        <div className="p-8">
          <Form layout="vertical" className="space-y-6">
            <div className="flex items-center justify-between p-4 bg-slate-50/50 rounded-xl border border-slate-100">
              <div className="flex items-center gap-4">
                <div className="w-10 h-10 rounded-xl bg-white border border-slate-100 flex items-center justify-center text-blue-500">
                  <Mail className="w-5 h-5" />
                </div>
                <div>
                  <div className="text-sm font-bold text-slate-700">邮件通知</div>
                  <p className="text-xs text-slate-400 font-medium mt-0.5">接收策略执行和系统重要事件的邮件提醒</p>
                </div>
              </div>
              <Switch
                checked={notificationSettings?.email_notifications}
                onChange={(checked) => handleNotificationChange('email_notifications', checked)}
                loading={notificationUpdateStatus === 'loading'}
              />
            </div>

            <div className="flex items-center justify-between p-4 bg-slate-50/50 rounded-xl border border-slate-100">
              <div className="flex items-center gap-4">
                <div className="w-10 h-10 rounded-xl bg-white border border-slate-100 flex items-center justify-center text-indigo-500">
                  <Zap className="w-5 h-5" />
                </div>
                <div>
                  <div className="text-sm font-bold text-slate-700">推送通知</div>
                  <p className="text-xs text-slate-400 font-medium mt-0.5">在桌面或移动端接收实时操作推送</p>
                </div>
              </div>
              <Switch
                checked={notificationSettings?.push_notifications}
                onChange={(checked) => handleNotificationChange('push_notifications', checked)}
                loading={notificationUpdateStatus === 'loading'}
              />
            </div>

            <div className="flex items-center justify-between p-4 bg-slate-50/50 rounded-xl border border-slate-100">
              <div className="flex items-center gap-4">
                <div className="w-10 h-10 rounded-xl bg-white border border-slate-100 flex items-center justify-center text-emerald-500">
                  <Smartphone className="w-5 h-5" />
                </div>
                <div>
                  <div className="text-sm font-bold text-slate-700">策略状态监控</div>
                  <p className="text-xs text-slate-400 font-medium mt-0.5">当您的量化策略状态发生变化时立即通知</p>
                </div>
              </div>
              <Switch
                checked={notificationSettings?.strategy_alerts}
                onChange={(checked) => handleNotificationChange('strategy_alerts', checked)}
                loading={notificationUpdateStatus === 'loading'}
              />
            </div>
          </Form>
        </div>
      </div>

      {/* 隐私设置 */}
      <div className="bg-white rounded-2xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="p-6 border-b border-gray-100 bg-slate-50/50 flex items-center gap-3">
          <ShieldCheck className="w-5 h-5 text-emerald-500" />
          <h2 className="text-base font-black text-slate-800 uppercase tracking-widest">隐私与安全控制</h2>
        </div>
        <div className="p-8">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div className="flex items-center justify-between p-4 bg-slate-50/50 rounded-xl border border-slate-100">
              <div className="flex items-center gap-3">
                <Globe className="w-4 h-4 text-slate-400" />
                <span className="text-sm font-bold text-slate-700">公开个人位置</span>
              </div>
              <Switch
                checked={privacySettings?.show_location}
                onChange={(checked) => handlePrivacyChange('show_location', checked)}
                loading={privacyUpdateStatus === 'loading'}
              />
            </div>

            <div className="flex items-center justify-between p-4 bg-slate-50/50 rounded-xl border border-slate-100">
              <div className="flex items-center gap-3">
                <BarChart3 className="w-4 h-4 text-slate-400" />
                <span className="text-sm font-bold text-slate-700">公开交易统计</span>
              </div>
              <Switch
                checked={privacySettings?.show_trading_stats}
                onChange={(checked) => handlePrivacyChange('show_trading_stats', checked)}
                loading={privacyUpdateStatus === 'loading'}
              />
            </div>

            <div className="flex items-center justify-between p-4 bg-slate-50/50 rounded-xl border border-slate-100">
              <div className="flex items-center gap-3">
                <MessageCircle className="w-4 h-4 text-slate-400" />
                <span className="text-sm font-bold text-slate-700">允许站内私信</span>
              </div>
              <Switch
                checked={privacySettings?.allow_messages}
                onChange={(checked) => handlePrivacyChange('allow_messages', checked)}
                loading={privacyUpdateStatus === 'loading'}
              />
            </div>

            <div className="flex items-center justify-between p-4 bg-slate-50/50 rounded-xl border border-slate-100">
              <div className="flex items-center gap-3">
                <Users className="w-4 h-4 text-slate-400" />
                <span className="text-sm font-bold text-slate-700">显示社交动态</span>
              </div>
              <Switch
                checked={true}
                onChange={() => { }}
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default SettingsPage;
