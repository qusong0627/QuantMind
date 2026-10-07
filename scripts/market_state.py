"""盘面状态快照（确定性注入，模型不可争辩的事实层）。

设计（2026-09-03）：提示词是软约束，要让"大盘剧本/时段打法"真正生效，
把状态判定下沉到代码——指数趋势/量能/位置/时段由本模块按桥数据实时
计算，作为【盘面状态（系统判定）】注入每轮分析任务。模型只需把
当前状态映射到 persona 里的剧本动作基调，而不是自己拍脑袋定牛熊。
"""
import threading
import time
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
_cache: dict = {"ts": 0.0, "text": ""}
_lock = threading.Lock()
TTL = 300  # 5 分钟缓存（整点/多 agent 复用同一条，省桥调用）


def _index_stats(broker, code: str) -> dict | None:
    """近 12 根日K → 5日趋势/量能比/20日位置。失败返回 None（不阻断）。"""
    try:
        k = broker.get_klines(code, interval="daily")  # 250 根，取尾 12
    except Exception:  # noqa: BLE001
        return None
    k = (k or [])[-12:]
    if len(k) < 7:
        return None
    closes = [float(x.get("close") or 0) for x in k]
    vols = [float(x.get("volume") or 0) for x in k]
    if not all(closes) or not all(vols):
        return None
    trend = (closes[-1] / closes[-6] - 1) * 100  # 5 日涨跌
    vol_ratio = sum(vols[-5:]) / max(sum(vols[-10:-5]), 1)
    wins = closes[-20:]
    pos = (closes[-1] - min(wins)) / max(max(wins) - min(wins), 1e-9)
    return {"trend": trend, "vol_ratio": vol_ratio, "pos": pos * 100}


def _quantdb_breadth() -> str | None:
    """昨日收盘全景（quantdb market_sentiment 最新 dt，EOD 口径）→ 温度计行。

    已验证（2026-09-03）：该表按动量粗判涨跌停严重失真（涨停占比 27% 不合理），
    故只取可信聚合：上涨/下跌家数、动量均值、买/卖压力均值；且做自校验——
    上涨+下跌占比 <80% 或动量均值超出 [-2,2] 视为脏数据整体丢弃，宁缺毋滥。
    返回 None 表示不可用（不注入）。
    """
    roots = ("/home/zbox/projects/quantmind/data/quantdb"
             "/5_technical_derived/market_sentiment",
             "/data/quantdb/5_technical_derived/market_sentiment")
    import glob
    import os

    root = next((p for p in roots if os.path.isdir(p)), None)
    if not root:
        return None
    ds = sorted(int(os.path.basename(f).split("=")[1])
                for f in glob.glob(f"{root}/dt=*"))
    if not ds:
        return None
    try:
        import duckdb

        df = duckdb.connect().execute(
            f"SELECT momentum_1d, buy_pressure, sell_pressure "
            f"FROM read_parquet('{root}/dt={ds[-1]}')").df()
    except Exception:  # noqa: BLE001
        return None
    if df.empty:
        return None
    mom = df["momentum_1d"].dropna()
    total_all = len(mom)
    mom = mom[mom.abs() <= 10]  # 剔除明显脏值（新股/异常），保留主体分布
    if len(mom) < max(1, int(total_all * 0.9)):
        return None  # 极端值占比过高 → 整体视为脏
    up = int((mom > 0).sum())
    down = int((mom < 0).sum())
    total = len(mom)
    if total == 0 or (up + down) / total < 0.8:  # 自校验：覆盖不完整视为脏
        return None
    bp = float(df["buy_pressure"].mean()) if "buy_pressure" in df else None
    sp = float(df["sell_pressure"].mean()) if "sell_pressure" in df else None
    press = ""
    if bp is not None and sp is not None:
        press = f" · 买压 {bp:.2f}/卖压 {sp:.2f}"
    dt = str(ds[-1])
    return (f"情绪温度（quantdb 昨日收盘全景 {dt[:4]}-{dt[4:6]}-{dt[6:]}）："
            f"上涨 {up}/{total} 下跌 {down}/{total}（涨跌比 {up / max(down, 1):.2f}）"
            f" · 动量均值 {mom.mean():+.3f}{press}（非今日实时，仅盘前/盘后参考）")


