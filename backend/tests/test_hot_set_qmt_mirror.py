"""QMT 热集镜像（db5）单测：原子替换、空集删除、失败不抛。"""

from __future__ import annotations

from backend.services.live_trading.services.hot_set_builder import (
    mirror_hot_set_to_qmt,
)


class _Pipe:
    def __init__(self, ops: list[tuple]) -> None:
        self.ops = ops

    def sadd(self, *args) -> None:
        self.ops.append(("sadd", args))

    def rename(self, *args) -> None:
        self.ops.append(("rename", args))

    def delete(self, *args) -> None:
        self.ops.append(("delete", args))

    def expire(self, *args) -> None:
        self.ops.append(("expire", args))

    def execute(self) -> list:
        self.ops.append(("execute", ()))
        return []


class _Client:
    def __init__(self, ops: list[tuple]) -> None:
        self.ops = ops

    def pipeline(self, transaction: bool = True) -> _Pipe:
        assert transaction is True
        return _Pipe(self.ops)

    def close(self) -> None:
        self.ops.append(("close", ()))


def test_mirror_writes_atomic_set_and_ttl():
    ops: list[tuple] = []
    n = mirror_hot_set_to_qmt(
        ["600036.SH", "000001.SZ"],
        client_factory=lambda: _Client(ops),
        key="qm:qmt:hot_set:test",
        ttl_s=123,
    )
    assert n == 2
    sadd = [op for op in ops if op[0] == "sadd"]
    assert len(sadd) == 1
    tmp_key = sadd[0][1][0]
    assert tmp_key.startswith("qm:qmt:hot_set:test:tmp:")
    assert sadd[0][1][1:] == ("600036.SH", "000001.SZ")
    assert any(op[0] == "rename" and op[1][1] == "qm:qmt:hot_set:test" for op in ops)
    assert ("expire", ("qm:qmt:hot_set:test", 123)) in ops
    assert ops[-1][0] == "close"


def test_mirror_empty_deletes_target():
    ops: list[tuple] = []
    n = mirror_hot_set_to_qmt([], client_factory=lambda: _Client(ops), key="k")
    assert n == 0
    assert ("delete", ("k",)) in ops
    assert not any(op[0] == "rename" for op in ops)


def test_mirror_failure_returns_zero_not_raise():
    def _boom():
        raise RuntimeError("redis down")

    assert mirror_hot_set_to_qmt(["600036.SH"], client_factory=_boom) == 0
