/** 交易通道在线状态推导（纯函数，供 ChannelStatus 组件与 vitest）。
 *
 *  「在线」有两个层次，界面上必须分开说，否则会把「桥活着但账户读不到」
 *  显示成一切正常（2026-09-10 实况：通达信桥 health ok、tdx_connected=true，
 *  但账户通道返回 0 持仓，用户看到的却是 QMT 的 2385 万）：
 *   - 通道/网关联通  ← 桥健康检查、tdx_connected、OpenD 握手
 *   - 账户数据新鲜度 ← 快照 age_sec（queries 有数据、多久没更新）
 */

import type { LiveAccount, RealAccountChannel, RealAccountSummary } from '../api/client';

export type ChanState = 'ok' | 'warn' | 'off' | 'unknown';

/** 快照新鲜度阈值（秒）：≤FRESH 实时；≤LAG 滞后；更久 = 停更 */
export const FRESH_SEC = 180;
export const LAG_SEC = 3600;

export interface ChanLine {
  k: string;
  v: string;
  tone?: ChanState;
}

export interface ChannelProbe {
  key: RealAccountChannel | 'futu' | 'ibkr';
  label: string;
  /** 该通道在系统里的角色（执行 / 只读观测 / 行情+交易） */
  role: string;
  state: ChanState;
  stateText: string;
  lines: ChanLine[];
}

/** 秒 → 人话时长（详情页用，越短越精确） */
export const fmtAgeSec = (sec: number | null | undefined): string => {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return '—';
  if (sec < 90) return `${Math.round(sec)} 秒前`;
  if (sec < 5400) return `${Math.round(sec / 60)} 分钟前`;
  if (sec < 172800) return `${(sec / 3600).toFixed(1)} 小时前`;
  return `${(sec / 86400).toFixed(1)} 天前`;
};

/** 快照年龄 → 状态（把「账户通道有没有数据」和「数据有多新」分开判） */
export const freshnessState = (ageSec: number | null | undefined): ChanState => {
  if (ageSec == null || !Number.isFinite(ageSec)) return 'unknown';
  if (ageSec <= FRESH_SEC) return 'ok';
  if (ageSec <= LAG_SEC) return 'warn';
  return 'off';
};

const freshnessText = (ageSec: number | null | undefined): string => {
  if (ageSec == null) return '无快照';
  const st = freshnessState(ageSec);
  if (st === 'ok') return `实时（${fmtAgeSec(ageSec)}）`;
  if (st === 'warn') return `滞后（${fmtAgeSec(ageSec)}）`;
  return `停更（${fmtAgeSec(ageSec)}）`;
};

const money = (v: number | null | undefined, unit = '¥'): string =>
  v == null || !Number.isFinite(v)
    ? '—'
    : `${unit}${v.toLocaleString('en-US', { maximumFractionDigits: 0 })}`;

/** 后端 /api/tdx/config 裸响应（不返回 token 明文） */
export interface TdxConfigLite {
  enabled?: boolean;
  bridge_url?: string;
  bridge_token_configured?: boolean;
  real_trading_enabled?: boolean;
  health?: { status?: string; error?: string; tdx_connected?: boolean } | null;
}

