import React, { useState } from 'react';
import { Button, Card, Divider, Checkbox, Tag, Empty, Typography, Tooltip } from 'antd';
import { Database, ShieldCheck, ChevronRight, Lock } from 'lucide-react';
import { clsx } from 'clsx';
import { AnimatePresence, motion } from 'framer-motion';
import { FeatureCategory, TRAINING_BASE_FEATURES } from './trainingUtils';

const { Text } = Typography;

interface FeatureSelectorProps {
  categories: FeatureCategory[];
  selectedFeatures: string[];
  onChange: (features: string[]) => void;
  loading: boolean;
  /** 未创建因子集时跳转后台训练服务的引导回调（刷新字段执行数据扫描） */
  onGuide?: () => void;
}

const SectionHeader: React.FC<{ title: string; desc: string; icon?: React.ReactNode }> = ({ title, desc, icon }) => (
  <div className="flex items-start justify-between gap-4">
    <div>
      <div className="flex items-center gap-2">
        {icon}
        <Typography.Title level={4} className="!mb-0 !text-slate-900">
          {title}
        </Typography.Title>
      </div>
      <Typography.Paragraph className="!mb-0 !mt-2 !text-xs !text-slate-500 leading-relaxed">
        {desc}
      </Typography.Paragraph>
    </div>
  </div>
);

