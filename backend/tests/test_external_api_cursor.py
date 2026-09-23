"""对外数据面游标（`qmc1.*`）的编解码与拒绝面。

**游标是这套接口里最容易静默出错的一环**，因为它错起来不像错：

* 解析「成功」但落在别的数据集上 → 消费者拿 A 的水位读 B，
  结果是**少了几天数据、没有任何报错**；
* 时间戳精度不够 → 边界上同一时刻的兄弟行被比较成「不大于」，
  **永久收不到**（见下面 `test_microsecond_survives_exactly`）；
* 解析失败时回落到「从头开始」→ 消费者每轮全量重来，把限流打满，
  现象是「同步一直很慢」，没人会去看游标解析。

所以本文件测的不是「能编能解」，而是**拒绝面**：
每一种坏输入是否都落到「明确报错」而不是「静默地给出一个看起来合理的错位置」。

游标**不签名**（见设计文档 §4.1）：它不是安全边界，是消费者自己提供、
自己承担的定位信息。伪造游标最多让自己拿到错的数据，拿不到权限外的数据。
本文件因此也不测「防篡改」——那是刻意不做的性质。
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest

from backend.services.api.routers.external.cursor import (
    CURSOR_PREFIX,
    InvalidCursor,
    decode_cursor,
    encode_cursor,
)

#: 带微秒的固定时刻。**不要**换成整数秒——精度是这里唯一测得到的东西。
T0 = datetime(2026, 9, 23, 12, 34, 56, 123456, tzinfo=timezone.utc)


def _raw(payload: dict) -> str:
    """按游标格式手工拼一个（用来造各种坏输入，不走 encode 的校验）。"""
    body = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    return f"{CURSOR_PREFIX}.{body}"


def _ok(**overrides: object) -> dict:
    """一份合法载荷，按需覆盖字段——坏输入都从它派生，以免测试自己写错基准。"""
    payload: dict[str, object] = {
        "d": "d",
        "t": "2026-09-23T12:34:56.123456Z",
        "k": ["a", "b"],
        "v": 1,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 往返
# ---------------------------------------------------------------------------


def test_round_trip_preserves_position() -> None:
    raw = encode_cursor("signal_scores", updated_at=T0, key=("t1", "u1", "run-9"))
    pos = decode_cursor(raw, dataset="signal_scores")
    assert pos.updated_at == T0
    assert pos.key == ("t1", "u1", "run-9")


def test_microsecond_survives_exactly() -> None:
    """**这条是本文件最重要的断言**：时间戳必须逐微秒无损往返。

    曾经的设计把时间戳编码成 float（epoch 秒）。在 1.79e9 附近 double 的
    分辨率约 0.24µs，往返一次可能**偏大** 0.5µs。后果不是「差一点点」：

        WHERE (updated_at, k) > (t_decoded, k_stored)

    同一批 upsert（同一个 `NOW()`）写下的兄弟行，`updated_at` 完全相同，
    只有排在后头的要靠 `k` 兜底。`t_decoded` 一旦比真实值大，这些行就被
    判成「不大于游标」——**永远不会再被发出**，而且查询一切正常。

    所以这里不测「约等于」，测**完全相等**，并且专门挑一个 float 表示不了的
    微秒值（`.123456` 在 1.79e9 量级上落不到 double 格点上）。
    """
    for micro in (1, 123456, 999999):
        t = T0.replace(microsecond=micro)
        pos = decode_cursor(encode_cursor("d", updated_at=t, key=("x",)), dataset="d")
        assert pos.updated_at == t, f"微秒 {micro} 往返后漂了：{pos.updated_at!r}"


def test_cursor_is_opaque_and_prefixed() -> None:
    """对外可见的形状：`qmc1.<base64url>`，消费者不该从中读出结构。"""
    raw = encode_cursor("signal_scores", updated_at=T0, key=("1",))
    assert raw.startswith(f"{CURSOR_PREFIX}.")
    assert len(raw.split(".")) == 2


def test_key_components_are_carried_verbatim() -> None:
    """兜底键原样保存——它会被拼回 SQL 的元组比较，改一个字符就是错位置。"""
    key = ("000001.SZ", "2026-09-22", "9" * 60)
    pos = decode_cursor(encode_cursor("d", updated_at=T0, key=key), dataset="d")
    assert pos.key == key


def test_unicode_dataset_name_round_trips() -> None:
    pos = decode_cursor(
        encode_cursor("特征快照", updated_at=T0, key=("甲",)), dataset="特征快照"
    )
    assert pos.key == ("甲",)


def test_empty_key_is_allowed() -> None:
    """数据集可以声明「`updated_at` 本身就唯一」，此时兜底键为空。"""
    assert (
        decode_cursor(encode_cursor("d", updated_at=T0, key=()), dataset="d").key == ()
    )


# ---------------------------------------------------------------------------
# 归属：游标只能用在自己那个数据集上
# ---------------------------------------------------------------------------


def test_cursor_from_another_dataset_is_rejected() -> None:
    """**核心断言**。静默接受等于让消费者拿 A 的水位读 B。

    这类错误的表现是「少了几天数据但一切正常」，是最贵的一种错——
    所以宁可直接 400 让消费者全量重来。
    """
    raw = encode_cursor("signal_scores", updated_at=T0, key=("1",))
    with pytest.raises(InvalidCursor):
        decode_cursor(raw, dataset="news")


def test_same_dataset_still_accepted() -> None:
    """反向对照：别把归属校验写成「永远拒绝」。"""
    raw = encode_cursor("news", updated_at=T0, key=("1",))
    assert decode_cursor(raw, dataset="news").key == ("1",)


# ---------------------------------------------------------------------------
# 版本与形状
# ---------------------------------------------------------------------------


def test_unknown_version_is_rejected() -> None:
    """版本不认时必须报错，不能按当前格式猜着解——猜错就是错位置。"""
    with pytest.raises(InvalidCursor):
        decode_cursor(_raw(_ok(v=99)), dataset="d")
    # `true == 1`，漏掉 bool 判定的话 `"v": true` 会被当成版本 1
    with pytest.raises(InvalidCursor):
        decode_cursor(_raw(_ok(v=True)), dataset="d")
    with pytest.raises(InvalidCursor):
        decode_cursor(_raw(_ok(v=1.0)), dataset="d")


def test_wrong_prefix_is_rejected() -> None:
    """尤其是**机器令牌**（`qmx1.*`）：两种 `qm*1.` 形状很容易被互相当成对方。"""
    token_like = "qmx1." + _raw(_ok()).split(".")[1]
    with pytest.raises(InvalidCursor):
        decode_cursor(token_like, dataset="d")


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "   ",
        "not-a-cursor",
        "qmc1",
        "qmc1.",
        "qmc1.a.b",
        "qmc1.!!!not-base64!!!",
        "qmc2.AAAA",
    ],
)
def test_malformed_cursors_are_rejected(garbage: str) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(garbage, dataset="d")


@pytest.mark.parametrize(
    "payload",
    [
        {},  # 空
        {"d": "d"},  # 缺字段
        {"d": "d", "t": "2026-09-23T12:34:56Z"},  # 缺 k
        {"d": "d", "k": ["a"], "v": 1},  # 缺 t
        {"t": "2026-09-23T12:34:56Z", "k": [], "v": 1},  # 缺数据集
        {"d": "", "t": "2026-09-23T12:34:56Z", "k": [], "v": 1},  # 数据集名为空
        {"d": "d", "t": 1758596400.5, "k": [], "v": 1},  # t 是数字
        {"d": "d", "t": None, "k": [], "v": 1},
    ],
)
def test_structurally_broken_payloads_are_rejected(payload: dict) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(_raw(payload), dataset="d")


@pytest.mark.parametrize(
    "bad_t",
    [
        "",
        "not-a-time",
        "2026-09-23",  # 只有日期，没有时刻
        "2026-09-23T12:34:56",  # naive：没带时区
        "2026-13-45T99:99:99Z",  # 月份/时刻越界
        "2026-09-23T12:34:56.123456+8:00",  # 偏移量写错（缺前导零）
        "nan",
        "Infinity",
    ],
)
def test_unusable_timestamps_are_rejected(bad_t: object) -> None:
    """**这条是防空转的关键**。

    解析不出来时必须报错。naive（不带时区）尤其要拒：本仓约定「无时区输入
    一律视为 UTC」，但在游标上「按约定猜」就是「按猜出来的位置读数据」——
    编码端永远带 `Z`，不带 `Z` 的载荷就不是本接口产出的，宁可 400。

    `nan`/`Infinity` 是 JSON 的合法字面量（Python 的 json 默认收），
    一旦漏进来，`(t, k) > (NaN, k)` 恒假——消费者永远收到「没有更多变化」，
    而且看起来完全正常（不是 500、不是超时，就是一个空列表）。
    """
    with pytest.raises(InvalidCursor):
        decode_cursor(_raw(_ok(t=bad_t)), dataset="d")


@pytest.mark.parametrize("bad_updated_at", ["2026-09-23T12:34:56Z", None, 1758596400])
def test_encode_rejects_non_datetimes(bad_updated_at: object) -> None:
    """编与解必须对称：encode 也不能产出 decode 会拒的东西。"""
    with pytest.raises(InvalidCursor):
        encode_cursor("d", updated_at=bad_updated_at, key=())  # type: ignore[arg-type]


def test_encode_accepts_naive_by_interpreting_it_as_utc() -> None:
    """编码端是全仓唯一口径的入口，naive 按 UTC 解释（约定见 utc_datetime）。

    注意这里**不能**用 `as_utc(None)`——它对 None 回落 `utc_now()`，
    于是「传了个空值」会变成「水位是现在」：消费者从此**永久收不到任何变化**。
    None 必须当场拒（上一条），naive 才走约定。
    """
    naive = datetime(2026, 9, 23, 12, 34, 56, 123456)  # noqa: DTZ001 — naive 正是被测对象
    pos = decode_cursor(encode_cursor("d", updated_at=naive, key=()), dataset="d")
    assert pos.updated_at == T0


@pytest.mark.parametrize(
    "bad_key",
    [
        "abc",  # 字符串，不是数组——最常见的写法错
        None,
        {"a": 1},
        [1],  # 元素不是字符串
        [None],
        [""],  # 空串会成为 SQL 里一个恒真的比较项
        ["a", ""],
        ["a", 2],
        ["a"] * 5,  # 组件过多
        ["x" * 200],  # 单个组件过长
        ["a\x00b"],  # 控制字符：进日志会污染终端回显
        ["a\nb"],
    ],
)
def test_bad_keys_are_rejected(bad_key: object) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(_raw(_ok(k=bad_key)), dataset="d")


def test_payload_must_be_a_json_object() -> None:
    """合法 base64 但载荷不是对象（数组/字符串/数字）——同样要拒。"""
    for value in ([1, 2, 3], "d", 42, None):
        body = (
            base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
        )
        with pytest.raises(InvalidCursor):
            decode_cursor(f"{CURSOR_PREFIX}.{body}", dataset="d")


def test_extra_fields_are_ignored_not_fatal() -> None:
    """向前兼容：将来加字段时，老服务端不该把新游标当坏的。

    （反过来不成立：新服务端遇到**老格式**靠 `v` 判定，见上面那条。）
    """
    pos = decode_cursor(_raw(_ok(future="x")), dataset="d")
    assert pos.key == ("a", "b")


def test_error_message_names_no_internals() -> None:
    """异常信息会进日志；不要把整段游标（可能很大）或堆栈细节抛出去。

    这条不是安全边界（游标不是秘密），是为了日志可读：一条被截断的
    base64 对排查毫无帮助，有用的是「哪个数据集、哪一类错」。
    """
    with pytest.raises(InvalidCursor) as exc:
        decode_cursor("qmc1.zzz", dataset="signal_scores")
    msg = str(exc.value)
    assert "signal_scores" in msg
    assert "zzz" not in msg
