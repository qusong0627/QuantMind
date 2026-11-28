#!/usr/bin/env bash
# 自有因子库每日尾窗物化（JST 23:00 起跑 = 北京 22:00；cron 尚未安装）
#
# 为什么有这个脚本：factor_defs / cand_factors / factor1300 / gap_mined / l1_batch1 /
# rd_mined 的公式**不在数据平台的同步链路里**——QuantDB 夜间同步只负责行情与下载类
# 数据，这些自研因子必须每天重算。漏一天，次日推理就少一批模型特征（维度门禁不过 →
# 无候选池 → 决策轮 rows=0）。2026-10-19 实测各库已滞后 1~4 周不等。
#
# 编排本身在 backend/scripts/materialize_library_tail.py（串行、staging 隔离、
# 重叠日探针、必需列回补）；本脚本只负责**时机、互斥、内存与告警**：
#
# 另有一个不经过编排的库：tdx_ind（通达信/同花顺指标截面库，81 列）。它自带
# _state.parquet 续算累计量，单日增量约 1–2 分钟，所以在主链**之前**单独跑——
# 主链超时也不会拖累它。失败只告警、不改主链退出码。
#
#   闸门：
#   1) flock 防重入（上一轮没跑完 → 本轮跳过，绝不并行——生产者共享 _partial_pilot
#      与内存预算）；
#   2) 等 alpha_library 刷新结束（它 JST02:30 开跑、实测 43–80 分钟、峰值 RSS ~24GB；
#      两个一起跑会挤爆 swap）——等待上限 ALPHA_WAIT_SECONDS，超时才继续；
#   3) 可用内存 < MIN_MEM_GB 有界等待，仍不足则跳过并推 QQ（这是每日唯一档期，
#      静默跳过 = 次日推理缺特征，不能沉默）。
#
# 时刻怎么定的（2026-10-20 用户拍板）：JST 23:00（北京 22:00）起跑，用户口径
# 是 QuantDB 数据一般在 22:00 前更新。注意本机 A 股同步调度存于 Redis
# quantmind:sync_schedule:A（当前 00:55 容器 CST = 01:55 JST），23:00 起跑时
# 目标日 = 当时最新日K（通常为前一交易日）；当晚新落盘的一日由次晚补。
# 首跑实测（2026-10-20 00:18 起，旧版代码）：快档 1h49m（rd_mined 独占 56m）；
# 长预热因子（复利窗口如 TSRANK(REF(c,504),504)=1008 日、f1300 最长 1220 日）
# 走第二遍 --only，额外 ~1~1.5h → 整轮预计 01:30–02:30 JST 收工。墙钟上限
# 3.5h（次日 02:30 兜底），早于 08:50 盘前检查与 09:00 CST 盘前推理。
# 注意：02:30 的 alpha 刷新档期可能与长窗遍重叠；本机 alpha 自身 30GB 内存闸
# 会先等后跳，长窗遍起跑前也有 alpha_busy/内存双闸，各自告警兜底。
#
# 告警：失败/部分完成/内存跳过 → QQ（scripts/push_notify.py）；成功只记日志。
# 状态：<quantdb>/6_ml_datasets/_materialize_status.json（保留最近 30 轮）。
#
# 用法：
#     bash scripts/factor_materialize_daily.sh              # 正常跑
#     bash scripts/factor_materialize_daily.sh --dry-run    # 只打印计划
#     MIN_MEM_GB=30 ALPHA_WAIT_SECONDS=0 ...               # 覆盖默认闸门
#
# 起跑：JST 23:00（cron 尚未安装；建议行 `0 23 * * 1-5`，待用户明确批准）
set -uo pipefail

