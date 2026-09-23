"""对外任务面（`external/task.py`）。

这一面存在的理由是**归一**，所以这个文件测的也主要是归一：
上游五种任务的状态/进度/入参形状各不相同，外部节点只该看到一种。

钉住四类东西：

1. **状态词表**——未知值必须落到 `unknown` 而不是被猜成某个近似值。
   猜错的代价是调用方对一个已失败的任务继续等。
2. **进度取的是哪个字段**——这里最容易静默出错：alpha-agent 的响应里
   `progress` 是**字符串**短语、`progress_pct` 才是 int；拿错了不报错，
   只是永远得到 0。TradingAgents 则**没有**百分比，必须是 None 而不是 0
   （0 的语义是「刚开始」，与「没有这个量」是两回事）。
3. **入参翻译**——对外一套市场词表，上游两套（同步码 `A/BC`、适配器
   `a_share/crypto`）。翻译表要与上游实际的注册表对得上，否则调用方会收到
   一个带着它被要求不要使用的词表的错误。
4. **如实缺席**——`data_sync` 没有作业 id 就说没有（`ref=null`），
   而不是编一个必然 404 的 id 出来。

上游一律用替身，本文件**不**产生任何真实提交：任务面每个端点都对应一个会
真的花掉 GPU / 拉起容器 / 打外部数据源的动作。
"""

from __future__ import annotations

import warnings

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.api.routers.external import auth as ext_auth
from backend.services.api.routers.external import router as router_module
from backend.services.api.routers.external import task as task_plane
from backend.shared import live_trading_gate as gate

SECRET = "task-test-external-secret-4c71"
ACCESS_KEY = "qm_live_tasktest0001"

#: 上游各种 id 的真实形状（`test_ref_shapes_survive_the_gate_pattern` 用）。
REF_TRAINING = "train_20260923143012_ab12cd34"
REF_BACKTEST = "3f9a1c0d5e7b24689a0b1c2d3e4f5a6b"
REF_ALPHA = "0b6d2f4a7c314e58"
REF_ANALYSIS = "a1b2c3d4"


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext_auth, "_read_secret_raw", lambda: SECRET)


# ---------------------------------------------------------------------------
# 鉴权接缝（与 test_external_api_permissions.py 同款：假的库，真的令牌）
# ---------------------------------------------------------------------------


class _FakeKey:
    def __init__(self) -> None:
        self.user_id = "10000001"
        self.tenant_id = "default"
        # 任务面与数据/控制面一样**没有**挂权限码（见 permissions.py 的说明），
        # 所以这里给空列表：它证明任务面的可达性不依赖任何权限字符串。
        self.permissions: list[str] = []
        self.is_active = True
        self.expires_at = None
        self.secret_hash = "$2b$12$" + "x" * 53


class _FakeResult:
    def scalar_one_or_none(self):
        return _FakeKey()


class _FakeSession:
    async def execute(self, *_a, **_kw):
        return _FakeResult()

    async def commit(self):
        pass


class _FakeCtx:
    async def __aenter__(self):
        return _FakeSession()

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import backend.shared.database_manager_v2 as db

    monkeypatch.setattr(db, "get_session", lambda **_kw: _FakeCtx())
    monkeypatch.setenv(gate.ENV_KEY, "false")
    app = FastAPI()
    app.include_router(router_module.router, prefix=gate.EXT_API)
    # 装上闸门：任务面的端点必须能在**默认部署**（实盘关闭）上到达。
    # 这是 `_ALLOWED_EXT_ENDPOINTS` 那套「未登记即拒绝」真正保护的性质，
    # 不装闸门就测不到它。
    gate.install_live_trading_gate_middleware(app, "test")
    return TestClient(app)


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    token, _ = ext_auth.mint_external_token(ACCESS_KEY)
    return {"Authorization": f"Bearer {token}"}


