/**
 * 用法指南组件
 */

import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ChevronDown, ChevronRight, Newspaper, Mail, FileText, FileSpreadsheet } from 'lucide-react';
import { GuideSection as GuideSectionType } from '../../types';
import { useDispatch } from 'react-redux';
import { setInputValue } from '../../store/chatSlice';

const initialGuides: GuideSectionType[] = [
  {
    category: '📰 新闻与检索',
    icon: 'Newspaper',
    examples: [
      '帮我查今天的财经新闻重点',
      '请整理最近一周的科技新闻头条',
      '搜索贵州茅台最新财报并总结要点',
    ],
    isExpanded: true,
  },
  {
    category: '📧 邮件协作',
    icon: 'Mail',
    examples: [
      '列出我今天未读的重要邮件',
      '帮我起草一封跟进客户的邮件',
      '搜索上周关于合同的往来邮件',
    ],
    isExpanded: false,
  },
  {
    category: '📄 文档处理',
    icon: 'FileText',
    examples: [
      '帮我生成一份项目备忘录 Word 文档',
      '读取这个 PDF 并提取表格内容',
      '把这份扫描版 PDF 做 OCR 后整理摘要',
    ],
    isExpanded: false,
  },
  {
    category: '📊 表格与自动化',
    icon: 'FileSpreadsheet',
    examples: [
      '读取这个 Excel 并生成统计图表',
      '创建一个每周一早上发送日报的定时任务',
      '帮我配置 QuantBot 的钉钉频道接入',
    ],
    isExpanded: false,
  },
];

const iconMap: Record<string, React.ComponentType<any>> = {
  Newspaper,
  Mail,
  FileText,
  FileSpreadsheet,
};

const GuideSection: React.FC = () => {
  const [guides, setGuides] = useState(initialGuides);
  const dispatch = useDispatch();

  const toggleSection = (index: number) => {
    (setGuides as any)(prev =>
      prev.map((guide, i) =>
        i === index ? { ...guide, isExpanded: !guide.isExpanded } : guide
      )
    );
  };

  const handleExampleClick = (example: string) => {
    dispatch(setInputValue(example));
  };

  return (
    <div className="space-y-3">
      <h2 className="text-sm font-semibold text-gray-700 px-2">使用指南</h2>

      {guides.map((guide, index) => {
        const Icon = iconMap[guide.icon];

        return (
          <div key={index} className="bg-gray-50 rounded-xl overflow-hidden border border-gray-100">
            <button
              onClick={() => toggleSection(index)}
              className="w-full flex items-center justify-between p-3.5 hover:bg-gray-100 transition-colors"
            >
              <div className="flex items-center gap-2.5">
                {Icon && <Icon className="w-4 h-4 text-slate-500" />}
                <span className="text-sm font-semibold text-slate-700">
                  {guide.category}
                </span>
              </div>
              {guide.isExpanded ? (
                <ChevronDown className="w-4 h-4 text-slate-400" />
              ) : (
                <ChevronRight className="w-4 h-4 text-slate-400" />
              )}
            </button>

            <AnimatePresence>
              {guide.isExpanded && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: 0.2 }}
                  className="overflow-hidden bg-white/50"
                >
                  <div className="px-3 pb-3 space-y-1">
                    {guide.examples.map((example, exIndex) => (
                      <button
                        key={exIndex}
                        onClick={() => handleExampleClick(example)}
                        className="w-full text-left px-3 py-2 text-[11px] text-slate-600 hover:bg-blue-50 hover:text-blue-600 rounded-lg transition-all cursor-pointer leading-relaxed"
                      >
                        • {example}
                      </button>
                    ))}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        );
      })}
    </div>
  );
};

export default GuideSection;
