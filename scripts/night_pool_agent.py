#!/usr/bin/env python3
"""晚间市场研究 Agent（研究总控）：quantdb 最新数据 → 板块强度 + 明日候选池 20 只。

【2026-10-14 改接（戊1）】大脑 = ``call_brain()`` 直连 OpenAI 兼容端点（原 dsh CLI +
BayMax MCP 栈 127.0.0.1:8100-8104 已退役；模型默认 glm-5.3-flash，``QM_NIGHT_POOL_MODEL``
可覆写）。**大脑失败/输出非 JSON/池为空一律不写 `{target}_agent_picks.json`**——该后缀
在读侧（`decision_context_source.pool_path`）优先于 `_picks.json`，空壳会遮蔽在产池
（2026-09-15 事故：主跑失败落 0 只池 → 当天全系统无候选池）；研究纪要与对话流照写。
两条 cron 已重新启用。

输出三处：
1. logs/night_pool/{date}.md/.json（研究纪要）
2. quantmind 报告目录 {date}_agent_picks.json（与现有 select_from_reports 格式兼容，
   明晨 load_pool 自动优先进该池——多个 agent 选股先从这里找）
3. 对话流（pseudo agent『研究总控』，data/agent_data_astock/market-research/）→ 模型对话页可见

用法：python scripts/night_pool_agent.py [--date 2026-09-03] [--if-missing]
cron：北京 02:30 周二~周六（JST 03:30）→ 等 quantdb 当日分区落盘后产「当日」池；
     另加 04:30 幂等补跑（--if-missing：目标日池已存在则跳过），覆盖同步延迟。
     数据未落（分区 < 会话日）→ 退出码 2，宁可不产也不产陈旧池。
     大脑未产出有效池 → 退出码 3（不落空壳，等 04:30 补跑重试）。
输出命名：池文件按**目标交易日** `{target}_agent_picks.json`（09:35 消费的就是当日池）；
         纪要 logs/night_pool/{session}.md/.json 按数据会话日。
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "prompts"))

from trading_cal import (
    is_trading_day,
    next_trading_day,  # noqa: E402
    prev_trading_day,
)
from stock_industry import load_industry_map  # noqa: E402
from backend.shared.env_flags import normalize_env_flag  # noqa: E402
from backend.shared.text_compact import short_clause  # noqa: E402

NIGHT_POOL_DIR = ROOT / "logs" / "night_pool"
PICKS_DIR = ROOT / "data" / "reports" / "stock_picks"
#: 产池 QQ 推送的去重状态（同目标交易日只推一次；未送达不记 → 下轮补推）
PUSH_STATE = ROOT / "data" / "logs" / ".night_pool_push_state.json"

#: repo .env（与 live_model_analysis._load_env 同口径：环境变量优先，文件补缺）
ENV_FILE = ROOT / ".env"

#: 研究大脑：直连 OpenAI 兼容端点（原 dsh CLI + BayMax MCP 栈已退役，2026-10-14 改接）。
#: 默认走 GLM 快端点（原 dsh 调用的同款选择，任务里时间盒 90~150s）；QM_NIGHT_POOL_MODEL 可覆写。
DEFAULT_BRAIN_MODEL = "glm-5.3-flash"
BRAIN_TIMEOUT_S = 420  # 原 dsh 调用同额，HTTP 侧留裕量
#: glm 族是「始终思考」模型：reasoning token 计入 completion_tokens，8000 会被思考吃光
#: 导致 length 截断（2026-10-15 实测首跑 exit 3）。故：思考降到 low 档
#: （该端点官方档位 low/high/max，thinking 字段直接 400 code 1210）+ 预算翻倍到 16k。
BRAIN_MAX_TOKENS = 16000  # 20 只池 + 板块 JSON 约 2~3k tokens，思考 low 档再留足余量


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
        return False, (
            f"quantdb 最新分区 {d0 or '（无）'} 落后于会话日 {session}"
            "——宁可不产也不产陈旧池（等 04:30 幂等补跑）"
        )
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
    return (
        f"\n⚠️ 最新收盘日为 {d0}，距今已 {idle} 个自然日（周末/长假休市），"
        "行情数据止于该收盘日；复市首日受休市期间增量信息影响可能跳变，"
        "判断需以下方休市窗口新闻简报为准，无法核实的信息不要臆测。"
    )


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
            lines.append(
                f"【新闻 agent·最新分子（{str(latest.get('ts'))[:16]}）】\n{text}"
            )
        decisions = latest.get("decisions") or []
        if str(os.getenv("NEWS_DECISION_ENABLED", "true")).strip().lower() == "false":
            decisions = []
        if isinstance(decisions, list):
            order = {"P0": 0, "P1": 1, "P2": 2, "WATCH": 3}
            rows = [d for d in decisions if isinstance(d, dict)
                    and str(d.get("decision") or "") in order]
            rows.sort(key=lambda d: order.get(str(d.get("decision")), 9))
            if rows:
                seg = ["【新闻 agent·决策（P0 禁买 / P2 不追）】"]
                for d in rows[:10]:
                    seg.append(
                        f"- {d.get('decision')} {d.get('name')}({d.get('code')}) "
                        f"{d.get('event_type')} · {d.get('reason')}"
                    )
                lines.append("\n".join(seg))
    except (OSError, _json.JSONDecodeError):
        pass

    # 2) 今日各轮要点（宏观/主题/持仓信号，跨轮合并去重）
    try:
        rounds = []
        for line in (
            (nb / "history.jsonl")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        ):
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
                seg.append(
                    f"- {str(b.get('ts'))[11:16]} 宏观:{m.get('view', '—')}"
                    f"(bias {m.get('bias', 0):+.1f})"
                    + "；主题:"
                    + "、".join(t.get("name", "") for t in (b.get("themes") or [])[:2])
                )
                for h in (b.get("holdings") or [])[:3]:
                    seg.append(
                        f"    · {h.get('name')}({h.get('code')}) {h.get('verdict')}"
                        f" impact{float(h.get('impact') or 0):+.0f} {h.get('event_type')}"
                    )
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
            seg = [
                f"【{day} 新闻提及个股→当日实际走势（晚间复盘档案，验证新闻可靠性）】"
            ]
            for m in ms[:10]:
                mv = m.get("move")
                seg.append(
                    f"- {m.get('name')}({m.get('code')}) [{m.get('event')}] "
                    f"当日{f'{mv:+.2f}%' if mv is not None else '无数据'}"
                )
            lines.append("\n".join(seg))
        break

    if not lines:
        return (
            "\n【新闻 agent 产物】今日无分子（19:05 全景轮未跑或管线异常）——"
            "本次研究不带新闻窗口信息，观点只基于初筛分数与盘面。\n"
        )
    return (
        "\n以下为『新闻 agent 管线』今日产物（门卫→宏观/板块/持仓→主编分子，"
        "非原始新闻流；判断直接引用其结论并注明）：\n\n" + "\n\n".join(lines) + "\n\n"
    )


def _research_block(target: str) -> str:
    """研究总控最终稿（夜间综合）→ 产池提示词里的权威结论段。

    研究总控 ``night`` 档 02:10 先跑、本脚本 02:30 后跑：正常情况下拿到的是同一
    ``target`` 的最终稿（方向/资金/新闻/模型分数/板块/风险）。拿不到（当日未跑、
    首次上线、档案被清）返回空串——由调用方在提示词里注明「无」，绝不臆造结论。
    """
    try:
        from backend.shared.research_control.dossier import brief_for_prompt

        return brief_for_prompt(target)
    except Exception as exc:  # noqa: BLE001 研究总控不可用不挡产池
        print(f"⚠️ 研究总控 brief 读取失败: {exc}")
        return ""


def _db():
    import duckdb

    return duckdb.connect()


def latest_dt() -> str:
    import glob

    for base in (
        ROOT / "data/quantdb/1_kline_data/daily_backward",
        Path("/data/quantdb/1_kline_data/daily_backward"),
    ):
        if base.is_dir():
            parts = sorted(glob.glob(f"{base}/dt=*"))
            return parts[-1].rsplit("dt=", 1)[-1]
    return ""


def load_names_industry() -> dict:
    import glob

    names = {}
    for base in (
        ROOT / "data/quantdb/2_base_sector/instrument_detail",
        Path("/data/quantdb/2_base_sector/instrument_detail"),
    ):
        f = base / "instrument_detail.parquet"
        if not f.is_file():
            continue
        try:
            con = _db()
            for sym, nm in con.execute(
                "SELECT Symbol, Name FROM read_parquet(?)", [str(f)]
            ).fetchall():
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
    for b in (
        ROOT / "data/quantdb/1_kline_data/daily_backward",
        Path("/data/quantdb/1_kline_data/daily_backward"),
    ):
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
            LEFT JOIN read_parquet('{f1}') b USING (symbol)"""
    ).df()
    df = df.dropna(subset=["c1", "c0", "amt"])
    df["chg1"] = (df["c1"] / df["c0"] - 1) * 100
    df["turn_est"] = (
        df["v1"] * df["c1"] / 10000
    )  # 成交额估算亿元（对照 amount 更稳则用 amt）
    df = df[df["amt"] >= 20000]  # ≥2 亿元成交（万元口径）
    df = df[(df["chg1"] >= -3) & (df["chg1"] <= 8)]
    # Alpha 因子 join（alpha_library 最近 ≤7 日分区；缺失则降级为基础三项打分）
    alpha_cols = ["a158_ROC20", "a158_ROC5", "a158_STD20"]
    alpha_df = None
    alpha_parts = sorted(
        glob.glob(f"{ROOT / 'data/quantdb/6_ml_datasets/alpha_library'}/dt=*")
    )
    for p in reversed(alpha_parts[-8:]):
        pdt = p.rsplit("dt=", 1)[-1]
        if pdt <= d0 and glob.glob(f"{p}/*.parquet"):
            try:
                alpha_df = con.execute(
                    f"SELECT symbol, {', '.join(alpha_cols)} FROM read_parquet('{p}/*.parquet')"
                ).df()
                alpha_df = alpha_df.rename(
                    columns={
                        "a158_ROC20": "mom20",
                        "a158_ROC5": "roc5",
                        "a158_STD20": "std20",
                    }
                )
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
            f"FROM read_parquet('{ms_files[-1]}')"
        ).df()
        df = df.merge(ms, on="symbol", how="left")
        base = (
            df["chg1"].clip(-3, 8) * 0.4
            + df["momentum_3d"].fillna(0).clip(-15, 15) * 0.35
            + (df["buy_pressure"].fillna(0.5) - df["sell_pressure"].fillna(0.5))
            * 100
            * 0.25
        )
    else:
        base = df["chg1"] * 0.6 + df["turn_est"] / 10000 * 0.4
    if "mom20" in df.columns:
        # Alpha 因子增强（可解释三项，各项 clip 到 ±1）：
        #   mom20 20日趋势确认 +；roc5 短线过热反转保护 -；std20 低波优先 -
        mom20 = df["mom20"].fillna(0).clip(-20, 20) / 20
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
        row = {
            "code": r["symbol"],
            "name": str(r["name"]),
            "chg1": round(float(r["chg1"]), 2),
            "amount_yi": round(float(r["amt"]) / 10000, 2),
            "score": round(float(r["score"]), 2),
        }
        if (
            "mom20" in r
            and r.get("mom20") == r.get("mom20")
            and r.get("mom20") is not None
        ):
            row["alpha"] = {
                k: round(float(r[k]), 2)
                for k in ("mom20", "roc5", "std20")
                if r.get(k) == r.get(k)
            }
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


