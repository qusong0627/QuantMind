"""行情字段字典与各源归一化。

**唯一出处**：桥对外所有"统一行情"字段都在 `FIELD_DEFS` 里定义——含义、单位、
以及"哪些源能提供"。新增源/字段只改这里；网页的字段字典也读它。

三个源（实际取值位置）：
- tdx    : 通达信客户端快照  get_market_snapshot  → Now/LastClose/Open/Max/Min/
           Volume/Amount/Buyp/Buyv/Sellp/Sellv/Inside/Outside
- aidata : 通达信付费快照    get_market_snapshot  → 字段同上（同源不同通道）
- ths    : 同花顺            /api/quote + /api/depth(10) + /api/quotes_ext
           quote: price/pre_close/open/high/low/volume
           depth: bids/asks [{level, price, volume}]（5 或 10 档）
           ext  : main_inflow(元) / dde_main(亿) / market_cap(元) / seal_amount(元)
                  / chg_pct / auction_chg_pct / auction_amount / amount / speed_4m
"""
from __future__ import annotations

from typing import Any

#: (字段, 中文, 单位, 说明, 可提供的源按优先级)
FIELD_DEFS: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    ("last", "最新价", "元", "最后一笔成交价", ("ths", "aidata", "tdx", "qmt")),
    ("prev_close", "昨收", "元", "前一交易日收盘价", ("ths", "aidata", "tdx", "qmt")),
    ("open", "今开", "元", "当日第一笔成交价", ("ths", "aidata", "tdx", "qmt")),
    ("high", "最高", "元", "当日最高价", ("ths", "aidata", "tdx", "qmt")),
    ("low", "最低", "元", "当日最低价", ("ths", "aidata", "tdx", "qmt")),
    ("volume", "成交量", "手（源而异）", "当日累计成交量；三源单位可能不同，跨源比较前先对齐",
     ("ths", "aidata", "tdx", "qmt")),
    ("amount", "成交额", "元", "当日累计成交金额；TDX 原始为万元，聚合层已 ×10000 归一为元",
     ("ths", "aidata", "tdx", "qmt")),
    ("change_pct", "涨跌幅", "%", "相对昨收；源内直取或本地计算",
     ("ths", "aidata", "tdx", "qmt")),
    ("bids", "买盘", "档位数组（volume=手）", "[{level,price,volume}]；TDX/QMT 5 档，THS 可 10 档",
     ("ths", "aidata", "tdx", "qmt")),
    ("asks", "卖盘", "档位数组（volume=手）", "[{level,price,volume}]；TDX/QMT 5 档，THS 可 10 档",
     ("ths", "aidata", "tdx", "qmt")),
    ("inside", "内盘", "手", "主动卖成交量（TDX 快照 Inside）", ("aidata", "tdx")),
    ("outside", "外盘", "手", "主动买成交量（TDX 快照 Outside）", ("aidata", "tdx")),
    ("main_inflow", "主力净流入", "元", "THS 0xc4 金额表 dt250", ("ths",)),
    ("dde_main", "DDE 主力", "亿", "THS 0xc4 金额表 dt248", ("ths",)),
    ("market_cap", "总市值", "元", "THS 0xc4 金额表 dt202", ("ths",)),
    ("seal_amount", "封单额", "元", "THS 金额表 dt44", ("ths",)),
    ("auction_chg_pct", "竞价涨幅", "%", "THS 统一列表派生", ("ths",)),
    ("auction_amount", "竞价金额", "元", "THS 统一列表派生", ("ths",)),
    ("speed_4m", "4分钟涨速", "%", "THS 统一列表派生", ("ths",)),
]

FIELD_ORDER = [f[0] for f in FIELD_DEFS]

SOURCE_DEFS: dict[str, dict] = {
    "tdx": {
        "label": "通达信（免费客户端）",
        "endpoint": "TQ 17709 get_market_snapshot（经桥 /api/v1/tdx/call）",
        "fields": ["last", "prev_close", "open", "high", "low", "volume",
                   "amount", "change_pct", "bids", "asks", "inside", "outside"],
        "cost": "0",
        "notes": "5 档；未订阅/未查看的标的可能滞后",
    },
    "aidata": {
        "label": "通达信（TdxAiData 付费）",
        "endpoint": "tdxaidata get_market_snapshot（桥 /api/v1/aidata/call）",
        "fields": ["last", "prev_close", "open", "high", "low", "volume",
                   "amount", "change_pct", "bids", "asks", "inside", "outside"],
        "cost": "按 200 条/词元",
        "notes": "与 tdx 同源不同通道；批量取更省",
    },
    "ths": {
        "label": "同花顺（thspypc 旁路）",
        "endpoint": "/api/quote + /api/depth(10) + /api/quotes_ext",
        "fields": ["last", "prev_close", "open", "high", "low", "volume",
                   "amount", "change_pct", "bids", "asks", "main_inflow",
                   "dde_main", "market_cap", "seal_amount", "auction_chg_pct",
                   "auction_amount", "speed_4m"],
        "cost": "0（需同花顺账号）",
        "notes": "10 档；资金面唯一来源；需扫码/账号登录",
    },
    "qmt": {
        "label": "迅投 QMT（同机管道桥）",
        "endpoint": "伴生桥 127.0.0.1:17720 get_full_tick（桥 /api/v1/tdx/call source=qmt）",
        "fields": ["last", "prev_close", "open", "high", "low", "volume",
                   "amount", "change_pct", "bids", "asks"],
        "cost": "0（需 QMT 登录）",
        "notes": "模拟/实盘终端自带行情；5 档；无内外盘字段；仅本机可用",
    },
}


