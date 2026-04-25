import React from 'react';
import {
  ArrowLeftRight,
  Boxes,
  BrainCircuit,
  CircleUserRound,
  FlaskConical,
  LayoutDashboard,
  LineChart,
  MessagesSquare,
  Orbit,
  ShieldCheck,
  SquareTerminal
} from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import { useSelector } from 'react-redux';
import { useCapabilities } from '../../hooks/useCapabilities';

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
  const { hasFeature, isOSS } = useCapabilities();

  const allNavItems: NavItemConfig[] = [
    { id: 'dashboard', label: '仪表盘', icon: LayoutDashboard },
    { id: 'strategy', label: '智能策略', icon: LineChart },
    { id: 'ai-ide', label: 'AI-IDE', icon: SquareTerminal },
    { id: 'model-training', label: '模型训练', icon: BrainCircuit },
    { id: 'backtest', label: '回测中心', icon: FlaskConical },
    { id: 'agent', label: 'QuantBot', icon: Orbit },
    { id: 'model-registry', label: '模型管理', icon: Boxes },
    { id: 'trading', label: '实盘交易', icon: ArrowLeftRight },
    { id: 'community', label: '策略社区', icon: MessagesSquare },
    { id: 'profile', label: '个人中心', icon: CircleUserRound }
  ];

  // 根据版本功能过滤
  const navItems = allNavItems.filter(item => {
    if (item.id === 'community' && !hasFeature('community')) return false;
    // 实盘交易在 OSS 版由于缺乏行情也暂且隐藏或标注
    // if (item.id === 'trading' && isOSS) return false;
    return true;
  });

  if (isAdmin) {
    navItems.push({ id: 'admin', label: '后台管理', icon: ShieldCheck });
  }

  const groupedNavItems: NavItemConfig[][] = [
    navItems.filter((item) => ['dashboard', 'strategy', 'ai-ide', 'model-training', 'backtest'].includes(item.id)),
    navItems.filter((item) => ['agent', 'model-registry', 'trading', 'community'].includes(item.id)),
    navItems.filter((item) => ['profile', 'admin'].includes(item.id))
  ].filter((group) => group.length > 0);

  return (
    <nav className="floating-nav-container pointer-events-none" style={{ zIndex: 9999 }}>
      <motion.div
        className="floating-nav-bar pointer-events-auto"
        initial={{ y: 20, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        transition={{ type: "spring", stiffness: 260, damping: 20 }}
      >
        <div className="nav-shell">
          {groupedNavItems.map((group, groupIndex) => (
            <React.Fragment key={`group-${groupIndex}`}>
              <div className="nav-group">
                {group.map((item) => {
                  const Icon = item.icon;
                  const isActive = current === item.id;

                  return (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => onChange?.(item.id)}
                      className={`nav-item group relative ${isActive ? 'active' : ''}`}
                      aria-current={isActive ? 'page' : undefined}
                    >
                      <span className="nav-item-glow" aria-hidden="true" />
                      <div className="nav-item-content relative z-10">
                        <motion.div
                          animate={{
                            scale: isActive ? 1.08 : 1,
                            y: isActive ? -1 : 0,
                            color: isActive ? 'var(--primary-blue)' : 'var(--slate-600)'
                          }}
                          transition={{ type: 'spring', stiffness: 320, damping: 22 }}
                          className="flex flex-col items-center gap-1"
                        >
                          <Icon className="nav-icon" />
                          <span className="nav-label">
                            {item.label}
                          </span>
                        </motion.div>
                      </div>

                      <AnimatePresence initial={false}>
                        {isActive && (
                          <motion.div
                            layoutId="nav-active-bubble"
                            className="nav-active-bubble absolute inset-0 z-0"
                            transition={{ type: "spring", bounce: 0.18, duration: 0.34 }}
                          >
                            <span className="nav-active-bar" aria-hidden="true" />
                          </motion.div>
                        )}
                      </AnimatePresence>

                      {!isActive && (
                        <motion.div
                          className="nav-hover-bubble absolute inset-0 z-0"
                          initial={{ opacity: 0 }}
                          whileHover={{ opacity: 1 }}
                          transition={{ duration: 0.2 }}
                        />
                      )}
                    </button>
                  );
                })}
              </div>
              {groupIndex < groupedNavItems.length - 1 && (
                <span className="nav-divider" aria-hidden="true" />
              )}
            </React.Fragment>
          ))}
        </div>
      </motion.div>
    </nav>
  );
};
