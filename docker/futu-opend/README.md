# futu-opend —— QuantMind 港股通道的网关容器

官方 [Futu OpenD](https://www.futunn.com/download/OpenAPI) 的容器化封装：QuantMind 后端
（`backend/services/trade/services/futu_subprocess.py`）经它查港股账户/持仓/委托并下单，
宿主机的港股分析循环（`scripts/live_hourly_analysis_hk.py`）经后端 HTTP 消费同一份数据。

```
量化后端/脚本 ──(futu-api SDK, RSA)──► futu-opend:11111 ──► 富途服务器
```

## 构建

```bash
docker build -t quantmind-futu-opend:latest docker/futu-opend
```

`FutuOpenD_ubuntu.tar.gz`（官方 Ubuntu 18.04 发行包）就是构建上下文里的那份。
`Dockerfile` 是 2026-10-08 按 `docker history` 还原的归档件——镜像先于文件存在。

## 运行 / 首次登录

```bash
docker compose -f docker/futu-opend/docker-compose.yml up -d
docker attach futu-opend          # 输入账号/密码/短信码；完事 Ctrl+P Ctrl+Q 分离
docker logs --tail 5 futu-opend   # 末行不是「请输入账号」即为登录成功
```

**登录后永不 `docker rm`**：设备绑定与登录态在数据卷里（见下方两个挂载），删容器要重走短信验证。
只 `docker stop/restart`。

## 探活（两级）

```bash
# ① 端口活着
docker exec futu-opend bash -c 'timeout 2 bash -c "echo > /dev/tcp/127.0.0.1/11111" && echo OK'

# ② SDK 真能读到账户（**关键**：容器 Up ≠ 已登录，未登录时 SDK 会挂着等握手）
docker exec quantmind python3 /app/backend/services/trade/services/futu_subprocess.py \
  futu-opend 11111 /data/futu-opend/rsa.key account '{"env":"SIMULATE"}' /tmp/futu_probe.json
docker exec quantmind cat /tmp/futu_probe.json && docker exec quantmind rm -f /tmp/futu_probe.json
```

（结果写文件而不是 stdout：futu SDK 会往 stdout 打日志。）

## 两个数据卷

| 宿主机 | 容器内 | 内容 |
|---|---|---|
| `data/futu-opend` | `/opt/futu/data` | `rsa.key`（quantmind 容器经 `/data/futu-opend` 读同一把）+ `-data_dir` |
| `data/futu-opend-home` | `/root/.com.futunn.FutuOpenD` | 登录态/设备绑定（`-login_by_remember`） |

`rsa.key` 两边**必须同一把**，否则 SDK 握手失败；两个目录都别放进 git（`data/` 已在忽略之列）。

## 已知边界

- **容器 Up ≠ 服务可用**：未登录时 TCP 端口照样接受连接，SDK 调用挂到超时。
  港股循环的降级路径对这两种状态分别给了明确文案（`futu-opend 容器未运行` /
  `容器内 futu 子进程 30s 无响应——OpenD 未登录…`）。
- **子进程自带 20s 硬超时**（`futu_subprocess.HARD_TIMEOUT_S`，env 可覆盖）：
  2026-10-08 实测未登录时 SDK 挂在握手**永不返回**，而 SDK 连接线程非 daemon，
  主线程抛异常后解释器仍卡在退出路径——单进程驻留 ~297MB。现已由子进程自己
  保证退出（超时写 `{success:false, message:"…硬超时…"}` 后 `os._exit`），
  调用方的 30s/45s 超时退为兜底。停机期间 `docker exec … ps | grep futu_subprocess`
  应为空。
- **11111 除 RSA 外无鉴权**：compose 里只发布到 `127.0.0.1`，不要改成 `0.0.0.0`。
- 版本：镜像内 OpenD `10.10.7008`，容器内 futu-api SDK `10.11.7108`（协议兼容，实测通行）。
- 富途行情/交易需要相应权限（LV2 行情、交易解锁）由**账号侧**决定，与本容器无关。
