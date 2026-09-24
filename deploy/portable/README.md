# QuantMind 便携版（免 Docker 一键启动包）

目标：**解压即用、零安装**。用户不需要 Docker、Python、PostgreSQL、Redis、Node —— 全部随包分发。

```
<仓库根>/scripts/package-for-windows.sh   # ← 一键出包入口（干净检出 → 构建前端 → 组装 → 过闸 → 出 zip）
deploy/portable/
├── build_linux_pack.sh     # 在 Linux 上构建 Linux 便携包（本机可直接完整验证）
├── build_windows_pack.sh   # 在 Linux 上交叉组装 Windows 便携包（需 Win 真机验证后分发）
├── pack_guard.py           # 出厂净化闸门（三个模式：--stage / --make-zip / --zip）
├── pack_rules.py           # 闸门判据唯一实现（排除 / 必备 / 成对 / 内容 / 宿主残留）
├── pack_assets/            # 放进包里的启动脚本与用户文档
│   ├── start.sh / stop.sh          # Linux/WSL 启停
│   ├── start.bat / stop.bat        # Windows 启停
│   ├── install.bat / install.ps1   # Windows 一键安装：体检 → 生成 pack.env → 拉起 start.bat
│   ├── pack.env.example            # 用户可选配置（端口/密钥）
│   └── README-portable.md          # 包内用户说明
├── build/                  # 组装暂存区与下载缓存（gitignore）
└── dist/                   # 成品包（gitignore）
```

## 包内结构

```
QuantMind-Portable-xxx/
├── runtime/python/   内嵌 Python 3.10（python-build-standalone，可搬迁、无绝对路径依赖）
├── pgsql/            便携 PostgreSQL 15（zonky 官方二进制，initdb 到包内 pgdata/）
├── redis/            便携 Redis（Linux 版源码编译 7.2；Windows 版 tporadowski 5.0）
├── backend/ config/ strategy_templates/   源码
├── web/              前端构建产物（electron/dist-react 拷贝）
├── data/             运行数据（空，用户启动后在线同步或拷入离线数据包）
├── start.sh|bat      一键启动：initdb → PG → Redis → main_oss(4服务) → celery → 浏览器
└── README.md         用户说明
```

## 与 Docker 版的差异（有意为之）

| 项 | Docker 版 | 便携版 |
|---|---|---|
| 前端伺服 | quantmind-web 容器 (nginx) | api 服务直接伺服（`QM_WEB_DIST_DIR` 环境变量，见 api/main.py 末尾 SPA 兜底） |
| Huntly/RSSHub 新闻 | 容器 | 不含，新闻功能降级 |
| qwenpaw / ib-gateway / futu-opend | 容器 | 不含 |
| GPU 训练镜像 | quantmind-oss-gpu | 不含（训练走远程 GPU 节点链路） |
| rd-agent 因子挖掘 | 镜像内 | **Linux 包含；Windows 包不含**：`pandarallel` 在 PyPI 上只有 sdist 没有 wheel，而交叉组装必须 `--only-binary=:all:`（不然会把需要本地编译的包也拉进来），整条解析一票否决；另运行期还要本机有 git（rd-agent 在工作区里 init/commit），便携包不带 git。想补：把这类依赖在构建机上预先编成 universal wheel 放进 `--find-links` |
| FinBERT 权重 | 构建时预下载 | 首次使用新闻情绪时在线下载（HF_HOME=data/hf） |
| qlib features 链接 | os.symlink | Windows 上自动改用目录联接 mklink /J（main_oss.py 已兼容） |

## 一键出包（Windows，推荐）

面向「发给别人」的通用包只走这一条命令（在仓库根执行）：

```bash
bash scripts/package-for-windows.sh            # 干净检出 HEAD 构建前端 → 组装 → 过闸 → 出 zip + .sha256
bash scripts/package-for-windows.sh --private  # 本机自用形态：用主工作树的前端产物（含本机独有实盘栏目）
bash scripts/package-for-windows.sh --require-clean   # 发版：工作树有未提交改动直接拒绝（VERSION 不带 -dirty）
```

它把三件容易做错的事固定下来：

1. **前端产物从 `git worktree` 干净检出里构建**——`electron/src/features/local-live/*.tsx` 是
   本机独有、未跟踪、不开源的实盘栏目，`import.meta.glob` 按**文件系统存在与否**决定是否打包。
   在主工作树里 `npm run build` 会把它一起打进去，而两份包都从这份产物取 `web/`。
   干净检出里那个目录**不存在**，于是产物天然是第三方形态（`VITE_ENABLE_REAL_TRADING` 也是
   开源版默认的 false）。`node_modules` 用符号链接借主工作树的（省 1.4G 与一次 `npm ci`），
   `--keep-worktree` 可保留现场排查。
2. **构建期就被闸门拦，而不是等 4GB 依赖下完、走到最后一步才报**：前端产物先扫私有栏目 chunk，
   再进组装。
