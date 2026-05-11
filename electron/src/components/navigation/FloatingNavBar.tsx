import React from 'react';
import {
  ArrowLeftRight,
  Boxes,
  Layers,
  CircleUserRound,
  FlaskConical,
  LayoutDashboard,
  LineChart,
  Orbit,
  Search,
  ShieldCheck,
  SquareTerminal
} from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import { useSelector } from 'react-redux';

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

  const navItems: NavItemConfig[] = [
    { id: 'dashboard', label: '仪表盘', icon: LayoutDashboard },
    { id: 'strategy', label: '智能策略', icon: LineChart },
    { id: 'ai-ide', label: 'AI-IDE', icon: SquareTerminal },
    { id: 'backtest', label: '回测中心', icon: FlaskConical },
    { id: 'agent', label: 'QuantBot', icon: Orbit },
    { id: 'model-training', label: '模型训练', icon: Layers },
    { id: 'model-registry', label: '模型管理', icon: Boxes },
    { id: 'research', label: '投研平台', icon: Search },
    { id: 'trading', label: '实盘交易', icon: ArrowLeftRight },
    { id: 'profile', label: '个人中心', icon: CircleUserRound }
  ];

  if (isAdmin) {
    navItems.push({ id: 'admin', label: '后台管理', icon: ShieldCheck });
  }

  const groupedNavItems: NavItemConfig[][] = [
    navItems.filter((item) => ['dashboard', 'strategy', 'ai-ide', 'backtest', 'agent'].includes(item.id)),
    navItems.filter((item) => ['model-training', 'model-registry', 'research', 'trading'].includes(item.id)),
    navItems.filter((item) => ['profile', 'admin'].includes(item.id))
  ].filter((group) => group.length > 0);

  return (
    <nav className="floating-nav-container pointer-events-none" style={{ zIndex: 1050 }}>
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
                            y: isActive ? -2 : 0,
                            color: isActive ? 'var(--primary-blue)' : 'var(--slate-600)'
                          }}
                          whileHover={{ scale: 1.15, y: -4 }}
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
