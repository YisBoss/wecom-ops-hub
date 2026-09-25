# 部署与回调排障

本文件覆盖：镜像构建/拉取、端口与反向代理、公网回调链路、常见故障排查、完整配置项参考。
API/表结构/路径的唯一权威是 [`CONTRACT.md`](CONTRACT.md) —— 本文与之冲突时以 CONTRACT 为准。

## 一、部署方式

### 方式 A：拉预构建镜像（推荐）

```bash
mkdir wecom-ops-hub && cd wecom-ops-hub
# 把仓库根的 docker-compose.yml 放到当前目录（或照 README 复制一份）
docker compose up -d
```

镜像在 GHCR：`ghcr.io/yisboss/wecom-ops-hub:latest`，多架构（amd64 + arm64）。

### 方式 B：本地构建

把 `docker-compose.yml` 里的 `image:` 行注释掉、`# build: .` 的注释打开：

```yaml
services:
  wecom-ops-hub:
    # image: ghcr.io/yisboss/wecom-ops-hub:latest
    build: .
    container_name: wecom-ops-hub
    # ……其余不变
```

```bash
docker compose up -d --build
```

### 容器约定（固定，别改）

| 项 | 值 | 说明 |
|---|---|---|
| 监听端口 | `8080` | 容器内固定，宿主机端口由 `HUB_PORT` 映射 |
| 数据目录 | `/data` | SQLite 在 `/data/hub.db`，由 `HUB_DATA_DIR` 映射 |
| 启动命令 | `uvicorn app.main:app --host 0.0.0.0 --port 8080` | |
| 健康检查 | `GET /api/health` → `{"ok":true}` | 见 compose healthcheck |

## 二、环境变量（仅首次引导）

| 变量 | 默认 | 说明 |
|---|---|---|
| `HUB_PORT` | `8080` | 宿主机端口 |
| `HUB_ADMIN_PASSWORD` | `admin` | 首次登录密码；登录后到面板改，改完存在 SQLite |
| `HUB_DATA_DIR` | `./data` | 宿主机数据目录映射 |
| `TZ` | `Asia/Shanghai` | 时区 |

> 这些只在**首次启动**时用。之后所有配置（企微凭据、监控目标、通知策略、菜单）都进面板改，存在 SQLite，重启容器不丢。

## 三、面板设置项完整参考

键名即 API/面板字段名（见 `app/settings.py` 的 `DEFAULT_SETTINGS`）。

### 面板自身

| 键 | 默认 | 敏感 | 说明 |
|---|---|---|---|
| `panel.admin_password` | `admin` | ✓ | 面板登录密码 |
| `panel.public_url` | （空） | | 公网基址，拼回调 URL 和菜单跳转链接，例 `https://hub.example.com` |

### 企业微信自建应用

| 键 | 默认 | 敏感 | 说明 |
|---|---|---|---|
| `wecom.corp_id` | （空） | | 企业 ID |
| `wecom.agent_id` | （空） | | 应用 AgentId（整数，存为字符串） |
| `wecom.secret` | （空） | ✓ | 应用 Secret |
| `wecom.callback_token` | （空） | ✓ | 接收消息的 Token |
| `wecom.callback_aes_key` | （空） | ✓ | 接收消息的 EncodingAESKey |
| `wecom.api_base` | `https://qyapi.weixin.qq.com` | | 企微 API 基址，一般不改 |
| `wecom.proxy_url` | （空） | | 企微 API 反代基址；出口 IP 不在「企业可信 IP」白名单时填 |
| `wecom.touser` | `@all` | | 接收人 userid，多个逗号分隔；`@all` = 应用可见范围内全部 |
| `wecom.toparty` | （空） | | 接收部门 id，多个逗号分隔 |

### 通知策略

| 键 | 默认 | 说明 |
|---|---|---|
| `notify.enabled` | `true` | 开关告警推送 |
| `notify.fail_threshold` | `2` | 连续失败几次才告警 |
| `notify.silence_minutes` | `30` | 同一目标同一问题静音窗口（分钟） |
| `notify.recovery` | `true` | 恢复时是否补推 |

### 监控引擎

| 键 | 默认 | 说明 |
|---|---|---|
| `monitor.tick_seconds` | `30` | 调度器心跳间隔 |
| `log.retain_days` | `7` | 探测结果保留天数 |

> 敏感项（`SECRET_KEYS`）在面板返回时打码为 `********`；`PUT /api/settings` 收到 `********` 视为「不修改」。

## 四、监控目标字段（`targets` 表）

