#!/usr/bin/env python3
"""实盘模型对话轮（「模型对话」tab「立即分析」的宿主执行端）。

上游 ``live_hourly_analysis.py``（quant-Trader）随平台退役后，「立即分析」按钮
一直置灰（A6）。本脚本是其**对话语义的最小复活**：读 **每个 agent 自己的分账
账本**（``qm_agent_ledger_*``：授予额度 + 该 agent 名下持仓，只读），现价/可卖量
按代码从 QM 桥取，连同当日的新闻分子摘要，喂给各模型各出一轮分析，按 arena
日志格式落盘 ``data/agent_data_astock/{sig}/log/{date}/log.jsonl`` → 前端
「模型对话」直接可读（复用 news_brief.append_log 的同一结构）。

**隐私边界（2026-10-19 用户拍板）**：agent 只能动用用户授予的额度，**不得看见
真实账户的总资产/可用资金/持仓市值**；可见面只有「授予额度 / 已用 / 剩余」与
它自己名下的持仓。部分模式允许看对手的买卖与盈亏，但同样不含账户总金额。

安全边界：**只读查询 + LLM 调用，绝不下单**——本脚本不 import 任何交易执行
代码。真在拍板下单的是**决策轮**（容器内 trade 服务的 P2.8 槽位回路，开关
``QM_DECISION_ROUND_ENABLED`` 在 ``config/runtime.env`` 里）——上游「一个回路
同时产分析与决策」的形状，QM 侧已由它补回（2026-10-14 起它也写 ``log.jsonl``）。

与决策轮的关系（2026-10-14，甲5）：**盘中让位**
------------------------------------------------
两边槽位几乎逐一对上（本脚本 08:30 + 整点 + 14:45；决策轮 11 槽含同样这些点），
且写的是同一份 ``log.jsonl``：都跑 = 同一个小时往同一段追加两张卡片（一张拍板
落单、一张明令不下单），token 用量还各记一份。故：

* 决策轮**在岗到点**时本脚本**让位**——判据是标记文件
  ``config/decision_round.onduty.json``（决策轮 worker 心跳式重写，
  ``backend/shared/decision_round_onduty.py`` 是唯一读法）；
* 决策轮**没开 / 停了 / 标记过期**（>10 分钟）→ 本脚本**照跑**：OSS 默认部署
  与老行为完全一致，**展示面不会静默变空**（这是判据故意选的方向）；
* ``--mode review``（15:30 复盘轮）与 ``--force``（「立即分析」按钮走这条路）
  **永不让位**。

真话纪律（T3-2，审计 C5/H13）：
* **账本挂 = 无账户模板**：分账账本读不到时提示词**禁止要求点评持仓**（也不许
  模型编造持仓/账户数字），并给日志落 ``data_gaps=['account_unreachable']``
  机器可读标记 → 前端卡片挂「无实盘账户数据」横幅；
* **截断不落盘**：``finish_reason=='length'`` 的回复整轮作废（重试 1 次**并追加
  「精简输出」提示**——2026-10-12 实测同一 payload 原样重试再撞上限；仍截断
  即放弃并如实报错）——半截分析混进「模型对话」与完整分析无法区分。

提醒（2026-10-12 补：当日桥失联 5 轮、截断 3 轮全程无任何告警）：
* 分账账本失联/恢复：状态翻转沿各推一条 QQ（状态持久化在
  ``data/logs/.live_model_analysis_alert_state.json``，不重复刷）；
* 轮次不满员（或全败）：推一条 QQ，1 小时限流（每小时一轮 = 每轮最多一条）；
* 复盘轮卡片：``--mode review`` 完成后推「📋 收盘复盘」合集（每模型一段摘要）。

用法：
    python3 scripts/live_model_analysis.py                 # 盘中轮：全部模型（名册）
    python3 scripts/live_model_analysis.py --agents deepseek-v4-flash
    python3 scripts/live_model_analysis.py --mode review   # 盘后复盘轮（北京 15:30 档）
    python3 scripts/live_model_analysis.py --force         # 无视「让位决策轮」强跑一轮

开关（``.env``，缺省全开；写 ``0/false/no/off`` 关，词表同 switch_enabled）：
    QM_DIALOGUE_ALERTS=off        # 账本失联/恢复 + 轮次不满员 两类提醒
    QM_DIALOGUE_REVIEW_PUSH=off   # 收盘复盘 QQ 卡片
    QM_DIALOGUE_MAX_TOKENS=8000   # 输出上限覆盖（默认 8000）
    QM_DIALOGUE_YIELD_TO_DECISION_ROUND=off  # 关掉「决策轮在岗就让位」（回到双写）
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# 本脚本目录（``import push_notify`` 同目录解析；keeper 系列脚本同口径）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock_industry import load_industry_map  # noqa: E402

CN_TZ = timezone(timedelta(hours=8))
LOCK = ROOT / "data" / "logs" / ".live_model_analysis.lock"
LOG_DIR = ROOT / "data" / "agent_data_astock"
BRIEF_FILE = ROOT / "data" / "news_brief" / "latest.json"
ALERT_STATE_FILE = ROOT / "data" / "logs" / ".live_model_analysis_alert_state.json"
TIMEOUT_S = 180
#: 输出上限：2026-10-12 实测 4000 会整轮截断（推理型模型的 reasoning 也吃预算），
#: 三家供应商实测均接受 8000（finish=stop）。env ``QM_DIALOGUE_MAX_TOKENS`` 可覆盖。
MAX_TOKENS = 8000
#: 截断重试时的精简提示（只追加在第 2 次尝试；抄一份 payload 原样重试大概率再撞）。
RETRY_BREVITY_NOTE = (
    "\n\n【系统提示】你上一次的回复超过输出长度上限被截断（该轮已作废）。"
    "本轮请**大幅精简**：只给结论与关键理由，省去铺陈与重复，全文控制在 800 字以内。"
)
#: 轮次不满员提醒的限流间隔（秒）：每小时一轮，1h 即每轮最多一条。
ROUND_ALERT_MIN_INTERVAL_S = 3600
#: 复盘轮的指数（suffix 式直查桥快照；拿不到就当该行缺失，绝不编数）。
REVIEW_INDEXES: tuple[tuple[str, str], ...] = (
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
)

#: 名册：签名 →（凭据 env 前缀, 实际调用的模型名）。签名即落盘目录名，与前端
#: 模型列表（扫描 agent_data_astock/ 目录）天然一致。
ROSTER: dict[str, tuple[str, str]] = {
    "deepseek-v4-flash": ("OPENAI", "deepseek-v4-flash"),
    "deepseek-v4-pro": ("OPENAI", "deepseek-v4-pro"),
    "glm-5.3-flash": ("GLM", "glm-5.3-flash"),
}


def _load_env() -> dict[str, str]:
    """读仓库根 .env（与 news_brief.call_llm 同口径：不覆盖已有键）。"""
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


def _session_label(now: datetime) -> str:
    """北京时刻 → 盘面会话标签（只影响提示词文案）。"""
    if now.weekday() >= 5:
        return "周末休市"
    hm = now.hour * 60 + now.minute
    if 555 <= hm <= 695:  # 09:15–11:35
        return "早盘交易时段"
    if 775 <= hm <= 905:  # 12:55–15:05
        return "午盘交易时段"
    if hm < 555:
        return "盘前"
    if 695 < hm < 775:
        return "午间休市"
    return "盘后"


def _bridge_account(env: dict[str, str]) -> dict | None:
    """只读拉账户 + 持仓；桥不可达返回 None（现价/可卖量降级为 —）。

    **本函数只用于可卖量与现价，调用方不得把 asset 三数写进提示词**：
    账户总额是账户主人的隐私，agent 可见面只认分账账本（见 ``read_agent_ledgers``）。
    """
    import requests

    url = (env.get("TDX_BRIDGE_URL") or "").rstrip("/")
    token = env.get("TDX_BRIDGE_TOKEN") or ""
    if not url:
        return None
    try:
        resp = requests.post(
            f"{url}/api/v1/account/query",
            json={"account_type": "stock"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as e:  # noqa: BLE001 桥不可达 → 降级提示词，不中断本轮
        print(f"⚠️ 桥账户查询失败（降级为无持仓上下文）: {e}")
        return None


# ── 分账账本（agent 可见面的唯一账户口径）──────────────────────────────
# 2026-10-19 用户拍板：agent 只能动用授予额度，**不得看见账户总金额**。
# 故本脚本不再把 ``/api/v1/account/query`` 的 asset 三数写进提示词——
# 持仓/额度一律读 ``qm_agent_ledger_*``（每个 agent 自己的账），与决策轮同源。
# 桥只用于两件事：每只票的实时价、可卖量（T+1 口径），且都按代码取，不取总额。
LEDGER_TENANT = "default"
DEFAULT_LEDGER_USER = "10000001"
LEDGER_CONNECT_TIMEOUT_S = 5
#: 真实持仓快照的最长可采信时长（秒）。tdx/qmt 两条同步链 30s 一落，900s 是三倍
#: 冗余；超过即视为不可用于对账——陈旧快照两头骗人：已卖的不消失、今日新买的
#: 看不见（唯一事实源见 ``backend/shared/real_positions.py``）。
REAL_POSITION_MAX_AGE_S = 900


def _ledger_identity(env: dict[str, str]) -> tuple[str, str]:
    """(tenant, user)：与决策轮 ``resolve_db_account_user`` 同口径。"""
    from backend.shared.simulation_account_keys import normalize_runtime_user

    raw = str(env.get("QM_DECISION_ACCOUNT_USER_ID") or "").strip()
    return LEDGER_TENANT, normalize_runtime_user(raw or DEFAULT_LEDGER_USER)


def _pg_connect(env: dict[str, str]):
    """宿主/容器都能连的 PG 连接；凭据只在内存里过一遍，不落盘。"""
    import psycopg2

    import host_env

    host = (
        os.environ.get("POSTGRES_HOST", "").strip()
        or host_env.resolve_postgres_host()
        or env.get("DB_HOST")
        or "127.0.0.1"
    )
    return psycopg2.connect(
        host=host,
        port=int(env.get("DB_PORT") or 5432),
        dbname=env.get("DB_NAME") or "quantmind",
        user=env.get("DB_USER") or "quantmind",
        password=env.get("DB_PASSWORD") or "",
        connect_timeout=LEDGER_CONNECT_TIMEOUT_S,
    )


def read_agent_ledgers(
    env: dict[str, str], agents: list[str] | tuple[str, ...]
) -> dict[str, dict | None]:
    """本轮各 agent 的分账账本段；读失败 → 该 agent 为 ``None``（降级模板）。

    返回 ``{agent: {"quota", "used", "remaining", "known", "positions": [...]}}``。
    **任何失败都不回退到桥账户**——回退就是把账户总额喂给模型的那条泄漏路径。

    额度按 agent 逐个取自 ``TradingPolicy``（阶段 1 单一出处）：本脚本曾是阶段 1
    消费方改造的漏网（2026-10-30 实测 15:30 复盘轮仍对模型说「用户授予 ¥100,000」，
    而决策轮同一时刻是 ¥200,000——模型拿着半额额度做次日计划）。策略文件坏 →
    整轮降级为无账户模板（**不**回退常量：错额度比没额度更坏）。
    """
    wanted = [str(a).strip() for a in agents if str(a).strip()]
    out: dict[str, dict | None] = dict.fromkeys(wanted)
    if not wanted:
        return out
    tenant, user = _ledger_identity(env)
    conn = None
    try:
        quotas = _policy_quotas(wanted)
    except Exception as exc:  # noqa: BLE001 策略不可用 → 整轮无账户上下文
        print(f"⚠️ 交易策略不可用（本轮降级为无持仓上下文）: {exc}")
        return out
    try:
        conn = _pg_connect(env)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.agent, p.code, p.volume, p.cost_price,
                       COALESCE(s.name, '') AS name
                  FROM qm_agent_ledger_position p
                  LEFT JOIN stocks s ON s.symbol = p.code
                 WHERE p.tenant_id = %s AND p.user_id = %s AND p.agent = ANY(%s)
                 ORDER BY p.agent, p.code
                """,
                (tenant, user, wanted),
            )
            rows = cur.fetchall()
            cur.execute(
                """
                SELECT agent FROM qm_agent_ledger_account
                 WHERE tenant_id = %s AND user_id = %s AND agent = ANY(%s)
                """,
                (tenant, user, wanted),
            )
            account_rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 账本不可达 → 本轮降级，不中断整轮
        print(f"⚠️ 分账账本查询失败（降级为无持仓上下文）: {exc}")
        return out
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 关闭失败不改变本轮结论
                pass

    return _ledger_view(wanted, rows, account_rows, quotas)


