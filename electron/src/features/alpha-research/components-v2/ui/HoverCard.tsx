/**
 * Minimal HoverCard fallback (no @radix-ui dependency).
 *
 * The AutoAlpha frontend-v2 originally used @radix-ui/react-hover-card.
 * To avoid pulling in another dependency just for one hover popover, this
 * implementation gives the same three-export surface backed by a
 * CSS-only :hover (and focus-within) reveal. It loses Radix's accessibility
 * polish (no escape-to-close, no Portal, no smart positioning), but it's good
 * enough for the FactorList row hover-preview where it's used.
 */

import * as React from 'react';
import { cn } from '../../utils-v2';

interface HoverCardProps {
  children: React.ReactNode;
  /** Radix API parity — accepted but unused by the fallback. */
  openDelay?: number;
  closeDelay?: number;
  open?: boolean;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
}
const HoverCard: React.FC<HoverCardProps> = ({ children }) => (
  <span className="qa-hover-card-root relative inline-flex">
    {children}
  </span>
);

interface HoverCardTriggerProps extends React.HTMLAttributes<HTMLSpanElement> {
  asChild?: boolean;
  children: React.ReactNode;
}
const HoverCardTrigger: React.FC<HoverCardTriggerProps> = ({
  asChild: _asChild,
  className,
  children,
  ...rest
}) => (
  <span className={cn('qa-hover-card-trigger', className)} {...rest}>
    {children}
  </span>
);

interface HoverCardContentProps extends React.HTMLAttributes<HTMLDivElement> {
  align?: 'start' | 'center' | 'end';
  sideOffset?: number;
  /** Radix API parity — accepted but unused. */
  side?: 'top' | 'right' | 'bottom' | 'left';
  collisionPadding?: number;
  avoidCollisions?: boolean;
  forceMount?: boolean;
}
const HoverCardContent = React.forwardRef<HTMLDivElement, HoverCardContentProps>(
  ({ className, align = 'center', sideOffset = 4, style, children, ...rest }, ref) => {
    const alignStyle: React.CSSProperties =
      align === 'start' ? { left: 0 } : align === 'end' ? { right: 0 } : { left: '50%', transform: 'translateX(-50%)' };
    return (
      <div
        ref={ref}
        className={cn(
          'qa-hover-card-content pointer-events-none absolute z-50 w-64 rounded-md border bg-popover p-4 text-popover-foreground shadow-md opacity-0 transition-opacity duration-150',
          className,
        )}
        style={{ top: `calc(100% + ${sideOffset}px)`, ...alignStyle, ...style }}
        {...rest}
      >
        {children}
      </div>
    );
  },
);
HoverCardContent.displayName = 'HoverCardContent';

export { HoverCard, HoverCardTrigger, HoverCardContent };
