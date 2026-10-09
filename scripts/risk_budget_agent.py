#!/usr/bin/env python3
"""风险预算 meta-agent v1（确定性规则内核，见 docs/AGENT_PHASE34_DESIGN.md）。

输入：指数 20 日波动（上证/沪深300）、分账净值回撤、Fuyao 情绪温度、大盘三态。
输出：configs/risk_budget.json（闸门读取）+ logs/budget/{date}.json 明细。
防抖：同一自然日只允许收紧一次、不允许放大（隔日按最新状态自动恢复）。
cron：交易日 北京 09:10。
"""
import json
import sys
from datetime import date, datetime  # noqa: E402
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from trading_cal import is_trading_day, prev_trading_day, why_not  # noqa: E402

OUT = ROOT / "configs" / "risk_budget.json"
DETAIL = ROOT / "logs" / "budget"
STATE = ROOT / "logs" / "budget" / "state.json"


def _index_vol() -> float | None:
    """上证近 20 日日收益率标准差（%）→ 波动率代理。"""
    try:
        from agent_tools.brokers.tdx_bridge import TdxBridgeBroker

        k = TdxBridgeBroker().get_klines("000001.SH", interval="daily")
    except Exception:  # noqa: BLE001
        return None
    closes = [float(x.get("close") or 0) for x in (k or [])[-21:]]
    if len(closes) < 11 or not all(closes):
        return None
    rets = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
    m = sum(rets) / len(rets)
    sd = (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5
    return round(sd, 2)


def _equity_drawdown() -> float | None:
    """分账总净值近 20 日（取每日最后一条）最大回撤 %。"""
    try:
        daily = {}
        for l in (ROOT / "logs" / "live_equity.jsonl").read_text(encoding="utf-8").splitlines():
            r = json.loads(l)
            if r.get("agent") is None and r.get("value") is not None:
                daily.setdefault(str(r.get("date")), r["value"])
        vals = [v for _, v in sorted(daily.items())[-20:]]
        if len(vals) < 5:
            return None
        peak = max(vals)
        dd = (peak - min(vals)) / peak * 100
        return round(dd, 2)
    except (OSError, ValueError):
        return None


SENTIMENT_LOG = ROOT / "logs" / "sentiment_zt.jsonl"


def _fuyao_get():
    """Fuyao 客户端 get()（Key 从 .env 补；缺失时调用会返回 code=-1）。"""
    import os

    sys.path.insert(0, str(ROOT / "skills/ths-fuyao/scripts"))
    from ths_fuyao import get

    os.environ.setdefault("THS_FUYAO_KEY", "")
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("THS_FUYAO_KEY="):
            env["THS_FUYAO_KEY"] = line.split("=", 1)[1].strip().strip('"')
    os.environ.update({k: v for k, v in env.items() if v})
    return get


def _bj_now() -> datetime:
    """北京时区当前时间（服务器时区为 JST，窗口/日期判定一律显式换算）。"""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai"))


def pool_count(payload: dict) -> int | None:
    """涨跌停池家数：**必须读 pagination.total**（market_state 也复用此口径）。

    item 被默认分页截断在 50 条——2026-09-08 实测真实涨停 72 家却被读成 50，
    且「过热 >90 家」阈值永远够不着（永远是 50）。取不到 → None（≠ 真的 0 家）。
    """
    d = (payload or {}).get("data")
    if not isinstance(d, dict):
        return None
    items = d.get("item")
    if not isinstance(items, list):
        return None
    total = (d.get("pagination") or {}).get("total")
    if isinstance(total, int) and total >= 0:
        return int(total)
    # 无分页字段：退回本页条数；**空列表且无分页 = 取数不可用（None）**，
    # 不能当成"0 家涨停"——真 0 家会带 pagination.total=0（跌停池实测如此）
    return len(items) if items else None


def ladder_max(payload: dict, date: str | None = None) -> int | None:
    """连板天梯最高板数（默认取响应里最新一天）。

    响应形态（2026-09-08 实测）：item 是**近 30 个交易日**的分组行
    `[{date, boards:{two_board:[{board_num}...], three_board:[...], ...}}]`——
    既不是扁平列表，也没有 `continue_day` 字段。旧实现读 `continue_day` → 恒为 0，
    注入给模型的"最高连板 0"是假数据。若不按日期取，历史某天的高板会串味
    （实测 09-07 有 6 板、09-08 只有 4 板）。
    """
    d = (payload or {}).get("data")
    if not isinstance(d, dict):
        return None
    items = d.get("item")
    if not isinstance(items, list) or not items:
        return None
    rows = [r for r in items if isinstance(r, dict)]
    if not rows:
        return None
    if all("boards" not in r for r in rows):  # 扁平形态兜底（端点若改版）
        mx = 0
        for r in rows:
            try:
                mx = max(mx, int(r.get("continue_day") or r.get("board_num") or 0))
            except (TypeError, ValueError):
                pass
        return mx
    if date:
        picked = [r for r in rows if str(r.get("date") or "") == str(date)]
        rows = picked or rows
    else:
        latest = max(str(r.get("date") or "") for r in rows)
        rows = [r for r in rows if str(r.get("date") or "") == latest]
    mx = 0
    for r in rows:
        boards = r.get("boards")
        if not isinstance(boards, dict):
            continue
        for lst in boards.values():
            if not isinstance(lst, list):
                continue
            for it in lst:
                if isinstance(it, dict):
                    try:
                        mx = max(mx, int(it.get("board_num") or 0))
                    except (TypeError, ValueError):
                        pass
    return mx


def _fuyao_live() -> tuple[int, int | None] | None:
    """盘中实时（涨停家数, 最高连板）；任何失败 → None。"""
    try:
        get = _fuyao_get()
        pool = get("/api/a-share/special-data/limit-up-pool", {})
        if pool.get("code") != 0:
            return None
        zt = pool_count(pool)
        if zt is None:
            return None
        ld = get("/api/a-share/special-data/limit-up-ladder", {})
        mx = ladder_max(ld) if ld.get("code") == 0 else None
        return zt, mx
    except Exception:  # noqa: BLE001
        return None


def record_sentiment(zt: int | None, ladder: int | None, ts: str | None = None) -> None:
    """落盘一次盘中情绪观测（供次日盘前定档引用；写失败不影响主流程）。"""
    if not isinstance(zt, int):
        return
    ts = ts or _bj_now().strftime("%Y-%m-%dT%H:%M:%S")
    try:
        SENTIMENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SENTIMENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, "date": ts[:10], "zt": int(zt),
                                "max_ladder": ladder}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _last_recorded() -> dict | None:
    """最近一次盘中情绪观测（倒序取首个合法行）。"""
    try:
        lines = SENTIMENT_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict) and isinstance(r.get("zt"), int):
            return r
    return None


