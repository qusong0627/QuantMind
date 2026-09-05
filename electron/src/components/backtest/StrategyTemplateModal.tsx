/**
 * 策略模板选择弹窗
 * 打开时实时从后端加载模板列表，选中后自动关闭。
 */

import React, { useEffect, useState, useCallback } from 'react';
import {
  X,
  RefreshCw,
  Loader2,
  Check,
  Lightbulb,
  Award,
  Shield,
  BookOpen,
  AlertTriangle,
} from 'lucide-react';
import { StrategyTemplate } from '../../data/qlibStrategyTemplates';
import { strategyTemplateService } from '../../features/strategy-wizard/services/strategyTemplateService';
import { registerRuntimeTemplates } from '../../shared/qlib/strategyParams';
import { getMarketConfig } from '../../config/marketConfig';
import type { AppMarket } from '../../store/slices/uiSlice';

type TemplateCategory = 'all' | 'basic' | 'advanced' | 'risk_control';

interface StrategyTemplateModalProps {
  isOpen: boolean;
  /** 当前市场（CN/HK/US/CRYPTO）：模板列表按市场隔离，缺省返回全部（旧行为） */
  market?: AppMarket;
  currentTemplateId?: string;
  onSelect: (template: StrategyTemplate) => void;
  onClose: () => void;
}

