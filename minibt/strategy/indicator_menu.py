"""指标标签齿轮设置菜单（JS 版）—— 复刻 miniqt 的「指标参数」卡片。

pylightcharts 的 JS 图例负责画齿轮 + 二级菜单（``SeriesCommon.enable_legend_menu``）；
这里负责把 minibt 的 ``IndicatorSpec`` / plotinfo 转成菜单配置，并把菜单回调写回模型：

* **视觉设置**（线型 / 线宽 / 线颜色 / 价格线 / 价格标签）→ 写回 spec，并记到
  ``style_overrides``，图表重建后仍然生效 ✓；
* **参数**（params）→ 调 ``recompute`` 回调（minibt 侧尽力重算并重画）。

两条渲染路径共用本模块：
* ``realtime.py``（btplot / 默认）→ ``IndicatorManager``；
* ``light_chart.py``（``MINIBT_LIGHT_CHART=1``）→ ``Chart``。
"""
from __future__ import annotations

from typing import Any, Optional

#: 与 miniqt ``INDICATOR_LINE_STYLES`` 一致（顺序 = 菜单顺序）
LINE_STYLES = ('solid', 'dotted', 'dashed', 'large_dashed', 'sparse_dotted')

#: minibt ``LineDash`` 的别名 -> 菜单/LWC 能画的 5 种之一 ✓
#: （LWC 只有 5 种，``dotdash``/``dashdot`` 这种“长短交替”只能取最接近的虚线 ✓）
STYLE_ALIASES = {
    'dotdash': 'dashed',
    'dashdot': 'dashed',
    'dashdotdot': 'dashed',
    'dash_dot': 'dashed',
    'dash_dot_dot': 'dashed',
    'long_dash': 'large_dashed',
    'longdash': 'large_dashed',
    'long_dashed': 'large_dashed',
    'large-dash': 'large_dashed',
    'big_dashed': 'large_dashed',
    'sparse_dotted': 'sparse_dotted',
    'sparsedotted': 'sparse_dotted',
    'dot': 'dotted',
    'dashed_line': 'dashed',
    'none': 'solid',
}


def menu_style(style: Any, kind: str = 'Line') -> str:
    """把指标实际用的线型名 -> **菜单能选中的那一种** ✓。

    以前 ``normalize_style`` 只认 ``LINE_STYLES`` 那 5 个名字，其余一律退回
    ``solid`` ✗ —— 于是 ``cci.py`` 里 ``LineDash.dashdot``（长虚线）的 SMA，
    菜单里勾的却是“实线” ✗（用户反馈）。现在：

    * 名字已在列表里 → 原样 ✓；
    * 在 ``STYLE_ALIASES`` 里 → 映射到最接近的一种（与画图时
      ``line_style_value`` 的口径**一致** ✓，所以菜单显示的就是图上真正画的样子 ✓）；
    * 不认识的含 “dash” → 归为 ``dashed`` ✓，其余 → ``solid`` ✓。
    """
    if kind in ('Histogram', 'Candlestick'):
        return 'solid'
    key = str(style).strip().lower().replace('-', '_').replace(' ', '_')
    if key in LINE_STYLES:
        return key
    mapped = STYLE_ALIASES.get(key)
    if mapped:
        return mapped
    return 'dashed' if 'dash' in key else 'solid'

#: 颜色候选（用户也可用取色器自定义）
LINE_COLORS = [
    '#ffffff', '#f23645', '#ff9800', '#ffeb3b', '#4caf50',
    '#00bcd4', '#2196f3', '#9c27b0', '#e91e63', '#9e9e9e',
]

#: 菜单文案（中文，和 miniqt 卡片一致）
MENU_LABELS = {
    'title': '指标设置', 'params': '参数', 'style': '线型', 'width': '线宽',
    'color': '线颜色', 'other': '其它设置', 'confirm': '确认',
    'priceLine': '价格线', 'priceLabel': '价格标签',
}


def normalize_style(style: Any, kind: str) -> str:
    """柱体/蜡烛没有 ``lineStyle``，统一按 ``solid`` 处理。

    其余走 :func:`menu_style`（别名也会映射到菜单里的一种 ✓），
    这样“菜单勾选的”与“图上画的”永远一致 ✓。
    """
    return menu_style(style, kind)