def _policy_quotas(agents: list[str]) -> dict[str, float]:
    """各 agent 的授予额度（元）——``TradingPolicy.for_agent`` 的逐家取值。

    与决策轮/净值采样器同一出处；策略文件坏时抛 ``TradingPolicyError``，由
    调用方降级（绝不在这里回退成某个常量）。
    """
    from backend.shared.trading_policy import load_trading_policy

    policy = load_trading_policy()
    return {agent: policy.for_agent(agent).quota for agent in agents}


def _ledger_view(
    agents: list[str],
    rows: list[tuple],
    account_rows: list[tuple],
    quota: float | Mapping[str, float],
) -> dict[str, dict]:
    """把账本查询行映射成提示词要的视图（纯函数，脏行逐条跳过）。

    ``rows`` = ``(agent, code, volume, cost_price, name)``；
    ``account_rows`` = ``(agent,)``（账本里有账户行、但名下无货的 agent）；
    ``quota`` = 单值（旧调用点/单测）或 ``{agent: 额度}``（生产路径，按 agent 取）。
    """
    known = {str(row[0]) for row in account_rows}
    grouped: dict[str, list[dict]] = {agent: [] for agent in agents}
    for agent, code, volume, cost, name in rows:
        agent_s = str(agent)
        try:
            volume_f = float(volume)
            cost_f = float(cost)
        except (TypeError, ValueError):
            continue
        if volume_f <= 0 or not math.isfinite(volume_f) or not math.isfinite(cost_f):
            continue
        grouped.setdefault(agent_s, []).append(
            {
                "code": str(code),
                "name": str(name or ""),
                "volume": volume_f,
                "cost_price": cost_f,
            }
        )

    out: dict[str, dict] = {}
    for agent in agents:
        positions = grouped.get(agent, [])
        used = round(sum(p["volume"] * p["cost_price"] for p in positions), 2)
        agent_quota = (
            float(quota[agent]) if isinstance(quota, Mapping) else float(quota)
        )
        out[agent] = {
            "quota": agent_quota,
            "used": used,
            "remaining": round(agent_quota - used, 2),
            "known": agent in known or bool(positions),
            "positions": positions,
        }
    return out


