---
name: portable-pack
description: 构建 QuantMind 免 Docker 一键便携包(Linux/Ubuntu+WSL2 与 Windows x64,含可选 GPU 增补包),以及真机验证清单与全部已知坑
---

# QuantMind 便携包打包 (portable-pack)

产出「解压即用、零安装」的一键启动包(环境全内嵌,数据不打包、启动后在界面同步)。
目录:`deploy/portable/`,打包脚本全部在 `deploy/portable/` 下。

## 产物一览(dist/)

| 文件 | 平台 | 说明 |
|---|---|---|
| `QuantMind-Portable-linux-x64.tar.gz` | Ubuntu/WSL2 | 主包,含 models/Huntly/QwenPaw |
| `QuantMind-Portable-gpu-addon-linux-x64.tar.gz` | Linux | 解压到包根 → `bash install_gpu.sh` |
| `QuantMind-Portable-win-x64.zip` | Windows x64 | CPU 版;不含 models/Huntly/QwenPaw(精简) |
| `QuantMind-Portable-gpu-addon-win-x64.zip` | Windows | 解压到包根 → 双击 `install_gpu.bat` |

macOS(M 系列):`build_macos_pack.sh` 已备,**必须在一台 Mac 上跑**(Redis 无 darwin 预编译,需 Mac 本机 clang 编译),产物需 `xattr -dr com.apple.quarantine`。

## 构建前置

1. **前端产物必须最新**:改动过 `electron/src/` 后先 `cd electron && npm run build:react`(dist-react 是打进包的;旧产物漂移是常见"修了没生效"根因)
2. 仓库改动先 git 提交(包内容 = 打包时刻的代码)
3. 磁盘剩余 ≥ 23GB;网络建议官方源优先(实测阿里云镜像对本机 ~90kB/s,官方源/uv 快 10 倍+)

## 构建命令(均在 deploy/portable/)

```bash
./build_linux_pack.sh            # Linux 主包(含 Huntly/QwenPaw/models 预置)
./build_gpu_addon.sh             # Linux GPU 增补包(本地 gpu 镜像提取优先,免下载)
./build_windows_pack.sh          # Win 主包(交叉组装,不执行任何 Windows 代码)
./build_win_gpu_addon.sh         # Win GPU 增补包(cu128 torch 自包含 ~2.6GB 下载)
./build_macos_pack.sh            # 在 M 系列 Mac 上运行
```

依赖下载可复用缓存;失败重跑幂等(除了 wheels 下载会重来,见下)。

## 已知坑(全部已固化进脚本,但改脚本时勿回退)

1. **Windows .bat 铁律:纯 ASCII + CRLF**
   - 中文系统 cmd 按 GBK 解码 UTF-8+LF 的 bat → 行结构错乱:报 `'chcp'/'ROOT' 不是内部或外部命令`、`此时不应有 .`、窗口闪退
   - 构建脚本已内嵌强制转换段(cp 后 python 转 CRLF+ASCII);手工加 bat 内容只用英文
2. **start.bat 必须扁平 goto 风格**:禁止嵌套括号块、`^` 多行续行、块内 `&` 链——部分 cmd 版本致命解析错误导致静默闪退。保持 `if ... goto :label` + 顶层 label
3. **中文 Windows 必须 PYTHONUTF8=1**(start.bat 环境变量,已加):
   - 不加则 Python 默认 GBK:读建表 SQL `'gbk' codec can't decode` → **整库建表失败**(orders/quotes/system_events 全缺,后台扫描器疯狂刷 UndefinedTable,像"全坏了"实为一个根因);admin seed 写 ✅ emoji 也炸;日志 UnicodeEncodeError 刷屏
   - 同时设 PYTHONIOENCODING=utf-8,并补 REDIS_URL(缺失时 celery 同步调度连 'redis' 主机名失败)
4. **PydanticUndefinedAnnotation 崩溃模式**:`from __future__ import annotations` 下 FastAPI 在装饰路由时立即解析类型——**路由引用的 Pydantic 模型必须定义在路由之前**(quantdb_console 的 DataSourcesRequest 曾定义在后部 → api 5 次崩溃后放弃,窗口显示 Service start timeout)。改后端后出包前必须做 import 冒烟(见下)
5. **pip --only-binary 下 Win 依赖解析**:sdist-only 老包(jsonpath/jieba/PyExecJS/gym)由脚本预构建纯 py wheel 注入 `--find-links`;**futu-api/qstock 无 win wheel 已在 Win 清单剔除**(后端均为可选导入/未注册遗留适配器)
6. **python-build-standalone 命名漂移**:2026-09 起 Windows 资产去掉 `-shared-` 段;脚本用多 pattern + 精确尾部匹配(endswith .tar.gz)防 `_stripped` 误选
7. **rd-agent(因子演化)在 Win 包降级**(依赖无法 win 解析,脚本按设计跳过并警告)
8. Linux/WSL2 与 Win 内容差异:Win 精简版不含 models/Huntly/QwenPaw(如需对齐另行加)

## 构建冒烟(出包前必做)

```bash
# 后端 import 冒烟: 能加载全部路由即无 PydanticUndefinedAnnotation 类崩溃
cd 仓库根 && backend 依赖环境下 python -c \
  "import backend.services.api.main, backend.services.engine.main, backend.services.trade.main, backend.services.stream.main; print('imports OK')"
# 前端产物新鲜度: 改过 electron/src 后必须 npm run build:react(见上)
```

## 真机验证清单(每次出包必须)

- 拷到**本地磁盘**(勿在 SMB 共享/压缩包内运行,PG/Redis 起不来;start.bat 有 UNC 检测)
- Win 双击 `start.bat`:窗口驻留,依次 PostgreSQL→Redis→后端,最终 `Ready: http://127.0.0.1:8000/` 自动开浏览器,登录 `admin/admin123`
- 启动过程看 `logs\startup.log`,服务错误看 `logs\backend.log`
- 验证点:登录页 / 回测中心顶部市场切换 A股(策略列表只出 A股)/ 数据管理同步 / stop.bat 能停
- 启动成功信号:窗口到 Ready;backend.log 无 `crashed too many times`、无 `UnicodeEncodeError/'gbk' codec`、无成片 `UndefinedTable`(三者任一出现=有根因没修)
- 闪退排查:PowerShell 里 `.\start.bat` 或用 cmd 跑,报错不会闪;再不行放探针 MARK
- 分发前先真机完整跑一轮(Win 包是 Linux 交叉组装,打包脚本头部自带的警告是认真的)

## 分发

- SMB 共享(本机):`\\192.168.31.68\quantmind` → deploy/portable(dist 内含产物),`quantmind-repo` → 仓库根;凭据向用户确认,勿写入脚本/文档
- GPU 要求:RTX 20 系+(sm_75+)、驱动 ≥525;Win cu128 torch 自包含(库在 torch/lib 内);Linux 增补包从本地 `quantmind-oss-gpu:latest` 镜像提取
- 版本检查:`electron/package.json` version;便携包含 `VERSION` 文件

## 维护备注

- 本技能背后的踩坑史见 memory:`portable-pack-no-docker`、`portable-win-bat-crlf-ascii`
- 若打包过程出现新坑,先修脚本(固化),再更新本 SKILL 的坑清单