#: 池代码三种可接受写法：``600000.SH`` / ``SH600000`` / 裸 6 位（市场由主表反查确定）
_POOL_CODE_RE = re.compile(r"^(?:(SH|SZ|BJ)(\d{6})|(\d{6})\.(SH|SZ|BJ))$")


def normalize_pool_code(raw: object) -> str:
    """池代码 → ``600000.SH`` 后缀式（quantdb/行情层口径）；无法识别返回 ``""``。

    **不猜市场**：裸 6 位码不在这里补后缀（同样开头既有 SH 也有 SZ 的历史遗留不存在，
    但猜错市场等于造一个幽灵码），交给 :func:`validate_pool_rows` 用证券主表反查。
    """
    code = str(raw or "").strip().upper()
    m = _POOL_CODE_RE.fullmatch(code)
    if not m:
        return ""
    if m.group(3):  # 已经是 6 位 + 后缀
        return code
    return f"{m.group(2)}.{m.group(1)}"


def _resolve_pool_code(
    raw_code: str,
    name: str,
    known: dict,
    by_six: dict,
    by_name: dict,
) -> tuple[str, str]:
    """单行代码解析 → ``(正确代码, 失败原因)``（成功时代码非空、原因空）。"""
    canon = normalize_pool_code(raw_code)
    if canon and canon in known:
        return canon, ""
    if re.fullmatch(r"\d{6}", raw_code):
        hits = by_six.get(raw_code) or []
        if len(hits) == 1:
            return hits[0], ""
    hits = (by_name.get(name) or []) if name else []
    if len(hits) == 1:
        return hits[0], ""
    if len(hits) > 1:
        return "", f"名称「{name}」对应多个代码 {sorted(hits)}，无法确定"
    if canon or re.fullmatch(r"\d{6}", raw_code):
        return (
            "",
            f"代码 {canon or raw_code} 不在证券主表，名称「{name or '空'}」无法反查",
        )
    return "", f"代码「{raw_code or '空'}」格式非法，名称「{name or '空'}」无法反查"