class UpstreamRecorder:
    """记录调用并返回预设响应；`model=` 时按真实 `fetch_json` 的语义返回模型实例。"""

    def __init__(self, responses: dict[tuple[str, str], object]):
        self.responses = responses
        self.calls: list[dict] = []

    async def __call__(self, service: str, method: str, path: str, **kw):
        self.calls.append({"service": service, "method": method, "path": path, **kw})
        key = (method, path)
        payload = self.responses.get(key)
        if payload is None:
            raise AssertionError(f"本用例没有为 {method} {path} 准备响应")
        if isinstance(payload, Exception):
            raise payload
        model = kw.get("model")
        if model is not None:
            return model.model_validate(payload)
        return payload

    def only_call(self) -> dict:
        assert len(self.calls) == 1, f"期望恰好一次上游调用，实际 {len(self.calls)}"
        return self.calls[0]


def _install(monkeypatch: pytest.MonkeyPatch, responses: dict) -> UpstreamRecorder:
    rec = UpstreamRecorder(responses)
    monkeypatch.setattr(task_plane, "fetch_json", rec)
    return rec


# ---------------------------------------------------------------------------
# 1. 状态词表
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "upstream,expected",
    [
        # 上游实际出现过的全部写法
        ("pending", task_plane.STATUS_QUEUED),
        ("provisioning", task_plane.STATUS_RUNNING),
        ("waiting_callback", task_plane.STATUS_RUNNING),
        ("running", task_plane.STATUS_RUNNING),
        ("completed", task_plane.STATUS_SUCCEEDED),
        ("failed", task_plane.STATUS_FAILED),
        ("cancelled", task_plane.STATUS_CANCELLED),
        # 大小写/空白
        ("  COMPLETED ", task_plane.STATUS_SUCCEEDED),
        # 没见过的一律 unknown
        ("some_new_state", task_plane.STATUS_UNKNOWN),
        ("", task_plane.STATUS_UNKNOWN),
        (None, task_plane.STATUS_UNKNOWN),
    ],
)
def test_status_vocabulary(upstream: object, expected: str) -> None:
    """未知状态落 `unknown`，**不猜**。"""
    assert task_plane.normalize_status(upstream) == expected


def test_unknown_status_is_not_guessed_into_a_terminal_state() -> None:
    """反面：一个没见过的状态不能被当成 `succeeded` 或 `failed`。

    这两者都会让调用方**停止轮询**。猜错方向的后果是不对称的：猜成功会让它
    拿着一个还没算完的结果走，猜失败会让它去重试一个其实还好好的任务。
    """
    got = task_plane.normalize_status("collecting_precomputed_factors")
    assert got == task_plane.STATUS_UNKNOWN
    assert got not in {
        task_plane.STATUS_SUCCEEDED,
        task_plane.STATUS_FAILED,
        task_plane.STATUS_CANCELLED,
    }


def test_every_declared_status_is_reachable() -> None:
    """声明的词表里不能有永远取不到的值。

    一个取不到的状态值意味着「文档/类型里承诺了一种状态，而实现永远不会
    返回它」——调用方会为它写一个永不到达的分支，并因此以为自己处理了
    `cancelled`（本版确实没有取消，但那个值仍可能来自上游的存量任务）。
    """
    reachable = {task_plane.normalize_status(k) for k in task_plane._STATUS_MAP}
    reachable.add(task_plane.STATUS_UNKNOWN)
    declared = {
        getattr(task_plane, name)
        for name in task_plane.__all__
        if name.startswith("STATUS_")
    }
    assert declared, "前提失效：__all__ 里一个状态常量都没有，本断言会空转"
    assert declared <= reachable, f"声明了但取不到：{declared - reachable}"


# ---------------------------------------------------------------------------
# 2. 进度：取哪个字段，以及「没有这个量」怎么表达
# ---------------------------------------------------------------------------


