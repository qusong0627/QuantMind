import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { TradingCard, TradingMain, TradingShell, TradingSidebar } from './TradingShell';

describe('TradingShell layout primitives', () => {
  it('wraps content with the shell background and preserves a custom class', () => {
    const { container } = render(<TradingShell className="custom-shell"><span>交易页</span></TradingShell>);
    const shell = container.firstElementChild as HTMLElement;
    expect(shell).toHaveClass('p-6', 'custom-shell');
    expect(shell.style.background).toContain('radial-gradient');
    expect(screen.getByText('交易页')).toBeInTheDocument();
  });

  it.each([
    [undefined, '216px'], [320, '320px'], ['18rem', '18rem'],
  ])('supports default, numeric, and string sidebar widths', (width, expected) => {
    const { container } = render(<TradingSidebar width={width as number | string | undefined}><span>侧栏</span></TradingSidebar>);
    expect(container.querySelector('aside')).toHaveStyle({ width: expected });
    expect(screen.getByText('侧栏')).toBeInTheDocument();
  });

  it('renders main content above its decorative grid layer', () => {
    const { container } = render(<TradingMain><span>主内容</span></TradingMain>);
    expect(container.querySelector('main')).toHaveClass('relative', 'min-w-0', 'flex-1');
    expect(container.querySelector('.pointer-events-none')).toHaveStyle({ backgroundSize: '22px 22px' });
    expect(screen.getByText('主内容')).toBeInTheDocument();
  });

  it.each(['default', 'elevated', 'dark'] as const)('renders the %s card variant', (variant) => {
    const { container } = render(<TradingCard variant={variant} className="extra-card"><span>卡片</span></TradingCard>);
    expect(container.firstElementChild).toHaveClass('overflow-hidden', 'extra-card');
    expect(screen.getByText('卡片')).toBeInTheDocument();
  });
});