def validate_pool_rows(
    rows: object, names: dict
) -> tuple[list[dict], list[tuple[dict, str]], list[str]]:
    """大脑池行 name↔code 交叉校验：拦截幽灵代码，回填可唯一确定的错码。

    ``names`` = 证券主表 ``symbol → name``（``load_names_industry()``，候选初筛条目
    另行并入——两者都是系统数据，互为兜底）。

    规则（**代码是权威键**，名称只在代码不可信时用来救回）：
    1. code 命中主表 → 保留，名称以主表为准（模型写错名称不毁代码）；
    2. code 不可识别/不在主表 → 用 name 反查：唯一命中则回填正确代码
       （2026-10-19 事故：模型把亿田智能写成 ``600930.SZ``，理由里甚至写了
       「300911.SZ更正」，但 code 字段没回填）；无命中/多命中则**丢弃该行**；
    3. 回填后重复代码去重（保留首次出现——池行本身按得分排序）。

    返回 ``(保留行, [(被拦行, 原因)], [校正说明])``。纯函数：不做 IO、不改入参。

    **空主表时必须由调用方跳过校验**（本函数拿到空表会把整池拦光）：主表不可用
    时幽灵码风险仍在，但「没有池」比「池里有一只查不到行情的票」坏得多。
    """
    known: dict[str, str] = {}
    by_six: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for sym, nm in (names or {}).items():
        code = normalize_pool_code(sym) or str(sym or "").strip().upper()
        if not code:
            continue
        known[code] = str(nm or "").strip()
        by_six.setdefault(code.split(".")[0], []).append(code)
        label = str(nm or "").strip()
        if label:
            by_name.setdefault(label, []).append(code)

    kept: list[dict] = []
    blocked: list[tuple[dict, str]] = []
    notes: list[str] = []
    seen: set[str] = set()
    for row in rows or ():
        if not isinstance(row, dict):
            blocked.append(({}, f"非对象行：{row!r}"))
            continue
        raw_code = str(row.get("code") or "").strip()
        raw_name = str(row.get("name") or "").strip()
        code, why = _resolve_pool_code(raw_code, raw_name, known, by_six, by_name)
        if not code:
            blocked.append((dict(row), why))
            continue
        if code in seen:
            blocked.append((dict(row), f"重复代码 {code}（保留首次出现）"))
            continue
        canon_name = known.get(code) or raw_name
        if normalize_pool_code(raw_code) != code and raw_code.upper() != code:
            notes.append(f"{raw_code or '空'}→{code}（{canon_name}）")
        elif raw_name and canon_name and raw_name != canon_name:
            notes.append(
                f"名称「{raw_name}」→「{canon_name}」（代码 {code}，以主表为准）"
            )
        seen.add(code)
        kept.append({**row, "code": code, "name": canon_name})
    return kept, blocked, notes


