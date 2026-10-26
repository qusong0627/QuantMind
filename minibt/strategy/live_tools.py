"""实时图表专属工具：买卖盘 / 持仓线 / 二级报价 + 简易下单。

minibt 的实时图表（``minibt/strategy/realtime.py::RealtimeChart`` →
``StrategyWindow``）是 **pywebview HTML** 窗口，不是 Qt —— 所以 miniqt 的 Qt 控件
（``QuotePanel`` / ``OrderTicketPanel`` / ``SimAccountPanel``）不能直接用；这里：

* **买卖盘**：复用 ``pylightcharts.panels.OrderTicket``（HTML 插件），点击用
  **``TargetPosTask.set_target_volume``** 交易（持仓时两侧变「买入/平仓」）；
* **持仓线**：``chart.horizontal_line(..., axis_label_visible=False,
  label_visible=True)`` —— 标签画在**可视范围左边**（pane 左沿），不挤进 Y 轴；
* **二级报价 + 简易下单**：注入 HTML（复刻 miniqt：合约代码/名称 + 5 档盘口 +
  信息栏 + 简易下单）。**开多/开空/平仓**按价格框价格 ``insert_order``（限价）——
  开多 = BUY/OPEN、开空 = SELL/OPEN、平仓 = 多头 SELL/CLOSE / 空头 BUY/CLOSE；
  **市价平仓 / 反手 / 撤单** 用 ``TargetPosTask`` / ``cancel_order`` ✓。
  下单按钮下面是**持仓 / 委托 / 历史成交** 三个可切换 tabs（只显示**当前合约**）：
  持仓 = 买卖/总仓/持仓均价/盈亏，委托 = 买卖/开平/手/未成交，
  历史成交 = 买卖/开平/成交价/手 ✓。

只对**实时**图表构建（``RealtimeChart(realtime=True)``）；回测/静态图不建。
"""
from __future__ import annotations

import json
from typing import Optional