def _sentiment(now=None) -> tuple[int | None, int | None, str]:
    """(涨停家数, 最高连板, 口径说明)。

    盘中 → 同花顺 Fuyao 实时（并落盘）；盘前/盘后 → **最近一次盘中记录**，
    并如实标注日期。涨停池是盘中时点数据，盘前查必然为空（2026-09-08 前
    每天 09:10 定档都读到 0 家 → 天天误判"情绪冰点" → 天天谨慎档）。
    两者都取不到 → (None, None, "")，交给 decide_level 的 fail-safe 降级；
    **绝不把"取不到数"当成"0 家涨停"**。
    """
    now = now or _bj_now()
    hm = now.hour * 60 + now.minute
    if now.weekday() < 5 and 9 * 60 + 15 <= hm <= 15 * 60 + 10:
        live = _fuyao_live()
        if live:
            record_sentiment(live[0], live[1], now.strftime("%Y-%m-%dT%H:%M:%S"))
            return live[0], live[1], "盘中实时（同花顺 Fuyao）"
    rec = _last_recorded()
    if rec:
        return (int(rec["zt"]), rec.get("max_ladder"),
                f"{rec.get('date', '')} 盘中记录（同花顺 Fuyao，非今日实时）")
    return None, None, ""


LEVELS = {
    "calm": {"leverage_max": 1.5, "per_stock_pct": 0.2, "max_new_buys": 3,
             "leverage_trim_to": 1.3, "per_stock_pos_pct": 0.30, "label": "平静"},
    "caution": {"leverage_max": 1.2, "per_stock_pct": 0.15, "max_new_buys": 2,
                "leverage_trim_to": 1.15, "per_stock_pos_pct": 0.25, "label": "谨慎"},
    "defensive": {"leverage_max": 1.0, "per_stock_pct": 0.10, "max_new_buys": 1,
                  "leverage_trim_to": 1.0, "per_stock_pos_pct": 0.15, "label": "防守"},
}
#: per_stock_pos_pct = 单票**累计持仓**上限（同票成本 ≤ 该比例×权益，2026-09-18）。
#: per_stock_pct 只管"单笔占剩余额度"，同一标的跨轮加仓此前不封顶——由它补上。

