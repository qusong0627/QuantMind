#!/usr/bin/env bash
# 补池链补跑（宿主 cron，JST 05:30 / 06:30 / 07:30 = 北京 04:30 / 05:30 / 06:30）
#
# 为什么需要它（2026-10-13 事故）：alpha_library_refresh.sh 的内存闸门（可用内存
# <30GB 跳过，实测峰值 RSS ~24GB、swap 已满）把它整轮跳了 → alpha 停在 dt=20261009
# 而日K 已到 dt=20261012 → CUSTOM 重建撞 fail-closed「源库分区缺失…拒绝写入」→
# 6 个模型推理全撞「主模型维度门禁未通过」→ 当天候选池根本不存在 → 决策轮 rows=0，
# `l2.pool_not_member` 拦下全部开仓。而这一切**要等人**：第一个告警是开盘后 09:35
# 决策轮跑起来才发的（`empty_pool`），当天全部开仓机会已经错过。
#
# 为什么 2026-10-18 扩成「链级」：2026-10-17 03:00 的 CUSTOM 定时任务在
# quantmind-celery（8G）里算 jq110（109 因子 × 925 日 × 5578 只，float64 帧约 4.5G）
# 时被 cgroup OOM SIGKILL，而 alpha 本身是新的——旧脚本「alpha 已最新就退出」的
# 早退条件把这类**自定义数据集滞后**挡在门外，周末又不补跑，周一开盘前无人接链。
# 现在 alpha / custom 任一滞后都触发；CUSTOM 重建放在 45G 的 quantmind 容器里
# （8G worker 装不下 jq110：2026-10-16/17 两晚都在 03:01:46 同一秒数被杀）。
#
# 为什么光多排几条 cron 去调刷新脚本不够：CUSTOM 的定时派发在 03:00 CST（=容器墙钟），
# 标记键 `quantmind:sync_schedule_last_run:CUSTOM:{date}` 一旦置位，market-sync-dispatch
# 当天不会再派。所以 05:30 之后补出的 alpha **不会**自动带动 CUSTOM 重建——本脚本
# 必须自己把下游接上，否则只是把 alpha 补新、CUSTOM 照样停在旧分区（假自愈）。
#
# 职责（只做「缺了才补」和「补完接下游」，其余交给已有的幂等链路）：
#   ① 读分区算 AL_DT / CUSTOM_DT / KL_DT（**不依赖 Redis**：宿主没装 redis-py；
#      CUSTOM 只认带 data.parquet 的分区——SIGKILL 会留下只有目录的空分区）
#   ② 两段都最新且无池待办 → 静默退出（绝大多数日子走这条，代价只有三次 ls）
#   ③ alpha 滞后 → 串行调 alpha_library_refresh.sh（它自带 flock / 已最新 / 可写 / 内存四闸）
#   ④ alpha 补新后（或 alpha 本就最新而 CUSTOM 滞后）→ **容器内 CUSTOM 重建（45G）**，
#      复用唯一实现：三库增量 + 增量守卫 + fail-closed 源库预检
#   ⑤ 重测 CUSTOM_DT 仍滞后 → 记日志；末档才推 🟠，不猜也不硬来
#   ⑥ 补新成功 → 备选池重算（postmarket_pipeline --picks-only，恒重算幂等；
#      缺推理批次时它自己会补跑推理 —— 已是既有路径）
#   ⑦ 池重算失败 → 写待办标记；后档即使数据已最新也只补池，不许因「已最新」早退，
#      否则第 1 档修好数据、池失败，第 2/3 档会空跑，池缺到盘前检查才发现
#
# 时刻怎么定的：宿主 JST 05:30 = 北京 04:30（alpha 实测 43–53 分钟）→ 06:20 前后收工
# → CUSTOM 约 2 分钟 → 推理 ~6 分钟 → 池 ~06:30（北京）落地，**早于容器 08:00 CST
# 盘前推理、远早于 09:30 开盘**。第 2 档 06:30 JST 是「第 1 档跑完仍缺」时的再试；
# 第 3 档 07:30 JST 是末档（`QM_RETRY_FINAL=1`，失败才推 🟠），三档都在盘前。
#
# 告警分工：**盘前告警归 scripts/preopen_pool_check.py（JST 08:50）**，那一份会带五段链条
# 明细。本脚本只在**自愈成功**时推 🟢（这是非常事件，要说一声），失败默认只记日志；
# 只有被显式标为当日最后一档（`QM_RETRY_FINAL=1`）时才推 🟠，避免凌晨连推两条。
# 例外：alpha 曾滞后但下游没走完（半途）时立即推 🟠——那时链已经被动过，不能沉默。
#
# 闸门：全程 flock 防重入；CUSTOM 调度被用户关掉时**尊重用户设置**，只补 alpha 不代跑重建，
# 也不做下游池重算（没有新数据，重算没有意义）。
#
# 用法::
#
#     bash scripts/alpha_refresh_retry.sh              # 正常跑
#     bash scripts/alpha_refresh_retry.sh --dry-run    # 只打印判断，不刷新/不接下游/不推 QQ
#     QM_RETRY_NOTIFY=0 ...                            # 静音 QQ（测试/手工排查）
#     QM_RETRY_FINAL=1 ...                             # 标记为当日最后一档（失败也推）
set -uo pipefail