#: 二级报价 + 简易下单面板（注入一次；之后 ``__minibtDepth.show/update`` 驱动）。
_DEPTH_JS = r"""
(function () {
  if (window.__minibtDepth) return;
  var style = document.createElement('style');
  style.id = 'minibt-depth-style';
  style.textContent = [
    '#minibt-depth{position:fixed;right:0;top:0;bottom:0;width:224px;',
    'background:var(--bg-color,#fff);color:var(--color,#222);z-index:2500;',
    'display:none;flex-direction:column;font:12px/1.45 system-ui,sans-serif;',
    'border-left:1px solid var(--border-color,#ccc);overflow-y:auto;}',
    '#minibt-depth .d-head{display:flex;justify-content:space-between;gap:6px;',
    'padding:6px 8px;font-weight:600;background:var(--hover-bg-color,#f0f0f0);}',
    '#minibt-depth .d-head .d-name{font-weight:400;opacity:.75;overflow:hidden;',
    'text-overflow:ellipsis;white-space:nowrap;}',
    '#minibt-depth table{width:100%;border-collapse:collapse;}',
    '#minibt-depth td{padding:0 8px;height:18px;font-variant-numeric:tabular-nums;}',
    '#minibt-depth td.s{padding-left:6px;opacity:.6;}',
    '#minibt-depth td.p{text-align:right;}',
    '#minibt-depth td.v{text-align:right;width:40%;opacity:.85;}',
    '#minibt-depth tr.d-ask td.p{color:#ef5350;}',
    '#minibt-depth tr.d-bid td.p{color:#26a69a;}',
    '#minibt-depth .d-info{display:grid;grid-template-columns:auto 1fr auto 1fr;',
    'gap:2px 6px;padding:6px 8px;margin-top:2px;',
    'border-top:1px solid var(--border-color,#ccc);',
    'border-bottom:1px solid var(--border-color,#ccc);}',
    '#minibt-depth .d-info .k{opacity:.6;}',
    '#minibt-depth .d-info .v{text-align:right;font-variant-numeric:tabular-nums;}',
    '#minibt-depth .d-up{color:#ef5350;}',
    '#minibt-depth .d-down{color:#26a69a;}',
    '#minibt-depth .d-ticket{padding:6px 8px;}',
    '#minibt-depth .d-row{display:flex;align-items:center;gap:4px;margin:3px 0;}',
    '#minibt-depth .d-row label{width:32px;opacity:.7;}',
    '#minibt-depth .d-row input{flex:1;min-width:0;box-sizing:border-box;',
    'padding:2px 4px;background:var(--muted-bg-color,#f5f5f5);',
    'color:var(--color,#222);border:1px solid var(--border-color,#ccc);}',
    '#minibt-depth .d-step{width:20px;height:20px;line-height:1;cursor:pointer;',
    'border:1px solid var(--border-color,#ccc);border-radius:3px;',
    'background:var(--hover-bg-color,#f0f0f0);color:var(--color,#222);}',
    '#minibt-depth .d-btns{display:flex;gap:6px;margin-top:5px;}',
    '#minibt-depth .d-btns button{flex:1;padding:4px 0;border:none;',
    'border-radius:3px;cursor:pointer;color:#fff;white-space:nowrap;}',
    '#minibt-depth .d-buy{background:#26a69a;}',
    '#minibt-depth .d-sell{background:#ef5350;}',
    '#minibt-depth .d-close{background:#607d8b;}',
    '#minibt-depth .d-btns button.d-mkt{background:var(--hover-bg-color,#eceff1);',
    'color:var(--color,#222);border:1px solid var(--border-color,#ccc);}',
    '#minibt-depth .d-status{margin-top:5px;opacity:.75;min-height:16px;',
    'word-break:break-all;}',
    // ---- 持仓 / 委托 / 历史成交 tabs（宽度仍与原二级报价一致 224px ✓）----
    '#minibt-depth .d-tabs{display:flex;margin-top:6px;',
    'border-top:1px solid var(--border-color,#ccc);}',
    '#minibt-depth .d-tab{flex:1;padding:4px 0;border:none;background:transparent;',
    'color:var(--color,#222);opacity:.6;cursor:pointer;font-size:11px;',
    'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
    '#minibt-depth .d-tab.d-on{opacity:1;font-weight:600;',
    'border-bottom:2px solid var(--active-color,#0a84ff);}',
    '#minibt-depth .d-tbl-wrap{max-height:170px;overflow-y:auto;}',
    '#minibt-depth table.d-tbl{width:100%;border-collapse:collapse;',
    'font-size:11px;table-layout:fixed;}',
    '#minibt-depth table.d-tbl th{padding:3px 4px;opacity:.6;font-weight:400;',
    'text-align:right;border-bottom:1px solid var(--border-color,#ccc);',
    'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
    '#minibt-depth table.d-tbl td{padding:2px 4px;text-align:right;',
    'font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;',
    'text-overflow:ellipsis;}',
    '#minibt-depth table.d-tbl th:first-child,',
    '#minibt-depth table.d-tbl td:first-child{text-align:left;}',
    '#minibt-depth table.d-tbl tr.d-up td{color:#ef5350;}',
    '#minibt-depth table.d-tbl tr.d-down td{color:#26a69a;}',
  ].join('');
  document.head.appendChild(style);
  var box = document.createElement('div');
  box.id = 'minibt-depth';
  var labels = ['五', '四', '三', '二', '一'];
  var rows = '';
  var i;
  for (i = 0; i < 5; i++) {
    var a = 5 - i;
    rows += '<tr class="d-ask"><td class="s">卖' + labels[i] + '</td>'
          + '<td class="p" id="md-askp' + a + '">--</td>'
          + '<td class="v" id="md-askv' + a + '">--</td></tr>';
  }
  for (i = 0; i < 5; i++) {
    var b = i + 1;
    rows += '<tr class="d-bid"><td class="s">买' + labels[4 - i] + '</td>'
          + '<td class="p" id="md-bidp' + b + '">--</td>'
          + '<td class="v" id="md-bidv' + b + '">--</td></tr>';
  }
  var cells = [
    ['最新', 'last'], ['涨跌', 'change'],
    ['涨跌幅', 'change_pct'], ['昨结', 'pre_settlement'],
    ['结算', 'settlement'], ['今开', 'open'],
    ['最高', 'highest'], ['最低', 'lowest'],
    ['成交量', 'volume'], ['持仓量', 'open_interest'],
  ];
  var info = '';
  for (i = 0; i < cells.length; i++) {
    info += '<span class="k">' + cells[i][0] + '</span>'
          + '<span class="v" id="md-i-' + cells[i][1] + '">--</span>';
  }
  box.innerHTML =
      '<div class="d-head"><span>二级报价</span>'
    + '<span class="d-name" id="md-title"></span></div>'
    + '<table>' + rows + '</table>'
    + '<div class="d-info">' + info + '</div>'
    + '<div class="d-ticket">'
    +   '<div class="d-row"><label>价格</label>'
    +     '<button class="d-step" id="md-p-down">-</button>'
    +     '<input id="md-price" value="">'
    +     '<button class="d-step" id="md-p-up">+</button></div>'
    +   '<div class="d-row"><label>手数</label>'
    +     '<button class="d-step" id="md-v-down">-</button>'
    +     '<input id="md-volume" value="1">'
    +     '<button class="d-step" id="md-v-up">+</button></div>'
    +   '<div class="d-btns">'
    +     '<button class="d-buy" id="md-buy">开多</button>'
    +     '<button class="d-sell" id="md-sell">开空</button>'
    +     '<button class="d-close" id="md-close">平仓</button></div>'
    +   '<div class="d-btns">'
    +     '<button class="d-mkt" id="md-mkt-close">市价平仓</button>'
    +     '<button class="d-mkt" id="md-reverse">反手</button>'
    +     '<button class="d-mkt" id="md-cancel">撤单</button></div>'
    +   '<div class="d-tabs">'
    +     '<button class="d-tab d-on" data-tab="pos">持仓</button>'
    +     '<button class="d-tab" data-tab="ord">委托</button>'
    +     '<button class="d-tab" data-tab="trd">历史成交</button></div>'
    +   '<div class="d-tbl-wrap">'
    +     '<table class="d-tbl" id="md-tbl-pos"><thead><tr>'
    +       '<th>买卖</th><th>总仓</th><th>持仓均价</th><th>盈亏</th>'
    +     '</tr></thead><tbody></tbody></table>'
    +     '<table class="d-tbl" id="md-tbl-ord" style="display:none">'
    +       '<thead><tr><th>买卖</th><th>开平</th><th>手</th><th>未成交</th>'
    +       '<th>报单价</th>'
    +     '</tr></thead><tbody></tbody></table>'
    +     '<table class="d-tbl" id="md-tbl-trd" style="display:none">'
    +       '<thead><tr><th>买卖</th><th>开平</th><th>成交价</th><th>手</th>'
    +     '</tr></thead><tbody></tbody></table>'
    +   '</div>'
    +   '<div class="d-status" id="md-status"></div>'
    + '</div>';
  document.body.appendChild(box);
  var byId = function (id) { return document.getElementById(id); };
  var tick = 0.0;
  var setText = function (id, text, cls) {
    var el = byId(id);
    if (!el) { return; }
    el.textContent = text;
    el.className = 'v' + (cls ? ' ' + cls : '');
  };
  var fmt = function (v, d) {
    if (v === null || v === undefined || v === '' || isNaN(v)) { return '--'; }
    return Number(v).toFixed(d);
  };
  var num = function (v) {
    var n = Number(v);
    return isNaN(n) ? null : n;
  };
  // ---- 持仓 / 委托 / 历史成交：表格渲染 + tab 切换 ----
  var activeTab = 'pos';
  var esc = function (v) {
    return String(v === null || v === undefined ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  };
  var rowCls = function (d) {
    return d === '\u4e70' ? ' class="d-up"'
      : (d === '\u5356' ? ' class="d-down"' : '');
  };
  var renderRows = function (id, list) {
    var table = byId(id);
    if (!table) { return; }
    var body = table.querySelector('tbody');
    if (!body) { return; }
    var rows = list || [];
    var html = '';
    for (var r = 0; r < rows.length; r++) {
      var cells = rows[r].cells || [];
      html += '<tr' + rowCls(rows[r].direction) + '>';
      for (var c = 0; c < cells.length; c++) {
        html += '<td>' + esc(cells[c]) + '</td>';
      }
      html += '</tr>';
    }
    body.innerHTML = html;
  };
  var renderAccounts = function (data) {
    renderRows('md-tbl-pos', data.positions);
    renderRows('md-tbl-ord', data.orders);
    renderRows('md-tbl-trd', data.trades);
  };
  var setTab = function (name) {
    activeTab = name || 'pos';
    var ids = { pos: 'md-tbl-pos', ord: 'md-tbl-ord', trd: 'md-tbl-trd' };
    var tabs = box.querySelectorAll('.d-tab');
    for (var i = 0; i < tabs.length; i++) {
      var on = tabs[i].dataset.tab === activeTab;
      tabs[i].className = 'd-tab' + (on ? ' d-on' : '');
    }
    for (var key in ids) {
      var el = byId(ids[key]);
      if (el) { el.style.display = (key === activeTab) ? '' : 'none'; }
    }
  };
  window.__minibtDepth = {
    show: function (flag) {
      box.style.display = flag ? 'flex' : 'none';
      var c = byId('container');
      if (c) { c.style.width = flag ? 'calc(100% - 224px)' : '100%'; }
      try { Lib.Handler.reLayoutAll(); } catch (e) {}
    },
    update: function (data) {
      var p = Number(data.precision);
      if (isNaN(p)) { p = 2; }
      tick = Number(data.tick) || 0.0;
      for (var k = 1; k <= 5; k++) {
        var ap = byId('md-askp' + k), av = byId('md-askv' + k);
        var bp = byId('md-bidp' + k), bv = byId('md-bidv' + k);
        if (ap) { ap.textContent = fmt(data['ask_price' + k], p); }
        if (av) { av.textContent = fmt(data['ask_volume' + k], 0); }
        if (bp) { bp.textContent = fmt(data['bid_price' + k], p); }
        if (bv) { bv.textContent = fmt(data['bid_volume' + k], 0); }
      }
      var title = byId('md-title');
      if (title) {
        title.textContent = [data.symbol, data.name].filter(Boolean).join(' ');
      }
      var changeCls = function (v) {
        return (num(v) > 0) ? 'd-up' : ((num(v) < 0) ? 'd-down' : '');
      };
      setText('md-i-last', fmt(data.last, p), changeCls(data.change));
      setText('md-i-change', fmt(data.change, p), changeCls(data.change));
      setText('md-i-change_pct',
              data.change_pct === null || data.change_pct === undefined
                ? '--' : (Number(data.change_pct) >= 0 ? '+' : '')
                  + Number(data.change_pct).toFixed(2) + '%',
              changeCls(data.change_pct));
      setText('md-i-pre_settlement', fmt(data.pre_settlement, p), '');
      setText('md-i-settlement', fmt(data.settlement, p), '');
      setText('md-i-open', fmt(data.open, p), '');
      setText('md-i-highest', fmt(data.highest, p), '');
      setText('md-i-lowest', fmt(data.lowest, p), '');
      setText('md-i-volume',
              data.volume === null || data.volume === undefined
                ? '--' : String(Math.round(Number(data.volume))), '');
      setText('md-i-open_interest',
              data.open_interest === null || data.open_interest === undefined
                ? '--' : String(Math.round(Number(data.open_interest))), '');
      var priceEl = byId('md-price');
      if (priceEl && !priceEl.dataset.touched && data.ask_price1 != null
          && !isNaN(data.ask_price1)) {
        priceEl.value = fmt(data.ask_price1, p);
      }
      if (data.status !== undefined && data.status !== null) {
        var s = byId('md-status');
        if (s) { s.textContent = data.status; }
      }
      renderAccounts(data);
    },
    tab: function (name) {
      setTab(name);
    },
    status: function (text) {
      var s = byId('md-status');
      if (s) { s.textContent = text; }
    },
  };
  var priceValue = function () { return byId('md-price').value; };
  var volumeValue = function () { return byId('md-volume').value; };
  var sendOrder = function (action) {
    window.callbackFunction('minibt_depth_order_~_' + action
      + ';;;' + priceValue() + ';;;' + volumeValue());
  };
  var sendMarket = function (action) {
    window.callbackFunction('minibt_depth_market_~_' + action
      + ';;;' + volumeValue());
  };
  var step = function (id, delta) {
    var el = byId(id);
    if (!el) { return; }
    var value = num(el.value);
    if (value === null) { value = 0; }
    if (id === 'md-volume') {
      value = Math.max(1, Math.round(value) + delta);
      el.value = String(value);
      return;
    }
    var unit = tick || 1;
    value = value + delta * unit;
    var decimals = (String(unit).split('.')[1] || '').length;
    el.value = value.toFixed(decimals);
    el.dataset.touched = '1';
  };
  byId('md-p-down').addEventListener('click', function () { step('md-price', -1); });
  byId('md-p-up').addEventListener('click', function () { step('md-price', 1); });
  byId('md-v-down').addEventListener('click', function () { step('md-volume', -1); });
  byId('md-v-up').addEventListener('click', function () { step('md-volume', 1); });
  byId('md-price').addEventListener('input', function () {
    this.dataset.touched = '1';
  });
  byId('md-buy').addEventListener('click', function () { sendOrder('long'); });
  byId('md-sell').addEventListener('click', function () { sendOrder('short'); });
  byId('md-close').addEventListener('click', function () { sendOrder('close'); });
  byId('md-mkt-close').addEventListener('click', function () {
    sendMarket('close_market');
  });
  byId('md-reverse').addEventListener('click', function () {
    sendMarket('reverse');
  });
  byId('md-cancel').addEventListener('click', function () {
    sendMarket('cancel');
  });
  // ---- tab 切换（持仓 / 委托 / 历史成交）----
  var tabButtons = box.querySelectorAll('.d-tab');
  for (var ti = 0; ti < tabButtons.length; ti++) {
    (function (btn) {
      btn.addEventListener('click', function () { setTab(btn.dataset.tab); });
    })(tabButtons[ti]);
  }
})();
"""

