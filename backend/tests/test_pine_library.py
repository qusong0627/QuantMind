"""Pine 策略库：索引构建（来源优先级 / 缩进判定）+ 服务层（过滤 / 编辑保存 / id 校验）。

来源优先级与缩进判定是这套东西的核心口径——旧语料被爬虫拍平过，
一旦优先级写反，界面就会拿残缺版本盖掉重爬好的完整版本。

运行（容器）：python3 -m pytest backend/tests/test_pine_library.py -q
"""
import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.agent_arena import pine_library as pl  # noqa: E402
from scripts import pine_library_index as pli  # noqa: E402


FLAT = """//@version=6
strategy("t", overlay=true)
if close > open
strategy.entry("L", strategy.long)
"""

INDENTED = """//@version=6
strategy("t", overlay=true)
if close > open
    strategy.entry("L", strategy.long)
"""


def _corpus(tmp: Path) -> Path:
    """造一份最小语料：分类清单.csv + 两个分类目录。"""
    src = tmp / "corpus"
    for cat, name, body in [("趋势跟踪", "001_a.pine", FLAT),
                            ("均值回归", "002_b.pine", FLAT)]:
        (src / cat).mkdir(parents=True, exist_ok=True)
        (src / cat / name).write_text(body, encoding="utf-8")
    with open(src / pli.CSV_NAME, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["序号", "分类", "中文标题", "原标题", "文件名", "URL", "状态"])
        w.writeheader()
        w.writerow({"序号": "1", "分类": "趋势跟踪", "中文标题": "甲", "原标题": "A",
                    "文件名": "001_a.pine", "URL": "https://tv/1", "状态": "ok"})
        w.writerow({"序号": "2", "分类": "均值回归", "中文标题": "乙", "原标题": "B",
                    "文件名": "002_b.pine", "URL": "https://tv/2", "状态": "ok"})
    return src


class TestMeta:
    def test_flat_source_is_flagged(self):
        assert pli._meta(FLAT)["indent_ok"] is False

    def test_indented_source_is_usable(self):
        m = pli._meta(INDENTED)
        assert m["indent_ok"] is True
        assert m["version"] == 6
        assert m["has_strategy"] is True
        assert m["lines"] == 4


class TestBuild:
    def test_index_lists_catalog_and_flags_flat(self, tmp_path):
        idx = pli.build(source=_corpus(tmp_path), lib_dir=tmp_path / "lib")
        assert idx["total"] == 2
        assert idx["usable"] == 0          # 两份都是拍平的
        assert {c["name"] for c in idx["categories"]} == {"趋势跟踪", "均值回归"}
        assert (tmp_path / "lib/index.json").exists()
        assert (tmp_path / "lib/source/0001.pine").read_text(encoding="utf-8") == FLAT

    def test_recrawl_wins_over_desktop(self, tmp_path):
        lib = tmp_path / "lib"
        recrawl = lib / "recrawl"
        recrawl.mkdir(parents=True)
        (recrawl / "001_x.pine").write_text(
            "// 来源: https://tv/1\n" + INDENTED, encoding="utf-8")
        idx = pli.build(source=_corpus(tmp_path), lib_dir=lib)
        by_id = {it["id"]: it for it in idx["items"]}
        assert by_id["0001"]["source_kind"] == "recrawl"
        assert by_id["0001"]["indent_ok"] is True
        assert by_id["0002"]["source_kind"] == "desktop"
        assert idx["usable"] == 1

    def test_edited_wins_over_recrawl(self, tmp_path):
        lib = tmp_path / "lib"
        (lib / "recrawl").mkdir(parents=True)
        (lib / "recrawl/001_x.pine").write_text(
            "// 来源: https://tv/1\n" + FLAT, encoding="utf-8")
        (lib / "edited").mkdir(parents=True)
        (lib / "edited/0001.pine").write_text(INDENTED, encoding="utf-8")
        idx = pli.build(source=_corpus(tmp_path), lib_dir=lib)
        by_id = {it["id"]: it for it in idx["items"]}
        assert by_id["0001"]["source_kind"] == "edited"
        assert (lib / "source/0001.pine").read_text(encoding="utf-8") == INDENTED

    def test_missing_file_marked_not_dropped(self, tmp_path):
        src = _corpus(tmp_path)
        (src / "均值回归/002_b.pine").unlink()
        idx = pli.build(source=src, lib_dir=tmp_path / "lib")
        by_id = {it["id"]: it for it in idx["items"]}
        assert by_id["0002"]["source_kind"] == "missing"
        assert idx["total"] == 2

    def test_orphan_recrawl_lands_in_unclassified(self, tmp_path):
        lib = tmp_path / "lib"
        (lib / "recrawl").mkdir(parents=True)
        (lib / "recrawl/zzz.pine").write_text(
            "// 来源: https://tv/999\n" + INDENTED, encoding="utf-8")
        idx = pli.build(source=_corpus(tmp_path), lib_dir=lib)
        orphans = [it for it in idx["items"] if it["category"] == "未归类"]
        assert len(orphans) == 1 and orphans[0]["url"] == "https://tv/999"


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    """把服务层的目录指到临时库，并按语料建一次索引。"""
    d = tmp_path / "lib"
    monkeypatch.setattr(pl, "lib_dir", lambda: d)
    monkeypatch.setattr(pl, "index_file", lambda: d / "index.json")
    monkeypatch.setattr(pl, "source_dir", lambda: d / "source")
    monkeypatch.setattr(pl, "edited_dir", lambda: d / "edited")
    pli.build(source=_corpus(tmp_path), lib_dir=d)
    return d


