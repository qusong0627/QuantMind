import React from 'react';
import {
  ArrowLeftRight,
  Boxes,
  Layers,
  CircleUserRound,
  FlaskConical,
  LayoutDashboard,
  Orbit,
  Rss,
  Search,
  ShieldCheck,
  SquareTerminal,
  Dna,
  FileText,
  Sparkles,
  Brain,
  BarChart3,
  Cpu,
  TrendingUp,
  CandlestickChart,
} from 'lucide-react';
import { useSelector } from 'react-redux';
import { selectCurrentMarket } from '../../store/slices/uiSlice';
import { getMarketConfig } from '../../config/marketConfig';

interface FloatingNavBarProps {
  current?: string;
  onChange?: (section: string) => void;
}

interface NavItemConfig {
  id: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}

export const FloatingNavBar: React.FC<FloatingNavBarProps> = ({ current, onChange }) => {
  const user = useSelector((state: any) => state.auth.user);
  const isAdmin = user?.is_admin || false;
  const currentMarket = useSelector(selectCurrentMarket);
  const marketLabel = getMarketConfig(currentMarket).label;

  const navItems: NavItemConfig[] = [
    // 1. 大盘分析模块
    { id: 'dashboard', label: marketLabel, icon: LayoutDashboard },
    { id: 'market-analysis', label: '市场分析', icon: BarChart3 },
    { id: 'rss-news', label: '新闻快讯', icon: Rss },
    { id: 'stock-terminal', label: '个股终端', icon: CandlestickChart },
    { id: 'ai-ide', label: 'AI-IDE', icon: SquareTerminal },
    { id: 'backtest', label: '回测中心', icon: FlaskConical },
    { id: 'trading', label: '模拟交易', icon: ArrowLeftRight },
    // 3. 模型区域
    { id: 'model-training', label: '模型训练', icon: Layers },
    { id: 'model-registry', label: '模型管理', icon: Boxes },
    { id: 'inference-center', label: '推理中心', icon: Cpu },
    { id: 'review-center', label: '模型复盘', icon: TrendingUp },
    // 4. 智能投研区域
    { id: 'research', label: '投研平台', icon: Search },
    { id: 'alpha-research', label: '因子挖掘', icon: Dna },
    { id: 'skills', label: '技能中心', icon: Sparkles },
    { id: 'agent', label: 'QuantBot', icon: Orbit },
    // 5. 个人中心
    { id: 'profile', label: '个人中心', icon: CircleUserRound }
  ];

  if (isAdmin) {
    navItems.push({ id: 'admin', label: '后台管理', icon: ShieldCheck });
  }

  const groupedNavItems: NavItemConfig[][] = [
    // 1. 大盘分析模块
    navItems.filter((item) => ['dashboard', 'market-analysis', 'rss-news'].includes(item.id)),
    // 2. 回测与交易区域
    navItems.filter((item) => ['stock-terminal', 'ai-ide', 'backtest', 'trading'].includes(item.id)),
    // 3. 模型区域
    navItems.filter((item) => ['model-training', 'model-registry', 'inference-center', 'review-center'].includes(item.id)),
    // 4. 智能投研区域
    navItems.filter((item) => ['research', 'alpha-research', 'skills', 'agent'].includes(item.id)),
    // 5. 个人与系统组
    navItems.filter((item) => ['profile', 'admin'].includes(item.id))
  ].filter((group) => group.length > 0);

  return (
    <nav className="bottom-dock" aria-label="主导航">
      <div className="bottom-dock-inner">
        {groupedNavItems.map((group, groupIndex) => (
          <React.Fragment key={`group-${groupIndex}`}>
            <div className="dock-group">
              {group.map((item) => {
                const Icon = item.icon;
                const isActive = current === item.id;

                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => onChange?.(item.id)}
                    className={`dock-item ${isActive ? 'active' : ''}`}
                    aria-current={isActive ? 'page' : undefined}
                    title={item.label}
                  >
                    <Icon className="dock-icon" />
                    <span className="dock-label">{item.label}</span>
                    {isActive && <span className="dock-active-dot" aria-hidden="true" />}
                  </button>
                );
              })}
            </div>
            {groupIndex < groupedNavItems.length - 1 && (
              <span className="dock-divider" aria-hidden="true" />
            )}
          </React.Fragment>
        ))}
      </div>
    </nav>
  );
};
