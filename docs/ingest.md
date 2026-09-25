# 外部事件接入（`POST /api/ingest`）

本文档说明如何把**外部脚本/服务**的事件推到 Argus，由它转发成企业微信告警。
典型场景：NAS 上的 `am-watch.sh` 发现某进程挂了、某磁盘 SMART 异常、某容器退出 ——
脚本不必自己实现企微加解密和 API 调用，只需 `curl` 一下 `/api/ingest`，告警由本服务统一发。

## 一、先在面板里配 API Key

`POST /api/ingest` 用一个共享密钥鉴权，不经过面板登录 Cookie。

1. 面板 → **设置** → 找到 `ingest.api_key`
2. 填一个强随机串，例如：
   ```bash
   openssl rand -hex 24
   # 输出形如：a1b2c3d4e5f6...（48 位十六进制）
   ```
3. 保存

> **留空 = 整体禁用**。`ingest.api_key` 为空时，`POST /api/ingest` 一律返回 `403`，不接受任何外部事件。
> 这是为了防止误开通：你不需要外部接入时，攻击面为零。

## 二、接口契约

```
POST /api/ingest
Content-Type: application/json
X-Api-Key: <你的 ingest.api_key>

{
  "source": "am-watch",
  "message": "容器 my-service 已退出（exit 137）",
  "severity": "fail"
}
```

### 请求头

| 头 | 必填 | 说明 |
|---|---|---|
| `X-Api-Key` | 是 | 等于面板里的 `ingest.api_key`；不匹配或缺失返回 `403` |
| `Content-Type` | 是 | `application/json` |

### 请求体（JSON）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `source` | string | 是 | 事件来源标识，例如 `am-watch`、`smartd`、`docker-events`。会写入 `events.source`，也用于告警页显示 |
| `message` | string | 是 | 告警正文，会原样推到企业微信。建议包含：**发生了什么 + 哪台机器 + 什么对象 + 时间** |
| `severity` | string | 否 | `fail`（默认）/ `recovery` / `info`。`fail` 触发企微推送；`recovery` 推送恢复通知；`info` 只入库不推送 |

### 响应

| 状态码 | 响应体 | 说明 |
|---|---|---|
| `200` | `{"ok":true,"event_id":123,"delivered":true}` | 事件已入库 + 企微已推送 |
| `200` | `{"ok":true,"event_id":123,"delivered":false}` | 事件已入库，但处于静音窗口内或 `severity=info`，未推送 |
| `403` | `{"error":"forbidden"}` | `X-Api-Key` 缺失/不匹配，或 `ingest.api_key` 为空 |
| `422` | `{"error":"..."}` | 缺少 `source` 或 `message` |

## 三、curl 示例

### 告警（fail）

```bash
curl -X POST https://hub.example.com/api/ingest \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: a1b2c3d4e5f6..." \
  -d '{
    "source": "am-watch",
    "message": "容器 my-service 已退出（exit 137），宿主机 nas-01",
    "severity": "fail"
  }'
```

成功响应：

```json
{"ok":true,"event_id":42,"delivered":true}
```

### 恢复（recovery）

```bash
curl -X POST https://hub.example.com/api/ingest \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: a1b2c3d4e5f6..." \
  -d '{
    "source": "am-watch",
    "message": "容器 my-service 已恢复运行，宿主机 nas-01",
    "severity": "recovery"
  }'
```

### 仅入库不推送（info）

```bash
curl -X POST https://hub.example.com/api/ingest \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: a1b2c3d4e5f6..." \
  -d '{
    "source": "cron",
    "message": "每日备份完成，大小 12.3 GB",
    "severity": "info"
  }'
```

## 四、在 shell 脚本里调用

把 API Key 存到环境变量，别硬编码进脚本：

```bash
# /etc/argus.env（权限 600，不进 git）
export WOH_INGEST_KEY="a1b2c3d4e5f6..."
export WOH_URL="https://hub.example.com"
```

```bash
#!/bin/sh
# am-watch.sh 片段：发现容器挂了就告警
source /etc/argus.env

NAME="my-service"
STATE=$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null)

if [ "$STATE" != "running" ]; then
  curl -sX POST "$WOH_URL/api/ingest" \
    -H "Content-Type: application/json" \
    -H "X-Api-Key: $WOH_INGEST_KEY" \
    -d "{
      \"source\": \"am-watch\",
      \"message\": \"容器 $NAME 状态异常（${STATE:-不存在}），宿主机 $(hostname)\",
      \"severity\": \"fail\"
    }"
fi
```

> 把上面的 `my-service`、`hub.example.com` 换成你自己的容器名和面板地址。
> 本仓库的代码和文档里不写死任何具体业务名。

## 五、静音与防抖

`POST /api/ingest` 与内部探针告警共享同一套静音机制：

- **全局静音中**：面板或企微菜单点了「静音 N 小时」→ 所有 `fail` 级别的外部事件**只入库（`delivered=false`），不推送**，直到静音到期或被手动解除。`recovery` 级别**不受静音影响**（恢复必发，与内部探针一致）。
- **同源防抖**：同一 `source` + 相同 `severity=fail`，在 `notify.silence_minutes`（默认 30 分钟）内不重复推送，只入库。`recovery` 不防抖。

> 这样设计是为了让外部脚本可以「无脑上报」——即使每分钟都发一遍，也只会在企微里收到一条，不会刷屏。

## 六、数据落库

| 表 | 写什么 |
|---|---|
| `events` | 检测到：`source` = 请求体 `source`，`payload` = 整个请求体 JSON。`GET /api/alerts` 不显示 events，但告警页会显示 `source` 字段标识外部来源 |
| `alerts` | `severity != info` 时写一条：`target_id=NULL`（外部事件不绑定探针目标）、`kind` = `severity`、`message` = 请求体 `message`、`delivered` 视推送结果 |

> 外部事件在告警历史里的标识：`target_id` 为空、`source` 字段为外部来源名。面板告警页据此区分「内部探针告警」和「外部事件告警」。

## 七、安全注意事项

1. **API Key 等同于「能往你的企微群发消息」的权限** —— 保管好，不进 git、不进日志。`.env` 文件权限设 `600`。
2. **用 HTTPS** —— `X-Api-Key` 是明文头，走 HTTP 会被中间人截获。企微回调本身也要求 HTTPS。
3. **限制来源 IP**（可选）—— 如果外部脚本固定在一两台机器上，在反代层（Nginx/Caddy）对 `/api/ingest` 加 IP 白名单，多一层防护。
4. **留空即禁用** —— 不用外部接入时，面板里把 `ingest.api_key` 清空，端点整体 `403`，无攻击面。

## 八、排障

| 症状 | 排查 |
|---|---|
| `403 forbidden` | `ingest.api_key` 为空（端点禁用）；或 `X-Api-Key` 头值不匹配 |
| `422` | 请求体缺 `source` 或 `message`，或不是合法 JSON |
| `delivered=false` 但非 `info` | 全局静音中，或同源防抖窗口内；查面板概览页静音状态 |
| 企微收不到 | 查面板「设置」→「测试」确认企微连通；查容器日志 `告警发送失败` |
| `events` 表有记录但 `alerts` 没有 | `severity=info` 只入 events，不入 alerts，符合预期 |
