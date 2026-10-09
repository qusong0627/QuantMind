#!/usr/bin/env python3
"""晚间市场研究 Agent（研究总控）：quantdb 最新数据 → 板块强度 + 明日候选池 20 只。

【2026-09-29 迁入 QuantMind】大脑侧未搬迁：LLM/MCP 工具面（dsh_agent.py + BayMax 栈
127.0.0.1:8100-8104）已停机，两条 cron 已注释（`# [待改造 2026-09-29]`）。数据路径
已按本仓根改好（原 `ROOT.parent/projects/quantmind` 写法迁入后会指到不存在的
`projects/projects/` 下），待改接 QM dsh 平台后启用。

输出三处：
1. logs/night_pool/{date}.md/.json（研究纪要）
2. quantmind 报告目录 {date}_agent_picks.json（与现有 select_from_reports 格式兼容，
   明晨 load_pool 自动优先进该池——多个 agent 选股先从这里找）
3. 对话流（pseudo agent『研究总控』，data/agent_data_astock/market-research/）→ 模型对话页可见

用法：python scripts/night_pool_agent.py [--date 2026-09-03] [--if-missing]
cron：北京 02:30 周二~周六（JST 03:30）→ 等 quantdb 当日分区落盘后产「当日」池；
     另加 04:30 幂等补跑（--if-missing：目标日池已存在则跳过），覆盖同步延迟。
     数据未落（分区 < 会话日）→ 退出码 2，宁可不产也不产陈旧池。
输出命名：池文件按**目标交易日** `{target}_agent_picks.json`（09:35 消费的就是当日池）；
         纪要 logs/night_pool/{session}.md/.json 按数据会话日。
"""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "prompts"))

from trading_cal import (is_trading_day, next_trading_day,  # noqa: E402
                         prev_trading_day)

NIGHT_POOL_DIR = ROOT / "logs" / "night_pool"
PICKS_DIR = ROOT / "data" / "reports" / "stock_picks"


def _dash8(d8: str) -> str:
    """YYYYMMDD → YYYY-MM-DD。"""
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"


def session_and_target(now: datetime) -> tuple[str, str]:
    """(数据会话日, 目标交易日)，均 YYYYMMDD。

    02:30 运行时当天会话尚未收盘：今天若是交易日 → session=上一交易日（严格回看）；
    若今天休市（周末/假期）→ session=最近一个交易日（含当日判断）。
    target = session 之后的第一个交易日（跨周末/长假自动跳）。
    """
    d = now.date()
    session = prev_trading_day(d, inclusive=not is_trading_day(d))
    return session.strftime("%Y%m%d"), next_trading_day(session).strftime("%Y%m%d")


def check_data_fresh(session: str) -> tuple[bool, str]:
    """quantdb 最新分区是否已到会话日。未到 → (False, 原因)，调用方退出码 2。"""
    d0 = latest_dt()
    if not d0 or d0 < session:
        return False, (f"quantdb 最新分区 {d0 or '（无）'} 落后于会话日 {session}"
                       "——宁可不产也不产陈旧池（等 04:30 幂等补跑）")
    return True, ""


def _idle_days(d0: str) -> int:
    """最新收盘日(YYYYMMDD)距今天(北京)的自然日数。"""
    if not (len(d0) == 8 and d0.isdigit()):
        return 1
    from datetime import date as _date
    from zoneinfo import ZoneInfo

    last = _date.fromisoformat(f"{d0[:4]}-{d0[4:6]}-{d0[6:]}")
    return (datetime.now(ZoneInfo("Asia/Shanghai")).date() - last).days


def _gap_note(d0: str) -> str:
    """长假提示：最新收盘日距今 ≥2 自然日（休市），复市首日受休市期间
    政策/外围市场/行业新闻影响可能跳变——提示模型优先说明增量信息与风险。

    若 last 与今天之间隔了长假，晚间研究基于的仍是上一交易日数据，
    避免模型把旧数据当新盘面空转；信息无从核实则明确交代数据止于该收盘日。
    """
    idle = _idle_days(d0)
    if idle < 2:
        return ""
    return (f"\n⚠️ 最新收盘日为 {d0}，距今已 {idle} 个自然日（周末/长假休市），"
            "行情数据止于该收盘日；复市首日受休市期间增量信息影响可能跳变，"
            "判断需以下方休市窗口新闻简报为准，无法核实的信息不要臆测。")