LIMIT_KEYS = ("leverage_max", "per_stock_pct", "max_new_buys", "leverage_trim_to",
              "per_stock_pos_pct")

#: 档位不可信时的回退：**只含买入侧参数**。三档 LEVELS 的这三个键恒 ≥ 本回退，
#: 因此"过期强制回退 / 缺键补齐"在买入侧只会收紧、绝不放松。
#: 有意不含 leverage_max / leverage_trim_to——强减是风险动作，数据故障不应触发
#: 强平；且压低 leverage_max 而 trim_to 保持原值会组合出"减到仍超限"的强平循环。
#: 过期分支的杠杆键取文件原值（与改动前行为一致）：不因故障放宽、也不制造卖出。
#: （2026-09-18 代码评审 H-1 修正：初版曾让过期分支的杠杆上限回落 1.5，方向错误。）
FALLBACK_LIMITS = {"per_stock_pct": 0.10, "max_new_buys": 1, "per_stock_pos_pct": 0.15}


def budget_stale_reason(doc: dict, today: date) -> str:
    """档位文件是否过期（返回原因，新鲜为 ""）。纯函数可测。

    应定档日 = 今天（交易日）；非交易日（周末/假期）沿用最近一个交易日的档位。
    只有**早于**应定档日才算过期；缺日期字段 = 不知道用的是哪天的档位，同样算。
    """
    want = today if is_trading_day(today) else prev_trading_day(today)
    raw = str((doc or {}).get("date") or "")
    try:
        got = date.fromisoformat(raw)
    except ValueError:
        return (f"档位日期缺失/不可解析（{raw or '空'}），应定档日 {want.isoformat()}"
                "——定档 cron 或写入链路可能故障")
    if got >= want:
        return ""
    return (f"档位日期 {got.isoformat()} 早于应定档日 {want.isoformat()}"
            "——定档 cron 或数据源可能故障，当天沿用旧档位")


def _warn_budget_issue(reason: str, path: Path) -> None:
    """档位不可用/过期 → 提醒 + **回退买入侧防守参数**（2026-09-18 fail-safe 化）。

    此前是"只提醒不改行为"（fail-open）：读不到档位就按调用方硬编码默认
    （1.5×/20%）跑——比预算档松，等于风控半失效（09-18 上午即实录一次
    "档位日期早于应定档日，照常 fail-open"）。现与 decide_level 同方向：
    风控数据不可信 → 收紧。回退只含买入侧（见 FALLBACK_LIMITS），
    杠杆/强减参数不因数据故障变化——强平是风险动作，不应由数据故障触发。
    同日一条事件（record_event 覆盖语义），kind 沿用 risk_budget_stale。"""
    if not reason:
        return
    print(f"⚠️ 风险预算异常：{reason}｜已回退买入侧防守参数（单票 10% / 新开仓 1 只 / "
          f"单票持仓 ≤15%）；杠杆/强减不因数据故障改变口径（不触发强平）；请检查 {path}")
    try:
        from live_fills import record_event  # 局部导入：本模块也要能脱离实盘栈单跑

        record_event("risk_budget_stale", "",
                     f"风险预算异常：{reason}（已回退买入侧防守参数，fail-safe）")
    except Exception as exc:  # noqa: BLE001 事件面故障不许反过来打断风控读取
        print(f"  ⚠️ 风险预算事件落盘失败（{str(exc)[:80]}）")


