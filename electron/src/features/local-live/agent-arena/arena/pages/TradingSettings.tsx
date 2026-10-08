import { useCallback, useEffect, useState } from 'react';
import { BrokerMarketInfo, fetchBrokerMarket, saveBrokerMarket } from '../api/client';
import { api } from '../api/client';
import { usePolling } from '../hooks/usePolling';
import './TradingSettings.css';
import { asUpdater } from '../reactCompat';

/** 交易所设置 —— 通达信交易桥 / 券商接入（QuantMind trade 服务 /api/v1/tdx/* 服务层）。
 *  桥连接/总览/实盘状态/券商配置走 QuantMind trade 服务直连；
 *  滚动买卖与止损止盈仍经 /api/quantmind 代理（quantmind 推理引擎的控制器）。
 *  embedded=true 时作为总控页 tab 嵌入，隐藏自身页头（标题由外层提供）。 */

// ---------- 数据类型（与 quantmind trade-core 对齐） ----------

interface TdxConfig {
  enabled: boolean;
  bridge_url: string;
  bridge_token_configured: boolean;
  real_trading_enabled: boolean;
  broker_type: string;
  health: { error?: string; tdx_connected?: boolean } | null;
}

interface TdxOverview {
  available: boolean;
  error?: string;
  bridge?: {
    hostname: string | null;
    local_ips: string[];
    bridge_url: string;
    port: number | null;
    tdx_connected: boolean;
    server_time: string | null;
    token_configured: boolean;
  };
  account?: {
    currency: string | null;
    balance: number | null;
    cash: number | null;
    asset: number | null;
    market_value: number | null;
    position_count: number;
  };
  positions?: { symbol?: string; stock_code?: string; code?: string }[];
  orders?: unknown[];
  cache?: { kline: number; market_snapshot: number };
  security?: { active_ips: number; banned_ips: number };
}

interface RollingConfig {
  score_threshold: number;
  fixed_buy_amount: number;
  execute_mode: 'off' | 'tdx' | 'paper';
}

interface SltpConfig {
  stop_loss_pct: number | null;
  take_profit_pct: number | null;
  trailing_stop_pct: number | null;
  enabled: boolean;
}

interface BrokerConfig {
  success: boolean;
  broker: string;
  label: string;
  fields: Record<string, string | boolean>;
}

interface RealTradingStatus {
  status: string;
  mode: string;
  strategy?: { id?: string; name?: string };
  execution_config?: { max_buy_drop?: number; stop_loss?: number };
}

type ExecuteMode = 'off' | 'tdx' | 'paper';

const EXECUTE_MODES: { value: ExecuteMode; label: string; hint: string }[] = [
  { value: 'off', label: '仅预警', hint: '只推通达信预警，不下单' },
  { value: 'tdx', label: '通达信下单', hint: 'TQ收费账号直接提交免确认，普通账号需客户端确认' },
  { value: 'paper', label: '模拟盘直接下单', hint: '本地模拟盘自动成交，免确认、零风险' },
];

const BROKER_META: Record<
  string,
  { name: string; markets: string; desc: string; fields: Record<string, { label: string; sensitive?: boolean }> }
> = {
  qmt: {
    name: '迅投 QMT',
    markets: 'A股 · 模拟账户',
    desc: '经 Windows 大 QMT + Redis 桥只读接入（与 quantmind 共用本机 Redis 6379/db5）；'
      + '当前阶段一：可查账户/持仓/委托/成交，下单未接线。',
    fields: {
      account_id: { label: '资金账号' },
      account_type: { label: '账号类型 STOCK / CREDIT' },
      redis_host: { label: '桥 Redis 主机' },
      redis_port: { label: 'Redis 端口' },
      redis_db: { label: 'Redis DB' },
      redis_password: { label: 'Redis 密码', sensitive: true },
    },
  },
  futu: {
    name: '富途证券',
    markets: '港股 · 美股',
    desc: '需安装 FutuOpenD 网关并保持登录；交易密码 MD5 只写不回显。',
    fields: {
      opend_host: { label: 'OpenD 主机' },
      opend_port: { label: 'OpenD 端口' },
      trade_pwd_md5: { label: '交易密码 MD5', sensitive: true },
      trade_env: { label: '交易环境' },
    },
  },
  tiger: {
    name: '老虎证券',
    markets: '港股 · 美股',
    desc: '免网关直连老虎 API；支持 SIM 模拟账户（TigerSIM）。',
    fields: {
      tiger_id: { label: 'Tiger ID' },
      rsa_private_key: { label: 'RSA 私钥', sensitive: true },
      account: { label: '账户' },
    },
  },
  ib: {
    name: '盈透证券 IB',
    markets: '全球',
    desc: '需 IB Gateway：paper 账户用 4002 端口，实盘用 4001 端口。',
    fields: {
      gateway_host: { label: 'Gateway 主机' },
      gateway_port: { label: 'Gateway 端口' },
      client_id: { label: 'Client ID' },
    },
  },
};