class TestService:
    def test_list_filters_by_category_and_keyword(self, lib):
        assert pl.list_items(category="趋势跟踪")["filtered"] == 1
        assert pl.list_items(q="乙")["filtered"] == 1
        assert pl.list_items(q="不存在")["filtered"] == 0
        assert pl.list_items()["filtered"] == 2

    def test_get_source_returns_text(self, lib):
        got = pl.get_source("0001")
        assert got["title"] == "甲" and "strategy(" in got["source"]
        assert got["edited"] is False

    def test_save_then_reset(self, lib):
        pl.save_source("0001", INDENTED)
        got = pl.get_source("0001")
        assert got["edited"] is True and got["source"] == INDENTED
        assert (lib / "edited/0001.pine").exists()
        pl.reset_source("0001")
        assert pl.get_source("0001")["edited"] is False
        assert not (lib / "edited/0001.pine").exists()

    def test_save_rejects_empty_and_oversize(self, lib):
        with pytest.raises(ValueError):
            pl.save_source("0001", "   ")
        with pytest.raises(ValueError):
            pl.save_source("0001", "x" * (pl.MAX_SOURCE_BYTES + 1))

    @pytest.mark.parametrize("bad", ["../etc/passwd", "1", "0001.pine", "", "x99"])
    def test_bad_id_rejected(self, lib, bad):
        with pytest.raises(ValueError):
            pl.get_source(bad)

    def test_unknown_id_rejected(self, lib):
        with pytest.raises(ValueError):
            pl.get_source("0099")

    def test_save_checks_id_exists_first(self, lib):
        with pytest.raises(ValueError):
            pl.save_source("0099", INDENTED)

    def test_index_payload_shape(self, lib):
        payload = json.loads((lib / "index.json").read_text(encoding="utf-8"))
        assert set(payload) >= {"generated", "total", "usable", "categories", "items"}


@pytest.fixture()
def queue(lib, tmp_path, monkeypatch):
    """把转写/队列目录指到临时目录；标的校验换成直通（另有用例单测真实校验）。"""
    d = tmp_path / "pine_transpile"
    monkeypatch.setattr(pl, "transpile_dir", lambda: d)
    monkeypatch.setattr(pl, "queue_dir", lambda: d / "queue")
    monkeypatch.setattr(pl, "_check_symbol", lambda s: s)
    return d


