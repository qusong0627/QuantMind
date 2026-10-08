/**
 * 港股最小下单/撤单面板（QuantMind 本地新增，2026-10-08）。
 *
 * ⚠️ 本组件**不在** arena 生成器（tools/port-from-arena.mjs）的产物范围内，
 * 目录位于 `agent-arena/hk-order-panel/`（生成器只写 `agent-arena/arena/**`）。
 * 上游 arena 从来没有下单 UI，这是恢复富途通道时新增的前端能力；
 * **不要**把它挪进 `arena/**`——会被下次重跑生成器覆盖。
 *
 * 数据面（均走 QM 原生 `/api/v1/agent-arena/futu/*`，见 arena/api/client.ts 尾部）：
 *   - 下单 placeFutuOrder(env, order)   env: SIMULATE（默认）/ REAL
 *   - 撤单 cancelFutuOrder(env, id)
 *   - 当前委托 fetchFutuOrders(env)（15s 轮询，含未成交/已撤全量当日委托）
 *
 * 安全边界（与后端 fail-closed 对齐，前端只做提示，不替代服务端判定）：
 *   - 实盘（REAL）提交前强制 window.confirm；服务端还有闸门（403
 *     real_trading_disabled）与解锁（409 futu_unlock_required）两道。
 *   - 后端返回的业务失败（SDK 拒单）是 200 + success:false + message，
 *     这里照原样展示拒因。
 */

import { useState } from 'react';
import type { FormEvent } from 'react';
import type { FutuOrderRaw } from '../arena/api/client';
import { cancelFutuOrder, fetchFutuOrders, placeFutuOrder } from '../arena/api/client';
import { usePolling } from '../arena/hooks/usePolling';
import './HkOrderPanel.css';

type Env = 'REAL' | 'SIMULATE';
type Side = 'BUY' | 'SELL';
type OrderType = 'NORMAL' | 'MARKET';

/** 可撤状态：终态（已成交/已撤/失败/已失效）不给撤单按钮。 */
const CANCELLABLE = new Set(['WAITING_SUBMIT', 'SUBMITTED', 'FILLED_PART']);

const STATUS_CN: Record<string, string> = {
  WAITING_SUBMIT: '待提交',
  SUBMITTING: '提交中',
  SUBMITTED: '已提交',
  FILLED_PART: '部分成交',
  FILLED_ALL: '全部成交',
  CANCELLING_ALL: '撤单中',
  CANCELLED_PART: '部分撤单',
  CANCELLED_ALL: '已撤单',
  FAILED: '失败',
  DELETED: '已删除',
  DISABLED: '已失效',
  TIME_OUT: '已超时',
};

const envLabel = (env: Env) => (env === 'REAL' ? '实盘' : '模拟');
const sideLabel = (side: string) => (String(side).toUpperCase() === 'SELL' ? '卖出' : '买入');

/** 后端错误 → 中文文案（axios 错误形状：response.status / response.data.detail）。 */
export function describeFutuError(err: unknown): string {
  const resp = (err as {
    response?: { status?: number; data?: { detail?: unknown } };
    message?: string;
  })?.response;
  const status = resp?.status;
  const detail = String(resp?.data?.detail ?? '');
  if (status === 403) return '实盘交易未开启：后端闸门已关闭（real_trading_disabled）';
  if (status === 409) return '实盘下单未解锁（需配置交易密码 MD5）';
  if (status === 502) return '富途通道不可达（OpenD 未连接 / 未登录）';
  if (status === 503) return '富途整体不可达（OpenD 未连接）';
  if (detail) return detail;
  const msg = (err as { message?: string })?.message;
  return msg || '请求失败（未知错误）';
}

