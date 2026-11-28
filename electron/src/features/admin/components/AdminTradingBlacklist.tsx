/**
 * 后台管理 · 交易核心 · 交易黑名单（2026-11-28 自模拟交易「个人中心」迁入）。
 *
 * 名单是**全平台一份**（`data/exclusions/<market>.json` 机器基线 +
 * `cn_user.json` 手工层），不是某个账户的私有配置：
 *
 * - 模拟盘与实盘的候选信号共用同一份排除结果（StockSidebar 默认勾选「名单」）；
 * - 推送下单的逐票风险预检读同一份（`push_orders._resolve_risk`）；
 * - 决策轮/持仓哨兵读同一份（`decision_round_io.load_excluded_symbols`）。
 *
 * 所以维护入口归「交易核心」，不归任何一个交易界面；推理/组合选股后续也直接引用
 * 这份名单，不再各建一套。
 */
import React from 'react';
import { ShieldAlert } from 'lucide-react';

import { TradingBlacklistPanel } from '../../trading-blacklist/TradingBlacklistPanel';

export const AdminTradingBlacklist: React.FC = () => (
  <div className="flex h-full min-h-0 flex-col gap-3">
    <div className="flex shrink-0 items-start gap-2 rounded-xl border border-rose-100 bg-rose-50/60 px-3 py-2 text-[11px] leading-relaxed text-rose-700">
      <ShieldAlert size={14} className="mt-0.5 shrink-0" />
      <span>
        全局名单：<b>模拟盘与实盘</b>的候选信号、推送预检共用这一份，后续推理/组合排名同样按此排除。
        机器基线由导入器整份覆盖（只读，可逐只放行）；手工加入与放行只影响叠加层，重导不会丢。
      </span>
    </div>
    <div className="min-h-0 flex-1">
      <TradingBlacklistPanel />
    </div>
  </div>
);