def build_market_state(broker=None) -> str:
    """【盘面状态（系统判定）】文本块：时段 + 大盘三态 + 动作基调 + 情绪提示。

    大盘判定：上证/沪深300 任一可用即可；5日涨跌 ±1.5% 为界分强/弱，
    量能比 >1.15 记放量，位置>60 记高位/ <30 记低位。无法取数时明确降级。
    """
    global _cache
    now = datetime.now(BJ)
    # 缓存 5 分钟（同一轮多 agent/多模式共用）
    if _cache["text"] and now.timestamp() - _cache["ts"] < TTL:
        return _cache["text"]
    session = "休市"
    hm = now.hour * 60 + now.minute
    if now.weekday() < 5:
        if 9 * 60 <= hm < 9 * 60 + 30:
            session = "集合竞价"
        elif 9 * 60 + 30 <= hm < 11 * 60 + 30:
            session = "盘中·上午"
        elif 11 * 60 + 30 <= hm < 13 * 60:
            session = "午间休市"
        elif 13 * 60 <= hm < 14 * 60 + 30:
            session = "盘中·下午"
        elif 14 * 60 + 30 <= hm < 14 * 60 + 57:
            session = "尾盘"
        elif 14 * 60 + 57 <= hm < 15 * 60:
            session = "尾盘竞价"
        elif hm < 9 * 60:
            session = "盘前"
        else:
            session = "盘后"
    stats = []
    if broker is not None:
        for code, name in (("000001.SH", "上证"), ("000300.SH", "沪深300")):
            s = _index_stats(broker, code)
            if s:
                stats.append((name, s))
    if stats:
        name, s = stats[0]
        if s["trend"] >= 1.5:
            regime = "大盘偏强"
        elif s["trend"] <= -1.5:
            regime = "大盘偏弱"
        else:
            regime = "大盘震荡"
        vol_tag = "放量" if s["vol_ratio"] >= 1.15 else ("缩量" if s["vol_ratio"] <= 0.85 else "平量")
        pos_tag = ("高位" if s["pos"] >= 60 else "低位" if s["pos"] <= 30 else "中位")
        extra = f"（指数校验：{name} 5日{s['trend']:+.1f}%、{vol_tag}、位置{pos_tag}）"
        if regime == "大盘偏强":
            tune = "进攻基调：主线龙头回踩低吸可给建仓/加仓；止损照执行但可用结构位"
        elif regime == "大盘偏弱":
            tune = "生存基调：减弱势仓留现金、禁止逆势买入、等企稳信号（缩量止跌/长下影）"
        else:
            tune = "防守反击基调：强势板块内轮动低吸、日内兑现、不做隔夜追涨"
    else:
        regime = "大盘状态未知（指数数据不可用）"
        tune = "按震荡处置：仓位中性、机会只做强势板块、控制隔夜"
        extra = ""
    text = (f"【盘面状态（系统判定）】时段={session}；{regime}{extra}；"
            f"动作基调={tune}。情绪提示：涨跌停与情绪极端点（一字/天地板）一律不参与，错过就错过。")
    # 盘中情绪温度（同花顺 Fuyao：涨停池/跌停池/连板天梯）——真实盘中全市场口径
    def _fuyao_temp() -> str:
        try:
            import os as _os
            import sys as _sys
            sk = _os.path.expanduser("~/projects/quantmind/skills/ths-fuyao/scripts")
            _sys.path.insert(0, sk)
            from ths_fuyao import get  # noqa: E402
            # 分页口径复用 risk_budget_agent.pool_count（item 被截断在 50 条）；
            # 天梯口径复用 ladder_max（item 是 30 日分组行，非扁平列表）
            from risk_budget_agent import ladder_max, pool_count, record_sentiment  # noqa: E402

            def count(path):
                d = get(path, {})
                if d.get("code") != 0:
                    return None
                return pool_count(d)
            zt, dt_, ladder = count("/api/a-share/special-data/limit-up-pool"), None, None
            dt_ = count("/api/a-share/special-data/limit-down-pool")
            ld = get("/api/a-share/special-data/limit-up-ladder", {})
            max_lb = ladder_max(ld) if ld.get("code") == 0 else None
            if zt is None:
                return ""
            # 盘中落盘一次（5 分钟 TTL 去重）：供次日 09:10 盘前定档引用——
            # 盘前涨停池结构性为空，没有这条记录风控就只能读到 0 家（2026-09-08 修复）
            record_sentiment(zt, max_lb, now.strftime("%Y-%m-%dT%H:%M:%S"))
            return (f"情绪温度（盘中·同花顺 {now:%H:%M}）：涨停 {zt} · 跌停 {dt_ if dt_ is not None else '—'}"
                    f" · 最高连板 {max_lb if max_lb is not None else '—'}")
        except Exception:  # noqa: BLE001
            return ""

    if 9 * 60 + 15 <= hm <= 15 * 60 + 10 and now.weekday() < 5:
        ft = _fuyao_temp()
        if ft:
            text += "\n" + ft
    # 数据源新鲜度清单（系统判定，agent 不必再试错发现滞后）
    import glob as _g
    import os as _os
    fresh=[]
    try:
        qroot=_os.path.expanduser("~/projects/quantmind/data/quantdb/5_technical_derived/market_sentiment")
        ds=sorted(int(_os.path.basename(x).split("=")[1]) for x in _g.glob(qroot+"/dt=*"))
        if ds:
            d=str(ds[-1]); fresh.append(f"quantdb 情绪库至 {d[4:6]}/{d[6:]}")
    except Exception: pass
    try:
        import pathlib as _p
        l2f=_p.Path("/home/zbox/projects/quantmind/data/l2_factors_live.json")
        if l2f.is_file():
            import datetime as _dt
            age=(datetime.now(BJ).timestamp()-l2f.stat().st_mtime)/60
            fresh.append(f"L2 采集 {age:.0f} 分钟前")
    except Exception: pass
    # quantdb 日K 最新分区（agent 工具层数据源可能滞后，以此为准绳防误判新鲜度）
    try:
        for qroot in (_os.path.expanduser("~/projects/quantmind/data/quantdb/1_kline_data/daily_backward"),
                      "/data/quantdb/1_kline_data/daily_backward"):
            ds=sorted(_g.glob(qroot+"/dt=*"))
            if ds:
                d=_os.path.basename(ds[-1]).split("=")[1]
                fresh.append(f"quantdb 日K至 {d[4:6]}/{d[6:]}")
                break
    except Exception: pass
    text += ("\n【数据源新鲜度（系统判定）】" + " · ".join(fresh)) if fresh else ""
    try:
        from ashare_rules import rules_brief
        text += "\n【板块交易规则（系统注入，照此执行勿再求证）】" + rules_brief()
    except Exception:
        pass
    breath = _quantdb_breadth()
    if breath:
        text += "\n" + breath
    with _lock:
        _cache = {"ts": now.timestamp(), "text": text}
    return text