def build_config(spec) -> dict:
    """``IndicatorSpec`` → ``LegendMenu`` 配置。

    ``spec.menu_params`` 为 ``None`` / 空时，菜单自动隐藏「参数」页
    （minibt 目前拿不到指标计算函数的签名，见 ``params_provider``）。

    ★ 线型必须与指标**实际用的**一致（用户反馈：菜单默认项与指标样式不一样 ✗）：
    线型走 :func:`menu_style`（``dashdot`` 这种别名映射到最接近的一种 ✓）。
    颜色保持原样（不追求“默认勾中”—— 指标常用 CSS 色名，与 hex 色板对不上，
    处理起来很绕；用户明确说**可以不选中默认** ✓）。
    """
    name = getattr(spec, 'name', '') or ''
    kind = getattr(spec, 'kind', 'Line') or 'Line'
    return {
        'title': f'{name}指标参数',
        'params': list(getattr(spec, 'menu_params', None) or []),
        'lineStyle': menu_style(getattr(spec, 'line_style', 'solid'), kind),
        'lineWidth': int(getattr(spec, 'line_width', 2) or 2),
        'lineColor': getattr(spec, 'color', None) or '',
        'priceLine': bool(getattr(spec, 'price_line', False)),
        'priceLabel': bool(getattr(spec, 'price_label', False)),
        'isArea': kind == 'Area',
        'showStyle': kind not in ('Histogram', 'Candlestick'),
        'styles': list(LINE_STYLES),
        'colors': list(LINE_COLORS),
        'labels': dict(MENU_LABELS),
    }


def override_key(spec) -> tuple:
    """跨重建的身份：``(source, name)``（同指标的不同线互不影响 ✓）。"""
    return (getattr(spec, 'source', '') or '',
            getattr(spec, 'name', '') or '')


def apply_overrides(spec, overrides: dict) -> None:
    """把之前用户改过的视觉设置套回**新**的 spec（重建后仍生效 ✓）。"""
    data = overrides.get(override_key(spec))
    if not data:
        return
    for field, value in data.items():
        try:
            setattr(spec, field, value)
        except Exception:                                        # noqa: BLE001
            pass


def apply_visual(spec, kind: str, payload: dict) -> bool:
    """把菜单回调的视觉设置写回 spec；``params`` 返回 False 交给上层重算。"""
    if kind == 'style':
        spec.line_style = normalize_style(
            payload.get('value', getattr(spec, 'line_style', 'solid')),
            getattr(spec, 'kind', 'Line'))
    elif kind == 'width':
        try:
            spec.line_width = int(payload.get('value', spec.line_width))
        except (TypeError, ValueError):
            return False
    elif kind == 'color':
        value = payload.get('value') or getattr(spec, 'color', None)
        spec.color = value
        spec.palette_color = False
    elif kind == 'price':
        spec.price_line = bool(payload.get('priceLine',
                                          getattr(spec, 'price_line', False)))
        spec.price_label = bool(payload.get('priceLabel',
                                            getattr(spec, 'price_label', False)))
    else:
        return False
    return True


def override_values(spec, kind: str, payload: dict) -> Optional[dict]:
    """菜单回调 → 要记进 ``style_overrides`` 的字段（``None`` = 不记）。"""
    if kind == 'style':
        return {'line_style': normalize_style(
            payload.get('value', getattr(spec, 'line_style', 'solid')),
            getattr(spec, 'kind', 'Line'))}
    if kind == 'width':
        try:
            return {'line_width': int(payload.get('value', spec.line_width))}
        except (TypeError, ValueError):
            return None
    if kind == 'color':
        return {'color': payload.get('value') or getattr(spec, 'color', None),
                'palette_color': False}
    if kind == 'price':
        return {'price_line': bool(payload.get('priceLine',
                                               getattr(spec, 'price_line', False))),
                'price_label': bool(payload.get('priceLabel',
                                                getattr(spec, 'price_label', False)))}
    return None


def enable_menu(series, config: dict, handler) -> bool:
    """挂齿轮菜单（pylightcharts 没有该 API 时安全跳过）。"""
    func = getattr(series, 'enable_legend_menu', None)
    if not callable(func):
        return False
    try:
        func(config, handler)
    except Exception:                                            # noqa: BLE001
        return False
    return True