export const StrategyTemplateModal: React.FC<StrategyTemplateModalProps> = ({
  isOpen,
  market,
  currentTemplateId,
  onSelect,
  onClose,
}) => {
  const [templates, setTemplates] = useState<StrategyTemplate[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [hasError, setHasError] = useState(false);
  const [category, setCategory] = useState<TemplateCategory>('all');

  const loadTemplates = useCallback(async (forceRefresh = false) => {
    setIsLoading(true);
    setHasError(false);
    try {
      // forceRefresh 时绕过 sessionStorage 缓存，直接拉后端最新数据
      const data = forceRefresh
        ? await strategyTemplateService.refresh(market)
        : await strategyTemplateService.getTemplates(market);

      // 前端兜底排序：按难度排序 (入门 -> 中级 -> 高级)，相同难度按 id 排序
      const difficultyWeight: Record<string, number> = {
        beginner: 1,
        intermediate: 2,
        advanced: 3,
      };

      const sortedData = [...data].sort((a, b) => {
        const weightA = difficultyWeight[a.difficulty] || 9;
        const weightB = difficultyWeight[b.difficulty] || 9;
        if (weightA !== weightB) return weightA - weightB;
        return a.id.localeCompare(b.id);
      });

      setTemplates(sortedData);
      registerRuntimeTemplates(sortedData);
    } catch {
      setHasError(true);
    } finally {
      setIsLoading(false);
    }
  }, [market]);

  // 每次弹窗打开时实时拉取后端数据；市场切换时同样重拉（按市场隔离模板）
  useEffect(() => {
    if (isOpen) {
      setCategory('all');
      loadTemplates(true);
    }
  }, [isOpen, market, loadTemplates]);

  // ESC 关闭
  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const filtered =
    category === 'all' ? templates : templates.filter((t) => t.category === category);

  const handleSelect = (template: StrategyTemplate) => {
    onSelect(template);
    onClose();
  };

  const difficultyLabel = (d: StrategyTemplate['difficulty']) =>
    d === 'beginner' ? '入门' : d === 'intermediate' ? '中级' : '高级';

  const difficultyColor = (d: StrategyTemplate['difficulty']) =>
    d === 'beginner'
      ? 'bg-green-100 text-green-700'
      : d === 'intermediate'
      ? 'bg-yellow-100 text-yellow-700'
      : 'bg-red-100 text-red-700';

  const categoryLabel = (c: StrategyTemplate['category']) =>
    c === 'basic' ? '基础' : c === 'advanced' ? '高级' : '风控';

  const categoryColor = (c: StrategyTemplate['category']) =>
    c === 'basic'
      ? 'bg-blue-100 text-blue-700'
      : c === 'advanced'
      ? 'bg-purple-100 text-purple-700'
      : 'bg-orange-100 text-orange-700';

  return (
    /* 遮罩层 */
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.45)' }}
      onClick={onClose}
    >
      {/* 弹窗主体 */}
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-3xl max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200 flex-shrink-0">
          <div className="flex items-center gap-2">
            <BookOpen className="w-5 h-5 text-blue-600" />
            <h2 className="text-base font-semibold text-gray-800">选择策略模板</h2>
            {market && (
              <span className="text-xs px-2 py-0.5 rounded-full bg-blue-50 text-blue-600 font-medium">
                {getMarketConfig(market).label}
              </span>
            )}
            {templates.length > 0 && (
              <span className="text-xs text-gray-400">共 {templates.length} 个模板</span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => loadTemplates(true)}
              disabled={isLoading}
              title="从后端刷新最新模板"
              className="p-1.5 text-gray-400 hover:text-blue-600 hover:bg-blue-50 rounded-xl transition-colors disabled:opacity-40"
            >
              <RefreshCw className={`w-4 h-4 ${isLoading ? 'animate-spin' : ''}`} />
            </button>
            <button
              onClick={onClose}
              className="p-1.5 text-gray-400 hover:text-gray-700 hover:bg-gray-100 rounded-xl transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* 分类筛选 */}
        <div className="flex gap-2 px-5 py-3 border-b border-gray-100 flex-shrink-0 flex-wrap">
          {(
            [
              { key: 'all', label: '全部', icon: null, active: 'bg-blue-500 text-white', inactive: 'bg-gray-100 text-gray-600' },
              { key: 'basic', label: '基础策略', icon: <Lightbulb className="w-3.5 h-3.5" />, active: 'bg-green-500 text-white', inactive: 'bg-gray-100 text-gray-600' },
              { key: 'advanced', label: '高级策略', icon: <Award className="w-3.5 h-3.5" />, active: 'bg-purple-500 text-white', inactive: 'bg-gray-100 text-gray-600' },
              { key: 'risk_control', label: '风控策略', icon: <Shield className="w-3.5 h-3.5" />, active: 'bg-orange-500 text-white', inactive: 'bg-gray-100 text-gray-600' },
            ] as const
          ).map(({ key, label, icon, active, inactive }) => (
            <button
              key={key}
              onClick={() => setCategory(key as TemplateCategory)}
              className={`px-3 py-1.5 text-xs rounded-full font-medium transition-colors flex items-center gap-1 ${
                category === key ? active : `${inactive} hover:bg-gray-200`
              }`}
            >
              {icon}
              {label}
              {key !== 'all' && templates.length > 0 && (
                <span className={`ml-0.5 opacity-75`}>
                  ({templates.filter((t) => t.category === key).length})
                </span>
              )}
            </button>
          ))}
        </div>

        {/* 模板列表 */}
        <div className="flex-1 overflow-y-auto p-5 custom-scrollbar">
          {isLoading && templates.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16 text-gray-400">
              <Loader2 className="w-8 h-8 animate-spin mb-3 text-blue-500" />
              <p className="text-sm">正在从后端加载最新模板...</p>
            </div>
          ) : hasError && templates.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16 text-gray-400">
              <AlertTriangle className="w-8 h-8 mb-3 text-yellow-500" />
              <p className="text-sm text-gray-600 mb-3">模板加载失败，请检查网络连接或后端模板服务</p>
              <button
                onClick={() => loadTemplates(true)}
                className="px-4 py-2 text-sm bg-blue-500 text-white rounded-xl hover:bg-blue-600 transition-colors"
              >
                重新加载
              </button>
            </div>
          ) : filtered.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16 text-gray-400">
              <BookOpen className="w-8 h-8 mb-3 opacity-40" />
              <p className="text-sm">该分类暂无模板</p>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {filtered.map((template) => {
                const isSelected = template.id === currentTemplateId;
                return (
                  <div
                    key={template.id}
                    onClick={() => handleSelect(template)}
                    className={`relative p-4 rounded-2xl border-2 cursor-pointer transition-all select-none ${
                      isSelected
                        ? 'border-green-400 bg-green-50 shadow-md'
                        : 'border-gray-200 hover:border-blue-300 hover:bg-blue-50/30 hover:shadow-sm'
                    }`}
                  >
                    {/* 已选标记 */}
                    {isSelected && (
                      <div className="absolute top-3 right-3 w-5 h-5 bg-green-500 rounded-full flex items-center justify-center">
                        <Check className="w-3 h-3 text-white" />
                      </div>
                    )}

                    {/* 模板标题行 */}
                    <div className="flex items-start gap-2 mb-2 pr-6">
                      <div className="flex-1 min-w-0">
                        <h4 className="font-semibold text-gray-800 text-sm truncate">
                          {template.name}
                        </h4>
                      </div>
                    </div>

                    {/* 难度/分类标签 */}
                    <div className="flex gap-1.5 mb-2">
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${difficultyColor(template.difficulty)}`}>
                        {difficultyLabel(template.difficulty)}
                      </span>
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${categoryColor(template.category)}`}>
                        {categoryLabel(template.category)}
                      </span>
                    </div>

                    {/* 描述 */}
                    <p className="text-xs text-gray-500 leading-relaxed line-clamp-2">
                      {template.description}
                    </p>

                    {/* 可调参数 */}
                    {template.params.length > 0 && (
                      <div className="mt-2.5 pt-2.5 border-t border-gray-100">
                        <p className="text-xs text-gray-400 mb-1.5">可调参数</p>
                        <div className="flex flex-wrap gap-1.5">
                          {template.params.slice(0, 4).map((param) => (
                            <span
                              key={param.name}
                              className="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded-lg"
                            >
                              {param.name}
                              {param.default !== undefined && (
                                <span className="text-blue-500 ml-0.5">={param.default}</span>
                              )}
                            </span>
                          ))}
                          {template.params.length > 4 && (
                            <span className="text-xs text-gray-400 px-1 py-0.5">
                              +{template.params.length - 4} 更多
                            </span>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* 底部提示 */}
        <div className="px-5 py-3 border-t border-gray-100 flex-shrink-0 bg-gray-50/50 rounded-b-2xl">
          <p className="text-xs text-gray-400">点击模板卡片即可选中并关闭此窗口</p>
        </div>
      </div>
    </div>
  );
};