QM_ROOT="${QM_ROOT:-/home/zbox/projects/quantmind}"
# 宿主解释器：跑 postmarket_pipeline.py / push_notify.py（宿主确有 /usr/bin/python3）。
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
# 容器内解释器：**必须与上一行分开**。`quantmind` 镜像只有 /usr/local/bin/python3，
# 没有 /usr/bin/python3 —— 2026-10-13 首次实跑（此前只跑过 --dry-run）时 CUSTOM 重建
# 一步以 rc=127「exec: /usr/bin/python3: no such file or directory」直接失败，整条
# 自愈链停在「alpha 已补但下游未走完」。容器内一律靠 PATH 解析，不写绝对路径。
CONTAINER_PYTHON_BIN="${CONTAINER_PYTHON_BIN:-python3}"
DOCKER_BIN="${DOCKER_BIN:-docker}"

SCRIPT="$QM_ROOT/scripts/alpha_library_refresh.sh"
ALPHA_ROOT="$QM_ROOT/data/quantdb/6_ml_datasets/alpha_library"
KLINE_ROOT="$QM_ROOT/data/quantdb/1_kline_data/daily_forward"
CUSTOM_ROOT="$QM_ROOT/data/quantcustom/6_ml_datasets/l1_factors"
LOG="$QM_ROOT/logs/keepers/alpha_refresh_retry.log"
LOCK="${QM_RETRY_LOCK:-/tmp/.qm_alpha_retry.lock}"
# 池重算失败标记：数据补新了但池没算成时，后档不能因「数据已最新」早退。
# 没有它就会出现：第 1 档补好 CUSTOM、池重算失败（非末档静默）→ 第 2 档看到
# alpha/custom 都最新直接退出 → 池一直缺到 07:50 盘前检查才被发现。
PENDING_FLAG="${QM_RETRY_PENDING_FLAG:-$QM_ROOT/logs/keepers/.qm_retry_pending_picks}"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

mkdir -p "$(dirname "$LOG")" 2>/dev/null
log_line() { echo "$(date '+%F %T') $*" >> "$LOG"; }

