#!/usr/bin/env python3
"""港股盘中分析循环（宿主机，交易时段每小时一轮）——2026-10-08 随富途通道恢复。

端口自旧栈 quant-Trader ``scripts/live_hourly_analysis_hk.py``（617 行），差异：

- **取数走 QM 自身**：``http://127.0.0.1:8000/api/v1/agent-arena/futu/*``（内部信任头，
  宿主脚本免 JWT；任何日志都不打印密钥）。HTTP 不可用时降级 ``docker exec quantmind``
  直调 ``futu_subprocess.py account_both``（零密钥路径，容器里跑得通就行）。
- **提示词/落盘口径照旧**：系统提示词用 ``analysis_modes.MARKET_RULES["hk"]`` +
  各模型选中的分析配置（``selected_modes(agent, market="hk")``，comp-config 无 hk 段
  → 基线模式）；写 ``data/agent_data_hk/{sig}/log/{北京日期}/log.jsonl``，结构逐字
  不变，前端「模型对话（港股）」直接可读。
- **不再引用**已退役的 Tiger 券商链路与 quanthk 本地日线（后者停更）：持仓的
  历史趋势段整段省略（候选池行自带动量列，口径一致且新鲜）。
- **执行默认不启用**（与 A 股 ``intraday_exec.json`` 隔离的双层把关）：
  ① 提示词里不出现 decisions schema 契约（``--decisions`` 才追加）；
  ② ``configs/hk_exec.json`` 不存在 ⇒ ``enabled`` 恒 False（今夜不创建该文件）。
  真执行时下到 ``/futu/place env=SIMULATE``（HK v1 只加仓已有持仓、整手 100 股、
  单票 ≤ 剩余现金 20%）；成交/委托回执由后端 ``qq_notify`` 推港股通道，本脚本
  **不重复外发**。交易日志 ``logs/live_trade_hk_{YYYYMMDD}.jsonl`` + ``mode`` 固定
  ``execute_hk``（文件名与 mode 双保险，防串进 A 股 feed）。

用法：
    python3 scripts/live_hourly_analysis_hk.py               # 交易时段内一轮（cron 用）
    python3 scripts/live_hourly_analysis_hk.py --force       # 忽略时段（补跑/调试）
    python3 scripts/live_hourly_analysis_hk.py --force --decisions --dry-run
    python3 scripts/live_hourly_analysis_hk.py --agents deepseek-v4-flash
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

CN_TZ = timezone(timedelta(hours=8))

API_BASE = "http://127.0.0.1:8000"
ARENA = "/api/v1/agent-arena"
NEWS_PATH = "/api/v1/news/articles"
SERVICE_TAG = "keeper-hk"

DATA_DIR = ROOT / "data" / "agent_data_hk"
POOL_FILE = ROOT / "data" / "hk_picks.json"
LOCK = ROOT / "data" / "logs" / ".live_hourly_analysis_hk.lock"
EXEC_CONFIG = ROOT / "configs" / "hk_exec.json"
TRADE_LOG_DIR = ROOT / "logs"

# 零密钥降级路径（与 P0 探活同一条命令）
QM_CONTAINER = "quantmind"
OPEND_CONTAINER = "futu-opend"
OPEND_HOST, OPEND_PORT = "futu-opend", "11111"
OPEND_RSA_KEY = "/data/futu-opend/rsa.key"

TIMEOUT_S = 180          # LLM 单次调用
HTTP_TIMEOUT_S = 30      # 账户/快照
EXEC_TIMEOUT_S = 30      # 容器内 futu 子进程（健康时 ~4s；未登录会挂等握手）
NEWS_TIMEOUT_S = 10
MAX_TOKENS = 4000
NEWS_HOURS = 8
NEWS_PER_KEYWORD = 5
NEWS_MAX_KEYWORDS = 5

POOL_MAX_STALE_DAYS = 5  # 候选池陈旧超过 5 个交易日 → 降级为「仅持仓复盘」
BUY_CAP_PCT = 0.2        # 单笔买入 ≤ 剩余现金的 20%（与旧栈同口径）
LOT_SIZE = 100           # 港股最小交易单位（v1 按整手 100 估）

#: 名册：签名 →（凭据 env 前缀, 实际调用的模型名）。签名即 agent_data_hk 下的目录名，
#: 与前端模型列表（扫目录）天然一致；与 live_model_analysis.py（A股）同族。
ROSTER: dict[str, tuple[str, str]] = {
    "deepseek-v4-flash": ("OPENAI", "deepseek-v4-flash"),
    "deepseek-v4-pro": ("OPENAI", "deepseek-v4-pro"),
    "glm-5.3-flash": ("GLM", "glm-5.3-flash"),
}


# ---------------------------------------------------------------- 时间/环境


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def in_trading_window(now: datetime) -> bool:
    """港股交易时段（北京）：09:30–12:00 / 13:00–16:00，周一至周五。

    **不建模港股假期**（QM 只有 A 股日历；旧栈同口径）——节假日那一轮会照跑，
    代价是几次 LLM 调用，不影响正确性；快照为空/池陈旧会在健康里如实标注。
    """
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= hm <= 12 * 60) or (13 * 60 <= hm <= 16 * 60)


def session_label(now: datetime) -> str:
    return "盘中" if in_trading_window(now) else "盘前/盘后"


def _load_env() -> dict[str, str]:
    """读仓库根 .env（与 live_model_analysis.py 同口径：不覆盖、不改 os.environ）。"""
    env: dict[str, str] = {}
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
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


def _env_get(env: dict[str, str], key: str) -> str:
    """先 .env 再进程环境（cron 可能直接注入；两者都没有 → 空串）。"""
    return env.get(key) or os.environ.get(key) or ""


# ---------------------------------------------------------------- 内部 HTTP


def _headers() -> dict[str, str]:
    """内部信任头：密钥解析复用 news_digest._internal_headers（宿主唯一出处），
    只把 ``X-Internal-Service`` 换成本循环的审计标签。密钥永不进日志。"""
    from news_digest import _internal_headers

    headers = dict(_internal_headers())
    headers["X-Internal-Service"] = SERVICE_TAG
    return headers


def _api_get(path: str, params: dict | None = None, timeout: int = HTTP_TIMEOUT_S) -> dict:
    import urllib.parse
    import urllib.request

    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _api_post(path: str, body: dict, timeout: int = HTTP_TIMEOUT_S) -> dict:
    import urllib.request

    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={**_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_account_both() -> tuple[dict | None, str]:
    """富途模拟账户（分析视角）：HTTP 优先，失败降级容器内子进程（零密钥）。

    返回 ``(simulate, error)``；两路都失败时 simulate=None 且 error 为合并原因。
    """
    http_err = ""
    try:
        data = _api_get(f"{ARENA}/futu/account-both")
        if data.get("success"):
            sim = (data.get("data") or {}).get("simulate")
            if sim:
                return sim, ""
            http_err = "account-both 无 simulate 段（OpenD 未登录？）: " + str(
                (data.get("data") or {}).get("errors") or {}
            )
        else:
            http_err = str(data.get("error") or "account-both 返回 success=false")
    except Exception as exc:  # noqa: BLE001 走零密钥降级
        http_err = f"{type(exc).__name__}: {exc}"

    try:
        out = _docker_account_both()
    except Exception as exc:  # noqa: BLE001
        return None, f"HTTP[{http_err}]；容器降级[{type(exc).__name__}: {exc}]"
    sim = (out or {}).get("simulate")
    if sim:
        return sim, ""
    return None, f"HTTP[{http_err}]；容器降级[{json.dumps(out, ensure_ascii=False)[:200]}]"


def _docker_account_both() -> dict:
    """``docker exec quantmind python3 futu_subprocess.py … account_both`` → JSON。

    子进程把结果写文件而不是 stdout（futu SDK 会往 stdout 打日志），故走临时文件；
    临时文件落在容器 ``/tmp``，读完即删，不留凭据/账户快照。
    """
    # 先探 OpenD 容器是否在跑：容器不在时 futu SDK 的连接要挂满 90s 超时才报错
    # （实测 2026-10-08），告警面被迫等一分半；这里 1 秒内给出根因。
    probe = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", OPEND_CONTAINER],
        capture_output=True, text=True, timeout=15,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise RuntimeError(
            f"{OPEND_CONTAINER} 容器未运行（docker ps 复核；未登录会停在「请输入账号」，"
            "见 docker logs futu-opend）"
        )
    remote_out = f"/tmp/futu_probe_{os.getpid()}.json"
    argv = [
        "docker", "exec", QM_CONTAINER, "python3",
        "/app/backend/services/trade/services/futu_subprocess.py",
        OPEND_HOST, OPEND_PORT, OPEND_RSA_KEY, "account_both", "{}", remote_out,
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=EXEC_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # OpenD 在跑但没登录时，SDK 会挂着等握手（实测 2026-10-08：90s 也等不到），
        # 报根因而不是把整条命令甩给告警面。
        raise RuntimeError(
            f"容器内 futu 子进程 {EXEC_TIMEOUT_S}s 无响应——OpenD 未登录时 SDK 会挂等握手"
            "（docker logs futu-opend 应停在「请输入账号」）"
        ) from None
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "子进程失败").strip()[:200])
    try:
        cat = subprocess.run(
            ["docker", "exec", QM_CONTAINER, "cat", remote_out],
            capture_output=True, text=True, timeout=30,
        )
        if cat.returncode != 0:
            raise RuntimeError((cat.stderr or "读取结果失败").strip()[:200])
        return json.loads(cat.stdout)
    finally:
        subprocess.run(
            ["docker", "exec", QM_CONTAINER, "rm", "-f", remote_out],
            capture_output=True, timeout=20,
        )


def fetch_snapshot(codes: list[str]) -> dict:
    """实时快照 ``{00700.HK: {last_price, prev_close, day_chg, …}}``；失败返回空。

    取不到价不炸本轮（提示词里该行现价退回持仓自带价并标 —）。
    """
    codes = [c for c in codes if c]
    if not codes:
        return {}
    try:
        data = _api_get(f"{ARENA}/futu/snapshot", {"codes": ",".join(codes)})
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 快照查询失败（现价退回持仓价）: {exc}")
        return {}
    if not data.get("success"):
        print(f"⚠️ 快照返回失败: {data.get('error')}")
        return {}
    return (data.get("data") or {}).get("snapshot") or {}


def load_news(names: list[str]) -> list[dict]:
    """盘中新闻（QM 新闻库按公司名关键词全文搜）；失败返回空（不阻塞分析）。"""
    keywords: list[str] = []
    for name in names:
        base = str(name).split("-")[0].strip()  # 阿里巴巴-SW → 阿里巴巴
        if base and base not in keywords:
            keywords.append(base)
    keywords.append("港股")  # 大盘面兜底

    since = (datetime.now(timezone.utc) - timedelta(hours=NEWS_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    out: list[dict] = []
    seen: set[str] = set()
    for kw in keywords[:NEWS_MAX_KEYWORDS]:
        try:
            data = _api_get(
                NEWS_PATH,
                {
                    "keyword": kw,
                    "since": since,
                    "page_size": NEWS_PER_KEYWORD,
                    "sort": "time_desc",
                },
                timeout=NEWS_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 单关键词失败静默（新闻是加分项）
            continue
        for art in data.get("articles") or []:
            title = str(art.get("title") or "").strip()[:120]
            if not title or title in seen:
                continue
            seen.add(title)
            label = str(
                ((art.get("enrichment") or {}).get("sentiment_label")) or "neutral"
            ).lower()
            if label not in ("bullish", "bearish", "neutral"):
                label = "neutral"
            bj = ""
            try:
                ts = datetime.fromisoformat(
                    str(art.get("published_at") or "").replace("Z", "+00:00")
                )
                bj = ts.astimezone(CN_TZ).strftime("%m-%d %H:%M")
            except ValueError:
                pass
            out.append({
                "title": title,
                "source": art.get("source_name") or "",
                "time": bj,
                "sentiment": label,
                "keyword": kw,
            })
    return out[:15]


def load_pool() -> tuple[dict, list[str]]:
    """候选池 ``data/hk_picks.json`` → ``(doc, notes)``。

    陈旧 > ``POOL_MAX_STALE_DAYS`` 个交易日 → 整段弃用（返回空 doc + note），
    本轮降级为「仅持仓复盘」；0 < stale_days ≤ 阈值时带 note 标注数据日，
    提示词照常注入（明确告诉模型这是哪天的分位）。
    判据用 ``stale_days``（交易日口径）而不是「日期必须等于今天」——后者在港股
    假期次日会把最新一份池子误判为过期。
    """
    if not POOL_FILE.is_file():
        return {}, ["候选池缺失（hk_picks.py 未跑？）本轮仅持仓复盘"]
    try:
        doc = json.loads(POOL_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"候选池读取失败（{type(exc).__name__}）本轮仅持仓复盘"]
    if not doc.get("picks"):
        return {}, ["候选池为空本轮仅持仓复盘"]
    try:
        stale = int(doc.get("stale_days") or 0)
    except (TypeError, ValueError):
        stale = 0
    if stale > POOL_MAX_STALE_DAYS:
        return {}, [
            f"候选池数据日 {doc.get('data_date')} 已陈旧 {stale} 个交易日（>{POOL_MAX_STALE_DAYS}）本轮仅持仓复盘"
        ]
    notes = []
    if stale > 0:
        notes.append(f"候选池数据日 {doc.get('data_date')}（陈旧 {stale} 个交易日，分位按该日计）")
    return doc, notes


# ---------------------------------------------------------------- 提示词


def build_rows(sim: dict, snaps: dict) -> list[dict]:
    """富途 SIM 持仓 → 分析行（快照价优先；无快照退回持仓自带价并标 —）。"""
    rows: list[dict] = []
    for code, pos in (sim.get("positions") or {}).items():
        if not isinstance(pos, dict):
            continue
        volume = float(pos.get("volume") or 0)
        if volume <= 0:
            continue
        cost = float(pos.get("cost") or 0)
        price = float(pos.get("price") or 0)
        snap = snaps.get(code) or {}
        if snap.get("last_price"):
            price = float(snap["last_price"])
        pnl = (price - cost) * volume
        pnl_pct = (price - cost) / cost * 100 if cost else 0.0
        day_chg = (
            round(float(snap.get("day_chg") or 0), 2) if snap.get("day_chg") is not None else None
        )
        rows.append({
            "code": code,
            "name": pos.get("name") or code,
            "price": round(price, 2),
            "cost": round(cost, 2),
            "volume": int(volume),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "day_chg": day_chg,
        })
    return rows


def build_user_content(
    rows: list[dict],
    asset: float,
    cash: float,
    agent: str,
    pool: dict,
    *,
    news: list[dict] | None = None,
    notes: list[str] | None = None,
    ask_decisions: bool = False,
) -> str:
    """提示词正文（逐字沿用旧栈版式：账户 → 持仓表 → 候选池表 → 新闻 → 要求）。"""
    lines = [
        f"现在是北京时间 {now_cn():%F %T}（港股{session_label(now_cn())}）。"
        f"你是 {agent}。你们共用一个富途港股模拟账户：总资产 HK${asset:,.0f}、"
        f"可用现金 HK${cash:,.0f}。"
    ]
    for note in notes or []:
        lines.append(f"（数据提示：{note}）")

    if rows:
        lines += [
            "当前持仓：",
            "",
            "| 股票 | 代码 | 现价 | 成本 | 数量 | 市值 | 盈亏 | 盈亏% | 今日涨跌% |",
            "|------|------|------|------|------|------|------|-------|-----------|",
        ]
        for r in rows:
            day = f"{r['day_chg']:+.2f}" if r["day_chg"] is not None else "—"
            lines.append(
                f"| {r['name']} | {r['code']} | {r['price']} | {r['cost']} | {r['volume']} "
                f"| HK${r['price'] * r['volume']:,.0f} | HK${r['pnl']:+,.0f} "
                f"| {r['pnl_pct']:+.2f}% | {day} |"
            )
    else:
        lines.append("当前无持仓。")

    if pool:
        lines += [
            "",
            f"候选池（{pool.get('date')} 动量评分 top {len(pool.get('picks') or [])}，"
            f"大盘方向 {pool.get('market_direction')}）：",
            "",
            "| 股票 | 代码 | 现价 | 20日动量% | 60日动量% | 评分 |",
            "|------|------|------|-----------|-----------|------|",
        ]
        for p in (pool.get("picks") or [])[:15]:
            lines.append(
                f"| {p.get('name') or '—'} | {p.get('code')} | {p.get('last_close')} "
                f"| {float(p.get('mom20') or 0):+.1f} | {float(p.get('mom60') or 0):+.1f} "
                f"| {p.get('score')} |"
            )

    if news:
        tag = {"bullish": "利好", "bearish": "利空", "neutral": "中性"}
        lines += ["", f"盘中新闻（近 {NEWS_HOURS} 小时，情感标注）：", ""]
        for n in news:
            lines.append(f"- [{tag[n['sentiment']]}] {n['title']}（{n['source']} {n['time']}）")

    if rows:
        lines += [
            "",
            "请逐只给出：①一句话简评（行情/基本面/消息面角度）②操作建议（持有/加仓/减仓/止损）③理由。",
        ]
    else:
        lines += [
            "",
            "请从候选池挑 3 只最值得关注的：①为什么关注 ②建议的观察/介入价位 ③主要风险。",
        ]
    lines.append("结合候选池与新闻，可顺带评估是否值得用现金换仓。输出简洁 markdown，不用复述表格。")

    if ask_decisions:
        from backend.shared.decision.contract import INTRADAY_SCHEMA_JSON

        lines += [
            "",
            "【可执行决策】在上面分析之后，**另起一行**输出一个 JSON 对象（不要放进代码块），"
            "逐只列出现有持仓与候选池中你确定要动手的标的：",
            INTRADAY_SCHEMA_JSON,
            "只列你**现在就要执行**的动作：hold 可省略，watch 表示挂守护意图。"
            "pct 是比例（买入=用剩余现金的比例，卖出=持仓比例）；没想好比例就不要给该行。",
        ]
    return "\n".join(lines)


def system_prompt_for(model: str, mode: dict) -> str:
    """系统提示词：港股人设 + 市场规则（analysis_modes 唯一出处）+ 本次配置要求。"""
    from backend.services.agent_arena.analysis_modes import market_rules

    base = (
        f"你是 {model} 模型驱动的港股交易助手盘中持仓分析师（富途模拟账户）。"
        "分析冷静客观，给可执行的操作建议。输出中文 markdown。"
        f"\n交易规则：{market_rules('hk')}"
    )
    return base + f"\n\n【本次分析配置：{mode['name']}】\n{mode['prompt']}"


# ---------------------------------------------------------------- LLM / 落盘


class TruncatedOutputError(RuntimeError):
    """输出被 max_tokens 截断（与 A 股 ``live_model_analysis`` / ``news_brief`` 同语义）：
    半截分析不许冒充完整分析——抛错走调用侧的降级链（fallback_summary 数据摘要）。"""

    def __init__(self, content: str, usage: dict | None):
        super().__init__("输出被 max_tokens 截断，整轮作废")
        self.content = content
        self.usage = usage


def call_model(
    env: dict[str, str], sig: str, model: str, user_content: str, system: str
) -> tuple[str, dict | None]:
    """OpenAI 兼容调用（各模型走各自供应商；失败重试 1 次后 raise）。"""
    prefix, _ = ROSTER[sig]
    base = _env_get(env, f"{prefix}_API_BASE").rstrip("/")
    key = _env_get(env, f"{prefix}_API_KEY")
    if prefix == "OPENAI" and not base:
        # 本仓 .env 的 OpenAI 兼容键有两种写法，兜底 DEEPSEEK_*
        base = _env_get(env, "DEEPSEEK_BASE_URL").rstrip("/")
        key = key or _env_get(env, "DEEPSEEK_API_KEY")
    if not base or not key:
        raise RuntimeError(f"{prefix}_API_BASE/KEY 缺失")

    import requests

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
        "max_tokens": MAX_TOKENS,
    }
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            resp = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message", {}) or {}
            content = (
                str(msg.get("content") or "").strip()
                or str(msg.get("reasoning_content") or "").strip()
            )
            finish = str(choice.get("finish_reason") or "")
            usage = data.get("usage") or None
            if usage:
                usage = {
                    k: int(usage.get(k) or 0)
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                }
            # max_tokens 截断：半截分析（含 reasoning 兜底捞出的思考草稿）整轮作废
            if finish == "length":
                raise TruncatedOutputError(content, usage)
            if not content:
                raise RuntimeError("空回复")
            return content, usage
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == 0:
                print(f"  ⚠️ {sig} 调用失败，重试 1 次: {exc}")
    raise last_exc  # type: ignore[misc]


def append_log(user: str, content: str, sig: str, usage: dict | None = None) -> Path:
    """落盘模型对话日志（结构与 A 股/旧栈同构 → 前端「模型对话」直接可读）。"""
    now = now_cn()
    log_dir = DATA_DIR / sig / "log" / now.strftime("%Y-%m-%d")
    log_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": now.isoformat(),
        "signature": sig,
        "new_messages": [
            {"role": "user", "content": user},
            {"role": "assistant", "content": content},
        ],
    }
    if usage:
        entry["usage"] = usage
    path = log_dir / "log.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


def fallback_summary(rows: list[dict], pool: dict, mode_name: str) -> str:
    """LLM 不可用时的数据摘要（对话不断流；未调 API 不计 token）。"""
    out = [f"（{mode_name} LLM 分析暂不可用，附实时数据）"]
    for r in rows:
        out.append(
            f"- {r['name']} {r['code']}: 现价 HK${r['price']} / 成本 HK${r['cost']}"
            f" / {r['volume']}股 / 盈亏 HK${r['pnl']:+,.0f}({r['pnl_pct']:+.2f}%)"
        )
    if pool:
        top3 = (pool.get("picks") or [])[:3]
        out.append(
            "候选池 top3: " + "、".join(f"{p.get('name') or p.get('code')}({p.get('code')})" for p in top3)
        )
    return "\n".join(out)


# ---------------------------------------------------------------- 决策 → 计划（纯函数）


def plan_hk_orders(
    decisions: list,
    *,
    holdings: dict[str, int],
    prices: dict[str, float],
    cash: float,
    lot: int = LOT_SIZE,
    cap_pct: float = BUY_CAP_PCT,
) -> tuple[list[dict], list[dict]]:
    """决策 → ``(orders, blocks)``（纯函数，不下单）。

    HK v1 口径（与旧栈一致，收敛在提示词已写明的最小改动面）：
      - 只做**已有持仓**的加仓/减仓——不建新仓（避免在共享模拟盘上凭空开仓）；
      - 决策里的代码先归一到 ``00700.HK``（模型可能写 HK.700 / 700），与持仓表对齐；
      - 整手 100 股；卖出 ``pct`` 三态照 ``Decision.sell_intent``：
        缺比例=清仓、脏值=**停手留痕**（blocks 里带 ``dirty`` 标记）；
      - 买入 ``pct`` 缺/脏都是**不动作**（``buy_intent`` 语义），单笔 ≤ 现金 20%；
      - 预算按**限价**口径算（``live_fills.buy_limit_and_cost`` 是三市场唯一出处），
        同一轮多笔买单按序扣减可用现金，不许超买；
      - **取不到价一律不动作**（买卖同侧 fail-closed）——限价单缺价只能是 0，
        报出去必被后端 400 拒，不如在这里留痕跳过。
    """
    from backend.shared.decision.contract import FRAC_DIRTY, FRAC_OK, HOLD, WATCH
    from backend.services.trade.services.futu_subprocess import _norm_hk_code
    from live_fills import buy_limit_and_cost, sell_limit

    orders: list[dict] = []
    blocks: list[dict] = []
    avail = float(cash)

    for d in decisions or []:
        action = str(getattr(d, "action", "") or "")
        code = _norm_hk_code(getattr(d, "code", "") or "")
        reason = str(getattr(d, "reason", "") or "")
        if not code or action in (HOLD, WATCH) or action not in ("buy", "sell"):
            continue
        price = float(prices.get(code) or 0)

        if action == "sell":
            pct, frac = d.sell_intent()
            if frac == FRAC_DIRTY:
                blocks.append({"code": code, "side": "sell", "rule": "pct_dirty",
                               "reason": "卖出比例读不出（原样保留证据）——停手留痕"})
                continue
            held = int(holdings.get(code) or 0)
            if held <= 0:
                blocks.append({"code": code, "side": "sell", "rule": "sell_not_held",
                               "reason": "无持仓，跳过"})
                continue
            volume = int(held * pct / lot) * lot
            if volume <= 0:
                blocks.append({"code": code, "side": "sell", "rule": "below_min_lot",
                               "reason": f"比例 {pct:.0%} 不足 1 手（持仓 {held} 股）"})
                continue
            if price <= 0:
                blocks.append({"code": code, "side": "sell", "rule": "no_quote",
                               "reason": "取不到价，跳过（限价单缺价必被拒）"})
                continue
            orders.append({"code": code, "side": "sell", "volume": volume,
                           "price": sell_limit(price, "hk"), "reason": reason})
            continue

        # buy
        if code not in holdings:
            blocks.append({"code": code, "side": "buy", "rule": "not_held_v1",
                           "reason": "HK v1 只支持已有持仓加仓，跳过建仓"})
            continue
        pct, frac = d.buy_intent()
        if frac == FRAC_DIRTY:
            blocks.append({"code": code, "side": "buy", "rule": "pct_dirty",
                           "reason": "买入比例读不出——停手留痕"})
            continue
        if frac != FRAC_OK:
            blocks.append({"code": code, "side": "buy", "rule": "pct_missing",
                           "reason": "未给出买入比例（买入必须自己给幅度），跳过"})
            continue
        if price <= 0:
            blocks.append({"code": code, "side": "buy", "rule": "no_quote",
                           "reason": "取价失败，跳过"})
            continue
        budget = min(avail, float(cash)) * min(pct, cap_pct)
        # 手数按**限价成本**折算（不是现价）：买单在限价内任意价位都能成交，
        # 按现价折手数会把单笔预算与现金闸双双顶穿 buffer（港股 0.5%）。
        unit_cost = buy_limit_and_cost(price, lot, "hk")[1]
        volume = int(budget / unit_cost) * lot if unit_cost > 0 else 0
        while volume > 0 and buy_limit_and_cost(price, volume, "hk")[1] > avail:
            volume -= lot
        if volume <= 0:
            blocks.append({"code": code, "side": "buy", "rule": "unaffordable",
                           "reason": f"预算 HK${budget:,.0f} 不足 1 手（可用现金 HK${avail:,.0f}）"})
            continue
        limit, cost = buy_limit_and_cost(price, volume, "hk")
        avail -= cost  # 同轮后续买单看到的现金已扣减
        orders.append({"code": code, "side": "buy", "volume": volume,
                       "price": limit, "reason": reason})
    return orders, blocks


def parse_agent_decisions(content: str):
    """LLM 输出 → DecisionBatch（schema=整点轮，含 watch 守护意图）。"""
    from backend.shared.decision.contract import SCHEMA_INTRADAY, parse_decisions

    return parse_decisions(content, schema=SCHEMA_INTRADAY)


def _hk_trade_log(rec: dict) -> None:
    """港股交易日志：``logs/live_trade_hk_YYYYMMDD.jsonl``，``mode`` 恒为 ``execute_hk``。

    文件名与 mode 双保险——A 股 feed 的读者按 ``live_trade_*`` 通配时，市场可辨。
    """
    TRADE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {**rec, "mode": "execute_hk"}
    path = TRADE_LOG_DIR / f"live_trade_hk_{datetime.now():%Y%m%d}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def execute_plan(agent: str, orders: list[dict], *, dry_run: bool) -> tuple[list[dict], list[str]]:
    """计划单 → ``/futu/place``（env=SIMULATE）。返回 ``(已执行, 错误文案)``。

    成交/委托回执由后端 ``qq_notify`` 推港股通道（本脚本不重复外发）；
    这里只写港股交易日志，并在失败时把原因回传给告警面。
    """
    done: list[dict] = []
    errors: list[str] = []
    for o in orders:
        body = {
            "env": "SIMULATE",
            "market": "HK",
            "order": {
                "code": o["code"],
                "price": float(o["price"]),
                "quantity": int(o["volume"]),
                "order_type": "NORMAL",
                "trd_side": "BUY" if o["side"] == "buy" else "SELL",
            },
        }
        rec = {"ts": now_cn().isoformat(), "agent": agent, "code": o["code"],
               "side": o["side"], "volume": o["volume"], "price": o["price"],
               "reason": o.get("reason", ""), "dry_run": dry_run}
        if dry_run:
            _hk_trade_log({**rec, "result": "dry-run"})
            done.append({**o, "dry_run": True})
            continue
        try:
            out = _api_post(f"{ARENA}/futu/place", body, timeout=45)
            data = out.get("data") or {}
            rec["result"] = data
            _hk_trade_log(rec)
            if out.get("success"):
                done.append({**o, "order_id": data.get("order_id", "")})
            else:
                errors.append(f"{o['side']} {o['code']} 被拒: {data.get('message') or '未知'}")
        except Exception as exc:  # noqa: BLE001 单笔失败不中断其余
            _hk_trade_log({**rec, "error": str(exc)})
            errors.append(f"{o['side']} {o['code']} 下单失败: {type(exc).__name__}: {exc}")
    return done, errors


def exec_enabled() -> bool:
    """``configs/hk_exec.json`` 的 ``enabled``（文件不存在 = 恒 False，fail-closed）。"""
    try:
        return bool(json.loads(EXEC_CONFIG.read_text(encoding="utf-8")).get("enabled"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


# ---------------------------------------------------------------- 通知（分市场通道）


def notify_hk(title: str, content: str) -> None:
    """推港股通道（未配 HK 机器人时 push_notify 自动回退默认通道 + [港股] 前缀）。

    通知永远不该打断分析：任何失败只打印一行。
    """
    try:
        import push_notify

        push_notify.notify(title, content, channel="hk")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ QQ 通知失败（不影响本轮）: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 主流程


def run_analysis(
    *,
    dry_run: bool,
    agents: list[str],
    ask_decisions: bool,
    notify: bool = True,
) -> int:
    now = now_cn()
    health: list[str] = []

    sim, err = fetch_account_both()
    if sim is None:
        print(f"[{now:%F %T}] 富途账户不可达: {err}")
        if notify:
            notify_hk(
                "港股分析中断 ⚠️",
                f"{now:%F %T} 富途模拟账户不可达，本轮跳过。\n原因：{err[:300]}\n"
                "排查：docker ps | grep futu-opend → docker logs futu-opend（未登录会停在「请输入账号」）。",
            )
        return 1
    asset = float(sim.get("total_asset") or 0)
    cash = float(sim.get("cash") or 0)
    codes = [c for c in (sim.get("positions") or {}) if c]
    snaps = fetch_snapshot(codes)
    if codes and not snaps:
        health.append("行情快照为空（OpenD 未登录 / 行情未连）")
    rows = build_rows(sim, snaps)
    pool, pool_notes = load_pool()
    health += [n for n in pool_notes if "仅持仓复盘" in n]
    news = load_news([r["name"] for r in rows] + [p.get("name") or "" for p in (pool.get("picks") or [])])
    print(
        f"[{now:%F %T}] 富途SIM 资产 HK${asset:,.0f} 持仓 {len(rows)} 只；"
        f"候选池 {'top ' + str(len(pool.get('picks') or [])) if pool else '无（降级为仅持仓复盘）'}；"
        f"新闻 {len(news)} 条"
    )

    holdings = {r["code"]: r["volume"] for r in rows}
    # 只认快照价：候选池的 last_close 是「数据日」收盘，拿它给实盘单定价会引入
    # 最多 stale_days 个交易日的旧价（取不到价的单子在 planner 里 fail-closed 跳过）。
    prices = {r["code"]: r["price"] for r in rows}

    ok: list[str] = []
    failed: list[str] = []
    summary_lines: list[str] = []
    all_blocks: list[dict] = []

    for sig in agents:
        _, model = ROSTER[sig]
        content_for_decisions = ""
        for mode in _modes_for(sig):
            user_content = build_user_content(
                rows, asset, cash, sig, pool,
                news=news, notes=pool_notes, ask_decisions=ask_decisions,
            )
            labeled = f"【分析配置：{mode['name']}】\n\n" + user_content
            usage = None
            try:
                content, usage = call_model(
                    _load_env(), sig, model, labeled, system_prompt_for(model, mode)
                )
            except Exception as exc:  # noqa: BLE001 单模型失败不挡其余
                print(f"[{now:%F %T}] {sig}·{mode['name']} LLM 调用失败: {exc}")
                content = ""
            if not content:
                usage = None
                content = fallback_summary(rows, pool, mode["name"])
                failed.append(f"{sig}·{mode['name']}")
            else:
                ok.append(f"{sig}·{mode['name']}")
            path = append_log(labeled, content, sig, usage)
            tok = (
                f"，token {usage.get('total_tokens')}"
                f"（入{usage.get('prompt_tokens')}/出{usage.get('completion_tokens')}）"
                if usage else ""
            )
            # 路径只作日志展示：DATA_DIR 被挪出 ROOT（数据盘迁移/测试）时退化为绝对路径，
            # 不能让 relative_to 的 ValueError 在一轮 LLM 调用**之后**炸掉整轮。
            shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            print(
                f"[{now:%F %T}] {sig}·{mode['name']} 完成（持仓 {len(rows)} 只）{tok} → "
                f"{shown}"
            )
            summary_lines.append(f"{sig}·{mode['name']} {len(content)} 字{tok}")
            content_for_decisions = content

        if not ask_decisions or not content_for_decisions:
            continue
        batch = parse_agent_decisions(content_for_decisions)
        if batch.failed:
            print(f"[{now:%F %T}] {sig} 决策解析 {batch.status}（无 json / 非法行）")
            all_blocks.append({"agent": sig, "rule": batch.status,
                               "reason": "决策解析失败——本轮该 agent 不动作"})
            continue
        orders, blocks = plan_hk_orders(
            list(batch.decisions), holdings=holdings, prices=prices, cash=cash
        )
        all_blocks += [{**b, "agent": sig} for b in blocks]
        if not orders:
            continue
        done, errors = execute_plan(sig, orders, dry_run=dry_run)
        tag = "🟡 DRY-RUN" if dry_run else "✅ 已提交"
        acts = "/".join(f"{o['side']} {o['code']} {o['volume']}股" for o in done)
        if done:
            print(f"[{now:%F %T}] {tag} [{sig}] 港股 {len(done)} 笔（{acts}）")
        if errors:
            notify_hk(f"港股下单异常 ⚠️（{sig}）", "\n".join(errors[:5]))

    # —— 汇总外发：每轮分析摘要（用户裁决的四类事件之一）——
    pool_desc = (
        f"top {len(pool.get('picks') or [])} · {pool.get('market_direction') or '—'}"
        if pool
        else "未注入（仅持仓复盘）"
    )
    head = (
        f"{now:%F %T} 资产 HK${asset:,.0f}（现金 HK${cash:,.0f}）持仓 {len(rows)} 只\n"
        f"候选池：{pool_desc}\n"
        f"新闻 {len(news)} 条 · 模型 {len(ok)}/{len(ok) + len(failed)} 完成"
    )
    body = [head, *summary_lines[:6]]
    if all_blocks:
        body.append("风控/解析留痕：")
        body += [
            f"- [{b.get('agent', '')}] {b.get('rule')}: {b.get('reason')}" for b in all_blocks[:6]
        ]
    if not dry_run and ask_decisions:
        body.append("（执行已开启：仅 HK v1 已有持仓加/减仓，整手 100 股，单笔 ≤ 现金 20%）")
    if notify:
        notify_hk("港股盘中分析", "\n".join(body))

    # —— 系统与行情健康（四类事件之一；有问题才发，避免每轮刷屏）——
    if failed:
        health.append(f"模型失败 {len(failed)} 个：{'、'.join(failed[:3])}")
    if health and notify:
        notify_hk("港股循环健康 ⚠️", f"{now:%F %T}\n" + "\n".join(f"- {h}" for h in health[:6]))

    if not ok and failed:
        print(f"[{now:%F %T}] [SUMMARY] 全部模型失败：" + " · ".join(failed)[:180])
        return 2
    print(f"[{now:%F %T}] [SUMMARY] {len(ok)} 个分析完成" + (f"，{len(failed)} 失败" if failed else ""))
    return 0


def _modes_for(agent: str) -> list[dict]:
    """持仓分析用的配置轮（comp-config 无 hk 段 → 基线模式）。"""
    from backend.services.agent_arena.analysis_modes import selected_modes

    return selected_modes(agent, market="hk")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="港股盘中分析循环（只出观点；执行默认关）")
    ap.add_argument("--force", action="store_true", help="忽略交易时段检查（补跑/调试，且不触发配置自动执行）")
    ap.add_argument("--execute", action="store_true", help="本轮真下单（SIMULATE；默认只打印）")
    ap.add_argument("--dry-run", action="store_true", help="强制不下单（覆盖配置开关）")
    ap.add_argument("--decisions", action="store_true", help="提示词追加 decisions JSON 契约并解析（含 --dry-run 时）")
    ap.add_argument("--agents", default="", help="逗号分隔模型签名；缺省=名册全部")
    ap.add_argument("--no-notify", action="store_true", help="不外发 QQ（手动调试用）")
    args = ap.parse_args(argv)

    now = now_cn()
    if not args.force and not in_trading_window(now):
        print(f"[{now:%F %T}] 非交易时段（北京 09:30–12:00/13:00–16:00 工作日），跳过")
        return 0

    # 双层把关：配置开关（默认关）+ 显式 --execute；--force（人工补跑）不触发配置自动执行
    do_execute = args.execute or (exec_enabled() and not args.force and not args.dry_run)
    if args.dry_run:
        do_execute = False

    targets = [a.strip() for a in args.agents.split(",") if a.strip()] or list(ROSTER)
    unknown = [t for t in targets if t not in ROSTER]
    for u in unknown:
        print(f"⚠️ 跳过 {u}：不在模型名册（{', '.join(ROSTER)}）")
    runnable = [t for t in targets if t in ROSTER]
    if not runnable:
        print("没有可运行的模型")
        return 2

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock = LOCK.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("已有港股分析实例在跑，本轮跳过")
        return 1
    try:
        return run_analysis(
            dry_run=not do_execute,
            agents=runnable,
            ask_decisions=bool(args.decisions or do_execute),
            notify=not args.no_notify,
        )
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