const fmtMoney = (v: number | null | undefined, suffix = '') =>
  v == null ? '-' : v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + suffix;

// ---------- 通用请求辅助 ----------

async function getJson<T>(path: string): Promise<T> {
  const res = await api.get(path);
  return res.data as T;
}

async function postJson(path: string, body?: unknown): Promise<{ ok: boolean; data: unknown; error?: string }> {
  try {
    const res = await api.post(path, body ?? {});
    return { ok: true, data: res.data };
  } catch (e) {
    return {
      ok: false,
      data: null,
      error: (e as { response?: { data?: { error?: string; detail?: string } } }).response?.data?.error
        ?? (e as { response?: { data?: { error?: string; detail?: string } } }).response?.data?.detail
        ?? String(e),
    };
  }
}

async function putJson(path: string, body: unknown): Promise<{ ok: boolean; data: unknown; error?: string }> {
  try {
    const res = await api.put(path, body);
    return { ok: true, data: res.data };
  } catch (e) {
    return {
      ok: false,
      data: null,
      error: (e as { response?: { data?: { error?: string; detail?: string } } }).response?.data?.error
        ?? (e as { response?: { data?: { error?: string; detail?: string } } }).response?.data?.detail
        ?? String(e),
    };
  }
}

// ============================================================

export default function TradingSettings({ embedded = false }: { embedded?: boolean }) {
  // ---- 通达信桥 ----
  const tdx = usePolling(() => getJson<TdxConfig>('/tdx/config'), [], 30000);
  const overview = usePolling(() => getJson<TdxOverview>('/tdx/overview'), [], 8000);
  const [status, setStatus] = useState<RealTradingStatus | null>(null);
  const [brokerCfgs, setBrokerCfgs] = useState<Record<string, BrokerConfig>>({});

  // 表单状态
  const [newUrl, setNewUrl] = useState('');
  const [newToken, setNewToken] = useState('');
  const [tdxMsg, setTdxMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const [threshold, setThreshold] = useState('2.2');
  const [amount, setAmount] = useState('10000');
  const [execMode, setExecMode] = useState<ExecuteMode>('off');
  const [rollingDate, setRollingDate] = useState('');
  const [rollingMsg, setRollingMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [rollingBusy, setRollingBusy] = useState(false);

  const [slStopLoss, setSlStopLoss] = useState('8');
  const [slTakeProfit, setSlTakeProfit] = useState('');
  const [slTrailing, setSlTrailing] = useState('');
  const [slEnabled, setSlEnabled] = useState(true);
  const [slMsg, setSlMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const [rollingResult, setRollingResult] = useState<{ ok: boolean; buys?: unknown[]; sells?: unknown[]; placed?: unknown[]; failed?: unknown[]; error?: string } | null>(null);
  const [pushResult, setPushResult] = useState<{ ok: boolean; pushed?: number; error?: string } | null>(null);
  const [busy, setBusy] = useState(false);

  // 初始加载 + 拉取配置
  useEffect(() => {
    void getJson<RollingConfig>('/quantmind/tdx/rolling-config')
      .then((c) => {
        setThreshold(String(c.score_threshold ?? '2.2'));
        setAmount(String(c.fixed_buy_amount ?? '10000'));
        setExecMode(c.execute_mode ?? 'off');
      })
      .catch(() => {});

    void getJson<SltpConfig>('/quantmind/tdx/sltp-config')
      .then((c) => {
        if (c.stop_loss_pct != null) setSlStopLoss(String(c.stop_loss_pct * 100));
        if (c.take_profit_pct != null) setSlTakeProfit(String(c.take_profit_pct * 100));
        if (c.trailing_stop_pct != null) setSlTrailing(String(c.trailing_stop_pct * 100));
        setSlEnabled(c.enabled);
      })
      .catch(() => {});

    void getJson<RealTradingStatus>('/real-trading/status')
      .then(setStatus)
      .catch(() => {});

    for (const broker of ['qmt', 'tiger', 'futu', 'ib']) {
      void getJson<BrokerConfig>(`/broker-config/${broker}`)
        .then((b) => setBrokerCfgs(asUpdater((prev) => ({ ...prev, [broker]: b }))))
        .catch(() => {});
    }
  }, []);

  // ---- 动作：保存桥配置 ----
  const saveBridge = useCallback(async () => {
    if (!newUrl.trim() && !newToken.trim()) return;
    setTdxMsg(null);
    const r = await postJson('/tdx/config', {
      bridge_url: newUrl.trim() || undefined,
      bridge_token: newToken.trim() || undefined,
    });
    setTdxMsg(r.ok
      ? { ok: true, text: '✅ 已保存，推送与实盘链路即时生效' }
      : { ok: false, text: `❌ 保存失败: ${r.error}` });
    if (r.ok) {
      setNewUrl('');
      setNewToken('');
      void tdx.refresh();
    }
  }, [newUrl, newToken, tdx.refresh]);

  // ---- 动作：保存滚动买卖配置 ----
  const saveRolling = useCallback(async () => {
    const t = parseFloat(threshold);
    const a = parseFloat(amount);
    if (!Number.isFinite(t) || t <= 0 || t > 10) {
      setRollingMsg({ ok: false, text: '❌ 阈值需在 0-10 之间' });
      return;
    }
    if (!Number.isFinite(a) || a <= 0) {
      setRollingMsg({ ok: false, text: '❌ 每只金额需大于 0' });
      return;
    }
    setRollingMsg(null);
    const r = await putJson('/quantmind/tdx/rolling-config', {
      score_threshold: t,
      fixed_buy_amount: a,
      execute_mode: execMode,
    });
    setRollingMsg(r.ok
      ? { ok: true, text: '✅ 已保存，推理自动推送即时生效' }
      : { ok: false, text: `❌ 保存失败: ${r.error}` });
    if (r.ok) {
      void getJson<RollingConfig>('/quantmind/tdx/rolling-config')
        .then((c) => {
          setThreshold(String(c.score_threshold ?? threshold));
          setAmount(String(c.fixed_buy_amount ?? amount));
          setExecMode(c.execute_mode ?? execMode);
        })
        .catch(() => {});
    }
  }, [threshold, amount, execMode]);

  // ---- 动作：保存止损止盈 ----
  const saveSltp = useCallback(async () => {
    const parsePct = (v: string): number | null => {
      const n = parseFloat(v);
      return Number.isFinite(n) && n > 0 ? n / 100 : null;
    };
    const stop = parsePct(slStopLoss);
    const take = parsePct(slTakeProfit);
    const trail = parsePct(slTrailing);
    if (!stop && !take && !trail) {
      setSlMsg({ ok: false, text: '❌ 至少填一个幅度' });
      return;
    }
    setSlMsg(null);
    const r = await putJson('/quantmind/tdx/sltp-config', {
      stop_loss_pct: stop ?? 0,
      take_profit_pct: take,
      trailing_stop_pct: trail,
      enabled: slEnabled,
    });
    setSlMsg(r.ok
      ? { ok: true, text: '✅ 已保存提醒配置' }
      : { ok: false, text: `❌ 保存失败: ${r.error}` });
    if (r.ok) {
      void getJson<SltpConfig>('/quantmind/tdx/sltp-config')
        .then((c) => {
          if (c.stop_loss_pct != null) setSlStopLoss(String(c.stop_loss_pct * 100));
          if (c.take_profit_pct != null) setSlTakeProfit(String(c.take_profit_pct * 100));
          if (c.trailing_stop_pct != null) setSlTrailing(String(c.trailing_stop_pct * 100));
          setSlEnabled(c.enabled);
        })
        .catch(() => {});
    }
  }, [slStopLoss, slTakeProfit, slTrailing, slEnabled]);

  // ---- 动作：推送今日选股 ----
  const pushSignals = useCallback(async () => {
    setBusy(true);
    setPushResult(null);
    const r = await postJson('/quantmind/tdx/push-signals', {});
    setBusy(false);
    if (!r.ok || !r.data || typeof r.data !== 'object' || !('success' in r.data)) {
      setPushResult({ ok: false, error: (r.data as { error?: string })?.error ?? r.error ?? '推送失败' });
      return;
    }
    const d = r.data as { success: boolean; pushed?: number; run_id?: string; error?: string };
    setPushResult(d.success
      ? { ok: true, pushed: d.pushed ?? 0 }
      : { ok: false, error: d.error ?? '推送失败' });
  }, []);

  // ---- 动作：滚动买卖检查 ----
  const runRolling = useCallback(async () => {
    setRollingBusy(true);
    setRollingResult(null);
    const r = await postJson('/quantmind/tdx/rolling-signals', rollingDate.trim() ? { trade_date: rollingDate.trim() } : {});
    setRollingBusy(false);
    if (!r.ok || !r.data || typeof r.data !== 'object') {
      setRollingResult({ ok: false, error: r.error ?? '检查失败' });
      return;
    }
    const d = r.data as { success?: boolean; error?: string; buys?: unknown[]; sells?: unknown[]; placed_orders?: unknown[]; failed_orders?: unknown[] };
    setRollingResult(d.success === false
      ? { ok: false, error: d.error ?? '检查失败' }
      : { ok: true, buys: d.buys, sells: d.sells, placed: d.placed_orders, failed: d.failed_orders });
  }, [rollingDate]);

  // ---- 动作：保存券商配置 ----
  const saveBroker = useCallback(async (broker: string, values: Record<string, string>) => {
    // 后端 BrokerConfigUpdate 期望 { values: {...} } 包裹；裸传 values 会触发 422
    const r = await putJson(`/broker-config/${broker}`, { values });
    return r.ok ? null : (r.error ?? '保存失败');
  }, []);

  // ---- 动作：测试券商连接（成功返回账户摘要文本，失败返回原因） ----
  const testBroker = useCallback(async (broker: string): Promise<{ ok: boolean; text: string }> => {
    const r = await postJson(`/broker-config/${broker}/test`, {});
    if (!r.ok || !r.data || typeof r.data !== 'object') {
      return { ok: false, text: `测试失败: ${r.error ?? ''}` };
    }
    const d = r.data as { success?: boolean; ok?: boolean; error?: string; message?: string; detail?: string };
    if (d.success === false || d.ok === false) {
      return { ok: false, text: d.error ?? d.detail ?? d.message ?? '连接失败' };
    }
    return { ok: true, text: d.message ?? '连接正常' };
  }, []);

  const cfg = tdx.data;

  return (
    <div className="ts">
      {!embedded && (
        <div className="ts-header">
          <div>
            <h1 className="ts-title">交易所设置</h1>
            <div className="ts-sub">通达信交易桥（A股）· 券商实盘接入（港股/美股）· 实时交易状态</div>
          </div>
          <span className="ts-refresh-note">
            局域网桥信息每 8s 自动刷新 · 配置经服务器保存（敏感字段只写不回显）
          </span>
        </div>
      )}

      {/* ==================== 通达信交易桥 ==================== */}
      <section className="ts-card">
        <div className="ts-card-head">
          <div>
            <div className="ts-card-title">通达信交易桥</div>
            <div className="ts-card-desc">通过桥连接 Windows 通达信客户端：推送选股 / 下单 / 拉取账户状态</div>
          </div>
          <button className="ts-btn" onClick={() => void tdx.refresh()} disabled={tdx.loading}>
            {tdx.loading ? '刷新中…' : '刷新'}
          </button>
        </div>

        {tdx.error && (
          <div className="ts-health err">桥配置加载失败: {tdx.error}</div>
        )}

        {cfg && (
          <>
            {/* 状态徽章 */}
            <div className="ts-badges">
              <span className={`ts-badge ${cfg.enabled ? 'on' : 'off'}`}>
                <span className={`ts-dot ${cfg.enabled ? 'on' : 'off'}`} /> 自动推送: {cfg.enabled ? '开启' : '关闭'}
              </span>
              <span className={`ts-badge ${cfg.real_trading_enabled ? 'on' : 'off'}`}>
                <span className={`ts-dot ${cfg.real_trading_enabled ? 'on' : 'off'}`} /> 实盘: {cfg.real_trading_enabled ? '开启' : '关闭'}
              </span>
              <span className="ts-badge">桥: {cfg.bridge_url || '-'}</span>
              <span className={`ts-badge ${cfg.bridge_token_configured ? 'on' : 'warn'}`}>
                Token: {cfg.bridge_token_configured ? '已配置' : '未配置'}
              </span>
            </div>

            {/* 桥可达性 */}
            {cfg.health?.error ? (
              <div className="ts-health err">桥不可达: {cfg.health.error}</div>
            ) : (
              <div className="ts-health ok">
                桥在线 · 通达信客户端: {cfg.health?.tdx_connected ? '已连接' : '未登录(17709)'}
              </div>
            )}

            {/* 桥地址 / Token（可编辑，保存后即时生效） */}
            <div className="ts-cfg" style={{ borderBottom: '2px solid #000' }}>
              <div className="ts-cfg-title">桥地址与 Token（留空保持现有值）</div>
              <div className="ts-cfg-fields">
                <label className="ts-field" style={{ flex: 1, minWidth: 240 }}>
                  <span className="ts-field-label">桥地址</span>
                  <input className="ts-input wide" type="text" placeholder="http://<tdx-bridge-ip>:8550"
                    value={newUrl} onChange={(e) => setNewUrl(e.target.value)} />
                </label>
                <label className="ts-field" style={{ flex: 1, minWidth: 240 }}>
                  <span className="ts-field-label">桥 Token（64位 hex，与 Windows 侧一致）</span>
                  <input className="ts-input wide" type="text" placeholder="输入新 token（留空则保持现有）"
                    value={newToken} onChange={(e) => setNewToken(e.target.value)} />
                </label>
                <button className="ts-btn dark" onClick={() => void saveBridge()} disabled={!newUrl.trim() && !newToken.trim()}>
                  保存配置
                </button>
              </div>
              {tdxMsg && <div className={`ts-msg ${tdxMsg.ok ? 'ok' : 'err'}`}>{tdxMsg.text}</div>}
            </div>

            {overview.data?.available && (
              <>
                {/* 操作行 */}
                <div className="ts-op-row">
                  <span className="ts-op-label">
                    模型推理选股 → 通达信板块 / 预警
                    <span className="dim">
                      {' '}· 同步于 {overview.data.bridge?.server_time ? String(overview.data.bridge.server_time).slice(11, 19) : '实时'}
                    </span>
                  </span>
                  <div className="ts-op-btns">
                    <button className="ts-btn dark" onClick={() => void pushSignals()} disabled={busy || !overview.data.bridge?.tdx_connected}>
                      {busy ? '推送中…' : '推送今日选股'}
                    </button>
                    <button className="ts-btn dark" onClick={() => void runRolling()} disabled={rollingBusy || !overview.data.bridge?.tdx_connected}>
                      {rollingBusy ? '检查中…' : rollingDate ? `推 ${rollingDate} 分数` : '滚动买卖检查'}
                    </button>
                  </div>
                </div>

                {/* 滚动买卖配置 */}
                <div className="ts-cfg">
                  <div className="ts-cfg-title">滚动买卖配置（quantmind 推理引擎 · 阈值与金额可自己改）</div>
                  <div className="ts-cfg-fields">
                    <label className="ts-field">
                      <span className="ts-field-label">买入分数阈值（{'>'}此分买入，低于则卖出）</span>
                      <input className="ts-input" type="number" step="0.1" min="0" max="10"
                        value={threshold} onChange={(e) => setThreshold(e.target.value)} />
                    </label>
                    <label className="ts-field">
                      <span className="ts-field-label">每只固定买入金额（元）</span>
                      <input className="ts-input" type="number" step="500" min="1000"
                        value={amount} onChange={(e) => setAmount(e.target.value)} />
                    </label>
                    <label className="ts-field">
                      <span className="ts-field-label">推历史日期（YYYY-MM-DD，留空=最新）</span>
                      <input className="ts-input date" type="date"
                        value={rollingDate} onChange={(e) => setRollingDate(e.target.value)} />
                    </label>
                    <button className="ts-btn dark" onClick={() => void saveRolling()}>保存配置</button>
                  </div>
                  <div className="ts-hint">执行模式（直接下单为付费会员专属）</div>
                  <div className="ts-modes">
                    {EXECUTE_MODES.map((m) => (
                      <button key={m.value} title={m.hint}
                        className={`ts-mode ${execMode === m.value ? `active ${m.value}` : ''}`}
                        onClick={() => setExecMode(m.value)}>
                        {m.label}
                      </button>
                    ))}
                  </div>
                  <div className="ts-hint">
                    卖单市价、买单收盘价限价；先卖后买。规则：分数 &gt; 阈值 → 买入；持仓分数 ≤ 阈值 → 卖出；大盘低于 MA20 → 只卖不买。推历史日期时跳过当日大盘过滤。执行模式选「通达信下单」或「模拟盘直接下单」后，信号直接生成委托。
                  </div>
                  {rollingMsg && (
                    <div className={`ts-msg ${rollingMsg.ok ? 'ok' : 'err'}`}>{rollingMsg.text}</div>
                  )}
                </div>

                {/* 止损止盈实时提醒 */}
                <div className="ts-cfg">
                  <div className="ts-cfg-title">止损止盈实时提醒（quantmind 引擎 · 仅持仓股 · 现价触发即推送，不下自动单）</div>
                  <div className="ts-cfg-fields">
                    <label className="ts-field">
                      <span className="ts-field-label">止损幅度 %（现价 ≤ 成本×(1-x%)）</span>
                      <input className="ts-input" type="number" step="1" min="0" max="50"
                        value={slStopLoss} onChange={(e) => setSlStopLoss(e.target.value)} />
                    </label>
                    <label className="ts-field">
                      <span className="ts-field-label">止盈幅度 %（留空不启用）</span>
                      <input className="ts-input" type="number" step="1" min="0" max="100" placeholder="如 10"
                        value={slTakeProfit} onChange={(e) => setSlTakeProfit(e.target.value)} />
                    </label>
                    <label className="ts-field">
                      <span className="ts-field-label">移动止损 %（离持仓最高价回撤）</span>
                      <input className="ts-input" type="number" step="1" min="0" max="50" placeholder="如 5"
                        value={slTrailing} onChange={(e) => setSlTrailing(e.target.value)} />
                    </label>
                    <button className="ts-btn rose" onClick={() => void saveSltp()}>保存提醒</button>
                  </div>
                  <label className="ts-toggle-row">
                    <input type="checkbox" className="ts-checkbox" checked={slEnabled}
                      onChange={(e) => setSlEnabled(e.target.checked)} />
                    启用实时止损止盈提醒
                    <span className="dim">（触发时站内通知 + 通达信预警弹窗；通达信守护进程负责真实止损单）</span>
                  </label>
                  {slMsg && <div className={`ts-msg ${slMsg.ok ? 'ok' : 'err'}`}>{slMsg.text}</div>}
                  <div className="ts-hint">
                    行情链路：通达信实时快照 → 行情 Feed → Redis → WebSocket；每 3s 校验一次持仓股现价，触发后 5 分钟内同股不重复提醒。
                  </div>
                </div>

                {/* 推送结果 */}
                {pushResult && (
                  <div className={`ts-result ${pushResult.ok ? 'ok' : 'err'}`}>
                    {pushResult.ok
                      ? `✅ 已推送今日选股 ${pushResult.pushed} 只到通达信板块`
                      : `❌ 推送失败: ${pushResult.error}`}
                  </div>
                )}

                {/* 滚动检查结果 */}
                {rollingResult && (
                  <div className={`ts-result ${rollingResult.ok ? 'ok' : 'err'}`}>
                    {rollingResult.ok ? (
                      <>
                        <div className="row">✅ 滚动买卖检查完成</div>
                        {rollingResult.buys && rollingResult.buys.length > 0 && (
                          <div className="row">
                            <b>买入预警 {rollingResult.buys.length} 只</b>
                            <div className="chips">
                              {(rollingResult.buys as { symbol?: string; name?: string; score?: number; volume?: number }[]).map((b, i) => (
                                <span key={i} className="ts-chip">{b.symbol} {b.name} <b>{b.score}</b> {b.volume}股</span>
                              ))}
                            </div>
                          </div>
                        )}
                        {rollingResult.sells && rollingResult.sells.length > 0 && (
                          <div className="row">
                            <b>卖出预警 {rollingResult.sells.length} 只</b>
                            <div className="chips">
                              {(rollingResult.sells as { symbol?: string; name?: string; score?: number }[]).map((s, i) => (
                                <span key={i} className="ts-chip">{s.symbol} {s.name} {s.score ?? '无分数'}</span>
                              ))}
                            </div>
                          </div>
                        )}
                        {!rollingResult.buys?.length && !rollingResult.sells?.length && (
                          <div className="row">无买卖动作（持仓均 &gt; 阈值且无新增候选）</div>
                        )}
                        {rollingResult.placed && rollingResult.placed.length > 0 && (
                          <div className="row">
                            <b>已生成委托 {rollingResult.placed.length} 笔</b>
                            <div className="chips">
                              {(rollingResult.placed as { side?: string; symbol?: string; volume?: number; status?: string }[]).map((o, i) => (
                                <span key={i} className="ts-chip">{o.side === 'buy' ? '买' : '卖'} {o.symbol} {o.volume}股 · {o.status}</span>
                              ))}
                            </div>
                          </div>
                        )}
                        {rollingResult.failed && rollingResult.failed.length > 0 && (
                          <div className="row">
                            <b className="err">下单失败 {rollingResult.failed.length} 笔</b>
                            <div className="chips">
                              {(rollingResult.failed as { side?: string; symbol?: string; error?: string }[]).map((f, i) => (
                                <span key={i} className="ts-chip" title={f.error}>{f.side === 'buy' ? '买' : '卖'} {f.symbol}: {f.error}</span>
                              ))}
                            </div>
                          </div>
                        )}
                      </>
                    ) : (
                      <>滚动检查失败: {rollingResult.error}</>
                    )}
                  </div>
                )}

                {/* 桥主机状态网格 */}
                <div className="ts-grid">
                  <div className="ts-grid-cell">
                    <div className="ts-cell-label">桥主机</div>
                    <div className="ts-cell-val">{overview.data.bridge?.hostname || '-'}</div>
                    <div className="ts-cell-sub">{(overview.data.bridge?.local_ips ?? []).join(' / ')}</div>
                  </div>
                  <div className="ts-grid-cell">
                    <div className="ts-cell-label">通达信连接</div>
                    <div className="ts-cell-val">
                      <span className={`ts-dot ${overview.data.bridge?.tdx_connected ? 'on' : 'off'}`} style={{ marginRight: 6 }} />
                      {overview.data.bridge?.tdx_connected ? '已连接' : '未连接'}
                    </div>
                    <div className="ts-cell-sub">端口 {overview.data.bridge?.port ?? '-'}</div>
                  </div>
                  <div className="ts-grid-cell">
                    <div className="ts-cell-label">账户资产</div>
                    <div className="ts-cell-val" style={{ color: '#ef4444' }}>{fmtMoney(overview.data.account?.asset)}</div>
                    <div className="ts-cell-sub">可用 <b>{fmtMoney(overview.data.account?.cash)}</b></div>
                  </div>
                  <div className="ts-grid-cell">
                    <div className="ts-cell-label">持仓 / 市值</div>
                    <div className="ts-cell-val">{overview.data.account?.position_count ?? 0} 只</div>
                    <div className="ts-cell-sub">市值 <b>{fmtMoney(overview.data.account?.market_value)}</b></div>
                  </div>
                  <div className="ts-grid-cell">
                    <div className="ts-cell-label">当日委托 / 持仓明细</div>
                    <div className="ts-cell-val">
                      委托 {overview.data.orders?.length ?? 0} · 持仓 {overview.data.positions?.length ?? 0}
                    </div>
                    <div className="ts-cell-sub">
                      {(overview.data.positions ?? []).map((p) => p.symbol ?? p.stock_code ?? p.code).filter(Boolean).slice(0, 3).join(', ') || '无持仓'}
                    </div>
                  </div>
                  <div className="ts-grid-cell">
                    <div className="ts-cell-label">缓存 / 安全</div>
                    <div className="ts-cell-val" style={{ fontSize: 11 }}>
                      K线 {overview.data.cache?.kline ?? 0} · 快照 {overview.data.cache?.market_snapshot ?? 0}
                    </div>
                    <div className="ts-cell-sub">
                      活跃IP <b>{overview.data.security?.active_ips ?? 0}</b>
                      {overview.data.security?.banned_ips ? <> · 封禁 <b>{overview.data.security.banned_ips}</b></> : null}
                    </div>
                  </div>
                </div>
              </>
            )}

            {overview.data && !overview.data.available && (
              <div className="ts-health warn">局域网桥信息暂不可用: {overview.data.error || '未知原因'}</div>
            )}
          </>
        )}
      </section>

      {/* ==================== 券商接入 ==================== */}
      <section className="ts-card">
        <div className="ts-card-head">
          <div>
            <div className="ts-card-title">券商实盘接入</div>
            <div className="ts-card-desc">
              港股: 富途/老虎/IB · 美股: 老虎/IB/富途 · 期货: IB · A股: 通达信桥 / 迅投 QMT（只读）
            </div>
          </div>
        </div>
        <MappingPicker />
        <div className="ts-broker-grid">
          {Object.keys(BROKER_META).map((key) => {
            const meta = BROKER_META[key];
            const cfg = brokerCfgs[key];
            return (
              <BrokerCard key={key} broker={key} meta={meta}
                initial={cfg?.fields}
                onSave={saveBroker}
                onTest={testBroker} />
            );
          })}
        </div>
      </section>

      {/* ==================== 实时交易状态 ==================== */}
      <section className="ts-card">
        <div className="ts-card-head">
          <div>
            <div className="ts-card-title">实时交易状态</div>
            <div className="ts-card-desc">实盘执行（通达信桥 + 模型自主调仓）状态</div>
          </div>
          <button className="ts-btn" onClick={() => void getJson<RealTradingStatus>('/real-trading/status').then(setStatus).catch(() => {})}>
            刷新
          </button>
        </div>
        {status ? (
          <div className="ts-status-grid">
            <div className="ts-grid-cell">
              <div className="ts-cell-label">运行状态</div>
              <div className="ts-cell-val">
                <span className={`ts-dot ${status.status === 'running' ? 'on' : 'off'}`} style={{ marginRight: 6 }} />
                {status.status === 'running' ? '运行中' : status.status === 'not_running' ? '未运行' : status.status}
              </div>
              <div className="ts-cell-sub">模式: {status.mode === 'REAL' ? '实盘' : status.mode === 'SIMULATION' ? '模拟盘' : status.mode}</div>
            </div>
            <div className="ts-grid-cell">
              <div className="ts-cell-label">策略</div>
              <div className="ts-cell-val" style={{ fontSize: 11 }}>{status.strategy?.name || '-'}</div>
              <div className="ts-cell-sub">ID {status.strategy?.id ?? '-'}</div>
            </div>
            <div className="ts-grid-cell">
              <div className="ts-cell-label">止损</div>
              <div className="ts-cell-val" style={{ fontSize: 11 }}>
                {status.execution_config?.stop_loss != null ? `${Math.abs(status.execution_config.stop_loss) * 100}%` : '-'}
              </div>
              <div className="ts-cell-sub">回撤 <b>{status.execution_config?.max_buy_drop != null ? `${Math.abs(status.execution_config.max_buy_drop) * 100}%` : '-'}</b></div>
            </div>
            <div className="ts-grid-cell">
              <div className="ts-cell-label">用户</div>
              <div className="ts-cell-val" style={{ fontSize: 11 }}>{(status as { user_id?: string }).user_id ?? '-'}</div>
              <div className="ts-cell-sub">QuantMind-Trader</div>
            </div>
          </div>
        ) : (
          <div className="ts-hint" style={{ padding: '12px 14px' }}>实时交易状态加载中…</div>
        )}
      </section>
    </div>
  );
}

// ============================================================

/** 券商接入卡：字段编辑 + 保存（敏感字段只写不回显）+ 测试连接 */
function BrokerCard({
  broker,
  meta,
  initial,
  onSave,
  onTest,
}: {
  broker: string;
  meta: { name: string; markets: string; desc: string; fields: Record<string, { label: string; sensitive?: boolean }> };
  initial?: Record<string, string | boolean> | null;
  onSave: (broker: string, values: Record<string, string>) => Promise<string | null>;
  onTest: (broker: string) => Promise<{ ok: boolean; text: string }>;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  // 初始字段回填（敏感字段以 *_configured 标记显示）
  useEffect(() => {
    if (!initial) return;
    const next: Record<string, string> = {};
    for (const f of Object.keys(meta.fields)) {
      const sensitive = initial[`${f}_configured`];
      const plain = initial[f];
      if (sensitive === true) next[f] = '••••••••••••（已配置，只写不回显）';
      else if (sensitive === false) next[f] = '';
      else if (typeof plain === 'string') next[f] = plain;
    }
    setValues(asUpdater((prev) => ({ ...prev, ...next })));
  }, [initial, meta.fields]);

  const save = async () => {
    setMsg(null);
    setBusy(true);
    const payload: Record<string, string> = {};
    for (const f of Object.keys(meta.fields)) {
      const v = (values[f] ?? '').trim();
      if (v && !v.includes('已配置')) payload[f] = v;
    }
    const err = await onSave(broker, payload);
    setBusy(false);
    setMsg(err ? { ok: false, text: `❌ ${err}` } : { ok: true, text: '✅ 已保存' });
  };

  const test = async () => {
    setMsg(null);
    setBusy(true);
    const r = await onTest(broker);
    setBusy(false);
    setMsg({ ok: r.ok, text: `${r.ok ? '✅' : '❌'} ${r.text}` });
  };

  return (
    <div className="ts-broker">
      <div className="ts-broker-head">
        <div className="ts-broker-name">{meta.name}</div>
        <div className="ts-broker-market">{meta.markets}</div>
      </div>
      <div className="ts-broker-body">
        <div className="ts-broker-desc">{meta.desc}</div>
        {Object.entries(meta.fields).map(([f, fm]) => (
          <label key={f} className="ts-field">
            <span className="ts-field-label">{fm.label}{fm.sensitive ? '（敏感字段只写不回显）' : ''}</span>
            <input className="ts-input" style={{ width: '100%', minWidth: 0 }}
              type={/pwd|key|password|secret/i.test(f) ? 'password' : 'text'}
              value={values[f] ?? ''}
              onChange={(e) => setValues(asUpdater((prev) => ({ ...prev, [f]: e.target.value })))} />
          </label>
        ))}
        <div className="ts-broker-actions">
          <button className="ts-btn dark" onClick={() => void save()} disabled={busy}>保存配置</button>
          <button className="ts-btn" onClick={() => void test()} disabled={busy}>测试连接</button>
        </div>
        <div className={`ts-broker-msg ${msg?.ok ? 'ok' : 'err'}`}>{msg?.text ?? ''}</div>
      </div>
    </div>
  );
}

// ---------- 市场→交易所映射（哪个市场用哪个券商执行） ----------

const MARKET_LABELS: Record<string, string> = { cn: '🇨🇳 A股', hk: '🇭🇰 港股', us: '🇺🇸 美股' };
const BROKER_LABELS: Record<string, string> = {
  tdx: '通达信桥', futu: '富途证券', tiger: '老虎证券', ibkr: '盈透 IB',
};

function MappingPicker() {
  const [info, setInfo] = useState<BrokerMarketInfo | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    fetchBrokerMarket()
      .then((d) => { setInfo(d); setValues(d.mapping ?? {}); })
      .catch(() => {});
  }, []);

  const save = async () => {
    setBusy(true);
    try {
      const d = await saveBrokerMarket(values);
      setInfo(d);
      setMsg({ ok: true, text: '映射已保存：执行链将按此选择券商（A股固定通达信桥）' });
    } catch {
      setMsg({ ok: false, text: '保存失败' });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ts-card-desc" style={{ margin: '8px 0 12px' }}>
      <div style={{ fontWeight: 700, marginBottom: 6 }}>市场 → 交易所（执行通道选择）</div>
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        {Object.entries(MARKET_LABELS).map(([mkt, label]) => (
          <label key={mkt} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <span>{label}</span>
            <select
              className="ts-input"
              style={{ width: 130, padding: '4px 6px' }}
              value={values[mkt] ?? ''}
              onChange={(e) => setValues(asUpdater((prev) => ({ ...prev, [mkt]: e.target.value })))}
              disabled={mkt === 'cn'}
            >
              {(info?.choices?.[mkt] ?? [mkt === 'cn' ? 'tdx' : 'tiger']).map((b) => (
                <option key={b} value={b}>{BROKER_LABELS[b] ?? b}</option>
              ))}
            </select>
          </label>
        ))}
        <button className="ts-btn dark" onClick={() => void save()} disabled={busy}>保存映射</button>
        {msg && <span className={`ts-broker-msg ${msg.ok ? 'ok' : 'err'}`}>{msg.text}</span>}
      </div>
    </div>
  );
}