3. **产物带可追溯与可校验信息**：`VERSION` 记 `git=<短修订>`（跟踪文件与 HEAD 不一致时记
   `<修订>-dirty`——源码是从工作树拷的，不标就等于谎报来源）、`built=<时间>`；zip 旁写 `.sha256`。

`--web-dist DIR`（或用 `--web-dist=DIR`）跳过前端构建、直接指定产物目录；`--rev REF` 用指定
修订而不是 HEAD；`--skip-assemble` 只跑到前端产物为止（调试前端形态时用）。

## 构建流程

```bash
# Linux 包（约 30-60 分钟，主要耗在 pip 下载 4-6GB 依赖）
bash deploy/portable/build_linux_pack.sh
# 成品: deploy/portable/dist/QuantMind-Portable-linux-x64.tar.gz

# GPU 增补包（可选，约 40 分钟，下载 CUDA 版 torch ~2.5GB）
# 依赖主包已构建；构建机有 NVIDIA 驱动时会实测 CUDA
bash deploy/portable/build_gpu_addon.sh
# 成品: deploy/portable/dist/QuantMind-Portable-gpu-addon-linux-x64.tar.gz (~1.6G)
# 用户侧: 增补包解压到便携包根目录 → bash install_gpu.sh → torch 切换为 CUDA 版

# Windows 包（交叉组装，成品必须在真机 Windows 验证）
bash deploy/portable/build_windows_pack.sh
# 成品: deploy/portable/dist/QuantMind-Portable-win-x64.zip
```

构建前置：`npm run dashboard:build`（生成 electron/dist-react）、curl、gcc/make（Linux 包编译 Redis 用）、磁盘 ≥ 23GB。

增量构建：脚本有缓存（build/cache 下载缓存、runtime 已装依赖自动跳过），改代码后重跑只需重新复制源码 + 重压缩；改依赖后删 build/QuantMind-Portable-*/runtime 重装。

## 出厂净化闸门（`pack_guard.py`）

**源码里干净 ≠ 出厂产物干净**，而包是要发给别人的。闸门在**写 zip 那一步**逐文件判，
判据只有一份（`pack_rules.py`），三个模式共用同一套判据，只差「命中排除清单算不算错」：

| 模式 | 用途 | 命中排除清单时 |
|---|---|---|
| `--stage <staging>` | 只体检 staging | 提示（列出来，不算错） |
| `--make-zip <staging> <out.zip>` | 出包（先校验 staging，有违规拒绝写） | 跳过、不写进 zip |
| `--zip <out.zip>` | 复核产物 | **违规**——文件既然进了产物，说明排除没生效 |

四层判据：

1. **路径**：排除清单（密钥载体 / 运行期残留 / 开发缓存 / Live 瘦节点注入物）＋ 必备清单
   （少一个就是残包）＋ **成对项**（哨兵在、必备不在 = 半个组件——文件清单看着有、界面入口也在，
   点开才报错，比整块没有更难排查）。
2. **内容**：内网地址与明文口令，直接复用公开仓那套检测器
   （`backend/tests/test_no_internal_addresses_or_plaintext_secrets.py`）——两处各写一份正则
   迟早分叉。检测器读不到时**拒绝降级**：宁可不出包，也不用一套悄悄松掉的规则出包。
3. **宿主残留值**：拿打包机 `.env` / `config/runtime.env` 里的真值逐字搜。两条纪律：
   读不到的来源要在报告里点名（`[提示] 探针来源读不到`），否则「探针是空的」和「扫干净了」
   长得一模一样；值**永不打印**，只打来源名（`[提示] 探针已丢弃 … .env:DB_PASSWORD` 这类）。
   已经在跟踪文件里出现过的值会被剔掉——拿一个**已公开**的值当探针，只会让一片正常代码默认值
   全被标成「宿主残留」，真问题反而看不见。
4. **形态**：让通用包整块功能消失的结构性标记（如实盘瘦节点的 `VITE_LIVE_NODE_ONLY`）。
   界面开关（`VITE_ENABLE_REAL_TRADING`）是策略不是泄漏，不在此列。

退出码：`0` 干净 / `1` 有违规（拒绝出包）/ `2` 用法错误。

**误报纪律**：`runtime/`、`qwenpaw_runtime/`、`pgsql/`、`redis/`、`huntly/` 是上游原样下载的
第三方运行时，**内容不扫**——里面有 `__pycache__`、`cacert.pem`、`test.key` 都是正常的，
不分范围就是每条都误报，护栏被淹掉然后被人关掉：**一条会误报的护栏等于没有护栏**。

**本机独有实盘栏目**：`web/assets/LiveTradingPage*` 默认按违规拦（源码 `electron/src/features/local-live/`
未跟踪、不开源，判据与 `scripts/deploy_frontend.sh` 第 3 步同源）。本机自用包显式放行：
`--allow-local-live`（等价环境变量 `PACK_ALLOW_LOCAL_LIVE=1`）。**拦的是意外，不是决定**。