/** 通达信桥：QuantMind 实盘执行通道（下单 + 成交回报都在这里） */
export function tdxChannel(
  cfg: TdxConfigLite | null,
  summary?: RealAccountSummary,
): ChannelProbe {
  const health = cfg?.health ?? null;
  const err = health?.error;
  const bridgeOk = !!health && !err && (health.status === 'ok' || health.tdx_connected === true);
  const clientOk = health?.tdx_connected === true;
  const accountAge = summary?.age_sec ?? null;

  let state: ChanState = 'unknown';
  let stateText = '未知';
  if (!cfg || !cfg.enabled) {
    state = 'off';
    stateText = '未配置';
  } else if (err) {
    state = 'off';
    stateText = '离线';
  } else if (!clientOk) {
    state = 'warn';
    stateText = '桥在线 · 客户端未登录';
  } else if (freshnessState(accountAge) === 'ok') {
    state = 'ok';
    stateText = '在线';
  } else {
    // 桥活着但账户快照不动 —— 这正是 09-10 的真实故障态，必须显式说
    state = 'warn';
    stateText = '桥在线 · 账户停更';
  }

  const lines: ChanLine[] = [
    { k: '桥服务', v: err ? `离线（${err}）` : bridgeOk ? '在线' : '未知', tone: err ? 'off' : bridgeOk ? 'ok' : 'unknown' },
    {
      k: '通达信客户端',
      v: clientOk ? '已登录' : cfg?.enabled ? '未连接' : '—',
      tone: clientOk ? 'ok' : cfg?.enabled ? 'warn' : 'unknown',
    },
    {
      k: '下单权限',
      v: cfg?.real_trading_enabled ? '允许（系统闸门生效）' : '禁用',
      tone: cfg?.real_trading_enabled ? 'ok' : 'warn',
    },
    {
      k: '账户快照',
      v: `${freshnessText(accountAge)}${summary?.position_count != null ? ` · ${summary.position_count} 只` : ''}`,
      tone: freshnessState(accountAge),
    },
    {
      k: '账户资产',
      v: summary?.total_asset != null ? money(summary.total_asset) : '—',
    },
  ];
  if (cfg?.bridge_url) lines.push({ k: '桥地址', v: cfg.bridge_url });
  return { key: 'tdx', label: '通达信桥', role: '实盘执行通道（决策下单 + 成交回报）', state, stateText, lines };
}

/** 后端 /api/qmt/status 返回的桥自述（只读 RPC ping，不含任何下单动作） */
export interface QmtStatusLite {
  /** Windows 侧总闸 rpc_allow_order_methods：true 表示那边已放开真实下单 */
  allow_order_methods?: boolean;
  /** 本侧总闸（config/qmt_bridge.json 的 allow_trading）：默认 false，即本系统只读 */
  allow_trading?: boolean;
  version?: string;
  account_type?: string;
  server_time?: string;
}

/** 本侧下单总闸 → 一行文案。true 意味着这台机器的脚本真能发单出去，tone 用 warn
 *  在视觉上与只读分开；字段缺失（后端旧版）不猜成「已开」也不猜成「已关」。 */
const qmtLocalOrderLine = (status?: QmtStatusLite | null): ChanLine => {
  if (status?.allow_trading === true) return { k: '本系统接线', v: '已接线 · 总闸开启', tone: 'warn' };
  if (status?.allow_trading === false) return { k: '本系统接线', v: '已接线 · 总闸关闭', tone: 'ok' };
  return { k: '本系统接线', v: '未知', tone: 'unknown' };
};

/** 下单权限两行：必须把「那边放没放开」和「本系统接没接线」分开说。
 *  2026-09-11 实录：Windows 侧 rpc_allow_order_methods 已被打开（passorder 可用），
 *  界面若仍只写「未接入（只读）」就是把自家未接线说成对方不支持——排查会走错方向。 */
const qmtOrderLines = (status?: QmtStatusLite | null): ChanLine[] => [
  {
    k: 'Windows 侧下单闸',
    v:
      status?.allow_order_methods === true
        ? '已放开'
        : status?.allow_order_methods === false
          ? '未放开'
          : '未知',
    tone: status?.allow_order_methods === true ? 'warn' : 'unknown',
  },
  qmtLocalOrderLine(status),
];

