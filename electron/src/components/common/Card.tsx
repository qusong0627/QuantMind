import React from 'react';
import { motion } from 'framer-motion';

interface CardProps {
  /** 卡标题：字符串或元素（首页六宫格在市场名旁挂市场徽标用时传元素） */
  title?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
  height?: string;
  background?: string;
}

export const Card: React.FC<CardProps> = ({
  title,
  className = '',
  children,
  height = 'auto',
  background = 'default'
}) => {
  const backgroundClass = {
    default: 'bg-white border border-slate-200 shadow-sm',
    market: 'bg-white border border-slate-200 shadow-sm',
    fund: 'bg-white border border-slate-200 shadow-sm',
    trade: 'bg-white border border-slate-200 shadow-sm',
    strategy: 'bg-white border border-slate-200 shadow-sm',
    charts: 'bg-white border border-slate-200 shadow-sm',
    alert: 'bg-white border border-slate-200 shadow-sm'
  }[background] || 'bg-white border border-slate-200 shadow-sm';

  return (
    <motion.div
      className={`panel-card ${backgroundClass} ${className}`}
      style={{ height }}
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 300, damping: 25 }}
    >
      {title && (
        <h3 className="panel-title text-slate-800 font-bold tracking-wide opacity-90">{title}</h3>
      )}
      <div className="panel-body">
        {children}
      </div>
    </motion.div>
  );
};