notify() {  # 推 QQ；失败只记日志（告警推不出去不能让 cron 报错刷屏）
    local title="$1" body="$2"
    if [ "${QM_RETRY_NOTIFY:-1}" != "1" ]; then
        log_line "（已静音 QM_RETRY_NOTIFY=0，未推：$title）"
        return 0
    fi
    local out rc
    out=$(cd "$QM_ROOT" && "$PYTHON_BIN" -c '
import sys
sys.path.insert(0, "scripts")
from push_notify import notify
notify(sys.argv[1], sys.argv[2])
' "$title" "$body" 2>&1)
    rc=$?
    if [ "$rc" -eq 0 ]; then
        log_line "已推 QQ：$title"
    else
        log_line "⚠️ QQ 推送失败（rc=$rc，链路状态不受影响）：$(echo "$out" | tail -2 | tr '\n' ' ')"
    fi
}

newest_dt() { ls -d "$1"/dt=* 2>/dev/null | grep -oE '[0-9]{8}' | sort | tail -1; }
# CUSTOM 分区必须带 data.parquet 才算落地：写入方先建目录、后写 parquet（非原子），
# SIGKILL 可能留下一个只有目录的空分区。只看目录名会把它判成「已最新」，
# 自动修复面反而永不触发（就绪卡同病，属既有边界）。
newest_custom_dt() {
    ls -d "$1"/dt=*/data.parquet 2>/dev/null \
        | grep -oE 'dt=[0-9]{8}' | cut -d= -f2 | sort | tail -1
}

# 最近一个**已收盘**的交易日（北京时区）。不能用「本地最新分区」当基准：
# 2026-10-29 的形态就是日K 本身停在 10-27，链把 10-27 当成了「已最新」，
# 三档全空跑，08:00 盘前推理要 10-28 直接撞维度门禁。
# 容器 TZ=Asia/Shanghai；取「昨天及以前」的最后一个交易日，避开当日未收盘的会话。
expected_last_session() {
    # 运维演练/单测注入点：显式指定期望交易日，跳过日历计算
    if [ -n "${QM_EXPECTED_DT_OVERRIDE:-}" ]; then
        echo "$QM_EXPECTED_DT_OVERRIDE"
        return 0
    fi
    # 测试/替身环境：DOCKER_BIN 不是真 docker 时跳过（假 docker 不实现 exec）
    if [ "${DOCKER_BIN:-docker}" != "docker" ]; then
        return 0
    fi
    timeout 30 docker exec -i quantmind python - <<'PY' 2>/dev/null
import datetime as dt
try:
    import exchange_calendars as xc
    cal = xc.get_calendar("XSHG")
    prev = dt.date.today() - dt.timedelta(days=1)
    print(cal.date_to_session(prev, direction="previous").strftime("%Y%m%d"))
except Exception:
    pass
PY
}

# 1) 防重入（三档 cron 可能撞在同一个还没跑完的窗口里）
exec 9>"$LOCK"
if ! flock -n 9; then
    log_line "跳过：上一次重试仍在运行"
    # 末档是最後的自动兜底。它抢不到锁 = 这一天的自动修复到此为止，
    # 不能静默退出；下一步只剩 07:50 的盘前检查。前一轮若最终成功，
    # 这条告警会是一条「虚惊」，但比「末档没跑却没人知道」可接受。
    if [ "${QM_RETRY_FINAL:-0}" = "1" ]; then
        notify "🟠 补池末档未能执行（前一轮仍在运行）" \
"最后一档未能抢到锁：上一轮补池重试仍在跑（长任务或卡住），本档无法再检查/修复。

盘前 07:50 的就绪检查会给出最终判据；若届时仍无结果，请手工看
logs/keepers/alpha_refresh_retry.log（本机 JST）。"
    fi
    exit 0
fi

KL_DT=$(newest_dt "$KLINE_ROOT")
AL_DT=$(newest_dt "$ALPHA_ROOT")
CUSTOM_DT=$(newest_custom_dt "$CUSTOM_ROOT")

if [ -z "$KL_DT" ]; then
    log_line "跳过：日K 分区目录为空（$KLINE_ROOT）"
    exit 0
fi

# 三段各自判滞后：alpha 是最上游，custom 是推理直读源，kline 是所有源的地基。
# **不许只看 alpha**——2026-10-17 的形态就是 alpha 最新、jq110 在 8G worker 被 OOM，
# custom 停在旧日；2026-10-29 的形态则是日K 本身停在 10-27，链把旧分区当「已最新」，
# 三档全空跑，08:00 盘前推理撞维度门禁。
AL_STALE=0
CUSTOM_STALE=0
KLINE_STALE=0
EXPECTED_DT=$(expected_last_session)
if [ -n "$EXPECTED_DT" ] && [ -n "$KL_DT" ] && [ "$KL_DT" -lt "$EXPECTED_DT" ]; then
    KLINE_STALE=1
fi
if [ -z "$AL_DT" ] || [ "$AL_DT" -lt "$KL_DT" ]; then AL_STALE=1; fi
if [ -z "$CUSTOM_DT" ] || [ "$CUSTOM_DT" -lt "$KL_DT" ]; then CUSTOM_STALE=1; fi

PENDING_PICKS=0
[ -f "$PENDING_FLAG" ] && PENDING_PICKS=1

if [ "$KLINE_STALE" = "0" ] && [ "$AL_STALE" = "0" ] && [ "$CUSTOM_STALE" = "0" ] && [ "$PENDING_PICKS" = "0" ]; then
    # 链是好的——绝大多数日子走这条。写一行日志便于确认守护在跑，但不推 QQ。
    log_line "无事：已最新（alpha=$AL_DT custom=$CUSTOM_DT kline=$KL_DT expected=${EXPECTED_DT:-?}）"
    exit 0
fi

AVAIL_GB=$(awk '/^MemAvailable:/ {print int($2/1024/1024)}' /proc/meminfo)
AL_DT_ORIG="$AL_DT"
CUSTOM_DT_ORIG="$CUSTOM_DT"
if [ "$KLINE_STALE" = "1" ]; then
    log_line "日K 滞后（kline=${KL_DT:-无} expected=$EXPECTED_DT）→ 先补 A 股上游同步"
elif [ "$AL_STALE" = "1" ] || [ "$CUSTOM_STALE" = "1" ]; then
    log_line "补池链滞后（alpha=${AL_DT:-无} custom=${CUSTOM_DT:-无} kline=$KL_DT，pending_picks=$PENDING_PICKS，可用内存 ${AVAIL_GB:-?}GB）→ 重试"
else
    log_line "补池链待办：数据已最新，但上次备选池重算失败（$PENDING_FLAG）→ 只重试池"
fi

if [ "$DRY_RUN" = "1" ]; then
    [ "$KLINE_STALE" = "1" ] && log_line "[dry-run] 将执行：容器内 A 股同步（run_market_sync('A')）"
    [ "$AL_STALE" = "1" ] && log_line "[dry-run] 将执行：bash $SCRIPT（alpha 刷新）"
    if [ "$AL_STALE" = "1" ] || [ "$CUSTOM_STALE" = "1" ]; then
        log_line "[dry-run] 将执行：容器内 CUSTOM 重建（若滞后）；成功后备选池重算（--picks-only）"
    elif [ "$KLINE_STALE" = "0" ]; then
        log_line "[dry-run] 将执行：只重试备选池（--picks-only）"
    fi
    exit 0
fi

# 1.5) 日K 本身滞后：先跑 A 股上游同步（盘前推理要的就是这一天）
if [ "$KLINE_STALE" = "1" ]; then
    log_line "触发 A 股同步：kline=${KL_DT:-无} expected=$EXPECTED_DT"
    timeout 2700 docker exec quantmind python -c "
import logging
logging.basicConfig(level=logging.INFO)
from backend.services.engine.tasks.market_sync_scheduler import get_schedule, run_market_sync
print(run_market_sync('A', get_schedule('A')))
" >> "$LOG" 2>&1
    SYNC_RC=$?
    KL_NEW=$(newest_dt "$KLINE_ROOT")
    log_line "A 股同步结束 rc=$SYNC_RC kline=${KL_NEW:-无}"
    KL_DT="${KL_NEW:-$KL_DT}"
    AL_DT=$(newest_dt "$ALPHA_ROOT")
    CUSTOM_DT=$(newest_custom_dt "$CUSTOM_ROOT")
    KLINE_STALE=0
    AL_STALE=0
    CUSTOM_STALE=0
    if [ -n "$EXPECTED_DT" ] && [ -n "$KL_DT" ] && [ "$KL_DT" -lt "$EXPECTED_DT" ]; then
        KLINE_STALE=1
    fi
    if [ -z "$AL_DT" ] || [ "$AL_DT" -lt "$KL_DT" ]; then AL_STALE=1; fi
    if [ -z "$CUSTOM_DT" ] || [ "$CUSTOM_DT" -lt "$KL_DT" ]; then CUSTOM_STALE=1; fi
    if [ "$KLINE_STALE" = "1" ]; then
        log_line "❌ 日K 仍滞后（kline=${KL_DT:-无} expected=$EXPECTED_DT）——上游未就绪，盘前推理将失败"
        if [ "${QM_RETRY_FINAL:-0}" = "1" ]; then
            notify "🟠 日K 仍缺 $EXPECTED_DT" \
"补池链已触发 A 股同步，但日K 仍停在 ${KL_DT:-无}（期望 $EXPECTED_DT）。

08:00 盘前推理会因数据范围不足失败；请检查上游 QuantDB release 是否已发布。"
        fi
        exit 0
    fi
fi

# 2) alpha：只有滞后才刷新（它自己会再判一次内存/可写/已最新，全部幂等）
if [ "$AL_STALE" = "1" ]; then
    bash "$SCRIPT" >> "$LOG" 2>&1
    REFRESH_RC=$?
    AL_NEW=$(newest_dt "$ALPHA_ROOT")

    if [ -z "$AL_NEW" ] || [ "$AL_NEW" -lt "$KL_DT" ]; then
        MSG="alpha 仍滞后（alpha=${AL_NEW:-无} kline=$KL_DT，刷新 rc=$REFRESH_RC，可用内存 ${AVAIL_GB:-?}GB）——本轮 gate 见 alpha_library/cron_run.log"
        log_line "❌ $MSG"
        if [ "${QM_RETRY_FINAL:-0}" = "1" ]; then
            notify "🟠 补池重试未补上 alpha 因子库" \
"$MSG

候选池今天仍会缺——开盘前决策轮会因「候选池非成员」拦下全部新开仓。
下一步：确认内存后手工跑 scripts/alpha_refresh_retry.sh，或看后台「数据管理」页的补池就绪卡。"
        fi
        exit 1
    fi

    log_line "✅ alpha 已补齐（alpha=$AL_NEW kline=$KL_DT，刷新 rc=$REFRESH_RC）→ 接下游"
else
    log_line "alpha 已最新（alpha=$AL_DT kline=$KL_DT）→ 直接补下游"
fi

# 2b) PG 地址：`pick_candidates` 的 psycopg2 缺省连回环 5432，而**回环无监听**——
#     DB 只发布在 LAN 地址上（compose 的 `QM_DB_BIND`）。宿主脚本读 `POSTGRES_HOST`
#     （`postmarket_pipeline._pg_connect`），crontab 头部有这条变量、**非 cron 手工
#     调用时没有**：2026-10-13 首次手工实跑就撞 `Connection refused`。
#     解析逻辑**不在这里写第二份**（同一个键两种读法 = 一个地址两个答案）：
#     `scripts/host_env.py` 是唯一实现（`QM_DB_BIND` 是「DB 发布到哪」的事实源，
#     compose 与 boot_guard.sh 同源），bash 只负责把结果 export 出去。
#     显式设过 `POSTGRES_HOST` 的一律尊重；解析不出则不动（退回脚本自身缺省）。
if [ -z "${POSTGRES_HOST:-}" ]; then
    _db_host=$(cd "$QM_ROOT/scripts" && "$PYTHON_BIN" -c \
        'from host_env import resolve_postgres_host; print(resolve_postgres_host() or "")' \
        2>/dev/null)
    if [ -n "$_db_host" ]; then
        export POSTGRES_HOST="$_db_host"
        log_line "POSTGRES_HOST 未设 → 用 .env 的 QM_DB_BIND=$_db_host"
    fi
fi

# 3) CUSTOM 数据集重建：容器内跑，复用唯一实现。
#    调度被用户关掉时尊重用户设置（只补 alpha，不代跑）；enabled=true 才代跑一次
#    （幂等：已最新则秒退，实测 0.09s）。**必须在 45G 的 quantmind 容器**：
#    jq110 的 1400 天回看窗在 8G celery worker 里会被内核 SIGKILL。
CUSTOM_DT=$(newest_custom_dt "$CUSTOM_ROOT")
CUSTOM_RC=0
CUSTOM_SKIPPED=0
if [ -n "$CUSTOM_DT" ] && [ "$CUSTOM_DT" -ge "$KL_DT" ]; then
    log_line "CUSTOM 已最新（custom=$CUSTOM_DT kline=$KL_DT）→ 跳过重建"
else
    CUSTOM_OUT=$("$DOCKER_BIN" exec -i -w /app -e PYTHONPATH=/app quantmind "$CONTAINER_PYTHON_BIN" - <<'PY' 2>&1
import json, os, sys
import redis
r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_timeout=3)
raw = r.get("quantmind:sync_schedule:CUSTOM")
cfg = json.loads(raw) if raw else {}
if not cfg.get("enabled"):
    print("SKIP: CUSTOM 调度未启用，尊重用户设置不代跑")
    sys.exit(0)
from backend.services.engine.tasks.market_sync_scheduler import run_market_sync
res = run_market_sync("CUSTOM", cfg) or {}
inner = res.get("result") or {}
print(
    "OK: CUSTOM rebuild"
    f" mode={inner.get('mode')} written={inner.get('written')}"
    f" n_factors={inner.get('n_factors')} elapsed={inner.get('elapsed_s')}s"
    f" libs={res.get('libraries')}"
)
PY
)
    CUSTOM_RC=$?
    log_line "CUSTOM 重建 rc=$CUSTOM_RC：$(echo "$CUSTOM_OUT" | tail -2 | tr '\n' ' ')"
    if echo "$CUSTOM_OUT" | grep -q 'SKIP: CUSTOM 调度未启用'; then
        CUSTOM_SKIPPED=1
        log_line "⏸ CUSTOM 调度未启用 → 尊重用户设置，未代跑重建"
    fi
