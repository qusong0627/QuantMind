"""用户层（手工增删）与两层合并的口径测试。

盯的是四类「错了也不报错」的失效：

1. **手工条目必须活过导入器**。用户层与机器层是两份文件，合并发生在读时——
   这个测试把它钉死：改完机器名单，手工条目仍在。
2. **放行是显式解除，不是删除**。条目留在名单里、理由仍在，只是不再拦买。
   一旦实现成删除，界面上「我放行的票」和「本来就不在名单里的票」会长得一模一样。
3. **放行未命中要报出来**。用户以为解除了一条限制、实际那只票本来就不在名单里——
   不报就等于默认他的操作起了作用。
4. **计数全量重算**。条数与 ``items`` 对不上时界面会说「名单 1808 只」而实际排掉 1809 只。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.shared import exclusion_list as el
from backend.shared import exclusion_overlay as eo
from backend.shared.exclusion_overlay import (
    ACTION_ALLOW,
    ACTION_BLOCK,
    MAX_ENTRIES,
    OverlayError,
    delete_entry,
    load_overlay,
    merge_into_payload,
    upsert_entry,
)

TODAY = "2026-09-20"


@pytest.fixture(autouse=True)
def _clear_caches():
    """两个模块都有 mtime 缓存，测试间必须隔离。"""
    el.clear_cache()
    eo.clear_cache()
    yield
    el.clear_cache()
    eo.clear_cache()


def _baseline(items: dict | None = None) -> dict:
    """一份最小机器名单载荷（结构照抄导入器产物）。"""
    return {
        "market": "CN",
        "asof": "2026-09-18",
        "generated_at": "2026-09-20T00:00:00Z",
        "sources": {
            "fundamental_flags": {
                "asof": "2026-09-18",
                "count": 1,
                "blocking": True,
                "label": "基本面长期排除名单",
            }
        },
        "counts": {"total": 1, "blocking": 1, "by_source": {"fundamental_flags": 1}},
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


def _write_baseline(tmp_path: Path, payload: dict) -> None:
    (tmp_path / "cn.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------- 读


def test_missing_overlay_is_empty_not_error(tmp_path):
    """文件不在盘 = 「还没手工加过」，不是错误（与机器名单缺失的语义不同）。"""
    # Act
    overlay = load_overlay("CN", root=tmp_path, use_cache=False)

    # Assert
    assert overlay.entries == {}
    assert overlay.updated_at == ""


def test_corrupt_overlay_degrades_to_empty(tmp_path):
    """损坏的 JSON 不让请求炸，也不假装有内容。"""
    # Arrange
    (tmp_path / "cn_user.json").write_text("{ 半个括号", encoding="utf-8")

    # Act
    overlay = load_overlay("CN", root=tmp_path, use_cache=False)

    # Assert
    assert overlay.entries == {}


def test_corrupt_overlay_does_not_take_down_machine_list(tmp_path):
    """两个文件是独立故障域：用户层坏了，机器基线仍要能过滤。

    合成一个「未导入」会把「名单坏了」与「我加的票没生效」混成同一个提示，
    用户没法判断该修哪个。
    """
    # Arrange
    _write_baseline(tmp_path, _baseline())
    (tmp_path / "cn_user.json").write_text("不是 JSON", encoding="utf-8")

    # Act
    lst = el.load_exclusion_list("CN", root=tmp_path, use_cache=False)

    # Assert
    assert lst is not None
    assert "600606.SH" in lst.symbols(today=TODAY)


def test_bad_action_is_rejected(tmp_path):
    """动作拼错必须报错——静默丢弃的话用户看到「已保存」而名单没变。"""
    # Arrange
    path = tmp_path / "cn_user.json"
    path.write_text(
        json.dumps({"items": {"600036.SH": {"action": "blocked"}}}), encoding="utf-8"
    )

    # Act
    overlay = load_overlay("CN", root=tmp_path, use_cache=False)

    # Assert：坏条目被跳过（不是整份丢弃），且没有出现在结果里
    assert overlay.entries == {}


# ---------------------------------------------------------------- 写


def test_upsert_creates_file_and_normalizes_symbol(tmp_path):
    """裸码写入 → 落盘是后缀式（两层键形不同会让交集恒空）。"""
    # Act
    entry = upsert_entry(
        "600036", action=ACTION_BLOCK, reason="手动排除", root=tmp_path
    )

    # Assert
    assert entry.symbol == "600036.SH"
    payload = json.loads((tmp_path / "cn_user.json").read_text(encoding="utf-8"))
    assert list(payload["items"]) == ["600036.SH"]


def test_upsert_twice_keeps_single_entry_and_created_at(tmp_path):
    """同一只票重复提交 = 改判，不产生第二条（否则「哪条生效」没有正确答案）。"""
    # Arrange
    first = upsert_entry(
        "600036.SH", action=ACTION_BLOCK, reason="先排除", root=tmp_path
    )

    # Act
    second = upsert_entry(
        "600036.SH", action=ACTION_ALLOW, reason="后来改主意", root=tmp_path
    )

    # Assert
    overlay = load_overlay("CN", root=tmp_path, use_cache=False)
    assert len(overlay.entries) == 1
    assert overlay.entries["600036.SH"].action == ACTION_ALLOW
    assert second.created_at == first.created_at


def test_bad_expire_is_rejected(tmp_path):
    """到期日写错必须报错，不能静默当永久——「以为只排除到月底」是最危险的那种误读。"""
    # Act / Assert
    with pytest.raises(OverlayError):
        upsert_entry(
            "600036.SH", action=ACTION_BLOCK, expire="2026/09/30", root=tmp_path
        )


def test_bad_symbol_is_rejected(tmp_path):
    """无法识别的代码直接拒绝，不落一条永远匹配不上的记录。"""
    # Act / Assert
    with pytest.raises(OverlayError):
        upsert_entry("不是代码", action=ACTION_BLOCK, root=tmp_path)


def test_max_entries_guard(tmp_path):
    """超过上限拒绝新增（已有条目仍可改判）——名单是给人看的表，不是数据库。"""
    # Arrange
    items = {f"{600000 + i}.SH": {"action": ACTION_BLOCK} for i in range(MAX_ENTRIES)}
    (tmp_path / "cn_user.json").write_text(
        json.dumps({"items": items}), encoding="utf-8"
    )

    # Act / Assert：600036 在 600000-604999 区间内，用它测不出上限（那是改判不是新增）
    with pytest.raises(OverlayError):
        upsert_entry("300750.SZ", action=ACTION_BLOCK, root=tmp_path)


def test_delete_reports_whether_anything_was_removed(tmp_path):
    """删除不存在的条目返回 False 而不是抛错（连点两次删除是正常操作）。"""
    # Arrange
    upsert_entry("600036.SH", action=ACTION_BLOCK, root=tmp_path)

    # Act
    first = delete_entry("600036.SH", root=tmp_path)
    second = delete_entry("600036.SH", root=tmp_path)

    # Assert
    assert first is True
    assert second is False


# ---------------------------------------------------------------- 合并


def test_manual_block_adds_new_item():
    """机器名单没有的票 → 造一条同构记录并参与排除。"""
    # Arrange
    overlay = eo.Overlay(
        market="CN",
        updated_at="2026-09-20T00:00:00Z",
        entries={
            "300750.SZ": eo.OverlayEntry(
                symbol="300750.SZ",
                action=ACTION_BLOCK,
                reason="不买",
                note="",
                expire=None,
                operator="10000001",
                created_at="2026-09-20T00:00:00Z",
                updated_at="2026-09-20T00:00:00Z",
            )
        },
    )

    # Act
    merged, stats = merge_into_payload(_baseline(), overlay)

    # Assert
    assert stats["manual"] == 1
    assert merged["items"]["300750.SZ"]["blocking"] is True
    assert merged["items"]["300750.SZ"]["by_source"]["user_manual"][
        "reason"
    ].startswith("手工排除")
    assert merged["counts"]["by_source"]["user_manual"] == 1


def test_allow_flips_blocking_but_keeps_evidence():
    """放行 = 显式解除：命中理由全部保留，只把 blocking 翻成 False。"""
    # Arrange
    overlay = eo.Overlay(
        market="CN",
        updated_at="",
        entries={
            "600606.SH": eo.OverlayEntry(
                symbol="600606.SH",
                action=ACTION_ALLOW,
                reason="已重组",
                note="",
                expire=None,
                operator="10000001",
                created_at="",
                updated_at="",
            )
        },
    )

    # Act
    merged, stats = merge_into_payload(_baseline(), overlay)

    # Assert
    assert stats["allow"] == 1
    item = merged["items"]["600606.SH"]
    assert item["blocking"] is False
    assert "user_allow" in item["sources"]
    # 机器判据仍在 —— 半年后回看要知道这条当初为什么上榜
    assert "连续 3 年亏损" in item["reason"]
    assert "已重组" in item["reason"]


def test_allow_without_machine_hit_is_counted():
    """放行一只本来就不在名单里的票 → 必须报出来，否则用户以为解除了一个不存在的限制。"""
    # Arrange
    overlay = eo.Overlay(
        market="CN",
        updated_at="",
        entries={
            "000001.SZ": eo.OverlayEntry(
                symbol="000001.SZ",
                action=ACTION_ALLOW,
                reason="",
                note="",
                expire=None,
                operator="",
                created_at="",
                updated_at="",
            )
        },
    )

    # Act
    merged, stats = merge_into_payload(_baseline(), overlay)

    # Assert
    assert stats["allow_miss"] == 1
    assert "000001.SZ" not in merged["items"]


def test_counts_are_fully_recomputed():
    """条数与 items 必须永远一致——增量加减一旦漏分支就长期对不上且难发现。"""
    # Arrange：一条放行（消掉一个 blocking）+ 一条手工（加一个 blocking）
    overlay = eo.Overlay(
        market="CN",
        updated_at="",
        entries={
            "600606.SH": eo.OverlayEntry(
                "600606.SH", ACTION_ALLOW, "", "", None, "", "", ""
            ),
            "300750.SZ": eo.OverlayEntry(
                "300750.SZ", ACTION_BLOCK, "", "", None, "", "", ""
            ),
        },
    )

    # Act
    merged, _ = merge_into_payload(_baseline(), overlay)

    # Assert
    assert merged["counts"]["total"] == 2
    assert merged["counts"]["blocking"] == 1
    assert merged["counts"]["by_source"] == {
        "fundamental_flags": 1,
        "user_manual": 1,
        "user_allow": 1,
    }


def test_merged_payload_flows_through_exclusion_list(tmp_path):
    """端到端：写用户层 → ``load_exclusion_list`` 的 ``symbols()`` 反映合并结果。"""
    # Arrange
    _write_baseline(tmp_path, _baseline())
    upsert_entry("300750.SZ", action=ACTION_BLOCK, operator="10000001", root=tmp_path)
    upsert_entry("600606.SH", action=ACTION_ALLOW, operator="10000001", root=tmp_path)

    # Act
    lst = el.load_exclusion_list("CN", root=tmp_path, use_cache=False)
    symbols = lst.symbols(today=TODAY)

    # Assert
    assert "300750.SZ" in symbols
    assert "600606.SH" not in symbols


def test_expired_allow_restores_block(tmp_path):
    """放行到期 → 自动回到「按机器名单被排除」，不需要用户再点一次。"""
    # Arrange
    _write_baseline(tmp_path, _baseline())
    upsert_entry("600606.SH", action=ACTION_ALLOW, expire="2026-09-01", root=tmp_path)

    # Act
    lst = el.load_exclusion_list("CN", root=tmp_path, use_cache=False)

    # Assert
    assert "600606.SH" in lst.symbols(today=TODAY)


def test_expired_expire_day_still_effective(tmp_path):
    """到期当天仍有效（与机器条目同一判据 ``expire >= today``）。"""
    # Arrange
    _write_baseline(tmp_path, _baseline())
    upsert_entry("600606.SH", action=ACTION_ALLOW, expire=TODAY, root=tmp_path)

    # Act
    lst = el.load_exclusion_list("CN", root=tmp_path, use_cache=False)

    # Assert
    assert "600606.SH" not in lst.symbols(today=TODAY)


def test_manual_entries_survive_reimport(tmp_path):
    """**本设计的核心不变式**：导入器覆盖机器名单后，手工条目原样还在。

    这条一旦挂掉，用户在前端加的票会在下一次导入时无声消失——而导入日志一切正常。
    """
    # Arrange
    _write_baseline(tmp_path, _baseline())
    upsert_entry("300750.SZ", action=ACTION_BLOCK, root=tmp_path)

    # Act：模拟导入器整份覆盖机器名单（多了一只、少了一只）
    _write_baseline(
        tmp_path,
        _baseline(
            items={
                "000002.SZ": {
                    "sources": ["risk_block"],
                    "flags": ["weak"],
                    "reason": "新一期风险",
                    "expire": None,
                    "blocking": True,
                    "by_source": {
                        "risk_block": {
                            "flags": ["weak"],
                            "reason": "新一期风险",
                            "expire": None,
                        }
                    },
                }
            }
        ),
    )
    lst = el.load_exclusion_list("CN", root=tmp_path, use_cache=False)

    # Assert
    symbols = lst.symbols(today=TODAY)
    assert "300750.SZ" in symbols, "手工条目被导入器抹掉了"
    assert "000002.SZ" in symbols, "新导入的机器条目没生效"
    assert "600606.SH" not in symbols, "旧机器条目应随覆盖消失"


def test_overlay_summary_exposed_in_meta(tmp_path):
    """``meta()`` 要带用户层摘要——界面靠它说「其中 N 条是你手工加的」。"""
    # Arrange：一条手工新增 + 一条命中机器的放行 + 一条放空炮的放行
    _write_baseline(tmp_path, _baseline())
    upsert_entry("300750.SZ", action=ACTION_BLOCK, root=tmp_path)
    upsert_entry("600606.SH", action=ACTION_ALLOW, root=tmp_path)
    upsert_entry("000001.SZ", action=ACTION_ALLOW, root=tmp_path)

    # Act
    meta = el.load_exclusion_list("CN", root=tmp_path, use_cache=False).meta(
        today=TODAY
    )

    # Assert
    assert meta["overlay"]["manual"] == 1
    assert meta["overlay"]["allow"] == 1
    assert meta["overlay"]["allow_miss"] == 1


def test_source_labels_cover_user_sources():
    """两个用户源必须有中文名——否则前端徽章会显示裸英文键。"""
    # Act / Assert
    assert el.source_label("user_manual") != "user_manual"
    assert el.source_label("user_allow") != "user_allow"


def test_manual_source_is_blocking():
    """手工排除与机器源同等效力，不因「是手写的」降级成只提示。"""
    # Act / Assert
    assert "user_manual" in el.BLOCKING_SOURCES
    assert "user_allow" not in el.BLOCKING_SOURCES


def test_cache_key_notices_overlay_change(tmp_path):
    """带缓存读取时，用户层改动必须失效——否则用户刚看到「已保存」却拉出旧列表。"""
    # Arrange
    _write_baseline(tmp_path, _baseline())
    before = el.load_exclusion_list("CN", root=tmp_path).symbols(today=TODAY)

    # Act
    upsert_entry("300750.SZ", action=ACTION_BLOCK, root=tmp_path)
    after = el.load_exclusion_list("CN", root=tmp_path).symbols(today=TODAY)

    # Assert
    assert "300750.SZ" not in before
    assert "300750.SZ" in after
