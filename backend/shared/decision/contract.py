"""LLM 决策契约：两套 schema、解析三态、执行比例口径（纯核心，不碰 LLM 网络）。

定位
----
本模块回答一个问题：**模型吐出的那段文本，到底表达了多少可执行的信息**。
它不做风控判定（``gates.py``）、不下单（P2.3）、不写审计表（``decision_ledger_store.py``），
只把不可信的文本变成**类型化的、缺失与脏值可区分**的决策对象。

为什么不是「一个 dict 加几个 get」
----------------------------------
隔壁 BayMax 用 21 个键的裸 dict 承载决策，结果是三类真实事故：

1. **`pct` 三态丢失**（2026-09-12 审查 HIGH-1/2/4）。裸 dict 里 `d["pct"]` 同时表示
   「模型没给」和「模型给了 0」；卖出链对「没给」按清仓执行（模型说了卖、只是漏了
   比例），对「给了但读不出」（`"0.3股"`）必须**停手留痕**。不区分时，把「想减 30%」
   静默执行成清仓——资金方向上不可逆的放大。故 ``Pct`` 是一个**带 state 的类型**，
   消费点必须显式选口径。
2. **`NaN` 归错侧**（HIGH-4 终审）。JSON 允许字面量 `NaN`，模型与手改都能给。
   `NaN` 不是「没表达」而是「表达了但没法用」；按 missing 处理会走整仓卖出，
   与 dirty 的停手原则正好相反。故非有限数一律 ``dirty``。
3. **抽取能力不一致**（2026-09-12 P0-5）。见 ``json_extract`` 模块 docstring。

本仓还把两条链（4 字段调仓 / 8 字段盘中含 `watch`）合成**一个按 schema 参数化的
解析器**：字段集与 action 白名单由 ``SCHEMAS`` 单源决定，调用方不再各写一份。

失败三态（``api_failed`` / ``empty_output`` / ``parse_failed``）
----------------------------------------------------------------
**不能都写成「模型输出解析不了」**——三者要查的方向不同：调不通去看 key/base、
空响应去看上游/限流、解析失败去看提示词与模型是否跑偏。故 ``DecisionBatch.status``
是这三个字面量之一（或 ``ok``），而不是一个 bool。

留痕
----
``DecisionBatch.raw`` 保留原始输出（截断到 ``RAW_LIMIT``）供审计表落库；
``skipped_rows`` / ``ignored_actions`` 记录被丢掉的行——**模型产了 5 行、4 行 action
非法**这件事必须看得见，否则「今天为什么零交易」永远查不清。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from backend.shared.decision.json_extract import extract_json

__all__ = [
    "ACTIONS",
    "BUY",
    "Decision",
    "DecisionBatch",
    "FRAC_DIRTY",
    "FRAC_MISSING",
    "FRAC_OK",
    "FRAC_ZERO",
    "HOLD",
    "INTRADAY_SCHEMA_JSON",
    "JSON_ONLY_HINT",
    "PCT_DIRTY",
    "PCT_GIVEN",
    "PCT_MISSING",
    "Pct",
    "REBALANCE_SCHEMA_JSON",
    "RAW_LIMIT",
    "SCHEMAS",
    "SCHEMA_INTRADAY",
    "SCHEMA_REBALANCE",
    "SELL",
    "STATUS_API_FAILED",
    "STATUS_EMPTY_OUTPUT",
    "STATUS_OK",
    "STATUS_PARSE_FAILED",
    "WATCH",
    "parse_decisions",
    "parse_pct",
]

# ── action 词表 ──────────────────────────────────────────────────────
HOLD = "hold"
SELL = "sell"
BUY = "buy"
WATCH = "watch"

ACTIONS: tuple[str, ...] = (HOLD, SELL, BUY, WATCH)

# ── 两套 schema ──────────────────────────────────────────────────────
#: 09:35 调仓轮（只做「换股」这一件事）。
SCHEMA_REBALANCE = "rebalance"
#: 整点轮（多一层守护意图 `watch`）。
SCHEMA_INTRADAY = "intraday"

#: schema → **合法 action 白名单**。调仓轮的提示词里没有 `watch`，模型给了就必须
#: 当成非法行（计入 ``ignored_actions``），不能静默当 hold——那会把一个条件单
#: 意图悄悄丢掉，而隔壁 79% 的决策产出是 watch。
SCHEMAS: dict[str, tuple[str, ...]] = {
    SCHEMA_REBALANCE: (HOLD, SELL, BUY),
    SCHEMA_INTRADAY: ACTIONS,
}

#: 提示词里的 JSON 模板（**字面量即契约**：改了要有人看见，测试钉住）。
REBALANCE_SCHEMA_JSON = (
    '{"decisions": [{"action": "hold|sell|buy", "code": "600519.SH", '
    '"pct": 0.2, "reason": "一句话理由"}]}'
)

INTRADAY_SCHEMA_JSON = (
    '{"decisions": [{"action": "hold|sell|buy|watch", "code": "600519.SH", '
    '"name": "贵州茅台", "pct": 0.2, "stop_loss": 1500.0, '
    '"take_profit": 1650.0, "move_stop": 1520.0, '
    '"invalidation": "跌破1500或买入理由失效", "confidence": 0.8, '
    '"risk_amount": 2000.0, '
    '"reason": "一句话理由（必填：宏观/板块/技术证据+为什么现在动手）"}]}'
)

#: 解析失败重试时追加的纠正语（**后缀**，不是新提示词——同一次对话继续）。
JSON_ONLY_HINT = (
    "\n\n【纠正】上一次输出无法解析为决策 JSON（没有 JSON / decisions 为空 / "
    "格式不符）。请**只输出 JSON 对象本身**（不要 markdown 代码块、不要任何"
    "解释文字），且 decisions 数组必须逐只列出现有持仓的判断，格式同上面的 schema。"
)

# ── 解析状态 ─────────────────────────────────────────────────────────
STATUS_OK = "ok"
STATUS_API_FAILED = "api_failed"  # 调用失败，无输出
STATUS_EMPTY_OUTPUT = "empty_output"  # 返回空串
STATUS_PARSE_FAILED = "parse_failed"  # 有输出但取不出合法决策
#: 供应商没配（缺 key/base/占位符）——与 ``empty_output`` **必须分开**：前者去查
#: 配置，后者去查接口/限流，排查方向完全相反。相邻系统把这一态混进了 empty_output
#: （``call_llm`` 缺配置时返回 ``("", None)``），故它是本仓的新增态。
STATUS_NOT_CONFIGURED = "not_configured"

#: 四种失败态（``STATUS_OK`` 之外）。留痕/告警按它取文案，别在调用点写 if 链。
FAILURE_STATUSES: tuple[str, ...] = (
    STATUS_API_FAILED,
    STATUS_EMPTY_OUTPUT,
    STATUS_PARSE_FAILED,
    STATUS_NOT_CONFIGURED,
)

#: ``DecisionBatch.raw`` 的截断上限（字符）。留痕要有限度：模型可能吐几十 KB 散文，
#: 审计表不能跟着爆。
RAW_LIMIT = 8_000

#: ``Pct.raw`` 的截断上限——够看清模型给了什么（`"0.3股"`），又不至于把一整段
#: 理由塞进日志行。
PCT_RAW_LIMIT = 40

# ── 执行比例状态 ─────────────────────────────────────────────────────
FRAC_OK = "ok"  # 正常比例
FRAC_MISSING = "missing"  # 模型没表达比例
FRAC_ZERO = "zero"  # 明说 0（不动作）
FRAC_DIRTY = "dirty"  # 表达了但读不出 → 停手留痕


# ── pct 三态 ─────────────────────────────────────────────────────────
PCT_GIVEN = "given"
PCT_MISSING = "missing"
PCT_DIRTY = "dirty"


@dataclass(frozen=True)
class Pct:
    """执行比例的三态值（``given`` / ``missing`` / ``dirty``）。

    **不要直接读 ``value`` 做执行判断**——先看 ``state``：``missing`` 与 ``dirty``
    都是 0.0，但买卖两侧对它们的处置**方向相反**（见 :meth:`Decision.sell_intent`
    与 :meth:`Decision.buy_intent`）。裸浮点数承载不了这个区别，这正是既有事故的成因。
    """

    value: float = 0.0
    state: str = PCT_MISSING
    raw: str = ""  # dirty 时的原始值 repr（已截断），供留痕/审计

    @property
    def is_given(self) -> bool:
        return self.state == PCT_GIVEN

    @property
    def is_dirty(self) -> bool:
        return self.state == PCT_DIRTY


def parse_pct(x: object) -> Pct:
    """pct 解析 → :class:`Pct`（三态）。

    * **given**：有效有限数。字符串先规范化（去空白、全角 ％→%、结尾 % 视作百分数，
      ``"30%"`` → 0.3）——模型确实表达了比例，就尽量读出来而不是当没看见；
    * **missing**：``None``/空串/``N-A``——**没表达**，消费点回退默认；
    * **dirty**：给了值但解析不出（``"0.3股"``、``"三成"``、``[0.3]``、**布尔**），
      或解析出非有限数（``NaN``/``Infinity``/``1e999``，含字符串形式）。

    布尔归 dirty 是本仓相对既有的**收紧**：``float(True) == 1.0`` 会让 JSON 里的
    ``"pct": true`` 静默变成整仓卖出——与 dirty 的停手原则背道而驰。
    """
    if x is None:
        return Pct(0.0, PCT_MISSING)
    if isinstance(x, bool):  # bool 是 int 的子类，必须在数值分支之前判掉
        return Pct(0.0, PCT_DIRTY, repr(x)[:PCT_RAW_LIMIT])
    if isinstance(x, str):
        s = x.strip().replace("％", "%")
        if s in ("", "N/A", "n/a"):
            return Pct(0.0, PCT_MISSING)
        if s.endswith("%"):
            try:
                v = float(s[:-1]) / 100.0
            except ValueError:
                return Pct(0.0, PCT_DIRTY, repr(x)[:PCT_RAW_LIMIT])
        else:
            try:
                v = float(s)
            except ValueError:
                return Pct(0.0, PCT_DIRTY, repr(x)[:PCT_RAW_LIMIT])
    else:
        try:
            v = float(x)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return Pct(0.0, PCT_DIRTY, repr(x)[:PCT_RAW_LIMIT])
    if not math.isfinite(v):
        return Pct(0.0, PCT_DIRTY, repr(x)[:PCT_RAW_LIMIT])
    return Pct(v, PCT_GIVEN)


# ── 字段级解析（坏了降级，不炸整轮）───────────────────────────────────
def _num(x: object) -> float | None:
    """价位解析：``None``/空/``N-A`` → None；非数、非有限、≤0、布尔 → None。

    价位字段（``stop_loss``/``take_profit``/``move_stop``/``risk_amount``）坏了按
    **「没给」**处理而不是炸穿整轮——一个字段脏掉不该让整个 agent 当天不交易。
    带 ``%`` 的字符串一律 None：价位写成百分数是另一种笔误，猜出来的止损位比没有
    止损位更危险（用户以为有保护）。
    """
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, str):
        s = x.strip().replace("％", "%")
        if s in ("", "N/A", "n/a") or s.endswith("%"):
            return None
        try:
            v = float(s)
        except ValueError:
            return None
    else:
        try:
            v = float(x)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    return v if math.isfinite(v) and v > 0 else None


def _fnum(x: object, default: float = 0.0) -> float:
    """宽松数字解析（``confidence`` 等辅助字段）：坏了按缺省，绝不炸穿整轮。

    与 ``_num`` 的区别有两条：允许 0/负数（置信度无正值语义），且**认百分号**
    ``"80%"`` → 0.8（与 ``parse_pct`` 同口径；这是本仓相对既有的收紧，既有实现
    对 ``"80%"`` 直接回落成 0.0，等于把模型明确表达的置信度读成了零）。
    """
    if isinstance(x, bool):
        return default
    if isinstance(x, str):
        s = x.strip().replace("％", "%")
        if s.endswith("%"):
            try:
                v = float(s[:-1]) / 100.0
            except ValueError:
                return default
        else:
            try:
                v = float(s)
            except ValueError:
                return default
    else:
        try:
            v = float(x)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
    return v if math.isfinite(v) else default


# ── 决策对象 ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Decision:
    """单条决策（类型化，缺省即「没表达」）。

    ``stop_loss``/``take_profit``/``move_stop`` 三个价位是 ``watch`` 的载荷，
    落到守护单时分别是绝对价止损、绝对价止盈、条件棘轮的触发价与目标价
    （``move_stop`` 是单值 = 零间隙棘轮，见 ``watch_map``）。
    """

    action: str
    code: str
    name: str = ""
    pct: Pct = field(default_factory=Pct)
    stop_loss: float | None = None
    take_profit: float | None = None
    move_stop: float | None = None
    invalidation: str = ""
    confidence: float = 0.0
    risk_amount: float | None = None
    reason: str = ""

    @property
    def is_watch(self) -> bool:
        return self.action == WATCH

    def buy_intent(self) -> tuple[float, str]:
        """买入可执行比例 → ``(比例, 状态)``。

        **没给比例 = 不可执行**（返回 0.0）：买入是「用多少额度」的声明，模型不说
        就是没说，按 100% 额度买是凭空放大。与卖出侧的 missing→清仓**方向相反**，
        因为「卖」本身就是方向（比例只是幅度），而「买」必须自己给出幅度。
        """
        if self.action != BUY:
            return 0.0, FRAC_ZERO
        if self.pct.is_dirty:
            return 0.0, FRAC_DIRTY
        if not self.pct.is_given:
            return 0.0, FRAC_MISSING
        if self.pct.value <= 0:
            return 0.0, FRAC_ZERO
        return min(self.pct.value, 1.0), FRAC_OK

    def sell_intent(self) -> tuple[float, str]:
        """卖出执行比例 → ``(比例, 状态)``。

        * 缺 pct（模型明说「卖出」、只是漏了比例）→ **按清仓**（1.0）：旧口径下
          ``min(max(pct,0),1)`` 在缺失时算出 0 股，打一行「无合法可卖量」就跳过，
          即「模型让卖、系统静默不卖」；
        * 脏 pct → **0.0 + dirty**，调用点**必须先停手留痕**再跳过。把「想减 30%」
          执行成清仓是不可逆的方向放大；宁可少卖（下一轮决策与守护单兜住）。
        """
        if self.action != SELL:
            return 0.0, FRAC_ZERO
        if self.pct.is_dirty:
            return 0.0, FRAC_DIRTY
        if not self.pct.is_given:
            return 1.0, FRAC_MISSING
        # `pct <= 0`（含模型写 -1 这类无意义负值）统一按「不动作」记 zero：
        # 与买入侧同形，调用点只需判 `<= 0 且状态非 missing` 即可跳过并留痕。
        # 原始数值仍在批次 JSON 里（`pct` 字段），不会因为归了 zero 就丢证据。
        if self.pct.value <= 0:
            return 0.0, FRAC_ZERO
        return min(self.pct.value, 1.0), FRAC_OK


# ── 批次 ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DecisionBatch:
    """一次解析的完整结果（成功与失败同构，调用方不必分叉两种返回类型）。"""

    status: str
    schema: str
    decisions: tuple[Decision, ...] = ()
    skipped_rows: int = 0
    ignored_actions: tuple[str, ...] = ()
    raw: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def failed(self) -> bool:
        """四种失败态之一（``ok`` 的反面，但读起来告诉你「这不是成功」而非「它坏了」）。"""
        return self.status in FAILURE_STATUSES

    @classmethod
    def api_failed(cls, schema: str) -> DecisionBatch:
        """LLM 调用失败（无输出）——与「有输出但解析不了」分开，排查方向不同。"""
        return cls(status=STATUS_API_FAILED, schema=schema)

    @classmethod
    def not_configured(cls, schema: str) -> DecisionBatch:
        """供应商未配置（缺 key/base 或落到了占位符）——别让运维去查模型输出格式。"""
        return cls(status=STATUS_NOT_CONFIGURED, schema=schema)

    def codes(self) -> tuple[str, ...]:
        """本轮涉及的标的（去重、保序）——执行段的在途/去重检查按它取数。"""
        seen: dict[str, None] = {}
        for d in self.decisions:
            if d.code:
                seen.setdefault(d.code, None)
        return tuple(seen)


def _rows(obj: dict, allowed: tuple[str, ...]) -> tuple[list[Decision], int, list[str]]:
    """一个候选 JSON 对象 → （决策行, 被跳过的行数, 被忽略的 action 去重）。

    只有 ``action`` 落在 schema 白名单内、且是 dict 的行才算决策；其余全部计入
    ``skipped_rows``（可见即留痕）。未知 action 的原值收进 ``ignored_actions``——
    模型说了个我们不认识的动作，这件事必须能查到，而不是只留一个数字。
    """
    out: list[Decision] = []
    skipped = 0
    ignored: list[str] = []
    rows = obj.get("decisions") if isinstance(obj, dict) else None
    for x in rows or []:
        if not isinstance(x, dict):
            skipped += 1
            continue
        action = str(x.get("action") or "").strip().lower()
        if action not in allowed:
            skipped += 1
            if action and action not in ignored:
                ignored.append(action)
            continue
        out.append(
            Decision(
                action=action,
                code=str(x.get("code") or "").strip(),
                name=str(x.get("name") or "").strip(),
                pct=parse_pct(x.get("pct")),
                stop_loss=_num(x.get("stop_loss")),
                take_profit=_num(x.get("take_profit")),
                move_stop=_num(x.get("move_stop")),
                invalidation=str(x.get("invalidation") or "").strip(),
                confidence=_fnum(x.get("confidence")),
                risk_amount=_num(x.get("risk_amount")),
                # 文本字段一律 strip。612 条真实输出差分下来这是**唯一**被语料命中的
                # 行为差异：既有实现不 strip，一条 `reason` 结尾多一个空格。去掉它
                # 只为让审计比对/去重/日志对齐拿到稳定串，正文一字未改。
                reason=str(x.get("reason") or "").strip(),
            )
        )
    return out, skipped, ignored


def _diagnostics(
    tried: list[tuple[list[Decision], int, list[str]]],
) -> tuple[int, tuple[str, ...]]:
    """全部候选块都不合格时，取**信息量最大**的那个块的丢弃统计。

    为什么不能直接返回空：``parse_failed`` 只说明「取不出决策」，而重试往往原样
    重演。要区分「模型压根没吐 JSON」（skipped=0）与「吐了 3 行、action 全不认识」
    （skipped=3 + `ignored_actions`）——后者是提示词/schema 串了，重试解决不了。
    取值口径：行数最多者；并列取靠后的（更接近模型最终想说的那个块）。
    """
    best: tuple[list[Decision], int, list[str]] | None = None
    for entry in tried:
        if best is None or len(entry[0]) + entry[1] >= len(best[0]) + best[1]:
            best = entry
    if best is None:
        return 0, ()
    return best[1], tuple(best[2])


def parse_decisions(
    text: str | None,
    *,
    schema: str = SCHEMA_INTRADAY,
    raw_limit: int = RAW_LIMIT,
) -> DecisionBatch:
    """LLM 输出文本 → :class:`DecisionBatch`（**不抛异常**，失败走三态 status）。

    ``schema`` 决定 action 白名单（见 ``SCHEMAS``）；拼错 schema 名直接 ``ValueError``
    ——静默按最宽的白名单跑会放行本来不该出现在这一轮的 ``watch``。

    **空 decisions 不是成功**：``{"decisions": []}`` 或「全是未知 action」的块会被
    校验器拒掉，继续往后找下一个 JSON 块；找不到才算 ``parse_failed``。否则模型吐
    一句「暂无操作」就会被当成「本轮无决策」静默跳过——事后无人解释得清
    「今天为什么零交易」。
    """
    if schema not in SCHEMAS:
        raise ValueError(f"未知 schema={schema!r}（合法值：{sorted(SCHEMAS)}）")
    allowed = SCHEMAS[schema]

    if text is None or not text.strip():
        # 空串是 empty_output 而不是 parse_failed：要查的是上游/限流，不是提示词
        return DecisionBatch(status=STATUS_EMPTY_OUTPUT, schema=schema, raw="")

    # 每个候选块只算一次：通过校验时 `tried[-1]` 就是**被采纳的那个块**；全部失败
    # 时用它给「为什么失败」留下证据（见下 `_diagnostics`）。
    tried: list[tuple[list[Decision], int, list[str]]] = []

    def _want(obj: dict) -> bool:
        rows, skipped, ignored = _rows(obj, allowed)
        tried.append((rows, skipped, ignored))
        return bool(rows)

    obj = extract_json(text, want=_want)
    if obj is None or not tried:
        skipped, ignored = _diagnostics(tried)
        return DecisionBatch(
            status=STATUS_PARSE_FAILED,
            schema=schema,
            skipped_rows=skipped,
            ignored_actions=ignored,
            raw=text[:raw_limit],
        )

    rows, skipped, ignored = tried[-1]
    return DecisionBatch(
        status=STATUS_OK,
        schema=schema,
        decisions=tuple(rows),
        skipped_rows=skipped,
        ignored_actions=tuple(ignored),
        raw=text[:raw_limit],
    )


def batch_to_json(batch: DecisionBatch) -> str:
    """批次 → 可入库的 JSON 字符串（审计表与事件留痕共用一份序列化）。"""
    return json.dumps(
        {
            "status": batch.status,
            "schema": batch.schema,
            "skipped_rows": batch.skipped_rows,
            "ignored_actions": list(batch.ignored_actions),
            "decisions": [
                {
                    "action": d.action,
                    "code": d.code,
                    "name": d.name,
                    "pct": d.pct.value,
                    "pct_state": d.pct.state,
                    "pct_raw": d.pct.raw,
                    "stop_loss": d.stop_loss,
                    "take_profit": d.take_profit,
                    "move_stop": d.move_stop,
                    "invalidation": d.invalidation,
                    "confidence": d.confidence,
                    "risk_amount": d.risk_amount,
                    "reason": d.reason,
                }
                for d in batch.decisions
            ],
        },
        ensure_ascii=False,
    )
