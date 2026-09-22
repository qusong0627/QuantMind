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
  Radio,
  Sigma } from 'lucide-react';
import { useSelector } from 'react-redux';
import { selectCurrentMarket, selectTradingMode } from '../../store/slices/uiSlice';
import { getMarketConfig } from '../../config/marketConfig';
import { isLiveTradingEnabled } from '../../config/tradingFlags';
import { LIVE_NODE_ONLY, LIVE_NODE_NAV_IDS } from '../../config/liveNodeFlags';
import { isLocalLiveAvailable } from '../../features/shared/localLive';
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
  // 实盘开关关闭时恒为模拟盘——栏目名不再跟随，避免出现指向已隐藏功能的入口。
  const rawTradingMode = useSelector(selectTradingMode);
  const tradingMode = isLiveTradingEnabled() ? rawTradingMode : 'simulation';
  // 本机有独立的「实盘交易」栏目时，交易栏目**恒为模拟交易**：那一栏的模式已定死
  // （`resolveSimColumnForcedMode`），栏目名再跟着全局模式走，就会出现两个都叫
  // 「实盘交易」的入口，而其中一个点进去是模拟盘。
  const tradingLabelKey = isLocalLiveAvailable ? 'simulation' : tradingMode;

  const navItems: NavItemConfig[] = [
    // 1. 大盘分析模块
    { id: 'dashboard', label: marketLabel, icon: LayoutDashboard },
    { id: 'market-analysis', label: '市场分析', icon: BarChart3 },
    { id: 'rss-news', label: 'RSS信息流', icon: Rss },
    { id: 'backtest', label: '回测中心', icon: FlaskConical },
    { id: 'trading', label: modeCopy(tradingLabelKey).full, icon: ArrowLeftRight },
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

  // 本机独有的「实盘交易」栏目：公开仓没有 electron/src/features/local-live/ 目录，
  // isLocalLiveAvailable 恒为 false，这一项根本不进数组——不是"藏起来"，是没构造。
  // 刻意不接 isLiveTradingEnabled：那个开关管的是"公开树里实盘组件渲染不渲染"，
  // 而这一项在本机就是常驻入口（开发者自用的真实盘，与控制公开发行的开关无关）。
  if (isLocalLiveAvailable) {
    navItems.push({ id: 'live', label: '实盘交易', icon: Radio });
  }

  // 实盘节点形态（Windows 单机包）：只留白名单里那两栏，顺序也照白名单。
  // 这里**直接按白名单构造**，不复用下面的共享分组：共享分组的白名单是给
  // 全栏目形态排版的，改它（加一栏、调顺序）会在这一形态里静默少一栏或错位。
  const groupedNavItems: NavItemConfig[][] = LIVE_NODE_ONLY
    ? [LIVE_NODE_NAV_IDS
        .map((id) => navItems.find((item) => item.id === id))
        .filter((item): item is NavItemConfig => Boolean(item))]
    : [
      // 1. 大盘分析模块
      navItems.filter((item) => ['dashboard', 'market-analysis', 'rss-news'].includes(item.id)),
      // 2. 回测与交易区域（AI-IDE/个股终端/今日交易台已迁移入口：QuantBot 顶栏 / 市场分析搜索浮窗 / 模拟交易页签，全屏路由保留）
      // 'live' 排在这一组末尾：本机存在时它是同组的第三项，不存在时 filter 自动只剩前两项。
      navItems.filter((item) => ['backtest', 'trading', 'live'].includes(item.id)),
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
                    // 栏目身份。**按 id 断言，不要按文案断言**：文案随交易模式变
                    // （模拟交易/实盘交易），而本机还会多出一个同名的「实盘交易」
                    // 栏目 —— 按文案取会在本机形态下取到两个元素。
                    data-nav-id={item.id}
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
