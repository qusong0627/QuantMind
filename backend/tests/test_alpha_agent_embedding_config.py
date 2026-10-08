"""因子挖掘「配置 API」的向量检索（embedding）配置：读写语义（纯函数，无 IO）。

背景：这块配置原先是**个人中心的 UI**，但它唯一的运行时消费者是因子挖掘
（``alpha_agent.launcher`` → ``embedding_overrides`` → RD-Agent 子进程 env →
``rdagent/oai/utils/embedding.py:resolve_embedding_channel`` 的记忆检索）。
AI-IDE 的向量检索走的是另一条独立通道（``DASHSCOPE_EMBEDDING_MODEL``），
不读这份配置。所以 UI 搬到因子挖掘，存储仍留在 ``user_profiles``（per-user，
按 user_id 拉取）。

这里锁的是三条**搬家时最容易弄丢**的性质：

1. **未传 ≠ 清空**。只改了模型名的请求不能把 Key 一起清掉——那会让子进程退回
   容器级 ``EMBEDDING_*``（或直接没有 key），表现为「改了个模型名，检索就悄悄
   换了个供应商」。所以 payload 只包含显式传入的字段。
2. **显式空串 = 清除**。这是用户唯一的「取消设置、回退容器兜底」入口。
3. **空串不能被当成「没填」而丢弃**，否则清除功能失效。

另外锁住 embedding 状态与 chat 配置的**解耦**：两个通道独立，没配 chat key
的账号照样要能看见/修改自己的 embedding 配置。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # pragma: no cover - 环境相关
    from starlette.requests import Request as StarletteRequest

    from backend.services.engine.alpha_agent import llm_client
    from backend.services.engine.routers import alpha_agent as aa
    from backend.services.engine.routers.alpha_agent import (
        EmbeddingFieldError,
        build_embedding_payload,
        build_subprocess_overrides,
        llm_config_from_profile,
        normalize_embedding_status,
        profile_embedding_overrides,
    )
except Exception as _exc:  # noqa: BLE001
    aa = None
    _IMPORT_ERR = _exc


pytestmark = pytest.mark.skipif(
    aa is None, reason="alpha_agent 依赖不可用（需容器环境）"
)


# ---------------------------------------------------------------------------
# 1. 未传 ≠ 清空（最关键的一条）
# ---------------------------------------------------------------------------
def test_absent_fields_are_not_touched():
    """只传模型名时，payload 里不能出现 key/base_url —— 否则会把已存的清掉。"""
    assert build_embedding_payload({"embedding_model": "BAAI/bge-m3"}) == {
        "embedding_model": "BAAI/bge-m3"
    }


def test_explicit_empty_string_clears():
    """显式空串是「清除」的唯一入口，必须原样带下去。"""
    assert build_embedding_payload({"embedding_api_key": ""}) == {
        "embedding_api_key": ""
    }


def test_empty_body_yields_empty_payload():
    """什么都没传 → 空 payload（端点据此判 400，而不是把配置清空）。"""
    assert build_embedding_payload({}) == {}


def test_whitespace_is_trimmed():
    """模型名/地址首尾空白一律去掉；``"  "`` 等同于显式清空。"""
    assert build_embedding_payload(
        {"embedding_model": "  bge-m3 ", "embedding_base_url": "   "}
    ) == {"embedding_model": "bge-m3", "embedding_base_url": ""}


def test_none_is_treated_as_absent_not_as_clear():
    """JSON 里的 null 是「没这个字段」，不是「清空」。"""
    assert build_embedding_payload({"embedding_model": None}) == {}


def test_unknown_keys_are_dropped():
    """只认三个 embedding 字段，别的键不能混进 profile 更新 payload。"""
    payload = build_embedding_payload(
        {
            "embedding_model": "m",
            "api_key": "sk-chat",
            "llm_model": "chat",
            "user_id": "x",
        }
    )
    assert payload == {"embedding_model": "m"}


def test_all_three_fields_together():
    payload = build_embedding_payload(
        {
            "embedding_model": "BAAI/bge-m3",
            "embedding_base_url": "https://api.siliconflow.cn/v1",
            "embedding_api_key": "sk-abc",
        }
    )
    assert payload == {
        "embedding_model": "BAAI/bge-m3",
        "embedding_base_url": "https://api.siliconflow.cn/v1",
        "embedding_api_key": "sk-abc",
    }


# ---------------------------------------------------------------------------
# 2. 读状态：与 chat 解耦 + 脱敏
# ---------------------------------------------------------------------------
def test_status_is_reported_even_when_chat_is_unconfigured():
    """没配 chat key 的账号也要能看到自己的 embedding 配置。

    改动前 embedding 只能经由 ``_fetch_profile_llm_config`` 读到，而那个函数在
    chat key 缺失时直接返回 None —— 于是「两个独立通道」在没配 chat 时
    退化成「embedding 也不可见」。
    """
    status = normalize_embedding_status(
        {
            "embedding_model": "bge-m3",
            "embedding_base_url": "http://host:11434/v1",
            "embedding_api_key": "sk-1234567890abcd",
        }
    )

    assert status["model"] == "bge-m3"
    assert status["base_url"] == "http://host:11434/v1"
    assert status["has_key"] is True


def test_status_never_returns_the_plaintext_key():
    """状态里只能有掩码，明文 Key 不得外泄。"""
    status = normalize_embedding_status({"embedding_api_key": "sk-1234567890abcd"})

    assert "sk-1234567890abcd" not in str(status)
    assert status["key_masked"] == "sk-****abcd"


def test_status_on_empty_profile():
    status = normalize_embedding_status({})

    assert status == {"model": "", "base_url": "", "has_key": False, "key_masked": ""}


def test_status_handles_none_profile():
    """profile 拉取失败时给空状态，而不是抛异常把整个配置页带崩。"""
    assert normalize_embedding_status(None)["has_key"] is False


@pytest.mark.parametrize("short", ["abc", "12345678"])
def test_status_does_not_leak_short_keys(short: str):
    """短 Key 不做「首3末4」——那样等于把整条 Key 打印出来。"""
    status = normalize_embedding_status({"embedding_api_key": short})

    assert status["key_masked"] == ""
    assert status["has_key"] is True


# ---------------------------------------------------------------------------
# 3. 端点接线
#
# 上面两组锁的是纯函数；这里锁「请求真的按预期落到 profile」。搬家前
# ``GET /llm-config`` **根本不返回** embedding 段（只有 chat 三段），前端于是
# 一律渲染成「未配置」——配置保存成功了，用户看到的还是空表单。这条回归在纯函数
# 层测不出来，因为函数本身没问题，是它没被接线。
# ---------------------------------------------------------------------------
async def _make_request(body: dict, *, user_id: str = "u-1", tenant_id: str = "t-1"):
    """造一个最小的 Request：够 ``request.json()`` 与 ``request.state.user`` 用。"""
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    request = StarletteRequest(
        {
            "type": "http",
            "method": "PUT",
            "path": "/",
            "headers": [],
            "query_string": b"",
        },
        receive,
    )
    request.state.user = {"user_id": user_id, "tenant_id": tenant_id}
    return request


@pytest.mark.integration
@pytest.mark.asyncio
async def test_endpoint_forwards_only_explicit_fields(monkeypatch):
    """端到端锁住「未传 ≠ 清空」：只改模型名的请求不能顺手把 Key 一起写掉。"""
    written: list[tuple[str, str, dict]] = []

    async def fake_update(user_id, tenant_id, payload):
        written.append((user_id, tenant_id, payload))

    async def fake_fetch(user_id, tenant_id):
        return {
            "embedding_model": "BAAI/bge-m3",
            "embedding_api_key": "sk-1234567890abcd",
        }

    monkeypatch.setattr(aa, "_update_profile", fake_update)
    monkeypatch.setattr(aa, "_fetch_profile_raw", fake_fetch)

    request = await _make_request({"embedding_model": "BAAI/bge-m3"}, user_id="u-9")
    out = await aa.update_embedding_config(request)

    assert written == [("u-9", "t-1", {"embedding_model": "BAAI/bge-m3"})]
    # 回包是写后重读的状态，前端据此刷新表单而不是自己猜
    assert out["data"]["model"] == "BAAI/bge-m3"
    assert out["data"]["key_masked"] == "sk-****abcd"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_endpoint_rejects_empty_body_without_writing(monkeypatch):
    """空请求体是 400，且**一次写都不能发生**（否则等于把配置清空）。"""
    called = False

    async def fake_update(user_id, tenant_id, payload):
        nonlocal called
        called = True

    monkeypatch.setattr(aa, "_update_profile", fake_update)

    with pytest.raises(aa.HTTPException) as exc:
        await aa.update_embedding_config(await _make_request({}))

    assert exc.value.status_code == 400
    assert called is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_endpoint_rejects_non_object_body(monkeypatch):
    """JSON 数组/字符串不是合法请求体，也不能被当成字段表写下去。"""

    async def fake_update(user_id, tenant_id, payload):  # pragma: no cover
        raise AssertionError("不该走到写")

    monkeypatch.setattr(aa, "_update_profile", fake_update)

    with pytest.raises(aa.HTTPException) as exc:
        await aa.update_embedding_config(await _make_request(["embedding_model"]))

    assert exc.value.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_endpoint_response_never_echoes_the_plaintext_key(monkeypatch):
    """写后重读会拿到明文 Key，回包必须只留掩码。"""

    async def fake_update(user_id, tenant_id, payload):
        return None

    async def fake_fetch(user_id, tenant_id):
        return {
            "embedding_model": "bge-m3",
            "embedding_base_url": "https://api.siliconflow.cn/v1",
            "embedding_api_key": "sk-1234567890abcd",
        }

    monkeypatch.setattr(aa, "_update_profile", fake_update)
    monkeypatch.setattr(aa, "_fetch_profile_raw", fake_fetch)

    out = await aa.update_embedding_config(
        await _make_request({"embedding_api_key": "sk-1234567890abcd"})
    )

    assert "sk-1234567890abcd" not in json.dumps(out, ensure_ascii=False)
    assert out["data"]["has_key"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_endpoint_propagates_write_failure(monkeypatch):
    """网关写失败必须原样抛出——吞掉会让前端弹「已保存」而后端什么都没存。"""

    async def fake_update(user_id, tenant_id, payload):
        raise aa.HTTPException(status_code=502, detail="保存失败，请稍后重试")

    monkeypatch.setattr(aa, "_update_profile", fake_update)

    with pytest.raises(aa.HTTPException) as exc:
        await aa.update_embedding_config(
            await _make_request({"embedding_model": "bge-m3"})
        )

    assert exc.value.status_code == 502


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_endpoint_exposes_embedding_when_chat_is_unconfigured(monkeypatch):
    """回归：没配 chat 的账号也必须在配置页看见自己的 embedding 配置。

    搬家前 embedding 只能经 ``_fetch_profile_llm_config`` 读到，而它在 chat key
    缺失时返回 None —— 于是「两个独立通道」退化成「chat 没配就都看不见」，
    用户保存完再进页面是空的。
    """

    async def fake_fetch(user_id, tenant_id):
        return {
            "embedding_model": "bge-m3",
            "embedding_base_url": "http://host.docker.internal:11434/v1",
            "embedding_api_key": "sk-1234567890abcd",
        }

    async def no_chat_config(*_args, **_kwargs):
        return None

    monkeypatch.setattr(aa, "_fetch_profile_raw", fake_fetch)
    monkeypatch.setattr(aa, "_fetch_profile_llm_config", no_chat_config)
    # 容器里可能真有 env 兜底的 chat 配置，先掐掉才能稳定走到「未配置」分支
    monkeypatch.setattr(llm_client, "resolve_llm_config", lambda: None)

    out = await aa.get_llm_config(await _make_request({}))
    data = out["data"]

    assert data["configured"] is False
    assert data["embedding"]["model"] == "bge-m3"
    assert data["embedding"]["base_url"] == "http://host.docker.internal:11434/v1"
    assert data["embedding"]["has_key"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_endpoint_exposes_embedding_when_chat_is_configured(monkeypatch):
    """chat 已配置的分支同样要带 embedding 段（两个分支都返回，不能只补一个）。"""

    async def fake_fetch(user_id, tenant_id):
        return {"embedding_api_key": "sk-1234567890abcd"}

    async def chat_config(*_args, **_kwargs):
        return llm_client.LLMConfig(
            api_key="sk-chat-1234abcd",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            protocol="openai",
        )

    monkeypatch.setattr(aa, "_fetch_profile_raw", fake_fetch)
    monkeypatch.setattr(aa, "_fetch_profile_llm_config", chat_config)

    out = await aa.get_llm_config(await _make_request({}))
    data = out["data"]

    assert data["configured"] is True
    assert data["api_key_masked"] == "****abcd"
    assert data["embedding"]["has_key"] is True
    assert "sk-chat-1234abcd" not in json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 4. 非法输入：不是字符串就拒绝，不做 str() 兜底
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [["https://x/v1"], {"url": "https://x/v1"}, 123, True],
    ids=["list", "dict", "int", "bool"],
)
def test_non_string_field_value_is_rejected(bad):
    """非字符串一律 400，而不是 ``str()`` 成垃圾写进 profile。

    ``str(["https://x/v1"])`` == ``"['https://x/v1']"``：会以 200 落库、界面显示
    「已保存」，直到子进程拿这串去请求才以不透明错误失败。
    """
    with pytest.raises(EmbeddingFieldError):
        build_embedding_payload({"embedding_base_url": bad})


def test_non_string_field_error_names_the_field():
    """报错要指明是哪个字段，否则用户面对三个输入框无从下手。"""
    with pytest.raises(EmbeddingFieldError) as exc:
        build_embedding_payload({"embedding_api_key": ["sk-x"]})

    assert "embedding_api_key" in str(exc.value)


def test_string_values_still_pass_after_type_check():
    """加了类型校验不能误伤正常的字符串输入。"""
    assert build_embedding_payload({"embedding_model": "bge-m3"}) == {
        "embedding_model": "bge-m3"
    }


# ---------------------------------------------------------------------------
# 5. 脱敏细节：短 Key 掩码下界 + model/base_url 的 strip 口径
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "length,should_mask", [(8, False), (15, False), (16, True), (40, True)]
)
def test_mask_threshold_is_sixteen(length: int, should_mask: bool):
    """「首3末4」掩码至少 16 位才做：9 位 Key 用这套会露出 7/9。

    边界锁在 16：短于它不给掩码（``has_key`` 仍为 True，前端用 `****` 兜底显示）。
    """
    status = normalize_embedding_status({"embedding_api_key": "k" * length})

    assert status["has_key"] is True
    if should_mask:
        assert status["key_masked"] == f"{'k' * 3}****{'k' * 4}"
    else:
        assert status["key_masked"] == ""
    # 无论掩不掩码，明文都不能出现在返回结构里
    assert "k" * length not in json.dumps(status) or length <= 4


def test_status_strips_whitespace_like_the_runtime_does():
    """状态里的 model/base_url 要与运行时同口径 strip。

    运行时取值会 strip（``"  "`` 等同未配置）；这里不 strip 就会出现
    「配置页显示已填、挖掘实际走容器默认值」的口径差。
    """
    status = normalize_embedding_status(
        {
            "embedding_model": "  bge-m3  ",
            "embedding_base_url": "  https://api.siliconflow.cn/v1  ",
            "embedding_api_key": "  sk-1234567890abcd  ",
        }
    )

    assert status["model"] == "bge-m3"
    assert status["base_url"] == "https://api.siliconflow.cn/v1"
    assert status["has_key"] is True


def test_status_treats_whitespace_only_fields_as_empty():
    """全是空白的 model/base_url 必须显示为未配置，而不是带一串空格。"""
    status = normalize_embedding_status(
        {"embedding_model": "   ", "embedding_base_url": "\t "}
    )

    assert status["model"] == ""
    assert status["base_url"] == ""


# ---------------------------------------------------------------------------
# 6. 非法 JSON 请求体
# ---------------------------------------------------------------------------
async def _make_raw_request(raw: bytes):
    """构造一个 body 为任意字节的 Request，用来喂非法 JSON。"""

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    request = StarletteRequest(
        {
            "type": "http",
            "method": "PUT",
            "path": "/",
            "headers": [],
            "query_string": b"",
        },
        receive,
    )
    request.state.user = {"user_id": "u-1", "tenant_id": "t-1"}
    return request


@pytest.mark.integration
@pytest.mark.asyncio
async def test_endpoint_rejects_malformed_json(monkeypatch):
    """非法 JSON 是 400，不是 500——用户要能看出是自己发的 body 有问题。"""

    async def fake_update(user_id, tenant_id, payload):  # pragma: no cover
        raise AssertionError("不该走到写")

    monkeypatch.setattr(aa, "_update_profile", fake_update)

    with pytest.raises(aa.HTTPException) as exc:
        await aa.update_embedding_config(await _make_raw_request(b"{not json"))

    assert exc.value.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_endpoint_rejects_non_string_field_with_400(monkeypatch):
    """数组/对象字段值走完整端点路径时是 400，且一次写都不能发生。"""
    called = False

    async def fake_update(user_id, tenant_id, payload):  # pragma: no cover
        nonlocal called
        called = True

    monkeypatch.setattr(aa, "_update_profile", fake_update)

    with pytest.raises(aa.HTTPException) as exc:
        await aa.update_embedding_config(
            await _make_request({"embedding_base_url": ["https://x/v1"]})
        )

    assert exc.value.status_code == 400
    assert called is False


# ---------------------------------------------------------------------------
# 7. IO 层：_update_profile / _fetch_profile_raw 的真实 httpx 路径
#
# 上面所有端点测试都把这两个函数整个换掉了，于是「状态码怎么映射、异常怎么
# 兜底、明文 Key 会不会进日志」这几条**一条都没被覆盖**。这里用 httpx 替身
# 走真实实现。
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def _install_fake_httpx(monkeypatch, *, get=None, put=None):
    """把 ``aa.httpx.AsyncClient`` 换成替身，返回记录调用的 calls。

    ``get``/``put`` 传 ``(status, payload)`` 元组表示正常响应，传异常实例表示
    抛出（用于覆盖 httpx.HTTPError 分支）。
    """
    calls: dict[str, list] = {"get": [], "put": []}

    def _respond(spec):
        if isinstance(spec, BaseException):
            raise spec
        if spec is None:  # pragma: no cover - 测试用例都显式给了返回值
            raise AssertionError("替身未配置该方法的响应")
        status, payload = spec
        return _FakeResponse(status, payload)

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.init_kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, **kwargs):
            calls["get"].append({"url": url, **kwargs})
            return _respond(get)

        async def put(self, url, **kwargs):
            calls["put"].append({"url": url, **kwargs})
            return _respond(put)

    monkeypatch.setattr(aa.httpx, "AsyncClient", FakeAsyncClient)
    return calls


@pytest.mark.asyncio
async def test_update_profile_maps_non_200_to_502_without_leaking(monkeypatch, caplog):
    """写失败 → 502，且**响应体不进日志/异常文案**。

    FastAPI 422 的 ``detail[].input`` 会把明文密钥原样回显；本请求体含
    ``embedding_api_key``，一旦记 ``resp.text`` 就等于把 Key 写进服务端日志。
    """
    secret = "sk-embedding-secret-9f3a2b1c"
    _install_fake_httpx(
        monkeypatch,
        put=(
            422,
            {"detail": [{"loc": ["body", "embedding_api_key"], "input": secret}]},
        ),
    )

    with caplog.at_level("ERROR"):
        with pytest.raises(aa.HTTPException) as exc:
            await aa._update_profile("u-1", "t-1", {"embedding_api_key": secret})

    assert exc.value.status_code == 502
    assert secret not in str(exc.value.detail)
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_update_profile_sends_the_payload_verbatim(monkeypatch):
    """正常路径：payload 原样 PUT 到 profile 端点（三态语义靠它落地）。"""
    calls = _install_fake_httpx(monkeypatch, put=(200, {"code": 200}))
    payload = {"embedding_model": "bge-m3", "embedding_api_key": ""}

    await aa._update_profile("u-7", "t-3", payload)

    assert len(calls["put"]) == 1
    sent = calls["put"][0]
    assert sent["url"].endswith("/api/v1/profiles/u-7")
    assert sent["json"] == payload
    assert sent["headers"]["X-Tenant-Id"] == "t-3"


@pytest.mark.asyncio
async def test_update_profile_maps_transport_error_to_502(monkeypatch):
    """连不上网关（超时/拒连）也是 502，而不是冒泡成 500。"""
    _install_fake_httpx(monkeypatch, put=aa.httpx.ConnectError("connection refused"))

    with pytest.raises(aa.HTTPException) as exc:
        await aa._update_profile("u-1", "t-1", {"embedding_model": "m"})

    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_fetch_profile_raw_returns_data_on_200(monkeypatch):
    _install_fake_httpx(monkeypatch, get=(200, {"data": {"embedding_model": "bge-m3"}}))

    data = await aa._fetch_profile_raw("u-1", "t-1")

    assert data == {"embedding_model": "bge-m3"}


@pytest.mark.asyncio
async def test_fetch_profile_raw_returns_none_on_non_200(monkeypatch):
    """读失败返回 None（配置页据此进「状态未加载」禁用态），不抛异常带崩页面。"""
    _install_fake_httpx(monkeypatch, get=(500, {"detail": "boom"}))

    assert await aa._fetch_profile_raw("u-1", "t-1") is None


@pytest.mark.asyncio
async def test_fetch_profile_raw_returns_none_on_transport_error(monkeypatch):
    _install_fake_httpx(monkeypatch, get=aa.httpx.ReadTimeout("timed out"))

    assert await aa._fetch_profile_raw("u-1", "t-1") is None


@pytest.mark.asyncio
async def test_fetch_profile_raw_tolerates_missing_data_key(monkeypatch):
    """``{"code":200}`` 没带 ``data`` 时给空 dict，而不是 None 崩掉调用方。"""
    _install_fake_httpx(monkeypatch, get=(200, {"code": 200}))

    assert await aa._fetch_profile_raw("u-1", "t-1") == {}


# ---------------------------------------------------------------------------
# 8. 既有问题 A：chat 走 env 时，Profile 里的 embedding 不能丢
#
# ``resolve_llm_config()`` 构造的 LLMConfig **不含** embedding_*（恒为空串），
# 所以只下发 ``llm_config.llm_env_overrides()`` 时，用户配的向量检索会静默
# 失效 —— 配置页显示「已保存」，挖掘却按容器默认供应商检索。
# ---------------------------------------------------------------------------
def test_profile_embedding_overrides_emits_env_when_all_three_present():
    envs = profile_embedding_overrides(
        {
            "embedding_model": "BAAI/bge-m3",
            "embedding_base_url": "https://api.siliconflow.cn",
            "embedding_api_key": "sk-embedding-1234567890",
        }
    )

    assert envs == {
        "EMBEDDING_MODEL": "BAAI/bge-m3",
        "EMBEDDING_BASE_URL": "https://api.siliconflow.cn/v1",
        "EMBEDDING_API_KEY": "sk-embedding-1234567890",
    }


@pytest.mark.parametrize(
    "profile",
    [
        {},  # 什么都没配
        {"embedding_model": "bge-m3"},  # 只有模型
        {"embedding_model": "bge-m3", "embedding_base_url": "https://x/v1"},  # 缺 key
        {"embedding_base_url": "https://x/v1", "embedding_api_key": "sk-1234567890"},
        {
            "embedding_model": "  ",
            "embedding_base_url": "  ",
            "embedding_api_key": "  ",
        },
        None,  # profile 拉取失败
    ],
    ids=["empty", "model-only", "no-key", "no-model", "blank", "none"],
)
def test_profile_embedding_overrides_stays_empty_unless_complete(profile):
    """三件套不齐就一个变量都不产出，交由容器级 EMBEDDING_* 兜底。

    半套配置比没配更糟：EMBEDDING_MODEL 覆盖了、EMBEDDING_BASE_URL 没覆盖，
    等于「用新模型打旧端点」。
    """
    assert profile_embedding_overrides(profile) == {}


def test_profile_embedding_overrides_rejects_placeholder_key():
    """占位符 Key（mock-api-key 之类）不是有效配置，不能覆盖容器兜底。"""
    envs = profile_embedding_overrides(
        {
            "embedding_model": "bge-m3",
            "embedding_base_url": "https://x/v1",
            "embedding_api_key": "mock-api-key-not-configured",
        }
    )

    assert envs == {}


def test_llm_config_from_profile_does_not_invent_a_base_url():
    """空 base 不能补成 ``/v1``：那会凭空造出一个看似合法的地址。"""
    cfg = llm_config_from_profile({"embedding_model": "bge-m3"})

    assert cfg.base_url == ""
    assert cfg.api_key == ""


def test_llm_config_from_profile_appends_v1_for_openai_protocol():
    cfg = llm_config_from_profile(
        {
            "api_key": "sk-x",
            "llm_base_url": "https://api.deepseek.com",
            "llm_model": "deepseek-chat",
        }
    )

    assert cfg.base_url == "https://api.deepseek.com/v1"
    assert cfg.protocol == "openai"


def test_llm_config_from_profile_keeps_anthropic_endpoint_as_is():
    """``.../anthropic`` 端点不能再补 /v1，chat() 会自己拼 /v1/messages。"""
    cfg = llm_config_from_profile(
        {
            "api_key": "sk-x",
            "llm_base_url": "https://api.deepseek.com/anthropic",
            "llm_model": "m",
        }
    )

    assert cfg.base_url == "https://api.deepseek.com/anthropic"
    assert cfg.protocol == "anthropic"


def test_subprocess_overrides_keep_embedding_when_chat_comes_from_env():
    """核心回归：chat 走容器 env 时，Profile 的 embedding 仍要进子进程 env。

    ``resolve_llm_config()`` 的 LLMConfig 恒不带 embedding_*，所以它单独产出的
    env 里没有 EMBEDDING_*；必须靠第二个参数补上。
    """
    env_chat = llm_client.LLMConfig(
        api_key="sk-env-chat",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        protocol="openai",
    )
    embedding_env = profile_embedding_overrides(
        {
            "embedding_model": "BAAI/bge-m3",
            "embedding_base_url": "https://api.siliconflow.cn",
            "embedding_api_key": "sk-embedding-1234567890",
        }
    )
    assert embedding_env, "前置条件：Profile 的 embedding 应产出一组覆盖变量"

    merged = build_subprocess_overrides(env_chat, embedding_env)

    # chat 侧照旧
    assert merged["OPENAI_API_KEY"] == "sk-env-chat"
    assert merged["CHAT_MODEL"] == "deepseek-chat"
    # embedding 侧不被 chat 来源吞掉
    assert merged["EMBEDDING_MODEL"] == "BAAI/bge-m3"
    assert merged["EMBEDDING_BASE_URL"] == "https://api.siliconflow.cn/v1"
    assert merged["EMBEDDING_API_KEY"] == "sk-embedding-1234567890"


def test_subprocess_overrides_with_no_embedding_leave_container_defaults_alone():
    """没配 embedding 时子进程 env 里不能出现 EMBEDDING_*（空串会覆盖容器兜底）。"""
    env_chat = llm_client.LLMConfig(
        api_key="sk-env-chat",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        protocol="openai",
    )

    merged = build_subprocess_overrides(env_chat, {})

    assert "EMBEDDING_MODEL" not in merged
    assert "EMBEDDING_BASE_URL" not in merged
    assert "EMBEDDING_API_KEY" not in merged


@pytest.mark.asyncio
async def test_resolve_effective_config_returns_profile_embedding_alongside_env_chat(
    monkeypatch,
):
    """端到端（到 _resolve_effective_llm_config 为止）：source=env 也要带 embedding。"""
    profile = {
        "embedding_model": "BAAI/bge-m3",
        "embedding_base_url": "https://api.siliconflow.cn",
        "embedding_api_key": "sk-embedding-1234567890",
        # 没有 api_key/llm_base_url/llm_model → chat 未配置，回退 env
    }

    async def fake_fetch(user_id, tenant_id):
        return profile

    env_chat = llm_client.LLMConfig(
        api_key="sk-env-chat",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        protocol="openai",
    )
    monkeypatch.setattr(aa, "_fetch_profile_raw", fake_fetch)
    monkeypatch.setattr(llm_client, "resolve_llm_config", lambda: env_chat)

    cfg, source, embedding_env = await aa._resolve_effective_llm_config("u-1", "t-1")

    assert source == "env"
    assert cfg is env_chat
    assert embedding_env["EMBEDDING_API_KEY"] == "sk-embedding-1234567890"


@pytest.mark.asyncio
async def test_resolve_effective_config_reads_profile_only_once(monkeypatch):
    """两个通道共取一次 profile：网关调用次数是 1，不是 2（也别退化成 0）。"""
    calls = {"n": 0}

    async def fake_fetch(user_id, tenant_id):
        calls["n"] += 1
        return {
            "api_key": "sk-chat-123456",
            "llm_base_url": "https://api.deepseek.com",
            "llm_model": "deepseek-chat",
            "embedding_model": "bge-m3",
            "embedding_base_url": "https://api.siliconflow.cn",
            "embedding_api_key": "sk-embedding-1234567890",
        }

    monkeypatch.setattr(aa, "_fetch_profile_raw", fake_fetch)

    cfg, source, embedding_env = await aa._resolve_effective_llm_config("u-1", "t-1")

    assert calls["n"] == 1
    assert source == "user_profile"
    assert cfg.api_key == "sk-chat-123456"
    assert embedding_env["EMBEDDING_MODEL"] == "bge-m3"
