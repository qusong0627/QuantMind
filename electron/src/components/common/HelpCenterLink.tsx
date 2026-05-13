import React from 'react';
import { HelpCircle } from 'lucide-react';

interface Props {
  href?: string;
  compact?: boolean;
  variant?: 'default' | 'white';
  showIcon?: boolean;
  className?: string;
}

const HelpCenterLink: React.FC<Props> = ({ href = 'https://www.quantmindai.cn/help', compact = false, variant = 'default', showIcon = true, className = '' }) => {
  const variantClass = variant === 'white'
    ? 'text-white hover:text-blue-200 hover:bg-transparent'
    : 'text-gray-400 hover:text-blue-600 hover:bg-gray-50';

  const iconColorClass = variant === 'white'
    ? 'text-white/80 group-hover:text-white'
    : 'text-gray-300 group-hover:text-blue-500';

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title="帮助文档"
      aria-label="帮助文档"
      className={`flex items-center gap-2.5 px-3 py-2 rounded-xl transition-all group ${variantClass} ${className}`}
    >
      {showIcon && <HelpCircle className={`w-3.5 h-3.5 ${iconColorClass}`} />}
      <span className="text-[11px] font-semibold tracking-wide">帮助文档</span>
    </a>
  );
};

export default HelpCenterLink;