class TestBacktestQueue:
    def test_enqueue_writes_request_and_reports_queued(self, queue):
        got = pl.enqueue_backtest("0001", "600309.SH", adj="forward")
        assert got["queued"] is True
        req = json.loads((queue / "queue/0001.json").read_text(encoding="utf-8"))
        assert req["symbol"] == "600309.SH" and req["adj"] == "forward"
        # 还没开跑 → 界面看到 queued
        assert pl.job_status("0001")["status"] == "queued"

    def test_duplicate_enqueue_rejected(self, queue):
        pl.enqueue_backtest("0001", "600309.SH")
        with pytest.raises(ValueError, match="已在队列"):
            pl.enqueue_backtest("0001", "600309.SH")

    def test_running_job_rejected(self, queue):
        d = queue / "0001"
        d.mkdir(parents=True)
        (d / "job.json").write_text(json.dumps({"status": "running", "stage": "backtest"}),
                                    encoding="utf-8")
        with pytest.raises(ValueError, match="正在跑"):
            pl.enqueue_backtest("0001", "600309.SH")

    def test_unknown_id_and_bad_adj_rejected(self, queue):
        with pytest.raises(ValueError):
            pl.enqueue_backtest("0099", "600309.SH")
        with pytest.raises(ValueError):
            pl.enqueue_backtest("0001", "600309.SH", adj="前复权")

    def test_check_symbol_rejects_unknown(self, monkeypatch):
        from backend.services.agent_arena import market_lab as ml

        monkeypatch.setattr(ml, "_names", lambda: {"600309.SH": "万华化学"})
        assert pl._check_symbol("600309") == "600309.SH"
        with pytest.raises(ValueError):
            pl._check_symbol("999999.SZ")

    def test_list_transpile_skips_work_dirs(self, queue):
        """queue/ 是工作目录，不能当成一条策略冒出来。"""
        (queue / "queue").mkdir(parents=True)
        d = queue / "0001"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"title": "甲", "problems": []}),
                                     encoding="utf-8")
        got = pl.list_transpile()
        assert set(got) == {"0001"}
        assert got["0001"]["job"] == {"status": "", "stage": "", "error": ""}

    def test_read_report_missing_is_empty(self, queue):
        assert pl.read_report("0001") == {}


@pytest.fixture()
def notes(lib, tmp_path, monkeypatch):
    """备注目录指到临时库（备注独立于 index.json，另建一份）。"""
    d = tmp_path / "notes"
    monkeypatch.setattr(pl, "notes_dir", lambda: d)
    return d


class TestNotes:
    def test_missing_note_returns_empty_shape(self, notes):
        got = pl.get_note("0001")
        assert got["id"] == "0001" and got["note"] == "" and got["tags"] == []
        assert got["rating"] is None and got["status"] == "待研究"

    def test_save_then_get_roundtrip(self, notes):
        pl.save_note("0001", {"note": "趋势市有效", "tags": ["趋势", "多周期"],
                              "rating": 4, "status": "观察中"})
        got = pl.get_note("0001")
        assert got["note"] == "趋势市有效"
        assert got["tags"] == ["趋势", "多周期"]
        assert got["rating"] == 4 and got["status"] == "观察中"
        assert got["by"] == "user" and got["updated"]

    def test_template_id_accepted_even_if_not_in_index(self, notes):
        """内置模板（sma_cross）不在 Pine 索引里，也要能写备注。"""
        assert pl.save_note("sma_cross", {"note": "参数少，抗过拟合"})["id"] == "sma_cross"
        assert "sma_cross" in pl.list_notes()["ids"]

    def test_tags_accept_comma_separated_string(self, notes):
        got = pl.save_note("0001", {"tags": "趋势, 多周期 突破"})
        assert got["tags"] == ["趋势", "多周期", "突破"]

    @pytest.mark.parametrize("bad", ["../etc/passwd", "0001.pine", "", "ABC", "x" * 33])
    def test_bad_id_rejected(self, notes, bad):
        with pytest.raises(ValueError):
            pl.get_note(bad)

    @pytest.mark.parametrize("rating", [6, -1, True, "4"])
    def test_rating_must_be_int_0_to_5(self, notes, rating):
        with pytest.raises(ValueError):
            pl.save_note("0001", {"rating": rating})

    def test_unknown_status_rejected(self, notes):
        with pytest.raises(ValueError, match="未知状态"):
            pl.save_note("0001", {"status": "已跑赢"})

    def test_oversize_note_rejected(self, notes):
        with pytest.raises(ValueError, match="过长"):
            pl.save_note("0001", {"note": "字" * (pl.MAX_NOTE_CHARS + 1)})

    def test_list_notes_counts_only_saved(self, notes):
        assert pl.list_notes() == {"ids": [], "count": 0}
        pl.save_note("0001", {"note": "甲"})
        pl.save_note("0002", {"note": "乙"})
        assert pl.list_notes() == {"ids": ["0001", "0002"], "count": 2}

    def test_save_leaves_no_tmp_file(self, notes):
        pl.save_note("0001", {"note": "甲"})
        assert sorted(p.name for p in notes.iterdir()) == ["0001.json"]

    def test_note_survives_index_rebuild(self, lib, notes, tmp_path):
        """索引是整体原子重建的——备注写进 index.json 就会被下次重建抹掉。"""
        pl.save_note("0001", {"note": "重建后还得在"})
        pli.build(source=_corpus(tmp_path), lib_dir=lib)
        assert pl.get_note("0001")["note"] == "重建后还得在"