def now_cn():
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _news_block(d0: str) -> str:
    """新闻 agent 管线产物（用户口径：市场研究只看新闻 agent 研究过的新闻，
    不再读原始 RSS 流）：今日最新分子 + 当日各轮要点 + 最近一次「新闻→走势」复盘。
    缺失时明确注明（不回退原始抓取）。"""
    import json as _json
    from datetime import timedelta as _td

    today = now_cn().strftime("%Y-%m-%d")
    nb = ROOT / "data" / "news_brief"
    lines: list = []

    # 1) 最新分子（主编汇总）
    try:
        latest = _json.loads((nb / "latest.json").read_text(encoding="utf-8"))
        text = str(latest.get("text") or "")
        if text:
            lines.append(f"【新闻 agent·最新分子（{str(latest.get('ts'))[:16]}）】\n{text}")
    except (OSError, _json.JSONDecodeError):
        pass

    # 2) 今日各轮要点（宏观/主题/持仓信号，跨轮合并去重）
    try:
        rounds = []
        for line in (nb / "history.jsonl").read_text(encoding="utf-8",
                                                     errors="replace").splitlines():
            try:
                b = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if str(b.get("ts") or "")[:10] == today:
                rounds.append(b)
        if rounds:
            seg = ["【新闻 agent·今日各轮要点】"]
            for b in rounds[-6:]:
                m = b.get("macro") or {}
                seg.append(f"- {str(b.get('ts'))[11:16]} 宏观:{m.get('view', '—')}"
                           f"(bias {m.get('bias', 0):+.1f})"
                           + "；主题:" + "、".join(t.get("name", "") for t in (b.get("themes") or [])[:2]))
                for h in (b.get("holdings") or [])[:3]:
                    seg.append(f"    · {h.get('name')}({h.get('code')}) {h.get('verdict')}"
                               f" impact{float(h.get('impact') or 0):+.0f} {h.get('event_type')}")
            lines.append("\n".join(seg))
    except OSError:
        pass

    # 3) 最近一次「新闻→当日实际走势」复盘（晚间复盘档案，验证新闻可靠性）
    for back in range(1, 6):
        day = (now_cn() - _td(days=back)).strftime("%Y-%m-%d")
        rp = nb / "reviews" / f"{day}.json"
        try:
            rv = _json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError):
            continue
        ms = rv.get("mentions") or []
        if ms:
            seg = [f"【{day} 新闻提及个股→当日实际走势（晚间复盘档案，验证新闻可靠性）】"]
            for m in ms[:10]:
                mv = m.get("move")
                seg.append(f"- {m.get('name')}({m.get('code')}) [{m.get('event')}] "
                           f"当日{f'{mv:+.2f}%' if mv is not None else '无数据'}")
            lines.append("\n".join(seg))
        break

    if not lines:
        return ("\n【新闻 agent 产物】今日无分子（19:05 全景轮未跑或管线异常）——"
                "本次研究不带新闻窗口信息，观点只基于初筛分数与盘面。\n")
    return ("\n以下为『新闻 agent 管线』今日产物（门卫→宏观/板块/持仓→主编分子，"
            "非原始新闻流；判断直接引用其结论并注明）：\n\n" + "\n\n".join(lines) + "\n\n")



def _db():
    import duckdb

    return duckdb.connect()


def latest_dt() -> str:
    import glob

    for base in (ROOT / "data/quantdb/1_kline_data/daily_backward",
                 Path("/data/quantdb/1_kline_data/daily_backward")):
        if base.is_dir():
            parts = sorted(glob.glob(f"{base}/dt=*"))
            return parts[-1].rsplit("dt=", 1)[-1]
    return ""


