/**
 * AI策略状态管理切片
 * 整合了原Recoil状态管理功能
 */

import { createSlice, createAsyncThunk, PayloadAction } from '@reduxjs/toolkit';
import {
  StrategyParams,
  Strategy,
  StrategyTemplate,
  MarketType,
  Timeframe,
  RiskLevel,
  StrategyStyle,
  StrategyLength,
  BacktestPeriod,
  ValidationResult,
  ChatMessage
} from '../../types/strategy';
// Local ValidationError type (strategy types file did not export this symbol)
export type ValidationError = {
  parameter: string;
  issue: string;
  severity: 'error' | 'warning' | 'info';
};
import { TemplateMatch } from '../../types/template';

// 从原Recoil atoms.ts导入的类型定义
export type DashboardTab = 'dashboard' | 'strategy' | 'backtest' | 'trading' | 'profile' | 'agent' | 'ai-ide' | 'admin' | 'model-training' | 'model-registry' | 'research';

// 创建兼容的StrategyParams接口，保持原有字段但使用正确的类型
export interface AIStrategyParams extends Omit<StrategyParams, 'style'> {
  style: StrategyStyle;
  strategyLength: StrategyLength;
  backtestPeriod: BacktestPeriod;
  // 扩展字段以支持更多功能
  stockPoolConfig?: any;
  framework?: 'standard' | 'miniqmt';
  outputFormat?: 'python' | 'miniqmt';
}

// 使用兼容的ChatMessage接口，保持与原有代码的兼容性
export interface AIStrategyChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  metadata?: Record<string, any>;
}

// 使用兼容的Strategy接口，保持与原有代码的兼容性
export interface AIStrategy {
  id: string;
  name: string;
  description: string;
  code: string;
  language?: string;
  framework?: string;
  parameters: AIStrategyParams;
  createdAt?: string;
  updatedAt?: string;
  status: 'draft' | 'active' | 'archived';
  metadata?: any;
  conversation?: AIStrategyChatMessage[];
  template?: StrategyTemplate;
  validation?: any;
  analysis?: any;
  performance?: {
    totalReturn: number;
    sharpeRatio: number;
    maxDrawdown: number;
    winRate: number;
    profitFactor: number;
  };
  // 添加缺少的属性
  strategy_name?: string;
  rationale?: string;
  artifacts?: any[];
}

export interface BacktestResult {
  id: string;
  strategyId: string;
  performance: {
    totalReturn: number;
    annualizedReturn: number;
    maxDrawdown: number;
    sharpeRatio: number;
    winRate: number;
    profitFactor: number;
  };
  trades: any[];
  equity: any[];
  charts: {
    equity: string;
    drawdown: string;
    returns: string;
  };
}


// 使用兼容的TemplateMatch接口，保持与原有代码的兼容性
type UIStrategyTemplate = StrategyTemplate;
export interface AIStrategyTemplateMatch {
  template: UIStrategyTemplate;
  matchScore: number;
  compatibilityScore: number;
  suggestions: string[];
  requiredModifications: string[];
}

// 验证相关类型
// 使用本地定义的 ValidationError 类型

export interface UIValidationResult extends ValidationResult {
  parameter_validation?: any;
  code_validation?: CodeValidationResult;
  template_validation?: any;
  processing_time: number;
  next_steps: string[];
}

export interface CodeValidationResult {
  isValid: boolean;
  quality_score?: number;
  syntax_errors: ValidationError[];
  logic_errors: ValidationError[];
  complexity: 'low' | 'medium' | 'high';
  metrics: {
    lines_of_code: number;
    cyclomatic_complexity?: number;
    maintainability_index?: number;
  };
  suggestions: string[];
}

export interface BatchValidationResult {
  isValid: boolean;
  results: UIValidationResult[];
  summary: {
    total: number;
    passed: number;
    failed: number;
    warnings: number;
  };
}

// 性能监控相关类型
export interface ProviderPerformance {
  provider_name: string;
  model_name: string;
  total_requests: number;
  successful_requests: number;
  failed_requests: number;
  avg_response_time: number;
  min_response_time: number;
  max_response_time: number;
  success_rate: number;
  error_rate: number;
  last_request_time: string;
  status: 'healthy' | 'degraded' | 'down';
}

export interface SystemPerformance {
  total_requests: number;
  successful_requests: number;
  failed_requests: number;
  avg_response_time: number;
  active_providers: number;
  memory_usage?: number;
  cpu_usage?: number;
  uptime: number;
  last_updated: string;
}