@pytest.fixture()
def chat(lib, tmp_path, monkeypatch):
    """对话目录指到临时库（与转写队列各自独立）。"""
    d = tmp_path / "chat"
    monkeypatch.setattr(pl, "chat_dir", lambda: d)
    monkeypatch.setattr(pl, "chat_queue", lambda: d / "queue")
    monkeypatch.setattr(pl, "chat_jobs", lambda: d / "jobs")
    return d


class TestChatQueue:
    def test_enqueue_writes_request_and_reads_back_as_queued(self, chat):
        got = pl.enqueue_chat("0001", "这个策略适合震荡市吗？")
        job_id = got["job_id"]
        assert pl.JOB_ID_RE.match(job_id)
        req = json.loads((chat / "queue" / f"{job_id}.json").read_text(encoding="utf-8"))
        assert req["message"] == "这个策略适合震荡市吗？" and req["mode"] == "ask"
        # worker 还没取走 → 界面看到 queued
        assert pl.chat_job("0001", job_id)["status"] == "queued"

    def test_second_message_rejected_while_pending(self, chat):
        pl.enqueue_chat("0001", "第一条")
        with pytest.raises(ValueError, match="还在处理"):
            pl.enqueue_chat("0001", "第二条")

    def test_other_strategy_not_blocked_by_pending(self, chat):
        pl.enqueue_chat("0001", "甲")
        assert pl.enqueue_chat("0002", "乙")["job_id"].startswith("0002-")

    def test_done_job_returned_and_scoped_to_strategy(self, chat):
        job_id = pl.enqueue_chat("0001", "问")["job_id"]
        (chat / "jobs").mkdir(parents=True)
        (chat / "jobs" / f"{job_id}.json").write_text(
            json.dumps({"job_id": job_id, "id": "0001", "status": "done",
                        "stage": "done", "reply": "适合趋势市"}), encoding="utf-8")
        assert pl.chat_job("0001", job_id)["reply"] == "适合趋势市"
        with pytest.raises(ValueError, match="不属于该策略"):
            pl.chat_job("0002", job_id)

    def test_unknown_job_rejected(self, chat):
        with pytest.raises(ValueError, match="任务不存在"):
            pl.chat_job("0001", "0001-20260909T120000-abcdef")

    @pytest.mark.parametrize("bad", ["../x", "0001", "0001-2026-abcdef", ""])
    def test_bad_job_id_rejected(self, chat, bad):
        with pytest.raises(ValueError):
            pl.chat_job("0001", bad)

    def test_empty_oversize_and_bad_mode_rejected(self, chat):
        with pytest.raises(ValueError, match="不能为空"):
            pl.enqueue_chat("0001", "   ")
        with pytest.raises(ValueError, match="过长"):
            pl.enqueue_chat("0001", "字" * (pl.MAX_MESSAGE_CHARS + 1))
        with pytest.raises(ValueError, match="未知模式"):
            pl.enqueue_chat("0001", "问", mode="chat")

    def test_unknown_strategy_rejected(self, chat):
        with pytest.raises(ValueError):
            pl.enqueue_chat("0099", "问")

    def test_thread_reads_jsonl_and_skips_broken_lines(self, chat):
        d = chat / "0001"
        d.mkdir(parents=True)
        (d / "thread.jsonl").write_text(
            json.dumps({"role": "user", "content": "问"}, ensure_ascii=False) + "\n"
            + '{"role": "assistant", "content": "半截' + "\n"      # 写一半的坏行
            + json.dumps({"role": "assistant", "content": "答"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        got = pl.chat_thread("0001")
        assert got["count"] == 2
        assert [m["content"] for m in got["messages"]] == ["问", "答"]

    def test_empty_thread_is_empty(self, chat):
        assert pl.chat_thread("0001") == {"id": "0001", "count": 0, "messages": []}


@pytest.fixture()
def staged(lib, chat, tmp_path, monkeypatch):
    """采用/回滚环境：版本目录 + 转写队列都指到临时目录，标的校验直通。"""
    monkeypatch.setattr(pl, "versions_dir", lambda: tmp_path / "versions")
    monkeypatch.setattr(pl, "transpile_dir", lambda: tmp_path / "pine_transpile")
    monkeypatch.setattr(pl, "queue_dir", lambda: tmp_path / "pine_transpile/queue")
    monkeypatch.setattr(pl, "_check_symbol", lambda s: s)
    return lib


def _stage_candidate(item_id: str, job_id: str, pine: str = "//@version=5\nstrategy('新')\n",
                     problems: list | None = None, report: dict | None = None) -> Path:
    """造一条跑完的候选（worker 的产物形态）。"""
    d = pl.chat_dir() / item_id / job_id
    d.mkdir(parents=True, exist_ok=True)
    (d / pl.CANDIDATE_PINE).write_text(pine, encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"problems": problems or []}), encoding="utf-8")
    (d / "report.json").write_text(json.dumps(report or {
        "symbol": "600309.SH", "adj": "backward", "trades": 12,
        "stats": {"Net profit": {"pct": 5.0}}}), encoding="utf-8")
    pl.chat_jobs().mkdir(parents=True, exist_ok=True)
    (pl.chat_jobs() / f"{job_id}.json").write_text(json.dumps({
        "job_id": job_id, "id": item_id, "status": "done", "stage": "done",
        "problems": problems or []}), encoding="utf-8")
    return d


class TestCandidateApply:
    JOB = "0001-20260909T120000-abcdef"

    def test_apply_snapshots_then_writes_edited(self, staged):
        before = pl.get_source("0001")["source"]
        _stage_candidate("0001", self.JOB)

        got = pl.apply_candidate("0001", self.JOB)

        assert got["applied"] and got["snapshot"]
        assert pl.get_source("0001")["source"] == "//@version=5\nstrategy('新')\n"
        assert (pl.edited_dir() / "0001.pine").exists()
        snap = pl.versions_dir() / "0001" / f"{got['snapshot']}.pine"
        assert snap.read_text(encoding="utf-8") == before      # 快照存的是改动前那版
        # 采用后自动重跑官方产物，且必须 force（否则会复用旧候选）
        q = json.loads((pl.queue_dir() / "0001.json").read_text(encoding="utf-8"))
        assert q["force"] is True and q["symbol"] == "600309.SH"
        assert got["queued"] is True and got["queue_error"] == ""

    def test_apply_rejects_unfinished_job(self, staged):
        pl.chat_jobs().mkdir(parents=True, exist_ok=True)
        (pl.chat_jobs() / f"{self.JOB}.json").write_text(json.dumps({
            "job_id": self.JOB, "id": "0001", "status": "running"}), encoding="utf-8")
        with pytest.raises(ValueError, match="还没跑完"):
            pl.apply_candidate("0001", self.JOB)

    def test_apply_rejects_candidate_that_failed_the_gate(self, staged):
        _stage_candidate("0001", self.JOB, problems=["仓位口径必须与库内统一"])
        with pytest.raises(ValueError, match="静态检查"):
            pl.apply_candidate("0001", self.JOB)
        assert not (pl.edited_dir() / "0001.pine").exists()

    def test_apply_still_lands_when_queue_is_busy(self, staged):
        _stage_candidate("0001", self.JOB)
        pl.queue_dir().mkdir(parents=True, exist_ok=True)
        (pl.queue_dir() / "0001.json").write_text("{}", encoding="utf-8")

        got = pl.apply_candidate("0001", self.JOB)

        assert got["applied"] and got["queued"] is False
        assert "队列" in got["queue_error"]
        assert (pl.edited_dir() / "0001.pine").exists()          # 采用照样生效

    def test_apply_without_symbol_reports_why_not_requeued(self, staged):
        _stage_candidate("0001", self.JOB, report={"symbol": "", "stats": {}})
        got = pl.apply_candidate("0001", self.JOB)
        assert got["queued"] is False and "没有标的" in got["queue_error"]

    def test_candidate_reads_pine_meta_report(self, staged):
        _stage_candidate("0001", self.JOB, pine="//@version=5\nstrategy('候选')\n")
        got = pl.candidate("0001", self.JOB)
        assert "strategy('候选')" in got["pine"]
        assert got["meta"]["problems"] == [] and got["report"]["trades"] == 12

    def test_missing_candidate_rejected(self, staged):
        with pytest.raises(ValueError, match="没有候选源码"):
            pl.candidate("0001", self.JOB)


class TestRevert:
    JOB = "0001-20260909T120000-abcdef"

    def test_revert_swaps_versions_so_it_is_reversible(self, staged):
        before = pl.get_source("0001")["source"]
        _stage_candidate("0001", self.JOB)
        pl.apply_candidate("0001", self.JOB)
        new = pl.get_source("0001")["source"]

        got = pl.revert_source("0001")

        assert got["reverted"] and got["remaining"] == 1
        assert pl.get_source("0001")["source"] == before
        # 还原成与语料一致 → 覆盖文件删掉（否则界面永远挂着「已编辑」）
        assert not (pl.edited_dir() / "0001.pine").exists()
        # 刚采用的那版也留了快照 → 再点一次能退回去（回滚不是单向的）
        pl.revert_source("0001")
        assert pl.get_source("0001")["source"] == new
        assert (pl.edited_dir() / "0001.pine").exists()

    def test_revert_to_hand_edited_version_keeps_override(self, staged):
        pl.save_source("0001", "//@version=5\nstrategy('手工改过的')\n")
        _stage_candidate("0001", self.JOB)
        pl.apply_candidate("0001", self.JOB)

        pl.revert_source("0001")

        # 回滚目标是「手改版」而不是原始语料 → 必须继续以 edited/ 生效
        assert (pl.edited_dir() / "0001.pine").read_text(encoding="utf-8") == \
            "//@version=5\nstrategy('手工改过的')\n"

    def test_revert_without_snapshot_rejected(self, staged):
        with pytest.raises(ValueError, match="没有可回滚"):
            pl.revert_source("0001")

    def test_revert_specific_version(self, staged):
        d = pl.versions_dir() / "0001"
        d.mkdir(parents=True, exist_ok=True)
        (d / "20260909T010000000000.pine").write_text("旧一版", encoding="utf-8")
        (d / "20260909T020000000000.pine").write_text("旧两版", encoding="utf-8")

        got = pl.revert_source("0001", "20260909T010000000000")

        assert got["reverted"] == "20260909T010000000000"
        assert pl.get_source("0001")["source"] == "旧一版"
        # 旧两版还在，且刚被换下的那版也留了快照
        assert pl.list_versions("0001")["count"] == 2

    @pytest.mark.parametrize("bad", ["../x", "2026", "20260909T01000000000"])
    def test_bad_version_rejected(self, staged, bad):
        with pytest.raises(ValueError):
            pl.revert_source("0001", bad)

    def test_list_versions_ignores_junk(self, staged):
        d = pl.versions_dir() / "0001"
        d.mkdir(parents=True, exist_ok=True)
        (d / "20260909T010000000000.pine").write_text("x", encoding="utf-8")
        (d / "note.txt").write_text("x", encoding="utf-8")
        (d / "latest.pine").write_text("x", encoding="utf-8")
        got = pl.list_versions("0001")
        assert [v["version"] for v in got["versions"]] == ["20260909T010000000000"]