def load_names_industry() -> dict:
    import glob

    names = {}
    for base in (ROOT / "data/quantdb/2_base_sector/instrument_detail",
                 Path("/data/quantdb/2_base_sector/instrument_detail")):
        f = base / "instrument_detail.parquet"
        if not f.is_file():
            continue
        try:
            con = _db()
            for sym, nm in con.execute(
                    "SELECT Symbol, Name FROM read_parquet(?)", [str(f)]).fetchall():
                if sym and nm:
                    names[str(sym)] = str(nm)
        except Exception:  # noqa: BLE001
            pass
        break
    return names


def build_candidates(date: str, top: int = 26) -> list:
    """最新日全市场打分 → 候选（量能/动量/买卖压力复合）。"""
    import glob

    base = None
    for b in (ROOT / "data/quantdb/1_kline_data/daily_backward",
              Path("/data/quantdb/1_kline_data/daily_backward")):
        if b.is_dir():
            base = b
            break
    if base is None:
        return []
    parts = sorted(glob.glob(f"{base}/dt=*"))
    d0 = parts[-1].rsplit("dt=", 1)[-1]
    prev = [p for p in parts if p.rsplit("dt=", 1)[-1] < d0]
    prev = prev[-1].rsplit("dt=", 1)[-1] if prev else d0
    con = _db()
    f0, f1 = f"{base}/dt={d0}/*.parquet", f"{base}/dt={prev}/*.parquet"
    df = con.execute(
        f"""SELECT a.symbol, a.close c1, a.volume v1, a.amount amt,
                   b.close c0
            FROM read_parquet('{f0}') a
            LEFT JOIN read_parquet('{f1}') b USING (symbol)""").df()
    df = df.dropna(subset=["c1", "c0", "amt"])
    df["chg1"] = (df["c1"] / df["c0"] - 1) * 100
    df["turn_est"] = df["v1"] * df["c1"] / 10000  # 成交额估算亿元（对照 amount 更稳则用 amt）
    df = df[df["amt"] >= 20000]  # ≥2 亿元成交（万元口径）
    df = df[(df["chg1"] >= -3) & (df["chg1"] <= 8)]
    # Alpha 因子 join（alpha_library 最近 ≤7 日分区；缺失则降级为基础三项打分）
    alpha_cols = ["a158_ROC20", "a158_ROC5", "a158_STD20"]
    alpha_df = None
    alpha_parts = sorted(glob.glob(
        f"{ROOT / 'data/quantdb/6_ml_datasets/alpha_library'}/dt=*"))
    for p in reversed(alpha_parts[-8:]):
        pdt = p.rsplit("dt=", 1)[-1]
        if pdt <= d0 and glob.glob(f"{p}/*.parquet"):
            try:
                alpha_df = con.execute(
                    f"SELECT symbol, {', '.join(alpha_cols)} FROM read_parquet('{p}/*.parquet')"
                ).df()
                alpha_df = alpha_df.rename(columns={
                    "a158_ROC20": "mom20", "a158_ROC5": "roc5", "a158_STD20": "std20"})
                alpha_df["alpha_dt"] = pdt
                break
            except Exception:  # noqa: BLE001
                alpha_df = None
    ms_dir = f"{ROOT / 'data/quantdb/5_technical_derived/market_sentiment'}/dt={d0}/*.parquet"
    import glob as _g

    ms_files = _g.glob(ms_dir)
    if ms_files:
        ms = con.execute(
            f"SELECT symbol, momentum_1d, momentum_3d, buy_pressure, sell_pressure "
            f"FROM read_parquet('{ms_files[-1]}')").df()
        df = df.merge(ms, on="symbol", how="left")
        base = (df["chg1"].clip(-3, 8) * 0.4
                + df["momentum_3d"].fillna(0).clip(-15, 15) * 0.35
                + (df["buy_pressure"].fillna(0.5)
                   - df["sell_pressure"].fillna(0.5)) * 100 * 0.25)
    else:
        base = df["chg1"] * 0.6 + df["turn_est"] / 10000 * 0.4
    if "mom20" in df.columns:
        # Alpha 因子增强（可解释三项，各项 clip 到 ±1）：
        #   mom20 20日趋势确认 +；roc5 短线过热反转保护 -；std20 低波优先 -
        mom20 = (df["mom20"].fillna(0).clip(-20, 20) / 20)
        rev = -(df["roc5"].fillna(0).clip(-10, 10) / 10)
        lowvol = -(df["std20"].fillna(0).clip(-5, 5) / 5)
        df["score"] = base * 0.6 + 0.15 * mom20 + 0.15 * rev + 0.1 * lowvol
    else:
        df["score"] = base
    if alpha_df is not None and len(alpha_df):
        df = df.merge(alpha_df, on="symbol", how="left")
    names = load_names_industry()
    df["name"] = df["symbol"].map(names)
    df = df[~df["name"].astype(str).str.contains("ST", na=False)]
    out = df.sort_values("score", ascending=False).head(top)
    rows = []
    for _, r in out.iterrows():
        row = {"code": r["symbol"], "name": str(r["name"]), "chg1": round(float(r["chg1"]), 2),
               "amount_yi": round(float(r["amt"]) / 10000, 2), "score": round(float(r["score"]), 2)}
        if "mom20" in r and r.get("mom20") == r.get("mom20") and r.get("mom20") is not None:
            row["alpha"] = {k: round(float(r[k]), 2) for k in ("mom20", "roc5", "std20")
                            if r.get(k) == r.get(k)}
        rows.append(row)
    return rows, d0