def test_clamp_pct_distinguishes_missing_from_zero() -> None:
    """`None` → `None`，**不是** 0。见模块 docstring：0 的语义是「刚开始」。

    `None == 0` 在 Python 里是 False，所以这一条本身也能抓住「写成
    `int(value or 0)`」的实现——那正是最容易顺手写出来的那种。
    """
    assert task_plane._clamp_pct(None) is None
    assert task_plane._clamp_pct(0) == 0
    assert task_plane._clamp_pct("0") == 0
    assert task_plane._clamp_pct("abc") is None
    assert task_plane._clamp_pct(150) == 100
    assert task_plane._clamp_pct(-5) == 0


def test_alpha_progress_takes_pct_field_not_the_string_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """**alpha-agent 的响应里有两个 `progress`**：`progress` 是给人看的字符串，
    `progress_pct` 才是 int 0-100。取错了不会抛异常，只会永远得到 0/None。

    这个用例把两个字段都放进去，断言取到的是数字那个。
    """
    _install(
        monkeypatch,
        {
            ("GET", f"/api/v1/alpha-agent/tasks/{REF_ALPHA}"): {
                "code": 200,
                "data": {
                    "task_id": REF_ALPHA,
                    "status": "running",
                    "progress": "第 3 轮演化中",  # 字符串陷阱
                    "progress_pct": 42,
                    "phase": "factor_mining",
                },
            }
        },
    )
    body = client.get(
        f"/api/ext/v1/task/alpha_evolve/{REF_ALPHA}", headers=auth_headers
    ).json()
    assert body["progress_pct"] == 42
    assert body["status"] == task_plane.STATUS_RUNNING
    assert body["stage"] == "factor_mining"