export interface PerformanceAlert {
  id: string;
  type: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  message: string;
  provider_name: string;
  metric_name: string;
  current_value: number;
  threshold: number;
  created_at: string;
  resolved_at?: string;
  status: 'active' | 'resolved';
}

// 文件管理相关类型
export interface FileInfo {
  file_id: string;
  filename: string;
  content_type: string;
  size: number;
  file_path: string;
  upload_time: string;
  user_id: string;
  category: string;
  content_hash?: string;
  description?: string;
  tags?: string[];
}

// API状态类型
export interface ApiStatus {
  isHealthy: boolean;
  lastChecked: string;
  responseTime: number;
  error?: string;
}

// 扩展的策略生成状态
export interface StrategyGenerationState {
  // 基础状态
  strategies: AIStrategy[];
  currentStrategy: AIStrategy | null;
  isGenerating: boolean;
  isSaving: boolean;
  error: string | null;
  generationHistory: AIStrategyParams[];

  // UI状态
  currentTab: DashboardTab;
  activeSection: 'chat' | 'code' | 'backtest';

  // 策略参数
  strategyParams: AIStrategyParams;

  // 对话相关
  chatMessages: AIStrategyChatMessage[];

  // 回测结果
  backtestResult: BacktestResult | null;

  // 策略历史
  strategyHistory: AIStrategy[];

  // 模板相关
  availableTemplates: StrategyTemplate[];
  selectedTemplate: StrategyTemplate | null;
  templateMatches: AIStrategyTemplateMatch[];

  // 验证相关
  parameterValidation: any | null;
  codeValidation: CodeValidationResult | null;
  batchValidation: BatchValidationResult | null;
  realtimeValidation: boolean;

  // 性能监控
  systemPerformance: SystemPerformance | null;
  providerPerformance: ProviderPerformance[];
  performanceAlerts: PerformanceAlert[];

  // 文件管理
  userFiles: FileInfo[];
  selectedFile: FileInfo | null;
  fileUploadProgress: number;
  isFileUploading: boolean;

  // API状态
  apiStatus: ApiStatus;
  apiLoading: boolean;
  apiError: string | null;
}

// 初始状态 - 整合所有Recoil状态的默认值
const initialState: StrategyGenerationState = {
  // 基础状态
  strategies: [],
  currentStrategy: null,
  isGenerating: false,
  isSaving: false,
  error: null,
  generationHistory: [],

  // UI状态
  currentTab: 'dashboard',
  activeSection: 'chat',

  // 策略参数 - 使用原Recoil默认值，转换为AIStrategyParams类型
  strategyParams: {
    description: '',
    market: 'CN' as MarketType,
    riskLevel: 'medium' as RiskLevel,
    style: 'custom' as const,
    symbols: [],
    framework: 'miniqmt',
    outputFormat: 'miniqmt',
    timeframe: '1d' as Timeframe,
    strategyLength: 'unlimited' as const,
    backtestPeriod: '1year' as const,
    initialCapital: 100000,
    positionSize: 10,
    maxPositions: 5,
    stopLoss: 5,
    takeProfit: 20,
    maxDrawdown: undefined,
    commissionRate: undefined,
    slippage: undefined,
    benchmark: undefined
  } as AIStrategyParams,

  // 对话相关
  chatMessages: [],

  // 回测结果
  backtestResult: null,

  // 策略历史
  strategyHistory: [],

  // 模板相关
  availableTemplates: [],
  selectedTemplate: null,
  templateMatches: [],

  // 验证相关
  parameterValidation: null,
  codeValidation: null,
  batchValidation: null,
  realtimeValidation: true,

  // 性能监控
  systemPerformance: null,
  providerPerformance: [],
  performanceAlerts: [],

  // 文件管理
  userFiles: [],
  selectedFile: null,
  fileUploadProgress: 0,
  isFileUploading: false,

  // API状态
  apiStatus: {
    isHealthy: false,
    lastChecked: '',
    responseTime: 0
  },
  apiLoading: false,
  apiError: null,
};

// 异步 actions
export const generateStrategy = createAsyncThunk(
  'aiStrategy/generateStrategy',
  async (params: AIStrategyParams, { rejectWithValue }) => {
    try {
      // 这里会调用 AI 策略服务
      const response = await fetch('/api/v1/strategy/generate', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(params),
      });

      if (!response.ok) {
        const error = await response.json();
        return rejectWithValue(error.message || '策略生成失败');
      }

      const data = await response.json();
      return data.data;
    } catch (error) {
      return rejectWithValue(error instanceof Error ? error.message : '网络错误');
    }
  }
);

