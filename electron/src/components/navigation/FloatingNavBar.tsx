import React from 'react';
import {
  ArrowLeftRight,
  Boxes,
  Layers,
  FlaskConical,
  LayoutDashboard,
  Orbit,
  Rss,
  Search,
  ShieldCheck,
  TestTube2,
  FileText,
  Brain,
  BarChart3,
  Cpu,
  Sigma } from 'lucide-react';
import { useSelector } from 'react-redux';
import { selectCurrentMarket, selectTradingMode } from '../../store/slices/uiSlice';
import { getMarketConfig } from '../../config/marketConfig';
import { modeCopy } from '../../pages/trading/utils/tradingModeCopy';

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
  // 交易栏目名跟随模式（与交易页内所有模式文案同取唯一事实源）：
  // 切了实盘还写着「模拟交易」，用户会以为真金白银的单只是模拟单。
  const tradingMode = useSelector(selectTradingMode);

  const navItems: NavItemConfig[] = [
    // 1. 大盘分析模块
    { id: 'dashboard', label: marketLabel, icon: LayoutDashboard },
    { id: 'market-analysis', label: '市场分析', icon: BarChart3 },
    { id: 'rss-news', label: 'RSS信息流', icon: Rss },
    { id: 'backtest', label: '回测中心', icon: FlaskConical },
    { id: 'trading', label: modeCopy(tradingMode).full, icon: ArrowLeftRight },
    // 3. 模型区域
    { id: 'model-training', label: '模型训练', icon: Layers },
    { id: 'model-registry', label: '模型管理', icon: Boxes },
    { id: 'inference-center', label: '推理中心', icon: Cpu },
    // 4. 智能投研区域
    { id: 'research', label: '投研平台', icon: Search },
    { id: 'alpha-research', label: '因子挖掘', icon: TestTube2 },
    { id: 'factor-research', label: '因子研究', icon: Sigma },
    { id: 'agent', label: 'QuantBot', icon: Orbit },
  ];

  if (isAdmin) {
    navItems.push({ id: 'admin', label: '后台管理', icon: ShieldCheck });
  }

  const groupedNavItems: NavItemConfig[][] = [
    // 1. 大盘分析模块
    navItems.filter((item) => ['dashboard', 'market-analysis', 'rss-news'].includes(item.id)),
    // 2. 回测与交易区域（AI-IDE/个股终端/今日交易台已迁移入口：QuantBot 顶栏 / 市场分析搜索浮窗 / 模拟交易页签，全屏路由保留）
    navItems.filter((item) => ['backtest', 'trading'].includes(item.id)),
    // 3. 模型区域
    navItems.filter((item) => ['model-training', 'model-registry', 'inference-center'].includes(item.id)),
    // 4. 智能投研区域
    navItems.filter((item) => ['research', 'alpha-research', 'factor-research', 'agent'].includes(item.id)),
    // 5. 个人与系统组（个人中心已移入后台管理，底部栏不再占据）
    navItems.filter((item) => ['admin'].includes(item.id))
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