def _safe_score(value: object) -> float:
    """大脑分的容错转换：非数字按 0 记（一行脏分数不该丢整池）。"""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _load_env() -> dict:
    """环境变量优先，repo .env 补缺（``live_model_analysis._load_env`` 同口径）。"""
    import os

    env = dict(os.environ)
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k and k not in env:
                env[k] = v.strip().strip('"')
    except OSError:
        pass
    return env


# ── 产池 QQ 推送（2026-10-20 用户：「top20 的股票板块都要发我」）──────────

_PUSH_RULE = "━" * 14
#: 方向原文是一整段策略叙述，卡片只留首句（同 ``text_compact.short_clause``）。
_CARD_DIRECTION_MAX = 50
#: 板块条目上限：池卡的重点是 20 只票，板块只是背景。
_CARD_SECTORS_MAX = 6


def format_pool_card(target: str, payload: dict, picks: list) -> tuple:
    """晚间研究产池后的 QQ 卡片（纯函数）→ ``(title, content)``。

    行口径：``序号 名称 代码 · 板块 · 分数``；板块缺失显示 ``—``（不编造）。
    """
    title = f"🌙 晚间股票池 · {_dash8(target)[5:]}"
    lines = [_PUSH_RULE]
    view = short_clause(
        str(payload.get("market_view") or payload.get("direction") or ""),
        max_chars=_CARD_DIRECTION_MAX,
    )
    if view:
        lines.append(f"🧭 方向：{view}")
    sector_lines = []
    for item in (payload.get("sectors") or [])[:_CARD_SECTORS_MAX]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        strength = str(item.get("strength") or "").strip()
        reason = short_clause(
            str(item.get("reason") or ""), max_chars=30, min_clause_chars=8
        )
        head = f"{name}（{strength}）" if strength else name
        sector_lines.append(f"　{head}：{reason}" if reason else f"　{head}")
    if sector_lines:
        lines.append("🔥 板块")
        lines.extend(sector_lines)
    lines.append(_PUSH_RULE)
    lines.append(f"📦 {len(picks)} 只 · 板块 · 分数")
    for idx, pick in enumerate(picks, 1):
        code = str(pick.get("code") or "").strip()
        name = str(pick.get("name") or "").strip() or code
        industry = str(pick.get("industry") or "").strip() or "—"
        row = f"{idx:02d} {name} {code} · {industry}"
        score = pick.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            row += f" · {float(score):.1f}"
        lines.append(row)
    lines.append(_PUSH_RULE)
    lines.append("详情见 交易台 › 候选池")
    return title, "\n".join(lines)


