"""arena 文件面**写侧**——决策轮落盘「模型对话 / 成交 / 持仓」三个 tab 的数据。

为什么需要这个模块
------------------
上游 quant-Trader 的 ``scripts/live_hourly_analysis.py`` 是**一个回路**：
分析写 ``agent_data_astock/{agent}/log/{date}/log.jsonl``、成交写
``.../position/position.jsonl``，同一次调用里完成「分析 → 决策 → 闸门 → 下单 →
分账 → 落盘」。迁 QM 后这个回路被拆成两半且各自断了一条腿：

* **决策轮**（``services/trade/services/decision_round.py``）保留了下单，但只写 PG
  （``qm_decision_ledger`` / ``qm_agent_ledger_*``），**一个 arena 文件都不写**；
* **对话轮**（``scripts/live_model_analysis.py``，宿主 cron）保留了分析落盘，但
  **只读不下单**。

于是 arena 前端的三个 tab 各自对着一个没有写者的文件：``position.jsonl`` 全仓 grep
**零写入方**（最后写入 2026-09-29 23:48），成交/持仓/净值冻结；``log.jsonl`` 的写者
是被阉割的只读轮，模型输出无四段式 → 总结段空白。

本模块把写侧接回来。**它是这两个文件的唯一写者**——别再在别处开第二个写点，
两份实现必然漂移（本仓的老病）。

为什么不改读侧去查 PG
--------------------
前端与 ``agent_data.py`` 的 8 个读侧（``load_position_records`` / ``load_trades`` /
``compute_equity_series`` / ``compute_extended_summary`` 的 Sharpe、胜率、盈亏比、
平均持仓时长）全部按下面的行格式写死。换读侧是另一场迁移；本次只补写侧。

行格式（**写入契约**，改格式必须同步改 ``agent_data.py``）
--------------------------------------------------------
``log.jsonl``::

    {"timestamp": "2026-10-14T13:02:19.634463+08:00", "signature": "glm-5.3-flash",
     "new_messages": [{"role": "user", "content": "<完整提示词>"},
                      {"role": "assistant", "content": "<模型原文>"}],
     "usage": {"prompt_tokens": 1104, "completion_tokens": 3819, "total_tokens": 4923}}

* ``usage`` 是**必须键**：``/token-usage`` 端点（``routers/agent_arena/agents_family.py``）
  只统计带 ``usage`` 的行，缺了这行就不进 token 账；
* 四段式标记（【总体总结】/【分析链路】/【推理论证】/【决策】）由**提示词**要求模型
  输出，前端 ``parseAnalysis.ts`` 据此切段——写入侧不合成标记，模型没写就没写。

``position.jsonl``::

    {"date": "2026-10-14", "id": 12,
     "this_action": {"action": "buy", "symbol": "600338.SH", "amount": 800},
     "positions": {"600338.SH": 800, "CASH": 91234.56}}

* 无 ``this_action`` 的行 = 非交易行（持仓快照/净值采样）——``load_trades`` 靠这个键
  筛成交，**不要**给非交易行补一个空 ``this_action``；
* 代码一律**后缀式**（``600338.SH``），与 PG 面的前缀式不同（``stock_utils`` 归一）；
* ``id`` 单调自增（读文件尾行的 ``id`` + 1），前端按它排序去重。

时间口径
--------
``timestamp`` 一律 **CST 墙钟 + ``+08:00`` 后缀**，与既有文件一致。不写 UTC ``Z``：
``datetime.ts`` 的 ``fmtDateTime`` 是**字符串切片**不做时区换算，写 ``Z`` 会让界面钟面
差 8 小时；而 ``agents_family.py`` 的 ``/token-usage`` 用字符串比大小取最新，混格式
会静默选错行。（PG 侧的「瞬时列一律 TIMESTAMPTZ + aware UTC」是另一条纪律，那条管的是
``sim_trades.executed_at`` 这类列，不管这个展示面文件。）

落盘纪律
--------
* **append-only**：一次 ``open("a")`` 写一行（单次 ``write`` 落整行），进程内
  ``threading.Lock`` 防三 agent 并发交错；
* **绝不静默**：写失败 ``logger.error`` 并返回 ``False``，调用方据此留痕——
  展示面可以坏，但不可以在没人知道的情况下坏；
* **绝不抛**：除编程错误外本模块不向上抛异常（一次磁盘满不该打断真钱回路）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.shared.env_flags import env_flag
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

#: 北京时间（arena 文件面时间口径，见模块 docstring）
CN_TZ = timezone(timedelta(hours=8))

#: 总开关。默认**开**——这正是本次要恢复的能力；关掉只是给排障留的退路。
ENABLE_FLAG = "QM_ARENA_FILES"

#: 一次 ``write`` 落整行的上限保护（模型可能吐几十 KB 散文）
_LINE_LIMIT = 4_000_000

#: 读文件尾部找上一行 ``id`` 时回看的字节数（够容纳一行就很够）
_TAIL_BYTES = 64 * 1024

#: 单进程内防交错（三 agent 的轮次可能并发落盘）
_LOCK = threading.Lock()


def arena_files_enabled() -> bool:
    """写侧总开关（``QM_ARENA_FILES``，只认真值 token，词表见 ``env_flags``）。"""
    return env_flag(ENABLE_FLAG, default=True)


def _as_cn(ts: datetime | None) -> datetime:
    """归一成 CST aware。

    naive 值**按 CST 解释**而不是按 UTC：调用方给的墙钟来自 ``now_cn()`` 一系，
    其语义就是北京时间；当 UTC 处理会把整条时间轴平移 8 小时，而文件里看不出错。
    """
    if ts is None:
        return datetime.now(CN_TZ)
    return ts.replace(tzinfo=CN_TZ) if ts.tzinfo is None else ts.astimezone(CN_TZ)


def _data_root() -> Path:
    """arena 数据根——**必须**走读侧同一个解析器，否则写到一个没人读的地方。

    容器里 ``/app/data`` 是空目录而真数据在 ``/data``；宿主仓库根 ``data/`` 是
    指向真实数据盘的符号链接。``arena_config.data_root()`` 用哨兵文件
    （``arena/quanttrader/MANIFEST.jsonl``）把两边判成同一份，正是此处要的。
    """
    from backend.services.agent_arena.arena_config import data_root

    return data_root()


def agents_root() -> Path:
    """A股 agent 数据目录（``…/agent_data_astock``）。

    换根**只走** ``QM_ARENA_DATA_ROOT``（``arena_config`` 已提供的唯一入口）：
    再开一个 ``QM_ARENA_AGENTS_DIR`` 就是同一件事的第二读法，两个变量必然漂移。
    """
    return _data_root() / "agent_data_astock"


def log_file(agent: str, day: str | None = None) -> Path:
    """``…/{agent}/log/{YYYY-MM-DD}/log.jsonl``（``day`` 缺省取北京今日）。"""
    d = day or datetime.now(CN_TZ).strftime("%Y-%m-%d")
    return agents_root() / agent / "log" / d / "log.jsonl"


def position_file(agent: str) -> Path:
    """``…/{agent}/position/position.jsonl``。"""
    return agents_root() / agent / "position" / "position.jsonl"


def _write_line(path: Path, record: Mapping[str, Any]) -> bool:
    """append 一行 JSON；失败记 error 并返回 False（不抛）。"""
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        logger.error("[arena_files] 记录无法序列化：%s（落点 %s）", exc, path)
        return False
    if len(line) > _LINE_LIMIT:
        # 截断是**欺骗**：半截 JSON 读侧解析不了，而文件里看起来「有一行」。
        # 宁可整行不写并报错——调用方看得见。
        logger.error(
            "[arena_files] 记录超长 %d 字节（上限 %d），整行拒写：%s",
            len(line),
            _LINE_LIMIT,
            path,
        )
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
        return True
    except OSError as exc:
        logger.error("[arena_files] 写入失败：%s（落点 %s）", exc, path)
        return False


def _read_last_id(path: Path) -> int:
    """文件尾行的 ``id``；文件不存在/读不出/无 ``id`` 一律 0（下一行即 1）。"""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            back = min(size, _TAIL_BYTES)
            fh.seek(size - back)
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return 0
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # 尾行可能是被回看窗口切半的——继续往前找
            continue
        if not isinstance(row, dict):
            continue
        try:
            return int(row.get("id") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _usage_or_none(usage: Any) -> dict[str, int] | None:
    """把 provider 的 usage 归成三键 int 表；取不到就**不带** ``usage``。

    不编造零值：``{"total_tokens": 0}`` 会被 ``/token-usage`` 当成一次真实调用累加，
    token 账虚低。无 usage 是「这次没统计到」，不是「用了 0 个 token」。
    """
    if not isinstance(usage, Mapping):
        return None
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        raw = usage.get(key)
        if raw is None:
            continue
        try:
            out[key] = int(raw)
        except (TypeError, ValueError):
            continue
    return out or None


def append_dialogue(
    *,
    agent: str,
    prompt: str,
    response: str,
    usage: Any = None,
    timestamp: datetime | None = None,
    day: str | None = None,
) -> bool:
    """落一行 ``log.jsonl``（一轮 = 一行的 user+assistant 两消息）。

    写的是**模型原文**：不做段落合成、不补四段式标记。前端靠原文里的标记切段，
    写入侧替模型「补」一段总结 = 界面显示了一段模型没说过的话。
    """
    if not arena_files_enabled():
        return False
    name = str(agent or "").strip()
    if not name:
        logger.error("[arena_files] agent 名为空：拒绝写对话日志（会落进别家目录）")
        return False
    ts = _as_cn(timestamp)
    record: dict[str, Any] = {
        "timestamp": ts.isoformat(),
        "signature": name,
        "new_messages": [
            {"role": "user", "content": str(prompt or "")},
            {"role": "assistant", "content": str(response or "")},
        ],
    }
    u = _usage_or_none(usage)
    if u is not None:
        record["usage"] = u
    # 目录日**从 ts 推**而不是取「今天」：调用方补跑历史轮时要落到那一天，
    # 取今天会让补跑的分析出现在错误的日子目录里（读侧按目录日期切档）。
    return _write_line(log_file(name, day or ts.strftime("%Y-%m-%d")), record)


def append_position(
    *,
    agent: str,
    positions: Mapping[str, Any],
    this_action: Mapping[str, Any] | None = None,
    when: datetime | None = None,
) -> bool:
    """落一行 ``position.jsonl``（持仓快照；``this_action`` 非空即为成交行）。

    ``positions`` 是 ``{代码: 数量}``，另须带 ``CASH`` 现金键（读侧
    ``list_agents`` 用它填 ``cash`` 字段）。代码经 ``StockCodeUtil.to_suffix``
    归一到后缀式——上游写的就是后缀式，PG 面的前缀式混进来会让前端按码查名查不到。
    """
    if not arena_files_enabled():
        return False
    name = str(agent or "").strip()
    if not name:
        logger.error("[arena_files] agent 名为空：拒绝写持仓行")
        return False
    ts = _as_cn(when)
    norm: dict[str, Any] = {}
    for code, qty in (positions or {}).items():
        key = str(code)
        if key.upper() in ("CASH", "现金"):
            norm["CASH"] = qty
            continue
        try:
            norm[StockCodeUtil.to_suffix(key)] = qty
        except Exception:  # noqa: BLE001 归一失败保原样——宁可显示怪码，不要静默丢持仓
            norm[key] = qty
    record: dict[str, Any] = {
        "date": ts.strftime("%Y-%m-%d"),
        "id": _read_last_id(position_file(name)) + 1,
        "positions": norm,
    }
    if this_action:
        action = dict(this_action)
        sym = action.get("symbol")
        if sym:
            try:
                action["symbol"] = StockCodeUtil.to_suffix(str(sym))
            except Exception:  # noqa: BLE001
                pass
        record["this_action"] = action
    return _write_line(position_file(name), record)


def positions_from_ledger(
    ledger_positions: Mapping[str, Any], virtual_cash: float
) -> dict[str, Any]:
    """分账账本段 → ``position.jsonl`` 的 ``positions`` 表。

    ``ledger_positions`` 形如 ``{后缀码: {volume, cost_price, buy_ts, last_ts}}``
    （见 ``decision_round_core.AgentLedgerRead``）。``volume`` 为 0 或缺失的票
    **照写 0**而不是删键：前端「已清仓」与「从未持有」是两回事（前者要出现在
    平仓记录里）。
    """
    out: dict[str, Any] = {}
    for code, pos in (ledger_positions or {}).items():
        vol = pos.get("volume") if isinstance(pos, Mapping) else pos
        out[str(code)] = vol if vol is not None else 0
    try:
        out["CASH"] = round(float(virtual_cash), 2)
    except (TypeError, ValueError):
        out["CASH"] = 0.0
    return out


def append_round_dialogue(
    *,
    agent: str,
    prompt: str,
    response: str,
    usage: Any = None,
    timestamp: datetime | None = None,
) -> bool:
    """决策轮的对话落盘入口（``append_dialogue`` 的语义别名，便于调用点自证意图）。"""
    return append_dialogue(
        agent=agent,
        prompt=prompt,
        response=response,
        usage=usage,
        timestamp=timestamp,
    )