每个 HTTP 探针的字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | — | 目标名称 |
| `enabled` | `1` | 是否启用 |
| `url` | — | 探测 URL |
| `method` | `GET` | HTTP 方法 |
| `headers` | `{}` | 请求头（JSON 字符串） |
| `body` | （空） | 请求体 |
| `expect_status` | `200-299` | 期望状态码，支持 `200` / `200-299` / `200,301` |
| `timeout_s` | `10` | 超时秒数 |
| `interval_s` | `60` | 探测间隔秒数 |
| `fail_threshold` | `2` | 连续失败几次告警（覆盖 `notify.fail_threshold`） |
| `silence_minutes` | `30` | 静音窗口分钟数 |
| `notify_recovery` | `1` | 恢复时是否通知 |
| `action_type` | `none` | 动作类型：`none` / `http` |
| `action_method` | `POST` | 动作 HTTP 方法 |
| `action_url` | （空） | 动作 URL |
| `action_headers` | `{}` | 动作请求头 |
| `action_body` | （空） | 动作请求体 |
| `action_confirm` | `1` | 是否需要面板手动确认执行 |

## 五、公网回调链路

企微的「接收消息」和菜单 `view` 跳转都要求面板有公网 URL。

### 回调 URL 格式

在企微后台「接收消息」里填：

```
https://<你的公网地址>/wecom/callback
```

### 回调验证机制

- **GET `/wecom/callback`**：企微保存时发来，带 `msg_signature`、`timestamp`、`nonce`、`echostr`。服务端用 `callback_token` 校验签名，用 `callback_aes_key` 解密 `echostr`，返回明文 echostr。
- **POST `/wecom/callback`**：企微推送消息/事件。验签解密后处理，**5 秒内返回 `success`**。

> 签名 = `sha1(sort([callback_token, timestamp, nonce, encrypt]))`；AES-256-CBC，key = `base64decode(EncodingAESKey + "=")`。

### 反向代理 / 内网穿透

常见做法：

- **frp / Lucky**：把宿主机 `HUB_PORT` 暴露到公网，用 HTTPS 反代到容器 8080
- **Cloudflare Tunnel**：`cloudflared` 直接把流量导到本地端口
- **Nginx/Caddy 反代**：公网机器上跑反代，转发到内网容器

无论哪种，最终要保证 `https://<公网域名>/wecom/callback` 能到达容器。

### 企业可信 IP 白名单

企微调用 `qyapi.weixin.qq.com` 取 token 时会校验出口 IP。如果你的服务器出口 IP 不在白名单：

1. 在一台白名单内的机器上跑反代，把 `qyapi.weixin.qq.com` 的路径原样转发
2. 在面板填 `wecom.proxy_url`，例如 `https://wx-proxy.example.com`
3. 服务端会把所有 `qyapi` 请求改成 `{proxy_url}/cgi-bin/...`

## 六、常见故障

### 「接收消息」保存报错

| 症状 | 排查 |
|---|---|
| 企微提示「URL 验证失败」 | 公网到容器链路不通；检查反代/穿透、端口、HTTPS 证书 |
| 签名校验不过 | `wecom.callback_token` 填错；与企微后台生成的 Token 逐字核对 |
| 解密失败 | `wecom.callback_aes_key` 填错；注意末尾不要多空格 |
| 超时 | 企微要求 5 秒内返回；检查容器是否启动完成、`/api/health` 是否 200 |

### 取 token 失败

| 症状 | 排查 |
|---|---|
| `invalid corpid` | `wecom.corp_id` 填错 |
| `invalid grant_type` 或 `40029` | `wecom.secret` 填错或被重置过；到企微后台重新复制 |
| `60020` 不在白名单 | 出口 IP 不在企业可信 IP；填 `wecom.proxy_url` 走反代 |

### 收不到告警

| 排查项 | 检查 |
|---|---|
| `notify.enabled` | 面板里是否 `true` |
| `wecom.touser` | 是否填了正确的 userid；`@all` 是否在应用可见范围内 |
| 探针是否真在失败 | 面板「目标」→ 历史记录看 `ok=0` |
| 是否在静音窗口 | 同一目标同一问题 30 分钟内不重复推 |
| access_token 是否有效 | 面板「设置」→ 测试 |

### 菜单推送失败

- `agentid` 必须是整数，不能是字符串（面板已处理，但 API 直调注意）
- `view` 型按钮的 `url` 不能为空；`panel.public_url` 为空时不生成 `view` 型，只生成 `click` 型

## 七、数据与备份

- SQLite 在 `${HUB_DATA_DIR}/hub.db`，所有配置和历史都在里面
- 备份：`cp ./data/hub.db ./data/hub.db.bak`
- 迁移：停容器 → 拷 `data/` 到新机器 → `docker compose up -d`
- 探测结果按 `log.retain_days` 自动清理

## 八、升级

```bash
docker compose pull
docker compose up -d
```

SQLite schema 用 `CREATE TABLE IF NOT EXISTS`，升级不丢数据。设置项用 `DEFAULT_SETTINGS` 补齐新增键。