def pin_row(series) -> None:
    """固定图例行（没有十字线也常驻显示 ✓）。"""
    func = getattr(series, 'pin_legend_row', None)
    if callable(func):
        try:
            func(True)
        except Exception:                                        # noqa: BLE001
            pass


def enable_collapse(chart) -> None:
    """挂「指标标签收起/展开」按钮（幂等 ✓）。"""
    func = getattr(chart, 'enable_legend_collapse', None)
    if callable(func):
        try:
            func()
        except Exception:                                        # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 指标参数重算（config.indicator_recompute = True 时启用）
# --------------------------------------------------------------------------- #
def recompute_enabled(strategy) -> bool:
    """``config.indicator_recompute`` 是否开启（默认 False）✓。

    开启时顺手打开 ``tobtind`` 的 ``_bt_recompute`` 元信息（普通函数指标重算
    兜底需要它；``BtIndicator`` 类指标不需要）。
    """
    try:
        from ..utils import resolve_indicator_recompute
        enabled = bool(resolve_indicator_recompute(strategy))
    except Exception:                                            # noqa: BLE001
        enabled = bool(getattr(getattr(strategy, 'config', None),
                               'indicator_recompute', False))
    if enabled:
        try:
            from ..indicators import base as _base
            _base.enable_recompute_meta(True)
        except Exception:                                        # noqa: BLE001
            pass
    return enabled


#: 这些是框架的配置项，不是用户指标参数，不进「参数」页
_FRAMEWORK_KEYS = {
    'self', 'cls', 'myself', 'category', 'sname', 'id', 'lines', 'overlap',
    'isplot', 'ismain', 'linestyle', 'signalstyle', 'bandstyle', 'spanstyle',
    'tablestyle', 'vpstyle', 'legend', 'light_chart', 'isindicator',
    'iscustom', '_source_for_ind', 'isresample', 'isreplay',
}


def _jsonable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


#: 菜单**只能编辑**这些类型；``KLine`` / ``DataFrame`` / 自定义对象一律只读 ✗
_EDITABLE_PARAM_TYPES = (bool, int, float, str)


def _is_editable(value) -> bool:
    """参数是否可在「参数」页里输入（``None`` 也允许 —— 可能是个字符串开关）。"""
    return value is None or isinstance(value, _EDITABLE_PARAM_TYPES)


def _display_value(value) -> str:
    """只读参数的**友好展示**（不要把整个 DataFrame repr 塞进输入框 ✗）。"""
    if value is None:
        return ''
    if isinstance(value, _EDITABLE_PARAM_TYPES):
        return str(value)
    for attr in ('symbol', 'contract', 'name', 'sname'):
        text = getattr(value, attr, None)
        if isinstance(text, str) and text:
            return f'{type(value).__name__}({text})'
    return f'<{type(value).__name__}>'


def _type_name(value) -> str:
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, int):
        return 'int'
    if isinstance(value, float):
        return 'float'
    if isinstance(value, str):
        return 'str'
    return 'str'


def _signature_params(dataset) -> tuple:
    """从 ``tobtind`` 记录的 wrapper 签名 + 原始调用还原 ``(参数字典, 位置顺序)``。

    ``ordered`` 是**按位置**传递的参数名顺序 —— 调用侧要靠它把
    ``k.close.sma(30)`` 这种写法还原成 ``length=30``（否则再传一次
    ``length=`` 会 ``got multiple values for argument`` ✗）。
    """
    import inspect

    meta = getattr(dataset, '_bt_recompute', None)
    if not isinstance(meta, dict):
        return {}, []
    wrapper = meta.get('wrapper')
    call = meta.get('call')
    if wrapper is None or not call:
        return {}, []
    func = getattr(wrapper, '__wrapped__', None) or wrapper
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return {}, []
    values: dict = {}
    ordered: list = []
    for name, item in signature.parameters.items():
        if name in _FRAMEWORK_KEYS:
            continue
        if item.kind in (inspect.Parameter.VAR_POSITIONAL,
                         inspect.Parameter.VAR_KEYWORD):
            continue
        if item.kind in (inspect.Parameter.POSITIONAL_ONLY,
                         inspect.Parameter.POSITIONAL_OR_KEYWORD):
            ordered.append(name)
        if item.default is inspect.Parameter.empty:
            continue
        values[name] = item.default
    call_args, call_kwargs = call
    for name, value in (call_kwargs or {}).items():
        if name.startswith('_') or name in _FRAMEWORK_KEYS:
            continue
        values[name] = value
    for index, value in enumerate(tuple(call_args)[1:]):
        if index < len(ordered):
            values[ordered[index]] = value
    return values, ordered