export const saveStrategy = createAsyncThunk(
  'aiStrategy/saveStrategy',
  async (strategy: Partial<AIStrategy>, { rejectWithValue }) => {
    try {
      const response = await fetch('/api/v1/strategies', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(strategy),
      });

      if (!response.ok) {
        const error = await response.json();
        return rejectWithValue(error.message || '策略保存失败');
      }

      const data = await response.json();
      return data.data;
    } catch (error) {
      return rejectWithValue(error instanceof Error ? error.message : '网络错误');
    }
  }
);

export const fetchStrategies = createAsyncThunk(
  'aiStrategy/fetchStrategies',
  async (_, { rejectWithValue }) => {
    try {
      const response = await fetch('/api/v1/strategies');

      if (!response.ok) {
        const error = await response.json();
        return rejectWithValue(error.message || '获取策略列表失败');
      }

      const data = await response.json();
      return data.data;
    } catch (error) {
      return rejectWithValue(error instanceof Error ? error.message : '网络错误');
    }
  }
);

// 切片定义
const aiStrategySlice = createSlice({
  name: 'aiStrategy',
  initialState,
  reducers: {
    // 原有的基础actions
    setCurrentStrategy: (state, action: PayloadAction<AIStrategy | null>) => {
      state.currentStrategy = action.payload;
    },
    clearError: (state) => {
      state.error = null;
    },
    addToHistory: (state, action: PayloadAction<AIStrategyParams>) => {
      state.generationHistory.unshift(action.payload);
      // 限制历史记录数量
      if (state.generationHistory.length > 10) {
        state.generationHistory = state.generationHistory.slice(0, 10);
      }
    },
    updateStrategy: (state, action: PayloadAction<AIStrategy>) => {
      const index = state.strategies.findIndex(s => s.id === action.payload.id);
      if (index !== -1) {
        state.strategies[index] = action.payload;
      }
      if (state.currentStrategy?.id === action.payload.id) {
        state.currentStrategy = action.payload;
      }
    },
    deleteStrategy: (state, action: PayloadAction<string>) => {
      state.strategies = state.strategies.filter(s => s.id !== action.payload);
      if (state.currentStrategy?.id === action.payload) {
        state.currentStrategy = null;
      }
    },
    duplicateStrategy: (state, action: PayloadAction<string>) => {
      const strategy = state.strategies.find(s => s.id === action.payload);
      if (strategy) {
        const duplicated: AIStrategy = {
          ...strategy,
          id: `${strategy.id}_copy_${Date.now()}`,
          name: `${strategy.name} (副本)`,
          status: 'draft',
          createdAt: new Date().toISOString(),
        };
        state.strategies.push(duplicated);
      }
    },

    // UI状态管理 - 替代Recoil
    setCurrentTab: (state, action: PayloadAction<DashboardTab>) => {
      state.currentTab = action.payload;
    },
    setActiveSection: (state, action: PayloadAction<'chat' | 'code' | 'backtest'>) => {
      state.activeSection = action.payload;
    },

    // 策略参数管理 - 替代Recoil
    updateStrategyParams: (state, action: PayloadAction<Partial<AIStrategyParams>>) => {
      state.strategyParams = { ...state.strategyParams, ...action.payload };
    },
    resetStrategyParams: (state) => {
      state.strategyParams = initialState.strategyParams;
    },
    setStrategyParams: (state, action: PayloadAction<AIStrategyParams>) => {
      state.strategyParams = action.payload;
    },
    setCustomSymbols: (state, action: PayloadAction<string[]>) => {
      state.strategyParams.symbols = action.payload;
    },
    fetchStockPool: (state, action: PayloadAction<any>) => {
      // 股票池获取逻辑
    },
    setStockPoolType: (state, action: PayloadAction<any>) => {
      // 设置股票池类型逻辑
    },
    setIsGenerating: (state, action: PayloadAction<boolean>) => {
      state.isGenerating = action.payload;
    },

    // 对话管理 - 替代Recoil
    addChatMessage: (state, action: PayloadAction<AIStrategyChatMessage>) => {
      state.chatMessages.push(action.payload);
    },
    clearChatMessages: (state) => {
      state.chatMessages = [];
    },
    updateChatMessage: (state, action: PayloadAction<{ id: string; updates: Partial<AIStrategyChatMessage> }>) => {
      const { id, updates } = action.payload;
      const index = state.chatMessages.findIndex(msg => msg.id === id);
      if (index !== -1) {
        state.chatMessages[index] = { ...state.chatMessages[index], ...updates };
      }
    },

    // 回测结果管理 - 替代Recoil
    setBacktestResult: (state, action: PayloadAction<BacktestResult | null>) => {
      state.backtestResult = action.payload;
    },

    // 策略历史管理 - 替代Recoil
    addToStrategyHistory: (state, action: PayloadAction<AIStrategy>) => {
      state.strategyHistory.unshift(action.payload);
      // 限制历史记录数量
      if (state.strategyHistory.length > 50) {
        state.strategyHistory = state.strategyHistory.slice(0, 50);
      }
    },
    clearStrategyHistory: (state) => {
      state.strategyHistory = [];
    },

    // 模板管理 - 替代Recoil
    setAvailableTemplates: (state, action: PayloadAction<StrategyTemplate[]>) => {
      state.availableTemplates = action.payload;
    },
    setSelectedTemplate: (state, action: PayloadAction<StrategyTemplate | null>) => {
      state.selectedTemplate = action.payload;
    },
    setTemplateMatches: (state, action: PayloadAction<AIStrategyTemplateMatch[]>) => {
      state.templateMatches = action.payload;
    },

    // 验证管理 - 替代Recoil
    setParameterValidation: (state, action: PayloadAction<any | null>) => {
      state.parameterValidation = action.payload;
    },
    setCodeValidation: (state, action: PayloadAction<CodeValidationResult | null>) => {
      state.codeValidation = action.payload;
    },
    setBatchValidation: (state, action: PayloadAction<BatchValidationResult | null>) => {
      state.batchValidation = action.payload;
    },
    setRealtimeValidation: (state, action: PayloadAction<boolean>) => {
      state.realtimeValidation = action.payload;
    },
    clearValidationResults: (state) => {
      state.parameterValidation = null;
      state.codeValidation = null;
      state.batchValidation = null;
    },

    // 性能监控管理 - 替代Recoil
    setSystemPerformance: (state, action: PayloadAction<SystemPerformance | null>) => {
      state.systemPerformance = action.payload;
    },
    setProviderPerformance: (state, action: PayloadAction<ProviderPerformance[]>) => {
      state.providerPerformance = action.payload;
    },
    addPerformanceAlert: (state, action: PayloadAction<PerformanceAlert>) => {
      state.performanceAlerts.unshift(action.payload);
      // 限制告警数量
      if (state.performanceAlerts.length > 100) {
        state.performanceAlerts = state.performanceAlerts.slice(0, 100);
      }
    },
    removePerformanceAlert: (state, action: PayloadAction<string>) => {
      state.performanceAlerts = state.performanceAlerts.filter(alert => alert.id !== action.payload);
    },
    resolvePerformanceAlert: (state, action: PayloadAction<string>) => {
      const alert = state.performanceAlerts.find(a => a.id === action.payload);
      if (alert) {
        alert.status = 'resolved';
        alert.resolved_at = new Date().toISOString();
      }
    },

    // 文件管理 - 替代Recoil
    setUserFiles: (state, action: PayloadAction<FileInfo[]>) => {
      state.userFiles = action.payload;
    },
    addUserFile: (state, action: PayloadAction<FileInfo>) => {
      state.userFiles.unshift(action.payload);
    },
    removeUserFile: (state, action: PayloadAction<string>) => {
      state.userFiles = state.userFiles.filter(file => file.file_id !== action.payload);
    },
    setSelectedFile: (state, action: PayloadAction<FileInfo | null>) => {
      state.selectedFile = action.payload;
    },
    setFileUploadProgress: (state, action: PayloadAction<number>) => {
      state.fileUploadProgress = action.payload;
    },
    setIsFileUploading: (state, action: PayloadAction<boolean>) => {
      state.isFileUploading = action.payload;
    },

    // API状态管理 - 替代Recoil
    setApiStatus: (state, action: PayloadAction<ApiStatus>) => {
      state.apiStatus = action.payload;
    },
    setApiLoading: (state, action: PayloadAction<boolean>) => {
      state.apiLoading = action.payload;
    },
    setApiError: (state, action: PayloadAction<string | null>) => {
      state.apiError = action.payload;
    },
    clearApiError: (state) => {
      state.apiError = null;
    },
  },
  extraReducers: (builder) => {
    // 生成策略
    builder
      .addCase(generateStrategy.pending, (state) => {
        state.isGenerating = true;
        state.error = null;
      })
      .addCase(generateStrategy.fulfilled, (state, action) => {
        state.isGenerating = false;
        state.currentStrategy = action.payload;
        state.strategies.unshift(action.payload);
      })
      .addCase(generateStrategy.rejected, (state, action) => {
        state.isGenerating = false;
        state.error = action.payload as string;
      });

    // 保存策略
    builder
      .addCase(saveStrategy.pending, (state) => {
        state.isSaving = true;
        state.error = null;
      })
      .addCase(saveStrategy.fulfilled, (state, action) => {
        state.isSaving = false;
        const index = state.strategies.findIndex(s => s.id === action.payload.id);
        if (index === -1) {
          state.strategies.unshift(action.payload);
        } else {
          state.strategies[index] = action.payload;
        }
        state.currentStrategy = action.payload;
      })
      .addCase(saveStrategy.rejected, (state, action) => {
        state.isSaving = false;
        state.error = action.payload as string;
      });

    // 获取策略列表
    builder
      .addCase(fetchStrategies.pending, (state) => {
        state.error = null;
      })
      .addCase(fetchStrategies.fulfilled, (state, action) => {
        state.strategies = action.payload;
      })
      .addCase(fetchStrategies.rejected, (state, action) => {
        state.error = action.payload as string;
      });
  },
});

