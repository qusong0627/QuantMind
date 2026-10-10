import React, { useState, useEffect, useRef } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '../components-v2/ui/Card';
import { Button } from '../components-v2/ui/Button';
import { Badge } from '../components-v2/ui/Badge';
import { Settings, Save, RotateCcw, Check, X, AlertCircle, Loader2, Database, Sliders, Box, Cpu, Compass, Shuffle, Info, Bot, BarChart3, Layers, Eye, EyeOff } from 'lucide-react';
import { healthCheck, getDataSummary, getUniverses, getLlmConfig, saveEmbeddingConfig, type LlmConfigStatus } from '../services-v2/api';
import { extractApiError } from '../../../utils/apiError';
import { apiClient } from '../../../services/aiStrategyClients';
import { REFERENCE_MINING_DIRECTIONS, getDirectionLabel, type MiningDirectionItem, importFeatureCatalogDirections, fetchMiningDirections } from '../utils-v2/miningDirections';
import { buildEmbeddingSavePayload } from '../utils-v2/embeddingPayload';
import type { DataSummary, UniverseId, UniverseInfo } from '../types-v2';
import { PageHeader } from '../components-v2/layout/PageHeader';
import { SupplyFacePanel } from '../components-v2/SupplyFacePanel';
import { Modal } from 'antd';

interface SystemConfig {
  // LLM
  apiKey: string;
  apiUrl: string;
  modelName: string;
  // Qlib
  qlibDataPath: string;
  resultsDir: string;
  // Parameters
  defaultNumDirections: number;
  defaultMaxRounds: number;
  defaultUniverse: UniverseId;
  // Advanced
  parallelExecution: boolean;
  qualityGateEnabled: boolean;
  backtestTimeout: number;
  defaultLibrarySuffix: string;
  // Mining direction: use selected directions / random
  miningDirectionMode: 'selected' | 'random';
  /** 选中的挖掘方向（存 label，避免与动态 L1 类别列表下标错位） */
  selectedMiningDirections: string[];
}

const DEFAULT_CONFIG: SystemConfig = {
  apiKey: '',
  apiUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  modelName: 'deepseek-v3',
  qlibDataPath: '',
  resultsDir: '',
  defaultNumDirections: 2,
  defaultMaxRounds: 3,
  defaultUniverse: 'csi300',
  parallelExecution: true,
  qualityGateEnabled: true,
  backtestTimeout: 600,
  defaultLibrarySuffix: '',
  miningDirectionMode: 'selected',
  selectedMiningDirections: [],
};

type SettingsTab = 'api' | 'data' | 'params' | 'directions';