def raw_params(dataset) -> dict:
    """指标的**原始**参数值（``KLine`` / ``DataFrame`` 等对象不转字符串 ✓）。

    优先用 ``BtIndicator`` 实例上的 ``params``；否则用 ``tobtind`` 记录的
    ``wrapper`` 签名 + 原始调用推断；拿不到返回 ``{}``。
    """
    raw = getattr(dataset, 'params', None)
    if isinstance(raw, dict) and raw:
        return {str(name): value for name, value in raw.items()
                if not str(name).startswith('_')}
    return _signature_params(dataset)[0]


def params_for_dataset(dataset) -> list:
    """从 ``dataset`` 推断指标参数（名字 / 当前值 / 类型 / 是否可编辑）。

    ★ 非标量参数（``KLine`` / ``DataFrame`` / 自定义对象）标 ``readonly`` ——
    菜单回传的是**字符串**，若把 ``str(kline)`` 写回参数，指标会拿不到对比合约
    （``SMTDivergences(sym1=...)`` 就是这么丢掉背离线的 ✗）。
    """
    out = []
    for name, value in raw_params(dataset).items():
        editable = _is_editable(value)
        out.append({
            'name': str(name),
            'value': _jsonable(value) if editable else _display_value(value),
            'type': _type_name(value),
            'readonly': not editable,
        })
    return out


def sanitize_params(dataset, values: dict) -> dict:
    """只保留**用户真正改过的标量参数**（对象参数原样保留 ✓）。

    菜单会把「参数」页里**所有**输入框回传一遍，所以必须过滤：

    * 不在原始参数表里的名字 → 丢弃（防注入）；
    * 原始值是对象（``KLine`` / ``DataFrame``）→ **丢弃**，保留原对象；
    * 原始值是 ``None`` → 只有用户填了新内容才接受（空串不覆盖 ✓）；
    * ``bool`` / ``int`` / ``float`` → 按原类型转换，转不了就丢弃。
    """
    reference = raw_params(dataset)
    out: dict = {}
    for name, value in (values or {}).items():
        if name not in reference:
            continue
        original = reference[name]
        if isinstance(original, bool):
            if isinstance(value, bool):
                out[name] = value
                continue
            text = str(value).strip().lower()
            if text in ('true', '1', 'yes', 'on', '开'):
                out[name] = True
            elif text in ('false', '0', 'no', 'off', '关', ''):
                out[name] = False
        elif isinstance(original, int):
            try:
                out[name] = int(float(str(value).strip()))
            except (TypeError, ValueError):
                pass
        elif isinstance(original, float):
            try:
                out[name] = float(str(value).strip())
            except (TypeError, ValueError):
                pass
        elif isinstance(original, str):
            out[name] = '' if value is None else str(value)
        elif original is None:
            text = '' if value is None else str(value).strip()
            if text and text.lower() not in ('none', 'null'):
                out[name] = text
        # else: KLine / DataFrame / 其它对象 → 不可编辑，保留原值 ✓
    return out


def params_equal(current: dict, params: dict) -> bool:
    """参数是否**等价**（对象比对容错：``KLine.__eq__`` 可能返回 DataFrame ✗）。"""
    for key, value in params.items():
        have = current.get(key)
        if have is value:
            continue
        try:
            same = bool(have == value)
        except Exception:                                        # noqa: BLE001
            same = False
        if not same:
            return False
    return True


def _dataset_source(dataset):
    """指标对应的源 K 线（``BtIndicator`` 用 ``_dataset.source_object``）。"""
    meta = getattr(dataset, '_bt_recompute', None)
    if isinstance(meta, dict) and meta.get('source') is not None:
        return meta['source']
    data = getattr(dataset, '_dataset', None)
    return getattr(data, 'source_object', None)


def _copy_id(old, new):
    """让重算出来的新指标沿用旧 ``id``（图表 / 策略引用更稳定 ✓）。"""
    try:
        old_id = getattr(old, 'id', None)
        if old_id is not None:
            new.id = old_id
    except Exception:                                            # noqa: BLE001
        pass
    return new


