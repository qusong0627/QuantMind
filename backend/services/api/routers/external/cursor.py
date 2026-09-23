"""对外数据面的增量水位：游标（`qmc1.*`）的编解码与拒绝面。

为什么单独成一个模块
--------------------
数据面里两类数据各有各的对账方式：分区型靠 `etag` 比对，表型靠**游标**。
`etag` 判错的代价是重下一遍；游标判错的代价是**静默少数据**——`decode`
成功但落在别的位置上，消费者照常翻页、照常收 `200`，只是永远差那么几天。
这类错没有异常、没有堆栈、没有可观测信号，所以在它身上花的代码量必须与
它的破坏力相称。

三条刻意的性质
--------------
1. **编与解严格对称**：`encode_cursor` 绝不产出 `decode_cursor` 会拒绝的东西。
   一旦不对称，我们自己的水位会在下一轮被自己拒掉，现象是「同步一直在全量
   重来」，而没人会去怀疑游标。
2. **不签名**。游标不是安全边界——它是消费者自己提供、自己承担的定位信息。
   伪造游标最多让自己拿到错的数据，拿不到权限外的数据。给它签名只会让人
   误以为它是凭证（`qmx1.*` 才是凭证，前缀刻意不同，见下）。
3. **报错只带数据集名，不带游标原文**。游标可以很长，截断的 base64 对排查
   毫无帮助；有用的是「哪个数据集、哪一类错」。

载荷里为什么一个数字都没有
--------------------------
``{"d": 数据集, "t": ISO-8601 字符串, "k": [字符串…], "v": 版本}``

`t` **不用** epoch 秒（float），`k` **不用**整数。两个都是被推翻的第一版设计：

* **float 时间戳会永久跳行。** 在 1.79e9 附近 double 的分辨率约 0.24µs，
  时间戳往返一次可能偏大 0.5µs。而同一批 upsert（同一个 `NOW()`）写下的行
  `updated_at` 完全相同，靠后的那些要靠 `k` 兜底；`t` 一旦偏大，它们就被
  判成「不大于游标」——**再也不会被发出**，且查询一切正常。
* **整数兜底键覆盖不到任何一张有值的表。** 本仓待同步的几张表主键是 text
  （`run_id`/`batch_id`）或复合（`(model_id, trade_date)`）；唯一有 `bigserial id`
  的 `engine_signal_scores` 恰好**没有 `updated_at`**，进不了行级游标。

所以两者都走**字符串**：JSON 对字符串是无损的，日期/数字/文本列在 PG 侧
本就有确定的全序，比较整个发生在数据库里。代价是每个数据集要声明自己那几列
的类型，好让 SQL 把参数 cast 回去——这个声明在数据集注册表里，不在这里。

前缀为什么是 `qmc1` 而不是共用 `qmx1`
--------------------------------------------
机器令牌是 `qmx1.<载荷>.<hmac>`，游标是 `qmc1.<载荷>`。两种 `qm?1.` 形状很像，
很容易被互相当成对方传；`c`/`x` 一字之差让人和日志都能一眼分辨，而认错的
那一侧必然报错（不是静默通过），这是有意的防呆。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from backend.shared.utc_datetime import UTC, to_utc_iso

__all__ = [
    "CURSOR_PREFIX",
    "CURSOR_VERSION",
    "CursorPosition",
    "InvalidCursor",
    "decode_cursor",
    "encode_cursor",
]

#: 游标前缀（含格式版本）。与机器令牌的 `qmx1.` 刻意区分，见模块 docstring。
CURSOR_PREFIX = "qmc1"

#: 载荷内的版本字段。前缀说明「形状」，这个字段才是**权威**：将来改格式时
#: 换前缀 + 抬版本，老服务端遇到新载荷靠它判定，不靠猜着解。
CURSOR_VERSION = 1

#: base64url 字母表（`-`/`_` 是 urlsafe 的两个替换字符，`.` 不在其中，
#: 所以合法游标必然只有一个分隔点）。**必须显式校验**：`binascii.a2b_base64`
#: 默认会**丢弃**落在字母表外的字符，于是 `"a!b"` 与 `"ab"` 解出同一个东西
#: ——畸形输入被悄悄「修正」成一个看起来合理的位置。
_BODY_RE = re.compile(r"[A-Za-z0-9_-]+")

#: 载荷长度上限（字符）。正常游标约 100–300 字符，这里留了数倍余量；
#: 拦住的是「用一条几 MB 的查询参数让网关做 base64+JSON 解析」这种廉价消耗。
_MAX_BODY_LEN = 2048

#: 时间戳必须**自带时区**。编码端永远产出 `Z`，所以不带时区的载荷不是本接口
#: 产出的；按本仓「无时区一律视为 UTC」的约定去猜，就是「按猜出来的位置读数据」，
#: 宁可 400。同时它顺带挡掉 `nan` / `Infinity`（JSON 的合法字面量，
#: `(t, k) > (NaN, k)` 恒假 → 消费者永远收到「没有更多变化」）。
_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})"
)

#: 兜底键的组件数上限。复合主键目前最多两列，留到四是为了不必改格式。
MAX_KEY_COMPONENTS = 4

#: 单个兜底键组件的长度上限（字符）。键会进 SQL 参数与日志。
MAX_KEY_COMPONENT_LEN = 128


class InvalidCursor(ValueError):
    """游标不可用：格式错 / 版本不认 / 不属于该数据集 / 值非法。

    端点把它翻成 `400 invalid_cursor`。客户端该做的是**丢弃游标、全量重来**，
    而不是重试同一个游标。
    """


@dataclass(frozen=True)
class CursorPosition:
    """游标的语义内容：在 `dataset` 里已经消费到的位置（**含**该行）。

    消费方的查询一律是严格大于、且用**同一个元组**：

        WHERE (updated_at, k0, k1, …) > (:t, :k0, :k1, …)
        ORDER BY updated_at, k0, k1, …

    用 `>=` 会让消费者卡在同一行上无限循环；只按时间戳翻页、不用兜底键，
    会在同一时间戳有多行时漏行（一次 upsert 写几千行在同一时刻是常态）。
    """

    dataset: str
    updated_at: datetime
    key: tuple[str, ...]


def _reject(dataset: str, reason: str) -> InvalidCursor:
    """构造异常。只带数据集名与原因，不带游标原文（见模块 docstring）。"""
    return InvalidCursor(f"游标不可用（数据集 {dataset}）：{reason}")


def _require_name(dataset: object) -> str:
    """数据集名必须是非空字符串。传进来的都是我们自己的常量，错了就是代码 bug。"""
    if not isinstance(dataset, str) or not dataset:
        raise InvalidCursor("游标不可用：数据集名必须是非空字符串")
    return dataset


def _coerce_timestamp(value: object, dataset: str) -> datetime:
    """任意 datetime → aware UTC。naive 按 UTC 解释（全仓唯一口径）。

    **`None` 必须当场拒，不能交给 `as_utc`**：那个函数对 None 回落 `utc_now()`，
    于是「传了个空值」会变成「水位是现在」——消费者从此永久收不到任何变化，
    而且看起来完全正常。
    """
    if not isinstance(value, datetime):
        raise _reject(dataset, "时间戳缺失或不是 datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_timestamp(value: object, dataset: str) -> datetime:
    """载荷里的 ISO-8601 字符串 → aware UTC。任何不确定的形状都拒。"""
    if not isinstance(value, str):
        raise _reject(dataset, "时间戳缺失或不是字符串")
    if not _TS_RE.fullmatch(value):
        raise _reject(dataset, "时间戳不是带时区的 ISO-8601")
    # Python 3.10 的 fromisoformat 不认尾部的 Z（3.11 才认），先归一。
    normalized = value.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        # 形状对了但数值越界（13 月、99 时…）
        raise _reject(dataset, "时间戳数值越界") from None
    return parsed.astimezone(UTC)


def _coerce_key(value: object, dataset: str) -> tuple[str, ...]:
    """兜底键 → 字符串元组。

    `value` 既可能是 JSON 解出来的 list，也可能是调用方传的 tuple。
    **字符串一律拒**（`"abc"` 不是 `["abc"]`——这是最常见的写法错，而
    `"abc"` 按字符拆开会得到一个看起来很合理、实际完全错的位置）。
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise _reject(dataset, "兜底键缺失或不是数组")
    if len(value) > MAX_KEY_COMPONENTS:
        raise _reject(dataset, f"兜底键组件过多（上限 {MAX_KEY_COMPONENTS}）")

    parts: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise _reject(dataset, "兜底键组件不是字符串")
        if not item:
            # 空串进 SQL 会变成一个恒真的比较项，等于静默退化成「只按时间戳翻页」。
            raise _reject(dataset, "兜底键组件为空")
        if len(item) > MAX_KEY_COMPONENT_LEN:
            raise _reject(dataset, f"兜底键组件过长（上限 {MAX_KEY_COMPONENT_LEN}）")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in item):
            # 控制字符：进日志会伪造日志行 / 污染终端回显。
            raise _reject(dataset, "兜底键组件含控制字符")
        parts.append(item)
    return tuple(parts)