// 导出 actions
export const {
  setCurrentStrategy,
  clearError,
  addToHistory,
  updateStrategy,
  deleteStrategy,
  duplicateStrategy,
  // UI状态actions
  setCurrentTab,
  setActiveSection,
  // 策略参数actions
  updateStrategyParams,
  resetStrategyParams,
  setStrategyParams,
  setCustomSymbols,
  setIsGenerating,
  // 股票池actions
  fetchStockPool,
  setStockPoolType,
  // 对话actions
  addChatMessage,
  clearChatMessages,
  updateChatMessage,
  // 回测结果actions
  setBacktestResult,
  // 策略历史actions
  addToStrategyHistory,
  clearStrategyHistory,
  // 模板actions
  setAvailableTemplates,
  setSelectedTemplate,
  setTemplateMatches,
  // 验证actions
  setParameterValidation,
  setCodeValidation,
  setBatchValidation,
  setRealtimeValidation,
  clearValidationResults,
  // 性能监控actions
  setSystemPerformance,
  setProviderPerformance,
  addPerformanceAlert,
  removePerformanceAlert,
  resolvePerformanceAlert,
  // 文件管理actions
  setUserFiles,
  addUserFile,
  removeUserFile,
  setSelectedFile,
  setFileUploadProgress,
  setIsFileUploading,
  // API状态actions
  setApiStatus,
  setApiLoading,
  setApiError,
  clearApiError,
} = aiStrategySlice.actions;