def filter_risk_candidates(cands: list, risk: dict) -> tuple:
    """候选池 × 风险清单 → (保留列表, [(被剔除候选, 命中条目), ...])。

    风险清单键为 6 位代码（risk_list.load_risk 口径），候选代码可能带 .SH/.SZ 后缀。
    纯函数：不改入参、不做 IO，供 run() 与单测共用。
    """
    kept, dropped = [], []
    for c in cands or []:
        code6 = str((c or {}).get("code") or "").split(".")[0]
        hit = (risk or {}).get(code6)
        if hit:
            dropped.append((c, hit))
            continue
        kept.append(c)
    return kept, dropped


def run(session: str, target: str, dry: bool = False) -> int:
    """session=数据会话日(YYYYMMDD)；target=该池服务的目标交易日(YYYYMMDD)。"""
    cands, d0 = build_candidates(session)
    if not cands:
        print(f"❌ 无候选（数据缺失，分区 {d0 or '无'}）")
        return 1
    if d0 < session:      # main 已先拦一次；直调 run() 的兜底（宁可不产，不产陈旧池）
        print(f"⏸️ 数据分区 {d0} 早于会话日 {session}，不产陈旧池（等分区落盘）")
        return 2
    from market_state import build_market_state

    from agent_tools.brokers.tdx_bridge import TdxBridgeBroker

    try:
        state = build_market_state(TdxBridgeBroker())
    except Exception:  # noqa: BLE001
        state = "（盘面状态不可用）"
    # 事件雷达：候选池打解禁/质押/回购标签（失败降级为无标签，不阻塞研究）
    try:
        from event_radar import tag_events

        _tags = tag_events([str(c.get("code") or "") for c in cands])
        for c in cands:
            ev = _tags.get(str(c.get("code") or ""), [])
            if ev:
                c["事件"] = "；".join(ev)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 事件标签不可用: {exc}")
    # 风险清单（解禁窗口内 / 近期负面新闻个股）直接从候选里剔除：
    # 模型看不见就不会选，比"选了再被闸门毙掉"少一轮无效决策（2026-09-08 用户口径）。
    # 闸门仍会独立拦一次（夜间过滤失效时兜底）。
    try:
        from risk_list import load_risk

        _risk = load_risk()
        if _risk:
            before = len(cands)
            cands, dropped = filter_risk_candidates(cands, _risk)
            for c, hit in dropped:
                print(f"  ⛔ 剔除风险标的 {c.get('code')} {c.get('name') or ''}："
                      f"{hit.get('reason')}")
            if not cands:
                print("❌ 候选全部命中风险清单，本轮不产出池")
                return 1
            if dropped:
                print(f"  候选 {before} → {len(cands)}（风险清单剔除 {len(dropped)} 只）")
    except Exception as exc:  # noqa: BLE001 风险清单不可用不阻塞研究
        print(f"⚠️ 风险清单不可用: {exc}")
    cand_json = json.dumps(cands, ensure_ascii=False)
    news = _news_block(d0)                      # 休市窗口新闻简报（平日晚间为空）
    time_box = 150 if news else 90              # 有简报时放宽思考时间盒
    task = f"""[晚间市场研究任务] 基于 quantdb {d0} 收盘数据已初筛出 {len(cands)} 只候选（附分数/涨跌/成交额）。
本池将用于 {_dash8(target)} 开盘后的实盘决策（行情数据止于 {_dash8(d0)} 收盘）。

要求（时间盒 {time_box} 秒，禁止逐只调工具取数）：
1. 初筛数据已系统算好（涨跌/成交额/复合分/名称），直接使用；
   只有当名称缺失或分数明显异常（如 ST/涨跌停残留）时，才对单只做一次核对。
2. 新闻输入只来自『新闻 agent 管线』（门卫→宏观/板块/持仓→主编），引用其结论时注明；
   不要臆测管线之外的新闻。
3. 推进股票池**不看任何 HOLD/BUY 侧标签**（历史占位标签，无信息量）：
   由你按 初筛分数 + 新闻分子的板块主线与持仓信号 + 昨日新闻→走势回顾 综合判断，
   给出 20 只池（code/name/score/一句话选股理由），按行业分散、剔除重复题材；
   若认为某持仓/题材应换掉，在 reason 里写明换仓逻辑。
3. 若现有持仓 688183/600309/300750 等本身强势也可保留注明。
4. 候选中【事件】字段标注解禁/质押/回购风险：近期大比例解禁或质押≥50%的标的
   建仓需在理由中明确风险对冲，无事件字段=无已知事件风险。
盘面状态：{state}{_gap_note(d0)}
{news}
候选初筛：{cand_json}

严格以 JSON 输出（无其他文字）：
{{"date":"{d0}","market_view":"...","sectors":[{{"name":"","strength":"","reason":""}}],
 "pool":[{{"code":"","name":"","score":0,"reason":""}}]}}
"""
    if dry:
        print(task[:2500])
        return 0
    from dsh_agent import run_agent

    try:
        content = run_agent(task, timeout_s=420, model="glm")  # glm 快端点，时间盒90s
    except Exception as exc:  # noqa: BLE001
        content = f"（研究 agent 失败：{exc}）"
    # JSON 抽取
    import re

    payload = None
    for m in re.finditer(r"\{[\s\S]*\}", content):
        try:
            payload = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
    if not isinstance(payload, dict):
        payload = {"date": d0, "pool": [], "market_view": content[:400]}
    payload["date"] = d0
    dpath = NIGHT_POOL_DIR
    dpath.mkdir(parents=True, exist_ok=True)
    (dpath / f"{session}.md").write_text(
        f"# 晚间市场研究 · {session}（服务 {target}）\n\n{content}", encoding="utf-8")
    (dpath / f"{session}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    # 候选池写 quantmind 报告目录（兼容 select_from_reports：side/score/reason）。
    # side 恒为 HOLD 占位（无信息量）——消费端（load_pool/pool_rows/提示词）已不再展示，
    # 判断全部由各 agent 依据分数+大盘+板块+新闻综合给出。
    # 命名按**目标交易日**（2026-09-12 P1-4）：09:35 消费端按日期取最新 → 当日池自然胜出。
    try:
        pool = []
        for p in (payload.get("pool") or [])[:20]:
            _ev = next((c.get("事件") or "" for c in cands
                        if str(c.get("code")) == str(p.get("code"))), "")
            pool.append({"code": p.get("code"), "name": p.get("name"),
                         "side": "HOLD", "score": float(p.get("score") or 0),
                         "events": _ev,
                         "reason": str(p.get("reason") or "")[:160]})
        out = {"date": target, "session": session,
               "market_direction": {"direction": payload.get("market_view", "")[:80]},
               "picks": pool}
        PICKS_DIR.mkdir(parents=True, exist_ok=True)
        (PICKS_DIR / f"{target}_agent_picks.json").write_text(
            json.dumps(out, ensure_ascii=False), encoding="utf-8")
        print(f"✅ 池文件 {target}_agent_picks.json（{len(pool)} 只，数据会话 {session}）")
    except OSError as exc:  # noqa: BLE001
        print(f"⚠️ 写池文件失败: {exc}")
    # 对话流：pseudo『研究总控』（归到该池服务的交易日——周一早班看得到周一的池）
    try:
        lf = ROOT / "data" / "agent_data_astock" / "market-research" / "log" / _dash8(target) / "log.jsonl"
        lf.parent.mkdir(parents=True, exist_ok=True)
        row = {"timestamp": datetime.now().astimezone().isoformat(),
               "signature": "market-research", "kind": "night_pool",
               "new_messages": [
                   {"role": "user", "content": f"【晚间市场研究任务】{d0} 收盘数据 → {_dash8(target)} 板块+候选池"},
                   {"role": "assistant", "content": content},
               ]}
        with lf.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:  # noqa: BLE001
        print(f"⚠️ 写对话流失败: {exc}")
    print(f"✅ 晚间研究完成 → logs/night_pool/{session}.md/.json（服务 {target}）")
    return 0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="", help="手工复跑：数据会话日（YYYY-MM-DD / YYYYMMDD）")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--if-missing", action="store_true",
                    help="幂等补跑（04:30）：目标日池文件已存在则立即退出")
    a = ap.parse_args(argv)
    # 日历闸门（2026-09-12 改 02:30 后的口径）：
    #   ①session = 最近已收盘交易日（凌晨运行时当天会话尚未收盘，自动回看前一天，
    #     周末/长假自动跳过）；②target = session 之后第一个交易日 = 该池服务日；
    #   ③quantdb 分区 < session（数据未落）→ 退出码 2：宁可不产也不产陈旧池
    #     （日报会显示夜池缺失；04:30 幂等补跑兜住同步延迟）。
    if a.date:
        session = a.date.replace("-", "")
        try:
            d = date.fromisoformat(_dash8(session))
        except ValueError:
            print(f"❌ --date 格式应为 YYYY-MM-DD 或 YYYYMMDD：{a.date}")
            return 1
        return run(session, next_trading_day(d).strftime("%Y%m%d"), dry=a.dry)
    bj = now_cn()
    session, target = session_and_target(bj)
    if a.if_missing and (PICKS_DIR / f"{target}_agent_picks.json").is_file():
        # 空池 = 研究 agent 失败（如 dsh STREAM_CLOSED 半途断流）落下的空壳，
        # 不是「已产出」——2026-09-15 实录：02:30 主跑失败落 0 只池，
        # 03:30 补跑因文件存在直接跳过，当天全系统无候选池。此处按非空才算完成。
        pool_n = -1
        try:
            pool_n = len((json.loads((PICKS_DIR / f"{target}_agent_picks.json")
                                     .read_text(encoding="utf-8")).get("picks") or []))
        except (OSError, ValueError, AttributeError):
            pool_n = -1
        if pool_n > 0:
            print(f"⏭️ {bj:%F %T} 目标日池已存在（{target}_agent_picks.json，{pool_n} 只），补跑跳过")
            return 0
        print(f"⚠️ {bj:%F %T} 目标日池为空/不可读（{target}_agent_picks.json）"
              f"——视为未产出，执行补跑")
    ok, why = check_data_fresh(session)
    if not ok:
        print(f"⏸️ {bj:%F %T} {why}")
        return 2
    print(f"🌙 {bj:%F %T} 晚间研究：数据会话 {session} → 服务交易日 {target}")
    return run(session, target, dry=a.dry)


if __name__ == "__main__":
    sys.exit(main())