/**
 * 策略选择器组件
 * 支持上传本地文件、从个人中心加载、使用模板
 */

import React, { useState, useEffect, useRef } from 'react';
import {
  Upload,
  FolderOpen,
  FileCode,
  Check,
  AlertTriangle,
  X,
  Loader2,
  BookOpen,
  Save,
  Trash2,
  RefreshCw,
  ChevronRight,
} from 'lucide-react';
import { strategyManagementService } from '../../services/strategyManagementService';
import { StrategyFile, StrategyValidationResult } from '../../types/backtest/strategy';
import { QlibStrategyParams } from '../../types/backtest/qlib';
import { QLIB_STRATEGY_TEMPLATES, StrategyTemplate } from '../../data/qlibStrategyTemplates';
import { StrategyTemplateModal } from './StrategyTemplateModal';

interface StrategyPickerProps {
  onStrategySelected: (
    code: string,
    strategyInfo?: StrategyFile,
    strategyParams?: QlibStrategyParams
  ) => void;
  onValidationResult?: (result: StrategyValidationResult) => void;
  hideUpload?: boolean;
  initialStrategy?: StrategyFile | null;
}

export const StrategyPicker: React.FC<StrategyPickerProps> = ({
  onStrategySelected,
  onValidationResult,
  hideUpload = false,
  initialStrategy,
}) => {
  const resolveActiveTab = (strategy: StrategyFile | null | undefined): 'upload' | 'personal' | 'template' => {
    if (!strategy) {
      return hideUpload ? 'template' : 'template';
    }
    if (strategy.source === 'template') {
      return 'template';
    }
    if (strategy.source === 'upload') {
      return hideUpload ? 'template' : 'upload';
    }
    return 'personal';
  };

  const [activeTab, setActiveTab] = useState<'upload' | 'personal' | 'template'>(
    resolveActiveTab(initialStrategy)
  );
  const [uploadedFile, setUploadedFile] = useState<File | null>(null);
  const [fileContent, setFileContent] = useState<string>('');
  const [personalStrategies, setPersonalStrategies] = useState<StrategyFile[]>([]);
  const [selectedStrategy, setSelectedStrategy] = useState<StrategyFile | null>(initialStrategy || null);
  const [isValidating, setIsValidating] = useState(false);
  const [validationResult, setValidationResult] = useState<StrategyValidationResult | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [saveDialogOpen, setSaveDialogOpen] = useState(false);
  const [strategyName, setStrategyName] = useState('');
  const [strategyDescription, setStrategyDescription] = useState('');
  const [selectedTemplate, setSelectedTemplate] = useState<StrategyTemplate | null>(null);
  const [isTemplateModalOpen, setIsTemplateModalOpen] = useState(false);

  // 当外部传入初始策略时，更新内部状态
  useEffect(() => {
    if (initialStrategy) {
      setSelectedStrategy(initialStrategy);
      setActiveTab(resolveActiveTab(initialStrategy));
    }
  }, [initialStrategy, hideUpload]);

  useEffect(() => {
    if (activeTab !== 'template' || selectedTemplate) {
      return;
    }
    const preferred =
      QLIB_STRATEGY_TEMPLATES.find((t) => t.id === 'standard_topk') ||
      QLIB_STRATEGY_TEMPLATES[0] ||
      null;
    if (preferred) {
      setSelectedTemplate(preferred);
    }
  }, [activeTab, selectedTemplate]);

  const parseTopkParamsFromCode = (code: string): QlibStrategyParams => {
    const defaults: QlibStrategyParams = {};
    const topkMatch =
      code.match(/TOPK\s*=\s*(\d+)/) || code.match(/['"]topk['"]\s*:\s*(\d+)/);
    if (topkMatch) {
      // 必须满足后端 Pydantic ge=5 约束
      defaults.topk = Math.max(5, Number(topkMatch[1]));
    }
    const nDropMatch =
      code.match(/N_DROP\s*=\s*(\d+)/) || code.match(/['"]n_drop['"]\s*:\s*(\d+)/);
    if (nDropMatch) {
      defaults.n_drop = Number(nDropMatch[1]);
    }
    return defaults;
  };

  const getTemplateParams = (template: StrategyTemplate): QlibStrategyParams => {
    const defaults: QlibStrategyParams = {};
    if (template.id === 'long_short_topk') {
      defaults.enable_short_selling = true;
    }
    template.params.forEach((param) => {
      const value = param.default;
      if (typeof value === 'number') {
        (defaults as Record<string, unknown>)[param.name] = value;
        return;
      }
      if (typeof value === 'boolean') {
        (defaults as Record<string, unknown>)[param.name] = value;
        return;
      }
      if (typeof value === 'string') {
        const lowered = value.toLowerCase();
        if (lowered === 'true' || lowered === 'false') {
          (defaults as Record<string, unknown>)[param.name] = lowered === 'true';
          return;
        }
        const numeric = Number(value);
        if (Number.isFinite(numeric)) {
          (defaults as Record<string, unknown>)[param.name] = numeric;
        }
      }
    });
    const code = template.code || '';
    if (defaults.topk === undefined) {
      defaults.topk = parseTopkParamsFromCode(code).topk;
    }
    if (defaults.n_drop === undefined) {
      defaults.n_drop = parseTopkParamsFromCode(code).n_drop;
    }
    return defaults;
  };

  const fileInputRef = useRef<HTMLInputElement>(null);

  // 处理模板选择
  const handleSelectTemplate = (template: StrategyTemplate) => {
    setSelectedTemplate(template);
    const templateParams = getTemplateParams(template);

    // [Native化改进] 不再传递代码字符串，而是传递模板ID
    // 这样后端会根据 ID 自动路由到原生的 Builder
    onStrategySelected('', {
      id: template.id, // 直接使用模板ID
      name: template.name,
      source: 'template',
      code: template.code,
      description: template.description,
      is_qlib_format: true,
      language: 'qlib',
    }, templateParams);

    // 模拟验证结果
    const validationResult: StrategyValidationResult = {
      is_valid: true,
      is_qlib_format: true,
      errors: [],
      warnings: [],
    };

    setValidationResult(validationResult);
    if (onValidationResult) {
      onValidationResult(validationResult);
    }
  };

  // 加载个人策略列表
  useEffect(() => {
    if (activeTab === 'personal') {
      loadPersonalStrategies();
    }
  }, [activeTab]);

  const loadPersonalStrategies = async () => {
    setIsLoading(true);
    try {
      const strategies = await strategyManagementService.loadStrategies();
      setPersonalStrategies(strategies);
    } catch (error: any) {
      console.error('加载策略失败:', error);
    } finally {
      setIsLoading(false);
    }
  };

  // 处理文件上传
  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    // 检查文件类型
    if (!file.name.endsWith('.py') && !file.name.endsWith('.txt')) {
      alert('请上传 .py 或 .txt 文件');
      return;
    }

    setUploadedFile(file);
    setIsValidating(true);
    setValidationResult(null);

    try {
      const content = await strategyManagementService.readLocalFile(file);
      setFileContent(content);

      // 验证策略
      const result = await strategyManagementService.validateStrategy(content);
      setValidationResult(result);

      if (onValidationResult) {
        onValidationResult(result);
      }

      // 如果验证通过，自动应用
      if (result.is_valid && result.is_qlib_format) {
        const codeParams = parseTopkParamsFromCode(content);
        onStrategySelected(content, {
          id: `upload_${Date.now()}`,
          name: file.name,
          source: 'upload',
          code: content,
          is_qlib_format: true,
          language: 'qlib',
        }, codeParams);
      }
    } catch (error: any) {
      console.error('文件读取失败:', error);
      setValidationResult({
        is_valid: false,
        is_qlib_format: false,
        errors: [{
          type: 'syntax',
          message: error.message || '文件读取失败',
          severity: 'error',
        }],
        warnings: [],
      });
    } finally {
      setIsValidating(false);
    }
  };

  // 处理策略选择
  const handleSelectStrategy = async (strategy: StrategyFile) => {
    setSelectedStrategy(strategy);
    setIsValidating(true);
    setValidationResult(null);

    try {
      // 列表接口为减载可能不返回 code，先按需拉取详情再验证
      let strategyToUse = strategy;
      if (!strategyToUse.code?.trim() && strategyToUse.id) {
        strategyToUse = await strategyManagementService.getStrategy(strategyToUse.id);
      }

      // 验证策略
      const result = await strategyManagementService.validateStrategy(strategyToUse.code);
      setValidationResult(result);

      if (onValidationResult) {
        onValidationResult(result);
      }

      // 如果验证通过，应用策略
      if (result.is_valid && result.is_qlib_format) {
        const codeParams = parseTopkParamsFromCode(strategyToUse.code);
        onStrategySelected(strategyToUse.code, strategyToUse, codeParams);
      }
    } catch (error: any) {
      console.error('策略验证失败:', error);
    } finally {
      setIsValidating(false);
    }
  };

  // 保存当前上传的策略
  const handleSaveStrategy = async () => {
    if (!fileContent || !strategyName.trim()) {
      alert('请填写策略名称');
      return;
    }

    try {
      await strategyManagementService.saveStrategy({
        name: strategyName,
        source: 'upload',
        code: fileContent,
        description: strategyDescription,
        is_qlib_format: validationResult?.is_qlib_format,
        language: validationResult?.is_qlib_format ? 'qlib' : 'python',
      });

      alert('策略保存成功');
      setSaveDialogOpen(false);
      setStrategyName('');
      setStrategyDescription('');

      // 刷新个人策略列表
      if (activeTab === 'personal') {
        loadPersonalStrategies();
      }
    } catch (error: any) {
      alert('保存失败: ' + error.message);
    }
  };

  // 删除策略
  const handleDeleteStrategy = async (strategyId: string) => {
    if (!confirm('确认删除此策略？')) return;

    try {
      await strategyManagementService.deleteStrategy(strategyId);
      loadPersonalStrategies();
    } catch (error: any) {
      alert('删除失败: ' + error.message);
    }
  };

  return (
    <div className="bg-white rounded-2xl border border-gray-200 shadow-sm">
      {/* 标题栏 */}
      <div className="px-4 py-3 border-b border-gray-200 flex items-center justify-between">
        <h3 className="font-medium text-gray-800">策略选择</h3>
        {uploadedFile && validationResult?.is_valid && (
          <button
            onClick={() => setSaveDialogOpen(true)}
            className="text-xs px-2 py-1 bg-blue-50 text-blue-600 rounded-2xl hover:bg-blue-100 flex items-center gap-1"
          >
            <Save className="w-3 h-3" />
            保存到个人中心
          </button>
        )}
      </div>

      {/* Tab切换 */}
      <div className="flex border-b border-gray-200">
        {!hideUpload && (
          <button
            onClick={() => setActiveTab('upload')}
            className={`flex-1 px-4 py-2.5 text-sm font-medium flex items-center justify-center gap-2 ${activeTab === 'upload'
              ? 'text-blue-600 border-b-2 border-blue-500 bg-blue-50/50'
              : 'text-gray-600 hover:text-gray-800 hover:bg-gray-50'
              }`}
          >
            <Upload className="w-4 h-4" />
            上传文件
          </button>
        )}
        <button
          onClick={() => setActiveTab('personal')}
          className={`flex-1 px-4 py-2.5 text-sm font-medium flex items-center justify-center gap-2 ${activeTab === 'personal'
            ? 'text-purple-600 border-b-2 border-purple-500 bg-purple-50/50'
            : 'text-gray-600 hover:text-gray-800 hover:bg-gray-50'
            }`}
        >
          <FolderOpen className="w-4 h-4" />
          个人中心
        </button>
        <button
          onClick={() => setActiveTab('template')}
          className={`flex-1 px-4 py-2.5 text-sm font-medium flex items-center justify-center gap-2 ${activeTab === 'template'
            ? 'text-green-600 border-b-2 border-green-500 bg-green-50/50'
            : 'text-gray-600 hover:text-gray-800 hover:bg-gray-50'
            }`}
        >
          <BookOpen className="w-4 h-4" />
          策略模板
        </button>
      </div>

      {/* 内容区域 */}
      <div className="p-4">
        {activeTab === 'upload' && (
          <div className="space-y-4">
            {/* 上传区域 */}
            <div
              onClick={() => fileInputRef.current?.click()}
              className="border-2 border-dashed border-gray-300 rounded-2xl p-8 text-center cursor-pointer hover:border-blue-400 hover:bg-blue-50/30 transition-colors"
            >
              <Upload className="w-12 h-12 mx-auto mb-3 text-gray-400" />
              <p className="text-sm text-gray-600 mb-1">
                点击上传或拖拽文件到此处
              </p>
              <p className="text-xs text-gray-500">
                支持 .py、.txt 格式，推荐符合规范的 Qlib 策略
              </p>
              <input
                ref={fileInputRef}
                type="file"
                accept=".py,.txt"
                onChange={handleFileUpload}
                className="hidden"
              />
            </div>

            {/* 文件信息 */}
            {uploadedFile && (
              <div className="bg-gray-50 rounded-2xl p-3 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <FileCode className="w-4 h-4 text-gray-600" />
                  <span className="text-sm text-gray-700">{uploadedFile.name}</span>
                  <span className="text-xs text-gray-500">
                    ({(uploadedFile.size / 1024).toFixed(1)} KB)
                  </span>
                </div>
                <button
                  onClick={() => {
                    setUploadedFile(null);
                    setFileContent('');
                    setValidationResult(null);
                  }}
                  className="text-gray-400 hover:text-gray-600"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            )}

            {/* 验证状态 */}
            {isValidating && (
              <div className="flex items-center gap-2 text-blue-600">
                <Loader2 className="w-4 h-4 animate-spin" />
                <span className="text-sm">正在验证策略...</span>
              </div>
            )}

            {/* 验证结果 */}
            {validationResult && (
              <ValidationResultDisplay result={validationResult} />
            )}
          </div>
        )}

        {activeTab === 'personal' && (
          <div className="space-y-3">
            {/* 刷新按钮 */}
            <div className="flex justify-end">
              <button
                onClick={loadPersonalStrategies}
                disabled={isLoading}
                className="text-xs px-2 py-1 text-gray-600 hover:text-gray-800 flex items-center gap-1"
              >
                <RefreshCw className={`w-3 h-3 ${isLoading ? 'animate-spin' : ''}`} />
                刷新
              </button>
            </div>

            {/* 策略列表 */}
            {isLoading ? (
              <div className="text-center py-8 text-gray-500">
                <Loader2 className="w-6 h-6 animate-spin mx-auto mb-2" />
                <p className="text-sm">加载中...</p>
              </div>
            ) : personalStrategies.length === 0 ? (
              <div className="text-center py-8 text-gray-500">
                <FolderOpen className="w-12 h-12 mx-auto mb-2 opacity-50" />
                <p className="text-sm">暂无保存的策略</p>
                <p className="text-xs mt-1">上传策略后可保存到个人中心</p>
              </div>
            ) : (
              <div className="space-y-2 max-h-96 overflow-y-auto">
                {personalStrategies.map((strategy) => (
                  <div
                    key={strategy.id}
                    className={`p-3 rounded-2xl border transition-colors cursor-pointer ${selectedStrategy?.id === strategy.id
                      ? 'border-purple-300 bg-purple-50'
                      : 'border-gray-200 hover:border-purple-200 hover:bg-gray-50'
                      }`}
                    onClick={() => handleSelectStrategy(strategy)}
                  >
                    <div className="flex items-start justify-between">
                      <div className="flex-1">
                        <div className="flex items-center gap-2 mb-1">
                          <FileCode className="w-4 h-4 text-gray-600" />
                          <span className="text-sm font-medium text-gray-800">
                            {strategy.name}
                          </span>
                          {strategy.is_qlib_format && (
                            <span className="text-xs px-1.5 py-0.5 bg-green-100 text-green-700 rounded-2xl">
                              Qlib
                            </span>
                          )}
                        </div>
                        {strategy.description && (
                          <p className="text-xs text-gray-600 mt-1">
                            {strategy.description}
                          </p>
                        )}
                        <p className="text-xs text-gray-500 mt-1">
                          {new Date(strategy.created_at!).toLocaleDateString()}
                        </p>
                      </div>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          handleDeleteStrategy(strategy.id);
                        }}
                        className="text-gray-400 hover:text-red-600 ml-2"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* 验证结果 */}
            {isValidating && (
              <div className="flex items-center gap-2 text-blue-600 mt-3">
                <Loader2 className="w-4 h-4 animate-spin" />
                <span className="text-sm">正在验证策略...</span>
              </div>
            )}
            {validationResult && (
              <ValidationResultDisplay result={validationResult} />
            )}
          </div>
        )}

        {activeTab === 'template' && (
          <div className="space-y-4">
            {/* 当前选中模板卡片 */}
            {selectedTemplate ? (
              <div className="rounded-2xl border-2 border-green-300 bg-green-50 p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1.5 flex-wrap">
                      <span className="font-semibold text-gray-800 text-sm">
                        {selectedTemplate.name}
                      </span>
                      <span
                        className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                          selectedTemplate.difficulty === 'beginner'
                            ? 'bg-green-100 text-green-700'
                            : selectedTemplate.difficulty === 'intermediate'
                            ? 'bg-yellow-100 text-yellow-700'
                            : 'bg-red-100 text-red-700'
                        }`}
                      >
                        {selectedTemplate.difficulty === 'beginner'
                          ? '入门'
                          : selectedTemplate.difficulty === 'intermediate'
                          ? '中级'
                          : '高级'}
                      </span>
                      <span
                        className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                          selectedTemplate.category === 'basic'
                            ? 'bg-blue-100 text-blue-700'
                            : selectedTemplate.category === 'advanced'
                            ? 'bg-purple-100 text-purple-700'
                            : 'bg-orange-100 text-orange-700'
                        }`}
                      >
                        {selectedTemplate.category === 'basic'
                          ? '基础'
                          : selectedTemplate.category === 'advanced'
                          ? '高级'
                          : '风控'}
                      </span>
                    </div>
                    <p className="text-xs text-gray-500 line-clamp-2">
                      {selectedTemplate.description}
                    </p>
                  </div>
                  <Check className="w-5 h-5 text-green-500 flex-shrink-0 mt-0.5" />
                </div>
              </div>
            ) : (
              <div className="rounded-2xl border-2 border-dashed border-gray-200 p-4 text-center text-gray-400">
                <BookOpen className="w-8 h-8 mx-auto mb-2 opacity-40" />
                <p className="text-sm">暂未选择策略模板</p>
              </div>
            )}

            {/* 切换模板按钮 */}
            <button
              onClick={() => setIsTemplateModalOpen(true)}
              className="w-full flex items-center justify-center gap-2 py-2.5 text-sm font-medium text-blue-600 border border-blue-200 bg-blue-50 hover:bg-blue-100 rounded-2xl transition-colors"
            >
              <BookOpen className="w-4 h-4" />
              切换策略模板
              <ChevronRight className="w-4 h-4" />
            </button>

            {/* 模板选择弹窗 */}
            <StrategyTemplateModal
              isOpen={isTemplateModalOpen}
              currentTemplateId={selectedTemplate?.id}
              onSelect={(template) => {
                handleSelectTemplate(template);
              }}
              onClose={() => setIsTemplateModalOpen(false)}
            />
          </div>
        )}
      </div>

      {/* 保存对话框 */}
      {saveDialogOpen && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4">
            <div className="px-4 py-3 border-b border-gray-200 flex items-center justify-between">
              <h3 className="font-medium text-gray-800">保存策略</h3>
              <button
                onClick={() => setSaveDialogOpen(false)}
                className="text-gray-400 hover:text-gray-600"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="p-4 space-y-4">
              <div>
                <label className="block text-sm text-gray-600 mb-1.5">
                  策略名称 <span className="text-red-500">*</span>
                </label>
                <input
                  type="text"
                  value={strategyName}
                  onChange={(e) => setStrategyName(e.target.value)}
                  placeholder="例如：动量策略_v1"
                  className="w-full px-3 py-2 border border-gray-200 rounded-2xl focus:outline-none focus:border-blue-500"
                />
              </div>
              <div>
                <label className="block text-sm text-gray-600 mb-1.5">
                  策略描述
                </label>
                <textarea
                  value={strategyDescription}
                  onChange={(e) => setStrategyDescription(e.target.value)}
                  placeholder="简单描述策略逻辑和参数..."
                  rows={3}
                  className="w-full px-3 py-2 border border-gray-200 rounded-2xl focus:outline-none focus:border-blue-500 resize-none"
                />
              </div>
            </div>
            <div className="px-4 py-3 border-t border-gray-200 flex justify-end gap-2">
              <button
                onClick={() => setSaveDialogOpen(false)}
                className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800"
              >
                取消
              </button>
              <button
                onClick={handleSaveStrategy}
                className="px-4 py-2 text-sm bg-blue-500 text-white rounded-2xl hover:bg-blue-600"
              >
                保存
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

/**
 * 验证结果展示组件
 */
const ValidationResultDisplay: React.FC<{ result: StrategyValidationResult }> = ({ result }) => {
  if (result.is_valid && result.is_qlib_format) {
    return (
      <div className="bg-green-50 border border-green-200 rounded-2xl p-3">
        <div className="flex items-start gap-2">
          <Check className="w-4 h-4 text-green-600 mt-0.5 flex-shrink-0" />
          <div className="flex-1">
            <div className="text-sm font-medium text-green-800 mb-1">
              策略验证通过
            </div>
            <div className="text-xs text-green-700">
              检测到符合规范的 Qlib 策略，可以直接使用
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* 错误信息 */}
      {result.errors.length > 0 && (
        <div className="bg-red-50 border border-red-200 rounded-2xl p-3">
          <div className="flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 text-red-600 mt-0.5 flex-shrink-0" />
            <div className="flex-1">
              <div className="text-sm font-medium text-red-800 mb-2">
                发现 {result.errors.length} 个错误
              </div>
              <div className="space-y-1.5">
                {result.errors.map((error, idx) => (
                  <div key={idx} className="text-xs text-red-700">
                    {error.line && <span className="font-mono">第{error.line}行: </span>}
                    {error.message}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 建议信息 */}
      {result.suggestions && result.suggestions.length > 0 && (
        <div className="bg-blue-50 border border-blue-200 rounded-2xl p-3">
          <div className="flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 text-blue-600 mt-0.5 flex-shrink-0" />
            <div className="flex-1">
              <div className="text-sm font-medium text-blue-800 mb-2">
                建议
              </div>
              <div className="space-y-1.5">
                {result.suggestions.map((suggestion, idx) => (
                  <div key={idx} className="text-xs text-blue-700">
                    • {suggestion}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 警告信息 */}
      {result.warnings.length > 0 && (
        <div className="bg-yellow-50 border border-yellow-200 rounded-2xl p-3">
          <div className="flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 text-yellow-600 mt-0.5 flex-shrink-0" />
            <div className="flex-1">
              <div className="text-sm font-medium text-yellow-800 mb-2">
                {result.warnings.length} 个警告
              </div>
              <div className="space-y-1.5">
                {result.warnings.map((warning, idx) => (
                  <div key={idx} className="text-xs text-yellow-700">
                    {warning.line && <span className="font-mono">第{warning.line}行: </span>}
                    {warning.message}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
