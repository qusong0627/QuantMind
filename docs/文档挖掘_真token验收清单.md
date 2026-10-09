# 文档挖掘链：真 Token 生产验收清单

> 面向**部署与运维人员**：在开启 `ENABLE_DOC_MINING` + `MINERU_API_TOKEN` 的
> 环境上，沿真实链路（真 MinerU 云端解析）逐项验收「因子挖掘文档链」。
> 启用步骤见 [文档挖掘_启用与通道指南.md](文档挖掘_启用与通道指南.md)；
> 本清单是它的 §3.4 验收的**全量版**（含配额告警演练与失败路径）。
>
> 建议用**专用验收账号**执行（每用户日限 200 页，本清单合计消耗约 30–40 页）。
> 全程记录在文末的验收记录表。

---

## 0. 前置状态

| 项 | 操作 | 期望 |
|---|---|---|
| 后端闸 | `.env` 有 `ENABLE_DOC_MINING=true`，容器已重建 | `curl -s -o /dev/null -w '%{http_code}' http://<host>:8000/api/v1/alpha-agent/docs/quota` ≠ 403（未登录 401 属正常） |
| Token | `.env` 有 `MINERU_API_TOKEN`，重建已执行 | `GET /docs/quota`（带登录态）里 `token_configured: true` |
| 前端闸 | 产物重新部署（`VITE_ENABLE_DOC_MINING=true` + `--allow-doc-mining`） | 因子挖掘首页出现「上传文档 ⇄ 文字指令」；挖掘历史出现「文档解析」Tab |
| nginx 上限 | `quantmind-web` 镜像为 2026-10 之后版本（210m） | `docker exec quantmind-web grep client_max_body_size /etc/nginx/conf.d/*.conf` 输出 ≥ 210m |

后续 curl 需要登录态：浏览器 DevTools → Application → Local Storage → 复制
`access_token`，令 `TOKEN=<该值>`，全部命令带 `-H "Authorization: Bearer $TOKEN"`。

## 1. 冒烟主链：文字型 PDF 全流程

| # | 操作 | 期望 |
|---|---|---|
| 1.1 | 上传一份**有文本层的** PDF（如 ≥5 页论文） | 上传立即 200；列表出现该文档，状态 `uploaded → parsing` |
| 1.2 | 等 10 秒~2 分钟（页面自动轮询） | 状态 `parsing → parsed`；详情可见页数、可预览 Markdown 与图片 |
| 1.3 | `GET /api/v1/alpha-agent/docs/quota` | `user_used/platform_used` 增加 ≈ 该 PDF 页数（settle 多退少补落地） |
| 1.4 | 点「开始整理」（kind 选 `paper`） | 进入整理中；完成后返回结构化 payload + Markdown，带 `prompt_version`；同一文档重复点整理会被 409 挡（在途锁） |
| 1.5 | 打开「整理后直通」**保持默认关**，点「确认挖掘」 | 跳转因子挖掘发起页，方向/任务参数预填了整理结果（`doc_id` 血缘已带上） |
| 1.6 | 发起挖掘 | 挖掘任务创建成功；任务详情可见方向（direction） |
| 1.7 | 打开「挖掘历史」→「文档解析」Tab | 历史可见：任务、状态流转（running → completed/failed）、发起时间；数据来自 PG（非内存） |

## 2. 扫描件路径（OCR）

| # | 操作 | 期望 |
|---|---|---|
| 2.1 | 上传**扫描件**（纯图片 PDF，或用手机拍一页照片存为 PNG 上传） | 接受（png/jpg 在白名单）；自动判定为 OCR 路径（无需手工勾选） |
| 2.2 | 等解析完成 | `parsed`；Markdown 非空（OCR 出了文字），页数入账 |

图片（png/jpg）按 1 页计；扫描 PDF 按实际页数计。

## 3. 容器重启：在途解析续轮询

| # | 操作 | 期望 |
|---|---|---|
| 3.1 | 上传一份**大** PDF（≥50 页，解析需要一两分钟），等它进入 `parsing` | 状态 `parsing` |
| 3.2 | 立刻 `docker restart quantmind`（或 `docker compose restart quantmind`） | 容器回来后 `docker logs quantmind` 出现该文档**重新接管轮询**的日志 |
| 3.3 | 等待解析完成 | 最终仍走到 `parsed`（PG 是权威状态；**不是** failed）；页数只入账一次（quota 不翻倍） |
| 3.4 | 重启期间前端刷新页面 | 状态照常显示（列表/详情从 PG 读） |

## 4. 配额告警演练（不重发、不刷屏）

默认预算 1000 页/日（北京时间日界）。用 Redis 播种把平台账号推进到告警区间
（演练结束后原额扣回，真实消耗留在账上）：

```bash
DAY=$(TZ=Asia/Shanghai date +%Y%m%d)
# 1) 播种平台已用 +950（真实使用量之上叠加）
docker exec quantmind-redis redis-cli INCRBY qm:docmining:pages:$DAY 950
```