def load_limits(path: Path | None = None) -> dict:
    """当日风控档位（风险预算输出 → **所有实盘入口共用**的硬约束）。

    2026-09-08 前的漏洞：只有 live_hourly_analysis 读这份预算，09:35 主入口
    （live_llm_trade）硬编码 1.5/20% 且没有新开仓上限——预算定档"防守"时
    主入口仍按宽松档下单，风险预算只兑现了一半。统一从本函数取。

    失效姿态（2026-09-18 **契约变更**，同日评审 H-1/MEDIUM 修正）：
      - 文件缺失/损坏/结构异常 → 返回 `FALLBACK_LIMITS`（买入侧防守参数）并留痕；
      - 档位过期 → 买入侧三键回退 FALLBACK，杠杆/强减键取文件原值；
      - 文件缺键（写读版本漂移）→ 缺的买入侧键按 FALLBACK 补齐，缺的杠杆类键
        留给调用方默认；
      - 所有失效形态下**买入侧只收紧不放松**（三档 LEVELS 均 ≥ FALLBACK），
        且不制造强平动作（见 FALLBACK_LIMITS 注释）。
    与 decide_level 同方向：风控数据不可信 → 收紧。
    """
    p = path or OUT
    try:
        raw = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        # 非 UTF-8 字节（写入侧非原子写 + ensure_ascii=False 含中文，截断在多字节
        # 字符中间）：UnicodeDecodeError 是 ValueError 子类、**不是** OSError，
        # 漏捕会让它穿出本函数、退回静默（2026-09-12 复审 LOW）
        _warn_budget_issue(
            f"档位文件不是合法的 UTF-8 文本（{str(exc)[:80]}）"
            "——档位不可信，回退买入侧防守参数", p)
        return dict(FALLBACK_LIMITS)
    except OSError as exc:
        _warn_budget_issue(
            f"档位文件不存在或不可读（{exc.__class__.__name__}: {str(exc)[:80]}）"
            "——档位不可信，回退买入侧防守参数", p)
        return dict(FALLBACK_LIMITS)
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        _warn_budget_issue(
            f"档位文件不是合法 JSON（{str(exc)[:80]}）"
            "——档位不可信，回退买入侧防守参数", p)
        return dict(FALLBACK_LIMITS)
    if not isinstance(doc, dict):
        _warn_budget_issue(
            f"档位文件结构异常（顶层是 {type(doc).__name__}）"
            "——档位不可信，回退买入侧防守参数", p)
        return dict(FALLBACK_LIMITS)
    problem = ""
    lv = doc.get("budget")
    if not isinstance(lv, dict):
        problem = (f"档位文件的 budget 字段缺失或结构异常（{type(lv).__name__}）"
                   "——档位不可信，回退买入侧防守参数")
        lv = {}
    out: dict = {}
    for k in LIMIT_KEYS:
        v = lv.get(k)
        if v is None:
            continue
        try:
            out[k] = int(v) if k == "max_new_buys" else float(v)
        except (TypeError, ValueError):
            continue
    if not problem and not out:
        problem = ("档位文件没有任何可用的档位字段（budget 为空或字段均不可解析）"
                   "——档位不可信，回退买入侧防守参数")
    if problem:
        _warn_budget_issue(problem, p)
        return dict(FALLBACK_LIMITS)
    stale = budget_stale_reason(doc, _bj_now().date())
    if stale:
        # 过期 = 整套档位不可信：买入侧三键强制回退防守；杠杆/强减键**取文件原值**
        # （不是买入侧刹车，见 FALLBACK_LIMITS 注释——取原值与改动前行为一致）。
        tightened = {**out, **FALLBACK_LIMITS}
        _warn_budget_issue(stale, p)
        return tightened
    missing = [k for k in LIMIT_KEYS if k not in out]
    if missing:
        # 写读键集不一致（文件由旧版写入方产生，如新键上线但 cron 尚未重启）：
        # 缺的**买入侧**键按防守回退补齐；缺的杠杆类键留给调用方默认（同上，
        # 不在数据故障形态下改强减口径）。2026-09-18 评审 MEDIUM：初版对缺键
        # 直接 `continue`，成功路径返回部分字典 → 新键静默不生效。
        filled = {**out,
                  **{k: v for k, v in FALLBACK_LIMITS.items() if k in missing}}
        _warn_budget_issue(
            f"档位文件缺少键 {missing}（写读版本漂移）"
            "——缺失的买入侧键已按防守回退补齐", p)
        return filled
    return out


