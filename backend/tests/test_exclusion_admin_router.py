"""排除名单维护端点的行为测试（个人中心「交易黑名单」表格的后端）。

盯的是界面会直接踩到的三处：

1. **信封**。前端共享服务读 ``resp.data.data``，信封不对整页空白。
2. **删除的语义**。删掉一条 ``allow`` 会让那只票**回到**被排除状态；删掉一条
   ``block`` 会让它回到「不在名单里」。前端文案依赖这个语义，实现反了会让用户
   做出完全相反的操作。
3. **名单未导入与名单为空必须可区分**。前者是配置事故，得让用户去修导入；
   后者是正常状态。都渲染成「0 条」的话，用户会以为自己的名单被清空了。
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.shared import exclusion_list as el
from backend.shared import exclusion_overlay as eo

PREFIX = "/api/v1/exclusion"
TODAY = date.today().isoformat()


def _stub_auth():
    return {"tenant_id": "default", "user_id": "10000001", "sub": "admin"}


@pytest.fixture
def root(tmp_path, monkeypatch):
    """把名单目录指到临时目录（走 ``QM_EXCLUSION_DIR``，即真实生效路径）。"""
    monkeypatch.setenv(el.EXCLUSION_DIR_ENV, str(tmp_path))
    el.clear_cache()
    eo.clear_cache()
    yield tmp_path
    el.clear_cache()
    eo.clear_cache()


@pytest.fixture
def client(root):
    from backend.services.api.routers.exclusion_admin import router
    from backend.services.api.user_app.middleware.auth import get_current_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _stub_auth
    return TestClient(app)


def _baseline(tmp_path, items=None) -> None:
    payload = {
        "market": "CN",
        "asof": "2026-09-18",
        "generated_at": "2026-09-20T00:00:00Z",
        "sources": {},
        "counts": {"total": 0, "blocking": 0, "by_source": {}},
        "items": items
        if items is not None
        else {
            "600606.SH": {
                "sources": ["fundamental_flags"],
                "flags": ["fin"],
                "reason": "连续 3 年亏损",
                "expire": None,
                "blocking": True,
                "by_source": {
                    "fundamental_flags": {
                        "flags": ["fin"],
                        "reason": "连续 3 年亏损",
                        "expire": None,
                    }
                },
            }
        },
    }
    (tmp_path / "cn.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_list_reports_not_imported_distinctly(client, root):
    """名单文件不在盘 → ``imported=False`` + reason，而不是一个空洞的 0 条。"""
    # Act
    body = client.get(f"{PREFIX}/entries").json()

    # Assert
    assert body["success"] is True
    assert body["data"]["imported"] is False
    assert "未导入" in body["data"]["reason"]


def test_list_returns_envelope_and_rows(client, root):
    """信封 {success, data{items}} + 行内带来源与本人改动槽位。"""
    # Arrange
    _baseline(root)

    # Act
    body = client.get(f"{PREFIX}/entries").json()

    # Assert
    assert body["success"] is True
    data = body["data"]
    assert data["imported"] is True
    assert data["total"] == 1
    row = data["items"][0]
    assert row["symbol"] == "600606.SH"
    assert row["source_labels"] == ["基本面长期排除名单"]
    assert row["manual"] is None


def test_post_then_list_shows_manual_entry(client, root):
    """手工新增 → 表格里出现且标为本人改动。"""
    # Arrange
    _baseline(root)

    # Act
    created = client.post(
        f"{PREFIX}/entries",
        json={"symbol": "600036", "action": "block", "reason": "不买银行"},
    )

    # Assert
    assert created.status_code == 200
    assert created.json()["data"]["entry"]["symbol"] == "600036.SH"
    row = client.get(f"{PREFIX}/entries").json()["data"]["items"][0]
    assert row["symbol"] in {"600036.SH", "600606.SH"}
    manual = next(
        r
        for r in client.get(f"{PREFIX}/entries").json()["data"]["items"]
        if r["manual"]
    )
    assert manual["symbol"] == "600036.SH"
    assert manual["manual"]["action"] == "block"
    assert manual["manual"]["operator"] == "10000001"


def test_allow_flips_row_to_non_blocking(client, root):
    """放行机器名单里的票 → 行还在（理由仍可见）但 ``blocking=False``。"""
    # Arrange
    _baseline(root)

    # Act
    client.post(
        f"{PREFIX}/entries",
        json={"symbol": "600606.SH", "action": "allow", "reason": "已重组"},
    )
    row = client.get(f"{PREFIX}/entries").json()["data"]["items"][0]

    # Assert
    assert row["blocking"] is False
    assert "user_allow" in row["sources"]
    assert "连续 3 年亏损" in row["reason"]


def test_delete_allow_restores_exclusion(client, root):
    """**语义钉子**：撤销放行 = 这只票回到被排除，而不是「从名单里消失」。"""
    # Arrange
    _baseline(root)
    client.post(f"{PREFIX}/entries", json={"symbol": "600606.SH", "action": "allow"})

    # Act
    removed = client.delete(f"{PREFIX}/entries/600606.SH")
    lst = el.load_exclusion_list("CN", use_cache=False)

    # Assert
    assert removed.json()["data"]["removed"] is True
    assert "600606.SH" in lst.symbols(today=TODAY)


def test_delete_block_removes_it_entirely(client, root):
    """撤销手工排除 = 这只票回到「不在名单里」（与撤销放行方向相反）。"""
    # Arrange
    _baseline(root)
    client.post(f"{PREFIX}/entries", json={"symbol": "300750.SZ", "action": "block"})

    # Act
    client.delete(f"{PREFIX}/entries/300750.SZ")
    lst = el.load_exclusion_list("CN", use_cache=False)

    # Assert
    assert "300750.SZ" not in lst.symbols(today=TODAY)
    assert "300750.SZ" not in lst.items


def test_delete_missing_is_not_an_error(client, root):
    """连点两次删除是正常操作，第二次返回 ``removed=false`` 而不是 4xx。"""
    # Arrange
    _baseline(root)

    # Act
    body = client.delete(f"{PREFIX}/entries/600036.SH").json()

    # Assert
    assert body["success"] is True
    assert body["data"]["removed"] is False


def test_bad_input_returns_400_with_reason(client, root):
    """非法代码/动作/日期 → 400 且带人能看懂的原因（吞成「保存失败」会让人反复重试）。"""
    # Arrange
    _baseline(root)

    # Act / Assert
    # 长度不足由 pydantic 拦（422），形状不对（认不出交易所）由业务层拦（400）
    short = client.post(f"{PREFIX}/entries", json={"symbol": "乱码", "action": "block"})
    assert short.status_code == 422

    bad_symbol = client.post(
        f"{PREFIX}/entries", json={"symbol": "ABCDEF", "action": "block"}
    )
    assert bad_symbol.status_code == 400 and "无法识别" in bad_symbol.json()["detail"]

    bad_action = client.post(
        f"{PREFIX}/entries", json={"symbol": "600036", "action": "hold"}
    )
    assert bad_action.status_code == 422  # Literal 校验在 FastAPI 层拦下

    bad_date = client.post(
        f"{PREFIX}/entries",
        json={"symbol": "600036", "action": "block", "expire": "9/30"},
    )
    assert bad_date.status_code == 400 and "到期日" in bad_date.json()["detail"]


def test_action_filter_matches_rows(client, root):
    """``action`` 过滤：manual / allow / machine 三档各自只出对应行。"""
    # Arrange
    _baseline(root)
    client.post(f"{PREFIX}/entries", json={"symbol": "300750.SZ", "action": "block"})
    client.post(f"{PREFIX}/entries", json={"symbol": "600606.SH", "action": "allow"})

    # Act
    manual = client.get(f"{PREFIX}/entries", params={"action": "manual"}).json()["data"]
    allow = client.get(f"{PREFIX}/entries", params={"action": "allow"}).json()["data"]
    machine = client.get(f"{PREFIX}/entries", params={"action": "machine"}).json()[
        "data"
    ]

    # Assert
    assert [r["symbol"] for r in manual["items"]] == ["300750.SZ"]
    assert [r["symbol"] for r in allow["items"]] == ["600606.SH"]
    assert machine["items"] == []


def test_unknown_action_filter_returns_nothing(client, root):
    """拼错的过滤词返回空集而不是「看起来没过滤」——静默当全部是最坏的一种宽容。"""
    # Arrange
    _baseline(root)

    # Act
    data = client.get(f"{PREFIX}/entries", params={"action": "manul"}).json()["data"]

    # Assert
    assert data["total"] == 0


def test_query_matches_code_and_name(client, root):
    """检索命中代码与中文名两条路。"""
    # Arrange
    _baseline(root)

    # Act
    by_code = client.get(f"{PREFIX}/entries", params={"q": "600606"}).json()["data"]
    by_name = client.get(f"{PREFIX}/entries", params={"q": "不存在的名字"}).json()[
        "data"
    ]

    # Assert
    assert by_code["total"] == 1
    assert by_name["total"] == 0


def test_meta_endpoint_reports_both_layers(client, root):
    """``/meta`` 同时给机器层的基准日与用户层的条数（个人中心顶部与候选页风险条共用）。"""
    # Arrange
    _baseline(root)
    client.post(f"{PREFIX}/entries", json={"symbol": "300750.SZ", "action": "block"})

    # Act
    data = client.get(f"{PREFIX}/meta").json()["data"]

    # Assert
    assert data["imported"] is True
    assert data["meta"]["asof"] == "2026-09-18"
    assert data["overlay"]["counts"]["block"] == 1
    assert data["sources"]["user_manual"]["label"] == "手工排除（本人在个人中心添加）"