QM_ROOT="${QM_ROOT:-/home/zbox/projects/quantmind}"
PY="${PYTHON_BIN:-/usr/bin/python3}"
DOCKER_BIN="${DOCKER_BIN:-/usr/bin/docker}"
QUANTDB_ROOT="${QM_QUANTDB_DATA_DIR:-$QM_ROOT/data/quantdb}"
SCRIPT="$QM_ROOT/backend/scripts/materialize_library_tail.py"
KLINE_ROOT="$QUANTDB_ROOT/1_kline_data/daily_forward"
STATUS="$QUANTDB_ROOT/6_ml_datasets/_materialize_status.json"
LOG="$QM_ROOT/logs/keepers/factor_materialize_daily.log"
LOCK="${QM_MATERIALIZE_LOCK:-/tmp/.qm_factor_materialize.lock}"
ALPHA_LOCK="${QM_ALPHA_LOCK:-/tmp/.qm_alpha_refresh.lock}"
ALPHA_RETRY_LOCK="${QM_ALPHA_RETRY_LOCK:-/tmp/.qm_alpha_retry.lock}"
#: 整轮起跑的最低可用内存。2026-10-20 实测：快档峰值 8.7GB、长窗遍
#: （全标的 ×1234 日）峰值 10.19GB。宿主机因 CIFS slab 泄漏长期只有 ~12GB
#: 可用，旧默认 24GB 会让本链在本机永远跳过。整轮闸取 10GB：只保第一遍
#: （峰值 8.7GB）能起跑；第二遍另有独立内存闸（orchestrator
#: LONG_PASS_MIN_MEM_GB，默认 11GB），可用不足时只跳第二遍、记 partial，
#: 不会为了一条长窗列把整晚的第一遍也挡掉。
MIN_MEM_GB="${MIN_MEM_GB:-10}"
MEM_WAIT_SECONDS="${MEM_WAIT_SECONDS:-1800}"
MEM_WAIT_SLEEP=120
ALPHA_WAIT_SECONDS="${ALPHA_WAIT_SECONDS:-3600}"
ALPHA_WAIT_SLEEP=60
#: 整轮墙钟上限（默认 3.5h：23:00 起跑最晚次日 02:30 JST 收工，早于盘前推理）
TIMEOUT_SECONDS="${MATERIALIZE_TIMEOUT_SECONDS:-12600}"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1
START_TS=$(date +%s)

ts() { date '+%F %T'; }
log_line() { echo "$(ts) $*" >> "$LOG"; }
notify() {  # notify <title> <content>；失败只记日志，绝不影响主流程
    [ "$DRY_RUN" -eq 1 ] && return 0
    [ "${QM_MATERIALIZE_NOTIFY:-1}" = "1" ] || return 0
    "$PY" "$QM_ROOT/scripts/push_notify.py" send "$1" "$2" >> "$LOG" 2>&1 || true
}

mkdir -p "$(dirname "$LOG")"
touch "$LOG" 2>/dev/null

# 1) 防重入（跳过要告警：上轮卡死会让管线无声停摆）
exec 9>"$LOCK"
if ! flock -n 9; then
    log_line "跳过：上一轮物化仍在运行"
    notify "🟠 因子物化跳过" "上一轮物化仍在运行（锁 $LOCK），本轮未执行；若上轮卡死请人工检查。"
    exit 0
fi

waited=0
mem_waited=0
AVAIL_GB=""
if [ "$DRY_RUN" -eq 0 ]; then
    # 2) 等 alpha_library 刷新（含补池重试链）结束。
    #    它们与本节共享内存预算；同时跑会把 swap 打满（swap 本已 15/15）。
    while :; do
        alpha_busy=0
        flock -n "$ALPHA_LOCK" -c true 2>/dev/null || alpha_busy=1
        flock -n "$ALPHA_RETRY_LOCK" -c true 2>/dev/null || alpha_busy=1
        [ "$alpha_busy" -eq 0 ] && break
        if [ "$waited" -ge "$ALPHA_WAIT_SECONDS" ]; then
            log_line "警告：alpha 刷新已占用 ${waited}s 仍未结束，按内存闸门继续（并发内存风险见告警）"
            notify "🟠 因子物化提前开跑" "alpha 刷新已占用 ${waited}s 未结束，按当前内存继续；注意并发内存风险。"
            break
        fi
        [ "$waited" -eq 0 ] && log_line "等待 alpha 刷新结束（上限 ${ALPHA_WAIT_SECONDS}s）…"
        sleep "$ALPHA_WAIT_SLEEP"
        waited=$((waited + ALPHA_WAIT_SLEEP))
    done

    # 3) 内存闸门：不足先有界等待，超时仍不足 → 跳过并推 QQ。
    while :; do
        AVAIL_GB=$(awk '/^MemAvailable:/ {print int($2/1024/1024)}' /proc/meminfo)
        if [ -n "$AVAIL_GB" ] && [ "$AVAIL_GB" -ge "$MIN_MEM_GB" ]; then
            break
        fi
        if [ "$mem_waited" -ge "$MEM_WAIT_SECONDS" ]; then
            log_line "跳过：可用内存 ${AVAIL_GB:-?}GB < ${MIN_MEM_GB}GB，已等待 ${mem_waited}s"
            notify "🟠 因子物化跳过" "可用内存 ${AVAIL_GB:-?}GB < ${MIN_MEM_GB}GB（等待 ${mem_waited}s）；当日自研因子未更新，次日推理可能缺特征。日志：logs/keepers/factor_materialize_daily.log"
            exit 0
        fi
        [ "$mem_waited" -eq 0 ] && log_line "内存不足（可用 ${AVAIL_GB:-?}GB < ${MIN_MEM_GB}GB），等待释放（上限 ${MEM_WAIT_SECONDS}s）…"
        sleep "$MEM_WAIT_SLEEP"
        mem_waited=$((mem_waited + MEM_WAIT_SLEEP))
    done