#: 买卖盘的 host div：插到图表图例里（OHLC 行下面）——和 miniqt 一致 ✓
_ORDER_BOOK_HOST_JS = r"""
(function () {
  if (document.getElementById('__minibtOrderTicketHost')) return;
  var host = document.createElement('div');
  host.id = '__minibtOrderTicketHost';
  host.style.cssText = 'display:flex;gap:0;margin:4px 0 2px 0;'
    + 'position:relative;z-index:4000;pointer-events:auto;';
  var handler = window.__minibtChartHandler || null;
  var legend = handler && handler.legend;
  var placed = false;
  if (legend && legend.div) {
    var wrapper = legend.seriesContainer && legend.seriesContainer.parentNode;
    if (wrapper) { legend.div.insertBefore(host, wrapper); placed = true; }
    else { legend.div.appendChild(host); placed = true; }
  }
  if (!placed) {
    // 找不到图例（图例还没建好）→ 退化成左上角浮层，**绝不能**留在 body 末位
    // 被图表画布盖住（用户反馈“看到K线遮挡了买卖盘”✗）
    host.style.position = 'fixed';
    host.style.top = '10px';
    host.style.left = '10px';
    document.body.appendChild(host);
  }
})();
"""


def _num(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def current_kline(strategy, contract: str):
    """按 ``{symbol}_{cycle}`` 找当前合约的 KLine（实时图 window.contract）✓。"""
    for value in (getattr(strategy, '_btklinedataset', {}) or {}).values():
        try:
            if f'{value.symbol}_{value.cycle}' == contract:
                return value
        except Exception:                                        # noqa: BLE001
            continue
    return None


def _tick_decimals(tick: float) -> int:
    text = f'{tick:.10f}'.rstrip('0')
    return len(text.split('.')[1]) if '.' in text else 0


#: 天勤 Position 的多空字段名（新老版本不一样，两个都试 ✓）
_LONG_VOLUME = ('pos_long', 'volume_long')
_SHORT_VOLUME = ('pos_short', 'volume_short')
_LONG_AVG = ('position_price_long', 'open_price_long')
_SHORT_AVG = ('position_price_short', 'open_price_short')
_LONG_PROFIT = ('float_profit_long', 'position_profit_long')
_SHORT_PROFIT = ('float_profit_short', 'position_profit_short')


def _field(item, key, default=None):
    """取一行里的字段（dict 或对象都行 ✓）。"""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _field_any(item, names, default=None):
    """按**多个别名**取字段（天勤 ``pos_long`` / ``volume_long`` 都认 ✓）。"""
    for name in names:
        value = _field(item, name)
        if value is not None:
            return value
    return default


def _same_instrument(item, symbol: str) -> bool:
    """订单 / 成交是否属于 ``symbol``（形如 ``SHFE.ag2612``）✓

    ★ 天勤把 symbol 拆成两段存：``instrument_id='ag2612'`` +
      ``exchange_id='SHFE'``（见 ``tqsdk/api.py``：
      ``(exchange_id, instrument_id) = symbol.split('.', 1)``）。直接拿
      ``instrument_id`` 跟 ``SHFE.ag2612`` 比会**全部过滤掉** → 历史成交 / 委托
      永远空 ✗。这里 ``SHFE.ag2612`` / ``ag2612`` 两种形式都认 ✓
    """
    instrument = str(_field(item, 'instrument_id') or '')
    if not instrument:
        return False
    if instrument == symbol:
        return True
    exchange = str(_field(item, 'exchange_id') or '')
    if exchange and f'{exchange}.{instrument}' == symbol:
        return True
    # 退路：只比合约号（``ag2612`` vs ``SHFE.ag2612``）
    return instrument == symbol.rsplit('.', 1)[-1]


def _records(source) -> list:
    """把天勤返回的一批记录统一成 ``[一行, ...]`` ✓。

    ``get_order`` / ``get_trade`` **不带参数**时返回 **pandas DataFrame**；
    带参数时可能是 ``{id: 对象}``；某些版本还会给 ``Entity``（**不是 dict 子类**，
    迭代拿到的是 key ✗）。不全处理的话表格会永远空 ✗。
    """
    if source is None:
        return []
    if hasattr(source, 'to_dict') and hasattr(source, 'columns'):
        try:
            return list(source.to_dict('records'))
        except Exception:                                        # noqa: BLE001
            return []
    if isinstance(source, dict):
        return list(source.values())
    if isinstance(source, (list, tuple, set)):
        return list(source)
    if hasattr(source, '__getitem__') and hasattr(source, '__len__'):
        try:
            keys = [key for key in source
                    if isinstance(key, str) and not key.startswith('_')]
        except Exception:                                        # noqa: BLE001
            keys = []
        rows = []
        for key in keys:
            try:
                rows.append(source[key])
            except Exception:                                    # noqa: BLE001
                continue
        if rows:
            return rows
    return [source]


def _dir_text(direction) -> str:
    return {'BUY': '买', 'SELL': '卖'}.get(str(direction or '').upper(),
                                           str(direction or ''))


def _off_text(offset) -> str:
    return {'OPEN': '开', 'CLOSE': '平', 'CLOSETODAY': '平今',
            'CLOSEYESTERDAY': '平昨'}.get(str(offset or '').upper(),
                                           str(offset or ''))


def _fmt_num(value, decimals=2) -> str:
    number = _num(value)
    if number is None:
        return '—'
    return f'{number:.{int(decimals)}f}'


def _fmt_signed(value, decimals=2) -> str:
    number = _num(value)
    if number is None:
        return '—'
    return f'{number:+.{int(decimals)}f}'


def _fmt_int(value) -> str:
    number = _num(value)
    if number is None:
        return '—'
    return str(int(round(number)))


class LiveTools:
    """实时图表的工具（买卖盘 / 持仓线 / 二级报价 + 简易下单）。"""

    def __init__(self, owner):
        self._owner = owner
        #: 只有**实时**图表才启用买卖盘 / 持仓线 / 二级报价；回测 / 回放 /
        #: ``btplot`` 静态图即使策略里有持仓，也不该画持仓线 / 打日志（用测试数据
        #: 跑 ``btplot`` 时会误报「持仓线已画」）。
        #: `RealtimeChart` 在造 LiveTools 之前已设好 `self._realtime` ✓
        self._enabled = bool(getattr(owner, '_realtime', False))
        self._order_book = None
        self._order_book_on = False
        self._depth_on = False
        self._depth_ready = False
        #: 持仓线开关：默认**开** —— 只要该合约有持仓就显示持仓线 ✓
        #: （用户下单后应自动出现，不需要先手动打开）
        self._position_line_on = True
        self._position_line = None

    # ------------------------------------------------------------------ 内部
    def _chart(self):
        return self._owner.chart

    def _strategy(self):
        return self._owner._strategy_obj()

    def _contract(self) -> str:
        return getattr(self._owner.window, 'contract', '') or ''

    def _kline(self):
        return current_kline(self._strategy(), self._contract())

    def _quote(self):
        kline = self._kline()
        try:
            return getattr(kline, 'quote', None) if kline is not None else None
        except Exception:                                        # noqa: BLE001
            return None

    def _api(self):
        """当前天勤 ``TqApi``（没连天勤就返回 None ✓）。"""
        try:
            return self._owner._first_api()
        except Exception:                                        # noqa: BLE001
            return None

    def _symbol(self) -> str:
        """当前合约的天勤 symbol（``SHFE.rb2610`` / ``DCE.i2609`` ✓）。"""
        kline = self._kline()
        return str(getattr(kline, 'symbol', '') or '')

    def _trade_account(self):
        """下单 / 查持仓要用的**账户实例**。

        ⚠ 天勤**多账户**（``TqApi(TqMultiAccount([...]))``）下，
        ``insert_order`` / ``TargetPosTask`` / ``get_position`` **必须**显式传
        ``account=``，否则报「多账户模式下，需要指定账户实例 account」✗。
        这里优先取**模拟账号**（TqKq / TqSim，用户下的就是模拟单 ✓），没有就取
        多账户里的第一个；单账户直接用它自己 ✓。
        """
        account = getattr(self._api(), '_account', None)
        if account is None:
            return None
        accounts = getattr(account, '_account_list', None)
        if accounts:
            for item in accounts:
                name = type(item).__name__
                if 'Sim' in name or 'Kq' in name:
                    return item
            return accounts[0]
        return account

    def _position(self):
        """当前合约持仓（多账户也带上 ``account`` ✓）。"""
        api = self._api()
        symbol = self._symbol()
        account = self._trade_account()
        if api is not None and symbol:
            try:
                return api.get_position(symbol, account=account)
            except Exception as error:                           # noqa: BLE001
                print(f'[live_tools] 读持仓失败（{symbol}）: {error!r}')
        kline = self._kline()
        try:
            return getattr(kline, 'position', None) if kline is not None else None
        except Exception:                                        # noqa: BLE001
            return None

    def _net(self) -> int:
        return int(_num(getattr(self._position(), 'pos', 0)) or 0)

    # -------------------------------------------------------------- 买卖盘
    def toggle_order_book(self, on: bool) -> None:
        self._order_book_on = bool(on)
        if not on:
            self._remove_order_book()
            return
        self._ensure_order_book()
        self.refresh()

    def _ensure_order_book(self) -> None:
        if self._order_book is not None:
            return
        chart = self._chart()
        try:
            chart.run_script(
                "window.__minibtChartHandler = "
                + str(getattr(chart, 'id', 'null')) + ";\nnull")
            chart.run_script(_ORDER_BOOK_HOST_JS)
            from pylightcharts.panels import OrderTicket
            # ★ 回调**不进面板自带的 ``_bind_callback``**：它拿 ``self.id``
            #   （形如 ``window.abcd1234``）当 handler key，某些桥下取不到 →
            #   点了没反应 ✗。这里跟二级报价按钮一样，用一个**固定名字**直连 ✓
            self._order_book = OrderTicket(
                chart.win,
                container="document.getElementById('__minibtOrderTicketHost')")
            chart.win.handlers['minibt_order_book'] = self._on_order_book_side
            # 买卖盘要能**点到**：祖先 .legend 是 ``pointer-events:none``（覆盖层
            # 默认穿透），不显式开 auto 的话点击全落到 K 线画布上 → “点了没反应”✗；
            # 同时抬高 z-index，别被蜡烛画布盖住 ✓。
            self._order_book.run_script(
                f'{self._order_book.id}.callbackName = "minibt_order_book";'
                f'(function(){{var el={self._order_book.id}.element;'
                f'if(el){{el.style.pointerEvents="auto";'
                f'el.style.position="relative";el.style.zIndex="4000";}}}})();')
        except Exception as error:                               # noqa: BLE001
            print('[live_tools] 买卖盘创建失败:', error)
            self._order_book = None

    def _remove_order_book(self) -> None:
        if self._order_book is not None:
            try:
                self._order_book.delete()
            except Exception:                                    # noqa: BLE001
                pass
            self._order_book = None

    def _on_order_book_side(self, side: str) -> None:
        """买卖盘点击：用 **``TargetPosTask.set_target_volume``** 交易 ✓。

        多仓时左侧=平仓（target 0），空仓时左侧=卖一（net-1）；右侧反之。
        """
        symbol = self._symbol()
        if not symbol:
            print('[live_tools] 买卖盘下单失败：无合约')
            return
        print(f'[live_tools] 买卖盘点击: {side}（{symbol}）')
        net = self._net()
        is_sell = str(side).lower().startswith('sell')
        if is_sell:
            target = 0 if net > 0 else net - 1
        else:
            target = 0 if net < 0 else net + 1
        try:
            self._target_task(self._api(), symbol).set_target_volume(target)
        except Exception as error:                               # noqa: BLE001
            print('[live_tools] 买卖盘下单失败:', error)

    def _target_task(self, api, symbol):
        """``TargetPosTask``（**带 account** —— 多账户必须 ✓；单例，重复取回同一个）。

        ``TqObjs.TargetPosTask`` 是一个**实例**（不是工厂）—— 所以只能
        ``TargetPosTask(api, symbol, account=...)`` 新建，或直接拿那个实例；
        千万不能当函数调 ✗（旧代码 ``kline.TargetPosTask(target)`` 就报
        ``'TargetPosTask' object is not callable`` → 点了没反应）。
        """
        account = self._trade_account()
        if api is not None and account is not None:
            from tqsdk import TargetPosTask
            return TargetPosTask(api, symbol, account=account)
        # 没接上天勤 api（测试 / 回测）：拿 ``TqObjs`` 上**已经建好**的那个实例 ✓
        kline = self._kline()
        try:
            tqobj = (getattr(kline, '_tqobjs', None) or {}).get(symbol)
        except Exception:                                        # noqa: BLE001
            tqobj = None
        task = getattr(tqobj, 'TargetPosTask', None)
        if task is not None:
            return task
        raise RuntimeError('未连接天勤，无法下单')

    def _sync_order_book(self) -> None:
        if self._order_book is None:
            return
        quote = self._quote()
        if quote is None:
            return
        bid = _num(getattr(quote, 'bid_price1', None))
        ask = _num(getattr(quote, 'ask_price1', None))
        try:
            self._order_book.set_data(sell=bid, buy=ask)
        except Exception:                                        # noqa: BLE001
            pass
        net = self._net()
        try:
            self._order_book.set_labels(
                sell='平仓' if net > 0 else '卖出',
                buy='平仓' if net < 0 else '买入')
        except Exception:                                        # noqa: BLE001
            pass

    # -------------------------------------------------------------- 持仓线
    def toggle_position_line(self, on: bool) -> None:
        self._position_line_on = bool(on)
        if not on:
            self._remove_position_line()
        else:
            self.refresh()

    def _remove_position_line(self) -> None:
        line = self._position_line
        self._position_line = None
        if line is not None:
            try:
                line.delete()
            except Exception:                                    # noqa: BLE001
                pass

    def _sync_position_line(self) -> None:
        if not self._position_line_on:
            return
        pos = self._position()
        if pos is None:
            self._remove_position_line()
            return
        net = self._net()
        if net == 0:
            self._remove_position_line()
            return
        price = None
        for attr in (('open_price_long', 'open_price_short')
                     if net > 0 else ('open_price_short', 'open_price_long')):
            price = _num(getattr(pos, attr, None))
            if price:
                break
        if not price:
            return
        profit = _num(getattr(pos, 'float_profit', None))
        side = '多' if net > 0 else '空'
        title = f'{side}仓{abs(net)}手'
        if profit is not None:
            title += f" {'盈利' if profit >= 0 else '亏损'}{profit:+.2f}"
        chart = self._chart()
        try:
            if self._position_line is None:
                self._position_line = chart.horizontal_line(
                    float(price), color='#ef5350', width=2, style='dashed',
                    text=title, axis_label_visible=False, label_visible=True)
                print(f'[live_tools] 持仓线已画: {title} @ {price}')
            else:
                self._position_line.update(float(price))
                self._position_line.options(color='#ef5350', width=2,
                                            style='dashed', text=title,
                                            axis_label_visible=False,
                                            label_visible=True)
        except Exception as error:                               # noqa: BLE001
            print('[live_tools] 持仓线失败:', error)

    # -------------------------------------------------- 二级报价 + 简易下单
    def toggle_depth(self, on: bool) -> None:
        self._depth_on = bool(on)
        self._ensure_depth()
        try:
            self._chart().run_script(
                'window.__minibtDepth && window.__minibtDepth.show('
                + json.dumps(bool(on)) + ')')
        except Exception:                                        # noqa: BLE001
            pass
        if on:
            self.refresh()

    def _ensure_depth(self) -> None:
        if self._depth_ready:
            return
        try:
            self._chart().win.handlers['minibt_depth_order'] = (
                self._on_depth_order)
            self._chart().win.handlers['minibt_depth_market'] = (
                self._on_depth_market)
        except Exception as error:                               # noqa: BLE001
            print('[live_tools] 二级报价回调注册失败:', error)
        try:
            self._chart().run_script(_DEPTH_JS)
            self._depth_ready = True
        except Exception as error:                               # noqa: BLE001
            print('[live_tools] 二级报价注入失败:', error)

    def _on_depth_order(self, action, price, volume) -> None:
        """简易下单（限价）：``long`` / ``short`` / ``close`` 按价格框价格 ✓。"""
        try:
            number = float(price)
        except (TypeError, ValueError):
            number = None
        try:
            lots = max(1, int(float(volume)))
        except (TypeError, ValueError):
            lots = 1
        message = self._place(str(action), number, lots)
        self._set_depth_status(message)

    def _on_depth_market(self, action, volume) -> None:
        """市价操作：``close_market`` / ``reverse`` / ``cancel`` ✓。"""
        name = str(action or '').lower()
        if name in ('cancel', 'cancel_all', 'cancel_orders'):
            # 撤单不需要手数（当前合约**所有**未成交委托全撤 ✓）
            self._set_depth_status(self._cancel_orders())
            return
        try:
            lots = max(1, int(float(volume)))
        except (TypeError, ValueError):
            lots = 1
        message = self._place_market(name, lots)
        self._set_depth_status(message)

    def _cancel_orders(self) -> str:
        """撤销**当前合约所有未成交委托**（用户反馈：挂单后无法撤单 ✗）。"""
        api = self._api()
        symbol = self._symbol()
        if api is None or not symbol:
            return '当前合约无法撤单'
        account = self._trade_account()
        try:
            orders = api.get_order(account=account)
        except Exception as error:                               # noqa: BLE001
            return f'撤单失败：{error}'
        cancelled = 0
        failed: list = []
        for item in _records(orders):
            if not _same_instrument(item, symbol):
                continue
            order_id = str(_field(item, 'order_id') or '')
            left = _num(_field(item, 'volume_left'))
            status = str(_field(item, 'status') or '').upper()
            if not order_id:
                continue
            # 只撤还有未成交手数的挂单（已完 / 已撤的跳过 ✓）
            if not (left and left > 0) and status not in ('ALIVE', 'PENDING',
                                                          'OPEN'):
                continue
            try:
                api.cancel_order(order_id, account=account)
                cancelled += 1
            except Exception as error:                           # noqa: BLE001
                failed.append(f'{order_id}: {error}')
        if failed:
            return (f'已撤 {cancelled} 笔，{len(failed)} 笔失败：'
                    f'{failed[0]}')
        if cancelled == 0:
            return '当前合约没有可撤的挂单'
        return f'已撤单 {cancelled} 笔'

    def _set_depth_status(self, message: str) -> None:
        try:
            self._chart().run_script(
                'window.__minibtDepth && window.__minibtDepth.status('
                + json.dumps(str(message)) + ')')
        except Exception:                                        # noqa: BLE001
            pass

    def _sync_depth(self) -> None:
        if not self._depth_on or not self._depth_ready:
            return
        quote = self._quote()
        if quote is None:
            return
        kline = self._kline()
        name = (str(getattr(kline, 'name', '') or '')
                or str(getattr(quote, 'instrument_name', '') or ''))
        data: dict = {
            'symbol': str(getattr(quote, 'symbol', '') or ''),
            'name': name,
        }
        for index in range(1, 6):
            for key in ('ask_price', 'ask_volume', 'bid_price', 'bid_volume'):
                data[f'{key}{index}'] = _num(
                    getattr(quote, f'{key}{index}', None))
        tick = _num(getattr(quote, 'price_tick', None))
        precision = _tick_decimals(tick) if tick else 2
        data['tick'] = tick
        data['precision'] = precision
        last = _num(getattr(quote, 'last_price', None))
        pre = _num(getattr(quote, 'pre_settlement', None))
        data['last'] = last
        data['pre_settlement'] = pre
        if last is not None and pre:
            data['change'] = round(last - pre, precision)
            data['change_pct'] = round((last - pre) / pre * 100, 2)
        else:
            data['change'] = None
            data['change_pct'] = None
        for key in ('settlement', 'open', 'highest', 'lowest',
                    'volume', 'open_interest'):
            data[key] = _num(getattr(quote, key, None))
        # 持仓 / 委托 / 历史成交（**只针对当前合约**，见 `_account_rows` ✓）
        data['positions'] = self._account_rows('pos', precision)
        data['orders'] = self._account_rows('ord', precision)
        data['trades'] = self._account_rows('trd', precision)
        try:
            self._chart().run_script(
                'window.__minibtDepth && window.__minibtDepth.update('
                + json.dumps(data) + ')')
        except Exception:                                        # noqa: BLE001
            pass

    def _account_rows(self, kind: str, precision: int) -> list:
        """二级报价下方 tabs 的数据（**只针对当前合约** ✓）：

        * ``pos``：买卖 / 总仓 / 持仓均价 / 盈亏
        * ``ord``：买卖 / 开平 / 手 / 未成交
        * ``trd``：买卖 / 开平 / 成交价 / 手

        每行 = ``{'direction': '买'/'卖', 'cells': [已格式化好的文本, ...]}``
        （Python 侧先格式化，JS 只管画表 ✓）。天勤返回的是**全账户**数据，
        这里按 ``instrument_id == 当前合约`` 过滤 ✓。
        """
        api = self._api()
        symbol = self._symbol()
        if not symbol:
            return []
        account = self._trade_account()
        if kind == 'pos':
            pos = self._position()
            if pos is None:
                return []
            rows = []
            for side, vol_keys, avg_keys, profit_keys in (
                    ('买', _LONG_VOLUME, _LONG_AVG, _LONG_PROFIT),
                    ('卖', _SHORT_VOLUME, _SHORT_AVG, _SHORT_PROFIT)):
                volume = _num(_field_any(pos, vol_keys))
                if not volume or volume <= 0:
                    continue
                rows.append({'direction': side, 'cells': [
                    side, _fmt_int(volume),
                    _fmt_num(_field_any(pos, avg_keys), precision),
                    _fmt_signed(_field_any(pos, profit_keys), 2)]})
            return rows
        if kind == 'ord':
            if api is None:
                return []
            try:
                orders = api.get_order(account=account)
            except Exception:                                    # noqa: BLE001
                return []
            rows = []
            for item in _records(orders):
                if not _same_instrument(item, symbol):
                    continue
                direction = _dir_text(_field(item, 'direction'))
                rows.append({'direction': direction, 'cells': [
                    direction, _off_text(_field(item, 'offset')),
                    _fmt_int(_field(item, 'volume_orign')),
                    _fmt_int(_field(item, 'volume_left')),
                    _fmt_num(_field(item, 'limit_price'), precision)]})
            return rows
        if kind == 'trd':
            if api is None:
                return []
            try:
                trades = api.get_trade(account=account)
            except Exception:                                    # noqa: BLE001
                return []
            rows = []
            for item in _records(trades):
                if not _same_instrument(item, symbol):
                    continue
                direction = _dir_text(_field(item, 'direction'))
                rows.append({'direction': direction, 'cells': [
                    direction, _off_text(_field(item, 'offset')),
                    _fmt_num(_field(item, 'price'), precision),
                    _fmt_int(_field(item, 'volume'))]})
            return rows
        return []

    # -------------------------------------------------------------- 下单
    def _place(self, action: str, price, volume: int) -> str:
        """限价单（``insert_order``，**按价格框价格**）：

        ``long`` = 开多（BUY/OPEN）；``short`` = 开空（SELL/OPEN）；
        ``close`` = 平仓（多头 → SELL/CLOSE，空头 → BUY/CLOSE）。
        """
        kline = self._kline()
        if kline is None:
            return '当前合约无 K 线，无法下单'
        action = str(action or '').lower()
        net = self._net()
        if price is None:
            return '请输入价格'
        try:
            if action == 'close':
                if net == 0:
                    return '当前无持仓'
                # 平仓单：多头挂卖平、空头挂买平（都按价格框的限价 ✓）
                direction = 'SELL' if net > 0 else 'BUY'
                kline.insert_order(direction=direction, offset='CLOSE',
                                   volume=min(abs(net), volume),
                                   limit_price=float(price),
                                   account=self._trade_account())
                return ('多头平仓' if net > 0 else '空头平仓') + '（限价）已提交'
            if action in ('short', 'sell'):
                kline.insert_order(direction='SELL', offset='OPEN',
                                   volume=int(volume) or 1,
                                   limit_price=float(price),
                                   account=self._trade_account())
                return '开空（限价）已提交'
            kline.insert_order(direction='BUY', offset='OPEN',
                               volume=int(volume) or 1,
                               limit_price=float(price),
                               account=self._trade_account())
            return '开多（限价）已提交'
        except Exception as error:                               # noqa: BLE001
            return f'下单失败：{error}'

    def _place_market(self, action: str, volume: int) -> str:
        """市价操作（``TargetPosTask``）：市价平仓 / 反手 / 部分平 ✓。"""
        kline = self._kline()
        symbol = self._symbol()
        if kline is None or not symbol:
            return '当前合约无法下单'
        net = self._net()
        action = str(action or '').lower()
        if action == 'close_market':
            target = 0
        elif action == 'reverse':
            if net == 0:
                return '当前无持仓，无法反手'
            target = -net
        elif action == 'partial':
            if net == 0:
                return '当前无持仓'
            step = min(abs(net), max(1, volume))
            target = net - (step if net > 0 else -step)
        else:
            return '未知操作'
        try:
            self._target_task(self._api(), symbol).set_target_volume(target)
        except Exception as error:                               # noqa: BLE001
            return f'下单失败：{error}'
        return {'close_market': '市价平仓已提交', 'reverse': '反手已提交',
                'partial': '部分平仓已提交'}.get(action, '已提交')

    # -------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        """每拍更新（只更新**显示中**的面板；隐藏的不更新 ✓）。

        非实时图表（回测 / 回放 / ``btplot`` 静态图）**直接跳过**：这些图没有
        天勤账户，策略里的持仓只是回测出来的，不该在图上画实时持仓线 ✗。
        """
        if not self._enabled:
            return
        try:
            self._sync_order_book()
        except Exception:                                        # noqa: BLE001
            pass
        try:
            self._sync_position_line()
        except Exception:                                        # noqa: BLE001
            pass
        try:
            self._sync_depth()
        except Exception:                                        # noqa: BLE001
            pass

    def on_contract_changed(self) -> None:
        """切合约：清掉持仓线（下一拍按新合约重建 ✓）。"""
        self._remove_position_line()
        self.refresh()
