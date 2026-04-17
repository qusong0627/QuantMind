import React, { useState, useEffect } from 'react';
import { message, Input, Button, Spin } from 'antd';
import { Key, Save, Eye, EyeOff, CheckCircle, AlertCircle } from 'lucide-react';
import { userCenterService } from '../services/userCenterService';

interface OtherSettingsProps {
  userId: string;
  tenantId: string;
}

export const OtherSettings: React.FC<OtherSettingsProps> = ({ userId, tenantId }) => {
  const [apiKey, setApiKey] = useState('');
  const [maskedKey, setMaskedKey] = useState('');
  const [hasKey, setHasKey] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [showKey, setShowKey] = useState(false);

  // 加载 API Key 状态
  useEffect(() => {
    loadApiKeyStatus();
  }, [userId]);

  const loadApiKeyStatus = async () => {
    setIsLoading(true);
    try {
      const result = await userCenterService.getLLMConfig();
      setHasKey(result.has_key || false);
      setMaskedKey(result.masked_key || '');
    } catch (error: any) {
      console.error('Failed to load API key status:', error);
      message.error('加载 API 配置失败');
    } finally {
      setIsLoading(false);
    }
  };

  const handleSaveApiKey = async () => {
    const trimmedKey = apiKey.trim();
    if (!trimmedKey) {
      message.warning('请输入 API Key');
      return;
    }

    setIsSaving(true);
    try {
      await userCenterService.saveLLMConfig(trimmedKey);
      message.success('API Key 保存成功');
      setApiKey(''); // 清空输入
      // 重新加载状态
      await loadApiKeyStatus();
    } catch (error: any) {
      console.error('Failed to save API key:', error);
      message.error(error.message || '保存失败');
    } finally {
      setIsSaving(false);
    }
  };

  const handleClearApiKey = async () => {
    setIsSaving(true);
    try {
      await userCenterService.saveLLMConfig('');
      message.success('API Key 已清除');
      setHasKey(false);
      setMaskedKey('');
    } catch (error: any) {
      console.error('Failed to clear API key:', error);
      message.error(error.message || '清除失败');
    } finally {
      setIsSaving(false);
    }
  };

  if (isLoading) {
    return (
      <div className="w-full pt-1">
        <div className="w-full rounded-2xl border border-gray-200 bg-white p-8 flex items-center justify-center min-h-[280px]">
          <Spin tip="加载中..." />
        </div>
      </div>
    );
  }

  return (
    <div className="w-full pt-1 space-y-6">
      {/* AI API Key 配置 */}
      <div className="w-full rounded-2xl border border-gray-200 bg-white overflow-hidden">
        {/* 标题 */}
        <div className="px-6 py-4 border-b border-gray-100 bg-gradient-to-r from-indigo-50 to-purple-50">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-indigo-100 rounded-lg">
              <Key className="w-5 h-5 text-indigo-600" />
            </div>
            <div>
              <h3 className="text-base font-bold text-gray-800">AI 服务配置</h3>
              <p className="text-xs text-gray-500 mt-0.5">配置 Qwen API Key，用于 AI-IDE 智能助手和策略生成</p>
            </div>
          </div>
        </div>

        {/* 内容 */}
        <div className="p-6 space-y-4">
          {/* 当前状态 */}
          <div className="flex items-center gap-3 p-4 rounded-xl bg-gray-50">
            {hasKey ? (
              <>
                <CheckCircle className="w-5 h-5 text-green-500" />
                <div className="flex-1">
                  <span className="text-sm text-gray-700">当前状态：</span>
                  <span className="text-sm font-medium text-green-600">已配置</span>
                  {maskedKey && (
                    <span className="ml-2 text-xs text-gray-500 font-mono bg-white px-2 py-0.5 rounded">
                      {maskedKey}
                    </span>
                  )}
                </div>
                <Button
                  size="small"
                  danger
                  onClick={handleClearApiKey}
                  loading={isSaving}
                >
                  清除
                </Button>
              </>
            ) : (
              <>
                <AlertCircle className="w-5 h-5 text-amber-500" />
                <div className="flex-1">
                  <span className="text-sm text-gray-700">当前状态：</span>
                  <span className="text-sm font-medium text-amber-600">未配置</span>
                </div>
              </>
            )}
          </div>

          {/* 输入新 Key */}
          <div className="space-y-2">
            <label className="text-sm font-medium text-gray-700">
              {hasKey ? '更新 API Key' : '输入 API Key'}
            </label>
            <div className="flex gap-3">
              <div className="flex-1 relative">
                <Input
                  type={showKey ? 'text' : 'password'}
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="sk-xxxxxxxxxxxxxxxx"
                  style={{ borderRadius: '8px', paddingRight: '40px' }}
                />
                <button
                  type="button"
                  onClick={() => setShowKey(!showKey)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 z-10"
                >
                  {showKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
              <Button
                type="primary"
                icon={<Save className="w-4 h-4" />}
                onClick={handleSaveApiKey}
                loading={isSaving}
                disabled={!apiKey.trim()}
                style={{ borderRadius: '8px' }}
              >
                保存
              </Button>
            </div>
          </div>

          {/* 说明 */}
          <div className="text-xs text-gray-500 space-y-1 pt-2 border-t border-gray-100">
            <p>• API Key 将安全存储在您的个人档案中</p>
            <p>• 用于 AI-IDE 智能助手对话和智能策略生成</p>
            <p>• 获取 API Key：<a href="https://bailian.console.aliyun.com/" target="_blank" rel="noopener noreferrer" className="text-indigo-600 hover:underline">阿里云百炼控制台</a></p>
          </div>
        </div>
      </div>
    </div>
  );
};

export default OtherSettings;