def _load_push_state() -> dict:
    try:
        data = json.loads(PUSH_STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_push_state(state: dict) -> None:
    try:
        PUSH_STATE.parent.mkdir(parents=True, exist_ok=True)
        PUSH_STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 状态落盘失败只影响去重
        print(f"⚠️ 推送状态落盘失败: {exc}")


def notify_qq(title: str, content: str) -> bool:
    """推一条 QQ（best-effort）：失败只打印，绝不挡产池。"""
    try:
        import push_notify  # scripts/ 同目录

        push_notify.notify(title, content, channel="default")
        print(f"📨 QQ 已推送：{title}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ QQ 推送失败（不影响产池）: {exc}")
        return False


def push_pool(
    target: str, payload: dict, picks: list, *, env: dict | None = None
) -> bool:
    """产池后推一条 QQ；同目标交易日只推一次（未送达不记状态，下轮补推）。

    开关 ``QM_NIGHT_POOL_PUSH`` 走全仓唯一词表（``env_flag`` 口径：只有 ``true``
    判开）；``env`` 缺省时读进程环境 + repo ``.env``。
    """
    env = _load_env() if env is None else env
    if not normalize_env_flag(env.get("QM_NIGHT_POOL_PUSH"), default=True):
        print("⏭️ 晚间股票池推送关闭（QM_NIGHT_POOL_PUSH 非 true）")
        return False
    state = _load_push_state()
    pushed = state.get("pushed")
    pushed = dict(pushed) if isinstance(pushed, dict) else {}
    if pushed.get(target):
        print(f"⏭️ 晚间股票池已推送（{target}），不重复")
        return False
    title, content = format_pool_card(target, payload, picks)
    if not notify_qq(title, content):
        return False
    pushed[target] = datetime.now().astimezone().isoformat(timespec="seconds")
    state["pushed"] = pushed
    _save_push_state(state)
    return True


def _brain_endpoint(model: str, env: dict) -> tuple:
    """(base, key)：glm* 走 GLM_* 键，其余走 OPENAI_*（DEEPSEEK_* 兜底）。"""
    if model.startswith("glm"):
        label = "GLM"
        base = env.get("GLM_API_BASE", "")
        key = env.get("GLM_API_KEY", "")
    else:
        label = "OPENAI"
        base = env.get("OPENAI_API_BASE", "") or env.get("DEEPSEEK_BASE_URL", "")
        key = env.get("OPENAI_API_KEY", "") or env.get("DEEPSEEK_API_KEY", "")
    base = base.rstrip("/")
    if not base or not key:
        raise RuntimeError(f"{label}_API_BASE/KEY 缺失")
    return base, key


def call_brain(task: str, model: str = "", timeout_s: int = BRAIN_TIMEOUT_S) -> tuple:
    """直连 LLM 跑研究任务（重试 1 次；失败/截断 raise，由 run() 降级为不产池）。

    ``finish_reason=length``（max_tokens 截断）即 raise：本任务输出是
    JSON，半截正文抽不出完整对象，救捞只会把残缺池当天产出。
    """
    import requests

    env = _load_env()
    model = (
        model
        or str(env.get("QM_NIGHT_POOL_MODEL") or "").strip()
        or DEFAULT_BRAIN_MODEL
    )
    base, key = _brain_endpoint(model, env)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是量化投研的晚间市场研究总控（A股）。"
                "先按任务要求输出 Markdown 正文，最后附一个 ```json 机读块；"
                "除该机读块外不要输出其他代码围栏。",
            },
            {"role": "user", "content": task},
        ],
        "temperature": 0.2,
        "max_tokens": BRAIN_MAX_TOKENS,
    }
    if model.startswith("glm"):
        # glm 族始终思考、thinking 不可关闭（error 1210），官方档位 low/high/max；
        # 本任务=按给定数据出结构化 JSON，low 最省且实测 reasoning=0（2026-10-15 探针）。
        payload["reasoning_effort"] = "low"
    last_exc = None
    for attempt in range(2):
        try:
            resp = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=timeout_s,
            )
            resp.raise_for_status()
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message", {}) or {}
            content = (
                str(msg.get("content") or "").strip()
                or str(msg.get("reasoning_content") or "").strip()
            )
            usage = data.get("usage") or None
            if usage:
                usage = {
                    k: int(usage.get(k) or 0)
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                }
            if str(choice.get("finish_reason") or "") == "length":
                raise RuntimeError("输出被 max_tokens 截断（半截 JSON 不可救）")
            if not content:
                raise RuntimeError("空回复")
            return content, usage
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == 0:
                print(f"  ⚠️ 研究大脑调用失败，重试 1 次: {exc}")
    raise last_exc  # type: ignore[misc]


