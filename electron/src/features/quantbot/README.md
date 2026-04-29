# QuantBot - 功能说明

> **当前状态（2026-03-18）**：QuantBot 已通过 `quantmind-api` 适配上游服务，进入测试接入阶段。
> 已启用：聊天、会话管理、历史消息、健康检查，以及基于 QuantBot 的新闻查阅、邮件管理、Word/PDF/Excel/PPT 处理、定时任务、钉钉频道接入等能力入口。
> 暂不支持：独立任务系统，以及模型/技能/工具/工作空间的可视化管理后台。

## 概述

QuantBot 是 QuantMind 平台的智能助手，通过自然语言交互帮助用户完成新闻检索、邮件协作、文档处理、表格分析与自动化任务等工作。

## 当前接入架构

前端 → `quantmind-api (:8000)` → QuantBot `(:8088)`

- 前端**不直连** QuantBot，统一调用 `quantmind-api` 暴露的 `/api/v1/openclaw/*`。
- 网关负责会话 ID 映射、字段补默认值、hop-by-hop header 清洗。
- 消息链路当前采用**网关直传 QuantBot SSE**，前端按增量事件实时渲染回复。
- 附件链路当前采用**共享卷写入**：前端上传到 `quantmind-api`，网关把文件写入 `quantbot-data` 共享卷，并把 QuantBot 容器可见路径注入到消息上下文。
- 测试阶段无上游访问密钥；如 QuantBot 后续增加鉴权，统一在网关适配层处理，不由前端承担。

## 界面布局

三栏设计：

| 区域 | 组件 | 当前用途 |
|------|------|----------|
| 左侧栏 | `GuideSection` + `QuickActions` | 展示 QuantBot 已接入技能与快捷示例 |
| 中间区 | `ChatContainer` | 对话区（发送消息、展示回复） |
| 右侧栏 | `TaskPanelContainer`（降级） | 会话管理面板（列表、切换、重命名、删除） |

顶部状态栏每 30 秒通过 `GET /api/v1/openclaw/health` 探活；前端当前仅以该请求是否成功返回来判断 `quantmind-api` 网关是否可达，不再展示 QuantBot 上游状态。

## API 端点

所有接口均由 `quantmind-api` 统一暴露，前端通过 `agentApi.ts` 调用：

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/openclaw/chat` | 发送消息（映射到 QuantBot `/api/agent/process`） |
| `POST` | `/api/v1/openclaw/files/upload` | 上传当前会话附件 |
| `GET`  | `/api/v1/openclaw/files?session_id=` | 获取当前会话附件列表 |
| `GET`  | `/api/v1/openclaw/push-messages?session_id=` | 获取历史兼容轮询消息 |
| `GET`  | `/api/v1/openclaw/sessions` | 获取会话列表 |
| `POST` | `/api/v1/openclaw/sessions` | 创建新会话 |
| `GET`  | `/api/v1/openclaw/sessions/{session_id}/messages` | 获取历史消息 |
| `PUT`  | `/api/v1/openclaw/sessions/{session_id}/title` | 重命名会话 |
| `DELETE` | `/api/v1/openclaw/sessions/{session_id}` | 删除会话 |
| `GET`  | `/api/v1/openclaw/health` | 健康检查（合成探活 QuantBot） |

## 前端消息发送流程

```
1. 用户输入消息 → ChatInput
2. 若无当前 session，先 createSession()
3. 若有附件，先 `POST /api/v1/openclaw/files/upload` 写入共享卷
4. POST /api/v1/openclaw/chat（含 `message / session_id / user_id / attachments`）
5. 插入用户消息 + 占位 AI 消息（status: sending）
6. 前端持续读取 `/chat` 返回的 SSE 增量事件
   - 有内容 → 追加写入占位 AI 消息
   - 流结束 → 将消息标记为 sent
7. 出错 → 写入 system message，保留错误提示
```

## 会话状态管理

- `sessionStore.ts`（Zustand）：会话以 `session_id` 为主键。
- 切换会话时，`QuantBotPage.tsx` 自动调用 `getSessionMessages(sessionId)` 回填历史消息。
- `session_id` 持久化到 `localStorage`（按 `user_id` 隔离），刷新页面后可复用。
- 会话列表渲染键值（React `key`）采用 `session_id`，若异常数据缺失 ID 会使用稳定兜底键，避免 `AnimatePresence` 重复 key 警告。
- 认证门禁：仅当 `auth.isInitialized && auth.isAuthenticated` 时，才会触发会话列表、历史消息和健康检查请求，避免登录初始化窗口内出现 401/503 噪声。
- Token 续期：`agentApi.ts` 在请求前会先校验 token，若 access token 过期则尝试使用 refresh token 静默刷新；刷新失败会清理本地登录态并提示重新登录。
- 上游抖动重试：会话、消息、健康检查和聊天流在遇到短暂 `503` 时会做一次轻量重试，降低偶发网关抖动对用户的影响。
- SSE 兼容：前端按标准 SSE “事件块（空行分隔）”解析 `data:`，兼容多行 data 与分块边界；并对上游 `status=failed/error` 事件显式抛错，避免“无报错但无回复”的静默失败。
- 降级模式：当 `quantmind-api` 当前容器缺少 QuantBot/鉴权配置导致 `/api/v1/openclaw/sessions` 返回 `503` 时，前端会自动退化为本地临时会话，不再把会话列表与建会话失败当成致命错误。
- 模型门禁：当上游流式返回 `PROVIDER_ERROR: No active model configured` 时，前端会把它转成可读提示“当前未配置可用模型”，不再以技术栈堆栈形式刷到控制台。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `COPAW_BASE_URL` | `http://quantbot:8088` | QuantBot 上游地址（后端读取） |
| `COPAW_CHANNEL` | `console` | QuantBot channel |
| `COPAW_TIMEOUT_SECONDS` | `60` | 上游请求超时（秒） |
| `COPAW_SHARED_FILES_DIR` | `/quantbot-shared` | `quantmind-api` 容器内的共享卷挂载点 |
| `COPAW_SHARED_VISIBLE_DIR` | `/app/working` | QuantBot 容器内读取附件时看到的根目录 |
| `OPENCLAW_MAX_FILE_SIZE_BYTES` | `52428800` | 单个附件大小上限 |
| `VITE_OPENCLAW_API_URL` | `{API_GATEWAY}/api/v1/openclaw` | 前端 API 基址（可覆盖） |

## 当前不支持范围

- 模型管理、技能管理、工具管理的可视化后台
- 工作空间与 Agent 文件管理后台
- 独立任务轮询和任务面板

## 技术栈

- **前端**：React + TypeScript，Zustand（会话状态）+ Redux Toolkit（聊天消息），Axios，Framer Motion
- **后端适配层**：FastAPI + httpx（`quantbot_proxy.py`），`quantmind-api` 统一暴露

## 后续规划

- 恢复独立任务系统（待 QuantBot 任务接口稳定后接入）
- 完善附件历史回显与结果结构化展示
- 接入模型、技能、工具等高级管理能力

---

**Version**: 2.0.0（QuantBot 测试接入）
**Last Updated**: 2026-03-28
**Author**: QuantMind Development Team
