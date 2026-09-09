"""大 QMT 桥运维脚本单测：链路自检 + Windows 开箱包生成（不进真网络）。

自检脚本的价值全在「断在哪一层」的判定与提示，这里逐层用假客户端/假 Redis 锁死；
开箱包的价值在「文件齐全 + 配置模板与服务端读取的键一致」，同样锁死。
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
import redis as redis_lib

from backend.scripts import export_qmt_bridge_kit as kit
from backend.scripts import qmt_bridge_selftest as selftest
from backend.services.live_trading.services import qmt_exec_client as client_mod


@pytest.fixture(autouse=True)
def _reset_selftest_state(monkeypatch: pytest.MonkeyPatch) -> None:
    selftest._RESULTS.clear()
    selftest._CFG.clear()
    FakeRedis.fail = None
    FakeRedis.llen_error = None
    FakeRedis.depth = 0
    # 默认把「业务 Redis 回落」隔离掉，避免读到跑测试这台机器的环境
    for name in ("REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


class FakeClient:
    """最小 QmtExecClient 替身：只实现自检用到的只读方法。"""

    def __init__(
        self,
        *,
        cfg: dict[str, Any],
        ping_error: Exception | None = None,
        ping_payload: dict[str, Any] | None = None,
        asset: dict[str, Any] | None = None,
        positions: list[dict[str, Any]] | None = None,
        orders: list[dict[str, Any]] | None = None,
        trades: list[dict[str, Any]] | None = None,
    ):
        self._cfg = cfg
        self._ping_error = ping_error
        # 服务端应答体；默认与本地配置一致（即「一切正常」）
        self._ping_payload = (
            ping_payload
            if ping_payload is not None
            else {
                "pong": True,
                "account_id": cfg.get("account_id", ""),
                "account_type": cfg.get("account_type", "STOCK"),
                "allow_order_methods": True,
            }
        )
        self._asset = (
            asset if asset is not None else {"total_asset": 123456.78, "cash": 5000.0}
        )
        self._positions = positions or []
        self._orders = orders or []
        self._trades = trades or []

    async def refresh_settings(self) -> dict[str, Any]:
        return self._cfg

    def effective_config(self) -> dict[str, Any]:
        return dict(self._cfg)

    async def ping(self) -> dict[str, Any]:
        if self._ping_error:
            raise self._ping_error
        return {
            "ok": True,
            "result": self._ping_payload,
            "account_id": self._cfg.get("account_id", ""),  # 客户端回显
        }

    async def get_asset(self) -> dict[str, Any]:
        return self._asset

    async def get_positions(self) -> list[dict[str, Any]]:
        return self._positions

    async def query_orders(self, cancelable_only: bool = False) -> list[dict[str, Any]]:
        return self._orders

    async def query_trades(self) -> list[dict[str, Any]]:
        return self._trades


def _cfg(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "enabled": True,
        "account_id": "1234567890",
        "account_type": "STOCK",
        "timeout": 2,
        "strategy_name": "quantmind",
        "redis_host": "192.168.31.20",
        "redis_port": "6380",
        "redis_db": "0",
        "redis_password": "s3cret",
    }
    base.update(overrides)
    return base


class FakeRedis:
    """只实现 PING 与 LLEN 的假客户端。"""

    depth = 0
    fail: Exception | None = None
    llen_error: Exception | None = None
    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        FakeRedis.last_kwargs = kwargs

    def ping(self) -> bool:
        if FakeRedis.fail:
            raise FakeRedis.fail
        return True

    def llen(self, key: str) -> int:
        if FakeRedis.llen_error:
            raise FakeRedis.llen_error
        return FakeRedis.depth


# -- 配置层 ---------------------------------------------------------------


class TestConfigLayer:
    def test_redis_params_falls_back_to_business_redis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REDIS_HOST", "redis")
        monkeypatch.setenv("REDIS_PORT", "6379")
        monkeypatch.setenv("REDIS_PASSWORD", "bizpw")
        params, note = selftest._redis_params(_cfg(redis_host=""))
        assert params == {"host": "redis", "port": 6379, "db": 0, "password": "bizpw"}
        assert "回落" in note

    def test_redis_params_prefers_page_config(self) -> None:
        params, note = selftest._redis_params(_cfg(redis_port="6381", redis_db="3"))
        assert params == {
            "host": "192.168.31.20",
            "port": 6381,
            "db": 3,
            "password": "s3cret",
        }
        assert note == ""

    def test_disabled_short_circuits(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert selftest._check_config(_cfg(enabled=False)) is False
        out = capsys.readouterr().out
        assert "enabled=False" in out
        assert "enabled 选 true" in out

    def test_missing_account_short_circuits(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert selftest._check_config(_cfg(account_id="")) is False
        assert "account_id" in capsys.readouterr().out

    def test_account_and_password_masked(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert selftest._check_config(_cfg()) is True
        out = capsys.readouterr().out
        assert "1234567890" not in out
        assert "12***90" in out
        assert "s3cret" not in out

    def test_safe_redacts_redis_password(self) -> None:
        selftest._CFG.update(_cfg())
        assert "s3cret" not in selftest._safe("boom redis://:s3cret@host:6380/0")


# -- 网络层 ---------------------------------------------------------------


class TestNetworkLayer:
    def test_success_reports_queue_depth(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        FakeRedis.fail = None
        FakeRedis.depth = 0
        monkeypatch.setattr(redis_lib, "Redis", FakeRedis)
        assert selftest._check_redis(_cfg()) is True
        assert "队列深度=0" in capsys.readouterr().out

    def test_backlog_hint(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        FakeRedis.fail = None
        FakeRedis.depth = 7
        monkeypatch.setattr(redis_lib, "Redis", FakeRedis)
        assert selftest._check_redis(_cfg()) is True
        assert "队列有积压" in capsys.readouterr().out

    def test_unreachable_returns_false_with_hint(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        FakeRedis.fail = OSError("Error 111 connecting to 192.168.31.20:6380")
        monkeypatch.setattr(redis_lib, "Redis", FakeRedis)
        assert selftest._check_redis(_cfg()) is False
        out = capsys.readouterr().out
        assert "访问失败" in out
        assert "防火墙" in out

    def test_llen_failure_is_network_layer_failure(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """PING 通但 LLEN 被 ACL 拦（NOPERM）也算网络层没过，不能崩。"""
        FakeRedis.llen_error = Exception("NOPERM this user has no permissions")
        monkeypatch.setattr(redis_lib, "Redis", FakeRedis)
        assert selftest._check_redis(_cfg()) is False
        out = capsys.readouterr().out
        assert "NOPERM" in out


# -- 监听层 ---------------------------------------------------------------


class TestRpcLayer:
    """监听层的判定必须以**服务端上报**为准（客户端回显证明不了对面是谁）。"""

    def _check(
        self,
        capsys: pytest.CaptureFixture[str],
        client: FakeClient,
        cfg: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        selftest._CFG.update(cfg or _cfg())
        ok = selftest._check_rpc(client)
        return ok, capsys.readouterr().out

    def test_healthy_server_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        ok, out = self._check(capsys, FakeClient(cfg=_cfg()))
        assert ok is True
        assert "12***90" in out and "STOCK" in out

    def test_account_mismatch_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        """页面账号与服务端 BIGQMT_ACCOUNT_ID 不一致 → 查的一定不是这个账号。"""
        client = FakeClient(
            cfg=_cfg(),
            ping_payload={
                "account_id": "9876543210",
                "account_type": "STOCK",
                "allow_order_methods": True,
            },
        )
        ok, out = self._check(capsys, client)
        assert ok is False
        assert "不一致" in out
        assert "BIGQMT_ACCOUNT_ID" in out
        assert "9876543210" not in out  # 账号必须脱敏

    def test_account_type_mismatch_fails(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """信用账户填成 STOCK 会静默返回「资产全 0」→ 这里必须判失败。"""
        client = FakeClient(
            cfg=_cfg(),
            ping_payload={
                "account_id": "1234567890",
                "account_type": "CREDIT",
                "allow_order_methods": True,
            },
        )
        ok, out = self._check(capsys, client)
        assert ok is False
        assert "account_type=STOCK" in out
        assert "资产全 0" in out

    def test_order_disabled_warns_but_passes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = FakeClient(
            cfg=_cfg(),
            ping_payload={
                "account_id": "1234567890",
                "account_type": "STOCK",
                "allow_order_methods": False,
            },
        )
        ok, out = self._check(capsys, client)
        assert ok is True
        assert "下单开关=False" in out
        assert "只影响下单" in out

    def test_missing_server_fields_tolerated(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """老版本/非 redis 传输的服务端可能不回报这些字段 → 不因此判失败。"""
        ok, out = self._check(capsys, FakeClient(cfg=_cfg(), ping_payload={}))
        assert ok is True
        assert "(未上报)" in out


# -- 主流程 ---------------------------------------------------------------


class TestMain:
    def _run(
        self, monkeypatch: pytest.MonkeyPatch, client: FakeClient, *argv: str
    ) -> int:
        # main() 里是「函数内 import」，所以要打在源模块的属性上
        monkeypatch.setattr(client_mod, "get_qmt_exec_client", lambda: client)
        monkeypatch.setattr(redis_lib, "Redis", FakeRedis)  # 网络层走真实代码路径
        monkeypatch.setattr(sys, "argv", ["qmt_bridge_selftest.py", *argv])
        return selftest.main()

    def test_all_layers_pass(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = self._run(
            monkeypatch, FakeClient(cfg=_cfg(), positions=[{"stock_code": "600519.SH"}])
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "5/5 层通过" in out
        assert "链路可用" in out

    def test_unconfigured_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = self._run(monkeypatch, FakeClient(cfg=_cfg(enabled=False)))
        assert code == 2
        assert "1.配置层" in capsys.readouterr().out

    def test_rpc_timeout_skips_deeper_layers(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from backend.services.live_trading.services.qmt_exec_client import QmtExecError

        client = FakeClient(cfg=_cfg(), ping_error=QmtExecError("超时", code="TIMEOUT"))
        code = self._run(monkeypatch, client)
        out = capsys.readouterr().out
        assert code == 1
        assert "3.监听层" in out and "TIMEOUT" in out
        assert "4.账号层" not in out  # 监听层没过就不再往下打
        assert "1 层失败" in out

    def test_network_failure_marks_rest_skipped(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            client_mod, "get_qmt_exec_client", lambda: FakeClient(cfg=_cfg())
        )
        monkeypatch.setattr(redis_lib, "Redis", FakeRedis)
        FakeRedis.fail = OSError("connection refused")
        monkeypatch.setattr(sys, "argv", ["qmt_bridge_selftest.py"])
        code = selftest.main()
        out = capsys.readouterr().out
        assert code == 1
        assert out.count("[SKIP]") == 3
        assert "3 层跳过" in out

    def test_json_output_is_parseable(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._run(monkeypatch, FakeClient(cfg=_cfg()), "--json")
        out = capsys.readouterr().out
        start = out.index("\n[\n")  # JSON 段落从第一个独立行 '[' 开始
        rows = json.loads(out[start + 1 :])
        assert {row["step"] for row in rows} >= {"1.配置层", "2.网络层", "3.监听层"}

    def test_state_reset_between_calls(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """同进程跑两次不能累积上一轮结果（否则分母会变 10）。"""
        client = FakeClient(cfg=_cfg())
        assert self._run(monkeypatch, client) == 0
        assert self._run(monkeypatch, client) == 0
        assert capsys.readouterr().out.count("5/5 层通过") == 2


# -- 开箱包 ---------------------------------------------------------------

# 字面期望：故意不复用 kit._REQUIRED/_OPTIONAL，删了条目这里就会红
_KIT_FILES = (
    "bigqmt_signal_trader_strategy.py",
    "bigqmt_signal_trader_redis_rpc_runtime.py",
    "BIGQMT_REDIS_DRYRUN.py",
    "BIGQMT_ZMQ_DRYRUN.py",
)


def _fake_source(tmp_path: Path) -> Path:
    """造一个假的 site-packages：包 + 三个必需文件 + 一个可选文件。"""
    source = tmp_path / "site-packages"
    package = source / "bigqmt_signal_trader"
    (package / "__pycache__").mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("VERSION = '0'\n", encoding="utf-8")
    (package / "redis_rpc.py").write_text("# rpc\n", encoding="utf-8")
    (package / "__pycache__" / "redis_rpc.cpython-310.pyc").write_bytes(b"\x00")
    for name in _KIT_FILES:
        (source / name).write_text(f"# {name}\n", encoding="utf-8")
    return source


class TestKitTemplate:
    def test_template_compiles(self) -> None:
        compile(kit._CONFIG_TEMPLATE, "bigqmt_signal_trader_local_config.py", "exec")

    def test_template_keys_match_server_reads(self) -> None:
        """服务端只读这几个全局名，模板写错就是静默不生效。"""
        namespace: dict[str, Any] = {}
        exec(compile(kit._CONFIG_TEMPLATE, "cfg", "exec"), namespace)
        assert namespace["BIGQMT_ACCOUNT_ID"]
        # 信用账户填错会静默「资产全 0」→ 模板必须带这个键且默认 STOCK
        assert namespace["BIGQMT_ACCOUNT_TYPE"] == "STOCK"
        config = namespace["BIGQMT_REDIS_CONFIG"]
        assert config["rpc_allow_order_methods"] is False  # 默认不下单
        assert {"host", "port", "db", "password"} <= set(config)

    def test_write_text_is_windows_friendly(self, tmp_path: Path) -> None:
        target = tmp_path / "步骤.txt"
        kit._write_text(target, "第一行\n第二行\n")
        raw = target.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
        assert raw.count(b"\r\n") == 2  # 两行都转成了 CRLF
        assert b"\n" not in raw.replace(b"\r\n", b"")  # 没有裸 LF


class TestKitGeneration:
    def _run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str) -> int:
        monkeypatch.setattr(kit, "_site_packages", lambda: _fake_source(tmp_path))
        monkeypatch.setattr(kit.metadata, "version", lambda _name: "9.9.9")
        monkeypatch.setattr(sys, "argv", ["export_qmt_bridge_kit.py", *argv])
        return kit.main()

    def test_kit_contains_required_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        out = tmp_path / "kit"
        assert self._run(monkeypatch, tmp_path, "--out", str(out)) == 0
        for name in ("bigqmt_signal_trader", *_KIT_FILES):
            assert (out / name).exists(), name
        assert not list(out.rglob("__pycache__"))
        assert not list(out.rglob("*.pyc"))
        manifest = json.loads((out / "kit_manifest.json").read_text(encoding="utf-8"))
        assert manifest["big_convert_version"] == "9.9.9"
        assert manifest["file_count"] == len([p for p in out.rglob("*") if p.is_file()])

    def test_zip_has_top_level_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        out = tmp_path / "kit"
        self._run(monkeypatch, tmp_path, "--out", str(out))
        with zipfile.ZipFile(tmp_path / "kit.zip") as archive:
            assert {n.split("/")[0] for n in archive.namelist()} == {"kit"}

    def test_existing_config_not_overwritten_without_force(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        out = tmp_path / "kit"
        self._run(monkeypatch, tmp_path, "--out", str(out))
        config = out / "bigqmt_signal_trader_local_config.py"
        config.write_text("BIGQMT_ACCOUNT_ID = 'user-edited'\n", encoding="utf-8")
        self._run(monkeypatch, tmp_path, "--out", str(out))
        assert "user-edited" in config.read_text(encoding="utf-8")
        self._run(monkeypatch, tmp_path, "--out", str(out), "--force")
        assert "user-edited" not in config.read_text(encoding="utf-8")

    def test_no_zip_flag(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        out = tmp_path / "kit"
        self._run(monkeypatch, tmp_path, "--out", str(out), "--no-zip")
        assert not (tmp_path / "kit.zip").exists()

    def test_zip_warns_when_config_has_credentials(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        out = tmp_path / "kit"
        self._run(monkeypatch, tmp_path, "--out", str(out))
        capsys.readouterr()
        self._run(monkeypatch, tmp_path, "--out", str(out))  # 未改动 → 不告警
        assert "别外传" not in capsys.readouterr().out

        (out / "bigqmt_signal_trader_local_config.py").write_text(
            'BIGQMT_ACCOUNT_ID = "1234567890"\n', encoding="utf-8"
        )
        self._run(monkeypatch, tmp_path, "--out", str(out))
        assert "别外传" in capsys.readouterr().out