# ── 真实持仓对账（读侧幻影仓闸门）────────────────────────────────────────
# 2026-10-21 用户报障：昨天在通达信桥手工清仓 6 只，今天 15:30 复盘仍在逐只
# 点评它们。根因是「手工单 ``orders.agent=NULL`` → ``agent_ledger_fill`` 按设计
# 不入分账 → 账本保留幻影仓」，而本脚本只读账本。这里按「账本持仓 ∩ 真实账户
# 持仓」裁掉已不在账户里的票——**只裁可见性，不改账本**（源头清理是另一件事）。


def _snapshot_age_seconds(
    snapshot_at: object, *, now: datetime | None = None
) -> float | None:
    """``snapshot_at`` → 距现在秒数（解析不了返回 ``None``）。

    库里存 **naive UTC**（见 ``real_account_snapshots`` 的历史坑），这里按 UTC
    解释；带 tz 的按原样换算。真实持仓是 30s 级热数据，绝对新鲜度必须自己判
    ——``merge_real_sources`` 只能剔除「相对其他源停更」的源。
    """
    if snapshot_at is None:
        return None
    if isinstance(snapshot_at, str):
        text = snapshot_at.strip()
        if not text:
            return None
        try:
            snapshot_at = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(snapshot_at, datetime):
        return None
    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    return (ref - snapshot_at.astimezone(timezone.utc)).total_seconds()