def _f(value: Any):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_tdx(raw: dict) -> dict:
    """TDX / TdxAiData 快照 → 统一字段。"""
    raw = raw or {}

    def levels(prices, volumes):
        out = []
        for i, (p, v) in enumerate(zip(prices or [], volumes or [], strict=False), 1):
            price, volume = _f(p), _f(v)
            if price is None:
                continue
            out.append({"level": i, "price": price, "volume": volume})
        return out or None

    last = _f(raw.get("Now"))
    prev = _f(raw.get("LastClose"))
    change_pct = None
    if last is not None and prev:
        change_pct = round((last - prev) / prev * 100, 4)
    # TDX 快照的 Amount 单位是**万元**（实测 600519：509327万元 ≈ 50.9亿元），
    # 统一字段一律用元，这里 ×10000 归一；THS 侧 quotes_ext.amount 本身是元。
    amount = _f(raw.get("Amount"))
    if amount is not None:
        # 四舍五入到分：26354.92×10000 在浮点里是 263549199.99999997
        amount = round(amount * 10000.0, 2)
    return {
        "last": last,
        "prev_close": prev,
        "open": _f(raw.get("Open")),
        "high": _f(raw.get("Max")),
        "low": _f(raw.get("Min")),
        "volume": _f(raw.get("Volume")),
        "amount": amount,
        "change_pct": change_pct,
        "bids": levels(raw.get("Buyp"), raw.get("Buyv")),
        "asks": levels(raw.get("Sellp"), raw.get("Sellv")),
        "inside": _f(raw.get("Inside")),
        "outside": _f(raw.get("Outside")),
    }


def normalize_ths(quote: dict | None, depth: dict | None,
                  ext: dict | None) -> dict:
    """THS（quote + depth + quotes_ext）→ 统一字段。缺失一律 None，不填 0。"""
    q = quote or {}
    d = depth or {}
    e = ext or {}

    _level_map = {"买一": 1, "买二": 2, "买三": 3, "买四": 4, "买五": 5,
                  "买六": 6, "买七": 7, "买八": 8, "买九": 9, "买十": 10,
                  "卖一": 1, "卖二": 2, "卖三": 3, "卖四": 4, "卖五": 5,
                  "卖六": 6, "卖七": 7, "卖八": 8, "卖九": 9, "卖十": 10}

    def levels(items):
        # 实测（2026-10-23）worker 的 depth 返回：
        #   {"buy":[{"level":"买一","price":1333.7,"qty":200,"amount":...}], "sell":[...]}
        # 兼容 bids/asks + volume 的旧形态。
        out = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            price = _f(item.get("price"))
            if price is None:
                continue
            lv = item.get("level")
            if isinstance(lv, str):
                lv = _level_map.get(lv.strip(), lv)
            vol = _f(item.get("volume", item.get("qty")))
            # THS 盘口 qty 单位是**股**（amount = price × qty 可验证），
            # TDX 的 Buyv/Sellv 是**手**；统一字段一律用手（÷100）。
            if vol is not None and item.get("qty") is not None:
                vol = round(vol / 100.0, 2)
            out.append({"level": lv, "price": price, "volume": vol})
        return out or None

    # `/api/quotes_ext`（统一列表字段）是标量的主来源：price/chg_pct/amount/
    # 资金面都在这里；`/api/quote` 返回的是协议原始 dt* 字段，只作兜底。
    return {
        "last": _f(e.get("price", q.get("price"))),
        "prev_close": _f(q.get("pre_close")),
        "open": _f(q.get("open")),
        "high": _f(q.get("high")),
        "low": _f(q.get("low")),
        "volume": _f(q.get("volume")),
        "amount": _f(e.get("amount", q.get("amount"))),
        "change_pct": _f(e.get("chg_pct")),
        "bids": levels(d.get("bids") or d.get("buy")),
        "asks": levels(d.get("asks") or d.get("sell")),
        "main_inflow": _f(e.get("main_inflow")),
        "dde_main": _f(e.get("dde_main")),
        "market_cap": _f(e.get("market_cap")),
        "seal_amount": _f(e.get("seal_amount")),
        "auction_chg_pct": _f(e.get("auction_chg_pct")),
        "auction_amount": _f(e.get("auction_amount")),
        "speed_4m": _f(e.get("speed_4m")),
    }


def normalize_qmt(tick: dict) -> dict:
    """QMT ``get_full_tick`` 单只 tick → 统一字段。

    QMT 字段名与 TDX 完全不同（lastPrice/lastClose/...），且**没有**内外盘；
    缺失一律 None，不填 0——0 会被下游当成真实价格/成交量。
    """
    tick = tick or {}

    def levels(prices, volumes):
        out = []
        for i, (price, volume) in enumerate(
                zip(prices or [], volumes or [], strict=False), 1):
            p, v = _f(price), _f(volume)
            if p is None:
                continue
            out.append({"level": i, "price": p, "volume": v})
        return out or None

    last = _f(tick.get("lastPrice"))
    prev = _f(tick.get("lastClose"))
    change_pct = None
    if last is not None and prev:
        change_pct = round((last - prev) / prev * 100, 4)
    return {
        "last": last,
        "prev_close": prev,
        "open": _f(tick.get("open")),
        "high": _f(tick.get("high")),
        "low": _f(tick.get("low")),
        "volume": _f(tick.get("volume")),
        "amount": _f(tick.get("amount")),
        "change_pct": change_pct,
        "bids": levels(tick.get("bidPrice"), tick.get("bidVol")),
        "asks": levels(tick.get("askPrice"), tick.get("askVol")),
        # QMT tick 不提供内外盘；保持 None 让优先序回落到 TDX/THS
        "inside": None,
        "outside": None,
    }
