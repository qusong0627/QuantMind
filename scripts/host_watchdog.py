#!/usr/bin/env python3
"""主机存活看门狗（2026-09-18）。

背景（2026-09-18 实录）：交易主机在盘中冻结/休眠——上一 boot 日志停在 JST 11:28
（北京 10:28），15:07 才重启；期间**止损哨兵、整点轮、新闻管道、全部告警停摆**，
恢复后零告警（用户是靠"新闻停更 19 小时"才发现）。cron 在冻结期间不执行、恢复后
也不会补跑错过的轮次，系统表面看起来一切正常。

口径：cron 每分钟执行一次本脚本：
  - 每次运行把 {ts, boot_id, pid} 原子写 logs/host_heartbeat.json；
  - 本次运行发现「距上次心跳的 gap」超阈值 → 判定主机曾离线，推 QQ 告警
    （含离线时长、时段、重启/冻结判别、停摆链路清单与人工动作提示），
    并追加一行到 logs/host_gaps.jsonl 供日报/复盘消费；
  - 先推送、后写新心跳：推送失败时下一分钟会重试（宁可重复，不可丢失）。
阈值（保守起步，可再调）：
  - 交易日白天（北京 09:00-15:30）：gap ≥ 3 分钟即报——哨兵/通道价值最高的时段；
  - 其余时段：gap ≥ 30 分钟才报——夜间维护/休眠属常见，少打扰。
--boot（@reboot 调用）：与普通模式同逻辑；boot_id 不同 → 报「主机重启」。

心跳写失败留痕（2026-09-21 盘满事故）：ENOSPC 期间心跳自己写不进去 → gap 虚涨，
磁盘腾出后误报「主机离线 177 分钟」（journal 显示主机全程活着）。写失败时用
**定长模板 + 同长原地覆写**留痕（盘满到 100% 时新建/扩展都失败，覆盖已分配的
旧块可以成功）；恢复后若留痕时间落在 gap 窗口内 → 归因改为「心跳写入失败」，
不再谎报主机离线。boot_id 变化（真重启）优先级最高。

绝不阻断：任何异常都退 0（cron 每分钟跑；报错写 stderr）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HB_FILE = ROOT / "logs" / "host_heartbeat.json"
GAP_LOG = ROOT / "logs" / "host_gaps.jsonl"
WF_FILE = ROOT / "logs" / "host_heartbeat.writefail.json"
WF_SIZE = 256            # 留痕模板定长（字节）；同长覆写不申请新块
BJ = timezone(timedelta(hours=8))
TRADING_GAP_MIN = 3      # 交易日白天阈值
OFFHOURS_GAP_MIN = 30    # 其余时段阈值

# ── 内存告急告警（2026-11-05 崩机事故后加）──────────────────────────────
# 那天 22:07 起 OOM 连环杀（orca-client / tigervnc ×2 / orca-daemon），22:14 dockerd
# 健康检查全部超时，22:23 硬崩、停机 11 小时；05:20–05:50 还有过一轮 l2-panel-full
# 被反复 OOM。内核 OOM-kill 是**结果**，机器在它之前就已经吃紧了——所以这里在水位
# 掉到阈值时就喊人，而不是等内核动手。
# 阈值 8GB：本机 60GB 常态可用 23–26GB，L2 面板夜跑会压到 10–15GB；8GB 之下基本
# 已经进入抖动区，再低就该动手停重活了。
MEM_ALERT_GB = float(os.getenv("QM_MEM_ALERT_GB") or 8)
MEM_ALERT_COOLDOWN_MIN = int(os.getenv("QM_MEM_ALERT_COOLDOWN_MIN") or 30)
MEM_STATE = ROOT / "logs" / "host_mem_alert.json"


def now_bj() -> datetime:
    return datetime.now(BJ)


def boot_id() -> str:
    """本次开机的 boot_id（内核随机串）；读不到返回空串（退化：只按 gap 判）。"""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def in_trading_daytime(now: datetime) -> bool:
    """交易日(退化为周一~五)白天：北京 09:00-15:30。"""
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return 9 * 60 <= m < 15 * 60 + 30


def prev_ts(prev: dict) -> datetime | None:
    """上次心跳时间戳 → 北京 aware datetime；坏/缺 → None。"""
    try:
        t = datetime.fromisoformat(str(prev.get("ts") or ""))
    except ValueError:
        return None
    return t.replace(tzinfo=BJ) if t.tzinfo is None else t.astimezone(BJ)


def classify_gap(prev: dict, now: datetime, cur_boot: str) -> tuple[float, str, float] | None:
    """读上次心跳 → (gap 分钟, 模式 reboot/freeze, 阈值)；无前值/时间戳坏 → None。"""
    t = prev_ts(prev)
    if t is None:
        return None
    gap = (now - t).total_seconds() / 60.0
    if gap < 0:
        return None                                  # 时钟回拨：不判
    mode = "reboot" if (cur_boot and prev.get("boot_id")
                        and prev.get("boot_id") != cur_boot) else "freeze"
    thresh = TRADING_GAP_MIN if in_trading_daytime(t) or in_trading_daytime(now) \
        else OFFHOURS_GAP_MIN
    return gap, mode, thresh


def write_heartbeat(now: datetime, cur_boot: str) -> None:
    """原子写心跳（tmp+replace）；失败向上抛 OSError（调用方留痕 + 下分钟重试）。"""
    HB_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HB_FILE.with_name(HB_FILE.name + ".tmp")
    tmp.write_text(json.dumps(
        {"ts": now.isoformat(), "boot_id": cur_boot, "pid": os.getpid()},
        ensure_ascii=False), encoding="utf-8")
    tmp.replace(HB_FILE)


def ensure_marker_template() -> None:
    """正常时预分配定长留痕模板——盘满时才能同长原地覆写。失败静默（有兜底重试）。"""
    try:
        if WF_FILE.stat().st_size == WF_SIZE:
            return
    except OSError:
        pass
    try:
        WF_FILE.parent.mkdir(parents=True, exist_ok=True)
        WF_FILE.write_text(json.dumps({"ts": "", "err": ""},
                                      ensure_ascii=True).ljust(WF_SIZE),
                           encoding="utf-8")
    except OSError:
        pass


def write_fail_marker(err, now: datetime) -> None:
    """心跳写失败留痕：定长模板上同长原地覆写（ensure_ascii 保证字节数=字符数）。"""
    payload = json.dumps({"ts": now.isoformat(), "err": str(err)[:120]},
                         ensure_ascii=True)
    if len(payload) > WF_SIZE:                       # 极端长错误串：保 ts 弃 err
        payload = json.dumps({"ts": now.isoformat(), "err": ""}, ensure_ascii=True)
    try:
        with WF_FILE.open("r+", encoding="utf-8") as f:   # 首选：同长覆写不占新块
            f.write(payload.ljust(WF_SIZE))
        return
    except OSError:
        pass
    try:                                             # 模板还没有（首次就撞盘满）：
        WF_FILE.parent.mkdir(parents=True, exist_ok=True)   # 退化为新建，尽力而为
        WF_FILE.write_text(payload.ljust(WF_SIZE), encoding="utf-8")
    except OSError:
        pass


def writefail_in_window(now: datetime, window_from: datetime | None) -> str | None:
    """留痕时间落在 gap 窗口内 → 返回 err 文本（离线判定不可信）；否则/读不到 → None。"""
    if window_from is None:
        return None
    try:
        doc = json.loads(WF_FILE.read_text(encoding="utf-8"))
        t = datetime.fromisoformat(str(doc.get("ts") or ""))
    except (OSError, ValueError, TypeError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=BJ)
    if window_from <= t.astimezone(BJ) <= now:
        return str(doc.get("err") or "") or "写入失败"
    return None


def render(gap: float, mode: str, prev: dict, now: datetime,
           wf: str | None = None) -> str:
    """告警文案：离线时长/时段/判别 + 停摆链路 + 人工动作。"""
    t0 = str(prev.get("ts") or "?")[5:16].replace("T", " ")
    t1 = now.strftime("%m-%d %H:%M")
    covered_trading = in_trading_daytime(now) or (
        mode != "reboot" and in_trading_daytime(now - timedelta(minutes=gap)))
    if mode == "writefail":
        msg = (f"心跳写入失败约 {gap:.0f} 分钟（北京 {t0} → {t1}，磁盘满/权限类故障）\n"
               f"期间心跳未能落盘（{wf}）——主机是否在线**无法据此判断**，告警本身"
               f"也可能没发出去；请先清理磁盘，再核对窗口内哨兵/轮次是否实际停摆")
        if covered_trading:
            msg += ("\n⚠️ 覆盖了交易时段——请人工核对窗口内该止损/减仓而未动的持仓")
        return msg
    kind = "重启" if mode == "reboot" else "冻结/休眠"
    msg = (f"交易主机曾离线约 {gap:.0f} 分钟（北京 {t0} → {t1}，{kind}）\n"
           f"离线期间：止损哨兵 / 整点轮 / 新闻管道 / 全部告警停摆")
    if covered_trading:
        msg += ("\n⚠️ 覆盖了交易时段——请核对离线窗口内触及止损/止盈位的持仓，"
                "必要时人工处置")
    return msg


def mem_available_gb() -> float | None:
    """``/proc/meminfo`` 的 MemAvailable（GB）；读不到返回 None（不猜）。"""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def top_rss(limit: int = 5) -> list[str]:
    """内存占用前 N 的进程——告警里直接给出「谁在吃」，不然收到消息还得自己查。"""
    rows: list[tuple[int, str]] = []
    try:
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                status = (proc / "status").read_text(encoding="utf-8")
                rss_kb = next(
                    int(line.split()[1])
                    for line in status.splitlines()
                    if line.startswith("VmRSS:")
                )
                cmd = (
                    (proc / "cmdline")
                    .read_bytes()
                    .replace(b"\0", b" ")
                    .decode("utf-8", "replace")
                    .strip()
                )
            except (OSError, StopIteration, ValueError, IndexError):
                continue
            rows.append((rss_kb, cmd[:90]))
    except OSError:
        return []
    rows.sort(reverse=True)
    return [f"{rss / 1024 / 1024:.1f}GB {cmd}" for rss, cmd in rows[:limit]]


def memory_pressure_alert(now: datetime, *, dry_run: bool) -> str | None:
    """可用内存低于阈值 → 推 QQ 告警（带冷却）；否则保持安静。

    冷却的意义：内存风暴会持续几分钟到几十分钟，每分钟推一条只会让人把告警静音，
    那正是最需要它响的时候。冷却期内只写 stdout（cron 日志里仍留痕）。
    """
    avail = mem_available_gb()
    if avail is None or avail >= MEM_ALERT_GB:
        return None
    last: datetime | None = None
    try:
        state = json.loads(MEM_STATE.read_text(encoding="utf-8"))
        last = datetime.fromisoformat(str(state.get("last_alert") or ""))
    except (OSError, json.JSONDecodeError, ValueError):
        last = None
    cooldown_active = False
    if last is not None:
        try:
            cooldown_active = (now - last) < timedelta(minutes=MEM_ALERT_COOLDOWN_MIN)
        except TypeError:  # 状态文件被写坏（naive 时间戳）：当作没有上次
            cooldown_active = False
    if cooldown_active:
        print(
            f"[{now:%F %T}] 🟠 内存仍低（{avail:.1f}GB < {MEM_ALERT_GB:.0f}GB），"
            f"冷却中不重复推（上次 {last:%F %T}）"
        )
        return None
    body = (
        f"可用内存 {avail:.1f}GB，低于阈值 {MEM_ALERT_GB:.0f}GB。\n"
        f"{MEM_ALERT_COOLDOWN_MIN} 分钟内不重复推。\n\n"
        "2026-11-05 就是这样崩的：22:07 起 OOM 连环杀、22:14 dockerd 健康检查全超时、"
        "22:23 硬崩，停机 11 小时（当晚 CN 同步与次日批量推理全丢）。\n\n"
        "占用前 5：\n" + "\n".join(f"· {t}" for t in top_rss()) + "\n\n"
        "处理：先看 l2-panel 夜跑（23:00–05:50 每 10 分钟检查）与训练/因子计算是否在跑，"
        "必要时停一轮，别等内核 OOM。"
    )
    if dry_run:
        return body
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from push_notify import notify

        notify("🟠 主机内存告急", body)
    except Exception as exc:  # noqa: BLE001 - 推不出去下分钟重试（不写状态）
        print(f"⚠️ 内存告警推送失败（下分钟重试）: {exc}", file=sys.stderr)
        return None
    try:
        MEM_STATE.write_text(json.dumps({"last_alert": now.isoformat()}), encoding="utf-8")
    except OSError:
        pass
    return body


def main() -> int:
    ap = argparse.ArgumentParser(description="主机存活看门狗")
    ap.add_argument("--boot", action="store_true", help="开机模式（@reboot 调用）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送不落盘")
    args = ap.parse_args()

    try:
        now = now_bj()
        cur_boot = boot_id()
        prev = {}
        try:
            d = json.loads(HB_FILE.read_text(encoding="utf-8"))
            prev = d if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError):
            prev = {}

        verdict = classify_gap(prev, now, cur_boot) if prev else None
        if verdict:
            gap, mode, thresh = verdict
            if gap >= thresh:
                wf = None
                if mode != "reboot":                  # 重启判别优先于写失败留痕
                    wf = writefail_in_window(now, prev_ts(prev))
                    if wf is not None:
                        mode = "writefail"
                msg = render(gap, mode, prev, now, wf=wf)
                print(f"[{now:%F %T}] 🚨 {msg}")
                if not args.dry_run:
                    try:
                        GAP_LOG.parent.mkdir(parents=True, exist_ok=True)
                        with GAP_LOG.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(
                                {"ts": now.isoformat(), "gap_min": round(gap, 1),
                                 "mode": mode, "from": prev.get("ts")},
                                ensure_ascii=False) + "\n")
                    except OSError:
                        pass
                    try:
                        sys.path.insert(0, str(ROOT / "scripts"))
                        from push_notify import notify
                        notify("🔴 心跳写入失败告警" if mode == "writefail"
                               else "🔴 主机离线告警", msg)
                    except Exception as exc:  # noqa: BLE001  推送失败下分钟重试
                        print(f"⚠️ 离线告警推送失败（下分钟重试）: {exc}",
                              file=sys.stderr)
                        return 0                      # 不推进心跳 → 下分钟重发
        if not args.dry_run:
            try:
                write_heartbeat(now, cur_boot)
                ensure_marker_template()
            except OSError as exc:                    # 盘满等：留痕 + 下分钟重试
                print(f"⚠️ 心跳写入失败（已留痕，下分钟重试）: {exc}",
                      file=sys.stderr)
                write_fail_marker(exc, now)
        # 内存告急放在心跳之后：内存压力不该拖累「主机还活着」这条最基础的证据
        mem_msg = memory_pressure_alert(now, dry_run=args.dry_run)
        if mem_msg:
            print(f"[{now:%F %T}] 🟠 内存告急\n{mem_msg}")
    except Exception as exc:  # noqa: BLE001  看门狗绝不阻断 cron
        print(f"⚠️ host_watchdog 异常（不影响其他任务）: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
