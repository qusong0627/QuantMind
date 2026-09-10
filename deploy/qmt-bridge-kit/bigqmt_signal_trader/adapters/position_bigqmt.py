"""Big QMT position and asset adapters."""

from ..account_type_map import account_type_for
from ..code_utils import normalize_stock_code
from ..models import AssetSnapshot, PositionSnapshot, PositionStatisticsSnapshot


def _attr(obj, names, default=None):
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _float_or_none(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Candidate ThinkTrader field names on the ACCOUNT row of get_trade_detail_data.
# The MiniQMT SDK only documents the normalized name (XtAsset.frozen_cash); the
# big QMT ACCOUNT struct is a different surface and brokers vary, so probe the
# plausible spellings the way cash/total_asset already do.
_FROZEN_CASH_FIELDS = (
    "m_dFrozenCash",
    "m_dFrozen",
    "m_dFrozenBalance",
    "m_dFrozenMargin",
    "frozen_cash",
    "frozen",
)
_MARKET_VALUE_FIELDS = (
    "m_dInstrumentValue",
    "m_dStockValue",
    "m_dMarketValue",
    "market_value",
)

# Printed once per process when the frozen field is not found, listing what the
# row actually carries. Guessing a field name and shipping it unverified is how
# the order-direction bug happened; this makes the real name self-reporting.
_missing_field_reported = set()


def _report_missing_field(label, row, candidates):
    if label in _missing_field_reported:
        return
    _missing_field_reported.add(label)
    try:
        available = sorted(name for name in dir(row) if name.startswith("m_"))
    except Exception:
        available = []
    print(
        "[bigqmt_asset] %s not found (tried %s); ACCOUNT row exposes: %s"
        % (label, ", ".join(candidates), ", ".join(available) or "<none>")
    )


# Big QMT reports an instrument's market as the 迅投简称 (XunTou short) token on
# POSITION/ORDER/DEAL rows. The same token is appended as the ContextInfo code
# suffix (a2609.DF, rb2401.SF, 000001.SZ, 00700.HGT).
# Stock/mutual-fund/HK-Connect markets: case-insensitive, unified via normalize.
_STOCK_EXCHANGE_TOKENS = frozenset({"SH", "SZ", "BJ", "HK", "HGT", "SGT"})
# Futures XunTou-short exchange tokens: only concatenate the suffix. The symbol
# must follow each exchange's canonical naming and is case-sensitive
# (AP401.ZF upper-case / rb2401.SF lower-case, not interchangeable), so never
# rewrite its case.
_FUTURES_EXCHANGE_TOKENS = frozenset({"IF", "SF", "DF", "ZF", "INE", "GF"})
# Counter-style (display) futures exchange IDs also seen on POSITION rows.
# Their symbol carries no suffix; return the raw code as a stable key.
_FUTURES_EXCHANGES = frozenset({
    "SHFE", "DCE", "CZCE", "ZCE", "CFFEX", "INE", "GFEX", "GZFE",
})


def _full_code(instrument_id, exchange_id):
    if instrument_id is None:
        return ""
    raw = str(instrument_id).strip()
    if not raw:
        return ""
    market = str(exchange_id or "").strip().upper()
    if "." in raw:
        return normalize_stock_code(raw)
    # Futures: decide purely from the exchange field (no code-shape guessing).
    # Symbol is case-sensitive and preserved as-is; XunTou-short tokens get the
    # suffix appended, counter display IDs return the raw bare key.
    if market in _FUTURES_EXCHANGE_TOKENS:
        return "%s.%s" % (raw, market)
    if market in _FUTURES_EXCHANGES:
        # Unexpected path: POSITION rows normally carry the XunTou-short token
        # (DF/SF/...), so a display ID here signals a structure/vendor mismatch.
        # Surface it so the caller can tell the difference from a genuine
        # futures row that carries the XunTou-short token.
        raise ValueError(
            f"[bigqmt_code] futures exchange reported a display ID "
            f"'{market}' for instrument '{raw}'; expected the XunTou-short "
            f"token (e.g. 'DF' for DCE), not the counter-style ID"
        )
    # Stock/mutual-fund/HK-Connect: normalize into standard upper-case with suffix.
    if market in _STOCK_EXCHANGE_TOKENS:
        return normalize_stock_code("%s.%s" % (raw, market))
    # Unknown/empty exchange: an instrument is never a futures contract without
    # an exchange, so hand it to the normalizer and let an unrecognizable code
    # raise -- a bare code here means the row itself is malformed.
    return normalize_stock_code(raw)


# One unparsable row must not cost the whole query. _full_code raises on a
# structure it does not recognise (a counter-style futures exchange ID, a
# malformed bare code), and every caller loops over rows without per-row
# protection -- so without this, a single odd row turns "one position missing"
# into "no positions at all", which for a trading system is far worse.
#
# Reported once per (kind, exchange) so a persistently odd row does not flood
# the QMT panel, while still naming what to look at.
_unparsable_rows_reported = set()


def skip_unparsable_row(kind, row, exc):
    """Log an unparsable row once and let the caller skip it."""
    exchange = ""
    instrument = ""
    try:
        exchange = str(_attr(row, ("m_strExchangeID", "exchange_id", "market"), "") or "")
        instrument = str(_attr(row, ("m_strInstrumentID", "instrument_id", "stock_code"), "") or "")
    except Exception:
        pass
    key = (kind, exchange)
    if key in _unparsable_rows_reported:
        return
    _unparsable_rows_reported.add(key)
    print("[bigqmt_code] skipping unparsable %s row (instrument=%r exchange=%r): %s"
          % (kind, instrument, exchange, exc))


class BigQmtPositionProvider:
    def __init__(self, get_trade_detail_data_func, account_type="STOCK"):
        self.get_trade_detail_data = get_trade_detail_data_func
        self.account_type = account_type

    def _resolve_account_type(self, account_id):
        """Per-request account_type: map lookup if configured, else default."""
        return account_type_for(account_id, self.account_type)

    def _require_query_func(self):
        if self.get_trade_detail_data is None:
            raise RuntimeError("get_trade_detail_data is not available in Big QMT runtime")
        return self.get_trade_detail_data

    def get_positions(self, account_id):
        query = self._require_query_func()
        # Let query failures reach the RPC error handler instead of reporting empty positions.
        rows = query(account_id, self._resolve_account_type(account_id), "POSITION") or []
        positions = {}
        for row in rows:
            try:
                code = _full_code(
                    _attr(row, ("m_strInstrumentID", "instrument_id", "stock_code")),
                    _attr(row, ("m_strExchangeID", "exchange_id", "market")),
                )
            except Exception as exc:
                skip_unparsable_row("POSITION", row, exc)
                continue
            positions[code] = PositionSnapshot(
                stock_code=code,
                volume=int(_attr(row, ("m_nVolume", "volume"), 0) or 0),
                available=int(_attr(row, ("m_nCanUseVolume", "available", "can_use_volume"), 0) or 0),
                cost=float(_attr(row, ("m_dOpenPrice", "m_dCostPrice", "cost"), 0.0) or 0.0),
                stock_name=str(_attr(row, ("m_strInstrumentName", "stock_name"), "") or ""),
                market_value=_float_or_none(_attr(row, ("m_dMarketValue", "m_dInstrumentValue", "market_value"))),
                price=_float_or_none(_attr(row, ("m_dLastPrice", "m_dSettlementPrice", "price", "last_price"))),
                open_price=_float_or_none(_attr(row, ("m_dOpenPrice", "m_dCostPrice", "open_price", "cost"))),
                frozen_volume=int(_attr(row, ("m_nFrozenVolume", "frozen_volume"), 0) or 0),
                on_road_volume=int(_attr(row, ("m_nOnRoadVolume", "on_road_volume"), 0) or 0),
                yesterday_volume=int(_attr(row, ("m_nYesterdayVolume", "yesterday_volume"), 0) or 0),
                direction=int(_attr(row, ("m_nDirection", "direction"), 48) or 48),
            )
        return positions

    def get_position_statistics(self, account_id):
        query = self._require_query_func()
        try:
            rows = query(account_id, self._resolve_account_type(account_id), "POSITION_STATISTICS") or []
        except Exception:
            return []
        stats = []
        for row in rows:
            try:
                code = _full_code(
                    _attr(row, ("m_strInstrumentID", "instrument_id", "stock_code")),
                    _attr(row, ("m_strExchangeID", "exchange_id", "market")),
                )
            except Exception as exc:
                skip_unparsable_row("POSITION_STATISTICS", row, exc)
                continue
            stats.append(
                PositionStatisticsSnapshot(
                    account_id=account_id,
                    exchange_id=str(_attr(row, ("m_strExchangeID", "exchange_id", "market"), "") or ""),
                    exchange_name=str(_attr(row, ("m_strExchangeName", "exchange_name"), "") or ""),
                    product_id=str(_attr(row, ("m_strProductID", "product_id"), "") or ""),
                    instrument_id=str(_attr(row, ("m_strInstrumentID", "instrument_id"), "") or ""),
                    instrument_name=str(_attr(row, ("m_strInstrumentName", "instrument_name"), "") or ""),
                    stock_code=code,
                    direction=int(_attr(row, ("m_nDirection", "direction"), 0) or 0),
                    hedge_flag=int(_attr(row, ("m_nHedgeFlag", "hedge_flag"), 0) or 0),
                    position=int(_attr(row, ("m_nPosition", "position"), 0) or 0),
                    yesterday_position=int(_attr(row, ("m_nYestodayPosition", "yesterday_position"), 0) or 0),
                    today_position=int(_attr(row, ("m_nTodayPosition", "today_position"), 0) or 0),
                    can_close_vol=int(_attr(row, ("m_nCanCloseVol", "can_close_vol"), 0) or 0),
                    position_cost=_float_or_none(_attr(row, ("m_dPositionCost", "position_cost"))),
                    avg_price=_float_or_none(_attr(row, ("m_dAvgPrice", "avg_price"))),
                    position_profit=_float_or_none(_attr(row, ("m_dPositionProfit", "position_profit"))),
                    float_profit=_float_or_none(_attr(row, ("m_dFloatProfit", "float_profit"))),
                    open_price=_float_or_none(_attr(row, ("m_dOpenPrice", "open_price"))),
                    used_margin=_float_or_none(_attr(row, ("m_dUsedMargin", "used_margin"))),
                    used_commission=_float_or_none(_attr(row, ("m_dUsedCommission", "used_commission"))),
                    frozen_margin=_float_or_none(_attr(row, ("m_dFrozenMargin", "frozen_margin"))),
                    frozen_commission=_float_or_none(_attr(row, ("m_dFrozenCommission", "frozen_commission"))),
                    instrument_value=_float_or_none(_attr(row, ("m_dInstrumentValue", "instrument_value"))),
                    open_times=int(_attr(row, ("m_nOpenTimes", "open_times"), 0) or 0),
                    open_volume=int(_attr(row, ("m_nOpenVolume", "open_volume"), 0) or 0),
                    cancel_times=int(_attr(row, ("m_nCancelTimes", "cancel_times"), 0) or 0),
                    last_price=_float_or_none(_attr(row, ("m_dLastPrice", "last_price"))),
                    rise_ratio=_float_or_none(_attr(row, ("m_dRiseRatio", "rise_ratio"))),
                    product_name=str(_attr(row, ("m_strProductName", "product_name"), "") or ""),
                    royalty=_float_or_none(_attr(row, ("m_dRoyalty", "royalty"))),
                    expire_date=str(_attr(row, ("m_strExpireDate", "expire_date"), "") or ""),
                    assest_weight=_float_or_none(_attr(row, ("m_dAssestWeight", "assest_weight"))),
                    increase_by_settlement=_float_or_none(
                        _attr(row, ("m_dIncreaseBySettlement", "increase_by_settlement"))
                    ),
                    margin_ratio=_float_or_none(_attr(row, ("m_dMarginRatio", "margin_ratio"))),
                    float_profit_divide_by_used_margin=_float_or_none(
                        _attr(row, ("m_dFloatProfitDivideByUsedMargin", "float_profit_divide_by_used_margin"))
                    ),
                    float_profit_divide_by_balance=_float_or_none(
                        _attr(row, ("m_dFloatProfitDivideByBalance", "float_profit_divide_by_balance"))
                    ),
                    today_profit_loss=_float_or_none(_attr(row, ("m_dTodayProfitLoss", "today_profit_loss"))),
                    yesterday_init_position=int(
                        _attr(row, ("m_nYestodayInitPosition", "yesterday_init_position"), 0) or 0
                    ),
                    frozen_royalty=_float_or_none(_attr(row, ("m_dFrozenRoyalty", "frozen_royalty"))),
                    today_close_profit_loss=_float_or_none(
                        _attr(row, ("m_dTodayCloseProfitLoss", "today_close_profit_loss"))
                    ),
                    close_profit=_float_or_none(_attr(row, ("m_dCloseProfit", "close_profit"))),
                    ft_product_name=str(_attr(row, ("m_strFtProductName", "ft_product_name"), "") or ""),
                    open_cost=_float_or_none(_attr(row, ("m_dOpenCost", "open_cost"))),
                )
            )
        return stats

    def get_asset(self, account_id):
        query = self._require_query_func()
        rows = []
        for detail_type in ("ACCOUNT", "ASSET"):
            try:
                rows = query(account_id, self._resolve_account_type(account_id), detail_type) or []
                if rows:
                    break
            except Exception:
                rows = []
        if not rows:
            return AssetSnapshot(account_id=account_id, cash=None, total_asset=None)

        row = rows[0]
        cash = _attr(row, ("m_dAvailable", "m_dAvailableCash", "available_cash", "cash"))
        total_asset = _attr(row, ("m_dBalance", "m_dAsset", "total_asset", "asset"))
        frozen_cash = _attr(row, _FROZEN_CASH_FIELDS)
        market_value = _attr(row, _MARKET_VALUE_FIELDS)
        if frozen_cash is None:
            _report_missing_field("frozen_cash", row, _FROZEN_CASH_FIELDS)
        if market_value is None and cash is not None and total_asset is not None:
            # Derive only as a last resort. Without frozen_cash this overstates
            # market value by the frozen amount, so subtract it when known.
            market_value = float(total_asset) - float(cash)
            if frozen_cash is not None:
                market_value -= float(frozen_cash)
        return AssetSnapshot(
            account_id=account_id,
            cash=float(cash) if cash is not None else None,
            total_asset=float(total_asset) if total_asset is not None else None,
            frozen_cash=float(frozen_cash) if frozen_cash is not None else None,
            market_value=float(market_value) if market_value is not None else None,
        )