#: ``PlotInfo`` 里所有**绘图 / 样式**字段 —— 重算后脚本里「建完再改」的部分要保留 ✓
_STYLE_FIELDS = (
    'category', 'isplot', 'legend', 'overlap', 'candlestyle', 'linestyle',
    'signalstyle', 'spanstyle', 'bandstyle', 'vpstyle', 'tablestyle',
    'watermark', 'height', 'color_rules', 'signal_texts',
)


def _plotinfo_style(obj) -> dict:
    """取指标对象的 PlotInfo 样式快照（拿不到返回 ``{}`` ✓）。"""
    info = getattr(obj, '_plotinfo', None)
    if info is None:
        return {}
    return {key: getattr(info, key, None) for key in _STYLE_FIELDS}


def _same_style(left, right) -> bool:
    """样式值是否等价（对象比对容错：``__eq__`` 可能返回 DataFrame ✗）。"""
    if left is right:
        return True
    try:
        return bool(left == right)
    except Exception:                                            # noqa: BLE001
        return False


def style_delta(dataset, baseline) -> dict:
    """脚本里「**建完再改**」的样式 = 旧实例 − 「同参数下新建的基座」✓。

    指标样式有两半：

    * 一半由 **wrapper 根据参数**算（``bull_color`` → ``linestyle``）；
    * 另一半是脚本里**创建之后**手动赋的::

        self.rsi.line_style = LineStyle(line_dash=LineDash.vbar, vbar_base=50.)
        self.ma.line_style  = LineStyle(line_dash=LineDash.dashdot, ...)
        self.cci.span_style += [SpanStyle(0), SpanStyle(-100), SpanStyle(100)]
        self.cci.bandstyle  += BandStyle('cci', 'zero', ...)
        self.cci.signal_style = dict(long_signal=SignalStyle(...), ...)
        self.kline.color_by(self.kline.close >= self.kline.open, ...)

    重算只重建了指标**对象**，这半不会跟着走 → 柱体（vbar）退化成实线 ✗。
    这里用「旧实例 − 同参数基座」把**参数之外**的改动挑出来，再套到新对象上：
    参数驱动的样式仍按**新参数**重算 ✓，脚本样式保留 ✓。

    列表字段（``spanstyle`` / ``bandstyle`` / ``color_rules``）只返回**多出来的
    那几项**，追加时还会按等值去重 ✓。
    """
    old, base = _plotinfo_style(dataset), _plotinfo_style(baseline)
    delta: dict = {}
    for key, value in old.items():
        base_value = base.get(key)
        if _same_style(value, base_value):
            continue
        if isinstance(value, (list, tuple)) and isinstance(base_value, (list, tuple)):
            extra = [item for item in value
                     if not any(_same_style(item, have) for have in base_value)]
            if extra:
                delta[key] = extra
        else:
            delta[key] = value
    return delta


def apply_style(dataset, delta: dict) -> None:
    """:func:`style_delta` 的结果套到新指标上（**逐项 merge**，不清重新值 ✓）。"""
    info = getattr(dataset, '_plotinfo', None)
    if info is None or not delta:
        return
    for key, value in delta.items():
        try:
            current = getattr(info, key, None)
            if isinstance(current, dict) and isinstance(value, dict):
                current.update(value)
            elif isinstance(current, list) and isinstance(value, (list, tuple)):
                for item in value:
                    if not any(_same_style(item, have) for have in current):
                        current.append(item)
            else:
                setattr(info, key, value)
        except Exception:                                        # noqa: BLE001
            pass


def _build(source, dataset, cls, params):
    """建一个新指标实例：优先 wrapper（参数驱动的动态开关 ✓），否则构造类。"""
    new = _wrapper_call(source, dataset, cls, params)
    if new is None:
        try:
            new = cls(source, **params)
        except Exception:                                        # noqa: BLE001
            new = None
    return new