/** 迅投 QMT：账户读取通道（与桥互为独立佐证；下单已接线，本侧默认关闭） */
export function qmtChannel(
  summary: RealAccountSummary | undefined,
  probe: { ok: boolean; error?: string } | null,
  status?: QmtStatusLite | null,
): ChannelProbe {
  const age = summary?.age_sec ?? null;
  const fresh = freshnessState(age);
  // 标题跟随本侧总闸：脚本真能发单时不能还挂「只读」二字
  const mode = status?.allow_trading === true ? '可下单' : '只读';
  let state: ChanState;
  let stateText: string;
  if (!probe || summary == null) {
    state = 'unknown';
    stateText = '未知';
  } else if (!probe.ok) {
    state = 'off';
    stateText = '离线';
  } else if (fresh === 'ok') {
    state = 'ok';
    stateText = `在线（${mode}）`;
  } else {
    state = 'warn';
    stateText = `同步滞后（${mode}）`;
  }
  return {
    key: 'qmt',
    label: '迅投 QMT',
    role: '账户读取 + 下单（与桥互为独立佐证；本侧下单总闸默认关闭）',
    state,
    stateText,
    lines: [
      { k: '桥服务', v: probe ? (probe.ok ? '在线' : `离线（${probe.error ?? '—'}）`) : '未知', tone: probe ? (probe.ok ? 'ok' : 'off') : 'unknown' },
      ...qmtOrderLines(status),
      {
        k: '账户快照',
        v: `${freshnessText(age)}${summary?.position_count != null ? ` · ${summary.position_count} 只` : ''}`,
        tone: fresh,
      },
      { k: '账户资产', v: summary?.total_asset != null ? money(summary.total_asset) : '—' },
      { k: '账户号', v: summary?.account_id ?? '—' },
      ...(status?.version ? [{ k: '桥版本', v: status.version }] : []),
      ...(status?.account_type ? [{ k: '账号类型', v: status.account_type }] : []),
    ],
  };
}

/** 港股：富途 OpenD（行情 + 交易，双环境 REAL/SIMULATE） */
export function futuChannel(
  both: { real: LiveAccount | null; simulate: LiveAccount | null } | null | undefined,
): ChannelProbe {
  const real = both?.real ?? null;
  const sim = both?.simulate ?? null;
  const ok = !!real || !!sim;
  return {
    key: 'futu',
    label: '富途 OpenD',
    role: '港股执行通道（实盘 + 模拟双环境）',
    state: ok ? 'ok' : 'off',
    stateText: ok ? '在线' : '离线',
    lines: [
      { k: '实盘环境', v: real ? money(real.asset, 'HK$') : '未读通', tone: real ? 'ok' : 'off' },
      { k: '模拟环境', v: sim ? money(sim.asset, 'HK$') : '未读通', tone: sim ? 'ok' : 'off' },
      { k: '通道', v: real?.channel_used ?? sim?.channel_used ?? 'futu' },
    ],
  };
}

/** 美股：盈透证券 IB Gateway（通道已下线：未随平台迁移；保留占位展示） */
export function ibkrChannel(acct: { total_asset: number } | null): ChannelProbe {
  const ok = !!acct;
  return {
    key: 'ibkr',
    label: 'IBKR Gateway',
    role: '美股执行通道（通道已下线：未随平台迁移）',
    state: ok ? 'ok' : 'off',
    stateText: ok ? '在线' : '已下线',
    lines: [
      { k: '总资产', v: ok ? money(acct.total_asset, '$') : '通道已下线', tone: ok ? 'ok' : 'off' },
      { k: '接入方式', v: '已下线（未迁移）' },
    ],
  };
}

/** 按市场取该市场的通道清单（A 股两条并列，供「选哪个通道」用） */
export function channelsOfMarket(
  market: 'cn' | 'hk' | 'us',
  input: {
    tdx: TdxConfigLite | null;
    qmtProbe: { ok: boolean; error?: string } | null;
    qmtStatus?: QmtStatusLite | null;
    summaries: RealAccountSummary[] | null;
    futuBoth?: { real: LiveAccount | null; simulate: LiveAccount | null } | null;
    ibkr?: { total_asset: number } | null;
  },
): ChannelProbe[] {
  if (market === 'cn') {
    const find = (k: string) => input.summaries?.find((s) => s.account === k);
    return [
      tdxChannel(input.tdx, find('tdx')),
      qmtChannel(find('qmt'), input.qmtProbe, input.qmtStatus),
    ];
  }
  if (market === 'hk') return [futuChannel(input.futuBoth)];
  return [ibkrChannel(input.ibkr ?? null)];
}