def test_trading_agents_progress_is_null_not_zero(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """TradingAgents **没有**百分比。必须是 null。

    特别地：不许拿 `completed_stages / 12` 凑一个出来——那个 12 是上游阶段表的
    长度，加一个阶段就过期，而它过期的方式是**悄悄地偏小**。
    """
    _install(
        monkeypatch,
        {
            ("GET", f"/api/v1/trading-agents/progress/{REF_ANALYSIS}"): {
                "code": 200,
                "data": {
                    "ticker": "600036",
                    "is_running": True,
                    "is_complete": False,
                    "current_stage": "debate",
                    "completed_stages": ["market", "social", "news"],
                    "stage_reports": {"market_report": "x" * 100000},
                },
            }
        },
    )
    body = client.get(
        f"/api/ext/v1/task/trading_agents/{REF_ANALYSIS}", headers=auth_headers
    ).json()
    assert body["progress_pct"] is None
    assert body["stage"] == "debate"
    assert body["stages_completed"] == 3
    assert body["status"] == task_plane.STATUS_RUNNING


@pytest.mark.parametrize(
    "payload,expected",
    [
        # 两个布尔推出状态（上游没有状态字段）
        ({"is_running": True, "is_complete": False}, task_plane.STATUS_RUNNING),
        ({"is_running": False, "is_complete": True}, task_plane.STATUS_SUCCEEDED),
        ({"is_running": False, "is_complete": False}, task_plane.STATUS_QUEUED),
        # error 压过一切：上游可能同时留着 is_complete 与 error
        (
            {"is_running": False, "is_complete": True, "error": "LLM 超时"},
            task_plane.STATUS_FAILED,
        ),
        # 只有空白的 error 不算错误
        (
            {"is_running": True, "is_complete": False, "error": "   "},
            task_plane.STATUS_RUNNING,
        ),
    ],
)
def test_trading_agents_status_derived_from_booleans(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict,
    payload: dict,
    expected: str,
) -> None:
    _install(
        monkeypatch,
        {
            ("GET", f"/api/v1/trading-agents/progress/{REF_ANALYSIS}"): {
                "code": 200,
                "data": payload,
            }
        },
    )
    body = client.get(
        f"/api/ext/v1/task/trading_agents/{REF_ANALYSIS}", headers=auth_headers
    ).json()
    assert body["status"] == expected


def test_training_error_is_read_from_result_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """训练的失败原因在 `result.error`，**不在顶层**。

    顶层只有 status/progress/logs/result/isCompleted。从顶层取 error 会永远
    得到 None——任务显示 failed 但 `error` 是空的，调用方无从知道为什么。
    """
    _install(
        monkeypatch,
        {
            ("GET", f"/api/v1/models/training-runs/{REF_TRAINING}"): {
                "runId": REF_TRAINING,
                "status": "failed",
                "progress": 40,
                "logs": "…" * 5000,
                "result": {"error": "OOM killed"},
                "isCompleted": True,
            }
        },
    )
    body = client.get(
        f"/api/ext/v1/task/training/{REF_TRAINING}", headers=auth_headers
    ).json()
    assert body["status"] == task_plane.STATUS_FAILED
    assert body["error"] == "OOM killed"
    assert body["result_available"] is False, "失败的任务不该说结果可取"
    assert body["progress_pct"] == 40


def test_backtest_progress_is_scaled_from_unit_float(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """回测的 `progress` 是 0.0-1.0 的浮点，不是 0-100。"""
    _install(
        monkeypatch,
        {
            ("GET", f"/api/v1/qlib/backtest/{REF_BACKTEST}/status"): {
                "backtest_id": REF_BACKTEST,
                "status": "completed",
                "progress": 1.0,
                "error_message": None,
            }
        },
    )
    body = client.get(
        f"/api/ext/v1/task/backtest/{REF_BACKTEST}", headers=auth_headers
    ).json()
    assert body["progress_pct"] == 100
    assert body["status"] == task_plane.STATUS_SUCCEEDED
    assert body["result_available"] is True


def test_backtest_error_message_is_surfaced(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    _install(
        monkeypatch,
        {
            ("GET", f"/api/v1/qlib/backtest/{REF_BACKTEST}/status"): {
                "status": "failed",
                "progress": 0.0,
                "error_message": "universe 为空",
            }
        },
    )
    body = client.get(
        f"/api/ext/v1/task/backtest/{REF_BACKTEST}", headers=auth_headers
    ).json()
    assert body["status"] == task_plane.STATUS_FAILED
    assert body["error"] == "universe 为空"
    assert body["result_available"] is False


# ---------------------------------------------------------------------------
# 3. 市场词表的翻译（对外一套 → 上游两套）
# ---------------------------------------------------------------------------


def _written_adapter_ids() -> set[str]:
    """上游**写了**哪些适配器 id，与环境无关。

    ⚠️ 不能拿 `list_markets()` 当全集：crypto 是**按 `ENABLE_CRYPTO` 条件
    注册**的（生产默认 false，本仓容器里实测就是 false），所以同一个注册表
    在不同部署上元素个数不同。拿它当全集会让这条断言在关 crypto 的部署上
    「表里有 crypto」假红。直接 import 适配器模块再按类取 `market_id`，
    拿到的才是「上游有哪些适配器」这个与运行环境无关的事实。
    （空串是基类的占位值，过滤掉。）
    """
    import inspect

    from backend.services.engine.rd_agent.market_adapters import (
        a_share,
        crypto,
        futures,
        hong_kong,
        us_stock,
    )

    ids: set[str] = set()
    for module in (a_share, crypto, futures, hong_kong, us_stock):
        for _, cls in inspect.getmembers(module, inspect.isclass):
            market_id = getattr(cls, "market_id", None)
            if isinstance(market_id, str) and market_id:
                ids.add(market_id)
    return ids


def test_adapter_id_table_matches_the_adapters_upstream_actually_has() -> None:
    """因子演化的适配器 id 表要与上游写的适配器**一一对应**。

    少一个：调用方收到一个带着 `a_share` 字样的 400——那是它被本面要求不要
    使用的词表，等于错误信息在教它用错词。
    多一个：那个市场在对外词表里存在却永远提不出任务。
    """
    try:
        written = _written_adapter_ids()
    except ImportError as exc:  # pragma: no cover - 依赖缺失时跳过而非假绿
        pytest.skip(f"engine 侧适配器模块不可导入：{exc}")

    assert written, "前提失效：一个适配器 id 都没取到，本断言会空转"
    declared = set(task_plane._MARKET_TO_ADAPTER_ID.values())
    assert declared == written, (
        f"映射表与上游适配器不一致。只在上游：{written - declared}；"
        f"只在本表：{declared - written}"
    )


def test_adapter_id_table_covers_every_registered_adapter() -> None:
    """本部署**当前注册**的适配器必须在表里（正常部署上都应该是子集）。

    这条与上一条的方向不同：上一条比的是「上游写了什么」，这条比的是
    「这个部署此刻开出了什么」。环境开关（`ENABLE_CRYPTO`）只影响后者。
    """
    try:
        from backend.services.engine.rd_agent.market_adapters import list_markets
    except ImportError as exc:  # pragma: no cover - 依赖缺失时跳过而非假绿
        pytest.skip(f"engine 侧市场注册表不可导入：{exc}")

    registered = {m["market_id"] for m in list_markets()}
    assert registered, "前提失效：本部署一个市场适配器都没注册，本断言会空转"
    declared = set(task_plane._MARKET_TO_ADAPTER_ID.values())
    missing = registered - declared
    assert not missing, (
        f"这些已注册的市场适配器在 _MARKET_TO_ADAPTER_ID 里没有对应项：{missing}。"
        "请补上映射（并同时确认对外 Market 词表是否要加一个值）。"
    )


def test_adapter_id_table_only_maps_declared_external_markets() -> None:
    """反向：表的键必须都在对外词表里（不许偷偷支持一个没声明的市场）。"""
    declared = set(task_plane.Market.__args__)  # type: ignore[attr-defined]
    assert set(task_plane._MARKET_TO_ADAPTER_ID) == declared


def test_alpha_evolve_translates_market_and_sends_query_params(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """**上游这个端点只收 query、没有 body**——本面把 body 翻成 query。"""
    rec = _install(
        monkeypatch,
        {
            ("POST", "/api/v1/alpha-agent/evolve"): {
                "code": 200,
                "data": {"task_id": REF_ALPHA, "status": "pending"},
            }
        },
    )
    resp = client.post(
        "/api/ext/v1/task/alpha_evolve",
        headers=auth_headers,
        json={"market": "CRYPTO", "universe": "all_a", "loop_n": 3},
    )
    assert resp.status_code == 202, resp.text
    call = rec.only_call()
    assert call.get("json_body") is None, "上游不收 body，不该发 body"
    assert call["params"]["market"] == "crypto", "对外词表没有翻成适配器 id"
    assert call["params"]["loop_n"] == 3
    assert "user_id" not in call["params"], "身份只能来自委托令牌，不能由调用方传"


@pytest.mark.parametrize(
    "external,sync_code",
    [("CN", "A"), ("CRYPTO", "BC"), ("HK", "HK"), ("US", "US"), ("FUTURES", "FUTURES")],
)
def test_data_sync_uses_upstream_sync_codes(
    external: str, sync_code: str
) -> None:
    """`CN`→`A`、`CRYPTO`→`BC`：**不是恒等映射**。

    同步键按这套码写，用 `CN` 去拼会永远查不到（上游注释里记着这是体检 C08
    假报「无同步记录」的成因）。这条也顺带钉住「引的是上游那份实现」——
    这里手抄一份就会在被改时静默漂开。
    """
    assert task_plane._to_sync_market(external) == sync_code


def test_normalize_market_delegates_to_the_single_source() -> None:
    """归一实现只该有一份（`shared/market_sessions`）。"""
    from backend.shared.market_sessions import normalize_market_key

    assert task_plane._normalize_market("CN") == normalize_market_key("CN")
    assert task_plane._normalize_market("US") == "US"


# ---------------------------------------------------------------------------
# 4. 提交：形状、翻译、以及「如实缺席」
# ---------------------------------------------------------------------------


def test_training_submit_returns_ref_and_passes_body_through(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """训练 body **原样**转发：本面不重校验，也不筛字段。

    这里塞一个本面模型没声明过的字段（`some_future_field`），断言它照样到了
    上游——这正是「不当第二份契约」的可执行版本。
    """
    rec = _install(
        monkeypatch,
        {
            ("POST", "/api/v1/models/run-training"): {
                "runId": REF_TRAINING,
                "status": "pending",
                "validFeatureCount": 10,
                "missingFeatureCount": 0,
                "missingFeatures": [],
            }
        },
    )
    resp = client.post(
        "/api/ext/v1/task/training",
        headers=auth_headers,
        json={
            "model_type": "lightgbm",
            "context": {"market": "CN"},
            "some_future_field": {"nested": [1, 2]},
        },
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["ref"] == REF_TRAINING
    assert body["pollable"] is True
    assert body["status"] == task_plane.STATUS_QUEUED
    sent = rec.only_call()["json_body"]
    assert sent["some_future_field"] == {"nested": [1, 2]}, "未知字段被吃掉了"
    assert sent["model_type"] == "lightgbm"


def test_training_submit_reports_dropped_features(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """被丢弃的特征必须说出来。

    不说的话，「我传了 20 个特征」与「实际用了 12 个」在外部看起来完全一样
    ——训练照跑、指标照出，只是少了几个因子，而调用方会以为配置生效了。
    """
    _install(
        monkeypatch,
        {
            ("POST", "/api/v1/models/run-training"): {
                "runId": REF_TRAINING,
                "status": "pending",
                "missingFeatureCount": 3,
                "missingFeatures": ["alpha_1", "alpha_2", "alpha_3"],
            }
        },
    )
    body = client.post(
        "/api/ext/v1/task/training", headers=auth_headers, json={"model_type": "lightgbm"}
    ).json()
    assert "3" in body["note"]
    assert "alpha_1" in body["note"]


def test_backtest_submit_forces_async_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """`async_mode=true` **必须**发。

    上游默认是 False（同步阻塞跑完整个回测）。漏了它，请求会在上游阻塞到回测
    结束，而我们的 30s 超时先到——调用方拿到 504，上游却还在跑，一次回测变
    两次（调用方重试）。
    """
    rec = _install(
        monkeypatch,
        {
            ("POST", "/api/v1/qlib/backtest"): {
                "backtest_id": REF_BACKTEST,
                "status": "running",
            }
        },
    )
    resp = client.post(
        "/api/ext/v1/task/backtest",
        headers=auth_headers,
        json={"model_id": "m1", "start_date": "2024-01-01", "end_date": "2024-06-30"},
    )
    assert resp.status_code == 202, resp.text
    call = rec.only_call()
    assert call["params"] == {"async_mode": "true"}
    assert call["service"] == "engine"


def test_backtest_rejects_undeclared_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """回测子集是 `extra=forbid`：写错字段名要 422，不能被静默忽略。

    这是有意与训练相反的取舍——训练是自由 dict（上游有完整校验），回测是我们
    自己收窄的子集，静默忽略会让调用方以为某个参数生效了。
    """
    _install(monkeypatch, {})  # 不该有任何上游调用
    resp = client.post(
        "/api/ext/v1/task/backtest",
        headers=auth_headers,
        json={"model_id": "m1", "strategy_content": "class X: ..."},
    )
    assert resp.status_code == 422
    assert any(
        "strategy_content" in str(item.get("loc")) for item in resp.json()["detail"]
    )


def test_data_sync_is_honest_about_having_no_job_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """数据同步没有作业句柄——说没有，而不是造一个必然 404 的 id。"""
    rec = _install(
        monkeypatch,
        {
            ("POST", "/api/v1/admin/data-platform/sync-schedule/A/run"): {
                "success": True,
                "data": {"market": "A", "label": "QuantDB A股", "status": "dispatched"},
            }
        },
    )
    resp = client.post(
        "/api/ext/v1/task/data_sync", headers=auth_headers, json={"market": "CN"}
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["ref"] is None
    assert body["pollable"] is False
    assert body["note"], "不可轮询时必须说明替代做法"
    call = rec.only_call()
    assert call["path"].endswith("/sync-schedule/A/run"), "同步码没翻对"
    assert call["admin"] is True, "这是上游唯一的管理员端点，必须显式提权"


def test_data_sync_polling_says_why_it_cannot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """轮询一个不可轮询的种类 → 400 且语义明确（不是 404）。"""
    _install(monkeypatch, {})
    resp = client.get("/api/ext/v1/task/data_sync/whatever", headers=auth_headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "kind_not_pollable: data_sync"


def test_unknown_kind_is_400_not_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """**400 而不是 404**：路由是存在的，是这个种类不存在。

    404 会让调用方去查自己是不是把路径拼错了（改 URL 而不是改 kind），
    方向反了。
    """
    _install(monkeypatch, {})
    posted = client.post(
        "/api/ext/v1/task/no_such_kind", headers=auth_headers, json={}
    )
    polled = client.get("/api/ext/v1/task/no_such_kind/abc", headers=auth_headers)
    for resp in (posted, polled):
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"].startswith("unknown_task_kind")


def test_submit_never_reaches_upstream_without_a_valid_credential(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有令牌时不许提交——**且不许碰到上游**。"""
    rec = _install(monkeypatch, {})
    resp = client.post("/api/ext/v1/task/training", json={"model_type": "lightgbm"})
    assert resp.status_code == 401
    assert rec.calls == []


# ---------------------------------------------------------------------------
# 5. 能力发现（/task/kinds）
# ---------------------------------------------------------------------------


def test_kinds_publishes_every_kind_with_a_schema(
    client: TestClient, auth_headers: dict
) -> None:
    """每个种类都要能被发现：schema 由服务端校验模型生成，不是手写副本。"""
    body = client.get("/api/ext/v1/task/kinds", headers=auth_headers).json()
    kinds = {k["kind"]: k for k in body["kinds"]}
    assert set(kinds) == {
        task_plane.KIND_TRAINING,
        task_plane.KIND_BACKTEST,
        task_plane.KIND_ALPHA_EVOLVE,
        task_plane.KIND_TRADING_AGENTS,
        task_plane.KIND_DATA_SYNC,
    }
    for name, info in kinds.items():
        assert info["request"].get("properties") is not None, f"{name} 没有 schema"
        assert isinstance(info["pollable"], bool)
    # 可轮询性与实际轮询表必须一致，否则外部节点会去调一个 400 的端点
    for name, info in kinds.items():
        assert info["pollable"] == (name in task_plane._POLLERS), (
            f"{name} 的 pollable 声明与实际轮询实现不符"
        )


def test_kinds_publishes_the_unified_market_vocabulary(
    client: TestClient, auth_headers: dict
) -> None:
    """对外市场词表要写明，且键就是对外词表本身。"""
    body = client.get("/api/ext/v1/task/kinds", headers=auth_headers).json()
    vocab = body["market_vocabulary"]
    assert set(task_plane.Market.__args__) <= set(vocab)  # type: ignore[attr-defined]
    # CUSTOM 只在数据同步里出现，它不该在通用市场词表里被当成一个可交易市场
    assert "CUSTOM" not in task_plane.Market.__args__  # type: ignore[attr-defined]


def test_kinds_status_vocabulary_matches_the_mapping_table(
    client: TestClient, auth_headers: dict
) -> None:
    """`/task/kinds` 公布的词表必须与实际会返回的值集合一致。"""
    body = client.get("/api/ext/v1/task/kinds", headers=auth_headers).json()
    declared = set(body["status_vocabulary"])
    produced = {task_plane.normalize_status(k) for k in task_plane._STATUS_MAP}
    produced.add(task_plane.STATUS_UNKNOWN)
    assert produced <= declared, f"会返回但没公布：{produced - declared}"


# ---------------------------------------------------------------------------
# 6. 闸门与路径形状
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ref", [REF_TRAINING, REF_BACKTEST, REF_ALPHA, REF_ANALYSIS])
def test_ref_shapes_survive_the_gate_pattern(
    ref: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """四种真实的上游 id 形状都必须过得了闸门的「未登记即拒绝」模式表。

    上游的 id 有三四种不同形状（`train_..._hex8`、`uuid4().hex`、
    `uuid4().hex[:16]`、`str(uuid4())[:8]`）。模式表把 `{ref}` 位放宽到
    `[A-Za-z0-9_.-]+` 就是为了这个——但放宽后**仍然**不许出现 `/`。
    这条用例拿真实形状去跑判定，避免模式表被收窄成只认其中一种。
    """
    monkeypatch.setenv(gate.ENV_KEY, "false")
    assert gate.is_blocked("GET", f"{gate.EXT_API}/task/training/{ref}") is False


def test_task_routes_are_reachable_on_a_real_trading_enabled_deployment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """反之，实盘**开启**时任务面同样可达（闸门不该影响它）。"""
    monkeypatch.setenv(gate.ENV_KEY, "true")
    _install(monkeypatch, {})
    assert client.get("/api/ext/v1/task/kinds", headers=auth_headers).status_code == 200


def test_task_plane_is_not_shadowed_by_the_data_plane(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, auth_headers: dict
) -> None:
    """`/task/kinds` 必须由任务面接住，而不是被数据面的动态段吃掉。

    数据面在**同一个 router 之下**注册了 `/{dataset}/changes` 这类根下一段的
    形状。若哪天数据面出现一个更宽的 `/{dataset}/{something}`，`/task/kinds`
    就会落到它手里——症状是任务面返回一个数据面的信封，或者一个看不懂的 404。
    这里对比两个响应体证明接住它的是哪一个。
    """
    task_body = client.get("/api/ext/v1/task/kinds", headers=auth_headers).json()
    assert "kinds" in task_body and "status_vocabulary" in task_body
    # 数据面的响应信封长这样（任一字段出现都说明接错了面）
    assert "datasets" not in task_body
    assert "changes" not in task_body


def test_no_task_route_is_left_ungated_in_the_gate_allowlist() -> None:
    """任务面的每条路由都要在闸门放行表里登记（否则默认部署上 403）。

    `live_trading_gate` 对对外命名空间是「未登记即拒绝」，所以漏登记的表现是
    「这个端点在生产上 403，而在开发机上（`ENABLE_REAL_TRADING=true`）正常」
    ——最难查的那类。这里对实际路由表逐条跑判定，而不是看名单。
    """
    probe = FastAPI()
    probe.include_router(router_module.router, prefix=gate.EXT_API)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        paths = probe.openapi().get("paths", {})

    task_paths = {p for p in paths if p.startswith(f"{gate.EXT_API}/task/")}
    assert task_paths, "前提失效：任务面一条路由都没有，本断言会空转"

    for path in sorted(task_paths):
        concrete = path.replace("{kind}", "training").replace("{ref}", REF_TRAINING)
        assert gate.is_blocked("GET", concrete) is False, (
            f"{path} 在默认部署（实盘关闭）上被闸门拒绝了：它没有登记进 "
            "live_trading_gate 的对外放行表。"
        )