fi

# 3b) 尊重用户设置：CUSTOM 调度关着就不做下游池重算，也不播报自愈。
if [ "$CUSTOM_SKIPPED" = "1" ]; then
    exit 0
fi

CUSTOM_NEW=$(newest_custom_dt "$CUSTOM_ROOT")
CUSTOM_OK=0
if [ -n "$CUSTOM_NEW" ] && [ "$CUSTOM_NEW" -ge "$KL_DT" ]; then
    CUSTOM_OK=1
else
    log_line "❌ CUSTOM 仍滞后（custom=${CUSTOM_NEW:-无} kline=$KL_DT，重建 rc=$CUSTOM_RC）"
fi

# 4) 下游② 备选池重算（宿主脚本；恒重算幂等，缺推理批次时它自己会补跑推理）
PICKS_OUT=$(cd "$QM_ROOT" && "$PYTHON_BIN" scripts/postmarket_pipeline.py --picks-only 2>&1)
PICKS_RC=$?
log_line "备选池重算 rc=$PICKS_RC：$(echo "$PICKS_OUT" | tail -3 | tr '\n' ' ')"
if [ "$PICKS_RC" -eq 0 ]; then
    rm -f "$PENDING_FLAG"
else
    # 数据可以已经齐了，但池没算成——后档必须能只补池，不能被「数据已最新」早退挡掉。
    date '+%F %T' > "$PENDING_FLAG" 2>/dev/null
    log_line "备选池重算失败 → 记待办标记（后档只补池）：$PENDING_FLAG"