// 选择器 - 基础状态
export const selectStrategies = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.strategies;

export const selectCurrentStrategy = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.currentStrategy;

export const selectIsGenerating = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.isGenerating;

export const selectIsSaving = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.isSaving;

export const selectStrategyError = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.error;

export const selectGenerationHistory = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.generationHistory;

// 选择器 - UI状态
export const selectCurrentTab = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.currentTab;

export const selectActiveSection = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.activeSection;

// 选择器 - 策略参数
export const selectStrategyParams = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.strategyParams;

export const selectStockPoolConfig = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.strategyParams.stockPoolConfig;

export const selectStockPoolLoading = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.isGenerating;

// 选择器 - 对话
export const selectChatMessages = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.chatMessages;

// 选择器 - 回测结果
export const selectBacktestResult = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.backtestResult;

// 选择器 - 策略历史
export const selectStrategyHistory = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.strategyHistory;

// 选择器 - 模板
export const selectAvailableTemplates = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.availableTemplates;

export const selectSelectedTemplate = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.selectedTemplate;

export const selectTemplateMatches = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.templateMatches;

// 选择器 - 验证
export const selectParameterValidation = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.parameterValidation;

export const selectCodeValidation = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.codeValidation;

export const selectBatchValidation = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.batchValidation;

export const selectRealtimeValidation = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.realtimeValidation;

// 选择器 - 性能监控
export const selectSystemPerformance = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.systemPerformance;

export const selectProviderPerformance = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.providerPerformance;

export const selectPerformanceAlerts = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.performanceAlerts;

// 选择器 - 文件管理
export const selectUserFiles = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.userFiles;

export const selectSelectedFile = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.selectedFile;

export const selectFileUploadProgress = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.fileUploadProgress;

export const selectIsFileUploading = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.isFileUploading;

// 选择器 - API状态
export const selectApiStatus = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.apiStatus;

export const selectApiLoading = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.apiLoading;

export const selectApiError = (state: { aiStrategy: StrategyGenerationState }) =>
  state.aiStrategy.apiError;

// 导出 reducer
export default aiStrategySlice.reducer;
