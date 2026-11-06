import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '../components-v2/ui/Card';
import { Button } from '../components-v2/ui/Button';
import { Badge } from '../components-v2/ui/Badge';
import { Settings, Save, RotateCcw, Check, X, AlertCircle, Loader2, Database, Sliders, Compass, Shuffle, Info, Bot, Activity, ChevronDown, type LucideIcon } from 'lucide-react';
import { healthCheck, getDataSummary, getUniverses, getLlmConfig, type LlmConfigStatus } from '../services-v2/api';
import { getMiningAlgorithms } from '../services-v2/algorithmApi';
import { apiClient } from '../../../services/aiStrategyClients';
import { REFERENCE_MINING_DIRECTIONS, getDirectionLabel, type MiningDirectionItem, importFeatureCatalogDirections, fetchMiningDirections } from '../utils-v2/miningDirections';
import type { DataSummary, UniverseId, UniverseInfo } from '../types-v2';
import { Modal } from 'antd';
import { getSessionScopedStorageKey } from '../../../store/sessionScopedStorage';
import { useAppSelector } from '../../../store';

/**
 * 苹果式精简原则：
 * - 只留真正被消费的偏好（轮次 / 股票池 / 方向），开箱即用全部有默认；
 * - LLM 凭证、数据路径只读展示（后端环境变量说了算，前端不让填）；
 * - 小众能力（特征字典导入、自选方向清单）收进折叠区。
 * 已删除的死配置：apiKey / apiUrl / modelName / qlibDataPath / resultsDir
 * （只写 localStorage、没有任何消费方，留着只会误导）。
 */
interface SystemConfig {
  // Parameters
  defaultMaxRounds: number;
  defaultUniverse: UniverseId;
  // Mining direction: use selected directions / random / quota-weighted
  miningDirectionMode: 'selected' | 'random' | 'quota';
  /** 选中的挖掘方向（存 label，避免与动态 L1 类别列表下标错位） */
  selectedMiningDirections: string[];
}

const DEFAULT_CONFIG: SystemConfig = {
  defaultMaxRounds: 3,
  defaultUniverse: 'all_a',
  miningDirectionMode: 'selected',
  selectedMiningDirections: [],
};

const ROUND_PRESETS = [
  { value: 3, label: '快速', hint: '3 轮' },
  { value: 5, label: '标准', hint: '5 轮' },
  { value: 10, label: '深度', hint: '10 轮' },
];

type SettingsTab = 'status' | 'prefs' | 'directions';