def run(session: str, target: str, dry: bool = False) -> int:
    """session=数据会话日(YYYYMMDD)；target=该池服务的目标交易日(YYYYMMDD)。"""
    cands, d0 = build_candidates(session)
    if not cands:
        print(f"❌ 无候选（数据缺失，分区 {d0 or '无'}）")
        return 1
    if d0 < session:  # main 已先拦一次；直调 run() 的兜底（宁可不产，不产陈旧池）
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
                print(
                    f"  ⛔ 剔除风险标的 {c.get('code')} {c.get('name') or ''}："
                    f"{hit.get('reason')}"
                )
            if not cands:
                print("❌ 候选全部命中风险清单，本轮不产出池")
                return 1
            if dropped:
                print(
                    f"  候选 {before} → {len(cands)}（风险清单剔除 {len(dropped)} 只）"
                )
    except Exception as exc:  # noqa: BLE001 风险清单不可用不阻塞研究
        print(f"⚠️ 风险清单不可用: {exc}")
    cand_json = json.dumps(cands, ensure_ascii=False)
    news = _news_block(d0)  # 休市窗口新闻简报（平日晚间为空）
    research = _research_block(target)  # 研究总控夜间综合稿（权威口径）
    if research:
        print(f"  🧠 研究总控 brief 已注入（{len(research)} 字）")
    else:
        print("  ⚠️ 无研究总控 brief（夜间综合尚未产出）：本轮只按初筛+新闻产池")
    research_part = (
        f"\n【研究总控结论（{_dash8(target)} 交易日前瞻 · 系统级综合判断）】\n{research}\n"
        if research
        else "\n【研究总控结论】本次无（夜间综合稿尚未产出）——方向/资金/新闻/模型分数"
        "只能按本任务输入判断，不得编造。\n"
    )
    time_box = 150 if news else 90  # 有简报时放宽思考时间盒
    task = f"""[晚间市场研究任务] 基于 quantdb {d0} 收盘数据已初筛出 {len(cands)} 只候选（附分数/涨跌/成交额）。
本池将用于 {_dash8(target)} 开盘后的实盘决策（行情数据止于 {_dash8(d0)} 收盘）。

要求（时间盒约 {time_box} 秒，单次调用完成，无逐只取数环节）：
0. **研究总控优先**：下方【研究总控结论】是系统级当日综合判断（方向/资金/新闻/模型分数/
   板块/风险），本池必须与之一致；若有分歧，在 market_view 里写明分歧点与依据。
   它未覆盖的项才按本任务输入判断，不得编造。
1. 初筛数据已系统算好（涨跌/成交额/复合分/名称），直接使用；池行的 code 必须
   逐字照抄候选初筛（含 .SH/.SZ 后缀），name 必须与 code 对应，不得凭记忆改写或
   自行补全代码；只有当名称缺失或分数明显异常（如 ST/涨跌停残留）时，在 reason 里标注存疑。
2. 新闻输入只来自『新闻 agent 管线』（门卫→宏观/板块/持仓→主编），引用其结论时注明；
   不要臆测管线之外的新闻。
3. 推进股票池**不看任何 HOLD/BUY 侧标签**（历史占位标签，无信息量）：
   由你按 初筛分数 + 新闻分子的板块主线与持仓信号 + 昨日新闻→走势回顾 综合判断，
   给出 20 只池（code/name/score/一句话选股理由），按行业分散、剔除重复题材；
   若认为某持仓/题材应换掉，在 reason 里写明换仓逻辑。
4. 若现有持仓 688183/600309/300750 等本身强势也可保留注明。
5. 候选中【事件】字段标注解禁/质押/回购风险：近期大比例解禁或质押≥50%的标的
   建仓需在理由中明确风险对冲，无事件字段=无已知事件风险。
6. **新闻决策硬约束**：若上方出现 P0 名单，这些 code 一律不得进入 pool；P2 是「不追/不加仓」，
   不得作为买入理由；WATCH 只观察。新闻层不产生买入信号。
盘面状态：{state}{_gap_note(d0)}
{news}
{research_part}
候选初筛：{cand_json}

输出两段，缺一不可：
A. Markdown 正文（≤1200 字）：
## 明日方向
一句话结论 + 3 条依据（引用研究总控时注明）。
## 资金判断
增量/存量、风格与仓位建议。
## 新闻与模型分数
新闻主线如何影响明日；模型批次分数（BUY/HOLD/SELL、均值、边际变化）怎么读。
## 板块主线
强/弱板块各 3 个，写明理由。
## 明日池说明
选池逻辑、换仓点、主要风险。
B. 最后附机读块（严格 JSON，无注释、无尾逗号）：
```json
{{"date":"{d0}","market_view":"一句话方向（≤60字）","direction":"完整方向叙述",
 "capital":"资金判断","news":"新闻要点","models":"模型分数解读",
 "sectors":[{{"name":"","strength":"","reason":""}}],
 "risks":["..."],"pool":[{{"code":"","name":"","score":0,"reason":""}}]}}
```
"""
    if dry:
        print(task[:2500])
        return 0
    try:
        content, usage = call_brain(task)
        brain_ok = True
    except Exception as exc:  # noqa: BLE001
        content, usage = f"（研究 agent 失败：{exc}）", None
        brain_ok = False
        print(f"❌ 研究大脑调用失败：{exc}")
    # 机读块抽取：优先 ```json 围栏，其次平衡 JSON（正文里的花括号不干扰）。
    from backend.shared.research_control.runner import extract_machine

    payload = extract_machine(content)
    payload_ok = bool(payload)
    if not payload_ok:
        payload = {"date": d0, "pool": [], "market_view": content[:400]}
    payload["date"] = d0
    dpath = NIGHT_POOL_DIR
    dpath.mkdir(parents=True, exist_ok=True)
    (dpath / f"{session}.md").write_text(
        f"# 晚间市场研究 · {session}（服务 {target}）\n\n{content}", encoding="utf-8"
    )
    (dpath / f"{session}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    # 候选池写 quantmind 报告目录（兼容 select_from_reports：side/score/reason）。
    # side 恒为 HOLD 占位（无信息量）——消费端（load_pool/pool_rows/提示词）已不再展示，
    # 判断全部由各 agent 依据分数+大盘+板块+新闻综合给出。
    # 命名按**目标交易日**（2026-09-12 P1-4）：09:35 消费端按日期取最新 → 当日池自然胜出。
    # **池空一律不落文件**：_agent_picks.json 在 pool_path 里优先于 _picks.json，
    # 空壳会把在产池整个遮蔽成「当天无池」（2026-09-15 事故）——没有池 ≠ 没有股票。
    produced = False
    try:
        # 代码校验（2026-10-19）：模型会在结构化输出里写错 code（当日 600930.SZ/
        # 亿田智能，正确是 300911.SZ），旧实现逐字落盘 → 下游按码查行情恒空，
        # 该票整日无人监控。真值表 = 证券主表 + 候选初筛（都是系统数据）。
        # 主表与候选同时为空时**放行**并告警：宁可有未校验的码，不可整池丢失。
        try:
            known_names = load_names_industry()
        except Exception as exc:  # noqa: BLE001 主表读失败不阻塞写池
            known_names = {}
            print(f"⚠️ 证券主表读取失败，代码校验降级: {exc}")
        for c in cands:
            _code = normalize_pool_code(c.get("code")) or str(c.get("code") or "")
            if _code and c.get("name"):
                known_names.setdefault(_code, str(c.get("name")))
        raw_rows = [r for r in (payload.get("pool") or []) if isinstance(r, dict)]
        if known_names:
            rows, blocked_rows, notes = validate_pool_rows(raw_rows, known_names)
            for bad, why in blocked_rows:
                print(f"  ⛔ 拦截池行 {bad.get('code')} {bad.get('name') or ''}：{why}")
            for note in notes:
                print(f"  🔧 池行校正：{note}")
        else:
            rows, blocked_rows, notes = raw_rows, [], []
            print("⚠️ 证券主表与候选均为空，池代码未经校验直接落盘")
        _events = {
            (normalize_pool_code(c.get("code")) or str(c.get("code") or "")): (
                c.get("事件") or ""
            )
            for c in cands
        }
        # 板块随池落盘（2026-10-20 用户：「top20 的股票板块都要发我」）；
        # 主表读不到只降级为空串，消费面显示 —，绝不编造。
        try:
            industry_map = load_industry_map()
        except Exception as exc:  # noqa: BLE001 板块是装饰，不得挡产池
            industry_map = {}
            print(f"⚠️ 板块表读取失败（industry 留空）: {exc}")
        pool = []
        for p in rows[:20]:
            code = normalize_pool_code(p.get("code")) or str(p.get("code") or "")
            pool.append(
                {
                    "code": p.get("code"),
                    "name": p.get("name"),
                    "side": "HOLD",
                    "score": _safe_score(p.get("score")),
                    "industry": industry_map.get(code, ""),
                    "events": _events.get(str(p.get("code")), ""),
                    "reason": str(p.get("reason") or "")[:160],
                }
            )
        if pool:
            # 方向/资金/新闻/模型分数随池落盘：消费端（决策上下文、候选池页）先看
            # ``direction``（80 字短句，旧契约），``summary`` 等长字段供研究面展开。
            view = str(
                payload.get("market_view") or payload.get("direction") or ""
            ).strip()
            detail = str(payload.get("direction") or view).strip()
            sectors = payload.get("sectors")
            risks = payload.get("risks")
            out = {
                "date": target,
                "session": session,
                "market_direction": {
                    "direction": view[:80],
                    "summary": detail[:800],
                    "capital": str(payload.get("capital") or "")[:600],
                    "news": str(payload.get("news") or "")[:600],
                    "models": str(payload.get("models") or "")[:600],
                    "sectors": sectors if isinstance(sectors, list) else [],
                    "risks": risks if isinstance(risks, list) else [],
                },
                "research_brief": bool(research),
                "picks": pool,
            }
            PICKS_DIR.mkdir(parents=True, exist_ok=True)
            (PICKS_DIR / f"{target}_agent_picks.json").write_text(
                json.dumps(out, ensure_ascii=False), encoding="utf-8"
            )
            produced = True
            print(
                f"✅ 池文件 {target}_agent_picks.json（{len(pool)} 只，数据会话 {session}）"
            )
            try:  # 推送是旁路：失败只打印，绝不改变产池结果
                push_pool(target, payload, pool)
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️ 晚间股票池推送异常（不影响产池）: {exc}")
        else:
            print(
                f"❌ 未产出有效池（大脑成功={brain_ok}，JSON 可抽={payload_ok}）"
                f"——不写 {target}_agent_picks.json（空壳会遮蔽在产池），"
                "等 04:30 幂等补跑重试"
            )
    except OSError as exc:  # noqa: BLE001
        print(f"⚠️ 写池文件失败: {exc}")
    # 对话流：pseudo『研究总控』（归到该池服务的交易日——周一早班看得到周一的池）
    try:
        lf = (
            ROOT
            / "data"
            / "agent_data_astock"
            / "market-research"
            / "log"
            / _dash8(target)
            / "log.jsonl"
        )
        lf.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "signature": "market-research",
            "kind": "night_pool",
            "new_messages": [
                {
                    "role": "user",
                    "content": f"【晚间市场研究任务】{d0} 收盘数据 → {_dash8(target)} 板块+候选池"
                    + (
                        "（已注入研究总控夜间综合稿）"
                        if research
                        else "（无研究总控稿，仅按初筛+新闻）"
                    ),
                },
                {"role": "assistant", "content": content},
            ],
        }
        if usage:
            row["usage"] = usage  # /token-usage 只看带 usage 的行（与对话轮同口径）
        with lf.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:  # noqa: BLE001
        print(f"⚠️ 写对话流失败: {exc}")
    if produced:
        print(f"✅ 晚间研究完成 → logs/night_pool/{session}.md/.json（服务 {target}）")
        return 0
    return 3


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--date", default="", help="手工复跑：数据会话日（YYYY-MM-DD / YYYYMMDD）"
    )
    ap.add_argument("--dry", action="store_true")
    ap.add_argument(
        "--if-missing",
        action="store_true",
        help="幂等补跑（04:30）：目标日池文件已存在则立即退出",
    )
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
        # 空池 = 研究 agent 失败（如大脑端点半途断流）落下的空壳，
        # 不是「已产出」——2026-09-15 实录：02:30 主跑失败落 0 只池，
        # 03:30 补跑因文件存在直接跳过，当天全系统无候选池。此处按非空才算完成。
        # （2026-10-14 起失败端根本不落池文件，此处是存量空壳的兼容分支）
        pool_n = -1
        try:
            pool_n = len(
                json.loads(
                    (PICKS_DIR / f"{target}_agent_picks.json").read_text(
                        encoding="utf-8"
                    )
                ).get("picks")
                or []
            )
        except (OSError, ValueError, AttributeError):
            pool_n = -1
        if pool_n > 0:
            print(
                f"⏭️ {bj:%F %T} 目标日池已存在（{target}_agent_picks.json，{pool_n} 只），补跑跳过"
            )
            return 0
        print(
            f"⚠️ {bj:%F %T} 目标日池为空/不可读（{target}_agent_picks.json）"
            f"——视为未产出，执行补跑"
        )
    ok, why = check_data_fresh(session)
    if not ok:
        print(f"⏸️ {bj:%F %T} {why}")
        return 2
    print(f"🌙 {bj:%F %T} 晚间研究：数据会话 {session} → 服务交易日 {target}")
    return run(session, target, dry=a.dry)


if __name__ == "__main__":
    sys.exit(main())