| # | 操作 | 期望 |
|---|---|---|
| 4.1 | 上传一份 **10 页** PDF 并等解析完成 | 解析照常成功；`GET /docs/quota` 显示 `platform_remaining < 100`（=预算 10%）→ 服务端判告警 |
| 4.2 | 查通知（管理员账号的通知中心；QQ 旁路已配则同时到 QQ） | 一条「**MinerU 解析配额告急**」（类型「系统」）；SQL 佐证：`SELECT count(*) FROM notifications WHERE title='MinerU 解析配额告急'` 比演练前 +1 |
| 4.3 | 再上传一份 **1 页** PDF 并等解析完成（余量仍 <10%） | **不再**出现第二条告警（按配额日去重；`notifications` 计数不变） |
| 4.4 | 演练收尾：`docker exec quantmind-redis redis-cli DECRBY qm:docmining:pages:$DAY 950` | `GET /docs/quota` 回到真实用量（本次演练实际解析的 ~11 页保留计账） |
| 4.5 | （如当天还要再演练）`docker exec quantmind-redis redis-cli DEL qm:docmining:lock:doc_quota_alert:$DAY` | 去重锁清除，可重演；**生产日常不要清** |

## 5. 失败路径（失败率可见、账目可对）

| # | 操作 | 期望 |
|---|---|---|
| 5.1 | 上传一份**可读的**超过 200 页 PDF | **本地预检**上传即 `400`（「超过单文件 200 页上限，请拆分后重试」）；不入列表、不占配额（预留尚未发生），不烧 MinerU |
| 5.2 | 上传**加密 PDF**（`qpdf --encrypt 1 1 256 -- in.pdf out.pdf`） | 本地数不出页数 → 按单文件上限（200 页）**保守预留**后照常提交；MinerU 判失败 → 行定格 `parse_failed`（保留 MinerU 原文错误码，文案可读、不吞单）。⚠️ 本次预留 = 200 页，账号当日需 ≥200 页余量（当日没跑过前置项时直接跑本行，或先在下一节 6.2 的 DECRBY 侧调） |
| 5.3 | 查 `GET /docs/quota` | 5.2 失败 → 预留**全额退回**（无产物不记用户的账）；若 MinerU 意外可解 → 行 `parsed` 且按实际页数结算（两种都接受，账目与终态一致即可） |
| 5.4 | `GET /api/v1/alpha-agent/docs/stats` | `counts`/`attempted`/`parse_failed`/`failure_rate` 与上面操作对得上；`total` 不含已删 |

> `-60006`（MinerU 侧超页）的兜底路径（本地读不动、MinerU 判定超页）刻意
> 不拦：由单元测试（假 MinerU 状态机）覆盖，手测不必刻意构造。

## 6. 配额闸与频控（429）

```bash
# 把该验收账号的当日已用顶到上限（演练后原额扣回）
docker exec quantmind-redis redis-cli INCRBY qm:docmining:pages:$DAY:<user_id> 200
```

| # | 操作 | 期望 |
|---|---|---|
| 6.1 | 再上传任意文档 | `429`，文案含「已用尽 / 北京时间 …重置」（scope=user）；容器日志无异常栈 |
| 6.2 | `docker exec quantmind-redis redis-cli DECRBY qm:docmining:pages:$DAY:<user_id> 200` | 恢复正常可传 |
| 6.3 | （可选）一小时内连续上传 31 次 | 第 31 次 `429`（每小时上限 30）；`DOC_UPLOAD_RATE_PER_HOUR` 可调 |

`<user_id>` 用 `GET /docs/quota` 返回里的 `user_id` 字段。

## 7. 删除与账目语义

| # | 操作 | 期望 |
|---|---|---|
| 7.1 | 删除一份**已 parsed** 的文档 | 列表消失、预览 404、产物目录连根清；`/docs/quota` 数字**不变**（该次早已 settle，退回 = no-op） |
| 7.2 | 上传一份**大 PDF 在 parsing 中立即删除** | 删除成功、不再复活（轮询已取消）；`/docs/quota` **不回落**——MinerU 无取消 API，云端照常解析计费，账面与真实账单一致（2026-10 安全审查后的**预期**行为） |
| 7.3 | `GET /docs/stats` | 已删行不计入 `total`；`expired`（GC 到期）计入 `attempted` |

## 8. 关闭回滚检查

| # | 操作 | 期望 |
|---|---|---|
| 8.1 | 后端 `ENABLE_DOC_MINING` 改回 `false` 并重建容器 | 全部文档端点 403 `doc_mining_disabled`；文字指令挖掘/任务中心/因子库全照常 |
| 8.2 | 前端不带 `VITE_ENABLE_DOC_MINING` 重新构建部署 | 「上传文档」「文档解析」Tab 整体消失；深链落说明页 |
| 8.3 | 恢复开关 | 按指南 §3 重新打开 |

## 9. 验收记录

| 项 | 结果（通过/失败/备注） | 执行人 | 日期 |
|---|---|---|---|
| 0 前置状态 | | | |
| 1 冒烟主链 | | | |
| 2 扫描件 OCR | | | |
| 3 重启续轮询 | | | |
| 4 配额告警（含去重） | | | |
| 5 失败路径 | | | |
| 6 配额闸 429 | | | |
| 7 删除语义 | | | |
| 8 关闭回滚 | | | |

> 失败的项：先查指南第七节「故障排查」；仍不通过则保留容器日志
> （`docker logs quantmind 2>&1 | grep -i doc`）与 `GET /docs/stats` 输出反馈。