export const SettingsPage: React.FC = () => {
  const authUser = useAppSelector((state) => state.auth.user);
  const isAuthenticated = useAppSelector((state) => state.auth.isAuthenticated);
  // OSS-ADAPT: 开源版 User 类型暂无 user_id（仅 id），此处兼容读取，保持与企业版一致的会话隔离语义。
  const authenticatedUserId = (authUser as unknown as { user_id?: string | number } | null)?.user_id ?? authUser?.id;
  const authenticatedOwner = isAuthenticated
    ? getSessionScopedStorageKey('autoalpha_config', authenticatedUserId) ?? ''
    : '';
  const [config, setConfig] = useState<SystemConfig>(DEFAULT_CONFIG);
  const [activeTab, setActiveTab] = useState<SettingsTab>('status');
  const [showCustomDirections, setShowCustomDirections] = useState(false);
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
  const [quotaSupported, setQuotaSupported] = useState(false);
  const configLoadGeneration = useRef(0);

  const loadConfig = useCallback(async () => {
    const generation = ++configLoadGeneration.current;
    setIsLoading(true);
    setError(null);

    try {
      await healthCheck();
      if (generation !== configLoadGeneration.current) return;
      setBackendStatus('online');
    } catch {
      if (generation !== configLoadGeneration.current) return;
      setBackendStatus('offline');
    }

    try {
      const storageKey = getSessionScopedStorageKey('autoalpha_config', authenticatedUserId);
      const saved = storageKey ? localStorage.getItem(storageKey) : null;
      if (generation !== configLoadGeneration.current) return;
      if (saved) {
        const parsed = JSON.parse(saved);
        setConfig({
          ...DEFAULT_CONFIG,
          ...parsed,
          selectedMiningDirections: Array.isArray(parsed.selectedMiningDirections)
            ? parsed.selectedMiningDirections.filter((l: unknown) => typeof l === 'string' && l)
            : DEFAULT_CONFIG.selectedMiningDirections,
        });
      } else {
        setConfig(DEFAULT_CONFIG);
      }
    } catch {
      if (generation !== configLoadGeneration.current) return;
      setConfig(DEFAULT_CONFIG);
    } finally {
      if (generation === configLoadGeneration.current) setIsLoading(false);
    }
  }, [authenticatedUserId]);

  // Load config from backend on mount
  useEffect(() => {
    getDataSummary()
      .then((res) => setDataSummary(res.data ?? null))
      .catch(() => {});
    getUniverses()
      .then((res) => setUniverses(res.data?.universes ?? []))
      .catch(() => {});
    fetchMiningDirections()
      .then(setL1Directions)
      .catch(() => {});
    getMiningAlgorithms()
      .then((res) => setQuotaSupported(
        (res.data?.algorithms ?? []).some((item) => item.key === 'rdagent' && item.supports_direction_quota === true),
      ))
      .catch(() => {});
    refreshLlmConfig();
  }, []);

  useEffect(() => {
    if (!isAuthenticated || !authenticatedOwner) {
      configLoadGeneration.current += 1;
      setConfig(DEFAULT_CONFIG);
      setIsDirty(false);
      setIsSaved(false);
      setIsLoading(false);
      return;
    }
    void loadConfig();
  }, [isAuthenticated, authenticatedUserId, authenticatedOwner, loadConfig]);

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

  const handleSave = async () => {
    setIsSaving(true);
    setError(null);

    // Save to localStorage (backend config is managed via .env and admin panel)
    const storageKey = getSessionScopedStorageKey('autoalpha_config', authenticatedUserId);
    if (!storageKey) {
      setError('无法确认当前账户，设置未保存。请重新登录后重试。');
      setIsSaving(false);
      return;
    }
    localStorage.setItem(storageKey, JSON.stringify(config));

    setIsSaved(true);
    setIsDirty(false);
    setIsSaving(false);
    setTimeout(() => setIsSaved(false), 2000);
  };

  const handleReset = () => {
    Modal.confirm({
      title: '恢复默认',
      content: '回到系统推荐的默认偏好？',
      okText: '恢复',
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
      setError('无法加载特征字典请检查后端连接');
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

  const updateConfigField = (key: keyof SystemConfig, value: unknown) => {
    setConfig({ ...config, [key]: value } as SystemConfig);
    setIsDirty(true);
  };

  /** L1 categories from QuantDB when available, else the static reference list */
  const activeDirections = l1Directions.length > 0 ? l1Directions : REFERENCE_MINING_DIRECTIONS;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[40vh]">
        <Loader2 className="h-5 w-5 animate-spin text-primary" />
        <span className="ml-3 text-xs text-muted-foreground">加载配置中...</span>
      </div>
    );
  }

  const TabButton = ({ id, label, icon: Icon }: { id: SettingsTab; label: string; icon: LucideIcon }) => (
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
    <div className="space-y-4 animate-fade-in-up">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold flex items-center gap-2">
            <Settings className="h-5 w-5 text-primary" />
            系统配置
          </h1>
          <p className="text-xs text-muted-foreground mt-0.5">
            开箱即用，不改也能挖；偏好保存在本机浏览器
          </p>
        </div>
        <div className="flex gap-3">
          <Button variant="outline" size="sm" className="text-xs" onClick={handleReset}>
            <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
            恢复默认
          </Button>
          <Button variant="primary" size="sm" className="text-xs" onClick={handleSave} disabled={!isDirty || isSaving}>
            {isSaving ? (
              <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
            ) : (
              <Save className="h-3.5 w-3.5 mr-1.5" />
            )}
            保存
          </Button>
        </div>
      </div>

      {/* Status Banners */}
      {isSaved && (
        <div className="glass rounded-lg p-4 flex items-center gap-3 bg-success/10 border-success/50 animate-fade-in-down">
          <Check className="h-5 w-5 text-success" />
          <span className="text-success">已保存</span>
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
          <span className="text-xs text-warning">{error}</span>
        </div>
      )}

      {/* Tabs Navigation */}
      <div className="flex gap-2 p-1 bg-secondary/20 rounded-xl w-fit flex-wrap">
        <TabButton id="status" label="运行状态" icon={Activity} />
        <TabButton id="prefs" label="挖掘偏好" icon={Sliders} />
        <TabButton id="directions" label="挖掘方向" icon={Compass} />
      </div>

      {/* Tab Content */}
      <div className="grid grid-cols-1 gap-6">

        {/* 运行状态：全部只读，出问题再找管理员 */}
        {activeTab === 'status' && (
          <Card className="glass card-hover animate-fade-in-up">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-sm">
                <Bot className="w-4 h-4" /> 模型与数据状态
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {/* LLM 状态 */}
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <div
                    className={`h-2.5 w-2.5 rounded-full ${
                      llmConfig?.configured ? 'bg-success' : 'bg-destructive'
                    }`}
                  />
                  <span className="text-xs">
                    大模型：{llmLoading ? '检测中…' : llmConfig?.configured ? `${llmConfig.model || '已就绪'}` : '未配置'}
                  </span>
                  {llmConfig?.configured && (
                    <Badge variant="outline" className="ml-1">系统统一配置</Badge>
                  )}
                </div>
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
              {!llmLoading && !llmConfig?.configured && (
                <p className="text-xs text-destructive">
                  {llmConfig?.reason || '未检测到可用模型，请联系管理员在服务器环境变量中配置。'}
                </p>
              )}

              {/* 后端连接 + 数据可用性 */}
              <div className="pt-3 border-t border-border/50 space-y-3">
                <div className="flex items-center gap-3">
                  <div
                    className={`h-2.5 w-2.5 rounded-full ${
                      backendStatus === 'online'
                        ? 'bg-success animate-pulse'
                        : backendStatus === 'offline'
                        ? 'bg-destructive'
                        : 'bg-warning animate-pulse'
                    }`}
                  />
                  <span className="text-xs">
                    后端：{backendStatus === 'online' ? '已连接' : backendStatus === 'offline' ? '未连接' : '检测中…'}
                    {dataSummary?.available && dataSummary.dateRange && (
                      <span className="text-muted-foreground">
                        {' · '}数据 {dataSummary.dateRange.start || '—'} ~ {dataSummary.dateRange.end || '—'}
                        {dataSummary.stockCount != null ? ` · ${dataSummary.stockCount} 只股票` : ''}
                      </span>
                    )}
                  </span>
                </div>
                {dataSummary && !dataSummary.available && (
                  <p className="text-xs text-destructive">
                    {dataSummary.error || '行情数据不可用，请联系管理员。'}
                  </p>
                )}
                <details className="text-xs text-muted-foreground">
                  <summary className="cursor-pointer hover:text-foreground">凭证与数据由服务端统一管理（展开）</summary>
                  <p className="mt-1 font-mono">DEEPSEEK_API_KEY 优先，其次 AI_IDE / OPENAI 系；数据目录以服务端挂载为准前端不保存路径。</p>
                </details>
              </div>
            </CardContent>
          </Card>
        )}

        {/* 挖掘偏好：两个真选项 */}
        {activeTab === 'prefs' && (
          <Card className="glass card-hover animate-fade-in-up">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-sm">
                <Sliders className="w-4 h-4" /> 挖掘偏好
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-5">
              <div>
                <label className="block text-xs font-medium mb-2">进化轮次</label>
                <div className="flex gap-2">
                  {ROUND_PRESETS.map((preset) => (
                    <button
                      key={preset.value}
                      type="button"
                      onClick={() => updateConfigField('defaultMaxRounds', preset.value)}
                      className={`flex-1 rounded-lg border px-3 py-2 text-center transition-all ${
                        config.defaultMaxRounds === preset.value
                          ? 'border-primary bg-primary/10 text-primary font-medium'
                          : 'border-input hover:bg-secondary/50'
                      }`}
                    >
                      <div className="text-xs">{preset.label}</div>
                      <div className="text-[10px] text-muted-foreground">{preset.hint}</div>
                    </button>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground mt-1">
                  轮次越多越慢但挖得越深；新人保持默认即可
                </p>
              </div>

              <div>
                <label className="block text-xs font-medium mb-2">默认股票池</label>
                <select
                  value={config.defaultUniverse}
                  onChange={(e) => updateConfigField('defaultUniverse', e.target.value)}
                  className="w-full rounded-lg border border-input bg-background px-4 py-2 text-xs focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-all"
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
              </div>

              <p className="pt-4 border-t border-border/50 text-xs text-muted-foreground">
                并发、时限等由平台统一管理；任务状态在任务中心查看。
              </p>
            </CardContent>
          </Card>
        )}

        {/* 挖掘方向：系统默认随机，其余收进折叠区 */}
        {activeTab === 'directions' && (
          <Card className="glass card-hover animate-fade-in-up">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-sm">
                <Compass className="h-5 w-5" />
                {l1Directions.length > 0 ? 'L1 因子类别' : '挖掘方向'}
              </CardTitle>
              <p className="text-xs text-muted-foreground mt-1">
                不选也能挖：启动任务时可单独指定方向
              </p>
            </CardHeader>
            <CardContent className="space-y-4">
              <div>
                <label className="block text-xs font-medium mb-3">使用方式</label>
                <div className="flex flex-wrap gap-4">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="radio"
                      name="miningDirectionMode"
                      checked={config.miningDirectionMode === 'selected'}
                      onChange={() => updateConfigField('miningDirectionMode', 'selected')}
                      className="h-4 w-4 text-primary focus:ring-primary"
                    />
                    <span className="text-xs">用我选的方向</span>
                  </label>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="radio"
                      name="miningDirectionMode"
                      checked={config.miningDirectionMode === 'random'}
                      onChange={() => updateConfigField('miningDirectionMode', 'random')}
                      className="h-4 w-4 text-primary focus:ring-primary"
                    />
                    <span className="flex items-center gap-1.5 text-xs">
                      <Shuffle className="h-4 w-4" />
                      随机（推荐新手）
                    </span>
                  </label>
                  {quotaSupported && (
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="radio"
                        name="miningDirectionMode"
                        checked={config.miningDirectionMode === 'quota'}
                        onChange={() => updateConfigField('miningDirectionMode', 'quota')}
                        className="h-4 w-4 text-primary focus:ring-primary"
                      />
                      <span className="text-xs">按成功率智能分配</span>
                    </label>
                  )}
                </div>
              </div>

              <div className="rounded-lg border border-border/50">
                <button
                  type="button"
                  onClick={() => setShowCustomDirections(!showCustomDirections)}
                  className="flex w-full items-center justify-between px-3 py-2.5 text-xs font-medium"
                >
                  <span>
                    按类别自选方向
                    <span className="ml-2 font-normal text-muted-foreground">
                      已选 {config.selectedMiningDirections.length} / {activeDirections.length} 项
                    </span>
                  </span>
                  <ChevronDown className={`h-4 w-4 text-muted-foreground transition-transform ${showCustomDirections ? 'rotate-180' : ''}`} />
                </button>
                {showCustomDirections && (
                  <div className="border-t border-border/50 p-3 space-y-3">
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
                        清空
                      </Button>
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
                            <span className="text-xs truncate flex-1" title={label}>
                              {label}
                            </span>
                          </label>
                        );
                      })}
                    </div>
                    <div className="pt-3 border-t border-border/50">
                      <div className="flex items-center justify-between mb-3">
                        <span className="text-xs font-medium">从模型训练特征字典导入</span>
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
                                <div className="text-xs font-medium">{item.label}</div>
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
                    </div>
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        )}
      </div>

      {/* Info Footer */}
      <Card className="glass border-primary/20 bg-primary/5">
        <CardContent className="p-4 flex gap-3">
          <Info className="w-5 h-5 text-primary shrink-0 mt-0.5" />
          <div className="text-xs text-muted-foreground">
            <p className="mb-1 font-medium text-foreground">配置提示</p>
            <p>偏好保存在本机浏览器。LLM 凭证、数据路径由服务器统一管理，不在前端保存。</p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
};
