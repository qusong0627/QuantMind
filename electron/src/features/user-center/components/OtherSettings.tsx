import React, { useState, useEffect } from 'react';
import { message, Input, Button, Spin, Select, Radio } from 'antd';
import { Key, Save, Eye, EyeOff, CheckCircle, AlertCircle, Trash2, Zap } from 'lucide-react';
import { userCenterService } from '../services/userCenterService';

interface OtherSettingsProps {
  userId: string;
  tenantId: string;
}

// 供应商及其地址选项
interface ProviderMeta {
  id: string;
  label: string;
  baseUrls: { id: string; label: string; url: string }[];
  defaultBaseUrlId: string;
  models: { label: string; value: string }[];
  defaultModel: string;
}

const PROVIDERS: ProviderMeta[] = [
  {
    id: 'deepseek',
    label: 'DeepSeek（深度求索）',
    baseUrls: [
      { id: 'openai', label: 'OpenAI 兼容', url: 'https://api.deepseek.com/v1' },
      { id: 'anthropic', label: 'Anthropic 兼容', url: 'https://api.deepseek.com/anthropic' },
    ],
    defaultBaseUrlId: 'openai',
    models: [
      { label: 'deepseek-flash', value: 'deepseek-flash' },
    ],
    defaultModel: 'deepseek-flash',
  },
  {
    id: 'qwen',
    label: '阿里云百炼（通义千问）',
    baseUrls: [
      { id: 'dashscope', label: 'DashScope 兼容', url: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
    ],
    defaultBaseUrlId: 'dashscope',
    models: [
      { label: 'qwen3.8-max', value: 'qwen3.8-max' },
      { label: 'qwen3.8-flash', value: 'qwen3.8-flash' },
      { label: 'qwen3.7-max', value: 'qwen3.7-max' },
      { label: 'qwen3.7-flash', value: 'qwen3.7-flash' },
      { label: 'qwen3.6-max-preview', value: 'qwen3.6-max-preview' },
      { label: 'qwen-max', value: 'qwen-max' },
      { label: 'qwen-plus', value: 'qwen-plus' },
      { label: 'qwen-turbo', value: 'qwen-turbo' },
      { label: 'qwen3-coder-plus', value: 'qwen3-coder-plus' },
      { label: 'qwen3-vl-plus', value: 'qwen3-vl-plus' },
    ],
    defaultModel: 'qwen3.8-max',
  },
  {
    id: 'custom',
    label: '自定义模型',
    baseUrls: [],
    defaultBaseUrlId: '__custom__',
    models: [],
    defaultModel: '',
  },
];

const getProvider = (id: string): ProviderMeta =>
  PROVIDERS.find((p) => p.id === id) || PROVIDERS[0];

export const OtherSettings: React.FC<OtherSettingsProps> = ({ userId, tenantId }) => {
  const [apiKey, setApiKey] = useState('');
  const [maskedKey, setMaskedKey] = useState('');
  const [hasKey, setHasKey] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [showKey, setShowKey] = useState(false);

  // 供应商选择
  const [providerId, setProviderId] = useState(PROVIDERS[0].id);

  // 地址区：供应商内置地址 id / 自定义
  const [baseUrlOption, setBaseUrlOption] = useState<string>(PROVIDERS[0].defaultBaseUrlId);
  const [customBaseUrl, setCustomBaseUrl] = useState('');

  // 模型区：下拉 + 自定义模型名
  const [selectedModel, setSelectedModel] = useState<string>(PROVIDERS[0].defaultModel);
  const [customModel, setCustomModel] = useState('');
  const [isCustomModel, setIsCustomModel] = useState(false);
  // 自定义请求头（JSON 文本），用于自建网关鉴权等（如 x-opencode-session）
  const [extraHeaders, setExtraHeaders] = useState('');

  useEffect(() => {
    loadApiKeyStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  const loadApiKeyStatus = async () => {
    setIsLoading(true);
    try {
      const result = await userCenterService.getLLMConfig();
      setHasKey(result.has_key || false);
      setMaskedKey(result.masked_key || '');
      setExtraHeaders(((result as any).extra_headers as string) || '');

      // 恢复供应商
      const savedProvider = (result.provider as string) || '';
      if (PROVIDERS.some((p) => p.id === savedProvider)) {
        setProviderId(savedProvider);
      }
      const active = savedProvider && PROVIDERS.some((p) => p.id === savedProvider) ? savedProvider : providerId;
      const meta = getProvider(active);

      // 恢复接口地址
      const savedBaseUrl = result.base_url || '';
      const matched = meta.baseUrls.find((b) => b.url === savedBaseUrl);
      if (matched) {
        setBaseUrlOption(matched.id);
        setCustomBaseUrl('');
      } else if (savedBaseUrl) {
        setBaseUrlOption('__custom__');
        setCustomBaseUrl(savedBaseUrl);
      } else {
        setBaseUrlOption(meta.defaultBaseUrlId);
      }

      // 恢复模型
      const savedModel = result.model || '';
      const isPreset = meta.models.some((m) => m.value === savedModel);
      if (savedModel && isPreset) {
        setSelectedModel(savedModel);
        setIsCustomModel(false);
      } else if (savedModel) {
        setSelectedModel('__custom__');
        setCustomModel(savedModel);
        setIsCustomModel(true);
      } else {
        setSelectedModel(meta.defaultModel);
        setIsCustomModel(false);
      }
    } catch (error: any) {
      console.error('Failed to load API key status:', error);
      message.error('加载 API 配置失败');
    } finally {
      setIsLoading(false);
    }
  };

  // 供应商切换
  const handleProviderChange = (id: string) => {
    setProviderId(id);
    const meta = getProvider(id);
    setBaseUrlOption(meta.defaultBaseUrlId);
    setCustomBaseUrl('');
    setSelectedModel(meta.defaultModel);
    setCustomModel('');
    setIsCustomModel(false);
  };

  const handleModelChange = (value: string) => {
    setSelectedModel(value);
    setIsCustomModel(value === '__custom__');
  };

  const meta = getProvider(providerId);

  const resolveBaseUrl = (): string => {
    if (baseUrlOption === '__custom__') return customBaseUrl.trim();
    const opt = meta.baseUrls.find((b) => b.id === baseUrlOption);
    return opt ? opt.url : '';
  };

  const handleSaveApiKey = async () => {
    const trimmedKey = apiKey.trim();
    if (!trimmedKey && !hasKey) {
      message.warning('请输入 API Key');
      return;
    }

    const model = isCustomModel ? customModel.trim() : selectedModel;
    if (!model) {
      message.warning('请输入或选择模型名称');
      return;
    }

    const baseUrl = resolveBaseUrl();
    if (!baseUrl) {
      message.warning('请输入接口地址');
      return;
    }

    setIsSaving(true);
    try {
      await userCenterService.saveLLMConfig(trimmedKey, model, baseUrl, providerId, extraHeaders);
      message.success(trimmedKey ? `${meta.label} 配置保存成功` : '模型与接口地址已更新');
      setApiKey('');
      await loadApiKeyStatus();
    } catch (error: any) {
      console.error('Failed to save config:', error);
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

  const handleTest = async () => {
    const trimmedKey = apiKey.trim();
    if (!trimmedKey) {
      message.warning('请先在下方输入新的 API Key 后测试');
      return;
    }

    const model = isCustomModel ? customModel.trim() : selectedModel;
    if (!model) {
      message.warning('请输入或选择模型名称');
      return;
    }

    const baseUrl = resolveBaseUrl();
    if (!baseUrl) {
      message.warning('请输入接口地址');
      return;
    }

    setIsTesting(true);
    try {
      const result = await userCenterService.testLLMConfig(trimmedKey, model, baseUrl, extraHeaders);
      if (result && result.success) {
        message.success(result.message || '连接成功');
      } else {
        message.error((result?.message as string) || '连接失败');
      }
    } catch (error: any) {
      console.error('Failed to test config:', error);
      message.error(error.message || '测试失败');
    } finally {
      setIsTesting(false);
    }
  };

  if (isLoading) {
    return (
      <div className="w-full pt-1">
        <div className="w-full rounded-xl border border-gray-200 bg-white p-8 flex items-center justify-center min-h-[200px]">
          <Spin />
        </div>
      </div>
    );
  }

  return (
    <div className="w-full pt-1 space-y-4">
      <div className="rounded-xl border border-gray-200 bg-white overflow-hidden">
        <div className="p-4 space-y-3">
          <div className="flex items-center gap-2.5">
            <div className="p-1.5 bg-indigo-100 rounded-md">
              <Key className="w-4 h-4 text-indigo-600" />
            </div>
            <div>
              <h3 className="text-sm font-semibold text-gray-800">AI 服务配置</h3>
              <p className="text-[11px] text-gray-500">配置模型供应商的 API Key、模型和接口地址，用于 AI-IDE 智能助手</p>
            </div>
          </div>

          {/* 供应商下拉 */}
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-gray-600">模型供应商</label>
            <Select
              value={providerId}
              onChange={handleProviderChange}
              style={{ width: '100%' }}
              className="[&_.ant-select-selector]:!h-8 [&_.ant-select-selector]:!rounded-[8px] [&_.ant-select-selector]:!items-center"
              options={PROVIDERS.map((p) => ({ label: p.label, value: p.id }))}
            />
          </div>

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

          {/* 两列：左=接口地址，右=模型 */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {/* 左列：接口地址 */}
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-gray-600">接口地址 Base URL</label>
              {meta.baseUrls.length > 0 ? (
                <>
                  <Radio.Group
                    value={baseUrlOption}
                    onChange={(e) => setBaseUrlOption(e.target.value)}
                    size="small"
                    className="flex flex-col !gap-1"
                  >
                    {meta.baseUrls.map((b) => (
                      <Radio key={b.id} value={b.id} className="!text-xs">{b.label}</Radio>
                    ))}
                    <Radio value="__custom__" className="!text-xs">自定义</Radio>
                  </Radio.Group>
                  {baseUrlOption === '__custom__' && (
                    <Input
                      value={customBaseUrl}
                      onChange={(e) => setCustomBaseUrl(e.target.value)}
                      placeholder="输入接口地址，如 https://api.openai.com/v1"
                      className="!h-8 !rounded-[8px]"
                    />
                  )}
                </>
              ) : (
                <Input
                  value={customBaseUrl}
                  onChange={(e) => setCustomBaseUrl(e.target.value)}
                  placeholder="输入接口地址，如 https://api.openai.com/v1"
                  className="!h-8 !rounded-[8px]"
                />
              )}
              <div className="text-[11px] text-gray-400 font-mono break-all">
                {resolveBaseUrl() || '未选择接口地址'}
              </div>
            </div>

            {/* 右列：模型 */}
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-gray-600">模型 Model</label>
              {meta.models.length > 0 ? (
                <>
                  <Select
                    value={selectedModel}
                    onChange={handleModelChange}
                    style={{ width: '100%' }}
                    className="[&_.ant-select-selector]:!h-8 [&_.ant-select-selector]:!rounded-[8px] [&_.ant-select-selector]:!items-center"
                    options={[
                      ...meta.models.map((m) => ({ label: m.label, value: m.value })),
                      { label: '自定义', value: '__custom__' },
                    ]}
                  />
                  {isCustomModel && (
                    <Input
                      value={customModel}
                      onChange={(e) => setCustomModel(e.target.value)}
                      placeholder="输入模型名称"
                      className="!h-8 !rounded-[8px]"
                    />
                  )}
                </>
              ) : (
                <Input
                  value={isCustomModel ? customModel : selectedModel}
                  onChange={(e) => {
                    setSelectedModel(e.target.value);
                    setCustomModel(e.target.value);
                    setIsCustomModel(true);
                  }}
                  placeholder="输入模型名称"
                  className="!h-8 !rounded-[8px]"
                />
              )}
            </div>
          </div>

          {/* 自定义请求头（可选） */}
          <div className="space-y-1.5 border-t border-gray-100 pt-3">
            <label className="text-xs font-medium text-gray-600">自定义请求头（可选）</label>
            <Input.TextArea
              value={extraHeaders}
              onChange={(e) => setExtraHeaders(e.target.value)}
              placeholder='JSON，例如 {"x-opencode-session": "xxxxxxxx"}'
              autoSize={{ minRows: 1, maxRows: 4 }}
              className="!rounded-[8px] !text-xs !font-mono"
            />
            <p className="text-[11px] text-gray-400">
              部分自建网关需额外鉴权头（如 x-opencode-session）；留空则不发送。
            </p>
          </div>

          {/* 下方：API Key */}
          <div className="space-y-1.5 border-t border-gray-100 pt-3">
            <label className="text-xs font-medium text-gray-600">{meta.label} API Key</label>
            <div className="flex gap-2 items-center">
              <div className="relative flex-1">
                <Input
                  type={showKey ? 'text' : 'password'}
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={hasKey ? '输入新 Key 以更新' : 'sk-xxxxxxxxxxxxxxxx'}
                  className="!pr-9 !h-8 !rounded-[8px]"
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
                disabled={!apiKey.trim() && !hasKey}
                className="!h-8 !rounded-[8px]"
              >
                保存
              </Button>
              <Button
                icon={<Zap className="w-4 h-4" />}
                onClick={handleTest}
                loading={isTesting}
                className="!h-8 !rounded-[8px]"
              >
                测试
              </Button>
            </div>
          </div>

          <div className="text-[11px] text-gray-400 space-y-0.5 pt-1 border-t border-gray-100">
            <p>• API Key 安全存储在您的个人档案中，由 AI-IDE 智能助手与因子挖掘共用</p>
            <p>• 支持多供应商：DeepSeek、阿里云百炼，可自定义接口地址与模型</p>
            <p>• 获取 Key：<a href="https://platform.deepseek.com/" target="_blank" rel="noopener noreferrer" className="text-indigo-600 hover:underline">DeepSeek</a> | <a href="https://bailian.console.aliyun.com/" target="_blank" rel="noopener noreferrer" className="text-indigo-600 hover:underline">阿里云百炼</a></p>
            <p>• 因子挖掘的向量检索（Embedding）另在「因子挖掘 → 设置 → 配置 API」中配置</p>
          </div>
        </div>
      </div>
    </div>
  );
};

export default OtherSettings;