export const SettingsPage: React.FC = () => {
  const [config, setConfig] = useState<SystemConfig>(DEFAULT_CONFIG);
  const [activeTab, setActiveTab] = useState<SettingsTab>('api');
  const [isSaved, setIsSaved] = useState(false);
  const [isDirty, setIsDirty] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [backendStatus, setBackendStatus] = useState<'checking' | 'online' | 'offline'>('checking');
  const [error, setError] = useState<string | null>(null);
  const [catalogDirections, setCatalogDirections] = useState<MiningDirectionItem[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [dataSummary, setDataSummary] = useState<DataSummary | null>(null);
  const [universes, setUniverses] = useState<UniverseInfo[]>([]);
  const [l1Directions, setL1Directions] = useState<MiningDirectionItem[]>([]);
  const [llmConfig, setLlmConfig] = useState<LlmConfigStatus | null>(null);
  const [llmLoading, setLlmLoading] = useState(false);
  // 向量检索（embedding）—— 因子挖掘的记忆检索走这条通道，与 chat 独立。
  const [embeddingForm, setEmbeddingForm] = useState({ model: '', baseUrl: '', apiKey: '' });
  const [embeddingSaving, setEmbeddingSaving] = useState(false);
  const [embeddingMsg, setEmbeddingMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [embKeyVisible, setEmbKeyVisible] = useState(false);
  /** 表单只在首次拿到状态时灌初值，之后不再覆盖用户正在编辑的内容 */
  const embHydrated = useRef(false);

  // Load config from backend on mount
  useEffect(() => {
    loadConfig();
    getDataSummary()
      .then((res) => setDataSummary(res.data ?? null))
      .catch(() => {});
    getUniverses()
      .then((res) => setUniverses(res.data?.universes ?? []))
      .catch(() => {});
    fetchMiningDirections()
      .then(setL1Directions)
      .catch(() => {});
    refreshLlmConfig();
  }, []);

  useEffect(() => {
    const emb = llmConfig?.embedding;
    if (embHydrated.current || !emb) return;
    setEmbeddingForm({ model: emb.model, baseUrl: emb.base_url, apiKey: '' });
    embHydrated.current = true;
  }, [llmConfig]);

  const updateEmbeddingField = (patch: Partial<typeof embeddingForm>) => {
    setEmbeddingForm({ ...embeddingForm, ...patch });
    // 一改输入就撤掉「已保存」绿字：否则用户改完模型名看到绿字还在，会以为
    // 新值已经存好了，切走页面 → 下次挖掘仍用旧模型。
    setEmbeddingMsg(null);
  };

  /** 服务端已知的 embedding 状态；为 undefined 表示状态**没拉到**（后端连不上）。 */
  const embeddingStatus = llmConfig?.embedding;

  const handleSaveEmbedding = async () => {
    // 状态没拉到就不许写：表单这时是空表，用户以为自己「没配过」而只补一个 Key，
    // 保存会把新 Key 与服务端**已存的** model/base_url 拼成一组从未验证过的组合
    // （新 Key 打旧端点），要等到挖掘任务调用 embedding 才暴露。
    if (!embeddingStatus) {
      setEmbeddingMsg({ ok: false, text: '配置状态未加载成功，请先刷新状态再保存' });
      return;
    }

    setEmbeddingSaving(true);
    setEmbeddingMsg(null);
    try {
      // 三态语义（undefined = 不动 / '' = 清除 / 有值 = 设置）统一在
      // buildEmbeddingSavePayload 里实现并有单测覆盖，这里只负责调用。
      const payload = buildEmbeddingSavePayload(embeddingForm, embeddingStatus);

      if (!Object.keys(payload).length) {
        setEmbeddingMsg({ ok: true, text: '没有需要保存的改动' });
        return;
      }

      const res = await saveEmbeddingConfig(payload);
      if (res.data) {
        setEmbeddingForm({ model: res.data.model, baseUrl: res.data.base_url, apiKey: '' });
      }
      setEmbeddingMsg({ ok: true, text: '向量检索配置已保存' });
      refreshLlmConfig();
    } catch (e: unknown) {
      setEmbeddingMsg({ ok: false, text: extractApiError(e, '保存失败，请稍后重试') });
    } finally {
      setEmbeddingSaving(false);
    }
  };

  /** 清除 Key：唯一会发空串的入口（空串在后端语义 = 回退容器级 EMBEDDING_*）。 */
  const handleClearEmbeddingKey = async () => {
    setEmbeddingSaving(true);
    setEmbeddingMsg(null);
    try {
      await saveEmbeddingConfig({ apiKey: '' });
      // 只清 apiKey 一项。原先展开闭包里的 embeddingForm 会把「点击那一刻」的
      // model/baseUrl 一并写回，等于把用户在这段等待里改的东西回滚掉。
      updateEmbeddingField({ apiKey: '' });
      setEmbeddingMsg({ ok: true, text: '已清除 Key，将沿用容器级 EMBEDDING_* 配置' });
      refreshLlmConfig();
    } catch (e: unknown) {
      setEmbeddingMsg({ ok: false, text: extractApiError(e, '清除失败，请稍后重试') });
    } finally {
      setEmbeddingSaving(false);
    }
  };

  const refreshLlmConfig = async () => {
    setLlmLoading(true);
    try {
      const res = await getLlmConfig();
      setLlmConfig(res.data ?? null);
    } catch {
      setLlmConfig(null);
    } finally {
      setLlmLoading(false);
    }
  };

  const loadConfig = async () => {
    setIsLoading(true);
    setError(null);

    // Check backend health
    try {
      await healthCheck();
      setBackendStatus('online');
    } catch {
      setBackendStatus('offline');
    }

    // Load config from localStorage (backend config is managed via .env and admin panel)
    try {
      const saved = localStorage.getItem('quantaalpha_config');
      if (saved) {
        const parsed = JSON.parse(saved);
        setConfig({
          ...DEFAULT_CONFIG,
          ...parsed,
          selectedMiningDirections: Array.isArray(parsed.selectedMiningDirections)
            ? parsed.selectedMiningDirections.filter((l: unknown) => typeof l === 'string' && l)
            : DEFAULT_CONFIG.selectedMiningDirections,
        });
      }
    } catch {
      // use defaults
    } finally {
      setIsLoading(false);
    }
  };

  const handleSave = async () => {
    setIsSaving(true);
    setError(null);

    // Save to localStorage (backend config is managed via .env and admin panel)
    localStorage.setItem('quantaalpha_config', JSON.stringify(config));

    setIsSaved(true);
    setIsDirty(false);
    setIsSaving(false);
    setTimeout(() => setIsSaved(false), 2000);
  };

  const handleReset = () => {
    Modal.confirm({
      title: '重置配置',
      content: '确定要重置为默认配置吗？',
      okText: '重置',
      cancelText: '取消',
      onOk: () => {
        setConfig(DEFAULT_CONFIG);
        setIsDirty(true);
      },
    });
  };

  const handleImportFeatureCatalog = async () => {
    setCatalogLoading(true);
    try {
      // 用户态接口（非管理端），普通用户亦可读取特征字典
      const resp = await apiClient.get('/models/feature-catalog');
      const data = resp.data?.data || resp.data;
      const catalog = data?.data || data;
      const directions = importFeatureCatalogDirections(catalog);
      if (directions.length > 0) {
        setCatalogDirections(directions);
      } else {
        setError('特征字典为空或格式不正确');
      }
    } catch {
      setError('无法加载特征字典，请检查后端连接');
    } finally {
      setCatalogLoading(false);
    }
  };

  const addCatalogDirection = (item: MiningDirectionItem) => {
    const label = getDirectionLabel(item);
    if (!label) return;
    const existing = config.selectedMiningDirections;
    if (!existing.includes(label)) {
      updateConfigField('selectedMiningDirections', [...existing, label]);
    }
  };

  const updateConfigField = (key: keyof SystemConfig, value: any) => {
    setConfig({ ...config, [key]: value });
    setIsDirty(true);
  };

  /** L1 categories from QuantDB when available, else the static reference list */
  const activeDirections = l1Directions.length > 0 ? l1Directions : REFERENCE_MINING_DIRECTIONS;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[40vh]">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
        <span className="ml-3 text-muted-foreground">加载配置中...</span>
      </div>
    );
  }

  const TabButton = ({ id, label, icon: Icon }: { id: SettingsTab; label: string; icon: any }) => (
    <button
      onClick={() => setActiveTab(id)}
      className={`flex items-center gap-2 px-4 py-2 rounded-lg transition-all ${
        activeTab === id
          ? 'bg-primary text-primary-foreground shadow-lg scale-105'
          : 'text-muted-foreground hover:bg-secondary/50 hover:text-foreground'
      }`}
    >
      <Icon className="h-4 w-4" />
      <span className="font-medium">{label}</span>
    </button>
  );

  return (
    <div className="space-y-6 animate-fade-in-up">
      {/* Header */}
      <PageHeader
        icon={Settings}
        title="系统配置"
        subtitle="管理 API 连接、数据源及实验参数"
        actions={
          <>
            <Button variant="outline" size="sm" className="h-7 px-2 text-xs" onClick={handleReset}>
              <RotateCcw className="h-3.5 w-3.5 mr-1" />
              重置
            </Button>
            <Button
              variant="primary"
              size="sm"
              className="h-7 px-2 text-xs"
              onClick={handleSave}
              disabled={!isDirty || isSaving}
            >
              {isSaving ? (
                <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
              ) : (
                <Save className="h-3.5 w-3.5 mr-1" />
              )}
              保存配置
            </Button>
          </>
        }
      />

      {/* Status Banners */}
      {isSaved && (
        <div className="glass rounded-lg p-4 flex items-center gap-3 bg-success/10 border-success/50 animate-fade-in-down">
          <Check className="h-5 w-5 text-success" />
          <span className="text-success">配置已保存</span>
        </div>
      )}
      {isDirty && !isSaved && (
        <div className="glass rounded-lg p-4 flex items-center gap-3 bg-warning/10 border-warning/50 animate-fade-in-down">
          <X className="h-5 w-5 text-warning" />
          <span className="text-warning">有未保存的更改</span>
        </div>
      )}
      {error && (
        <div className="glass rounded-lg p-4 flex items-center gap-3 bg-warning/10 border-warning/50">
          <AlertCircle className="h-5 w-5 text-warning flex-shrink-0" />
          <span className="text-sm text-warning">{error}</span>
        </div>
      )}

      {/* Tabs Navigation */}
      <div className="flex gap-2 p-1 bg-secondary/20 rounded-xl w-fit flex-wrap">
        <TabButton id="api" label="配置 API" icon={Cpu} />
        <TabButton id="data" label="数据路径" icon={Database} />
        <TabButton id="params" label="默认参数" icon={Sliders} />
        <TabButton id="directions" label={l1Directions.length > 0 ? 'L1 因子类别' : '挖掘方向'} icon={Compass} />
      </div>

      {/* Tab Content */}
      <div className="grid grid-cols-1 gap-6">
        
        {/* API Configuration Tab */}
        {activeTab === 'api' && (
          <>
          <Card className="glass card-hover animate-fade-in-up">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Bot className="w-4 h-4" /> LLM 模型配置
                <Badge variant="default">核心</Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="rounded-lg border border-blue-500/30 bg-blue-500/5 p-4">
                <div className="flex items-start gap-3">
                  <AlertCircle className="h-5 w-5 text-blue-500 mt-0.5 shrink-0" />
                  <div className="space-y-1 text-sm">
                    <p className="font-medium text-foreground">用户配置优先，环境变量兜底</p>
                    <p className="text-muted-foreground">
                      对话模型的凭证在<span className="font-medium">个人中心「其他设置 → AI 服务配置」</span>填写，
                      AI-IDE、因子挖掘与因子解释共用这一份；未配置时回落到容器/服务器环境变量。
                    </p>
                    <p className="text-xs text-muted-foreground mt-2 font-mono">
                      支持的环境变量（兜底，按优先级）：
                      <br />· DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL
                      <br />· AI_IDE_LLM_API_KEY / AI_IDE_LLM_BASE_URL / AI_IDE_LLM_MODEL
                      <br />· OPENAI_API_KEY / OPENAI_BASE_URL / CHAT_MODEL
                    </p>
                  </div>
                </div>
              </div>

              {/* Backend-resolved LLM config (read-only) */}
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <h4 className="text-sm font-semibold">后端当前生效配置</h4>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={refreshLlmConfig}
                    disabled={llmLoading}
                    className="h-7 px-2 text-xs"
                  >
                    {llmLoading ? (
                      <Loader2 className="h-3 w-3 animate-spin mr-1" />
                    ) : (
                      <RotateCcw className="h-3 w-3 mr-1" />
                    )}
                    重新检测
                  </Button>
                </div>

                {llmLoading && !llmConfig ? (
                  <div className="flex items-center gap-2 text-sm text-muted-foreground py-4">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    正在检测后端 LLM 配置...
                  </div>
                ) : llmConfig?.configured ? (
                  <div className="space-y-3">
                    <div className="flex items-center gap-2">
                      <Check className="h-5 w-5 text-success" />
                      <span className="text-sm font-medium text-success">已配置可用</span>
                      <Badge variant="outline" className="ml-2">
                        {llmConfig.source === 'user_profile' ? '个人中心配置' : '服务器环境变量'}
                      </Badge>
                      <Badge variant="outline" className="ml-2">
                        {llmConfig.provider === 'anthropic' ? 'Anthropic 协议' : 'OpenAI 兼容'}
                      </Badge>
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      <div className="rounded-lg border border-border/60 bg-background/50 p-3">
                        <p className="text-xs text-muted-foreground mb-1">模型</p>
                        <p className="text-sm font-mono break-all">{llmConfig.model || '-'}</p>
                      </div>
                      <div className="rounded-lg border border-border/60 bg-background/50 p-3">
                        <p className="text-xs text-muted-foreground mb-1">API Key</p>
                        <p className="text-sm font-mono">{llmConfig.api_key_masked || '****'}</p>
                      </div>
                      <div className="rounded-lg border border-border/60 bg-background/50 p-3 sm:col-span-2">
                        <p className="text-xs text-muted-foreground mb-1">Base URL</p>
                        <p className="text-sm font-mono break-all">{llmConfig.base_url || '-'}</p>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-4">
                    <div className="flex items-start gap-3">
                      <X className="h-5 w-5 text-destructive mt-0.5 shrink-0" />
                      <div className="space-y-1 text-sm">
                        <p className="font-medium text-destructive">未检测到可用的 LLM 配置</p>
                        <p className="text-muted-foreground">
                          {llmConfig?.reason || '请在环境变量中配置 DEEPSEEK_API_KEY 等凭证后重启后端。'}
                        </p>
                        <p className="text-xs text-muted-foreground mt-2">
                          未配置时因子挖掘会空转（factor 表 0 条），因子解释会返回错误。
                        </p>
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {/* Connection Status */}
              <div className="pt-4 border-t border-border/50">
                <div className="flex items-center gap-3">
                  <div
                    className={`h-3 w-3 rounded-full ${
                      backendStatus === 'online'
                        ? 'bg-success animate-pulse'
                        : backendStatus === 'offline'
                        ? 'bg-destructive'
                        : 'bg-warning animate-pulse'
                    }`}
                  />
                  <span className="text-sm">
                    后端连接状态：
                    {backendStatus === 'online' ? <span className="text-success font-medium">已连接</span> :
                     backendStatus === 'offline' ? <span className="text-destructive font-medium">未连接</span> :
                     '检测中...'}
                  </span>
                </div>
              </div>
            </CardContent>
          </Card>

          {/* 向量检索（Embedding）—— 因子挖掘**独有**的配置，故从个人中心迁到这里。
              chat 配置仍留在个人中心：AI-IDE 与因子挖掘共用同一份，放这边会让
              AI-IDE 的用户找不到自己的设置。 */}
          <Card className="glass card-hover animate-fade-in-up">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Layers className="w-4 h-4" /> 向量检索（Embedding）
                <Badge variant="default">因子挖掘专用</Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-5">
              <p className="text-sm text-muted-foreground">
                因子挖掘的<span className="font-medium">记忆检索</span>使用这条通道（RD-Agent 从历史
                实验中召回相似尝试）。它与上面的对话模型相互独立——DeepSeek 这类 chat 供应商
                没有 <span className="font-mono text-xs">/embeddings</span> 端点，因此这里可以指向
                完全不同的供应商或本地服务。
              </p>

              {/* 状态没拉到时禁止保存（见 handleSaveEmbedding 的守卫）：空表 + 只补一个
                  Key，会和**服务端已存的** model/base_url 拼成一组没验证过的组合。 */}
              {!embeddingStatus && (
                <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                  <AlertCircle className="w-3.5 h-3.5 mt-[1px] shrink-0" />
                  <span className="flex-1">
                    未能读到服务端的向量检索配置，表单此时是空白——若直接保存，可能与已存配置拼成
                    错误组合（新 Key 打旧端点）。请先刷新状态。
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-6 px-2 text-xs"
                    onClick={refreshLlmConfig}
                    disabled={llmLoading}
                  >
                    {llmLoading ? '刷新中…' : '刷新状态'}
                  </Button>
                </div>
              )}

              {/* 快速填充 */}
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-muted-foreground">快速填充：</span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-7 px-2 text-xs"
                  disabled={embeddingSaving || !embeddingStatus}
                  onClick={() =>
                    updateEmbeddingField({
                      model: 'BAAI/bge-m3',
                      baseUrl: 'https://api.siliconflow.cn/v1',
                    })
                  }
                >
                  SiliconFlow · bge-m3
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-7 px-2 text-xs"
                  disabled={embeddingSaving || !embeddingStatus}
                  onClick={() =>
                    updateEmbeddingField({
                      model: 'bge-m3',
                      // 不能填 127.0.0.1：挖掘子进程跑在容器里，那是**容器自己**。
                      baseUrl: 'http://host.docker.internal:11434/v1',
                    })
                  }
                >
                  本地 ollama · bge-m3
                </Button>
              </div>

              <div>
                <label className="block text-sm font-medium mb-2">模型名称</label>
                <input
                  type="text"
                  value={embeddingForm.model}
                  disabled={embeddingSaving || !embeddingStatus}
                  onChange={(e) => updateEmbeddingField({ model: e.target.value })}
                  placeholder="BAAI/bge-m3"
                  className="w-full rounded-lg border border-input bg-background px-4 py-2 text-sm font-mono focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                />
              </div>

              <div>
                <label className="block text-sm font-medium mb-2">接口地址</label>
                <input
                  type="text"
                  value={embeddingForm.baseUrl}
                  disabled={embeddingSaving || !embeddingStatus}
                  onChange={(e) => updateEmbeddingField({ baseUrl: e.target.value })}
                  placeholder="https://api.siliconflow.cn/v1"
                  className="w-full rounded-lg border border-input bg-background px-4 py-2 text-sm font-mono focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                />
                <p className="text-xs text-muted-foreground mt-1">
                  OpenAI 兼容端点。留空则沿用容器级 EMBEDDING_BASE_URL；本地 ollama 用
                  http://host.docker.internal:11434/v1（且 ollama 需监听 0.0.0.0，
                  默认只监听 127.0.0.1 时容器够不到）
                </p>
              </div>

              <div>
                <label className="block text-sm font-medium mb-2">
                  API Key
                  {embeddingStatus?.has_key && (
                    <span className="ml-2 font-normal text-muted-foreground font-mono text-xs">
                      已保存 {embeddingStatus.key_masked || '****'}
                    </span>
                  )}
                </label>
                <div className="relative">
                  <input
                    type={embKeyVisible ? 'text' : 'password'}
                    value={embeddingForm.apiKey}
                    disabled={embeddingSaving || !embeddingStatus}
                    onChange={(e) => updateEmbeddingField({ apiKey: e.target.value })}
                    placeholder={
                      embeddingStatus?.has_key ? '输入新 Key 以更新（留空则不变）' : 'sk-xxxxxxxxxxxxxxxx'
                    }
                    className="w-full rounded-lg border border-input bg-background px-4 py-2 pr-10 text-sm font-mono focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                  {/* 明文回显：粘贴截断/多空格只能靠肉眼核对，掩码看不出来 */}
                  <button
                    type="button"
                    onClick={() => setEmbKeyVisible(!embKeyVisible)}
                    disabled={embeddingSaving || !embeddingStatus}
                    aria-label={embKeyVisible ? '隐藏 API Key' : '显示 API Key'}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground disabled:opacity-50"
                  >
                    {embKeyVisible ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
                </div>
              </div>

              <div className="flex items-center gap-2 flex-wrap">
                <Button
                  size="sm"
                  onClick={handleSaveEmbedding}
                  disabled={embeddingSaving || !embeddingStatus}
                >
                  {embeddingSaving ? (
                    <Loader2 className="h-4 w-4 animate-spin mr-1" />
                  ) : (
                    <Save className="h-4 w-4 mr-1" />
                  )}
                  保存向量检索配置
                </Button>
                {embeddingStatus?.has_key && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleClearEmbeddingKey}
                    disabled={embeddingSaving}
                  >
                    清除 Key
                  </Button>
                )}
                {embeddingMsg && (
                  <span
                    className={`text-xs ${embeddingMsg.ok ? 'text-success' : 'text-destructive'}`}
                    title={embeddingMsg.text}
                  >
                    {embeddingMsg.text}
                  </span>
                )}
              </div>

              <p className="text-xs text-muted-foreground">
                保存后对<span className="font-medium">新启动</span>的挖掘任务生效（配置经子进程环境变量注入）。
                「清除 Key」会回退到容器级 EMBEDDING_* 配置。
              </p>
            </CardContent>
          </Card>
          </>
        )}

        {/* Data Path Configuration Tab */}
        {activeTab === 'data' && (
          <Card className="glass card-hover animate-fade-in-up">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <BarChart3 className="w-4 h-4" /> 数据存储路径
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              {/* QuantDB data availability */}
              {dataSummary && (
                <div className={`rounded-lg p-4 border ${
                  dataSummary.available
                    ? 'bg-success/5 border-success/20'
                    : 'bg-destructive/5 border-destructive/20'
                }`}>
                  <h4 className="text-sm font-medium mb-3 flex items-center gap-2">
                    {dataSummary.available ? (
                      <Check className="h-4 w-4 text-success" />
                    ) : (
                      <AlertCircle className="h-4 w-4 text-destructive" />
                    )}
                    QuantDB 数据可用性
                  </h4>
                  {dataSummary.available ? (
                    <div className="grid grid-cols-2 md:grid-cols-3 gap-3 text-xs">
                      {dataSummary.dateRange && (
                        <>
                          <div>
                            <div className="text-muted-foreground">数据起始</div>
                            <div className="font-mono font-medium">{dataSummary.dateRange.start || '—'}</div>
                          </div>
                          <div>
                            <div className="text-muted-foreground">数据截止</div>
                            <div className="font-mono font-medium">{dataSummary.dateRange.end || '—'}</div>
                          </div>
                          <div>
                            <div className="text-muted-foreground">交易日数</div>
                            <div className="font-mono font-medium">{dataSummary.dateRange.tradingDays || 0}</div>
                          </div>
                        </>
                      )}
                      {dataSummary.stockCount != null && (
                        <div>
                          <div className="text-muted-foreground">股票总数</div>
                          <div className="font-mono font-medium">{dataSummary.stockCount}</div>
                        </div>
                      )}
                      {dataSummary.datasets &&
                        Object.entries(dataSummary.datasets).map(([name, info]) => (
                          <div key={name}>
                            <div className="text-muted-foreground">{name}</div>
                            <div className="font-mono font-medium">
                              {info.columns} 列
                              {info.categoryCount ? ` / ${info.categoryCount} 类` : ''}
                            </div>
                          </div>
                        ))}
                    </div>
                  ) : (
                    <p className="text-xs text-muted-foreground">
                      {dataSummary.error || 'QuantDB 数据不可用，请检查 QM_QUANTDB_DATA_DIR 配置。'}
                    </p>
                  )}
                </div>
              )}

              <div>
                <label className="block text-sm font-medium mb-2">
                  Qlib 数据目录 <span className="text-destructive">*</span>
                </label>
                <div className="flex items-center gap-2">
                  <Database className="h-4 w-4 text-muted-foreground" />
                  <input
                    type="text"
                    value={config.qlibDataPath}
                    onChange={(e) => updateConfigField('qlibDataPath', e.target.value)}
                    placeholder="/path/to/qlib/cn_data"
                    className="flex-1 rounded-lg border border-input bg-background px-4 py-2 text-sm font-mono focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                </div>
                <p className="text-xs text-muted-foreground mt-1 ml-6">
                  需包含 calendars/, features/, instruments/ 等 Qlib 标准数据子目录
                </p>
              </div>

              <div>
                <label className="block text-sm font-medium mb-2">
                  实验结果输出目录
                </label>
                <div className="flex items-center gap-2">
                  <Box className="h-4 w-4 text-muted-foreground" />
                  <input
                    type="text"
                    value={config.resultsDir}
                    onChange={(e) => updateConfigField('resultsDir', e.target.value)}
                    placeholder="/path/to/results"
                    className="flex-1 rounded-lg border border-input bg-background px-4 py-2 text-sm font-mono focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                </div>
                <p className="text-xs text-muted-foreground mt-1 ml-6">
                  用于存放挖掘出的因子、回测报告及日志文件
                </p>
              </div>
            </CardContent>
          </Card>
        )}

        {/* Default Parameters Tab */}
        {activeTab === 'params' && (
          <Card className="glass card-hover animate-fade-in-up">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Sliders className="w-4 h-4" /> 实验默认参数
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div>
                  <label className="block text-sm font-medium mb-2">并行方向数</label>
                  <input
                    type="number"
                    value={config.defaultNumDirections}
                    onChange={(e) => updateConfigField('defaultNumDirections', parseInt(e.target.value))}
                    min={1}
                    max={10}
                    className="w-full rounded-lg border border-input bg-background px-4 py-2 text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    单次实验同时探索的独立方向数量 (1-10) · 类别方向生效：顺序模式取前 N 条、随机模式优先抽没挖过的方向；自由文本方向恒为 1 条，超出并发上限的任务自动排队
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium mb-2">进化轮次</label>
                  <input
                    type="number"
                    value={config.defaultMaxRounds}
                    onChange={(e) => updateConfigField('defaultMaxRounds', parseInt(e.target.value))}
                    min={1}
                    max={20}
                    className="w-full rounded-lg border border-input bg-background px-4 py-2 text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    因子自我进化和优化的最大迭代次数 (1-20)
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium mb-2">默认股票池</label>
                  <select
                    value={config.defaultUniverse}
                    onChange={(e) => updateConfigField('defaultUniverse', e.target.value)}
                    className="w-full rounded-lg border border-input bg-background px-4 py-2 text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  >
                    {universes.length > 0 ? (
                      universes.map((u) => (
                        <option key={u.id} value={u.id}>
                          {u.name}
                          {u.indexSymbol ? ` (${u.indexSymbol})` : ''}
                          {u.stockCount > 0 ? ` — ${u.stockCount} 只` : ''}
                        </option>
                      ))
                    ) : (
                      <>
                        <option value="csi300">沪深300</option>
                        <option value="csi500">中证500</option>
                        <option value="csi1000">中证1000</option>
                        <option value="sse50">上证50</option>
                        <option value="gem">创业板指</option>
                        <option value="star">科创50</option>
                        <option value="csi800">中证800</option>
                        <option value="all_a">全部A股</option>
                      </>
                    )}
                  </select>
                  <p className="text-xs text-muted-foreground mt-1">
                    因子挖掘和回测的默认股票池，来自 QuantDB 指数成分数据
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium mb-2">回测超时 (秒)</label>
                  <input
                    type="number"
                    value={config.backtestTimeout}
                    onChange={(e) => updateConfigField('backtestTimeout', parseInt(e.target.value))}
                    min={60}
                    max={3600}
                    className="w-full rounded-lg border border-input bg-background px-4 py-2 text-sm focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    单次回测最大执行时间 (秒) · <span className="text-warning">后端暂未生效</span>
                  </p>
                </div>

                <div className="md:col-span-2">
                  <label className="block text-sm font-medium mb-2">默认因子库名称后缀</label>
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-muted-foreground font-mono">all_factors_library_</span>
                    <input
                      type="text"
                      value={config.defaultLibrarySuffix}
                      onChange={(e) => {
                        const val = e.target.value.replace(/[^a-zA-Z0-9_\-]/g, '');
                        updateConfigField('defaultLibrarySuffix', val);
                      }}
                      placeholder="例如 momentum_v1 (留空则无后缀)"
                      className="flex-1 rounded-lg border border-input bg-background px-4 py-2 text-sm font-mono focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
                    />
                    <span className="text-sm text-muted-foreground font-mono">.json</span>
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">
                    生成的因子将保存到此文件。支持字母、数字、下划线。· <span className="text-warning">后端暂未生效</span>
                  </p>
                </div>
              </div>

              <div className="pt-4 border-t border-border/50 space-y-4">
                <h4 className="text-sm font-medium">高级控制</h4>
                
                <label className="flex items-center gap-3 cursor-pointer group p-3 rounded-lg border border-border/50 hover:bg-secondary/20 transition-all">
                  <input
                    type="checkbox"
                    checked={config.parallelExecution}
                    onChange={(e) => updateConfigField('parallelExecution', e.target.checked)}
                    className="h-5 w-5 rounded border-input text-primary focus:ring-primary"
                  />
                  <div className="flex-1">
                    <div className="font-medium group-hover:text-primary transition-colors">
                      启用并行执行
                    </div>
                    <div className="text-xs text-muted-foreground">
                      多方向并行由「并行方向数」驱动（逐条派发、超限自动排队）；本开关暂未接入后端
                    </div>
                  </div>
                </label>

                <label className="flex items-center gap-3 cursor-pointer group p-3 rounded-lg border border-border/50 hover:bg-secondary/20 transition-all">
                  <input
                    type="checkbox"
                    checked={config.qualityGateEnabled}
                    onChange={(e) => updateConfigField('qualityGateEnabled', e.target.checked)}
                    className="h-5 w-5 rounded border-input text-primary focus:ring-primary"
                  />
                  <div className="flex-1">
                    <div className="font-medium group-hover:text-primary transition-colors">
                      启用质量门控
                    </div>
                    <div className="text-xs text-muted-foreground">
                      自动检测并过滤低质量因子，防止其进入下一轮迭代，保证最终结果质量 · <span className="text-warning">后端暂未生效</span>
                    </div>
                  </div>
                </label>
              </div>
            </CardContent>
          </Card>
        )}

        {/* Mining Direction Tab */}
        {activeTab === 'directions' && (
          <Card className="glass card-hover animate-fade-in-up">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Compass className="h-5 w-5" />
                {l1Directions.length > 0 ? 'L1 因子类别' : '挖掘方向（内置参考）'}
              </CardTitle>
              <p className="text-sm text-muted-foreground mt-1">
                {l1Directions.length > 0
                  ? `来自 QuantDB L1 因子集的 ${l1Directions.length} 个类别；启动任务时可从中选用或随机一条`
                  : '选择作为默认参考的挖掘方向；启动任务时可从中选用或随机一条'}
              </p>
            </CardHeader>
            <CardContent className="space-y-6">
              <div>
                <label className="block text-sm font-medium mb-3">使用方式</label>
                <div className="flex flex-wrap gap-4">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="radio"
                      name="miningDirectionMode"
                      checked={config.miningDirectionMode === 'selected'}
                      onChange={() => updateConfigField('miningDirectionMode', 'selected')}
                      className="h-4 w-4 text-primary focus:ring-primary"
                    />
                    <span>使用下方选中的方向（启动时从选中中取一条或按业务逻辑使用）</span>
                  </label>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="radio"
                      name="miningDirectionMode"
                      checked={config.miningDirectionMode === 'random'}
                      onChange={() => updateConfigField('miningDirectionMode', 'random')}
                      className="h-4 w-4 text-primary focus:ring-primary"
                    />
                    <span className="flex items-center gap-1.5">
                      <Shuffle className="h-4 w-4" />
                      随机（从选中方向中随机选一条）
                    </span>
                  </label>
                </div>
              </div>

              <div>
                <div className="flex items-center justify-between mb-3">
                  <label className="text-sm font-medium">参考方向（可多选）</label>
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        updateConfigField(
                          'selectedMiningDirections',
                          activeDirections.map((d: MiningDirectionItem) => getDirectionLabel(d))
                        );
                      }}
                    >
                      全选
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => updateConfigField('selectedMiningDirections', [])}
                    >
                      取消全选
                    </Button>
                  </div>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-[320px] overflow-y-auto rounded-lg border border-border/50 bg-secondary/10 p-3">
                  {activeDirections.map((item: MiningDirectionItem, idx: number) => {
                    const label = getDirectionLabel(item);
                    return (
                      <label
                        key={idx}
                        className="flex items-center gap-2 p-2 rounded-lg hover:bg-secondary/20 cursor-pointer"
                      >
                        <input
                          type="checkbox"
                          checked={config.selectedMiningDirections.includes(label)}
                          onChange={(e) => {
                            const next = e.target.checked
                              ? [...config.selectedMiningDirections, label]
                              : config.selectedMiningDirections.filter((l) => l !== label);
                            updateConfigField('selectedMiningDirections', next);
                          }}
                          className="h-4 w-4 rounded border-input text-primary focus:ring-primary"
                        />
                        <span className="text-sm truncate flex-1" title={label}>
                          {label}
                        </span>
                      </label>
                    );
                  })}
                </div>
                <p className="text-xs text-muted-foreground mt-2">
                  已选 {config.selectedMiningDirections.length} / {activeDirections.length} 项。
                </p>
              </div>

              {/* 池内供给面（T-MV-02）：选方向前先看哪类挖过、哪类还空着 */}
              <SupplyFacePanel />

              {/* Import from Feature Catalog */}
              <div className="pt-4 border-t border-border/50">
                <div className="flex items-center justify-between mb-3">
                  <label className="text-sm font-medium">从模型训练特征字典导入</label>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleImportFeatureCatalog}
                    disabled={catalogLoading}
                  >
                    {catalogLoading ? (
                      <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                    ) : (
                      <Database className="h-4 w-4 mr-1" />
                    )}
                    加载特征字典
                  </Button>
                </div>
                {catalogDirections.length > 0 && (
                  <div className="space-y-2 max-h-[300px] overflow-y-auto rounded-lg border border-border/50 bg-secondary/10 p-3">
                    {catalogDirections.map((item, idx) => (
                      <div
                        key={idx}
                        className="flex items-center justify-between p-2 rounded-lg hover:bg-secondary/20"
                      >
                        <div className="flex-1">
                          <div className="text-sm font-medium">{item.label}</div>
                          {item.factors && item.factors.length > 0 && (
                            <div className="text-xs text-muted-foreground mt-0.5">
                              示例: {item.factors.map(f => f.shortName).join(', ')}
                            </div>
                          )}
                        </div>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => addCatalogDirection(item)}
                        >
                          添加
                        </Button>
                      </div>
                    ))}
                  </div>
                )}
                <p className="text-xs text-muted-foreground mt-2">
                  从模型训练特征字典中按类别导入挖掘方向
                </p>
              </div>
            </CardContent>
          </Card>
        )}
      </div>

      {/* Info Footer */}
      <Card className="glass border-primary/20 bg-primary/5">
        <CardContent className="p-4 flex gap-3">
          <Info className="w-5 h-5 text-primary shrink-0 mt-0.5" />
          <div className="text-sm text-muted-foreground">
            <p className="mb-1 font-medium text-foreground">配置提示</p>
            <p>配置修改保存在本地浏览器（localStorage）。LLM 凭证、数据路径与后端运行参数由服务器环境变量 / 个人中心统一管理，不在前端保存。</p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
};