**为什么排除只发生在写 zip 侧**：`build/QuantMind-Portable-win-x64` 这份 staging 是**两个构建器
共用**的（通用包与实盘瘦节点包，后者往同一份 staging 覆盖 `pack.env` / `bridge/` / `live/` /
前端产物）。在 staging 上删 = 偷偷改掉别人要打的包，所以通用构建器只把清单作用在 zip 写入侧，
产物再由 `--zip` 复核一遍。

## ⚠️ 涨跌停口径：Windows 侧已重建，**Linux 侧仍是旧口径**（2026-09-24 记账）

2026-09-20 的涨跌停口径收敛（提交 `609df61c`、`90640ec7`）之后，`build/` 下的两份 staging
一度都还是**收敛前**的源码。2026-09-24 重建 Windows staging（`scripts/package-for-windows.sh`）后，
实测这 10 个文件在 win staging 里与源码**逐字节一致**（逐文件 sha256 比对，不是看 mtime）：

| 文件（源码路径） | win staging | linux staging |
|---|---|---|
| `backend/services/engine/qlib_app/utils/cn_exchange.py` | 一致 | **旧版** |
| `backend/services/trade/simulation/services/local_market_data.py` | 一致 | **旧版** |
| `backend/shared/market_breadth.py` | 一致 | **旧版** |
| `backend/scripts/review_stats.py` | 一致 | **旧版** |
| `backend/services/simulation/services/execution_engine.py` | 一致 | **旧版** |
| `backend/services/live_trading/services/broker_client.py` | 一致 | **旧版** |
| `backend/services/engine/inference/trading_cost.py` | 一致 | **旧版** |
| `backend/services/engine/inference/inference_backtest_service.py` | 一致 | **旧版** |
| `backend/services/engine/qlib_app/utils/extended_strategies.py` | 一致 | **旧版** |
| `backend/scripts/enrich_sdl_data.py` | 一致 | **旧版** |

旧版 `cn_exchange.py` 是**按代码前缀返回 0.195/0.295/0.095 的静态表**——无 `trade_date`、
无 ST 档，还把 900xxx 沪市 B 股当北交所 30%；源码版本早已改为委托（唯一事实源）
`local_market_data.limit_pct`。

**为什么要紧**：包是发给用户的出厂产物，**源码修好 ≠ 出厂修好**。旧口径的具体后果是
回测/实盘在创业板 2020-08-24 改革前后、ST 5% 板、北交所与 B 股上判错涨跌停，
而报告上看不出来——正是「漂亮数据而非真实市场」。

**Linux 侧重建即修复**（源码已是收敛后的，无需改脚本；`$STAGE` 已存在时 Python 运行时 /
PostgreSQL / Redis 的下载与编译会自动跳过，只重新复制源码 + 重压缩）：

```bash
bash deploy/portable/build_linux_pack.sh      # → dist/QuantMind-Portable-linux-x64.tar.gz
```

`dist/` 里 2026-09-24 之前出的包（Linux 包、GPU 增补包）同样带旧口径，一并重出。
重建后把本节删掉，或改为「两侧均已重建 + 各自 VERSION 里的修订号」。


## 发布检查单

- [ ] **闸门输出留档**：出包命令的末尾必须有 `[guard] 产物复核 … 合计：违规 0 项` 与 sha256 两行
      （违规非 0 时脚本自己会拒绝出包，退出码 1——看到 zip 就是过闸了，但仍要扫一眼提示条数）
- [ ] **产物复核**（贴进发布说明）：体积、sha256、`VERSION` 里的 `git=` 与 `built=`
- [ ] **洗白抽查**（30 秒，别只看闸门）：`unzip -l` 里**不应**出现 `pack.env`、`bridge/`、
      `live/`、`README-LIVE.md`、`web/assets/LiveTradingPage*`、`models/users/*`、
      `backend/config/users/*`；`web/index.html` 与 `install.bat` / `install.ps1` 必须在
- [ ] Linux 包：本机解压到新路径跑 `bash start.sh`，/health 200、前端页面可打开、数据管理页可发起同步
- [ ] Windows 包（真机）：`install.bat` 先跑一遍（体检 → 生成 pack.env → 拉起 start.bat）；
      故意留一个端口被占，确认它真的报出来并给出占用进程（**闸门式的检查要亲眼见它红一次**，
      否则不知道它是不是恒绿）；重点盯 Redis 5.0 命令兼容、qlib features junction
- [ ] 网盘分发建议：环境包与离线数据包分开上传（数据包按市场拆分），`.sha256` 与包同目录

## macOS

pyqlib 0.9.7 有 macosx universal2 wheel、PG 有 zonky darwin 二进制、启动脚本可直接复用 start.sh，
理论可行；但没有 mac 构建机验证，未提供打包脚本。需要时参照 build_linux_pack.sh 换
`-apple-darwin` 运行时与 darwin PG/Redis 二进制即可。
