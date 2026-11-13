"""Pine 转写/回测队列 worker：沙箱命令构造 + 队列消费状态机。

worker 是唯一执行模型产出代码的地方，所以这里锁三件事：
  1. 回测段必须包在 bwrap 里（只读根 + 断网 + 仅策略目录可写），缺 bwrap 就拒跑；
  2. 静态检查没过的候选永远不进回测；
  3. 每一步状态都落到 job.json（界面靠它轮询）。

运行（容器）：python3 -m pytest backend/tests/test_pine_transpile_worker.py -q
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import pine_transpile_worker as w  # noqa: E402


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    d = tmp_path / "pine_transpile"
    (d / "queue").mkdir(parents=True)
    monkeypatch.setattr(w, "OUT_DIR", d)
    monkeypatch.setattr(w, "QUEUE_DIR", d / "queue")
    monkeypatch.setattr(w, "LOCK_PATH", d / "worker.lock")
    return d


def _job(out_dir: Path, item_id: str) -> dict:
    return json.loads((out_dir / item_id / "job.json").read_text(encoding="utf-8"))


class TestSandboxCmd:
    def test_wraps_in_bwrap_without_network(self, tmp_path, monkeypatch):
        # 容器里没有 bwrap（宿主专属，见 docker/Dockerfile）——这里只验证命令构造，
        # 把 which 打桩成宿主路径；「缺 bwrap 拒跑」由下一个用例单独锁。
        monkeypatch.setattr(w.shutil, "which", lambda _: "/usr/bin/bwrap")
        cmd = w.sandbox_cmd(tmp_path, ["--id", "0001", "--no-transpile"])
        assert cmd[0].endswith("bwrap")
        assert "--unshare-net" in cmd
        assert "--ro-bind" in cmd and "/" in cmd
        # 只有策略目录可写
        i = cmd.index("--bind")
        assert cmd[i + 1] == str(tmp_path) == cmd[i + 2]
        # 真正的命令在最后，且是 --no-transpile 那条
        assert cmd[-4:] == [str(w.CLI), "--id", "0001", "--no-transpile"]

    def test_bind_resolves_symlinked_item_dir(self, tmp_path, monkeypatch):
        """data/ 是符号链接（QM 部署形态）：--bind 必须绑真实路径。

        bwrap 为挂载点建父目录时不穿符号链接，绑 ``…/data/…``（软链在中间）会直接
        报 ``Can't mkdir parents … No such file or directory``——实测 0001 首跑即此错。
        """
        monkeypatch.setattr(w.shutil, "which", lambda _: "/usr/bin/bwrap")
        real = tmp_path / "real" / "0001"
        real.mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(tmp_path / "real")
        cmd = w.sandbox_cmd(link / "0001", ["--id", "0001"])
        i = cmd.index("--bind")
        assert cmd[i + 1] == str(real) == cmd[i + 2]

    def test_refuses_without_bwrap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(w.shutil, "which", lambda _: None)
        with pytest.raises(RuntimeError, match="bwrap"):
            w.sandbox_cmd(tmp_path, ["--id", "0001"])


class TestProcess:
    def test_static_failure_never_reaches_backtest(self, out_dir, monkeypatch):
        """静态检查没过的候选永远不进沙箱——修不好也只能停在静态阶段。"""
        calls: list[list[str]] = []

        def fake_run(argv, timeout):
            calls.append(argv)
            d = out_dir / "0001"
            d.mkdir(parents=True, exist_ok=True)
            (d / "meta.json").write_text(
                json.dumps({"title": "甲", "problems": ["禁止动态执行：eval("]}),
                encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="静态检查未通过", stderr="")

        monkeypatch.setattr(w, "_run", fake_run)
        monkeypatch.setattr(w, "sandbox_cmd", lambda *a, **k: pytest.fail("不该进回测"))

        job = w.process({"id": "0001", "symbol": "600309.SH"})
        assert job["status"] == "failed" and job["stage"] == "static"
        assert job["problems"] == ["禁止动态执行：eval("]
        # 转写一次 + 修复 MAX_REPAIRS 次；一次回测都没跑
        assert len(calls) == 1 + w.MAX_REPAIRS
        assert all(r[0] != "bwrap" for r in calls)

    def test_initial_static_problems_get_repaired_then_backtested(self, out_dir, monkeypatch):
        """首轮语法错误也要修（30 条抽样里 8 条卡在这），修好再进沙箱。"""
        runs: list[list[str]] = []

        def fake_run(argv, timeout):
            runs.append(argv)
            d = out_dir / "0009"
            d.mkdir(parents=True, exist_ok=True)
            if argv[0] == "bwrap":
                return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
            repaired = any("--repair" in r for r in runs)
            (d / "meta.json").write_text(json.dumps(
                {"problems": [] if repaired else ["语法错误：invalid syntax（第 3 行）"]}),
                encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

        monkeypatch.setattr(w, "_run", fake_run)
        monkeypatch.setattr(w, "sandbox_cmd", lambda item_dir, argv: ["bwrap", *argv])

        job = w.process({"id": "0009", "symbol": "600309.SH"})
        assert job["status"] == "done"
        assert [r[0] for r in runs] == [w.sys.executable, w.sys.executable, "bwrap"]

    def test_happy_path_runs_transpile_then_sandboxed_backtest(self, out_dir, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(argv, timeout):
            calls.append(argv)
            d = out_dir / "0001"
            d.mkdir(parents=True, exist_ok=True)
            if len(calls) == 1:                       # 转写段
                (d / "meta.json").write_text(
                    json.dumps({"title": "甲", "problems": []}), encoding="utf-8")
            else:                                     # 回测段
                (d / "report.json").write_text(
                    json.dumps({"symbol": "600309.SH", "trades": 3, "stats": {}}),
                    encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

        monkeypatch.setattr(w, "_run", fake_run)
        monkeypatch.setattr(w, "sandbox_cmd", lambda item_dir, argv: ["bwrap", *argv])

        job = w.process({"id": "0001", "symbol": "600309.SH", "adj": "forward"})
        assert job["status"] == "done" and job["stage"] == "done"
        assert len(calls) == 2
        assert calls[1][0] == "bwrap" and "--no-transpile" in calls[1]
        assert "--adj" in calls[1] and calls[1][calls[1].index("--adj") + 1] == "forward"

    def test_timeout_marks_failed(self, out_dir, monkeypatch):
        def fake_run(argv, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)

        monkeypatch.setattr(w, "_run", fake_run)
        job = w.process({"id": "0002", "symbol": "600309.SH"})
        assert job["status"] == "failed" and "超时" in job["error"]

    def test_missing_id_rejected(self, out_dir):
        with pytest.raises(ValueError):
            w.process({"symbol": "600309.SH"})

    def test_backtest_failure_feeds_traceback_back_to_model(self, out_dir, monkeypatch):
        """回测炸了不能只报 exit code——把 traceback 喂回模型修，最多 MAX_REPAIRS 轮。"""
        runs: list[list[str]] = []

        def fake_run(argv, timeout):
            runs.append(argv)
            d = out_dir / "0003"
            d.mkdir(parents=True, exist_ok=True)
            (d / "meta.json").write_text(json.dumps({"problems": []}), encoding="utf-8")
            if argv[0] == "bwrap":
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="Traceback...\nTypeError: 'float' object")
            return subprocess.CompletedProcess(argv, 0, stdout="修复完成", stderr="")

        monkeypatch.setattr(w, "_run", fake_run)
        monkeypatch.setattr(w, "sandbox_cmd", lambda item_dir, argv: ["bwrap", *argv])

        job = w.process({"id": "0003", "symbol": "600309.SH"})
        assert job["status"] == "failed" and job["stage"] == "backtest"
        assert job["error"] == "TypeError: 'float' object"
        repairs = [r for r in runs if "--repair" in r]
        assert len(repairs) == w.MAX_REPAIRS          # 修复次数有上限
        assert "TypeError" in repairs[0][repairs[0].index("--repair") + 1]

    def test_repair_recovers_to_done(self, out_dir, monkeypatch):
        runs: list[list[str]] = []

        def fake_run(argv, timeout):
            runs.append(argv)
            d = out_dir / "0006"
            d.mkdir(parents=True, exist_ok=True)
            (d / "meta.json").write_text(json.dumps({"problems": []}), encoding="utf-8")
            if argv[0] == "bwrap":
                second = sum(1 for r in runs if r[0] == "bwrap") > 1
                return subprocess.CompletedProcess(argv, 0 if second else 1,
                                                   stdout="ok" if second else "",
                                                   stderr="" if second else "TypeError: boom")
            return subprocess.CompletedProcess(argv, 0, stdout="修复完成", stderr="")

        monkeypatch.setattr(w, "_run", fake_run)
        monkeypatch.setattr(w, "sandbox_cmd", lambda item_dir, argv: ["bwrap", *argv])

        job = w.process({"id": "0006", "symbol": "600309.SH"})
        assert job["status"] == "done"
        assert sum(1 for r in runs if "--repair" in r) == 1

    def test_repair_cli_failure_marks_failed(self, out_dir, monkeypatch):
        def fake_run(argv, timeout):
            d = out_dir / "0007"
            d.mkdir(parents=True, exist_ok=True)
            (d / "meta.json").write_text(json.dumps({"problems": []}), encoding="utf-8")
            if argv[0] == "bwrap":
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
            if "--repair" in argv:
                return subprocess.CompletedProcess(
                    argv, 3, stdout="修复失败：模型输出被 max_tokens 截断", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="复用已有候选", stderr="")

        monkeypatch.setattr(w, "_run", fake_run)
        monkeypatch.setattr(w, "sandbox_cmd", lambda item_dir, argv: ["bwrap", *argv])

        job = w.process({"id": "0007", "symbol": "600309.SH"})
        assert job["status"] == "failed" and job["stage"] == "repair"
        assert job["error"] == "修复失败：模型输出被 max_tokens 截断"

    def test_static_problems_after_repair_reported(self, out_dir, monkeypatch):
        """修完语法又坏了：报 stage=static 并把 problems 带上，界面才说得清。

        repair CLI 此时 exit 2（修了但仍有静态问题）——必须继续下一轮，不能当失败退出。
        """
        runs: list[list[str]] = []

        def fake_run(argv, timeout):
            runs.append(argv)
            d = out_dir / "0008"
            d.mkdir(parents=True, exist_ok=True)
            if argv[0] == "bwrap":
                return subprocess.CompletedProcess(argv, 2, stdout="静态检查未通过", stderr="")
            problems = (["语法错误：invalid syntax（第 3 行）"] if "--repair" in argv else [])
            (d / "meta.json").write_text(json.dumps({"problems": problems}), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 2 if "--repair" in argv else 0,
                                               stdout="ok", stderr="")

        monkeypatch.setattr(w, "_run", fake_run)
        monkeypatch.setattr(w, "sandbox_cmd", lambda item_dir, argv: ["bwrap", *argv])

        job = w.process({"id": "0008", "symbol": "600309.SH"})
        assert job["status"] == "failed" and job["stage"] == "static"
        assert job["problems"] == ["语法错误：invalid syntax（第 3 行）"]
        assert sum(1 for r in runs if "--repair" in r) == w.MAX_REPAIRS


class TestLock:
    def test_second_worker_skips(self, out_dir):
        first = w._claim_lock()
        assert first is not None
        try:
            assert w._claim_lock() is None
        finally:
            first.close()


class TestLastLine:
    def test_picks_last_non_empty(self):
        assert w._last_line("a\n\n  b  \n") == "b"

    def test_empty_is_empty(self):
        assert w._last_line("") == "" and w._last_line(None) == ""


class TestFailureDetail:
    """失败原因要能直接给用户看，不能只剩 exit code。"""

    def test_transpile_failure_surfaces_cli_message(self, out_dir, monkeypatch):
        def fake_run(argv, timeout):
            return subprocess.CompletedProcess(
                argv, 3, stdout="转写失败：模型输出被 max_tokens 截断\n", stderr="")

        monkeypatch.setattr(w, "_run", fake_run)
        job = w.process({"id": "0001", "symbol": "600309.SH"})
        assert job["status"] == "failed" and job["stage"] == "transpile"
        assert job["error"] == "转写失败：模型输出被 max_tokens 截断"

    def test_silent_failure_falls_back_to_exit_code(self, out_dir, monkeypatch):
        def fake_run(argv, timeout):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

        monkeypatch.setattr(w, "_run", fake_run)
        assert w.process({"id": "0005", "symbol": "600309.SH"})["error"] == "转写失败（exit 1）"


class TestMain:
    def test_consumes_queue_and_clears_request(self, out_dir, monkeypatch):
        (out_dir / "queue/0001.json").write_text(
            json.dumps({"id": "0001", "symbol": "600309.SH"}), encoding="utf-8")
        monkeypatch.setattr(w, "process",
                            lambda req, **kw: {"status": "done", "stage": "done"})
        assert w.main() == 0
        assert not (out_dir / "queue/0001.json").exists()   # 摘牌

    def test_broken_request_is_dropped(self, out_dir):
        (out_dir / "queue/0001.json").write_text("{ 不是 json", encoding="utf-8")
        assert w.main() == 0
        assert not (out_dir / "queue/0001.json").exists()


class TestCandidateRedirect:
    """对话改策略的候选：同一套 CLI/闸门/沙箱，只把路径改道。"""

    def _fake(self, monkeypatch, calls: list, seen_dirs: list):
        def fake_run(argv, timeout):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

        monkeypatch.setattr(w, "_run", fake_run)
        monkeypatch.setattr(w, "sandbox_cmd",
                            lambda item_dir, argv: seen_dirs.append(item_dir) or ["bwrap", *argv])

    def test_source_and_out_reach_the_cli(self, out_dir, tmp_path, monkeypatch):
        cand = tmp_path / "chat/0001/job1"
        calls: list[list[str]] = []
        seen: list[Path] = []
        self._fake(monkeypatch, calls, seen)
        pine = cand / "candidate.pine"

        job = w.process({"id": "0001", "symbol": "600309.SH", "source": str(pine)},
                        item_dir=cand, job_path=tmp_path / "chat/jobs/job1.json")

        assert job["status"] == "done"
        assert calls[0][calls[0].index("--out") + 1] == str(cand)
        assert calls[0][calls[0].index("--source") + 1] == str(pine)
        # 三段调用（转写/沙箱/修复）argv 一致：--no-transpile 不读 Pine，带了也无害
        assert calls[1][calls[1].index("--out") + 1] == str(cand)
        assert calls[1][calls[1].index("--source") + 1] == str(pine)
        assert seen == [cand]                      # 沙箱只把候选目录挂成可写

    def test_official_job_json_untouched(self, out_dir, tmp_path, monkeypatch):
        """候选进度由对话 worker 记，正式 job.json 不能被候选覆写。"""
        cand = tmp_path / "chat/0001/job1"
        job_path = tmp_path / "chat/jobs/job1.json"
        calls: list[list[str]] = []
        self._fake(monkeypatch, calls, [])

        w.process({"id": "0001", "symbol": "600309.SH"}, item_dir=cand, job_path=job_path)

        assert json.loads(job_path.read_text(encoding="utf-8"))["status"] == "done"
        assert not (out_dir / "0001" / "job.json").exists()

    def test_official_job_written_when_path_given(self, out_dir, monkeypatch):
        calls: list[list[str]] = []
        self._fake(monkeypatch, calls, [])
        path = out_dir / "0001" / "job.json"

        w.process({"id": "0001", "symbol": "600309.SH"}, job_path=path)

        assert _job(out_dir, "0001")["status"] == "done"
        assert calls[0][calls[0].index("--out") + 1] == str(out_dir / "0001")

    def test_main_points_job_at_official_path(self, out_dir, monkeypatch):
        """队列主循环自己决定 job 落点，别让 process 猜。"""
        (out_dir / "queue/0001.json").write_text(
            json.dumps({"id": "0001", "symbol": "600309.SH"}), encoding="utf-8")
        seen: dict = {}

        def fake_process(req, **kw):
            seen.update(kw)
            return {"status": "done", "stage": "done"}

        monkeypatch.setattr(w, "process", fake_process)
        assert w.main() == 0
        assert seen["job_path"] == out_dir / "0001" / "job.json"
