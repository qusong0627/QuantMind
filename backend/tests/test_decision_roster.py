"""决策名册配置面测试（P2.9）：读不泄密 / 校验不落盘 / 先 key 后名册 / 回滚 / 清残留。

口径的**唯一出处**是 ``shared/decision_llm_client.resolve_roster``：这里的用例刻意反复
拿它当裁判（保存前先过一遍、保存后回读复验），而不是另写一套「我以为的规则」。
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit

_SECRET = "sk-this-must-never-leak-into-the-roster"


@pytest.fixture(autouse=True)
def _hermetic_runtime_env(tmp_path, monkeypatch):
    """把 ``runtime.env`` 钉到空临时文件：**本文件所有用例都不许读生产那份**。

    ``_stale_key_vars`` 是刻意读**文件**的（清历史残留），于是没钉路径的用例会读到
    ``/app/config/runtime.env``——生产里只要有一个带前缀的残留变量，这些用例就红，
    而失败原因看上去是「实现多删了一条」。实测踩过：探针写完没清，两条用例报
    `('del', …) != ('set', …)`。用例自己 ``monkeypatch.setenv`` 的路径覆盖本夹具。
    """
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(tmp_path / "runtime.env"))
    return tmp_path / "runtime.env"


def _env(**kw):
    """干净的环境映射：不碰真 ``os.environ``（写侧注入写进该映射的假 set_secret）。"""
    base = {
        "QM_DECISION_LLM_BASE_URL": "https://api.deepseek.com/v1",
        "QM_DECISION_LLM_API_KEY": "sk-global",
        "QM_DECISION_LLM_MODEL": "deepseek-chat",
    }
    base.update(kw)
    return base


class _Store:
    """假的 set/delete：写进给定映射并记录调用顺序（真实现的接线另有一条用例覆盖）。"""

    def __init__(self, env):
        self.env = env
        self.calls: list[tuple[str, str]] = []

    def set(self, key, value):
        self.calls.append(("set", key))
        self.env[key] = value

    def delete(self, key):
        self.calls.append(("del", key))
        self.env.pop(key, None)
        return True


def _apply(payload, env, **kw):
    from backend.services.trade.services.decision_roster_config import apply_roster

    store = _Store(env)
    out = apply_roster(
        payload, env=env, set_secret_fn=store.set, delete_secret_fn=store.delete, **kw
    )
    return out, store


def _describe(env, **kw):
    from backend.services.trade.services.decision_roster_config import describe

    kw.setdefault("status_reader", lambda: {"last": None, "agents": {}, "error": ""})
    return describe(env=env, **kw)


# ── 变量名生成 ──────────────────────────────────────────────────────


def test_key_var_name_is_identity_based_and_env_safe():
    from backend.services.trade.services.decision_roster_config import key_var_name

    assert key_var_name("deepseek-v4-pro") == "QM_DECISION_LLM_KEY_DEEPSEEK_V4_PRO"
    # 归一化口径与 agent 身份一致（同一个 normalize_agent）
    assert key_var_name("  DeepSeek-V4-Pro ") == key_var_name("deepseek-v4-pro")
    # 变量名必须是 runtime_secrets 认的字符集，否则 set_secret 直接 ValueError
    from backend.shared.runtime_secrets import _KEY_PATTERN

    assert _KEY_PATTERN.match(key_var_name("glm/4.5:flash"))


# ── 读现状 ──────────────────────────────────────────────────────────


def test_describe_without_roster_reports_single_path():
    env = _env(QM_DECISION_ROUND_ENABLED="1")  # 非 "true" 一律不算开
    data = _describe(env)
    assert data["roster_configured"] is False
    assert data["source"] == "single" and data["entries"] == []
    assert data["limit"] == 8 and data["single"]["ok"] is True
    assert data["round"]["enabled"] is False
    assert data["round"]["env"] == "QM_DECISION_ROUND_ENABLED"


def test_describe_marks_unconfigured_when_nothing_is_set():
    data = _describe({})
    assert data["source"] == "none" and data["error"]
    assert data["single"]["ok"] is False


def test_describe_never_returns_key_values():
    """读接口只报「配没配」。序列化整份返回，逐字搜 key 明文。"""
    env = _env(
        QM_DECISION_LLM_ROSTER=json.dumps(
            [
                {"model": "glm-4.6", "base_url": "https://open.bigmodel.cn/api/paas/v4",
                 "api_key_env": "GLM_API_KEY"},
                {"model": "qwen3-max", "api_key": _SECRET},
            ]
        ),
        GLM_API_KEY=_SECRET,
    )
    data = _describe(env)
    blob = json.dumps(data, ensure_ascii=False)
    assert _SECRET not in blob
    assert data["entries"][0]["api_key_set"] is True
    assert data["entries"][0]["api_key_env"] == "GLM_API_KEY"
    assert data["entries"][1]["api_key_inline"] is True  # 字面值：只报事实，不回传值
    assert data["source"] == "roster" and data["error"] == ""


def test_describe_attributes_errors_to_the_failing_entry_only():
    """逐家解析：第二家缺 key 不该让第一家也变「坏」，也不该整页 500。"""
    env = _env(
        QM_DECISION_LLM_ROSTER=json.dumps(
            [
                {"model": "glm-4.6", "base_url": "https://open.bigmodel.cn/api/paas/v4",
                 "api_key_env": "GLM_API_KEY"},
                {"model": "qwen3-max", "api_key_env": "MISSING_KEY"},
            ]
        ),
        GLM_API_KEY="sk-glm",
    )
    data = _describe(env)
    ok, bad = data["entries"]
    assert ok["ok"] is True and ok["agent"] == "glm-4.6"
    assert bad["ok"] is False and bad["agent"] == "qwen3-max"
    assert "MISSING_KEY" in bad["error"]          # 点名变量
    assert "qwen3-max" in bad["error"]            # 点名是哪家
    assert _SECRET not in json.dumps(data) and "sk-glm" not in json.dumps(data)
    assert data["error"]  # 整段名册确实不可用，如实报


def test_describe_survives_broken_roster_json():
    data = _describe(_env(QM_DECISION_LLM_ROSTER="{not json"))
    assert data["roster_configured"] is True
    assert data["entries"] == [] and "JSON" in data["error"]


def test_describe_survives_status_reader_failure():
    from backend.services.trade.services.decision_roster_config import describe

    def boom():
        raise RuntimeError("redis down")

    data = describe(env=_env(), status_reader=boom)
    assert data["status_error"]  # 面板显示「无状态镜像」是对的，显示「一切正常」才是错的
    assert data["entries"] == []


# ── 保存：校验 ──────────────────────────────────────────────────────


def test_apply_rejects_empty_and_oversized_rosters():
    env = _env()
    out, _ = _apply({"entries": []}, env)
    assert out["ok"] is False and "至少要有一家" in out["errors"][0]
    out, _ = _apply({"entries": [{"model": f"m{i}"} for i in range(9)]}, env)
    assert out["ok"] is False and "超过上限 8" in out["errors"][0]
    assert "QM_DECISION_LLM_ROSTER" not in env  # 一条都没落盘


def test_apply_rejects_placeholder_model_and_bad_numbers():
    env = _env()
    out, _ = _apply({"entries": [{"model": "your-model-name"}]}, env)
    assert out["ok"] is False and "model" in out["errors"][0]
    out, _ = _apply({"entries": [{"model": "glm-4.6", "timeout": "很久"}]}, env)
    assert out["ok"] is False and "timeout" in out["errors"][0]
    assert "QM_DECISION_LLM_ROSTER" not in env


def test_apply_rejects_duplicate_agents_before_writing_anything():
    """归一后重名 = 两家并成一本账（各自虚拟现金/持仓互相吃），必须在落盘前拦。"""
    env = _env()
    out, _ = _apply(
        {"entries": [{"model": "glm-4.6", "api_key": "a"}, {"model": " glm-4.6 ", "api_key": "b"}]},
        env,
    )
    assert out["ok"] is False and "重名" in out["errors"][0]
    assert "QM_DECISION_LLM_ROSTER" not in env
    assert not [k for k in env if k.startswith("QM_DECISION_LLM_KEY_")]


# ── 保存：落盘 ──────────────────────────────────────────────────────


def test_apply_writes_key_var_first_then_roster_without_the_key():
    env = _env()
    payload = {
        "entries": [
            {"model": "glm-4.6", "base_url": "https://open.bigmodel.cn/api/paas/v4",
             "api_key": _SECRET},
        ]
    }
    out, store = _apply(payload, env)
    assert out["ok"] is True and out["agents"] == ["glm-4.6"]
    assert store.calls[-1] == ("set", "QM_DECISION_LLM_ROSTER")  # 名册最后写
    roster_text = env["QM_DECISION_LLM_ROSTER"]
    assert _SECRET not in roster_text               # key 绝不进名册串
    assert "QM_DECISION_LLM_KEY_GLM_4_6" in roster_text  # 只引用变量名
    assert env["QM_DECISION_LLM_KEY_GLM_4_6"] == _SECRET
    # 落盘结果真能被解析器读出来（不是「看着对」）
    from backend.shared.decision_llm_client import resolve_roster

    (cfg,) = resolve_roster(env)
    assert cfg.model == "glm-4.6" and cfg.api_key == _SECRET
    assert cfg.base_url == "https://open.bigmodel.cn/api/paas/v4"


def test_apply_accepts_a_brand_new_key_on_first_save():
    """回归钉：预检跑在落盘之前，新 key 必须先放进**预览环境**再校验。

    修之前的表现是——第一次保存新 key 永远被自己的预检判「未配置」，
    用户看到「保存失败」，而唯一出路是手工改 runtime.env。
    """
    env = _env(QM_DECISION_LLM_ROSTER=json.dumps([{"model": "old-model"}]))
    out, _ = _apply({"entries": [{"model": "new-model", "api_key": "sk-brand-new"}]}, env)
    assert out["ok"] is True, out["errors"]


def test_apply_blank_key_keeps_previous_reference():
    """只改端点时不许把 key 改没：留空 = 沿用这家原来引用的变量。"""
    env = _env()
    _apply({"entries": [{"model": "glm-4.6", "api_key": "sk-first"}]}, env)
    out, store = _apply(
        {"entries": [{"model": "glm-4.6", "base_url": "https://mirror.example.com/v1"}]}, env
    )
    assert out["ok"] is True and store.calls[-1][0] == "set"
    entry = json.loads(env["QM_DECISION_LLM_ROSTER"])[0]
    assert entry["api_key_env"] == "QM_DECISION_LLM_KEY_GLM_4_6"
    assert env["QM_DECISION_LLM_KEY_GLM_4_6"] == "sk-first"


def test_apply_clear_api_key_drops_reference_and_deletes_var():
    env = _env()
    _apply({"entries": [{"model": "glm-4.6", "api_key": "sk-first"}]}, env)
    out, store = _apply(
        {"entries": [{"model": "glm-4.6", "base_url": "https://x/v1", "clear_api_key": True}]},
        env,
    )
    assert out["ok"] is True
    entry = json.loads(env["QM_DECISION_LLM_ROSTER"])[0]
    assert "api_key_env" not in entry and "api_key" not in entry
    assert "QM_DECISION_LLM_KEY_GLM_4_6" not in env
    assert ("del", "QM_DECISION_LLM_KEY_GLM_4_6") in store.calls


def test_apply_rolls_back_to_previous_roster_when_readback_fails(monkeypatch):
    """复验失败 → 回滚到上一版并如实报：绝不留下「显示已保存、一轮都跑不动」的名册。"""
    previous = json.dumps([{"model": "glm-4.6", "api_key": "sk-first"}])
    env = _env(QM_DECISION_LLM_ROSTER=previous)
    store = _Store(env)
    real_set = store.set
    state = {"sabotaged": False}

    def sabotaging_set(key, value):
        """只坏**第一次**名册写入（模拟落盘被截断/并发覆盖）；rollback 那次要能写成。"""
        if key == "QM_DECISION_LLM_ROSTER" and not state["sabotaged"]:
            state["sabotaged"] = True
            real_set(key, "{oops")
        else:
            real_set(key, value)

    from backend.services.trade.services.decision_roster_config import apply_roster

    out = apply_roster(
        {"entries": [{"model": "glm-4.6", "api_key": "sk-new"}]},
        env=env,
        set_secret_fn=sabotaging_set,
        delete_secret_fn=store.delete,
    )
    assert out["ok"] is False and out.get("rolled_back") is True
    assert env["QM_DECISION_LLM_ROSTER"] == previous  # 上一版原样回来


def test_apply_cleans_stale_generated_key_vars(tmp_path, monkeypatch):
    """删掉一家模型后它的 key 不该永远留在 runtime.env 里没人知道。"""
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "QM_DECISION_LLM_KEY_RETIRED_MODEL=sk-retired\nOTHER_KEY=keep-me\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(env_file))
    env = _env()
    out, store = _apply({"entries": [{"model": "glm-4.6", "api_key": "sk-live"}]}, env)
    assert out["ok"] is True
    assert "QM_DECISION_LLM_KEY_RETIRED_MODEL" in out["deleted_key_vars"]
    assert ("del", "QM_DECISION_LLM_KEY_RETIRED_MODEL") in store.calls
    assert "OTHER_KEY" not in out["deleted_key_vars"]  # 只清本模块生成的


# ── 清空 ────────────────────────────────────────────────────────────


def test_clear_roster_returns_to_single_path():
    env = _env(QM_DECISION_LLM_ROSTER=json.dumps([{"model": "glm-4.6", "api_key": "k"}]))
    from backend.services.trade.services.decision_roster_config import clear_roster

    store = _Store(env)
    out = clear_roster(env=env, set_secret_fn=store.set, delete_secret_fn=store.delete)
    assert out["ok"] is True and env["QM_DECISION_LLM_ROSTER"] == ""
    from backend.shared.decision_llm_client import resolve_roster

    (cfg,) = resolve_roster(env)
    assert cfg.model == "deepseek-chat"  # 回到单家三件套


def test_clear_roster_rolls_back_when_single_path_is_unusable():
    """清空后连单家都不通 = 把能跑的搞成不能跑 → 回滚，不硬清。"""
    previous = json.dumps([{"model": "glm-4.6", "base_url": "https://x/v1", "api_key": "k"}])
    env = {"QM_DECISION_LLM_ROSTER": previous}  # 全局三件套一个都没配
    from backend.services.trade.services.decision_roster_config import clear_roster

    store = _Store(env)
    out = clear_roster(env=env, set_secret_fn=store.set, delete_secret_fn=store.delete)
    assert out["ok"] is False and out.get("rolled_back") is True
    assert env["QM_DECISION_LLM_ROSTER"] == previous


# ── 与真实密钥存储的接线 ─────────────────────────────────────────────


def test_real_set_secret_wiring_writes_key_var_and_resolves(tmp_path, monkeypatch):
    """用**真的** set_secret/delete_secret 跑一遍：落进 runtime.env 的那份能被解析器读出来。

    这一条覆盖的是「接口对、接线错」那一类：假 store 全绿也可能真实现根本写不进去。
    """
    env_file = tmp_path / "runtime.env"
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(env_file))
    # 真 set_secret 会写本进程环境 ⇒ 跑完把 QM_DECISION_LLM_* 恢复原样，别污染后续用例
    snapshot = {k: v for k, v in os.environ.items() if k.startswith("QM_DECISION_LLM_")}

    def restore():
        for key in [k for k in os.environ if k.startswith("QM_DECISION_LLM_")]:
            os.environ.pop(key, None)
        os.environ.update(snapshot)

    try:
        from backend.shared.decision_llm_client import resolve_roster
        from backend.shared.runtime_secrets import delete_secret, read_runtime_env
        from backend.services.trade.services.decision_roster_config import apply_roster

        out = apply_roster(
            {
                "entries": [
                    {"model": "glm-4.6", "base_url": "https://open.bigmodel.cn/api/paas/v4",
                     "api_key": _SECRET, "max_tokens": 2000}
                ]
            }
        )
        assert out["ok"] is True, out["errors"]
        on_disk = read_runtime_env()
        assert on_disk["QM_DECISION_LLM_KEY_GLM_4_6"] == _SECRET
        assert _SECRET not in on_disk["QM_DECISION_LLM_ROSTER"]
        assert env_file.stat().st_mode & 0o777 == 0o600
        # 真解析器读活环境（trade 进程下一轮就是这样读的）
        (cfg,) = resolve_roster(os.environ)
        assert cfg.api_key == _SECRET and cfg.max_tokens == 2000
        # delete_secret 真的把它抹掉
        assert delete_secret("QM_DECISION_LLM_KEY_GLM_4_6") is True
        assert "QM_DECISION_LLM_KEY_GLM_4_6" not in read_runtime_env()
        assert not os.environ.get("QM_DECISION_LLM_KEY_GLM_4_6")
    finally:
        restore()


def test_delete_secret_refuses_to_claim_a_real_env_var_is_gone(monkeypatch, tmp_path):
    """compose 注入的同名变量不归本模块管：删不掉就说删不掉（返回 False），不假装。"""
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(tmp_path / "runtime.env"))
    monkeypatch.setenv("QM_DECISION_LLM_KEY_OPERATOR_OWNED", "sk-operator")
    from backend.shared.runtime_secrets import delete_secret

    assert delete_secret("QM_DECISION_LLM_KEY_OPERATOR_OWNED") is False
    assert os.environ["QM_DECISION_LLM_KEY_OPERATOR_OWNED"] == "sk-operator"


def test_delete_secret_rejects_illegal_key_names(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(tmp_path / "runtime.env"))
    from backend.shared.runtime_secrets import delete_secret

    for bad in ("lowercase", "1LEADING_DIGIT", "WITH-DASH"):
        with pytest.raises(ValueError):
            delete_secret(bad)
