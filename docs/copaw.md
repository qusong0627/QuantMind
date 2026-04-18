# QuantBot (Copaw) 安装指南

QuantBot 是基于阿里 QwenPaw 的量化交易机器人，支持自然语言交互和智能策略生成。

## 快速安装

### 1. 拉取镜像

```bash
docker pull agentscope/qwenpaw:latest
```

### 2. 启动服务（首次配置）

首次启动时，需要开放外部访问以便登录配置：

```bash
docker run -d \
  --name qwenpaw \
  --restart unless-stopped \
  -p 0.0.0.0:8088:8088 \
  -v qwenpaw-data:/app/working \
  -v qwenpaw-secrets:/app/working.secret \
  agentscope/qwenpaw:latest
```

### 3. 配置模型

1. 浏览器访问：`http://<服务器IP>:8088`
2. 按照页面提示完成模型配置
3. 配置完成后，服务即可正常使用

### 4. 安全加固（配置完成后执行）

配置完成后，建议限制为本地访问，通过 Nginx 反向代理对外服务：

```bash
# 停止并删除容器
docker stop qwenpaw && docker rm qwenpaw

# 重新启动，仅监听本地
docker run -d \
  --name qwenpaw \
  --restart unless-stopped \
  -p 127.0.0.1:8088:8088 \
  -v qwenpaw-data:/app/working \
  -v qwenpaw-secrets:/app/working.secret \
  agentscope/qwenpaw:latest
```

### 5. Nginx 反向代理（可选）

在 QuantMind 的 Nginx 配置中添加：

```nginx
# QuantBot 代理
location /quantbot/ {
    proxy_pass http://127.0.0.1:8088/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

然后重载 Nginx：

```bash
nginx -t && systemctl reload nginx
```

访问地址变为：`http://<服务器IP>/quantbot/`

## 常用命令

```bash
# 查看服务状态
docker ps --filter name=qwenpaw

# 查看日志
docker logs -f qwenpaw

# 重启服务
docker restart qwenpaw

# 停止服务
docker stop qwenpaw

# 删除服务（保留数据）
docker rm -f qwenpaw

# 完全清理（包括数据）
docker rm -f qwenpaw
docker volume rm qwenpaw-data qwenpaw-secrets
```

## 数据持久化

| 卷名 | 用途 |
|------|------|
| `qwenpaw-data` | 工作目录数据 |
| `qwenpaw-secrets` | 密钥和敏感配置 |

## 注意事项

1. **首次配置**：必须使用 `0.0.0.0:8088:8088` 开放外部访问
2. **安全加固**：配置完成后切换为 `127.0.0.1:8088:8088`
3. **端口冲突**：确保 8088 端口未被占用
4. **资源需求**：建议至少 4GB 内存
