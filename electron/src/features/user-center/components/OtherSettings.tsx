import React, { useState, useEffect } from 'react';
import { message, Input, Button, Spin } from 'antd';
import { Key, Save, Eye, EyeOff, CheckCircle, AlertCircle, Trash2 } from 'lucide-react';
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
      setApiKey('');
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
        <div className="w-full rounded-xl border border-gray-200 bg-white p-8 flex items-center justify-center min-h-[200px]">
          <Spin tip="加载中..." />
        </div>
      </div>
    );
  }

  return (
    <div className="w-full pt-1 space-y-4">
      <div className="rounded-xl border border-gray-200 bg-white overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-100 bg-gradient-to-r from-indigo-50/60 to-purple-50/60">
          <div className="flex items-center gap-2.5">
            <div className="p-1.5 bg-indigo-100 rounded-md">
              <Key className="w-4 h-4 text-indigo-600" />
            </div>
            <div>
              <h3 className="text-sm font-semibold text-gray-800">AI 服务配置</h3>
              <p className="text-[11px] text-gray-500">Qwen API Key，用于 AI-IDE 和策略生成</p>
            </div>
          </div>
        </div>

        <div className="p-4 space-y-3">
          <div className={`flex items-center gap-2 px-3 py-2 rounded-xl text-xs ${hasKey ? 'bg-green-50 text-green-700 border border-green-100' : 'bg-amber-50 text-amber-700 border border-amber-100'}`}>
            {hasKey ? <CheckCircle className="w-3.5 h-3.5 shrink-0" /> : <AlertCircle className="w-3.5 h-3.5 shrink-0" />}
            <span className="font-medium">{hasKey ? '已配置' : '未配置'}</span>
            {hasKey && maskedKey && (
              <span className="font-mono text-gray-500 bg-white/60 px-1.5 py-0.5 rounded">{maskedKey}</span>
            )}
            {hasKey && (
              <Button
                type="text"
                size="small"
                danger
                className="ml-auto !text-[11px] !px-2 !h-6"
                icon={<Trash2 className="w-3 h-3" />}
                onClick={handleClearApiKey}
                loading={isSaving}
              >
                清除
              </Button>
            )}
          </div>

          <div className="flex gap-2 items-center">
            <div className="relative w-1/2">
              <Input
                type={showKey ? 'text' : 'password'}
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={hasKey ? '输入新 Key 以更新' : 'sk-xxxxxxxxxxxxxxxx'}
                className="!pr-9 rounded-xl h-9"
                onPressEnter={handleSaveApiKey}
              />
              <button
                type="button"
                onClick={() => setShowKey(!showKey)}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 z-10"
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
              className="rounded-xl h-9"
            >
              保存
            </Button>
          </div>

          <div className="text-[11px] text-gray-400 space-y-0.5 pt-1 border-t border-gray-100">
            <p>• API Key 安全存储在您的个人档案中</p>
            <p>• 获取 Key：<a href="https://bailian.console.aliyun.com/" target="_blank" rel="noopener noreferrer" className="text-indigo-600 hover:underline">阿里云百炼控制台</a></p>
          </div>
        </div>
      </div>
    </div>
  );
};

export default OtherSettings;