export const FeatureSelector: React.FC<FeatureSelectorProps> = ({ 
  categories, 
  selectedFeatures, 
  onChange, 
  loading,
  onGuide
}) => {
  const [expandedId, setExpandedId] = useState<string>(categories[0]?.id || '');

  const toggleFeature = (key: string) => {
    if (TRAINING_BASE_FEATURES.includes(key)) return;
    if (selectedFeatures.includes(key)) {
      onChange(selectedFeatures.filter(f => f !== key));
    } else {
      onChange([...selectedFeatures, key]);
    }
  };

  const toggleCategory = (category: FeatureCategory) => {
    const categoryKeys = category.features.map(f => f.key);
    const mandatoryKeysInCategory = categoryKeys.filter(k => TRAINING_BASE_FEATURES.includes(k));
    
    const allSelected = categoryKeys.every(k => selectedFeatures.includes(k));
    
    if (allSelected) {
      // Unselect all EXCEPT mandatory ones
      onChange([
        ...selectedFeatures.filter(f => !categoryKeys.includes(f)),
        ...mandatoryKeysInCategory
      ]);
    } else {
      const otherFeatures = selectedFeatures.filter(f => !categoryKeys.includes(f));
      onChange([...otherFeatures, ...categoryKeys]);
    }
  };

  return (
    <div className="grid gap-4 xl:grid-cols-[1.3fr_0.9fr]">
      <Card className="rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20 } }}>
        <SectionHeader
          title="第一步：选择特征维度"
          desc="按大类折叠选择模型输入特征，默认展开动量模块。点击其他模块标题后，当前模块会自动收起，始终只保留一个展开内容。"
          icon={<Database size={18} className="text-blue-500" />}
        />
        <Divider className="my-4" />
        <div className="space-y-2.5">
          {categories.length === 0 && !loading && (
            <div className="rounded-2xl border border-dashed border-amber-200 bg-amber-50 py-10 text-center">
              <div className="text-sm font-medium text-amber-700">尚未创建可用因子集</div>
              <div className="mt-1.5 text-xs text-amber-600">字段集尚未刷新，请在后台完成『刷新字段』数据扫描后再返回选择特征。</div>
              {onGuide && (
                <Button
                  size="small"
                  type="primary"
                  className="mt-4 rounded-xl font-bold h-8 px-4"
                  onClick={onGuide}
                >
                  前往后台刷新字段
                </Button>
              )}
            </div>
          )}
          {categories.map((category) => {
            const categoryFeatureKeys = new Set(category.features.map((feature) => feature.key));
            const categorySelectedCount = selectedFeatures.filter((featureKey) => categoryFeatureKeys.has(featureKey)).length;
            const isExpanded = expandedId === category.id;
            
            return (
              <div key={category.id} className="relative overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-sm transition-shadow hover:shadow-md">
                {isExpanded ? <div className="absolute left-0 top-0 h-full w-1 bg-blue-500" /> : null}
                <button
                  type="button"
                  onClick={() => setExpandedId(isExpanded ? '' : category.id)}
                  className={clsx(
                    'relative w-full flex items-center justify-between gap-3 px-5 py-4 text-left transition-colors',
                    isExpanded ? 'bg-blue-50' : 'bg-white hover:bg-gray-50',
                  )}
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <div className={clsx(
                      'flex h-9 w-9 items-center justify-center rounded-2xl shrink-0 transition-colors',
                      isExpanded ? 'bg-blue-500/10 text-blue-600 shadow-sm' : 'bg-gray-100 text-gray-600',
                    )}>
                      <span className="flex items-center justify-center [&_svg]:h-4 [&_svg]:w-4">
                        {category.icon}
                      </span>
                    </div>
                    <div className="min-w-0">
                      <div className="font-medium text-gray-900 flex items-center gap-2">
                        <span>{category.name}</span>
                        {isExpanded && <Tag className="m-0 rounded-full border-0 bg-blue-100 text-blue-600">展开中</Tag>}
                      </div>
                      <div className="text-xs text-gray-500">
                        {category.features.length} 个候选特征 · 已选 {categorySelectedCount} 项
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <Tag className={clsx(
                      'm-0 rounded-full border-0 px-2.5 py-0.5',
                      isExpanded ? 'bg-blue-50 text-blue-600' : 'bg-gray-100 text-gray-500',
                    )}>
                      已选 {categorySelectedCount}
                    </Tag>
                    <ChevronRight className={clsx('w-4 h-4 text-gray-400 transition-transform', isExpanded && 'rotate-90 text-blue-500')} />
                  </div>
                </button>

                <AnimatePresence initial={false}>
                  {isExpanded && (
                    <motion.div
                      key={category.id}
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: 'auto', opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.18 }}
                      className="overflow-hidden"
                    >
                      <div className="border-t border-gray-100 bg-gray-50/70 px-5 py-4">
                        <div className="mb-3 flex items-center justify-between">
                          <Checkbox
                            checked={category.features.every((f) => selectedFeatures.includes(f.key))}
                            indeterminate={
                              category.features.some((f) => selectedFeatures.includes(f.key)) &&
                              !category.features.every((f) => selectedFeatures.includes(f.key))
                            }
                            onChange={() => toggleCategory(category)}
                            className="text-xs font-medium text-slate-600"
                          >
                            全选本类特征 ({category.features.length})
                          </Checkbox>
                          <Text type="secondary" className="text-[10px] uppercase tracking-wider">
                            {categorySelectedCount} / {category.features.length}
                          </Text>
                        </div>
                        <div className="grid grid-cols-2 gap-2.5">
                          {category.features.map((feature) => {
                            const value = feature.key;
                            const isMandatory = TRAINING_BASE_FEATURES.includes(value);
                            const checked = isMandatory || selectedFeatures.includes(value);
                            const tile = (
                              <div
                                key={value}
                                className={clsx(
                                  'flex items-center gap-2.5 rounded-2xl border px-3 py-2.5 transition-colors select-none',
                                  isMandatory ? 'cursor-not-allowed opacity-80' : 'cursor-pointer',
                                  checked
                                    ? 'border-blue-200 bg-blue-50/80 shadow-sm'
                                    : 'border-gray-200 bg-white hover:border-blue-200 hover:bg-blue-50/40',
                                )}
                                onClick={() => !isMandatory && toggleFeature(value)}
                              >
                                <Checkbox
                                  checked={checked}
                                  disabled={isMandatory}
                                  onClick={(event) => event.stopPropagation()}
                                  onChange={() => !isMandatory && toggleFeature(value)}
                                  className="m-0 shrink-0"
                                />
                                <div className="flex-1 flex items-center justify-between min-w-0 gap-2">
                                  <span className={clsx('text-xs truncate', checked ? 'text-blue-700 font-medium' : 'text-gray-600')}>
                                    {feature.label}
                                  </span>
                                  {isMandatory && (
                                    <Tag className="m-0 px-1 py-0 border-0 bg-blue-100 text-blue-600 text-[10px] scale-90 flex items-center gap-0.5">
                                      <Lock size={8} /> 必选
                                    </Tag>
                                  )}
                                </div>
                              </div>
                            );
                            return feature.explanation ? (
                              <Tooltip key={value} title={feature.explanation} placement="topLeft" mouseEnterDelay={0.4}>
                                {tile}
                              </Tooltip>
                            ) : tile;
                          })}
                        </div>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            );
          })}
        </div>
      </Card>

      <Card className="rounded-3xl border-slate-200 shadow-sm" styles={{ body: { padding: 20 } }}>
        <SectionHeader
          title="特征预览"
          desc="同步展示已选特征、分类归属和整理后的输入蓝图。"
          icon={<ShieldCheck size={18} className="text-emerald-500" />}
        />
        <Divider className="my-4" />
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold text-slate-800">已选字段</div>
          <Tag className="m-0 rounded-full border-0 bg-emerald-50 text-emerald-600">{selectedFeatures.length} 项</Tag>
        </div>
        <div className="mt-3 min-h-40 rounded-2xl border border-slate-200 bg-slate-50 p-4">
          {selectedFeatures.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="请从左侧选择至少一个特征维度" />
          ) : (
            <div className="space-y-3">
              {categories.map((category) => {
                const items = category.features.filter((feature) => selectedFeatures.includes(feature.key));
                if (items.length === 0) return null;
                return (
                  <div key={category.id} className="rounded-2xl border border-white bg-white p-3 shadow-sm">
                    <div className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-400">{category.name}</div>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {items.map((item) => (
                        <Tag
                          key={item.key}
                          closable={!TRAINING_BASE_FEATURES.includes(item.key)}
                          onClose={() => toggleFeature(item.key)}
                          className={clsx(
                            "m-0 rounded-lg text-[10px] py-0.5 flex items-center gap-1",
                            TRAINING_BASE_FEATURES.includes(item.key) 
                              ? "border-blue-100 bg-blue-50 text-blue-600 font-bold" 
                              : "border-slate-100 bg-slate-50 text-slate-600"
                          )}
                        >
                          {TRAINING_BASE_FEATURES.includes(item.key) && <Lock size={8} />}
                          {item.label}
                        </Tag>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </Card>
    </div>
  );
};