def decide_level(vol: float | None, dd: float | None,
                zt: int | None, ladder: int | None) -> tuple[str, list]:
    """纯判定（P0-3 可测）：波动/回撤/情绪 → (档位, 触发因素)。

    fail-safe（2026-09-08）：风控输入缺失**不允许**落到最松档——数据故障时
    宁可保守。关键输入缺 ≥2 项 → 防守；缺 1 项 → 至少谨慎。
    此前行为：波动/回撤取不到时被静默跳过，只剩情绪闸 → 数据故障反而放宽，
    属 fail-open（违反"风控故障不能跳过风控"）。
    """
    reasons, missing = [], []
    if vol is None:
        missing.append("指数波动")
    if dd is None:
        missing.append("分账回撤")
    if zt is None:
        missing.append("情绪温度")
    if vol is not None and vol >= 1.2:
        reasons.append(f"波动 {vol}% 偏高")
    if dd is not None and dd >= 5:
        reasons.append(f"回撤 {dd}% 超限")
    elif dd is not None and dd >= 3:
        reasons.append(f"回撤 {dd}% 加深")
    if zt is not None and zt < 25:
        reasons.append(f"涨停仅 {zt} 家情绪偏冷")
    if zt is not None and zt > 90:
        reasons.append(f"涨停 {zt} 家情绪过热")
    if dd is not None and dd >= 5:
        return "defensive", reasons
    if len(missing) >= 2:
        return "defensive", reasons + [f"风控数据缺失（{'、'.join(missing)}）→ 降级防守"]
    if missing:
        reasons.append(f"风控数据缺失（{'、'.join(missing)}）→ 至少谨慎")
    if reasons:
        return "caution", reasons
    return "calm", []


def main() -> int:
    from zoneinfo import ZoneInfo

    bj = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    if not is_trading_day(bj):
        print(f"⏭️ {bj} 非交易日（{why_not(bj)}），跳过风险预算定档（沿用上一交易日档位）")
        return 0
    vol = _index_vol()
    dd = _equity_drawdown()
    zt, ladder, senti_src = _sentiment()
    state_txt, reasons = decide_level(vol, dd, zt, ladder)
    lvl = LEVELS.get(state_txt)
    today = datetime.now().strftime("%Y-%m-%d")
    # 防抖：同日只收紧一次；不允许放大
    st = {}
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    prev_level = st.get("level")
    order = {"calm": 0, "caution": 1, "defensive": 2}
    effective = state_txt
    if st.get("date") == today and prev_level and order.get(state_txt, 0) < order.get(prev_level, 0):
        effective = prev_level  # 防抖：当日已收紧则维持，不放大
        reasons.append(f"防抖：当日已定 {prev_level}，不再放宽")
    lvl = LEVELS.get(effective)
    DETAIL.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"date": today, "level": effective}, ensure_ascii=False), encoding="utf-8")
    doc = {"date": today, "level": effective, "label": lvl["label"],
           "inputs": {"vol20": vol, "drawdown20": dd, "limit_up": zt, "max_ladder": ladder,
                      "sentiment_source": senti_src or "无可用情绪数据"},
           "budget": {k: lvl[k] for k in LIMIT_KEYS},   # 唯一出处：写读两侧同一键集
           "reasons": reasons,
           "note": "确定性规则内核 v1；状态恢复即自动放松（隔日生效），同日只收紧一次"}
    # v2：LLM 解释档位含义（数值以确定性为准，解释失败不影响）
    try:
        from dsh_agent import run_agent

        task = ("当前风险档=" + lvl["label"] + "，预算="
                + json.dumps(doc["budget"], ensure_ascii=False) + "，触发因素="
                + str(reasons or "无")
                + "。用 2-3 句话给当日三个分账 agent 讲清操作含义：加仓空间、减仓纪律、新动作数量建议。")
        doc["explain"] = run_agent(task, timeout_s=90, model="glm")[:300]
    except Exception:  # noqa: BLE001
        doc["explain"] = ""
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    (DETAIL / f"{today}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    print(f"✅ 风险预算 → {state_txt}（{lvl['label']}）", "·".join(reasons) if reasons else "(平静)",
          f"[情绪口径: {senti_src or '无数据'}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())