def _wrapper_call(source, dataset, cls, params):
    """重新跑一遍 ``source.<accessor>.<Name>(**params)``（拿回 wrapper 的动态开关）。

    ``BtIndicator`` 通常由 ``kline.tradingview.SMTDivergences(...)`` 这类 wrapper
    包一层，wrapper 会**根据参数动态**算 ``isplot`` / ``overlap`` / ``linestyle``
    等绘图开关（例：传了对比合约就把 ``pivot_high`` / ``pivot_low`` 关掉）。
    直接构造类会退回类上的**静态** ``isplot`` ✗。找不到对应 wrapper 时返回
    ``None``（上层回退到构造类 ✓）。
    """
    name = None
    for attr in ('sname', 'ind_name', 'name'):
        text = getattr(dataset, attr, None)
        if isinstance(text, str) and text:
            name = text
            break
    name = name or getattr(cls, '__name__', None)
    if not name:
        return None
    groups: list = []
    leaf = (getattr(cls, '__module__', '') or '').rsplit('.', 1)[-1]
    for group in (leaf, 'tradingview', 'btind', 'instruments', 'ta', 'core'):
        if group and group not in groups:
            groups.append(group)
    for group in groups:
        accessor = getattr(source, group, None)
        if accessor is None:
            continue
        func = getattr(accessor, name, None)
        if not callable(func):
            continue
        try:
            return func(**params)
        except Exception as error:                               # noqa: BLE001
            print(f'[indicator_menu] {name} wrapper 重算失败: {error!r}')
            return None
    return None


def recompute_indicator(dataset, values: dict):
    """按新参数重算 ``dataset``，返回新指标对象（失败返回 ``None``）。

    优先级：

    1. **重跑 wrapper**（``source.tradingview.<Name>(**params)``）—— 会重算
       动态绘图开关（这是 ``SMTDivergences`` 改参数后 ``pivot_high`` /
       ``pivot_low`` 突然冒出来的根因 ✗）；
    2. 直接构造 ``BtIndicator`` 类（``_bt_indicator_cls`` + ``params``）；
    3. ``tobtind`` 普通函数指标（用记录的 ``wrapper`` + 原始调用重跑）。

    ``values`` 先过 :func:`sanitize_params`：**只接受标量改动**，
    ``KLine`` / ``DataFrame`` 这类对比合约参数原样保留 ✓。
    整个流程不抛异常。
    """
    values = sanitize_params(dataset, values)
    cls = getattr(dataset, '_bt_indicator_cls', None)
    source = _dataset_source(dataset)
    if cls is not None and source is not None:
        old_params = dict(getattr(dataset, 'params', None) or {}) \
            or raw_params(dataset)
        current = dict(getattr(dataset, 'params', None) or {})
        current.update(values)
        new = _build(source, dataset, cls, current)
        if new is not None:
            # ★ 保留脚本里「建完再改」的样式（vbar / span / band / signal /
            #   color_by …）：先建一个**同参数基座**，把参数之外的样式改动
            #   挑出来再套到新实例上 ✓
            if values:
                baseline = _build(source, dataset, cls, old_params)
                if baseline is not None:
                    apply_style(new, style_delta(dataset, baseline))
            return _copy_id(dataset, new)
    meta = getattr(dataset, '_bt_recompute', None)
    if not isinstance(meta, dict):
        return None
    wrapper = meta.get('wrapper')
    call = meta.get('call')
    if wrapper is None or not call:
        return None
    call_args, call_kwargs = call
    call_args = tuple(call_args)
    base_values, ordered = _signature_params(dataset)
    base = dict(getattr(dataset, 'params', None) or {}) or base_values
    merged = dict(base)
    merged.update(values)
    # 原始调用里可能把参数**按位置**传了（``k.close.sma(30)`` → ``(source, 30)``）；
    # 若再把 ``length=42`` 当 kwarg 传进去就会 ``got multiple values for
    # argument 'length'`` ✗ —— 所以只留 **source** 在位置上，参数全走 kwarg ✓
    head = call_args[:1]
    rest = call_args[1 + len(ordered):]

    def _call(params):
        return wrapper(*head, *rest, **params)

    try:
        old_id = getattr(dataset, 'id', None)
        if old_id is not None:
            merged.setdefault('id', old_id)
    except Exception:                                            # noqa: BLE001
        pass
    try:
        new = _call(merged)
    except Exception:                                            # noqa: BLE001
        return None
    if new is None:
        return None
    if values and base:
        try:
            baseline = _call(base)
        except Exception:                                        # noqa: BLE001
            baseline = None
        if baseline is not None:
            apply_style(new, style_delta(dataset, baseline))
    return _copy_id(dataset, new)