def _real_position_volumes_from_snapshot(
    env: dict[str, str], *, now: datetime | None = None
) -> dict[str, float] | None:
    """PG 快照 → ``{suffix 码: 持股数}``；过期/空/PG 读败 → ``None``。

    与决策轮同源（``real_positions.load_real_positions_sync``：多源并集、相对
    停更剔除、活跃券商优先），只换同步驱动。symbol 归一为 suffix —— 账本持仓
    列就是这一口径。
    """
    from backend.shared.real_positions import load_real_positions_sync
    from backend.shared.stock_utils import StockCodeUtil

    tenant, user = _ledger_identity(env)
    positions, meta = load_real_positions_sync(
        tenant, user, connect=lambda: _pg_connect(env)
    )
    age_s = _snapshot_age_seconds(meta.get("snapshot_at"), now=now)
    if age_s is None:
        print("⚠️ 实盘持仓快照无时间戳，不可用于对账")
        return None
    if age_s > REAL_POSITION_MAX_AGE_S:
        print(f"⚠️ 实盘持仓快照不可用于对账（最新快照 {age_s:.0f}s 前）")
        return None
    out: dict[str, float] = {}
    for symbol, pos in positions.items():
        try:
            volume = float(pos.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        if volume <= 0:
            continue
        out[StockCodeUtil.to_suffix(str(symbol))] = volume
    return out


def _real_position_volumes_from_bridge(env: dict[str, str]) -> dict[str, float] | None:
    """桥实时账户 → ``{suffix 码: 持股数}``；桥不可达 → ``None``。

    **0 股模板行不是持仓**（桥清仓后仍会返回，2026-10-20 实测），按
    ``total_volume > 0`` 过滤；可卖量（T+1）不参与对账。
    """
    acct = _bridge_account(env)
    if acct is None:
        return None
    out: dict[str, float] = {}
    for p in acct.get("positions") or []:
        code = str(p.get("stock_code") or "").strip()
        try:
            volume = float(p.get("total_volume") or 0)
        except (TypeError, ValueError):
            continue
        if not code or volume <= 0:
            continue
        out[code] = volume
    return out


def read_real_position_volumes(
    env: dict[str, str], *, now: datetime | None = None
) -> tuple[dict[str, float] | None, str]:
    """真实账户持仓 ``{suffix 码: 持股数}`` + 来源（snapshot/bridge/unavailable）。

    **只用于对账**（把账本幻影仓挡在提示词外），不展示总额、不下单。快照优先
    （与决策轮同源）；快照过期/读败 → 桥实时账户兜底；两者都不可信 → ``None``
    ——调用方**不做剔除**（不知道就别动：宁可显示旧账，不可少显示真仓），只在
    ``data_gaps`` 留痕。
    """
    try:
        volumes = _real_position_volumes_from_snapshot(env, now=now)
        if volumes is not None:
            return volumes, "snapshot"
    except Exception as exc:  # noqa: BLE001 读败 → 桥兜底，不中断整轮
        print(f"⚠️ 实盘持仓快照读取失败（改走桥账户对账）: {exc}")
    volumes = _real_position_volumes_from_bridge(env)
    if volumes is not None:
        return volumes, "bridge"
    return None, "unavailable"


def reconcile_ledger_positions(
    ledger: dict, real_volumes: dict[str, float] | None
) -> tuple[dict, list[str]]:
    """账本持仓 ∩ 真实账户持仓 → ``(新账本视图, 被剔除的代码)``（纯函数）。

    ``real_volumes`` = ``{suffix 码: 真实持股数}``；``None`` = 真实账户不可信 →
    **原样返回**（不做剔除）。

    规则：
    * 真实账户里没有（或为 0 股）→ 整行剔除（幻影仓）；
    * 真实量 < 账本量 → 可见量夹到真实量（部分手工卖出，账本只记归属、账户才是
      事实）；
    * 真实量 ≥ 账本量 → 不动（同一只票可被多家模型同时持有，真实量是合计）。

    额度按**可见持仓**重算 ``used/remaining``：幻影仓的成本不是当下的敞口。
    """
    if real_volumes is None:
        return ledger, []
    kept: list[dict] = []
    dropped: list[str] = []
    for p in ledger.get("positions") or []:
        code = str(p.get("code") or "")
        real_volume = real_volumes.get(code)
        try:
            volume = float(p.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        if real_volume is None or real_volume <= 0 or volume <= 0:
            dropped.append(code)
            continue
        kept.append({**p, "volume": min(volume, float(real_volume))})
    quota = float(ledger.get("quota") or 0.0)
    used = 0.0
    for p in kept:
        try:
            used += float(p["volume"]) * float(p["cost_price"])
        except (KeyError, TypeError, ValueError):
            continue
    used = round(used, 2)
    return (
        {
            **ledger,
            "positions": kept,
            "used": used,
            "remaining": round(quota - used, 2),
        },
        dropped,
    )


def _bridge_availability(env: dict[str, str]) -> dict[str, int]:
    """每只票的桥实时可卖量（T+1）。**只取代码→可卖量，不读 asset 三数。**"""
    acct = _bridge_account(env)
    out: dict[str, int] = {}
    if not acct:
        return out
    for p in acct.get("positions") or []:
        code = str(p.get("stock_code") or "").strip()
        if not code:
            continue
        try:
            available = int(float(p.get("available_volume")))
        except (TypeError, ValueError):
            continue
        out[code] = max(0, available)
    return out


def _bridge_snapshot(env: dict[str, str], code: str) -> dict:
    """单只快照（桥 get_market_snapshot 直查）；失败返回 ``{}``（该行留空）。

    返回 ``{"now": float, "last_close": float}``——复盘轮要昨收算当日涨跌。
    """
    import requests

    from backend.shared.stock_utils import StockCodeUtil

    url = (env.get("TDX_BRIDGE_URL") or "").rstrip("/")
    token = env.get("TDX_BRIDGE_TOKEN") or ""
    if not url:
        return {}
    try:
        resp = requests.post(
            f"{url}/api/v1/tdx/call",
            json={
                "method": "get_market_snapshot",
                "params": {"stock_code": StockCodeUtil.to_suffix(code) or code},
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
        resp.raise_for_status()
        result = resp.json().get("result") or {}
        if not isinstance(result, dict):
            return {}
        now = float(result.get("Now") or result.get("now") or 0) or 0.0
        last_close = (
            float(
                result.get("LastClose")
                or result.get("last_close")
                or result.get("PreClose")
                or 0
            )
            or 0.0
        )
        return {"now": now, "last_close": last_close}
    except Exception:  # noqa: BLE001 单只失败静默（该行现价留空）
        return {}


def _bridge_price(env: dict[str, str], code: str) -> float:
    """单只现价（``_bridge_snapshot`` 的 now 取用）；失败返回 0（提示词留空）。"""
    return float(_bridge_snapshot(env, code).get("now") or 0.0)


def _news_block() -> str:
    """当日新闻分子摘要（容错：文件缺失/字段缺失都返回空串，不挡本轮）。"""
    try:
        brief = json.loads(BRIEF_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(brief, dict):
        return ""
    seg = brief.get("segments") or {}
    macro = seg.get("news-macro") or brief.get("macro") or {}
    micro = seg.get("news-micro") or brief.get("micro") or {}
    hold = seg.get("news-holdings") or brief.get("holdings") or {}
    lines: list[str] = [
        f"（窗口 {brief.get('window', {}).get('start', '?')} ~ "
        f"{brief.get('window', {}).get('end', '?')}）"
    ]
    if isinstance(macro, dict) and macro.get("view"):
        lines.append(f"- 大盘方向：{macro['view']}")
    if isinstance(macro, dict):
        for r in (macro.get("risks") or [])[:3]:
            lines.append(f"- 风险：{r}")
    if isinstance(micro, dict):
        themes = micro.get("themes") or []
        if themes:
            lines.append(f"- 热点主题：{'、'.join(str(t) for t in themes[:8])}")
    if isinstance(hold, dict):
        for h in (hold.get("action_hints") or [])[:5]:
            lines.append(f"- 持仓相关：{h}")
        for c in (hold.get("cross_risks") or [])[:3]:
            lines.append(f"- 交叉风险：{c}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _research_block() -> str:
    """研究总控 brief（前一日夜间综合 + 当日各档复盘）→ 全局背景。

    缺失（首次上线/当日未跑）返回空串：本轮照常按新闻与持仓分析，绝不臆造。
    """
    try:
        from backend.shared.research_control.dossier import brief_for_prompt

        return brief_for_prompt(datetime.now(CN_TZ).strftime("%Y%m%d"))
    except Exception:  # noqa: BLE001 研究面读失败不挡本轮
        return ""


_UNSET = object()


def build_user_content(
    env: dict[str, str],
    *,
    agent: str = "",
    ledger: dict | None | object = _UNSET,
    availability: dict[str, int] | None = None,
    price_reader=None,
    snapshot_reader=None,
    mode: str = "intraday",
    real_volumes: dict[str, float] | None | object = _UNSET,
) -> tuple[str, list[str]]:
    """拼**本 agent 的分账额度 + 持仓** + 新闻的提示词正文。

    账户口径（2026-10-19 用户拍板）：只认 ``qm_agent_ledger_*`` 里这家 agent
    自己的授予额度与持仓。提示词里**绝不出现真实账户的总资产/可用资金/持仓
    市值**——账户里有多少钱是账户主人的隐私，模型只需要知道「这条线还能动用
    多少」。可卖量（T+1）与现价按代码从桥取，同样不涉及总额。

    返回 ``(正文, data_gaps)``：``ledger is None``（账本不可达）时落
    ``data_gaps=['account_unreachable']`` 并降级为「无账户模板」（**绝不要求
    点评持仓**，C5：编出来的持仓点评比不分析更坏）。

    ``mode='review'``（盘后复盘轮）：附三大指数当日表现，持仓表加「今日」涨跌
    列，要求模板换成复盘三问（今日复盘 / 持仓回顾 / 明日计划 + 开头 ≤120 字摘要）。

    ``ledger`` 缺省才现读（单测/一次性调用）；生产轮次由 ``main`` 一次读好传入，
    避免逐模型重复连库。``price_reader``/``snapshot_reader`` 缺省走真实桥。

    ``real_volumes`` = 真实账户持仓 ``{suffix 码: 持股数}``（``None`` = 读不到）：
    账本持仓先与它取交集再拼提示词——手工清仓走 ``orders.agent=NULL``，按设计
    不进分账，账本会留下幻影仓（2026-10-21 用户报障）。缺省才现读。
    """
    if ledger is _UNSET:
        ledger = read_agent_ledgers(env, [agent]).get(agent) if agent else None
    if real_volumes is _UNSET:
        real_volumes, _real_source = read_real_position_volumes(env)
    read_price = price_reader or _bridge_price
    read_snapshot = snapshot_reader or _bridge_snapshot
    available_map = availability or {}
    is_review = mode == "review"
    now = datetime.now(CN_TZ)
    gaps: list[str] = []
    parts = [
        f"【时间】{now.strftime('%Y-%m-%d %H:%M')}（北京时间，{_session_label(now)}）"
    ]

    if is_review:
        idx_lines = ["【收盘行情】指数当日表现"]
        got_idx = False
        for idx_code, idx_name in REVIEW_INDEXES:
            snap = read_snapshot(env, idx_code) or {}
            idx_now = float(snap.get("now") or 0)
            idx_prev = float(snap.get("last_close") or 0)
            if idx_now > 0 and idx_prev > 0:
                idx_lines.append(
                    f"- {idx_name} {idx_now:.2f}（{(idx_now / idx_prev - 1) * 100:+.2f}%）"
                )
                got_idx = True
        parts.append(
            "\n".join(idx_lines)
            if got_idx
            else "【收盘行情】指数数据不可用（只按新闻面复盘）"
        )

    has_account = ledger is not None
    has_positions = False
    if has_account and isinstance(ledger, dict):
        positions = list(ledger.get("positions") or [])
        if positions:
            if real_volumes is None:
                # 真实账户不可信：**不做剔除**（宁可显示旧账，不可少显示真仓），
                # 但要留痕——下一轮仍可能带着幻影仓复盘。
                gaps.append("real_positions_unavailable")
            else:
                ledger, dropped = reconcile_ledger_positions(ledger, real_volumes)
                positions = list(ledger.get("positions") or [])
                if dropped:
                    gaps.append("ledger_phantom_positions")
                    print(
                        f"⚠️ 账本里 {len(dropped)} 只持仓已不在真实账户，"
                        f"本轮不展示：{', '.join(dropped)}"
                    )
        quota = float(ledger.get("quota") or 0.0)
        used = float(ledger.get("used") or 0.0)
        remaining = float(ledger.get("remaining") or 0.0)
        parts.append(
            "【你的分账额度】\n"
            f"- 用户授予 ¥{quota:,.0f} ｜ 已用（持仓成本） ¥{used:,.0f} "
            f"｜ 剩余 ¥{remaining:,.0f}\n"
            "- 你只能在这个额度内决策；账户总资产、可用资金与其他 agent 的额度"
            "不是你的可见范围。"
        )
        if positions:
            has_positions = True
            if is_review:
                rows = [
                    "【你名下的持仓】",
                    "| 代码 | 名称 | 持仓 | 可用 | 成本 | 现价 | 浮动盈亏 | 今日 |",
                    "|---|---|---|---|---|---|---|---|",
                ]
            else:
                rows = [
                    "【你名下的持仓】",
                    "| 代码 | 名称 | 持仓 | 可用 | 成本 | 现价 | 浮动盈亏 |",
                    "|---|---|---|---|---|---|---|",
                ]
            for p in positions:
                code = str(p.get("code") or "")
                name = str(p.get("name") or "—")
                try:
                    total_f = float(p.get("volume") or 0)
                    cost = float(p.get("cost_price") or 0)
                except (TypeError, ValueError):
                    continue
                total = f"{total_f:.0f}"
                # 可卖量按代码从桥取（T+1 口径）；桥不可达 → —（绝不拿持仓量冒充）。
                # 但桥的 available 是**真实账户**的整只可卖量，可能大于本 agent 名下的
                # 持仓（共用账户，别家也持有同一只）→ 必须夹到本账持仓，否则模型会
                # 以为自己能卖出超过持仓的量（2026-10-19 实测 601857：持仓 700 / 桥 1500）。
                avail = available_map.get(code)
                avail_s = "—"
                if avail is not None:
                    try:
                        avail_s = str(min(int(avail), int(total_f)))
                    except (TypeError, ValueError, OverflowError):
                        avail_s = str(avail)
                if is_review:
                    snap = read_snapshot(env, code) if code else {}
                    last = float(snap.get("now") or 0)
                    prev = float(snap.get("last_close") or 0)
                    day_pct = (
                        f"{(last / prev - 1) * 100:+.2f}%"
                        if last > 0 and prev > 0
                        else "—"
                    )
                else:
                    last = read_price(env, code) if code else 0.0
                    day_pct = ""
                if last > 0 and cost > 0:
                    pnl = f"{(last / cost - 1) * 100:+.2f}%"
                    last_s = f"{last:.2f}"
                else:
                    pnl, last_s = "—", "—"
                row = (
                    f"| {code} | {name} | {total} | {avail_s} | {cost:.3f} | "
                    f"{last_s} | {pnl}"
                )
                if is_review:
                    row += f" | {day_pct}"
                rows.append(row + " |")
            parts.append("\n".join(rows))
        elif ledger.get("known"):
            parts.append("【你名下的持仓】空仓")
        else:
            parts.append("【你名下的持仓】空仓（分账账本里暂无你的成交记录）")
    else:
        gaps.append("account_unreachable")
        parts.append(
            "【你的分账额度】分账账本不可达，本轮无额度/持仓数据（只按新闻面分析）"
        )

    news = _news_block()
    if news:
        parts.append("【当日新闻分子】\n" + news)

    # 研究总控（系统级综合）：其他 agent 次日可见——方向/资金/新闻/模型分数/
    # 板块/风险。缺失不占位（空串），本轮按新闻与持仓照常分析。
    research = _research_block()
    if research:
        parts.append("【研究总控·今日前瞻】\n" + research)

    parts.append(
        _requirement_block(
            has_account=has_account, has_positions=has_positions, mode=mode
        )
    )
    return "\n\n".join(parts), gaps


#: 复盘轮的统一开头要求（各态模板共用；「先行输出」保证 QQ 摘要抽取拿到干货）。
REVIEW_SUMMARY_INSTRUCTION = (
    "4) 【先行输出】开头先给一段 ≤120 字的「收盘复盘摘要」（一段话：大盘一句、"
    "持仓一句、明日一句），随后再展开全文。"
)


def _requirement_block(
    *, has_account: bool, has_positions: bool, mode: str = "intraday"
) -> str:
    """尾段要求模板——**随数据齐缺三态降级**（C5 核心）。

    无账户数据时逐字禁止「点评持仓/编造持仓」：账本不可达的轮次只许按新闻面
    说话。``mode='review'`` 换成收盘复盘三问（今日复盘 / 持仓回顾 / 明日计划）。
    """
    tail = (
        "输出中文 markdown，简洁、先结论后理由；你的输出展示在实盘看板的「模型对话」里，"
        "只输出观点与建议（本系统不会据此自动下单）。"
    )
    is_review = mode == "review"
    if not has_account:
        if is_review:
            return (
                "【要求】本轮**没有**分账额度与持仓数据（分账账本不可达），给出今日收盘复盘：\n"
                "1) **禁止**点评持仓、禁止编造任何持仓或账户数字——你没有这些数据；\n"
                "2) 按上述指数与新闻面复盘今日大盘与板块；\n"
                "3) 给出明日关注方向（仅为观察，不是指令）；\n"
                "4) 【先行输出】开头先给一段 ≤120 字的「收盘复盘摘要」（大盘与明日，"
                "一段话），随后再展开全文；并在文中明确说明缺少账户上下文。\n" + tail
            )
        return (
            "【要求】本轮**没有**分账额度与持仓数据（分账账本不可达），给出本轮盘中分析：\n"
            "1) **禁止**点评持仓、禁止编造任何持仓或账户数字——你没有这些数据；\n"
            "2) 只按上述新闻面分析大盘与板块的风险与机会；\n"
            "3) 明确说明本轮结论缺少账户上下文，仅供参考。\n" + tail
        )
    if not has_positions:
        if is_review:
            return (
                "【要求】账户当前**空仓**（无持仓），给出今日收盘复盘：\n"
                "1) 不要点评持仓——当前没有持仓，也禁止编造；\n"
                "2) 按上述指数与新闻面复盘今日大盘与板块；\n"
                "3) 给出明日候选方向与入场触发条件（仅为观察，不是指令）；\n"
                + REVIEW_SUMMARY_INSTRUCTION
                + "\n"
                + tail
            )
        return (
            "【要求】账户当前**空仓**（无持仓），给出本轮盘中分析：\n"
            "1) 不要点评持仓——当前没有持仓，也禁止编造；\n"
            "2) 结合新闻面指出风险与机会；\n"
            "3) 如认为值得关注，给出候选方向与入场触发条件（仅为观察，不是指令）。\n"
            + tail
        )
    if is_review:
        return (
            "【要求】围绕上述你的分账额度与持仓给出今日收盘复盘：\n"
            "1) 复盘今日大盘与板块（结合指数与新闻面）；\n"
            "2) 逐一回顾持仓今日表现，判断各持仓逻辑是否变化；\n"
            "3) 给出明日计划：持仓处置倾向（持有/减仓/加仓）与候选方向/触发条件"
            "（仅为观察，不是指令）；\n" + REVIEW_SUMMARY_INSTRUCTION + "\n" + tail
        )
    return (
        "【要求】围绕上述你的分账额度与持仓给出本轮盘中分析：\n"
        "1) 逐一点评持仓（结合成本与现价）；\n"
        "2) 结合新闻面指出风险与机会；\n"
        "3) 给出操作倾向（加仓/减仓/持有）与触发条件。\n" + tail
    )


class TruncatedOutputError(RuntimeError):
    """输出被 max_tokens 截断（与 ``news_brief.TruncatedOutputError`` 同语义）：
    半截分析混进「模型对话」与完整分析无法区分，**整轮作废、绝不落盘**。"""

    def __init__(self, content: str, usage: dict | None):
        super().__init__("输出被 max_tokens 截断，整轮作废")
        self.content = content
        self.usage = usage


def call_model(
    env: dict[str, str], sig: str, model: str, user_content: str
) -> tuple[str, dict | None]:
    """OpenAI 兼容调用（各模型走各自供应商；失败重试 1 次后 raise）。"""
    prefix, _ = ROSTER[sig]
    base = (env.get(f"{prefix}_API_BASE") or "").rstrip("/")
    key = env.get(f"{prefix}_API_KEY") or ""
    if prefix == "OPENAI" and not base:
        # 本仓 .env 的 OpenAI 兼容键有两种写法，兜底 DEEPSEEK_*
        base = (env.get("DEEPSEEK_BASE_URL") or "").rstrip("/")
        key = key or env.get("DEEPSEEK_API_KEY") or ""
    if not base or not key:
        raise RuntimeError(f"{prefix}_API_BASE/KEY 缺失")
    import requests

    system = (
        f"你是 {model} 模型驱动的 A股 实盘交易助手盘中持仓分析师。"
        "分析冷静客观，给可执行的操作建议，注意 A股 T+1 规则与风险。输出中文 markdown。"
    )
    max_tokens = MAX_TOKENS
    try:
        override = int(str(env.get("QM_DIALOGUE_MAX_TOKENS") or "").strip() or "0")
        if override > 0:
            max_tokens = override
    except ValueError:
        pass

    def _payload(user_text: str) -> dict:
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }

    last_exc: Exception | None = None
    for attempt in range(2):
        # 第 2 次尝试追加「精简输出」提示：截断轮首因是话太长，同一 payload 原样
        # 重试大概率再撞上限（2026-10-12 实测）；其余失败（网络等）追加也无害。
        payload = _payload(user_content + (RETRY_BREVITY_NOTE if attempt else ""))
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
            # max_tokens 截断：半截答案（含 reasoning 兜底捞出的思考草稿）整轮作废
            # ——重试 1 次后仍截断即放弃，绝不落盘（照 news_brief.call_llm 的 TRUNC 语义）
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


def append_log(
    user: str,
    content: str,
    sig: str,
    usage: dict | None = None,
    data_gaps: list[str] | None = None,
    mode: str = "intraday",
) -> Path:
    """落盘对话日志（与 news_brief.append_log 同结构 → 前端直接可读）。

    ``data_gaps`` 随条目落盘（前端据此在卡片挂「无实盘账户数据」横幅）；
    截断轮**根本不会走到这里**（call_model 直接 raise）。``mode='review'`` 的
    条目附 ``"mode":"review"`` 标记（前端/审计据此区分复盘轮）。
    """
    now = datetime.now(CN_TZ)
    log_dir = LOG_DIR / sig / "log" / now.strftime("%Y-%m-%d")
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
    if data_gaps:
        entry["data_gaps"] = list(data_gaps)
    if mode != "intraday":
        entry["mode"] = mode
    path = log_dir / "log.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


#: 模型按提示词写的摘要标题（加粗行「**收盘复盘摘要**」也算标题）。
_SUMMARY_LABEL = "复盘摘要"
#: 免责声明不是摘要：短行含这些词即跳过（长正文里出现不受影响）。
_BOILERPLATE_MARKERS = (
    "仅复盘观点",
    "不构成自动交易指令",
    "不构成投资建议",
    "仅供参考",
    "本系统不会据此自动下单",
)
_BOILERPLATE_MAX_CHARS = 60
_MD_RULE_RE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*")
_MD_BULLET_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+")
_MD_QUOTE_RE = re.compile(r"^\s{0,3}>\s?")


def _classify_line(raw: str) -> tuple[str, str]:
    """markdown 行 → ``(heading/text/"", 去装饰文本)``；空行与分割线返回空 kind。"""
    line = str(raw or "").strip()
    if not line or _MD_RULE_RE.fullmatch(line):
        return "", ""
    is_heading = bool(_MD_HEADING_RE.match(line))
    if is_heading:
        line = _MD_HEADING_RE.sub("", line)
    else:
        line = _MD_BULLET_RE.sub("", line)
        line = _MD_QUOTE_RE.sub("", line)
    line = line.replace("**", "").replace("`", "").strip()
    if not line:
        return "", ""
    if not is_heading and len(line) <= 12 and _SUMMARY_LABEL in line:
        return "heading", line
    return ("heading" if is_heading else "text"), line


def _paragraphs(content: str) -> list[tuple[str, str]]:
    """按空行分段 → ``[(最近标题, 段文本)]``；标题后的空行不丢标题。"""
    out: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []

    def _flush() -> None:
        nonlocal buf
        if buf:
            out.append((heading, " ".join(buf).strip()))
            buf = []

    for raw in str(content or "").splitlines():
        kind, text = _classify_line(raw)
        if kind == "":
            _flush()
        elif kind == "heading":
            _flush()
            heading = text
        else:
            buf.append(text)
    _flush()
    return out


def _is_boilerplate(text: str) -> bool:
    return len(text) <= _BOILERPLATE_MAX_CHARS and any(
        marker in text for marker in _BOILERPLATE_MARKERS
    )


def extract_summary(content: str, limit: int = 160) -> str:
    """从模型回复抽一段话摘要（复盘 QQ 卡片用，纯函数）。

    优先取「复盘摘要」标题下的那段正文；标题、分割线、以及开头孤立的免责声明
    （「仅复盘观点，不构成自动交易指令」）都**不是**摘要——2026-10-20 实况：
    旧实现把这些当摘要，卡片上只有标题或免责声明。找不到标题时退回第一个有
    实质内容的段落。只做裁剪，绝不改写或编造。
    """
    paras = [
        (heading, body)
        for heading, body in _paragraphs(content)
        if body and not _is_boilerplate(body)
    ]
    preferred = [body for heading, body in paras if _SUMMARY_LABEL in heading]
    joined = (preferred or [body for _, body in paras] or [""])[0]
    if len(joined) > limit:
        joined = joined[: limit - 1].rstrip() + "…"
    return joined


def _flag_on(env: dict[str, str], key: str, default: bool = True) -> bool:
    """提醒开关判定（默认开的词表口径，同 ``scheduler_registry.switch_enabled``）：
    未配置用默认值；``0/false/no/off`` 视为关（大小写不敏感）。"""
    raw = str(env.get(key, "") or "").strip().lower()
    if raw == "":
        return default
    return raw not in {"0", "false", "no", "off"}


def _yield_to_decision_round(env: dict[str, str], now: datetime) -> tuple[bool, str]:
    """盘中轮让位判据：决策轮在岗到点 → 本轮对话由它产出（甲5）。``(让位?, 理由)``。

    判据本体在 ``backend.shared.decision_round_onduty``（读决策轮 worker 心跳式重写
    的标记文件）。**三处失败都 fail-open 到「照跑」**：

    * ``QM_DIALOGUE_YIELD_TO_DECISION_ROUND=off``（运维显式关掉归并）；
    * 模块 import 不到（宿主装的是老码 / 公开仓缺包）；
    * 判据本身抛异常（标记坏了、路径不可达……）。

    方向是**故意**的：判据失灵时回到「两边同跑 + 双写」的老行为——展示面宁可重复
    也不能**静默变空**（「模型对话没有总结」正是那类故障的投诉）。
    """
    if not _flag_on(env, "QM_DIALOGUE_YIELD_TO_DECISION_ROUND", default=True):
        return False, ""
    try:
        from backend.shared.decision_round_onduty import on_duty_for
    except Exception as exc:  # noqa: BLE001 缺包=老行为，照跑
        print(f"⚠️ 让位判据不可用（{type(exc).__name__}: {exc}），本轮照跑")
        return False, ""
    try:
        return on_duty_for(now)
    except Exception as exc:  # noqa: BLE001 判据异常一律照跑
        print(f"⚠️ 让位判据异常（{type(exc).__name__}: {exc}），本轮照跑")
        return False, ""


def _load_alert_state() -> dict:
    try:
        data = json.loads(ALERT_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_alert_state(state: dict) -> None:
    try:
        ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ALERT_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass  # 状态落盘失败只影响提醒去重，不挡分析


def decide_transition_alert(prev_down: bool, is_down: bool) -> str | None:
    """桥可达性翻转判定（纯函数）：``'down'``/``'up'``/``None``（无变化不推）。"""
    if is_down and not prev_down:
        return "down"
    if prev_down and not is_down:
        return "up"
    return None


def format_round_alert(failed: list[str], ok_count: int, total: int) -> tuple[str, str]:
    """轮次不满员/全失败的 QQ 文案（纯函数）→ ``(title, content)``。"""
    failed_sigs = "、".join(f.split(":", 1)[0] for f in failed)
    if ok_count:
        title = "⚠️ 模型对话轮不满员"
        content = (
            f"本轮 {ok_count}/{total} 个模型完成，未完成：{failed_sigs}。\n"
            "多为输出超长截断（已带精简提示重试一次）；明细见 "
            "logs/keepers/live_model_analysis.log，完整对话见 交易台 › 模型对话。"
        )
    else:
        title = "❌ 模型对话轮全部失败"
        content = (
            f"本轮 {total} 个模型全部失败：{failed_sigs}。\n"
            "明细见 logs/keepers/live_model_analysis.log。"
        )
    return title, content


#: QQ 消息上限 1600（``push_notify.notify`` 超限静默截断）；自留余量。
_REVIEW_CARD_MAX_CHARS = 1500
_CARD_RULE = "━" * 14


def _format_pool_row(idx: int, row: dict) -> str:
    """池行：``序号 名称 代码 · 板块 · 分数``（板块缺失显示 —，不编造）。"""
    code = str(row.get("code") or "").strip()
    name = str(row.get("name") or "").strip() or code
    industry = str(row.get("industry") or "").strip() or "—"
    line = f"{idx:02d} {name} {code} · {industry}"
    score = row.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        line += f" · {float(score):.1f}"
    return line


def format_review_card(
    summaries: list[tuple[str, str]],
    now: datetime,
    pool_rows: list[dict] | None = None,
    max_chars: int = _REVIEW_CARD_MAX_CHARS,
) -> tuple[str, str]:
    """收盘复盘 QQ 卡片（纯函数）→ ``(title, content)``；卡片式纯文本（QQ 拒 markdown）。

    ``pool_rows`` = 当日池（``load_pool_snapshot``）：「包括晚上推理出来的模型
    top20 的股票板块都要发我」（2026-10-20）。超预算时从尾部丢池行并如实
    标注剩余条数——绝不静默截断半行。
    """
    head = [_CARD_RULE]
    for sig, summary in summaries:
        head.append(f"◆ {sig}")
        head.append(summary or "（本轮无摘要）")
    pool_header = (
        [_CARD_RULE, f"📦 今日池 · {len(pool_rows)} 只（板块 · 分数）"]
        if pool_rows
        else []
    )
    tail = [_CARD_RULE, "详情见 交易台 › 模型对话"]

    rows: list[str] = []
    dropped = 0
    if pool_rows:
        budget = max_chars - len("\n".join(head + pool_header + tail))
        for idx, row in enumerate(pool_rows, 1):
            line = _format_pool_row(idx, row)
            cost = len(line) + 1  # 行 + 换行
            if budget - cost < 0:
                dropped = len(pool_rows) - idx + 1
                break
            rows.append(line)
            budget -= cost
    if dropped:
        rows.append(f"…另有 {dropped} 只见交易台 › 候选池")
    content = "\n".join(head + pool_header + rows + tail)
    return f"📋 收盘复盘 · {now.strftime('%m-%d')}", content


def load_pool_snapshot(day: datetime) -> list[dict]:
    """当日池（按**目标交易日**命名）+ 板块 → ``[{code,name,industry,score}]``。

    池文件缺 ``industry``（2026-10-20 前的存量）时用 QuantDB 主表补；主表不可读
    则显示 —。读不到池返回 ``[]``（卡片不带池段，不编造）。
    """
    path = (
        ROOT
        / "data"
        / "reports"
        / "stock_picks"
        / f"{day.strftime('%Y%m%d')}_agent_picks.json"
    )
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = doc.get("picks") or doc.get("candidates") or []
    if not isinstance(raw, list):
        return []
    rows: list[dict] = []
    for item in raw[:20]:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "code": str(item.get("code") or "").strip(),
                "name": str(item.get("name") or "").strip(),
                "industry": str(item.get("industry") or "").strip(),
                "score": item.get("score"),
            }
        )
    if any(not row["industry"] for row in rows):
        try:
            industries = load_industry_map()
        except Exception:  # noqa: BLE001 板块是装饰，不得拖垮复盘
            industries = {}
        for row in rows:
            if not row["industry"]:
                row["industry"] = industries.get(row["code"], "")
    return rows


def notify_qq(title: str, content: str) -> bool:
    """推一条 QQ（best-effort）：失败只打印，绝不挡分析（更不挡交易）。"""
    try:
        import push_notify  # scripts/ 同目录（keeper 系列同口径）

        push_notify.notify(title, content, channel="default")
        print(f"📨 QQ 已推送：{title}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ QQ 推送失败（不影响分析）: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="实盘模型对话轮（只出观点，不下单）")
    ap.add_argument("--agents", default="", help="逗号分隔的模型签名；缺省=名册全部")
    ap.add_argument(
        "--mode",
        default="intraday",
        choices=["intraday", "review"],
        help="intraday=盘中轮（缺省）；review=盘后复盘轮（北京 15:30 档）",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="无视「决策轮在岗则让位」判据强跑一轮（「立即分析」按钮走这条路）",
    )
    args = ap.parse_args()

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock = LOCK.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("已有分析实例在跑，本轮跳过")
        return 1

    try:
        targets = [a.strip() for a in args.agents.split(",") if a.strip()] or list(
            ROSTER
        )
        env = _load_env()
        mode = args.mode
        alerts_on = _flag_on(env, "QM_DIALOGUE_ALERTS", default=True)
        now = datetime.now(CN_TZ)
        # 甲5：决策轮在岗到点 → 本让位（同槽同文件的对话由决策轮落，见模块头）。
        # 复盘轮是收盘语义、--force 是人按的按钮，两者都不过这道闸。
        if mode != "review" and not args.force:
            yielded, why = _yield_to_decision_round(env, now)
            if yielded:
                print(f"[让位] {why} → 本轮不跑（要强跑用 --force）")
                return 0
        unknown = [t for t in targets if t not in ROSTER]
        for u in unknown:
            print(f"⚠️ 跳过 {u}：不在模型名册（{', '.join(ROSTER)}）")
        runnable = [t for t in targets if t in ROSTER]
        if not runnable:
            print("没有可运行的模型")
            return 2

        # 一轮读一次分账账本（每家一份），再按模型拼各自的提示词：额度与持仓
        # **只出现该 agent 自己的那本账**，不读真实账户总额。
        ledgers = read_agent_ledgers(env, runnable)
        availability = _bridge_availability(env)
        # 一轮读一次真实账户持仓（幻影仓闸门）：账本管归属，账户管事实。
        real_volumes, real_source = read_real_position_volumes(env)
        if real_volumes is None:
            print("⚠️ 真实账户持仓不可读：本轮不做幻影仓剔除（账本原样展示）")
        else:
            print(f"实盘持仓对账源：{real_source}（{len(real_volumes)} 只）")
        account_down = bool(runnable) and all(
            ledgers.get(sig) is None for sig in runnable
        )
        if account_down:
            print("⚠️ 分账账本不可达：本轮提示词降级为无账户模板")
        # 账本失联/恢复：只在翻转沿推（状态文件去重，morning 失联 5 轮只吵一次）。
        # 状态键沿用 ``bridge_down``（历史键名），语义 =「账户上下文不可达」。
        if alerts_on:
            state = _load_alert_state()
            transition = decide_transition_alert(
                bool(state.get("bridge_down")), account_down
            )
            if transition == "down":
                notify_qq(
                    "⚠️ 模型对话轮降级",
                    f"{now.strftime('%H:%M')} 起分账账本不可达（PG 查询失败），"
                    "模型对话轮已降级为无持仓上下文（恢复前每轮都如此）。"
                    "请检查 DB/网络与容器状态。",
                )
            elif transition == "up":
                notify_qq(
                    "✅ 模型对话轮恢复",
                    f"{now.strftime('%H:%M')} 分账账本已恢复，模型对话轮恢复正常持仓上下文。",
                )
            if transition:
                state["bridge_down"] = account_down
                _save_alert_state(state)

        ok, failed, summaries = [], [], []
        for sig in runnable:
            _, model = ROSTER[sig]
            try:
                user_content, data_gaps = build_user_content(
                    env,
                    agent=sig,
                    ledger=ledgers.get(sig),
                    availability=availability,
                    real_volumes=real_volumes,
                    mode=mode,
                )
                if data_gaps:
                    degraded = (
                        "（提示词已降级为无账户模板）"
                        if "account_unreachable" in data_gaps
                        else ""
                    )
                    print(f"⚠️ {sig} 数据缺口：" + ",".join(data_gaps) + degraded)
                content, usage = call_model(env, sig, model, user_content)
                append_log(user_content, content, sig, usage, data_gaps, mode=mode)
                ok.append(f"{sig} {len(content)}字")
                if mode == "review":
                    summaries.append((sig, extract_summary(content)))
                print(f"✅ {sig} 完成（{len(content)} 字）")
            except Exception as e:  # noqa: BLE001 单模型失败不挡其余
                failed.append(f"{sig}:{e}")
                print(f"❌ {sig} 失败: {e}")

        ok_count = len(ok)
        if ok_count:
            print(f"[SUMMARY] {ok_count}/{len(runnable)} 模型完成：" + " · ".join(ok))
        else:
            print("[SUMMARY] 全部模型失败：" + " · ".join(failed)[:180])

        # 轮次不满员/全败提醒（1h 限流——每小时一轮即每轮最多一条）
        if failed and alerts_on:
            state = _load_alert_state()
            last_ts = float(state.get("last_round_alert_ts") or 0)
            if time.time() - last_ts >= ROUND_ALERT_MIN_INTERVAL_S:
                title, content = format_round_alert(failed, ok_count, len(runnable))
                if notify_qq(title, content):
                    state["last_round_alert_ts"] = time.time()
                    _save_alert_state(state)

        # 复盘轮 QQ 卡片（同日只推一次——手动重跑不重复打扰）
        if (
            mode == "review"
            and ok_count
            and _flag_on(env, "QM_DIALOGUE_REVIEW_PUSH", default=True)
        ):
            state = _load_alert_state()
            today = now.strftime("%Y-%m-%d")
            if state.get("review_pushed_on") != today:
                # 复盘卡同时带上当日池（板块 + 分数）：2026-10-20 用户要求
                # 「晚上推理出来的模型 top20 的股票板块都要发我」。
                title, content = format_review_card(
                    summaries, now, pool_rows=load_pool_snapshot(now)
                )
                if notify_qq(title, content):
                    state["review_pushed_on"] = today
                    _save_alert_state(state)

        return 0 if ok_count else 2
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