/** 数量守卫：富途要求正整数（港股每手股数因股而异，整百仅提示不强校验）。 */
export function validateOrder(input: {
  code: string;
  quantity: number;
  price: number;
  orderType: OrderType;
}): string | null {
  if (!input.code.trim()) return '请填写股票代码（如 00700.HK / 700）';
  if (!Number.isInteger(input.quantity) || input.quantity <= 0) return '数量必须是正整数';
  if (input.orderType === 'NORMAL' && !(input.price > 0)) return '限价单必须填写大于 0 的价格';
  return null;
}

export default function HkOrderPanel() {
  const [env, setEnv] = useState<Env>('SIMULATE');
  const [code, setCode] = useState('00700.HK');
  const [side, setSide] = useState<Side>('BUY');
  const [orderType, setOrderType] = useState<OrderType>('NORMAL');
  const [price, setPrice] = useState('');
  const [quantity, setQuantity] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitMsg, setSubmitMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [cancelBusy, setCancelBusy] = useState('');

  const orders = usePolling<FutuOrderRaw[]>(() => fetchFutuOrders(env), [env], 15000, 2500);
  const rows = orders.data ?? [];

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    const qty = Number(quantity);
    const px = orderType === 'MARKET' ? 0 : Number(price);
    const invalid = validateOrder({ code, quantity: qty, price: px, orderType });
    if (invalid) {
      setSubmitMsg({ ok: false, text: invalid });
      return;
    }
    if (
      env === 'REAL' &&
      !window.confirm(
        `确认向【实盘】提交：${sideLabel(side)} ${code} ${qty} 股` +
          (orderType === 'NORMAL' ? ` @${px}` : '（市价）') +
          '\n将使用真实资金，请再次确认。',
      )
    ) {
      return;
    }
    setSubmitting(true);
    setSubmitMsg(null);
    try {
      const out = await placeFutuOrder(env, {
        code: code.trim(),
        price: px,
        quantity: qty,
        order_type: orderType,
        trd_side: side,
      });
      if (out.success) {
        setSubmitMsg({
          ok: true,
          text: `已受理：委托号 ${out.order_id || '—'}（${STATUS_CN[out.status] || out.status || '已提交'}）`,
        });
      } else {
        setSubmitMsg({ ok: false, text: `拒单：${out.message || '富途未返回原因'}` });
      }
      orders.refresh();
    } catch (err) {
      setSubmitMsg({ ok: false, text: describeFutuError(err) });
    } finally {
      setSubmitting(false);
    }
  };

  const onCancel = async (orderId: string) => {
    if (cancelBusy || !orderId) return;
    if (!window.confirm(`确认撤销委托 ${orderId}？`)) return;
    setCancelBusy(orderId);
    setSubmitMsg(null);
    try {
      const out = await cancelFutuOrder(env, orderId);
      setSubmitMsg(
        out.success
          ? { ok: true, text: `撤单已受理：${orderId}` }
          : { ok: false, text: `撤单失败：${out.message || '富途未返回原因'}` },
      );
      orders.refresh();
    } catch (err) {
      setSubmitMsg({ ok: false, text: describeFutuError(err) });
    } finally {
      setCancelBusy('');
    }
  };

  return (
    <div className="real-account-card hkop-card">
      <div className="real-account-head">
        <span className="real-account-title">港股下单（富途）</span>
        <span className="real-account-ts">
          <select
            className="filter-select hkop-env"
            aria-label="下单环境"
            value={env}
            onChange={(e) => {
              setEnv(e.target.value as Env);
              setSubmitMsg(null);
            }}
          >
            <option value="SIMULATE">模拟（SIMULATE）</option>
            <option value="REAL">实盘（REAL）</option>
          </select>
        </span>
      </div>

      {env === 'REAL' && (
        <div className="hkop-real-warn" role="alert">
          实盘环境：提交后将使用真实资金；后端未配置交易密码时会以 409 拒绝（fail-closed）。
        </div>
      )}

      {/* noValidate：min/step 只作控件提示，校验统一走 validateOrder（中文文案 +
          错误面与后端 400 同口径），否则浏览器原生气泡会抢占提交 */}
      <form className="hkop-form" noValidate onSubmit={(e) => void onSubmit(e)}>
        <label className="hkop-field">
          <span>代码</span>
          <input
            aria-label="股票代码"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="00700.HK / HK.700 / 700"
            autoComplete="off"
          />
        </label>
        <label className="hkop-field">
          <span>方向</span>
          <select
            aria-label="买卖方向"
            value={side}
            onChange={(e) => setSide(e.target.value as Side)}
          >
            <option value="BUY">买入</option>
            <option value="SELL">卖出</option>
          </select>
        </label>
        <label className="hkop-field">
          <span>类型</span>
          <select
            aria-label="委托类型"
            value={orderType}
            onChange={(e) => setOrderType(e.target.value as OrderType)}
          >
            <option value="NORMAL">限价</option>
            <option value="MARKET">市价</option>
          </select>
        </label>
        <label className="hkop-field">
          <span>价格</span>
          <input
            aria-label="委托价格"
            type="number"
            inputMode="decimal"
            step="0.001"
            min="0"
            value={price}
            onChange={(e) => setPrice(e.target.value)}
            placeholder={orderType === 'MARKET' ? '市价免填' : '如 380.0'}
            disabled={orderType === 'MARKET'}
          />
        </label>
        <label className="hkop-field">
          <span>数量</span>
          <input
            aria-label="委托数量"
            type="number"
            inputMode="numeric"
            step="1"
            min="1"
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            placeholder="股数（整数）"
          />
        </label>
        <button className="btn btn-primary hkop-submit" type="submit" disabled={submitting}>
          {submitting ? '提交中…' : `提交${envLabel(env)}${sideLabel(side)}`}
        </button>
      </form>

      <div className="hkop-hint">港股每手股数因股而异（常见 100 / 500），请按整手数量下单。</div>

      {submitMsg && (
        <div className={`hkop-msg ${submitMsg.ok ? 'hkop-ok' : 'hkop-fail'}`} role="status">
          {submitMsg.text}
        </div>
      )}

      <div className="hkop-orders">
        <div className="real-section-title">
          当前委托（{envLabel(env)}）
          <button
            type="button"
            className="hkop-refresh"
            onClick={() => orders.refresh()}
            title="刷新委托列表"
          >
            刷新
          </button>
        </div>
        {orders.error ? (
          <div className="real-pos-empty">委托读取失败：{orders.error}</div>
        ) : rows.length === 0 ? (
          <div className="real-pos-empty">{orders.loading ? '读取中…' : '当日暂无委托'}</div>
        ) : (
          <div className="hkop-order-list">
            {rows.map((o) => (
              <div className="hkop-order-row" key={o.order_id || `${o.code}-${o.create_time}`}>
                <span className="hkop-order-main">
                  <span className="hkop-order-name">{o.name || o.code}</span>
                  <span className="hkop-order-code">{o.code}</span>
                  <span className={o.trd_side === 'SELL' ? 'down' : 'up'}>
                    {sideLabel(o.trd_side)}
                  </span>
                </span>
                <span className="hkop-order-detail">
                  {Number(o.dealt_qty) || 0}/{Number(o.qty) || 0} 股
                  {Number(o.price) > 0 ? ` @${Number(o.price)}` : '（市价）'}
                  {Number(o.dealt_avg_price) > 0 ? ` · 均价 ${Number(o.dealt_avg_price)}` : ''}
                </span>
                <span className="hkop-order-status">
                  {STATUS_CN[o.order_status] || o.order_status || '—'}
                  {o.last_err_msg ? <span className="hkop-order-err"> {o.last_err_msg}</span> : null}
                </span>
                {CANCELLABLE.has(String(o.order_status).toUpperCase()) && (
                  <button
                    type="button"
                    className="hkop-cancel"
                    disabled={cancelBusy === o.order_id}
                    onClick={() => void onCancel(o.order_id)}
                  >
                    {cancelBusy === o.order_id ? '撤单中…' : '撤单'}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
