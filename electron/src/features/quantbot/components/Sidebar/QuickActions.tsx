/**
 * 快速示例按钮组件
 */

import React from 'react';
import { motion } from 'framer-motion';
import { QuickAction as QuickActionType } from '../../types';
import { useDispatch } from 'react-redux';
import { setInputValue } from '../../store/chatSlice';

const quickActions: QuickActionType[] = [
  {
    label: '查财经新闻',
    prompt: '请整理今天最重要的财经新闻，并给出简要解读',
    icon: '📰',
    color: 'blue',
  },
  {
    label: '处理邮件',
    prompt: '请列出我最近的重要未读邮件，并按优先级排序',
    icon: '📧',
    color: 'green',
  },
  {
    label: '创建定时任务',
    prompt: '请帮我创建一个每周一上午9点执行的定时任务，用于提醒我整理本周待办',
    icon: '⏰',
    color: 'purple',
  },
  {
    label: '读取 PDF',
    prompt: '请读取这份 PDF，并帮我提取正文和表格要点',
    icon: '📑',
    color: 'blue',
  },
  {
    label: '整理 Excel',
    prompt: '请读取这个 Excel 文件，清洗数据并生成汇总结论',
    icon: '📊',
    color: 'green',
  },
  {
    label: '配置钉钉',
    prompt: '请帮我配置 QuantBot 的钉钉频道接入，告诉我需要哪些参数',
    icon: '💬',
    color: 'purple',
  },
];

const colorClasses = {
  blue: 'bg-blue-50 hover:bg-blue-100 text-blue-700 border-blue-200',
  green: 'bg-green-50 hover:bg-green-100 text-green-700 border-green-200',
  purple: 'bg-purple-50 hover:bg-purple-100 text-purple-700 border-purple-200',
};

const QuickActions: React.FC = () => {
  const dispatch = useDispatch();

  const handleClick = (prompt: string) => {
    dispatch(setInputValue(prompt));
  };

  return (
    <div className="space-y-3">
      <h2 className="text-sm font-semibold text-gray-700 px-2">快速开始</h2>

      <div className="space-y-2">
        {quickActions.map((action, index) => (
          <motion.button
            key={index}
            whileHover={{ scale: 1.02, x: 4 }}
            whileTap={{ scale: 0.98 }}
            onClick={() => handleClick(action.prompt)}
            className={`w-full p-3.5 rounded-xl border text-left transition-all shadow-sm hover:shadow-md ${
              colorClasses[action.color as keyof typeof colorClasses]
            }`}
          >
            <div className="flex items-center gap-3">
              <span className="text-xl filter drop-shadow-sm">{action.icon}</span>
              <span className="text-sm font-semibold">{action.label}</span>
            </div>
          </motion.button>
        ))}
      </div>
    </div>
  );
};

export default QuickActions;