fi

if [ "$CUSTOM_OK" = "1" ] && [ "$PICKS_RC" -eq 0 ]; then
    if [ "$AL_STALE" = "0" ] && [ "$CUSTOM_STALE" = "0" ] && [ "$PENDING_PICKS" = "1" ]; then
        log_line "🚑 备选池补算完成（数据链本就最新）"
        notify "🟢 备选池补算完成" \
"数据链本就最新（日K $KL_DT，alpha ${AL_DT:-无}，CUSTOM $CUSTOM_NEW）；
上一档备选池重算失败，本档已重试成功。"
        exit 0
    fi

    AL_NOW=$(newest_dt "$ALPHA_ROOT")
    if [ "$AL_STALE" = "1" ]; then
        AL_LINE="alpha：${AL_DT_ORIG:-无} → ${AL_NOW:-无}（本轮补新）"
    else
        AL_LINE="alpha：${AL_NOW:-无}（本就最新）"
    fi
    if [ "$CUSTOM_STALE" = "1" ]; then
        CUSTOM_LINE="CUSTOM：${CUSTOM_DT_ORIG:-无} → ${CUSTOM_NEW:-无}（本轮补新）"
    else
        CUSTOM_LINE="CUSTOM：${CUSTOM_NEW:-无}（本就最新）"
    fi
    log_line "🚑 自愈完成（alpha=${AL_NOW:-无} custom=$CUSTOM_NEW）"
    notify "🟢 补池自愈完成" \
"补池链已接上（日K $KL_DT）：
- $AL_LINE
- $CUSTOM_LINE
- 备选池已重算（--picks-only）

开盘前请看一眼后台「数据管理」页的补池就绪卡确认。"
    exit 0
fi

MSG="补池链未走完（custom=${CUSTOM_NEW:-无} kline=$KL_DT，CUSTOM rc=$CUSTOM_RC，备选池 rc=$PICKS_RC）"
log_line "⚠️ $MSG"
# 只补 CUSTOM 的失败默认留给下一档再试；末档才喊人。alpha 曾滞后（半途）时立即喊人。
if [ "${QM_RETRY_FINAL:-0}" = "1" ] || [ "$AL_STALE" = "1" ]; then
    notify "🟠 补池自愈半途" \
"$MSG

开盘前决策轮仍可能因「候选池非成员」拦下新开仓。
详见 logs/keepers/alpha_refresh_retry.log（本机 JST）。"
fi
exit 1