def encode_cursor(dataset: str, *, updated_at: datetime, key: Sequence[str]) -> str:
    """把位置编成对外可见的游标。

    不校验任何权限或存在性——它只是个编码器，数据集是否可读由端点自己判。

    与 `decode_cursor` **严格对称**：本函数产出的必能被解开；本函数拒绝的，
    `decode_cursor` 也拒绝。
    """
    name = _require_name(dataset)
    payload = {
        "d": name,
        "t": to_utc_iso(_coerce_timestamp(updated_at, name)),
        "k": list(_coerce_key(key, name)),
        "v": CURSOR_VERSION,
    }
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return f"{CURSOR_PREFIX}.{base64.urlsafe_b64encode(blob).rstrip(b'=').decode()}"


def decode_cursor(raw: str, *, dataset: str) -> CursorPosition:
    """解析游标，并核对它**属于** `dataset`。

    归属核对是这里的重点：静默接受别的数据集的游标，等于让消费者拿 A 的水位
    去读 B，结果是「少了几天数据、没有任何报错」（见模块 docstring）。
    拒掉它则退化成一次全量重来——慢，但正确。
    """
    name = _require_name(dataset)
    if not isinstance(raw, str):
        raise _reject(name, "游标不是字符串")

    prefix = f"{CURSOR_PREFIX}."
    if not raw.startswith(prefix):
        raise _reject(name, "前缀不认（不是本接口的游标，或来自别的版本）")
    body = raw[len(prefix) :]
    if len(body) > _MAX_BODY_LEN:
        raise _reject(name, "载荷过长")
    if not _BODY_RE.fullmatch(body):
        raise _reject(name, "载荷不是合法的 base64url")

    # 编码端去掉了 `=` 填充（URL 里更干净），这里补回来。
    padded = body + "=" * (-len(body) % 4)
    try:
        # validate=True 是重点：不允许解码器「丢弃非法字符后继续」。
        blob = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(blob)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise _reject(name, "载荷解不开（base64 或 JSON 坏了）") from None

    if not isinstance(payload, dict):
        raise _reject(name, "载荷不是 JSON 对象")

    version = payload.get("v")
    # 类型先判，别只比值：`True == 1` 且 `1.0 == 1`，两种都会绕过相等比较
    # ——`"v": true` / `"v": 1.0` 都不是本接口产出的，放过它们就是「猜着解」。
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != CURSOR_VERSION
    ):
        raise _reject(name, "版本不认（请丢弃游标，全量重来）")

    embedded = payload.get("d")
    if not isinstance(embedded, str) or not embedded:
        raise _reject(name, "载荷里没有数据集名")
    if embedded != name:
        raise _reject(name, f"这个游标属于数据集 {embedded}，不能用在这里")

    return CursorPosition(
        dataset=name,
        updated_at=_parse_timestamp(payload.get("t"), name),
        key=_coerce_key(payload.get("k"), name),
    )