fi

# 4) 行情新鲜度提示（只记日志：真缺口由 _materialize_status.json 与盘前检查暴露）
KL_DT=$(ls -d "$KLINE_ROOT"/dt=* 2>/dev/null | grep -oE '[0-9]{8}' | sort | tail -1)
if [ -z "$KL_DT" ]; then
    log_line "跳过：日K 分区目录为空（$KLINE_ROOT）"
    notify "🔴 因子物化跳过" "日K 分区目录为空：$KLINE_ROOT"
    exit 0
fi
log_line "开始物化（日K最新=${KL_DT} 可用内存=${AVAIL_GB}GB alpha等待=${waited}s 内存等待=${mem_waited}s）"

# 3b) tdx_ind 增量（独立于编排：自带状态续算，不写 staging、不需合并）
TDX_SCRIPT="$QM_ROOT/backend/scripts/factor_materialize/tdx_ind.py"
if [ "$DRY_RUN" -eq 0 ] && [ -f "$TDX_SCRIPT" ]; then
    tdx_rc=0
    # 优先在 quantmind 容器内跑（root）：存量分区由容器写入，宿主机 zbox 对
    # `dt=YYYYMMDD` 目录没有写权限，2026-11-20 起 nightly 每天 rc=1
    # PermissionError，库停在 11-19，通达信模型推理门禁因此集体失败。
    # 容器不可用才回退宿主机（此时仍需显式传 QM_QUANTDB_DATA_DIR，否则
    # quantdb_root() 取不到 /data/quantdb 会 rc=1「no trading days」）。
    if "$DOCKER_BIN" exec quantmind true >/dev/null 2>&1; then
        "$DOCKER_BIN" exec -e QM_QUANTDB_DATA_DIR=/data/quantdb quantmind \
            python backend/scripts/factor_materialize/tdx_ind.py >> "$LOG" 2>&1 || tdx_rc=$?
    else
        QM_QUANTDB_DATA_DIR="$QUANTDB_ROOT" nice -n 10 "$PY" "$TDX_SCRIPT" >> "$LOG" 2>&1 || tdx_rc=$?
    fi
    if [ "$tdx_rc" -ne 0 ]; then
        log_line "警告：tdx_ind 增量失败 rc=$tdx_rc（不影响主链）"
        notify "🟠 tdx_ind 增量失败" "rc=$tdx_rc；次日训练会缺 tdx_ind 列。日志：logs/keepers/factor_materialize_daily.log"
    else
        log_line "tdx_ind 增量完成"
    fi
fi

ARGS=(--docker "$DOCKER_BIN" --quantdb-root "$QUANTDB_ROOT")
[ "$DRY_RUN" -eq 1 ] && ARGS+=(--dry-run)
if [ "$TIMEOUT_SECONDS" -gt 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    timeout --signal=TERM --kill-after=60 "$TIMEOUT_SECONDS" \
        nice -n 10 "$PY" "$SCRIPT" "${ARGS[@]}" >> "$LOG" 2>&1
else
    nice -n 10 "$PY" "$SCRIPT" "${ARGS[@]}" >> "$LOG" 2>&1
fi
rc=$?

# 5) 汇总最近一轮状态，失败/部分完成推 QQ；状态早于开跑时间 = 本轮没落盘（硬崩/OOM）
summary=$("$PY" - "$STATUS" "$START_TS" <<'PYEOF' 2>/dev/null
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.is_file():
    print("无状态文件")
    raise SystemExit
data = json.loads(path.read_text(encoding="utf-8"))
stale = int(float(sys.argv[2])) - 5 > path.stat().st_mtime
parts = []
for step in data.get("steps") or []:
    parts.append(f"{step['library']}={step['status']}")
flag = " [状态陈旧：本轮未落盘]" if stale else ""
print(
    f"target={data.get('target')} run_at={data.get('run_at')} ok={data.get('ok')}{flag} "
    + " ".join(parts)
)
PYEOF
)
[ -z "$summary" ] && summary="状态文件不可读：$STATUS"
log_line "结束 rc=$rc（$summary）"

if [ "$DRY_RUN" -eq 0 ] && [ "$rc" -ne 0 ]; then
    reason="rc=$rc"
    [ "$rc" -eq 124 ] && reason="墙钟超时（>${TIMEOUT_SECONDS}s）"
    notify "🔴 因子物化失败" "$reason；$summary；详见 logs/keepers/factor_materialize_daily.log"
fi
exit "$rc"
